#!/usr/bin/env python3
"""一次性迁移：pixiv 图库扁平 jsonl → 三级目录 + SQLite 单库。

- 旧：image_library/pixiv/{index.jsonl, rank_history.jsonl, *.jpg/png/..., daily_run.log}
- 新：image_library/pixiv/pixiv.db + SFW|NSFW/{daily,weekly,monthly}/
- 默认 dry-run 打印计划；--commit 才执行迁移与删除旧文件。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_searxng.library import ImageLibrary, pixiv_mode_parts  # noqa: E402

PIXIV_ROOT = Path(__file__).resolve().parent.parent / "image_library" / "pixiv"
DB_PATH = PIXIV_ROOT / "pixiv.db"
QUERY_MODE_RE = re.compile(r"^pixiv:rank:([^:]+):")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pixiv 图库扁平 jsonl 迁移到三级目录 + SQLite")
    parser.add_argument("--commit", action="store_true", help="执行迁移（默认仅 dry-run）")
    return parser.parse_args()


def build_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
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
            subdir TEXT DEFAULT 'daily'
        );
        CREATE TABLE IF NOT EXISTS rank_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            mode TEXT NOT NULL,
            date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            UNIQUE(source_url, mode, date)
        );
        """
    )
    conn.commit()


def main() -> int:
    args = parse_args()
    if not PIXIV_ROOT.is_dir():
        print(f"错误: 未找到 {PIXIV_ROOT}")
        return 1

    index_file = PIXIV_ROOT / "index.jsonl"
    history_file = PIXIV_ROOT / "rank_history.jsonl"
    if not index_file.is_file():
        print("未找到 index.jsonl（可能已迁移）")
        return 1

    rows: list[dict] = []
    with index_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("source_url") and rec.get("sha256"):
                rows.append(rec)
    print(f"index.jsonl: {len(rows)} 条")

    history: list[dict] = []
    if history_file.is_file():
        with history_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("source_url") and rec.get("rank") is not None:
                    history.append(rec)
    print(f"rank_history.jsonl: {len(history)} 条")

    moves: list[tuple[Path, Path]] = []
    bucket_counts: dict[str, int] = {}
    for rec in rows:
        m = QUERY_MODE_RE.match(str(rec.get("query") or ""))
        if m is None:
            print(f"  跳过无法解析 mode 的行: {rec.get('source_url')}")
            continue
        mode = m.group(1)
        try:
            bucket, subdir = pixiv_mode_parts(mode)
        except ValueError:
            print(f"  跳过未知 mode {mode}: {rec.get('source_url')}")
            continue
        rec["bucket"] = bucket
        rec["subdir"] = subdir
        sha = rec["sha256"]
        src = PIXIV_ROOT / f"{sha}{ImageLibrary.mime_suffix(rec.get('mime') or 'image/jpeg')}"
        dst = PIXIV_ROOT / bucket / subdir / src.name
        if src.is_file():
            moves.append((src, dst))
        bucket_counts[f"{bucket}/{subdir}"] = bucket_counts.get(f"{bucket}/{subdir}", 0) + 1
    print("目录分布:", bucket_counts)
    print(f"待移动图片文件: {len(moves)}")

    if not args.commit:
        print("dry-run 完成（未执行任何变更）；加 --commit 执行迁移")
        return 0

    # 建库灌数据
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    build_schema(conn)
    for rec in rows:
        cols = [
            "source_url", "sha256", "mime", "size", "query", "engine",
            "rank", "rank_mode", "rank_date", "is_masked", "illust_type",
            "fetched_at", "bucket", "subdir",
        ]
        conn.execute(
            f"INSERT OR IGNORE INTO images ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [rec.get(c) for c in cols],
        )
    for rec in history:
        conn.execute(
            "INSERT OR IGNORE INTO rank_history (source_url, mode, date, rank) VALUES (?, ?, ?, ?)",
            (rec["source_url"], rec.get("mode") or "", rec.get("date") or "", int(rec["rank"])),
        )
    conn.commit()

    moved = 0
    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            src.unlink(missing_ok=True)
            continue
        os.rename(src, dst)
        moved += 1
    print(f"图片文件迁移: {moved}")

    # 删除旧扁平文件
    for name in ("index.jsonl", "rank_history.jsonl", "daily_run.log"):
        p = PIXIV_ROOT / name
        if p.is_file():
            p.unlink()
            print(f"已删除: {p.name}")
    for p in PIXIV_ROOT.iterdir():
        if p.is_file() and p.suffix.lower() in ImageLibrary.MIME_BY_SUFFIX:
            p.unlink()
            print(f"已删除旧根目录图片: {p.name}")
    conn.close()
    print("迁移完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
