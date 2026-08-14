"""图片库：内容 SHA256 命名 + source_url 索引，双重去重。

双后端：
- backend="jsonl"：主库（web 图）保持 jsonl 追加索引，行为不变
- backend="sqlite"：pixiv 子库，SQLite 单库（WAL，支持多进程并发），
  images 表按 source_url 去重；图片文件物理单份存于共享目录 files/
  （bucket/subdir 仅作逻辑元数据）
"""

import hashlib
import json
import math
import random
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from .config import ServerConfig


def pixiv_mode_parts(mode: str) -> tuple[str, str]:
    """Pixiv 榜 mode（daily/weekly/monthly/daily_r18/...）→ (bucket, subdir)。

    bucket ∈ {SFW, NSFW}，subdir ∈ {daily, weekly, monthly}。
    """
    r18 = "r18" in mode
    for sub in ("daily", "weekly", "monthly"):
        if mode.startswith(sub):
            return ("NSFW" if r18 else "SFW"), sub
    raise ValueError(f"未知 mode: {mode}")


class ImageLibrary:
    MIME_BY_SUFFIX = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".avif": "image/avif",
        ".svg": "image/svg+xml",
    }
    EXT_BY_MIME = {mime: suffix for suffix, mime in MIME_BY_SUFFIX.items()}

    def __init__(
        self,
        directory: Path = ServerConfig.image_library_dir,
        backend: str = "jsonl",
        db_path: Path | None = None,
        bucket: str = "",
        subdir: str = "",
    ):
        self.directory = directory
        self.backend = backend
        self.bucket = bucket
        self.subdir = subdir
        self._lock = threading.Lock()
        if backend == "sqlite":
            directory.mkdir(parents=True, exist_ok=True)
            self._db_path = db_path or (directory.parent / "pixiv.db")
            self._storage_root = Path(self._db_path).parent / "files"
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS images (
                    source_url TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    rank INTEGER,
                    rank_mode TEXT,
                    rank_date TEXT,
                    is_masked INTEGER DEFAULT 0,
                    illust_type INTEGER DEFAULT 0,
                    fetched_at REAL NOT NULL,
                    bucket TEXT DEFAULT 'SFW',
                    subdir TEXT DEFAULT 'daily',
                    illust_id TEXT,
                    user_id TEXT,
                    description TEXT
                )
                """
            )
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(images)")}
            if "description" not in cols:
                self._conn.execute(
                    "ALTER TABLE images ADD COLUMN description TEXT"
                )
            if "user_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE images ADD COLUMN user_id TEXT"
                )
            self._conn.commit()
        else:
            self._index_file = directory / "index.jsonl"
            self._url_to_libpath: dict[str, str] = {}
            self._records: list[dict] = []
            self._load_index_jsonl()

    # ===== jsonl 后端（主库） =====

    def _load_index_jsonl(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self._index_file.exists():
            return
        with self._index_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                source_url = rec.get("source_url")
                sha256 = rec.get("sha256")
                if source_url and sha256:
                    self._url_to_libpath[source_url] = (
                        f"{sha256}{self.mime_suffix(rec.get('mime') or 'image/jpeg')}"
                    )
                self._records.append(rec)

    def search_by_query_prefix(self, prefix: str, limit: int = 0) -> list[dict]:
        """按 query 前缀检索图库记录（含文件相对路径）。

        limit > 0 时随机取至多 limit 条；limit <= 0 返回全部匹配。
        返回记录含：source_url / query / fetched_at / engine / bucket / subdir
        与 "lib_rel"（文件名）。
        """
        if self.backend == "sqlite":
            sql = "SELECT * FROM images WHERE query LIKE ?"
            params: list = [prefix + "%"]
            if self.bucket:
                sql += " AND bucket = ?"
                params.append(self.bucket)
            if self.subdir:
                sql += " AND subdir = ?"
                params.append(self.subdir)
            if limit > 0:
                sql += " ORDER BY RANDOM() LIMIT ?"
                params.append(max(1, limit))
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM images LIMIT 0").description]
            return [self._record_with_path(dict(zip(cols, row))) for row in rows]

        with self._lock:
            matches = [rec for rec in self._records if str(rec.get("query") or "").startswith(prefix)]
        if not matches:
            return []
        if limit > 0:
            random.shuffle(matches)
            matches = matches[: max(1, limit)]
        return [self._record_with_path(rec) for rec in matches]

    def search_rank_candidates(self, prefix: str = "pixiv:rank:", limit: int = 0) -> list[dict]:
        """发图候选：SQLite 库中 bucket 匹配实例且（query 前缀匹配 或 subdir=manual）。

        manual 记录为按需下载（pixiv_illust_detail / pixiv_user_illusts 等），
        与榜库记录一起参与 pixiv_local_rank 发图选择；limit>0 随机取。
        """
        if self.backend != "sqlite":
            return []
        conds = ["(query LIKE ? OR subdir = 'manual')"]
        params: list = [prefix + "%"]
        if self.bucket:
            conds.insert(0, "bucket = ?")
            params.insert(0, self.bucket)
        sql = f"SELECT * FROM images WHERE {' AND '.join(conds)}"
        if limit > 0:
            sql += " ORDER BY RANDOM() LIMIT ?"
            params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        cols = [d[0] for d in self._conn.execute("SELECT * FROM images LIMIT 0").description]
        return [self._record_with_path(dict(zip(cols, row))) for row in rows]

    def _record_with_path(self, rec: dict) -> dict:
        sha256 = rec.get("sha256") or ""
        lib_rel = f"{sha256}{self.mime_suffix(rec.get('mime') or 'image/jpeg')}"
        user_id = str(rec.get("user_id") or "").strip()
        rel_dir = (
            f"manual/{user_id}"
            if user_id
            else f"{rec.get('bucket') or 'SFW'}/{rec.get('subdir') or 'daily'}"
        )
        return {**rec, "lib_rel": lib_rel, "rel_dir": rel_dir}

    def resolve_path(self, rec: dict) -> Path:
        """按记录解析图片文件绝对路径（物理单份：sqlite 走共享 files/ 目录）。"""
        if self.backend != "sqlite":
            return self.path(rec.get("lib_rel") or "")
        lib_rel = str(rec.get("lib_rel") or "").strip()
        if not lib_rel:
            return Path()
        return self._storage_root / lib_rel

    def lookup_record(self, source_url: str) -> dict | None:
        """按 source_url 查 SQLite 库完整记录（含 rel_dir/lib_rel）；无记录返回 None。"""
        if self.backend != "sqlite" or not source_url:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM images WHERE source_url = ? LIMIT 1", (source_url,)
            ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._conn.execute("SELECT * FROM images LIMIT 0").description]
        return self._record_with_path(dict(zip(cols, row)))

    def lookup(self, source_url: str) -> str | None:
        """URL → lib_rel（文件名）；无记录返回 None。"""
        if self.backend == "sqlite":
            with self._lock:
                row = self._conn.execute(
                    "SELECT sha256, mime FROM images WHERE source_url = ?", (source_url,)
                ).fetchone()
            if row is None:
                return None
            return f"{row[0]}{self.mime_suffix(row[1])}"
        return self._url_to_libpath.get(source_url)

    def lookup_by_illust_id(self, illust_id: str) -> str | None:
        """按作品 ID 匹配本地记录，返回本地 source_url（用于增量判重与历史统一 key）。"""
        if self.backend != "sqlite" or not illust_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT source_url FROM images WHERE illust_id = ? LIMIT 1", (illust_id,)
            ).fetchone()
        return row[0] if row else None

    def path(self, lib_rel: str) -> Path:
        return self.directory / lib_rel

    def store(
        self,
        raw: bytes,
        source_url: str,
        mime: str,
        query: str,
        engine: str,
        extra: dict | None = None,
    ) -> str:
        sha256 = hashlib.sha256(raw).hexdigest()
        lib_rel = f"{sha256}{self.mime_suffix(mime)}"
        if self.backend == "sqlite":
            path = self._storage_root / lib_rel
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM images WHERE source_url = ?", (source_url,)
                ).fetchone()
                if exists:
                    return lib_rel
                if not path.exists():
                    self._storage_root.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                record = {
                    "sha256": sha256,
                    "mime": mime,
                    "size": len(raw),
                    "source_url": source_url,
                    "query": query,
                    "engine": engine,
                    "fetched_at": time.time(),
                    "bucket": self.bucket or "SFW",
                    "subdir": self.subdir or "daily",
                }
                if extra:
                    record.update(extra)
                cols = list(record.keys())
                self._conn.execute(
                    f"INSERT OR IGNORE INTO images ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [record[c] for c in cols],
                )
                self._conn.commit()
            return lib_rel

        with self._lock:
            if self._url_to_libpath.get(source_url) == lib_rel:
                return lib_rel
            path = self.directory / lib_rel
            if not path.exists():
                path.write_bytes(raw)
            record = {
                "sha256": sha256,
                "mime": mime,
                "size": len(raw),
                "source_url": source_url,
                "query": query,
                "engine": engine,
                "fetched_at": time.time(),
            }
            if extra:
                record.update(extra)
            with self._index_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._url_to_libpath[source_url] = lib_rel
            self._records.append(record)
        return lib_rel

    @staticmethod
    def guess_mime(url: str) -> str:
        path = urlparse(url).path.lower()
        return next(
            (m for s, m in ImageLibrary.MIME_BY_SUFFIX.items() if path.endswith(s)),
            "image/jpeg",
        )

    @staticmethod
    def mime_suffix(mime: str) -> str:
        return ImageLibrary.EXT_BY_MIME.get(mime, ".jpg")

    @staticmethod
    def mime_for_file(lib_rel: str) -> str:
        return ImageLibrary.MIME_BY_SUFFIX.get(Path(lib_rel).suffix.lower(), "image/jpeg")


class RankScorer:
    """排行榜排名历史评分：SQLite rank_history 表按 URL 聚合。

    单期分 = 101 - rank（clamp ≥1）；加权分 = mean(单期分) × (1 + 0.3×log2(出现次数))。
    无历史记录返回 None（不参与排序）。
    """

    _LOG2_FACTOR = 0.3

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rank_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT NOT NULL,
                mode TEXT NOT NULL,
                date TEXT NOT NULL,
                rank INTEGER NOT NULL,
                UNIQUE(source_url, mode, date)
            )
            """
        )
        self._conn.commit()
        self._lock = threading.Lock()

    @staticmethod
    def _clamp_rank(rank: object) -> int:
        try:
            return max(1, int(rank))
        except (TypeError, ValueError):
            return 100

    def record(self, source_url: str, mode: str, date: str, rank: object) -> None:
        """记录一次上榜历史（同 url+mode+date 幂等，INSERT OR IGNORE）。"""
        if not source_url:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO rank_history (source_url, mode, date, rank) VALUES (?, ?, ?, ?)",
                (source_url, mode, date, self._clamp_rank(rank)),
            )
            self._conn.commit()

    def entry(self, source_url: str) -> dict | None:
        """返回 (score, best_rank, count) 汇总；无历史返回 None。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT rank FROM rank_history WHERE source_url = ?", (source_url,)
            ).fetchall()
        ranks = [r[0] for r in rows]
        if not ranks:
            return None
        scores = [101.0 - r for r in ranks]
        mean = sum(scores) / len(scores)
        weight = 1.0 + self._LOG2_FACTOR * (len(ranks) and math.log2(len(ranks)))
        score = mean * weight
        return {
            "score": round(score, 1),
            "best_rank": min(ranks),
            "count": len(ranks),
        }
