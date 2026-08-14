"""网页/图片抓取：代理候选 failover、超时与体积上限、页面缓存接入。"""

import asyncio
import time
from urllib.parse import urlparse
import httpx
from .cache import PageCache
from .config import ServerConfig
from .exceptions import CandidateFailure, FetchError, SiteError, SizeLimitError
from .markdown import PageProcessor
from .network import DownloadManager

class PageFetcher:
    def __init__(self, config: ServerConfig, cache: PageCache, download: DownloadManager):
        self._config = config
        self._cache = cache
        self._download = download

    def validate_url(self, url: str) -> str:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("URL 不能为空")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"非法 URL scheme: {parsed.scheme!r}，仅允许 http/https")
        if not parsed.netloc:
            raise ValueError(f"非法 URL（缺少主机名）: {url}")
        return url

    def get_page(self, url: str, use_proxy: bool) -> tuple[dict, bool]:
        cache_hit = self._cache.get(url)
        if cache_hit is not None:
            return dict(cache_hit), True
        html, final_url = self.fetch_html(url, use_proxy)
        md = PageProcessor.extract_text(html)
        title = PageProcessor.extract_title(html)
        entry = {
            "markdown": md,
            "title": title,
            "final_url": final_url,
            "fetched_at": time.time(),
        }
        self._cache.put(url, entry)
        return dict(entry), False

    def fetch_html(self, url: str, use_proxy: bool) -> tuple[str, str]:
        candidates = self._config.proxy_candidates or (None,)
        labels = self._config.proxy_candidates or ("直连",)
        if not use_proxy:
            candidates = (None,)
            labels = ("直连",)
        deadline = time.monotonic() + self._config.page_timeout_total
        errors = []
        for proxy, label in zip(candidates, labels):
            if time.monotonic() >= deadline:
                break
            try:
                return self._fetch_once(url, proxy, deadline)
            except CandidateFailure as e:
                errors.append(f"{label}: {e}")
                continue
        mode = "代理" if use_proxy else "直连"
        raise FetchError(f"连接失败（{mode}，候选均不可用）: {'; '.join(errors)}")

    def _fetch_once(self, url: str, proxy: str | None, deadline: float) -> tuple[str, str]:
        timeout = httpx.Timeout(
            self._config.page_timeout_total,
            connect=self._config.page_timeout_connect,
            read=self._config.page_timeout_read,
            write=self._config.page_timeout_write,
            pool=self._config.page_timeout_pool,
        )
        client = self._download.sync_client(proxy)
        try:
            with client.stream("GET", url, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raise SiteError(
                        f"HTTP {resp.status_code} {resp.reason_phrase} — 站点拒绝访问 {url}"
                    )
                chunks = []
                size = 0
                for chunk in resp.iter_bytes():
                    if time.monotonic() > deadline:
                        raise CandidateFailure(
                            f"总耗时超过 {int(self._config.page_timeout_total)}s，已放弃 {url}"
                        )
                    size += len(chunk)
                    if size > self._config.max_body_bytes:
                        raise SizeLimitError(
                            f"响应体超过 {self._config.max_body_bytes} 字节上限，已停止下载"
                            f"（拒绝喂半截页面给解析器）: {url}"
                        )
                    chunks.append(chunk)
                html = b"".join(chunks).decode("utf-8", errors="replace")
                return html, str(resp.url)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise CandidateFailure(str(e)) from e

    async def fetch_image(self, url: str, use_proxy: bool) -> bytes:
        deadline = time.monotonic() + self._config.image_timeout_total
        candidates = self._config.proxy_candidates or (None,)
        if not use_proxy:
            candidates = (None,)
        errors = []
        timeout = httpx.Timeout(
            self._config.image_timeout_total,
            connect=self._config.image_timeout_connect,
            read=self._config.image_timeout_read,
            write=self._config.image_timeout_write,
            pool=self._config.image_timeout_pool,
        )
        for proxy in candidates:
            try:
                client = self._download.async_client(proxy)
                async with client.stream("GET", url, timeout=timeout) as resp:
                    if resp.status_code >= 400:
                        raise SiteError(
                            f"HTTP {resp.status_code} {resp.reason_phrase} — 站点拒绝访问 {url}"
                        )
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
                    return b"".join(chunks)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                errors.append(str(e))
                continue
        mode = "代理" if use_proxy else "直连"
        raise FetchError(f"图片下载失败（{mode}）: {'; '.join(errors)}")

    async def fetch_images_parallel(
        self, urls: list[str], use_proxy: bool = True
    ) -> list[tuple[str, bytes | None, str | None]]:
        """并行下载多张图片，返回 [(url, bytes | None, error | None)]，顺序与 urls 一致。

        重复 URL 只下载一次（并发上限 8），结果按输入顺序回填。
        """

        seen: set[str] = set()
        unique = [u for u in urls if not (u in seen or seen.add(u))]

        async def fetch_one(url: str) -> tuple[str, bytes | None, str | None]:
            try:
                async with self._download.semaphore:
                    return url, await self.fetch_image(url, use_proxy), None
            except Exception as e:
                return url, None, str(e)

        fetched = await asyncio.gather(*(fetch_one(u) for u in unique))
        by_url = {url: (raw, err) for url, raw, err in fetched}
        return [(u, by_url[u][0], by_url[u][1]) for u in urls]

