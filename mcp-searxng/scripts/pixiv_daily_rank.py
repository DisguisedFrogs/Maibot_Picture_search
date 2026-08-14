#!/usr/bin/env python3
"""Pixiv 榜前 100 张原图入库（image_library/pixiv/{SFW|NSFW}/{daily|weekly|monthly}/）。

- 复用 mcp_searxng 包的 PixivClient/ImageLibrary/DownloadManager
- 排行榜按性别标签过滤（默认只保留女性相关图，剔除男性/男の娘/双性/BL 类），
  过滤后不足目标数自动翻页补足（上限 --max-pages 10 页），跳过 ugoira 动图
- 原图按内容 SHA256 命名 + SQLite source_url 索引双重去重：重复自动跳过
- 索引与排名历史存 image_library/pixiv/pixiv.db（SQLite 单库，WAL）
- 统计结果追加写入对应榜目录的 daily_run.log，stdout 输出摘要
- 由 systemd timer 触发（--mode daily/weekly/monthly/daily_r18/...）；可手动运行
"""

import argparse
import asyncio
import re
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import (  # noqa: E402
    DownloadManager,
    ImageLibrary,
    PixivClient,
    RankScorer,
    SearxngServer,
    ServerConfig,
    pixiv_mode_parts,
)
from mcp_searxng.tagfilter import PixivGenderFilter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIXIV_ROOT = PROJECT_ROOT / "image_library" / "pixiv"
DB_PATH = PIXIV_ROOT / "pixiv.db"


