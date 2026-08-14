"""SearXNG JSON API 客户端。"""

import httpx
from .config import ServerConfig
from .network import DownloadManager

class SearxngClient:
    def __init__(self, config: ServerConfig, download: DownloadManager):
        self._config = config
        self._download = download

    def search(self, params: dict) -> dict:
        timeout = httpx.Timeout(
            self._config.search_timeout_total,
            connect=self._config.search_timeout_connect,
            read=self._config.search_timeout_read,
            write=self._config.search_timeout_write,
            pool=self._config.search_timeout_pool,
        )
        client = self._download.sync_client(None)
        resp = client.get(f"{self._config.searxng_base}/search", params=params, timeout=timeout)
        if resp.status_code in (429, 403):
            raise RuntimeError(
                f"SearXNG 返回 HTTP {resp.status_code}（限流或拒绝访问，请稍后再试）"
            )
        resp.raise_for_status()
        return resp.json()

