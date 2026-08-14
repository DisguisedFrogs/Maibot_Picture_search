#!/usr/bin/env python3
"""一次性迁移：pixiv 图库三级目录 → 共享扁平目录 files/（物理单份）。

- 旧：image_library/pixiv/{SFW,NSFW}/{daily,weekly,monthly}/ 与 manual/<uid>/ 存图片
- 新：全部图片集中于 image_library/pixiv/files/<sha256><ext>；
  bucket/subdir/user_id 仅作 pixiv.db 逻辑元数据（DB 无需改动）
- 同内容（同名 sha256）在旧目录多处出现时只保留一份，其余删源计为 dup
- daily_run.log 留在原目录；空目录清理
- 默认 dry-run 打印计划；--commit 才执行迁移
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_searxng.library import ImageLibrary  # noqa: E402

PIXIV_ROOT = Path(__file__).resolve().parent.parent / "image_library" / "pixiv"
FILES_DIR = PIXIV_ROOT / "files"
KEEP_NAMES = {"daily_run.log"}


def iter_legacy_images(root: Path) -> list[Path]:
    """收集旧目录结构下所有图片文件（SFW|NSFW/*/ 与 manual/*/）。"""
    images: list[Path] = []
    for bucket in ("SFW", "NSFW"):
        bdir = root / bucket
        if not bdir.is_dir():
            continue
        for sub in bdir.iterdir():
            if sub.is_dir():
                images.extend(p for p in sub.iterdir() if p.is_file())
    manual = root / "manual"
    if manual.is_dir():
        for uid in manual.iterdir():
            if uid.is_dir():
                images.extend(p for p in uid.iterdir() if p.is_file())
    return [p for p in images if p.name not in KEEP_NAMES]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pixiv 图库三级目录迁移到共享 files/ 扁平目录")
    parser.add_argument("--commit", action="store_true", help="执行迁移（默认仅 dry-run）")
    args = parser.parse_args(argv)

    if not PIXIV_ROOT.is_dir():
        print(f"错误: 未找到 {PIXIV_ROOT}")
        return 1

    images = iter_legacy_images(PIXIV_ROOT)
    if not images:
        print("未找到待迁移图片（可能已迁移）")
        return 0

    by_name: dict[str, list[Path]] = {}
    for p in images:
        by_name.setdefault(p.name, []).append(p)
    dup_extra = sum(len(v) - 1 for v in by_name.values())
    print(f"待迁移图片: {len(images)} 个（同内容重复 {dup_extra} 个，仅保留一份）")
    for p in sorted(p.name for p in images):
        print(f"  {p}")

    if not args.commit:
        print("dry-run 完成（未执行任何变更）；加 --commit 执行迁移")
        return 0

    FILES_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    deduped = 0
    for name, paths in by_name.items():
        dst = FILES_DIR / name
        first = paths[0]
        if dst.exists():
            deduped += 1
        else:
            os.rename(str(first), str(dst))
            moved += 1
        for extra in paths[1:]:
            extra.unlink(missing_ok=True)
            deduped += 1

    for p in images:
        try:
            p.parent.rmdir()
        except OSError:
            pass
    print(f"迁移完成: moved={moved} dedup={deduped}（daily_run.log 保留原目录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
