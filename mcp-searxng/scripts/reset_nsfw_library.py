#!/usr/bin/env python3
"""清空 NSFW 图库并保留排名历史（配合新爬图规则整目录重爬用）。

- 删除 files/ 下仅被 NSFW 记录引用的图片文件（仍被 SFW/manual 引用的保留，
  物理单份共享，不可误删）
- DELETE images 表中 bucket='NSFW' 的记录（保留 rank_history 评分历史）
- 用法: .venv/bin/python scripts/reset_nsfw_library.py
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIXIV_ROOT = PROJECT_ROOT / "image_library" / "pixiv"
DB_PATH = PIXIV_ROOT / "pixiv.db"
FILES_DIR = PIXIV_ROOT / "files"


def main() -> int:
    sys.path.insert(0, str(PROJECT_ROOT))
    from mcp_searxng.library import ImageLibrary  # noqa: E402

    conn = sqlite3.connect(DB_PATH)
    nsfw_rows = conn.execute(
        "SELECT sha256, mime FROM images WHERE bucket = 'NSFW'"
    ).fetchall()
    keep_sha = {
        r[0]
        for r in conn.execute("SELECT DISTINCT sha256 FROM images WHERE bucket != 'NSFW'")
    }
    removed_rows = conn.execute("DELETE FROM images WHERE bucket = 'NSFW'").rowcount
    conn.commit()
    conn.close()

    removed_files = 0
    for sha, mime in nsfw_rows:
        if sha in keep_sha:
            continue
        p = FILES_DIR / f"{sha}{ImageLibrary.mime_suffix(mime or 'image/jpeg')}"
        if p.is_file():
            p.unlink()
            removed_files += 1

    print(
        f"已删除 NSFW 图片文件 {removed_files} 个（{len(nsfw_rows) - removed_files} 个被"
        f"SFW/manual 引用保留），images 记录 {removed_rows} 条（rank_history 保留）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
