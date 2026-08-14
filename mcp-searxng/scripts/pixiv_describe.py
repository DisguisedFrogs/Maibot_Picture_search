#!/usr/bin/env python3
"""Pixiv 榜库图片描述预生成（dashscope VLM）。

- 扫描 image_library/pixiv/pixiv.db 中 description 为空的图片记录（SFW + NSFW）
- 按 sha256 定位图片文件，并发调用 dashscope OpenAI 兼容 API 生成描述并写回
- 幂等可重跑：只处理无描述记录；单张失败重试后跳过；R18 图被内容审核
  拒绝（DataInspectionFailed）时本次放弃该图（不计失败），下次运行自动重试
- 由 systemd timer（mcp-searxng-pixiv-desc）每日触发；手动运行可回填存量
- 凭证：--api-key/--base-url > DASHSCOPE_API_KEY/DASHSCOPE_BASE_URL env
  > MaiBot config/model_config.toml 的 Alibaba provider（base_url + api_key）
- 模型：--model > MaiBot「为模型分配功能」配置的 VLM 模型
  （model_task_config.vlm）；未配置时提醒并退出
"""

import argparse
import base64
import io
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import httpx

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIXIV_ROOT = PROJECT_ROOT / "image_library" / "pixiv"
DEFAULT_DB = PIXIV_ROOT / "pixiv.db"
FILES_DIR = PIXIV_ROOT / "files"
MAIBOT_CONFIG = Path.home() / "MaiBot" / "config" / "model_config.toml"
MAIBOT_PROMPT = Path.home() / "MaiBot" / "prompts" / "zh-CN" / "image_description.prompt"

DEFAULT_PROMPT = (
    "请用中文详细描述这张图片的内容。如果有文字，请把文字描述概括出来，"
    "请留意其主题、直观感受，输出为一段平文本，最多100字，请注意不要分点，"
    "就输出一段文本"
)
DEFAULT_TIMEOUT = 60.0
MAX_RETRY = 2
MAX_DATA_BYTES = 12 * 1024 * 1024  # dashscope data-uri 限制按 base64 字符串计（20M 字符 ≈ 15MB 二进制），留余量

REJECTED_MARKER = "DataInspectionFailed"  # dashscope 输出内容审核拒绝（本次放弃，下次运行自动重试）


def is_moderation_rejection(message: str) -> bool:
    """判断错误是否为内容审核拒绝（本次运行放弃该图，不重试、不计失败）。"""
    return REJECTED_MARKER in message


def compress_for_vlm(raw: bytes) -> bytes:
    """图片字节超过上限时用 PIL 缩放/重编码压缩到上限内；失败原样返回。"""
    if len(raw) <= MAX_DATA_BYTES:
        return raw
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGB")
        img.thumbnail((2048, 2048))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as exc:
        print(f"[pixiv-describe] 提示：大图压缩失败，按原图重试: {exc}")
        return raw


def load_mai_maibot_provider() -> dict:
    """从 MaiBot model_config.toml 读取 Alibaba provider 的 base_url/api_key 与 VLM 模型名。

    模型名取「为模型分配功能」（[model_task_config.vlm]）配置的视觉模型：
    model_list 首项（name）→ [[models]] 匹配 name 取 model_identifier；
    无匹配时直接用该名字（兼容 name == model_identifier）。
    """
    if tomllib is None:
        return {}
    try:
        with MAIBOT_CONFIG.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    result: dict = {}
    for provider in data.get("api_providers") or []:
        if str(provider.get("name") or "").strip() == "Alibaba":
            base_url = str(provider.get("base_url") or "").strip()
            api_key = str(provider.get("api_key") or "").strip()
            if base_url and api_key:
                result = {"base_url": base_url, "api_key": api_key}
                break
    models = data.get("models") or []
    vlm_task = (data.get("model_task_config") or {}).get("vlm") or {}
    for model_name in vlm_task.get("model_list") or []:
        name = str(model_name).strip()
        if not name:
            continue
        for model in models:
            if str(model.get("name") or "").strip() == name:
                identifier = str(model.get("model_identifier") or "").strip()
                if identifier:
                    result["model"] = identifier
                    return result
        result["model"] = name
        return result
    return result


def load_prompt(prompt_file: str | None) -> str:
    if prompt_file:
        path = Path(prompt_file)
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        print(f"[pixiv-describe] 提示：找不到 --prompt-file {path}，使用默认 Prompt")
    if MAIBOT_PROMPT.is_file():
        return MAIBOT_PROMPT.read_text(encoding="utf-8").strip()
    return DEFAULT_PROMPT


def build_file_index(root: Path) -> dict[str, Path]:
    """扫描共享物理目录 files/，建立 {sha256: Path} 映射（内容命名天然单份）。"""
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for f in root.iterdir():
        if f.is_file():
            index[f.stem] = f
    return index


def fetch_pending(db: Path, only_bucket: str | None, limit: int) -> list[dict]:
    """扫描 description 为空的记录，按 sha256 去重。"""
    conn = sqlite3.connect(db)
    try:
        sql = (
            "SELECT source_url, sha256, mime, bucket, subdir, description "
            "FROM images "
            "WHERE (description IS NULL OR trim(description) = '')"
        )
        params: list = []
        if only_bucket:
            sql += " AND bucket = ?"
            params.append(only_bucket)
        sql += " ORDER BY fetched_at ASC"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        cols = ["source_url", "sha256", "mime", "bucket", "subdir", "description"]
        records = [dict(zip(cols, row)) for row in rows]
        deduped: dict[str, dict] = {}
        for rec in records:
            deduped.setdefault(rec["sha256"], rec)
        return list(deduped.values())
    finally:
        conn.close()


