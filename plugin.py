"""maibot-mood：按群跟踪聊天心情（LLM 分析 + 文件持久化）。

供模型调用 get_maibot_mood 获取当前聊天流的心情档位（差/中/好），
用于"按心情从本地 Pixiv 榜库选图发图"流程。

持久化：每次分析结果写入运行时注入的统一持久化目录
data/plugins/<plugin_id>/mood_store.json（原子写），插件/进程重启后心情
不丢失；加载时若发现旧式插件源码目录 data/mood_store.json 则一次性迁移，
成功后删除旧文件。无新消息直接复用持久化值，新增消息不足门槛时复用等待
累计，LLM 失败时回退到上次持久化的心情，避免情绪归零。

更新细化：发图意愿新旧值加权平滑防跳变；心情档位切换需连续多次分析一致
才生效（防抖）；输出附较上次意愿变化趋势。
"""

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

LEVEL_CN = {"good": "好", "neutral": "中", "bad": "差"}

RANK_SUBDIR_ORDER = {"daily": 0, "weekly": 1, "monthly": 2, "manual": 3}
RANK_SUBDIR_LABELS = {"daily": "日榜", "weekly": "周榜", "monthly": "月榜", "manual": "手动"}


def _parse_epoch_ts(value: Any) -> Optional[float]:
    """将消息 timestamp（字符串形式的 epoch 秒）解析为 float，失败返回 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

DEFAULT_PROMPT_TEMPLATE = """你是聊天氛围分析师。请分析下面这段群聊消息，判断当前聊天氛围处于哪个心情档位，以及当前氛围下发图的合适程度。

消息：
{messages}

严格只输出一个 JSON 对象，不要输出其他任何内容：
{{"level": "good" 或 "neutral" 或 "bad", "reason": "一句话理由，不超过20字", "keywords": ["关键词1", "关键词2", "关键词3"], "willingness": 0到100的整数}}

level 定义：
- good：氛围轻松、愉快、兴奋、满意
- neutral：平淡、日常、普通交流
- bad：低落、烦躁、疲惫、冲突、压抑

willingness 定义（0-100 的整数）：当前氛围下发图（分享图片）的合适程度与自然度。
- 话题与图片/视觉相关（晒图、求图、聊二次元/壁纸/照片）、情绪起伏明显、氛围活跃轻松时高（70 以上）
- 话题与图无关但氛围平淡、或氛围低落压抑时低（40 以下）
- 其余情况取中间值（40-69）
"""


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "heart"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.4.0", description="配置版本")


class MoodConfig(PluginConfigBase):
    """心情分析配置。"""

    __ui_label__ = "心情分析"
    __ui_icon__ = "activity"
    __ui_order__ = 1

    window: int = Field(default=20, description="分析最近多少条消息")
    min_new_messages: int = Field(default=3, description="距上次分析新增多少条消息才触发重新分析")
    willingness_alpha: float = Field(default=0.5, description="发图意愿平滑系数：新分析结果权重（0-1，越大响应越快）")
    level_confirm_times: int = Field(default=2, description="心情档位切换需连续多少次分析一致才生效（防抖）")
    model: str = Field(default="", description="分析所用模型名，留空使用默认模型")
    max_keywords: int = Field(default=5, description="语境关键词数量上限")
    prompt_template: str = Field(default=DEFAULT_PROMPT_TEMPLATE, description="分析 Prompt 模板，{messages} 为消息占位符")
    pixiv_db_path: str = Field(
        default="",
        description="Pixiv 图库数据库路径（pixiv.db），用于配置页展示图库图片存储数量",
    )


class MaibotMoodConfig(PluginConfigBase):
    """maibot-mood 插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    mood: MoodConfig = Field(default_factory=MoodConfig)


class MoodStore:
    """心情持久化存储：data/mood_store.json，原子写入（tmp + rename）。

    记录结构（按 stream_id 索引）：
    {level, reason, keywords, willingness, delta, analyzed_at, last_message_ts,
     message_count, pending_level, pending_count, updated_at}
    """

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def get(self, stream_id: str) -> Optional[dict]:
        if not stream_id:
            return None
        with self._lock:
            record = self._data.get(stream_id)
            return dict(record) if record else None

    def set(self, stream_id: str, record: dict) -> None:
        if not stream_id:
            return
        with self._lock:
            self._data[stream_id] = dict(record)
            self._save_locked()

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            pass

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._data)


