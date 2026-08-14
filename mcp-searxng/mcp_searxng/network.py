"""URL 规范化与共享连接池下载调度（代理候选 failover、并发上限、同步+异步客户端）。"""

import asyncio
import threading
from urllib.parse import urlparse
import httpx
from .config import ServerConfig

class UrlNormalizer:
    """URL 规范化：归一缓存键，避免同一页面的多种写法占用多个缓存槽。"""

    @staticmethod
    def normalize(url: str) -> str:
        try:
            parsed = urlparse(url)
        except ValueError:
            return url
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return url
        host = (parsed.hostname or "").lower()
        if ":" in host:  # IPv6 字面量，原样返回避免重构破坏
            return url
        path = parsed.path
        if path == "/":
            path = ""
        while len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme}://{netloc}{path}{query}"


class DownloadManager:
    """共享连接池 + 并发上限 + 去重的下载调度。

    - 按代理候选（直连/各代理）懒建共享 httpx client（sync+async 两套），
      连接与 TLS 握手跨请求复用
    - 图片下载经 asyncio.Semaphore 限制并发（默认 8）
    - fetch_images_parallel 输入保序去重，重复 URL 只下载一次
    """

    def __init__(self, config: ServerConfig, max_concurrency: int = 8):
        self._config = config
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._sync_clients: dict[str, httpx.Client] = {}
        self._clients_lock = threading.Lock()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    def sync_client(self, proxy: str | None) -> httpx.Client:
        with self._clients_lock:
            client = self._sync_clients.get(proxy)
            if client is None:
                client = httpx.Client(
                    headers={"User-Agent": self._config.user_agent},
                    follow_redirects=True,
                    proxy=proxy,
                )
                self._sync_clients[proxy] = client
            return client

    def async_client(self, proxy: str | None) -> httpx.AsyncClient:
        with self._clients_lock:
            client = self._clients.get(proxy)
            if client is None:
                client = httpx.AsyncClient(
                    headers={"User-Agent": self._config.user_agent},
                    follow_redirects=True,
                    proxy=proxy,
                )
                self._clients[proxy] = client
            return client

