"""Pixiv 前端 AJAX API 客户端：搜索/排行/画师/详情 + 原图下载。"""

import asyncio
import re
import threading
import time
from urllib.parse import quote, urlencode
import httpx
from .config import ServerConfig
from .exceptions import FetchError, SiteError, SizeLimitError
from .network import DownloadManager

class PixivClient:
    """Pixiv 前端 AJAX API 客户端：搜索/排行/画师/详情 + 原图下载。

    - 全部请求走代理候选 failover（Pixiv 国内不可直连）
    - 带 Referer: https://www.pixiv.net/ 与可选 PHPSESSID cookie（防限流、可看 R18）
    - 原图 URL 由缩略图正则推导（免详情请求），推导失败/下载失败回退详情接口取精确 URL
    """

    _IMG_RE = re.compile(
        r"^https://i\.pximg\.net/(?:c/[^/]+/)?img-master/img/"
        r"(\d{4}/\d{2}/\d{2}/\d{2}/\d{2}/\d{2})/(\d+)_p(\d+)_(?:square1200|master1200)\.(?:jpg|webp)$"
    )
    _UGOIRA_TYPE = 2

    def __init__(self, config: ServerConfig, download: DownloadManager):
        self._config = config
        self._download = download
        self._last_ajax = 0.0
        self._ajax_lock = threading.Lock()

    def _headers(self, accept_json: bool = True) -> dict[str, str]:
        headers = {"Referer": self._config.pixiv_referer}
        if accept_json:
            headers["Accept"] = "application/json"
        if self._config.pixiv_cookie:
            headers["Cookie"] = f"PHPSESSID={self._config.pixiv_cookie}"
        return headers

    def _throttle(self) -> None:
        with self._ajax_lock:
            wait = self._config.pixiv_ajax_delay - (time.monotonic() - self._last_ajax)
            if wait > 0:
                time.sleep(wait)
            self._last_ajax = time.monotonic()

    def get_json(self, url: str) -> dict:
        """AJAX JSON 请求：代理候选 failover；429/站点拒绝直接报错不换代理。"""
        self._throttle()
        timeout = httpx.Timeout(
            self._config.page_timeout_total,
            connect=self._config.page_timeout_connect,
            read=self._config.page_timeout_read,
            write=self._config.page_timeout_write,
            pool=self._config.page_timeout_pool,
        )
        errors = []
        for proxy in self._config.proxy_candidates or (None,):
            try:
                client = self._download.sync_client(proxy)
                resp = client.get(url, headers=self._headers(), timeout=timeout)
                if resp.status_code == 429:
                    raise SiteError(
                        "Pixiv 限流（HTTP 429），请稍后再试；建议配置 PIXIV_PHPSESSID "
                        "cookie 缓解（systemd 单元加 Environment=PIXIV_PHPSESSID=...）"
                    )
                if resp.status_code >= 400:
                    raise SiteError(f"Pixiv HTTP {resp.status_code} — 站点拒绝访问 {url}")
                data = resp.json()
                if data.get("error"):
                    raise SiteError(
                        f"Pixiv 接口报错: {data.get('message') or data.get('error')} — {url}"
                    )
                return data
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                errors.append(str(e))
                continue
        raise FetchError(f"Pixiv 连接失败（代理候选均不可用）: {'; '.join(errors)}")

    def search_artworks(
        self,
        keyword: str,
        pageno: int = 1,
        mode: str = "all",
        s_mode: str = "s_tag",
        order: str = "date_d",
    ) -> dict:
        q = quote(keyword)
        params = urlencode({"word": keyword, "order": order, "mode": mode, "p": max(1, pageno), "s_mode": s_mode})
        return self.get_json(f"https://www.pixiv.net/ajax/search/artworks/{q}?{params}")

    def get_ranking(self, mode: str = "daily", content: str = "illust", pageno: int = 1) -> dict:
        params = urlencode({"mode": mode, "content": content, "p": max(1, pageno), "format": "json"})
        return self.get_json(f"https://www.pixiv.net/ranking.php?{params}")

    def get_user_work_ids(self, user_id: int) -> list[str]:
        data = self.get_json(f"https://www.pixiv.net/ajax/user/{user_id}/profile/all?lang=zh")
        return list((data.get("body") or {}).get("illusts") or {})

    def get_user_illusts(self, user_id: int, ids: list[str]) -> list[dict]:
        works: list[dict] = []
        for i in range(0, len(ids), 20):
            chunk = ids[i : i + 20]
            q = "&".join(f"ids%5B%5D={wid}" for wid in chunk)
            url = (
                f"https://www.pixiv.net/ajax/user/{user_id}/profile/illusts"
                f"?{q}&work_category=illustManga&is_first_page={1 if i == 0 else 0}"
            )
            data = self.get_json(url)
            works_dict = (data.get("body") or {}).get("works") or {}
            if isinstance(works_dict, dict):
                works.extend(works_dict.values())
        return works

    def get_illust_detail(self, artwork_id: str) -> dict:
        data = self.get_json(f"https://www.pixiv.net/ajax/illust/{artwork_id}?lang=zh")
        return data.get("body") or {}

    @classmethod
    def original_url(cls, img_url: str) -> str | None:
        """缩略图/普通图 URL → 原图 URL 推导；不匹配返回 None。"""
        m = cls._IMG_RE.match(img_url)
        if m is None:
            return None
        return f"https://i.pximg.net/img-original/img/{m.group(1)}/{m.group(2)}_p{m.group(3)}.jpg"

    async def fetch_image(self, url: str, use_proxy: bool = True) -> tuple[bytes, str]:
        """下载图片（带 Referer/Cookie），返回 (bytes, content_type)。"""
        deadline = time.monotonic() + self._config.image_timeout_total
        timeout = httpx.Timeout(
            self._config.image_timeout_total,
            connect=self._config.image_timeout_connect,
            read=self._config.image_timeout_read,
            write=self._config.image_timeout_write,
            pool=self._config.image_timeout_pool,
        )
        candidates = (self._config.proxy_candidates or (None,)) if use_proxy else (None,)
        errors = []
        headers = self._headers(accept_json=False)
        for proxy in candidates:
            try:
                client = self._download.async_client(proxy)
                async with client.stream("GET", url, headers=headers, timeout=timeout) as resp:
                    if resp.status_code >= 400:
                        raise SiteError(f"HTTP {resp.status_code} — 站点拒绝访问 {url}")
                    chunks = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        if time.monotonic() > deadline:
                            raise FetchError(
                                f"总耗时超过 {int(self._config.image_timeout_total)}s，已放弃 {url}"
                            )
                        size += len(chunk)
                        if size > self._config.max_image_bytes:
                            raise SizeLimitError(
                                f"图片超过 {self._config.max_image_bytes} 字节上限，"
                                f"已停止下载: {url}"
                            )
                        chunks.append(chunk)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                    return b"".join(chunks), ctype or "image/jpeg"
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                errors.append(str(e))
                continue
        raise FetchError(f"图片下载失败（代理）: {'; '.join(errors)}")

    async def fetch_images_parallel(
        self, urls: list[str], use_proxy: bool = True
    ) -> list[tuple[str, bytes | None, str | None, str]]:
        """并行下载多张图片，返回 [(url, bytes|None, error|None, content_type)]，保序去重。"""

        seen: set[str] = set()
        unique = [u for u in urls if not (u in seen or seen.add(u))]

        async def fetch_one(url: str) -> tuple[str, bytes | None, str | None, str]:
            try:
                async with self._download.semaphore:
                    raw, ctype = await self.fetch_image(url, use_proxy)
                return url, raw, None, ctype
            except Exception as e:
                return url, None, str(e), "image/jpeg"

        fetched = await asyncio.gather(*(fetch_one(u) for u in unique))
        by_url = {url: (raw, err, ctype) for url, raw, err, ctype in fetched}
        return [(u, by_url[u][0], by_url[u][1], by_url[u][2]) for u in urls]