class MaibotMoodPlugin(MaiBotPlugin):
    """按群 LLM 分析聊天心情并持久化的插件。"""

    config_model = MaibotMoodConfig

    async def on_load(self) -> None:
        """加载持久化的心情数据（含旧式数据目录的一次性迁移）。"""
        store_path = self._resolve_store_path()
        self._store = MoodStore(store_path)
        self._get_logger().info(f"maibot-mood 加载完成，已恢复 {self._store.size} 个聊天流的心情记录")

    def _resolve_store_path(self) -> Path:
        """解析持久化存储路径：优先运行时注入的统一持久化目录。

        Runner 在调用 on_load 前已注入 ctx（含 paths.data_dir，即
        data/plugins/<plugin_id>/），旧版 SDK 也会被补挂 ctx.paths；
        仅当解析失败时回退到旧式插件源码目录 data/。
        """
        try:
            data_dir = Path(self.ctx.paths.data_dir)
        except Exception:
            return self._legacy_store_file()
        target = data_dir / "mood_store.json"
        if not target.is_file():
            old_id_file = self._old_id_store_file(data_dir)
            if old_id_file.is_file():
                self._migrate_legacy_store(old_id_file, target)
            if not target.is_file():
                legacy = self._legacy_store_file()
                if legacy.is_file():
                    self._migrate_legacy_store(legacy, target)
                if not target.is_file():
                    return legacy
        else:
            self._cleanup_legacy_dir()
        return target

    @staticmethod
    def _old_id_store_file(data_dir: Path) -> Path:
        """旧插件 ID 目录下的持久化文件（local.maibot-mood → github.DisguisedFrogs.maibot-mood 变更前）。"""
        return data_dir.parent / "local.maibot-mood" / "mood_store.json"

    @staticmethod
    def _legacy_store_file() -> Path:
        return Path(__file__).resolve().parent / "data" / "mood_store.json"

    def _migrate_legacy_store(self, legacy: Path, target: Path) -> None:
        """一次性迁移旧式 mood_store.json 到统一持久化目录，成功后删除旧文件与空目录。"""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
            legacy.unlink()
        except OSError as exc:
            self._get_logger().warning(f"旧式心情数据迁移失败，继续使用旧路径: {exc}")
            return
        try:
            legacy.parent.rmdir()
        except OSError:
            pass
        self._get_logger().info(f"已迁移旧式心情数据 {legacy} → {target}")

    def _cleanup_legacy_dir(self) -> None:
        """迁移后清理旧式数据目录（仅删除空目录，避免运行时继续提示迁移）。"""
        try:
            self._legacy_store_file().parent.rmdir()
        except OSError:
            pass

    async def on_unload(self) -> None:
        """卸载时无需额外保存（每次分析后已原子落盘）。"""

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载：无需额外处理（分析参数每次读取最新配置）。"""

        del scope
        del config_data
        del version

    # ===== WebUI 图库信息 =====

    @staticmethod
    def _pixiv_stats(db_path: str) -> Optional[dict]:
        """统计 Pixiv 图库图片存储数量：数据库计数 + 各榜明细 + 物理文件数。

        数据库缺失/不可读时返回 None（由调用方展示提示文案）。
        返回 {"total": int, "buckets": {("SFW", "daily"): int, ...}, "files": int|None}。
        """
        if not db_path:
            return None
        db_file = Path(db_path)
        if not db_file.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            try:
                total = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
                rows = conn.execute(
                    "SELECT bucket, subdir, COUNT(*) FROM images GROUP BY bucket, subdir"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        buckets = {(str(bucket), str(subdir)): int(count) for bucket, subdir, count in rows}
        files_dir = db_file.parent / "files"
        try:
            files_count = (
                sum(1 for p in files_dir.iterdir() if p.is_file()) if files_dir.is_dir() else None
            )
        except OSError:
            files_count = None
        return {"total": int(total), "buckets": buckets, "files": files_count}

    @staticmethod
    def _format_pixiv_stats(stats: dict) -> str:
        """组装图库计数摘要文案（单行，配置页节描述展示）。"""
        buckets = stats.get("buckets") or {}
        total = int(stats.get("total") or 0)

        def group_text(bucket: str) -> str:
            items = []
            for (b, subdir), count in buckets.items():
                if b != bucket:
                    continue
                label = RANK_SUBDIR_LABELS.get(subdir, subdir)
                items.append((RANK_SUBDIR_ORDER.get(subdir, 99), f"{label}{count}"))
            items.sort(key=lambda item: item[0])
            return "/".join(text for _, text in items)

        parts = [f"图库图片总数 {total}"]
        for bucket in ("SFW", "NSFW"):
            count = sum(c for (b, _), c in buckets.items() if b == bucket)
            inner = group_text(bucket)
            parts.append(f"{bucket} {count}" + (f"：{inner}" if inner else ""))
        files_count = stats.get("files")
        if files_count is not None:
            parts.append(f"物理文件 {files_count}")
        return "；".join(parts)

    def get_webui_config_schema(self, **kwargs: Any) -> dict:
        """在配置页注入只读「图库信息」节，展示相关图片存储数量。

        每次打开配置页都会重新生成 Schema，计数保持实时；图库缺失或读取
        失败时描述显示提示文案，不影响其余配置节。
        """
        try:
            schema = super().get_webui_config_schema(**kwargs)
        except Exception:
            return {}
        try:
            db_path = str(getattr(self.config.mood, "pixiv_db_path", "") or "")
        except Exception:
            db_path = ""
        stats = self._pixiv_stats(db_path)
        description = (
            self._format_pixiv_stats(stats)
            if stats
            else "未找到图库数据库（pixiv.db），请在「心情分析」中配置 pixiv_db_path"
        )
        sections = schema.get("sections")
        if isinstance(sections, dict):
            sections["image_library"] = {
                "name": "image_library",
                "title": "图库信息",
                "description": description,
                "icon": "image",
                "collapsed": False,
                "order": 99,
                "fields": {},
            }
        return schema

    # ===== 工具组件 =====

    @Tool(
        "get_maibot_mood",
        description="获取当前聊天流的心情档位（好/中/差）及简短理由与语境关键词。"
        "用于需要根据聊天氛围决定行为（如选图发图）时。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="context_hint",
                param_type=ToolParamType.STRING,
                description="可选。对当前氛围的补充描述，供分析参考。",
                required=False,
            ),
        ],
    )
    async def handle_get_maibot_mood(self, context_hint: str = "", **kwargs: Any):
        """返回当前聊天流的心情（差/中/好 + 理由 + 关键词）。"""
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not stream_id:
            return {
                "name": "get_maibot_mood",
                "content": "无法获取当前聊天流上下文（未注入 stream_id），心情暂按「中」处理。",
            }

        cfg = self.config.mood
        recent, latest_ts, ts_list = await self._fetch_recent_text(stream_id, int(cfg.window or 20))
        message_count = len(recent)

        record = self._store.get(stream_id)
        if not record:
            pass
        elif latest_ts is not None:
            last_ts = record.get("last_message_ts")
            if last_ts is None:
                # 旧记录缺时间戳：无法判断新增量，重新分析一次回填
                pass
            elif latest_ts <= float(last_ts):
                # 窗口内无新消息：直接复用持久化心情
                return self._format_result(record, cached=True, new_count=0)
            else:
                # 窗口内有新消息：按门槛决定是否重分析（不足则复用，等待累计）
                new_count = sum(1 for t in ts_list if t is not None and t > float(last_ts))
                if new_count < int(cfg.min_new_messages or 3):
                    return self._format_result(record, cached=True, new_count=new_count)
        else:
            # 拿不到消息时间戳：无法判断是否有新消息，复用持久化心情
            return self._format_result(record, cached=True)

        if not recent:
            # 拿不到消息文本：优先复用持久化心情，避免情绪归零
            if record:
                return self._format_result(record, cached=True)
            return {
                "name": "get_maibot_mood",
                "content": "当前聊天心情：中（暂时无法读取聊天记录，按中性处理）",
            }

        analyzed = await self._analyze(stream_id, recent, message_count, latest_ts, context_hint, prev_record=record)
        if analyzed is None and record:
            # LLM 分析失败：回退到上次持久化的心情
            return self._format_result(record, cached=True, degraded=True)
        if analyzed is None:
            return {
                "name": "get_maibot_mood",
                "content": "当前聊天心情：中（分析失败，按中性处理）",
            }
        return self._format_result(analyzed)

    async def _fetch_recent_text(self, stream_id: str, limit: int) -> tuple[list[str], Optional[float], list[Optional[float]]]:
        """读取最近消息并转成文本行（容错：字段缺失/异常都跳过）。

        host 侧 message.get_recent 返回 {"success": bool, "messages": [...]}，
        消息文本位于 processed_plain_text（可选）或 raw_message 的 text 段。
        返回 (文本行列表, 窗口内最新消息时间戳, 与文本行对齐的时间戳列表)，
        时间戳解析失败时为 None。
        """
        try:
            result = await self.ctx.message.get_recent(chat_id=stream_id, limit=limit)
        except Exception as exc:
            self._get_logger().warning(f"读取聊天记录失败: {exc}")
            return [], None, []

        if isinstance(result, list):
            items = result
        elif isinstance(result, dict) and result.get("success"):
            items = result.get("messages") or []
        else:
            return [], None, []
        texts: list[str] = []
        ts_list: list[Optional[float]] = []
        latest_ts: Optional[float] = None
        for msg in items:
            if not isinstance(msg, dict):
                continue
            text = str(msg.get("processed_plain_text") or "").strip()
            if not text:
                raw = msg.get("raw_message")
                if isinstance(raw, list):
                    parts = [
                        str(seg.get("data", "")).strip()
                        for seg in raw
                        if isinstance(seg, dict) and seg.get("type") == "text"
                    ]
                    text = " ".join(p for p in parts if p)
            ts = _parse_epoch_ts(msg.get("timestamp"))
            if text:
                texts.append(text[:120])
                ts_list.append(ts)
            if ts is not None and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
        return texts, latest_ts, ts_list

    async def _analyze(
        self,
        stream_id: str,
        texts: list[str],
        message_count: int,
        latest_ts: Optional[float],
        context_hint: str,
        prev_record: Optional[dict] = None,
    ) -> Optional[dict]:
        """调用 LLM 分析心情，做平滑/防抖处理后持久化。"""
        cfg = self.config.mood
        messages_text = "\n".join(texts)
        if context_hint:
            messages_text += f"\n（补充描述：{context_hint[:100]}）"
        if len(messages_text) > 4000:
            messages_text = messages_text[-4000:]

        prompt = (cfg.prompt_template or DEFAULT_PROMPT_TEMPLATE).replace("{messages}", messages_text)
        try:
            result = await self.ctx.llm.generate(
                prompt,
                model=str(cfg.model or "").strip(),
                temperature=0.2,
                max_tokens=256,
            )
        except Exception as exc:
            self._get_logger().warning(f"心情分析 LLM 调用失败: {exc}")
            return None

        if result.get("success") is False or result.get("error"):
            err = str(result.get("error") or result.get("response") or "").strip()
            self._get_logger().warning(f"心情分析 LLM 生成失败: {err}")
            return None

        raw = str(result.get("response") or result.get("content") or "").strip()
        parsed = self._parse_level_json(raw)
        if parsed is None:
            self._get_logger().warning(f"心情分析 JSON 解析失败: {raw[:200]}")
            return None

        new_level = str(parsed.get("level") or "neutral").strip().lower()
        new_willingness = self._coerce_willingness(parsed.get("willingness"))

        # 发图意愿平滑：新值与旧值加权，避免跳变
        prev_willingness = None
        if prev_record:
            prev_willingness = self._coerce_willingness(prev_record.get("willingness"))
        if prev_willingness is None:
            willingness = new_willingness
            delta = 0
        else:
            alpha = max(0.0, min(1.0, float(cfg.willingness_alpha if cfg.willingness_alpha is not None else 0.5)))
            willingness = round(alpha * new_willingness + (1 - alpha) * prev_willingness)
            delta = willingness - prev_willingness

        # 心情档位防抖：新档位需连续多次分析一致才切换
        level, reason, level_keywords, pending_level, pending_count = self._apply_level_hysteresis(
            prev_record, new_level, parsed, cfg
        )

        record = {
            "level": level,
            "reason": reason,
            "keywords": level_keywords,
            "willingness": willingness,
            "delta": delta,
            "analyzed_at": time.time(),
            "last_message_ts": latest_ts,
            "message_count": message_count,
            "pending_level": pending_level,
            "pending_count": pending_count,
            "updated_at": time.time(),
        }
        self._store.set(stream_id, record)
        return record

    def _apply_level_hysteresis(
        self,
        prev_record: Optional[dict],
        new_level: str,
        parsed: dict,
        cfg: MoodConfig,
    ) -> tuple[str, str, list, Optional[str], int]:
        """心情档位防抖：新档位需连续确认 level_confirm_times 次才生效。

        返回 (生效档位, 理由, 关键词, pending_level, pending_count)。
        pending 期间理由/关键词沿用当前档位旧值，保证输出自洽。
        """
        confirm_times = max(1, int(cfg.level_confirm_times or 2))
        new_reason = str(parsed.get("reason") or "")[:60]
        new_keywords = [str(k)[:20] for k in (parsed.get("keywords") or [])][: int(cfg.max_keywords or 5)]

        prev_level = None
        if prev_record:
            prev_level = str(prev_record.get("level") or "").strip().lower()
            if prev_level not in LEVEL_CN:
                prev_level = None

        if prev_level is None or prev_level == new_level:
            # 首次分析或档位一致：直接生效，pending 清零
            return new_level, new_reason, new_keywords, None, 0

        old_reason = str(prev_record.get("reason") or "")
        old_keywords = prev_record.get("keywords") or []
        pending = str(prev_record.get("pending_level") or "").strip().lower() or None
        try:
            pending_count = int(prev_record.get("pending_count") or 0)
        except (TypeError, ValueError):
            pending_count = 0

        if pending == new_level:
            # 与上次待确认档位一致：计数累加，达到阈值才切换
            pending_count += 1
            if pending_count >= confirm_times:
                return new_level, new_reason, new_keywords, None, 0
            return prev_level, old_reason, old_keywords, pending, pending_count

        # 换了个新档位：重置待确认状态
        return prev_level, old_reason, old_keywords, new_level, 1

    @staticmethod
    def _coerce_willingness(value: Any) -> int:
        """将 LLM 输出的意愿值规范为 0-100 整数，缺失/非法默认 60。"""
        try:
            num = int(float(value))
        except (TypeError, ValueError):
            return 60
        return max(0, min(100, num))

    @staticmethod
    def _json_candidates(text: str):
        """逐字符扫描提取平衡花括号 JSON 候选块（跳过字符串内的 {} 与转义）。"""
        start: Optional[int] = None
        depth = 0
        in_str = False
        esc = False
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start : i + 1]
                    start = None

    @classmethod
    def _parse_level_json(cls, raw: str) -> Optional[dict]:
        """从 LLM 输出中提取 JSON 对象并校验档位（宽容：多候选块/尾逗号/错误文本）。"""
        if not raw:
            return None
        text = raw.strip()
        if text.startswith("生成内容时出错"):
            return None
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        for candidate in cls._json_candidates(text):
            cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                data = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError):
                continue
            level = str(data.get("level") or "").strip().lower()
            if level not in LEVEL_CN:
                continue
            return data
        return None

    @staticmethod
    def _willingness_band(score: int) -> str:
        """意愿分数转档位：<40 低 / 40-69 中 / ≥70 高。"""
        if score >= 70:
            return "高"
        if score >= 40:
            return "中"
        return "低"

    def _format_result(
        self,
        record: dict,
        cached: bool = False,
        degraded: bool = False,
        new_count: Optional[int] = None,
    ) -> dict:
        """组装返回给模型的文本。"""
        level = str(record.get("level") or "neutral").strip().lower()
        if level not in LEVEL_CN:
            level = "neutral"
        level_cn = LEVEL_CN[level]
        reason = str(record.get("reason") or "").strip()
        keywords = record.get("keywords") or []
        willingness = self._coerce_willingness(record.get("willingness"))

        parts = [f"当前聊天心情：{level_cn}"]
        parts.append(self._format_willingness(record))
        if reason:
            parts.append(f"理由：{reason}")
        if keywords:
            parts.append(f"语境关键词：{'、'.join(str(k) for k in keywords)}")
        if cached:
            if new_count is None:
                parts.append("（基于近期分析结果）")
            elif new_count > 0:
                parts.append(f"（新增 {new_count} 条，未触发重分析）")
            else:
                parts.append("（窗口内无新消息）")
        if degraded:
            parts.append("（LLM 分析暂不可用，沿用上次心情）")
        return {"name": "get_maibot_mood", "content": "，".join(parts)}

    @staticmethod
    def _format_willingness(record: dict) -> str:
        """组装发图意愿文案，附较上次变化趋势（有 delta 时）。"""
        score = MaibotMoodPlugin._coerce_willingness(record.get("willingness"))
        text = f"发图意愿：{MaibotMoodPlugin._willingness_band(score)}（{score}/100"
        delta = record.get("delta")
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            delta = None
        if delta is not None:
            if delta > 0:
                text += f"，较上次 ↑{delta}"
            elif delta < 0:
                text += f"，较上次 ↓{abs(delta)}"
            else:
                text += "，较上次持平"
        return text + "）"


def create_plugin() -> MaibotMoodPlugin:
    """创建 maibot-mood 插件实例。"""

    return MaibotMoodPlugin()