class PixivRankCrawler:
    """每日日榜抓取入库器：排行拉取 → 原图 URL 推导 → 并行下载 → 去重入库。"""

    _UGOIRA_TYPE = 2
    DEFAULT_MAX_IMAGE_BYTES = 50 * 1024 * 1024
    _IMG_RE_LENIENT = re.compile(
        r"https://i\.pximg\.net/(?:c/[^/]+/)?img-master/img/"
        r"(\d{4}/\d{2}/\d{2}/\d{2}/\d{2}/\d{2})/"
        r"(\d+)(-[0-9a-f]+)?_p(\d+)_(?:square1200|master1200)\.(?:jpg|webp)$"
    )

    def __init__(
        self,
        mode: str = "daily",
        content: str = "illust",
        pages: int = 2,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        max_pages: int = 10,
        no_filter: bool = False,
        strict: bool = False,
    ) -> None:
        self.mode = mode
        self.content = content
        self.pages = pages
        self.max_pages = max_pages
        self.pages_used = 0
        self._config = replace(ServerConfig(), max_image_bytes=max_image_bytes)
        self._download = DownloadManager(self._config)
        self._pixiv = PixivClient(self._config, self._download)
        self._filter = None if no_filter else PixivGenderFilter(strict=strict)
        bucket, subdir = pixiv_mode_parts(mode)
        self._lib_dir = PIXIV_ROOT / bucket / subdir
        self._library = ImageLibrary(
            self._lib_dir,
            backend="sqlite",
            db_path=DB_PATH,
            bucket=bucket,
            subdir=subdir,
        )
        self._scorer = RankScorer(DB_PATH)

    @staticmethod
    def _normalize(item: dict) -> dict:
        return SearxngServer._pixiv_normalize(item)

    @classmethod
    def _derive_original(cls, url: str) -> str | None:
        """原图 URL 推导：先走 PixivClient 正则，再尝试 lenient 变体（文件名带 hash 后缀）。"""
        orig = PixivClient.original_url(url)
        if orig:
            return orig
        m = cls._IMG_RE_LENIENT.match(url)
        if m is None:
            return None
        return (
            f"https://i.pximg.net/img-original/img/{m.group(1)}/"
            f"{m.group(2)}{m.group(3) or ''}_p{m.group(4)}.jpg"
        )

    def _log(self, line: str) -> None:
        self._lib_dir.mkdir(parents=True, exist_ok=True)
        with (self._lib_dir / "daily_run.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line)

    async def fetch_works(self) -> list[dict]:
        """拉取排行原始条目，过滤后不足目标数（pages×50）时自动翻页补足（上限 max_pages）。

        异常上抛由 run 统一处理。
        """
        target = self.pages * 50
        works: list[dict] = []
        for pageno in range(1, self.max_pages + 1):
            try:
                data = await asyncio.to_thread(
                    self._pixiv.get_ranking, self.mode, self.content, pageno
                )
            except Exception as e:
                if "404" in str(e):
                    break
                raise
            raw_items = data.get("contents") or []
            if not isinstance(raw_items, list):
                raw_items = []
            works.extend(raw_items)
            self.pages_used = pageno
            if not raw_items:
                break
            if self._filter is None:
                if len(works) >= target:
                    break
            elif sum(1 for it in works if self._filter.keep(self._normalize(it))) >= target:
                break
        return works

    def _classify(self, works: list[dict]) -> tuple[list, list, dict]:
        """归一化并分类：性别过滤 → 可推导原图的进 slots，其余进 deferred；跳过 ugoira。"""
        slots: list[tuple[dict, str]] = []
        deferred: list[dict] = []
        stats = {
            "total": len(works),
            "new": 0,
            "dup": 0,
            "fail": 0,
            "ugoira": 0,
            "filtered": 0,
        }
        for it in works:
            w = self._normalize(it)
            if self._filter is not None and not self._filter.keep(w):
                stats["filtered"] += 1
                continue
            wid = str(w.get("id") or "")
            if not wid or not w.get("url"):
                continue
            if w.get("type") == self._UGOIRA_TYPE:
                stats["ugoira"] += 1
                continue
            orig = self._derive_original(w["url"])
            if orig:
                slots.append((w, orig))
            else:
                deferred.append(w)
        return slots, deferred, stats

    def _store(self, w: dict, url: str, raw: bytes, ctype: str, label: str, stats: dict) -> None:
        mime = (ctype or "").lower().strip()
        if mime not in ImageLibrary.EXT_BY_MIME:
            mime = ImageLibrary.guess_mime(url)
        rank = self._rank_of(w)
        extra = {
            "rank": rank,
            "rank_mode": self.mode,
            "rank_date": self._rank_date,
            "is_masked": int(w.get("is_masked") or 0),
            "illust_type": int(w.get("type") or 0),
            "illust_id": str(w.get("id") or ""),
        }
        if self._library.lookup(url):
            stats["dup"] += 1
            if rank is not None:
                self._scorer.record(url, self.mode, self._rank_date, rank)
            return
        self._library.store(raw, url, mime, label, "pixiv", extra=extra)
        if rank is not None:
            self._scorer.record(url, self.mode, self._rank_date, rank)
        stats["new"] += 1

    @staticmethod
    def _rank_of(w: dict) -> int | None:
        try:
            return int(w.get("rank"))
        except (TypeError, ValueError):
            return None

    def _skip_existing(self, w: dict, stats: dict) -> bool:
        """增量匹配：按作品 ID 查本地，命中则跳过下载并记录排名历史（评分累积）。

        匹配优先 illust_id；illust_id 为空/未命中时按推导 URL 兜底。
        返回 True 表示已存在（无需下载）。
        """
        wid = str(w.get("id") or "")
        rank = self._rank_of(w)
        if wid:
            local_url = self._library.lookup_by_illust_id(wid)
            if local_url:
                stats["dup"] += 1
                if rank is not None:
                    self._scorer.record(local_url, self.mode, self._rank_date, rank)
                return True
        url = w.get("url") or ""
        orig = self._derive_original(url) if url else None
        if orig and self._library.lookup(orig):
            stats["dup"] += 1
            if rank is not None:
                self._scorer.record(orig, self.mode, self._rank_date, rank)
            return True
        return False

    async def download_and_store(
        self,
        slots: list[tuple[dict, str]],
        deferred: list[dict],
        stats: dict,
        label: str,
        failed_ids: list[str],
    ) -> None:
        """增量下载：先按作品 ID 匹配本地，已存在的跳过下载（仅记排名历史），
        只下载不存在的图 → 失败/未推导的逐个回退详情接口 → 去重入库。"""
        slots = [(w, o) for w, o in slots if not self._skip_existing(w, stats)]
        deferred = [w for w in deferred if not self._skip_existing(w, stats)]

        ok_urls: set[str] = set()
        w_by_url = {o: w for w, o in slots}
        if slots:
            fetched = await self._pixiv.fetch_images_parallel([o for _, o in slots])
            for url, raw, err, ctype in fetched:
                if raw is not None:
                    ok_urls.add(url)
                    self._store(w_by_url[url], url, raw, ctype, label, stats)

        retry = [w for w, o in slots if o not in ok_urls] + deferred
        for w in retry:
            try:
                detail = await asyncio.to_thread(self._pixiv.get_illust_detail, w["id"])
                exact = (detail.get("urls") or {}).get("original")
                if not exact:
                    stats["fail"] += 1
                    failed_ids.append(str(w["id"]))
                    continue
                async with self._download.semaphore:
                    raw, ctype = await self._pixiv.fetch_image(exact)
                self._store(w, exact, raw, ctype, label, stats)
            except Exception:
                stats["fail"] += 1
                failed_ids.append(str(w["id"]))

    async def run(self) -> int:
        """编排一次抓取入库：拉取 → 分类 → 下载存储 → 记录日志，返回退出码。"""
        date = datetime.now().strftime("%Y-%m-%d")
        self._rank_date = date
        label = f"pixiv:rank:{self.mode}:{date}"
        start = time.monotonic()

        try:
            works = await self.fetch_works()
        except Exception as e:
            self._log(f"{date} {datetime.now().strftime('%H:%M:%S')} | 排行榜获取失败: {e}")
            return 1

        if not works:
            self._log(f"{date} {datetime.now().strftime('%H:%M:%S')} | 排行榜无数据（限流或 cookie 过期）")
            return 1

        slots, deferred, stats = self._classify(works)
        failed_ids: list[str] = []
        await self.download_and_store(slots, deferred, stats, label, failed_ids)

        elapsed = int(time.monotonic() - start)
        summary = (
            f"{date} {datetime.now().strftime('%H:%M:%S')} | 用时 {elapsed}s | "
            f"total={stats['total']} new={stats['new']} dup={stats['dup']} "
            f"fail={stats['fail']} ugoira={stats['ugoira']} "
            f"filtered={stats['filtered']} pages_used={self.pages_used}"
        )
        if failed_ids:
            summary += f" | failed_ids: {','.join(failed_ids[:50])}"
        self._log(summary)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日 Pixiv 日榜前 100 张原图入库")
    parser.add_argument("--mode", default="daily", help="排行榜 mode（默认 daily）")
    parser.add_argument("--content", default="illust", help="content：illust/manga（默认 illust）")
    parser.add_argument("--pages", type=int, default=2, help="目标页数，每页 50 条（默认 2）")
    parser.add_argument("--max-pages", type=int, default=10, help="过滤后翻页补足上限（默认 10）")
    parser.add_argument("--no-filter", action="store_true", help="禁用性别标签过滤")
    parser.add_argument("--strict", action="store_true", help="严格模式：必须命中女性白名单标签")
    args = parser.parse_args(argv)
    crawler = PixivRankCrawler(
        mode=args.mode,
        content=args.content,
        pages=args.pages,
        max_pages=args.max_pages,
        no_filter=args.no_filter,
        strict=args.strict,
    )
    return asyncio.run(crawler.run())


if __name__ == "__main__":
    sys.exit(main())
