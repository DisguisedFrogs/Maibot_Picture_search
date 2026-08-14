#!/usr/bin/env python3
"""存量迁移：image_library/ 根 jsonl 主库的 pixiv 记录 → pixiv.db manual 目录（按画师 userId 分类）。

- 读取 image_library/index.jsonl 中 engine=pixiv 的记录
- 在线调用 pixiv API 获取画师 userId 与 xRestrict（判定 bucket=NSFW/SFW）
- 移动图片文件到 image_library/pixiv/files/（物理单份），写入 pixiv.db（subdir=manual）
- 从 index.jsonl 移除已迁移记录；--dry-run 只统计不落盘
- 幂等可重跑：pixiv.db 按 source_url 去重；API 失败（作品删除等）跳过保留 jsonl

用法示例：
  .venv/bin/python scripts/migrate_pixiv_jsonl_manual.py --dry-run
  PIXIV_PHPSESSID=xxxx .venv/bin/python scripts/migrate_pixiv_jsonl_manual.py
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_ROOT = PROJECT_ROOT / "image_library"
INDEX_FILE = IMAGE_ROOT / "index.jsonl"
PIXIV_ROOT = IMAGE_ROOT / "pixiv"
DB_PATH = PIXIV_ROOT / "pixiv.db"

_ARTWORK_RE = re.compile(r"/(\d{5,})(?:-[0-9a-f]+)?_p\d+\.")


def extract_artwork_id(source_url: str) -> str:
    m = _ARTWORK_RE.search(source_url or "")
    return m.group(1) if m else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="迁移根 jsonl 主库的 pixiv 记录到 pixiv.db manual 目录（按画师分类）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    parser.add_argument("--limit", type=int, default=0, help="处理条数上限，0=全部（默认 0）")
    parser.add_argument("--verbose", action="store_true", help="打印每条迁移明细")
    args = parser.parse_args(argv)

    if not INDEX_FILE.is_file():
        print(f"[migrate] 错误：找不到 {INDEX_FILE}")
        return 1
    if not DB_PATH.is_file():
        print(f"[migrate] 错误：找不到 {DB_PATH}")
        return 1

    from mcp_searxng.config import ServerConfig
    from mcp_searxng.library import ImageLibrary
    from mcp_searxng.network import DownloadManager
    from mcp_searxng.pixiv import PixivClient

    # 借库实例确保 images 表 schema（含 user_id 列）就绪
    ImageLibrary(
        PIXIV_ROOT / "SFW" / "daily",
        backend="sqlite",
        db_path=DB_PATH,
        bucket="SFW",
        subdir="daily",
    )

    lines = [ln for ln in INDEX_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    recs = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if str(rec.get("engine") or "") == "pixiv":
            recs.append(rec)
    if args.limit > 0:
        recs = recs[: args.limit]

    print(f"[migrate] jsonl pixiv 记录 {len(recs)} 条（共 {len(lines)} 行）")

    config = ServerConfig()
    download = DownloadManager(config)
    pixiv = PixivClient(config, download)

    conn = sqlite3.connect(DB_PATH)
    stats = {"migrated": 0, "skipped": 0, "failed": 0}
    pending_lines = list(lines)
    start = time.monotonic()

    for rec in recs:
        source_url = str(rec.get("source_url") or "")
        sha256 = str(rec.get("sha256") or "")
        wid = extract_artwork_id(source_url)
        if not wid or not sha256:
            stats["skipped"] += 1
            print(f"[migrate] 跳过（无法提取作品 ID）: {source_url[:100]}")
            continue
        try:
            body = pixiv.get_illust_detail(wid)
            if not body:
                raise RuntimeError("作品不存在")
            uid = str(body.get("userId") or "").strip() or "0"
            x_restrict = int(body.get("xRestrict") or 0)
        except Exception as exc:
            stats["failed"] += 1
            print(f"[migrate] 失败（API）: {wid} {str(exc)[:120]}")
            continue

        bucket = "NSFW" if x_restrict else "SFW"
        ext = ImageLibrary.mime_suffix(str(rec.get("mime") or "image/jpeg"))
        src = IMAGE_ROOT / f"{sha256}{ext}"
        dst = PIXIV_ROOT / "files" / f"{sha256}{ext}"

        if args.dry_run:
            stats["migrated"] += 1
            if args.verbose:
                print(f"  dry: {wid} uid={uid} {bucket} {src.name} -> manual/{uid}/")
            continue

        try:
            conn.execute(
                "INSERT OR IGNORE INTO images (source_url, sha256, mime, size, query, "
                "engine, rank, rank_mode, rank_date, is_masked, illust_type, fetched_at, "
                "bucket, subdir, illust_id, user_id, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_url, sha256, rec.get("mime", "image/jpeg"), rec.get("size", 0),
                    rec.get("query", ""), "pixiv", rec.get("rank"), rec.get("rank_mode"),
                    rec.get("rank_date"), rec.get("is_masked", 0), rec.get("illust_type", 0),
                    rec.get("fetched_at", time.time()), bucket, "manual", wid, uid,
                    rec.get("description"),
                ),
            )
            conn.commit()
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.move(str(src), str(dst))
            elif not dst.is_file():
                raise RuntimeError(f"源文件缺失: {src.name}")
            pending_lines = [ln for ln in pending_lines if sha256 not in ln]
            stats["migrated"] += 1
            if args.verbose:
                print(f"  ok: {wid} uid={uid} {bucket} -> manual/{uid}/{src.name}")
        except Exception as exc:
            conn.rollback()
            stats["failed"] += 1
            print(f"[migrate] 失败（迁移）: {wid} {str(exc)[:120]}")

    if not args.dry_run and stats["migrated"]:
        INDEX_FILE.write_text("\n".join(pending_lines) + ("\n" if pending_lines else ""), encoding="utf-8")
    conn.close()

    cost = time.monotonic() - start
    print(
        f"[migrate] 完成 迁移={stats['migrated']} 跳过={stats['skipped']} 失败={stats['failed']}"
        f" | 用时 {cost:.1f}s{'（dry-run）' if args.dry_run else ''}"
    )
    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
