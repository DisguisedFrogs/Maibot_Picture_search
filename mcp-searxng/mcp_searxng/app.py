"""SearxngServer 编排层与 MCP 注册：9 个工具、模块级 app/mcp 实例与别名。"""

import asyncio
import base64
import logging
import math
import random
import re
import sys
import threading
import time
from collections import OrderedDict

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)

from mcp import types  # noqa: E402
from mcp.server.mcpserver import MCPServer
from .cache import PageCache
from .config import ServerConfig
from .fetcher import PageFetcher
from .gitcache import GitRepoCache
from .library import ImageLibrary, RankScorer, pixiv_mode_parts
from .markdown import MarkdownAnalyzer, PageProcessor
from .network import DownloadManager
from .pixiv import PixivClient
from .searxng import SearxngClient

class SearxngServer:
    MAX_IMAGES_PER_CALL = 20
    MAX_THUMB_CACHE = 40

    def __init__(self, config: ServerConfig | None = None):
        self.config = config or ServerConfig()
        self.cache = PageCache(self.config.cache_ttl, self.config.cache_max)
        self.library = ImageLibrary(self.config.image_library_dir)
        self._pixiv_db = self.config.image_library_dir / "pixiv" / "pixiv.db"
        self._pixiv_libs: dict[str, ImageLibrary] = {}
        self._pixiv_manual_libs: dict[str, ImageLibrary] = {}
        self.rank_scorer = RankScorer(self._pixiv_db)
        self.gitcache = GitRepoCache(self.config)
        self.download = DownloadManager(self.config)
        self.fetcher = PageFetcher(self.config, self.cache, self.download)
        self.searxng = SearxngClient(self.config, self.download)
        self.pixiv = PixivClient(self.config, self.download)
        self._thumb_cache: OrderedDict[str, bytes] = OrderedDict()
        self._thumb_cache_lock = threading.Lock()

    def _thumb_cache_get(self, url: str) -> bytes | None:
        with self._thumb_cache_lock:
            raw = self._thumb_cache.get(url)
            if raw is None:
                return None
            self._thumb_cache.move_to_end(url)
            return raw

    def _thumb_cache_put(self, url: str, raw: bytes) -> None:
        with self._thumb_cache_lock:
            self._thumb_cache[url] = raw
            self._thumb_cache.move_to_end(url)
            while len(self._thumb_cache) > self.MAX_THUMB_CACHE:
                self._thumb_cache.popitem(last=False)

    def _entry_for(self, url: str, use_proxy: bool) -> tuple[dict, bool]:
        """先查缓存；git 托管站仓库 URL 走本地 clone 分析；其余走 HTTP 抓取。

        统一返回 dict 拷贝，调用方修改不会污染缓存。
        """
        cache_hit = self.cache.get(url)
        if cache_hit is not None:
            return dict(cache_hit), True
        git_entry = self.gitcache.analyze(url)
        if git_entry is not None:
            self.cache.put(url, git_entry)
            return dict(git_entry), False
        return self.fetcher.get_page(url, use_proxy)

    @staticmethod
    def _pixiv_normalize(item: dict) -> dict:
        """搜索/排行/画师三种响应元素统一为同构 dict。"""

        def pick(*keys, default=None):
            for k in keys:
                v = item.get(k)
                if v not in (None, ""):
                    return v
            return default

        def pick_int(*keys, default=0):
            v = pick(*keys, default=default)
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        return {
            "id": str(pick("id", "illust_id", default="")),
            "title": pick("title", default="(无标题)"),
            "user_name": pick("userName", "user_name", default="(未知画师)"),
            "user_id": str(pick("userId", "user_id", default="")),
            "tags": pick("tags", default=[]),
            "url": pick("url", default=""),
            "width": pick_int("width"),
            "height": pick_int("height"),
            "pages": pick_int("pageCount", "illust_page_count", default=1),
            "type": pick_int("illustType", "illust_type"),
            "x_restrict": pick_int("xRestrict", "x_restrict"),
            "ai_type": pick_int("aiType", "illust_ai_type"),
            "bookmarks": pick_int("bookmarkCount", "bookmark_count"),
            "likes": pick_int("likeCount", "like_count"),
            "views": pick_int("viewCount", "view_count"),
            "rating": pick_int("rating_count"),
            "rank": pick("rank", default=""),
            "is_masked": pick_int("is_masked"),
            "date": pick("date", "createDate", default=""),
        }

    @staticmethod
    def _pixiv_work_lines(w: dict, idx) -> list[str]:
        lines = [f"{idx}. {w['title']}"]
        marks = []
        if w["ai_type"] == 1:
            marks.append("AI")
        if w["x_restrict"]:
            marks.append("R18")
        if marks:
            lines.append(f"   ⚠️ {'/'.join(marks)}")
        lines.append(f"   画师: {w['user_name']} (ID {w['user_id']})")
        if w["tags"]:
            lines.append(f"   tags: {'、'.join(str(t) for t in w['tags'][:12])}")
        stat = []
        if w["width"] and w["height"]:
            stat.append(f"{w['width']}x{w['height']}")
        if w["pages"] > 1:
            stat.append(f"{w['pages']} 页")
        if w["bookmarks"]:
            stat.append(f"收藏 {w['bookmarks']}")
        if w["likes"]:
            stat.append(f"赞 {w['likes']}")
        if w["views"]:
            stat.append(f"浏览 {w['views']}")
        if w["rating"]:
            stat.append(f"评分 {w['rating']}")
        if w["date"]:
            stat.append(w["date"])
        if stat:
            lines.append(f"   {' · '.join(stat)}")
        if w["id"]:
            lines.append(f"   作品: https://www.pixiv.net/artworks/{w['id']}")
        return lines

    @staticmethod
    def _pixiv_mime(ctype: str, url: str) -> str:
        ct = (ctype or "").lower().strip()
        if ct in ImageLibrary.EXT_BY_MIME:
            return ct
        return ImageLibrary.guess_mime(url)

    def _pixiv_emit_image(
        self,
        w: dict,
        url: str,
        raw: bytes,
        ctype: str,
        label: str,
        contents: list,
        library_rows: list[str],
        lib: ImageLibrary | None = None,
    ) -> None:
        """原图入库：lib 为空时按画师 userId 存 manual 库（bucket 按 x_restrict 判定）。"""
        mime = self._pixiv_mime(ctype, url)
        if lib is not None:
            extra = {"illust_id": str(w.get("id") or "")}
        else:
            lib = self._pixiv_manual_lib(w.get("user_id"))
            extra = {
                "bucket": "NSFW" if int(w.get("x_restrict") or 0) else "SFW",
                "subdir": "manual",
                "illust_id": str(w.get("id") or ""),
                "user_id": str(w.get("user_id") or ""),
            }
        lib_rel = lib.store(raw, url, mime, label, "pixiv", extra=extra)
        contents.append(
            types.ImageContent(
                type="image", data=base64.b64encode(raw).decode(), mimeType=mime
            )
        )
        library_rows.append(f"   - {lib_rel}（{w['title']}）")

    async def _pixiv_attach_images(
        self,
        works: list[dict],
        label: str,
        include_images: int,
        include_thumbnails: int,
        lines: list[str],
        contents: list[types.TextContent | types.ImageContent],
        lib: ImageLibrary | None = None,
    ) -> None:
        """给作品列表附加缩略图预览与原图（入图库），行为对齐 image_search。"""

        if include_thumbnails > 0:
            cap = min(include_thumbnails, self.MAX_IMAGES_PER_CALL)
            thumbs: list[str] = []
            for w in works:
                if w.get("url") and w["url"] not in thumbs:
                    thumbs.append(w["url"])
            if len(thumbs) > cap:
                lines.append(
                    f"（单次最多 {self.MAX_IMAGES_PER_CALL} 张缩略图，本次已截取前 {cap} 张）"
                )
            thumbs = thumbs[:cap]
            if thumbs:
                lines.insert(
                    0,
                    "注意：本次附带的图片为缩略图预览，仅供判断相关性，"
                    "请勿直接发送；确认后请用 include_images 获取原图。",
                )
                to_fetch = [u for u in thumbs if self._thumb_cache_get(u) is None]
                fetched = await self.pixiv.fetch_images_parallel(to_fetch)
                for url, raw, _, _ in fetched:
                    if raw is not None:
                        self._thumb_cache_put(url, raw)
                ok = 0
                for url in thumbs:
                    raw = self._thumb_cache_get(url)
                    if raw is None:
                        continue
                    ok += 1
                    contents.append(
                        types.ImageContent(
                            type="image",
                            data=base64.b64encode(raw).decode(),
                            mimeType=ImageLibrary.guess_mime(url),
                        )
                    )
                if ok < len(thumbs):
                    lines.append(f"（缩略图预览 {ok}/{len(thumbs)} 张可用，失败的已跳过）")

        if include_images <= 0:
            return
        cap = min(include_images, self.MAX_IMAGES_PER_CALL)
        uniq: list[dict] = []
        seen: set[str] = set()
        ugoira = 0
        for w in works:
            wid = str(w.get("id") or "")
            if not w.get("url") or not wid or wid in seen:
                continue
            if w.get("type") == PixivClient._UGOIRA_TYPE:
                ugoira += 1
                continue
            seen.add(wid)
            uniq.append(w)
        if ugoira:
            lines.append(f"（{ugoira} 件 ugoira 动图不支持原图下载，已跳过）")
        if len(uniq) > cap:
            lines.append(
                f"（单次最多下载 {self.MAX_IMAGES_PER_CALL} 张原图，本次已截取前 {cap} 张）"
            )
        uniq = uniq[:cap]
        if not uniq:
            return

        slots: list[tuple[dict, str]] = []
        deferred: list[dict] = []
        for w in uniq:
            orig = PixivClient.original_url(w["url"])
            if orig:
                slots.append((w, orig))
            else:
                deferred.append(w)
        library_rows: list[str] = []
        ok_urls: set[str] = set()

        if slots:
            fetched = await self.pixiv.fetch_images_parallel([o for _, o in slots])
            for url, raw, _, ctype in fetched:
                if raw is not None:
                    ok_urls.add(url)
                    w = next(w for w, o in slots if o == url)
                    self._pixiv_emit_image(w, url, raw, ctype, label, contents, library_rows, lib=lib)

        retry = [w for w, o in slots if o not in ok_urls] + deferred
        for w in retry:
            try:
                detail = await asyncio.to_thread(self.pixiv.get_illust_detail, w["id"])
                exact = (detail.get("urls") or {}).get("original")
                if not exact:
                    lines.append(f"   [{w['title']}] 无原图 URL，已降级为链接")
                    continue
                async with self.download.semaphore:
                    raw, ctype = await self.pixiv.fetch_image(exact)
                self._pixiv_emit_image(w, exact, raw, ctype, label, contents, library_rows, lib=lib)
            except Exception as e:
                lines.append(f"   [{w['title']}] 原图下载失败: {e}")

        if library_rows:
            lines.append("")
            lines.append("📁 已存图库（永久保存）:")
            lines.extend(library_rows)

    _PIXIV_SEARCH_ORDERS = ("popular_d", "date_d", "date", "popular_male_d", "popular_female_d")
    _PIXIV_ORDER_LABELS = {
        "popular_d": "按热度",
        "date_d": "按最新",
        "date": "按最旧",
        "popular_male_d": "按男性热度",
        "popular_female_d": "按女性热度",
    }

    async def pixiv_search(
        self,
        keyword: str,
        pageno: int = 1,
        mode: str = "all",
        s_mode: str = "s_tag",
        order: str = "popular_d",
        include_images: int = 0,
        include_thumbnails: int = 0,
    ) -> list[types.TextContent | types.ImageContent]:
        try:
            if not keyword.strip():
                raise ValueError("keyword 不能为空")
            if mode not in ("all", "safe", "r18"):
                raise ValueError("mode 仅支持 all/safe/r18")
            if order not in self._PIXIV_SEARCH_ORDERS:
                raise ValueError(
                    "order 仅支持 " + "/".join(self._PIXIV_SEARCH_ORDERS)
                )
            try:
                data = await asyncio.to_thread(
                    self.pixiv.search_artworks, keyword, pageno, mode, s_mode, order
                )
                degraded = False
            except Exception:
                if order == "date_d":
                    raise
                data = await asyncio.to_thread(
                    self.pixiv.search_artworks, keyword, pageno, mode, s_mode, "date_d"
                )
                order = "date_d"
                degraded = True
            block = (data.get("body") or {}).get("illustManga") or {}
            items = block.get("data") or []
            lines: list[str] = []
            meta = []
            total = block.get("total")
            last = block.get("lastPage")
            if total:
                meta.append(f"共 {total} 件作品")
            if last and pageno < last:
                meta.append(f"第 {pageno}/{last} 页")
            meta.append(self._PIXIV_ORDER_LABELS.get(order, order))
            lines.append(f"[Pixiv 搜索] {keyword}" + (f"（{'、'.join(meta)}）" if meta else ""))
            if degraded:
                lines.append("⚠️ 按热度排序失败，已降级为最新排序")
            if mode == "r18" and not self.config.pixiv_cookie:
                lines.append("⚠️ 未配置 PIXIV_PHPSESSID cookie，R18 内容可能返回空结果")
            if not items:
                lines.append("（无搜索结果，可能是限流或该关键词无匹配）")
            for i, it in enumerate(items, 1):
                lines.extend(self._pixiv_work_lines(self._pixiv_normalize(it), i))
            contents: list[types.TextContent | types.ImageContent] = []
            await self._pixiv_attach_images(
                [self._pixiv_normalize(it) for it in items],
                f"pixiv:{keyword}",
                include_images,
                include_thumbnails,
                lines,
                contents,
            )
            contents.insert(0, types.TextContent(type="text", text="\n".join(lines)))
            return contents
        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {e}")]

    async def pixiv_user_illusts(
        self,
        user_id: int,
        max_works: int = 20,
        include_images: int = 0,
        include_thumbnails: int = 0,
    ) -> list[types.TextContent | types.ImageContent]:
        try:
            uid = int(user_id)
            if uid <= 0:
                raise ValueError("user_id 非法")
            ids = await asyncio.to_thread(self.pixiv.get_user_work_ids, uid)
            total = len(ids)
            cap = max(1, min(max_works, 100))
            works = await asyncio.to_thread(self.pixiv.get_user_illusts, uid, ids[:cap])
            lines: list[str] = [
                f"[Pixiv 画师] UID {uid}（共 {total} 件插画"
                + (f"，展示前 {len(works)} 件" if len(works) < total else "")
                + f"） https://www.pixiv.net/users/{uid}"
            ]
            if not works:
                lines.append("（该画师无公开插画或作品未公开）")
            for i, w in enumerate(works, 1):
                lines.extend(self._pixiv_work_lines(self._pixiv_normalize(w), i))
            contents: list[types.TextContent | types.ImageContent] = []
            await self._pixiv_attach_images(
                [self._pixiv_normalize(w) for w in works],
                f"pixiv:user:{uid}",
                include_images,
                include_thumbnails,
                lines,
                contents,
            )
            contents.insert(0, types.TextContent(type="text", text="\n".join(lines)))
            return contents
        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {e}")]

    async def pixiv_ranking(
        self,
        mode: str = "daily",
        content: str = "illust",
        pageno: int = 1,
        include_images: int = 0,
        include_thumbnails: int = 0,
    ) -> list[types.TextContent | types.ImageContent]:
        try:
            data = await asyncio.to_thread(self.pixiv.get_ranking, mode, content, pageno)
            raw_items = data.get("contents") or []
            if not isinstance(raw_items, list):
                raw_items = []
            lines: list[str] = [
                f"[Pixiv 排行] mode={mode} content={content} 第 {pageno} 页"
                f"（rank_total {data.get('rank_total', '?')}）"
            ]
            if str(mode).endswith("r18") and not self.config.pixiv_cookie:
                lines.append("⚠️ 未配置 PIXIV_PHPSESSID cookie，R18 排行可能返回空结果")
            if not raw_items:
                lines.append("（无排行数据）")
            for i, c in enumerate(raw_items, 1):
                w = self._pixiv_normalize(c)
                idx = w["rank"] or i
                lines.extend(self._pixiv_work_lines(w, idx))
            try:
                rank_lib = self._pixiv_lib(mode)
            except ValueError:
                rank_lib = None
            contents: list[types.TextContent | types.ImageContent] = []
            await self._pixiv_attach_images(
                [self._pixiv_normalize(c) for c in raw_items],
                f"pixiv:rank:{mode}",
                include_images,
                include_thumbnails,
                lines,
                contents,
                lib=rank_lib,
            )
            contents.insert(0, types.TextContent(type="text", text="\n".join(lines)))
            return contents
        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {e}")]

    async def pixiv_illust_detail(
        self,
        artwork_id: int,
        include_images: int = 1,
        include_thumbnails: int = 1,
    ) -> list[types.TextContent | types.ImageContent]:
        try:
            wid = str(int(artwork_id))
            body = await asyncio.to_thread(self.pixiv.get_illust_detail, wid)
            if not body:
                return [types.TextContent(type="text", text=f"作品 {wid} 不存在或已删除")]
            title = body.get("title") or "(无标题)"
            lines: list[str] = [f"[Pixiv 详情] {title}"]
            marks = []
            if body.get("aiType") == 1:
                marks.append("AI")
            if body.get("xRestrict"):
                marks.append("R18")
            if marks:
                lines.append(f"⚠️ {'/'.join(marks)}")
            lines.append(f"作品 ID: {wid}  https://www.pixiv.net/artworks/{wid}")
            user_id = body.get("userId", "")
            user_name = body.get("userName") or "(未知画师)"
            lines.append(f"画师: {user_name} (ID {user_id}) https://www.pixiv.net/users/{user_id}")
            tags = [t.get("tag") for t in (body.get("tags") or {}).get("tags", [])]
            if tags:
                lines.append(f"tags: {'、'.join(str(t) for t in tags[:15])}")
            stat = []
            width, height = body.get("width"), body.get("height")
            if width and height:
                stat.append(f"{width}x{height}")
            pages = body.get("pageCount") or 1
            if pages > 1:
                stat.append(f"{pages} 页")
            for label, key in (("收藏", "bookmarkCount"), ("赞", "likeCount"),
                               ("浏览", "viewCount"), ("评论", "commentCount"),
                               ("响应", "responseCount")):
                if body.get(key):
                    stat.append(f"{label} {body[key]}")
            if body.get("uploadDate"):
                stat.append(str(body["uploadDate"]))
            if stat:
                lines.append(f"   {' · '.join(stat)}")
            desc = (body.get("description") or "").strip()
            if desc:
                lines.append(f"简介: {desc[:500]}{'…' if len(desc) > 500 else ''}")
            contents: list[types.TextContent | types.ImageContent] = []

            page_originals: list[str] = []
            page_thumbs: list[str] = []
            for p in body.get("metaPages") or []:
                urls = p.get("imageUrls") or {}
                if urls.get("original"):
                    page_originals.append(urls["original"])
                if urls.get("thumb"):
                    page_thumbs.append(urls["thumb"])
            if not page_originals:
                urls = body.get("urls") or {}
                page_originals = [urls.get("original")] if urls.get("original") else []
                page_thumbs = [urls.get("thumb")] if urls.get("thumb") else []
            page_count = body.get("pageCount") or 1
            if page_count > len(page_originals) and page_originals:
                for p in range(1, page_count):
                    page_originals.append(re.sub(r"_p0(?=\.)", f"_p{p}", page_originals[0]))
                if page_thumbs:
                    for p in range(1, page_count):
                        page_thumbs.append(re.sub(r"_p0(?=\.)", f"_p{p}", page_thumbs[0]))

            if include_thumbnails > 0:
                cap = min(include_thumbnails, self.MAX_IMAGES_PER_CALL)
                thumbs = page_thumbs[:cap]
                to_fetch = [u for u in thumbs if self._thumb_cache_get(u) is None]
                if to_fetch:
                    for url, raw, _, _ in await self.pixiv.fetch_images_parallel(to_fetch):
                        if raw is not None:
                            self._thumb_cache_put(url, raw)
                for url in thumbs:
                    raw = self._thumb_cache_get(url)
                    if raw is None:
                        continue
                    contents.append(
                        types.ImageContent(
                            type="image",
                            data=base64.b64encode(raw).decode(),
                            mimeType=ImageLibrary.guess_mime(url),
                        )
                    )

            library_rows: list[str] = []
            if include_images > 0 and page_originals:
                w = self._pixiv_normalize(body)
                cap = min(include_images, self.MAX_IMAGES_PER_CALL)
                if len(page_originals) > cap:
                    lines.append(
                        f"（作品共 {len(page_originals)} 页，单次最多下载 {cap} 页原图）"
                    )
                pending: list[tuple[str, str]] = []
                for url in page_originals[:cap]:
                    hit_rec = self._pixiv_lib("daily").lookup_record(url)
                    if hit_rec is not None:
                        hit_path = self._pixiv_lib("daily").resolve_path(hit_rec)
                        if hit_path.is_file():
                            raw = hit_path.read_bytes()
                            mime = ImageLibrary.mime_for_file(str(hit_path))
                            contents.append(
                                types.ImageContent(
                                    type="image",
                                    data=base64.b64encode(raw).decode(),
                                    mimeType=mime,
                                )
                            )
                            library_rows.append(
                                f"   - {hit_rec.get('rel_dir')}/{hit_rec.get('lib_rel')}（图库命中）"
                            )
                            continue
                    lib_rel = self.library.lookup(url)
                    if lib_rel and self.library.path(lib_rel).is_file():
                        raw = self.library.path(lib_rel).read_bytes()
                        mime = ImageLibrary.mime_for_file(lib_rel)
                        contents.append(
                            types.ImageContent(
                                type="image",
                                data=base64.b64encode(raw).decode(),
                                mimeType=mime,
                            )
                        )
                        library_rows.append(f"   - {lib_rel}（图库命中）")
                        continue
                    pending.append((url, url))
                if pending:
                    fetched = await self.pixiv.fetch_images_parallel([u for _, u in pending])
                    for (url, _), (_, raw, _, ctype) in zip(pending, fetched):
                        if raw is None:
                            lines.append(f"   [第 {page_originals.index(url) + 1} 页] 原图下载失败")
                            continue
                        page_w = dict(w)
                        page_w["id"] = str(wid)
                        self._pixiv_emit_image(
                            page_w, url, raw, ctype, f"pixiv:{wid}",
                            contents, library_rows, lib=None,
                        )
                if library_rows:
                    lines.append("")
                    lines.append("📁 已存图库（永久保存）:")
                    lines.extend(library_rows)
            contents.insert(0, types.TextContent(type="text", text="\n".join(lines)))
            return contents
        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {e}")]

    @staticmethod
    def _pixiv_artwork_url(source_url: str) -> str:
        """从原图 URL 提取作品 ID，拼作品链接；无法提取返回空串。"""
        m = re.search(r"/(\d{5,})(?:-[0-9a-f]+)?_p\d+\.", source_url)
        if m is None:
            return ""
        return f"https://www.pixiv.net/artworks/{m.group(1)}"

    def _pixiv_lib(self, mode: str) -> ImageLibrary:
        """按 mode 懒构造三级目录对应的 SQLite 图库实例。"""
        lib = self._pixiv_libs.get(mode)
        if lib is None:
            bucket, subdir = pixiv_mode_parts(mode)
            lib = ImageLibrary(
                self.config.image_library_dir / "pixiv" / bucket / subdir,
                backend="sqlite",
                db_path=self._pixiv_db,
                bucket=bucket,
                subdir=subdir,
            )
            self._pixiv_libs[mode] = lib
        return lib

    def _pixiv_manual_lib(self, user_id: object) -> ImageLibrary:
        """按画师 userId 懒构造 manual SQLite 图库实例（image_library/pixiv/manual/<uid>/）。

        bucket/subdir 由存储时的 extra 逐条覆盖（x_restrict 判 NSFW、subdir=manual）。
        """
        uid = str(user_id or "").strip() or "0"
        lib = self._pixiv_manual_libs.get(uid)
        if lib is None:
            lib = ImageLibrary(
                self.config.image_library_dir / "pixiv" / "manual" / uid,
                backend="sqlite",
                db_path=self._pixiv_db,
                bucket="",
                subdir="manual",
            )
            self._pixiv_manual_libs[uid] = lib
        return lib

    @staticmethod
    def _mood_band(mood_score: int) -> str:
        """心情分数分档：1-33 差 / 34-66 中 / 67-100 好。"""
        if mood_score <= 33:
            return "bad"
        if mood_score <= 66:
            return "neutral"
        return "good"

    @staticmethod
    def _mood_candidates(band: str) -> tuple[str, str | None]:
        """档位 → (SFW 榜, NSFW 榜)；差档无 NSFW 候选。"""
        if band == "bad":
            return "daily", None
        if band == "neutral":
            return "weekly", "daily_r18"
        return "monthly", "weekly_r18"

    def _pick_mood_mode(self, band: str, prefer: str) -> str:
        """档位候选选榜：prefer 指定落点；未指定时按偏态概率（偏向 SFW）。"""
        sfw_mode, nsfw_mode = self._mood_candidates(band)
        pref = str(prefer or "").strip().lower()
        if pref == "nsfw" and nsfw_mode:
            return nsfw_mode
        if pref == "sfw":
            return sfw_mode
        if nsfw_mode and random.random() >= self.config.pixiv_mood_sfw_bias:
            return nsfw_mode
        return sfw_mode

    @staticmethod
    def _mood_beta_params(mood_score: int) -> tuple[float, float]:
        """心情分 → Beta 偏态分布参数 (α, β)。

        60 → (1,1) 均匀；越高分布越左偏（质量移向高分侧）且越尖；
        越低分布越右偏（质量移向低分侧）。
        """
        mu = 0.5 + (mood_score - 60) / 40 * 0.4
        mu = min(0.95, max(0.05, mu))
        nu = 2 + 6 * abs(mood_score - 60) / 40
        alpha = max(0.05, mu * nu)
        beta = max(0.05, (1 - mu) * nu)
        return alpha, beta

    @staticmethod
    def _beta_pdf(x: float, alpha: float, beta: float) -> float:
        """Beta 分布概率密度（log 空间计算，x ∈ [0,1]）。"""
        if x <= 0 or x >= 1:
            return 0.0
        lg = math.lgamma
        return math.exp(
            (alpha - 1) * math.log(x)
            + (beta - 1) * math.log1p(-x)
            + lg(alpha + beta)
            - lg(alpha)
            - lg(beta)
        )

    @staticmethod
    def _weighted_sample(weighted: list[tuple[dict, float]], n: int) -> list[dict]:
        """加权不重复采样 n 条（权重非正时退化为均匀随机）。"""
        pool = list(weighted)
        selected: list[dict] = []
        for _ in range(min(n, len(pool))):
            total = sum(w for _, w in pool)
            if total <= 0:
                rec = pool.pop(random.randrange(len(pool)))[0]
            else:
                r = random.uniform(0, total)
                cum = 0.0
                idx = len(pool) - 1
                for i, (_, w) in enumerate(pool):
                    cum += w
                    if cum >= r:
                        idx = i
                        break
                rec = pool.pop(idx)[0]
            selected.append(rec)
        return selected

    async def pixiv_local_rank(
        self,
        mood_score: int = 60,
        n: int = 20,
        mode: str = "",
        prefer: str = "",
    ) -> list[types.TextContent | types.ImageContent]:
        """按心情分数从本地图库概率选图：档位映射选榜 + 心情加权概率采样。"""
        try:
            try:
                mood = max(1, min(100, int(mood_score)))
            except (TypeError, ValueError):
                mood = 60
            count = max(1, min(int(n), self.MAX_IMAGES_PER_CALL))

            if str(mode).strip():
                mode = str(mode).strip()
                if mode not in ("daily", "weekly", "monthly", "daily_r18", "weekly_r18"):
                    raise ValueError("mode 仅支持 daily/weekly/monthly/daily_r18/weekly_r18")
                chosen = mode
                band = ""
            else:
                band = self._mood_band(mood)
                chosen = self._pick_mood_mode(band, prefer)

            lib = self._pixiv_lib(chosen)
            records = lib.search_rank_candidates(prefix="pixiv:rank:", limit=0)
            if not records:
                lines = [
                    f"[本地 Pixiv 榜库] {chosen} 榜",
                    "（该榜暂无本地图库，先让 pixiv_daily_rank.py 入库后再试）",
                ]
                return [types.TextContent(type="text", text="\n".join(lines))]

            scored = []
            for rec in records:
                entry = self.rank_scorer.entry(rec.get("source_url") or "")
                scored.append((rec, entry["score"] if entry else None))
            scores = [s for _, s in scored if s is not None]
            smin, smax = (min(scores), max(scores)) if scores else (0, 0)
            span = smax - smin
            alpha, beta = self._mood_beta_params(mood)
            weighted = []
            for rec, s in scored:
                if s is None:
                    w = 0.1
                elif span <= 0:
                    w = self._beta_pdf(0.5, alpha, beta)
                else:
                    w = self._beta_pdf((s - smin) / span, alpha, beta)
                weighted.append((rec, w))
            selected = self._weighted_sample(weighted, count)

            # R18 榜只允许返回已有预生成描述的图（无描述图不发送，避免触发 VLM 识别）
            is_nsfw = chosen.endswith("r18")
            desc_skipped = 0
            if is_nsfw:
                keep = []
                for rec in selected:
                    if str(rec.get("description") or "").strip():
                        keep.append(rec)
                    else:
                        desc_skipped += 1
                selected = keep

            band_cn = {"bad": "差", "neutral": "中", "good": "好"}
            lines = [
                f"[本地 Pixiv 榜库] 心情 {mood}/100"
                + (f"（档位 {band_cn[band]}）" if band else "")
                + f" → 榜 {chosen}",
                f"（候选 {len(records)} 张，心情加权概率预选 {count} 张"
                + (f"，NSFW 无描述剔除 {desc_skipped} 张" if desc_skipped else "")
                + "）",
            ]

            contents: list[types.TextContent | types.ImageContent] = []
            for i, rec in enumerate(selected):
                source_url = rec.get("source_url") or ""
                lib_rel = rec.get("lib_rel") or ""
                path = lib.resolve_path(rec)
                if not lib_rel or not path.is_file():
                    continue
                raw = path.read_bytes()
                rec_mime = str(rec.get("mime") or "").strip().lower()
                if rec_mime in ImageLibrary.EXT_BY_MIME:
                    mime = rec_mime
                else:
                    mime = ImageLibrary.mime_for_file(lib_rel)
                artwork = self._pixiv_artwork_url(source_url)
                fetched = rec.get("fetched_at") or 0
                when = time.strftime("%Y-%m-%d", time.localtime(fetched)) if fetched else "?"
                entry = self.rank_scorer.entry(source_url)
                if entry:
                    meta = f"评分 {entry['score']} · 最高第 {entry['best_rank']} 名 · 上榜 {entry['count']} 次"
                else:
                    meta = "暂无排名"
                rank_now = rec.get("rank")
                if rank_now:
                    meta += f" · 当期第 {rank_now} 名"
                desc = str(rec.get("description") or "").strip()
                line = f"  {i + 1}. {artwork or source_url}（{meta} · 入库 {when}）"
                if desc:
                    line += f"\n     描述：{desc}"
                lines.append(line)
                contents.append(
                    types.ImageContent(
                        type="image",
                        data=base64.b64encode(raw).decode(),
                        mimeType=mime,
                    )
                )
            if not contents:
                if is_nsfw and desc_skipped and not selected:
                    lines.append("（该榜图片均无预生成描述，暂无可返回图；待 pixiv_describe.py 生成后重试）")
                else:
                    lines.append("（图库文件缺失，无法返回图片）")
                return [types.TextContent(type="text", text="\n".join(lines))]
            contents.insert(0, types.TextContent(type="text", text="\n".join(lines)))
            return contents
        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {e}")]

    def register_tools(self, mcp: MCPServer) -> None:
        mcp.tool(
            description="通过 SearXNG 聚合搜索网页。query 必填；language 如 zh-CN/en；"
            "time_range 取 day/week/month/year；pageno 页码；safesearch 0/1/2。"
            "返回搜索结果 markdown 列表，末尾附注未响应引擎。",
            meta={"visibility": "visible"},
        )(self.web_search)
        mcp.tool(
            description="通过 SearXNG 图片引擎聚合搜索图片。query 必填；language 如 zh-CN/en；"
            "pageno 页码；safesearch 0/1/2；include_images 为 0 时只返回文本列表"
            "（标题+原图 URL+来源页 URL）；include_thumbnails 大于 0 时额外下载前 N 张"
            "缩略图预览（仅供判断，请勿直接发送，确认后用 include_images 获取原图）；"
            "include_images 大于 0 时并行下载前 N 张原图，永久保存到本地图库"
            "image_library/ 并以 MCP 图片内容块返回（失败的原图自动降级为文本链接）。"
            "单次最多 20 张、并发 8；同一图片自动去重；缩略图进程内缓存复用。",
            meta={"visibility": "visible"},
        )(self.image_search)
        mcp.tool(
            description="抓取网页并返回结构化预览：标题、最终 URL、标题目录、正文前 preview_chars 字符。"
            "默认经代理抓取（use_proxy=True）；国内站点可传 use_proxy=False 直连。"
            "git 托管站（github/gitlab/gitee 等）仓库内容 URL 自动本地 clone 分析"
            "（仓库根→文件树+README，blob/raw→文件内容），避开托管站限流；"
            "同 URL 十分钟内重复调用命中缓存，不做二次抓取。",
            meta={"visibility": "visible"},
        )(self.web_fetch)
        mcp.tool(
            description="从网页正文中抽取包含 query 的标题块（最近一个 # 标题起）及其段落，多命中分块列出。"
            "缓存未命中则先抓取一次；同 URL 后续调用直接命中缓存。",
            meta={"visibility": "visible"},
        )(self.web_fetch_section)
        mcp.tool(
            description="抓取网页全文（markdown），截断到 max_chars（默认 16000，优先在段落/代码块边界断）。"
            "若截断，正文末尾附注 [已截断，全文 N 字符]。同 URL 十分钟内命中缓存。",
            meta={"visibility": "visible"},
        )(self.web_fetch_full)
        mcp.tool(
            description="Pixiv 关键词搜索插画。keyword 必填；pageno 页码；mode 取 all/safe/r18"
            "（r18 需配置 PIXIV_PHPSESSID cookie）；s_mode 取 s_tag（tag 精确）/s_tag_full"
            "（tag 部分）/s_tc（标题+简介）；order 取 popular_d（按热度，默认）/"
            "date_d（最新）/date（最旧）/popular_male_d/popular_female_d，按热度请求失败"
            "时自动降级为 date_d 重试。返回作品列表（标题/画师/tags/尺寸/页数/"
            "收藏/AI 标记/作品链接）；include_thumbnails 大于 0 时额外下载前 N 张缩略图预览"
            "（仅供判断，请勿直接发送）；include_images 大于 0 时并行下载前 N 张原图，"
            "永久保存到本地图库 image_library/ 并以 MCP 图片内容块返回"
            "（非 jpg 原图自动回退详情接口解析，ugoira 动图跳过）。单次最多 20 张。",
            meta={"visibility": "visible"},
        )(self.pixiv_search)
        mcp.tool(
            description="Pixiv 画师（用户）插画列表。user_id 必填（画师主页 URL 的 /users/ 后数字）；"
            "max_works 最多展示的作品数（上限 100）；include_thumbnails/include_images 同 pixiv_search。"
            "返回画师全部插画数量与作品列表，图片可下载原图入图库。",
            meta={"visibility": "visible"},
        )(self.pixiv_user_illusts)
        mcp.tool(
            description="Pixiv 排行榜。mode 取 daily/weekly/monthly/rookie/original/male/female/"
            "daily_r18/weekly_r18 等（r18 类需配置 PIXIV_PHPSESSID cookie）；content 取 "
            "illust/manga；pageno 页码。返回带名次的作品列表；include_thumbnails/include_images "
            "同 pixiv_search。",
            meta={"visibility": "visible"},
        )(self.pixiv_ranking)
        mcp.tool(
            description="Pixiv 作品详情。artwork_id 必填；返回标题/画师/tags/尺寸/页数/收藏/赞/浏览/"
            "上传时间/简介/作品链接，并默认（include_images=1）下载全部页面原图永久入图库"
            "image_library/，include_thumbnails=1 附带页面缩略图预览。",
            meta={"visibility": "visible"},
        )(self.pixiv_illust_detail)
        mcp.tool(
            description="按心情从本地图库 image_library/pixiv 概率选图（最多 20 张），"
            "以 MCP 图片内容块返回（附评分/最高名次/上榜次数/入库日期）。"
            "mood_score 为心情量化分 1-100（默认 60，取 get_maibot_mood 的 willingness 值）："
            "1-33 差→SFW 日榜；34-66 中→SFW 周榜/NSFW 日榜；67-100 好→SFW 月榜/NSFW 周榜"
            "（中/好档候选榜偏态偏向 SFW，prefer 可传 sfw/nsfw 按语境指定落点）。"
            "选图按心情生成的 Beta 偏态分布加权概率预选：心情越好概率质量越偏向"
            "评分高的图（低分图几乎不出现），越差越偏向低分图。"
            "mode 可显式指定榜种（daily/weekly/monthly/daily_r18/weekly_r18）绕过心情映射。",
            meta={"visibility": "visible"},
        )(self.pixiv_local_rank)

    def web_search(
        self,
        query: str,
        language: str | None = None,
        time_range: str | None = None,
        pageno: int = 1,
        safesearch: int | None = None,
    ) -> str:
        try:
            params = {
                "format": "json",
                "q": query,
                "pageno": max(1, pageno),
            }
            if language:
                params["language"] = language
            if time_range:
                params["time_range"] = time_range
            if safesearch is not None:
                params["safesearch"] = int(safesearch)

            data = self.searxng.search(params)
            if not data.get("results") and data.get("unresponsive_engines"):
                time.sleep(1)
                data = self.searxng.search(params)

            results = data.get("results") or []
            lines = []
            for i, r in enumerate(results, 1):
                title = r.get("title") or "(无标题)"
                url = r.get("url", "")
                content = (r.get("content") or "").strip()
                lines.append(f"{i}. [{title}]({url})")
                if content:
                    lines.append(f"   {content}")
            if not lines:
                lines.append("（无搜索结果）")

            unresponsive = data.get("unresponsive_engines") or []
            if unresponsive:
                notes = [f"{name}({reason})" for name, reason in unresponsive]
                lines.append("")
                lines.append(f"⚠️ 未响应引擎: {'、'.join(notes)}")
            return "\n".join(lines)
        except Exception as e:
            return f"错误: {e}"

    async def image_search(
        self,
        query: str,
        language: str | None = None,
        pageno: int = 1,
        safesearch: int | None = None,
        include_images: int = 0,
        include_thumbnails: int = 0,
    ) -> list[types.TextContent | types.ImageContent]:
        try:
            params = {
                "format": "json",
                "q": query,
                "pageno": max(1, pageno),
                "categories": "images",
            }
            if language:
                params["language"] = language
            if safesearch is not None:
                params["safesearch"] = int(safesearch)

            data = await asyncio.to_thread(self.searxng.search, params)
            if not data.get("results") and data.get("unresponsive_engines"):
                await asyncio.sleep(1)
                data = await asyncio.to_thread(self.searxng.search, params)

            results = data.get("results") or []
            lines = []
            for i, r in enumerate(results, 1):
                title = r.get("title") or "(无标题)"
                img_src = r.get("img_src") or ""
                thumb_src = r.get("thumbnail_src") or ""
                url = r.get("url", "")
                engine = r.get("engine", "")
                lines.append(f"{i}. {title}")
                if engine:
                    lines.append(f"   引擎: {engine}")
                if img_src:
                    lines.append(f"   原图: {img_src}")
                if thumb_src and not img_src:
                    lines.append(f"   缩略图: {thumb_src}")
                if url:
                    lines.append(f"   来源: {url}")
            if not lines:
                lines.append("（无搜索结果）")

            contents: list[types.TextContent | types.ImageContent] = []
            library_rows: list[str] = []

            if include_images > 0:
                cap = min(include_images, self.MAX_IMAGES_PER_CALL)
                total_with_img = sum(1 for r in results if r.get("img_src"))
                seen_src: set[str] = set()
                originals = []
                for r in results:
                    src = r.get("img_src")
                    if src and src not in seen_src:
                        seen_src.add(src)
                        originals.append(r)
                if len(originals) < total_with_img:
                    lines.append(f"（同一图片多引擎重复返回，已去重为 {len(originals)} 张）")
                originals = originals[:cap]
                if include_images > self.MAX_IMAGES_PER_CALL:
                    lines.append(
                        f"（单次最多下载 {self.MAX_IMAGES_PER_CALL} 张原图，本次已截取前 {cap} 张）"
                    )
                pending: list[tuple[dict, str]] = []
                for r in originals:
                    img_src = r["img_src"]
                    lib_rel = self.library.lookup(img_src)
                    if lib_rel:
                        path = self.library.path(lib_rel)
                        if path.is_file():
                            raw = path.read_bytes()
                            mime = ImageLibrary.mime_for_file(lib_rel)
                            contents.append(
                                types.ImageContent(
                                    type="image",
                                    data=base64.b64encode(raw).decode(),
                                    mimeType=mime,
                                )
                            )
                            library_rows.append(
                                f"   - {lib_rel}（图库命中，{r.get('engine', '')}）"
                            )
                            continue
                    pending.append((r, img_src))

                if pending:
                    fetched = await self.fetcher.fetch_images_parallel(
                        [img_src for _, img_src in pending], use_proxy=True
                    )
                    for (r, img_src), (_, raw, err) in zip(pending, fetched):
                        if raw is None:
                            contents.append(
                                types.TextContent(
                                    type="text",
                                    text=f"[图片下载失败，仅保留链接] {img_src}: {err}",
                                )
                            )
                            continue
                        mime = ImageLibrary.guess_mime(img_src)
                        lib_rel = self.library.store(
                            raw, img_src, mime, query, r.get("engine") or ""
                        )
                        contents.append(
                            types.ImageContent(
                                type="image",
                                data=base64.b64encode(raw).decode(),
                                mimeType=mime,
                            )
                        )
                        library_rows.append(f"   - {lib_rel}（{r.get('engine', '')}）")

            thumbnail_count = 0
            if include_thumbnails > 0:
                cap = min(include_thumbnails, self.MAX_IMAGES_PER_CALL)
                thumbs: list[str] = []
                for r in results:
                    t = r.get("thumbnail_src")
                    if t and t not in thumbs:
                        thumbs.append(t)
                if len(thumbs) > cap:
                    lines.append(
                        f"（单次最多下载 {self.MAX_IMAGES_PER_CALL} 张缩略图，本次已截取前 {cap} 张）"
                    )
                thumbs = thumbs[:cap]
                if thumbs:
                    lines.insert(
                        0,
                        "注意：本次附带的图片为缩略图预览，仅供判断相关性，"
                        "请勿直接发送；确认后请用 include_images 获取原图。",
                    )
                    to_fetch = [u for u in thumbs if self._thumb_cache_get(u) is None]
                    fetched_thumbs = await self.fetcher.fetch_images_parallel(
                        to_fetch, use_proxy=True
                    )
                    for url, raw, _ in fetched_thumbs:
                        if raw is not None:
                            self._thumb_cache_put(url, raw)
                    for url in thumbs:
                        raw = self._thumb_cache_get(url)
                        if raw is None:
                            continue
                        thumbnail_count += 1
                        contents.append(
                            types.ImageContent(
                                type="image",
                                data=base64.b64encode(raw).decode(),
                                mimeType=ImageLibrary.guess_mime(url),
                            )
                        )
                    if thumbnail_count < len(thumbs):
                        lines.append(
                            f"（缩略图预览 {thumbnail_count}/{len(thumbs)} 张可用，"
                            "失败的已跳过）"
                        )

            if library_rows:
                lines.append("")
                lines.append("📁 已存图库（永久保存）:")
                lines.extend(library_rows)

            unresponsive = data.get("unresponsive_engines") or []
            if unresponsive:
                notes = [f"{name}({reason})" for name, reason in unresponsive]
                lines.append("")
                lines.append(f"⚠️ 未响应引擎: {'、'.join(notes)}")

            contents.insert(0, types.TextContent(type="text", text="\n".join(lines)))
            return contents
        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {e}")]

    def web_fetch(self, url: str, use_proxy: bool = True, preview_chars: int = 2000) -> str:
        try:
            self.fetcher.validate_url(url)
            entry, cache_hit = self._entry_for(url, use_proxy)
            md = entry["markdown"]
            heads = MarkdownAnalyzer(md).headings()
            preview, truncated = PageProcessor.truncate_md(md, max(1, preview_chars))
            parts = [
                f"[{'缓存命中' if cache_hit else '本次抓取'}]",
                f"标题: {entry['title']}",
                f"最终 URL: {entry['final_url']}",
            ]
            if heads:
                parts.append("目录:")
                parts.extend(heads[:50])
            parts.append("")
            parts.append("正文预览:")
            parts.append(preview if preview else "（无正文内容）")
            if truncated:
                parts.append(f"\n[预览已截断至 {preview_chars} 字符，全文 {len(md)} 字符]")
            return "\n".join(parts)
        except Exception as e:
            return f"错误: {e}"

    def web_fetch_section(self, url: str, query: str, use_proxy: bool = True) -> str:
        try:
            self.fetcher.validate_url(url)
            if not query.strip():
                raise ValueError("query 不能为空")
            entry, cache_hit = self._entry_for(url, use_proxy)
            md = entry["markdown"]
            needle = query.strip().lower()
            analyzer = MarkdownAnalyzer(md)
            matched = [
                (h, b)
                for h, b in analyzer.sections()
                if needle in f"{h} {b}".lower()
            ]
            parts = [f"[{'缓存命中' if cache_hit else '本次抓取'}]", f"标题: {entry['title']}"]
            if not matched:
                parts.append(f"正文中未找到包含 {query!r} 的内容")
                parts.append("目录:")
                parts.extend(analyzer.headings()[:50] or ["（无标题）"])
                return "\n".join(parts)
            for h, b in matched:
                parts.append("")
                parts.append(h or "（正文开头）")
                parts.append(b)
            return "\n".join(parts)
        except Exception as e:
            return f"错误: {e}"

    def web_fetch_full(self, url: str, max_chars: int = 16000, use_proxy: bool = True) -> str:
        try:
            self.fetcher.validate_url(url)
            entry, cache_hit = self._entry_for(url, use_proxy)
            md = entry["markdown"]
            body, truncated = PageProcessor.truncate_md(md, max(1, max_chars))
            parts = [
                f"[{'缓存命中' if cache_hit else '本次抓取'}]",
                f"标题: {entry['title']}",
                f"最终 URL: {entry['final_url']}",
                "",
            ]
            parts.append(body if body else "（无正文内容）")
            if truncated:
                parts.append(f"\n[已截断，全文 {len(md)} 字符]")
            return "\n".join(parts)
        except Exception as e:
            return f"错误: {e}"


mcp = MCPServer("searxng", version="1.0.0")

app = SearxngServer()
app.register_tools(mcp)

web_search = app.web_search
image_search = app.image_search
web_fetch = app.web_fetch
web_fetch_section = app.web_fetch_section
web_fetch_full = app.web_fetch_full
pixiv_search = app.pixiv_search
pixiv_user_illusts = app.pixiv_user_illusts
pixiv_ranking = app.pixiv_ranking
pixiv_illust_detail = app.pixiv_illust_detail
pixiv_local_rank = app.pixiv_local_rank


def main() -> None:
    if "--http" in sys.argv:
        mcp.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=8765,
            streamable_http_path="/mcp",
            stateless_http=True,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()