def describe_one(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    raw: bytes,
    mime: str,
) -> str:
    """调用一次 VLM，返回描述文本；失败抛异常由调用方重试。"""
    image_mime = mime if mime.startswith("image/") else "image/jpeg"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image_mime};base64,"
                            f"{base64.b64encode(raw).decode()}"
                        },
                    },
                ],
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
    }
    resp = client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"无 choices: {str(data)[:200]}")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("空描述")
    return content.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pixiv 榜库图片描述预生成（VLM 批量）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="pixiv.db 路径（默认 image_library/pixiv/pixiv.db）")
    parser.add_argument("--limit", type=int, default=0, help="处理条数上限，0=全部（默认 0）")
    parser.add_argument("--concurrency", type=int, default=4, help="并发请求数（默认 4）")
    parser.add_argument("--only-bucket", choices=["SFW", "NSFW"], default=None, help="只处理指定 bucket")
    parser.add_argument("--dry-run", action="store_true", help="只统计待处理记录，不调用 API")
    parser.add_argument("--prompt-file", default=None, help="覆盖描述 Prompt 文件")
    parser.add_argument("--api-key", default="", help="覆盖 API Key（优先于环境变量与 MaiBot 配置）")
    parser.add_argument("--base-url", default="", help="覆盖 API Base URL")
    parser.add_argument("--model", default=None, help="覆盖模型名（默认取 MaiBot「为模型分配功能」配置的 VLM 模型）")
    parser.add_argument("--verbose", action="store_true", help="打印每条描述")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"[pixiv-describe] 错误：找不到数据库 {db}")
        return 1

    records = fetch_pending(db, args.only_bucket, args.limit)
    file_index = build_file_index(FILES_DIR)
    ready = []
    for rec in records:
        path = file_index.get(rec["sha256"])
        if path is None:
            rec["missing_file"] = True
            continue
        rec["path"] = path
        ready.append(rec)

    no_file = len(records) - len(ready)
    log_file = PIXIV_ROOT / "describe_run.log"
    PIXIV_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    summary = (
        f"[pixiv-describe] 待处理 {len(records)} 张"
        f"（无文件 {no_file}）| bucket={args.only_bucket or 'ALL'}"
    )
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"{timestamp} {summary}\n")
    print(summary)

    if args.dry_run or not ready:
        return 0

    provider = load_mai_maibot_provider()
    api_key = args.api_key or os.environ.get("DASHSCOPE_API_KEY", "") or provider.get("api_key", "")
    base_url = args.base_url or os.environ.get("DASHSCOPE_BASE_URL", "") or provider.get("base_url", "")
    model = args.model or provider.get("model", "")
    if not api_key or not base_url:
        print(
            "[pixiv-describe] 错误：缺少 API Key/Base URL。请设置 DASHSCOPE_API_KEY "
            "环境变量，或确认 MaiBot config/model_config.toml 中存在 Alibaba provider"
        )
        return 1
    if not model:
        print(
            "[pixiv-describe] 错误：未配置 VLM 模型。请在 MaiBot「为模型分配功能」"
            "中配置 VLM 任务（model_task_config.vlm），或使用 --model 参数指定模型"
        )
        return 1
    prompt = load_prompt(args.prompt_file)

    client = httpx.Client(timeout=DEFAULT_TIMEOUT)
    lock = threading.Lock()
    stats = {"success": 0, "fail": 0, "rejected": 0}
    failed: list[str] = []

    def worker(rec: dict) -> None:
        raw = rec["path"].read_bytes()
        compressed = compress_for_vlm(raw)
        mime = "image/jpeg" if compressed is not raw else str(rec.get("mime") or "")
        desc = ""
        for attempt in range(1 + MAX_RETRY):
            try:
                desc = describe_one(
                    client, base_url, api_key, model, prompt,
                    compressed, mime,
                )
                break
            except Exception as exc:
                if is_moderation_rejection(str(exc)):
                    with lock:
                        stats["rejected"] += 1
                    return
                if attempt >= MAX_RETRY:
                    with lock:
                        stats["fail"] += 1
                        failed.append(f"{rec['sha256'][:8]} ({str(exc)[:100]})")
                    return
                time.sleep(1.5 * (attempt + 1))
        with lock:
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE images SET description = ? WHERE sha256 = ?",
                    (desc, rec["sha256"]),
                )
                conn.commit()
            finally:
                conn.close()
            stats["success"] += 1
            if args.verbose:
                print(f"  {rec['sha256'][:8]} [{rec['bucket']}] {desc}")

    threads = []
    for i in range(0, len(ready), args.concurrency):
        batch = ready[i : i + args.concurrency]
        workers = [threading.Thread(target=worker, args=(rec,)) for rec in batch]
        for t in workers:
            t.start()
        for t in workers:
            t.join()

    done = time.strftime("%Y-%m-%d %H:%M:%S")
    tail = (
        f"[pixiv-describe] 完成 success={stats['success']} "
        f"rejected={stats['rejected']} fail={stats['fail']} no_file={no_file}"
    )
    if failed:
        tail += " | failed: " + "; ".join(failed[:20])
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"{done} {tail}\n")
    print(tail)
    if stats["rejected"]:
        print(f"[pixiv-describe] {stats['rejected']} 张图被内容审核拒绝，本次放弃，下次运行自动重试")
    return 0 if stats["fail"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
