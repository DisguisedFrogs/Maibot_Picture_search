"""进程内 LRU 页面缓存（TTL 10 分钟，键经 UrlNormalizer 归一）。"""

import threading
import time
from collections import OrderedDict
from .config import ServerConfig
from .network import UrlNormalizer

class PageCache:
    def __init__(self, ttl: float = ServerConfig.cache_ttl, max_size: int = ServerConfig.cache_max):
        self._ttl = ttl
        self._max_size = max_size
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, url: str) -> dict | None:
        key = UrlNormalizer.normalize(url)
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if now - entry["fetched_at"] > self._ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return entry

    def put(self, url: str, entry: dict) -> None:
        key = UrlNormalizer.normalize(url)
        with self._lock:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

