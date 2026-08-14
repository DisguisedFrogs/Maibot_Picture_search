#!/usr/bin/env python3
"""一次性迁移：images 表新增 illust_id 列，并从 source_url 正则提取回填。

用途：支持按作品唯一标识符增量匹配（跳过已存在图的下载）。
"""

import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "image_library" / "pixiv" / "pixiv.db"
ID_RE = re.compile(r"/(\d{5,})(?:-[0-9a-f]+)?_p\d+\.")


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(images)")]
    if "illust_id" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN illust_id TEXT")
        conn.commit()
        print("已添加 illust_id 列")

    rows = conn.execute(
        "SELECT source_url FROM images WHERE illust_id IS NULL OR illust_id = ''"
    ).fetchall()
    updated = 0
    for (url,) in rows:
        m = ID_RE.search(url or "")
        if m:
            conn.execute(
                "UPDATE images SET illust_id = ? WHERE source_url = ?",
                (m.group(1), url),
            )
            updated += 1
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    with_id = conn.execute(
        "SELECT COUNT(*) FROM images WHERE illust_id IS NOT NULL AND illust_id != ''"
    ).fetchone()[0]
    print(f"回填 {updated} 条；illust_id 覆盖 {with_id}/{total}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
