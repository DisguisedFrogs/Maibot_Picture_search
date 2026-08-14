"""mcp_searxng 包：MCP SearXNG 服务器（面向对象模块化实现）。

模块分层（依赖单向，无循环）：
  config/exceptions → network → cache/markdown/library/gitcache
  → fetcher/pixiv/searxng → app（SearxngServer 编排 + MCP 注册）

兼容入口：根目录 server.py 为薄壳，`import server` 与 systemd
ExecStart 行为不变。
"""

from .cache import PageCache
from .config import ServerConfig
from .exceptions import CandidateFailure, FetchError, SiteError, SizeLimitError
from .fetcher import PageFetcher
from .gitcache import GitRepoCache
from .library import ImageLibrary, RankScorer, pixiv_mode_parts
from .markdown import MarkdownAnalyzer, PageProcessor
from .network import DownloadManager, UrlNormalizer
from .pixiv import PixivClient
from .searxng import SearxngClient
from .app import (
    SearxngServer,
    app,
    image_search,
    main,
    mcp,
    pixiv_illust_detail,
    pixiv_local_rank,
    pixiv_ranking,
    pixiv_search,
    pixiv_user_illusts,
    web_fetch,
    web_fetch_full,
    web_fetch_section,
    web_search,
)

__all__ = [
    "ServerConfig",
    "FetchError",
    "CandidateFailure",
    "SiteError",
    "SizeLimitError",
    "UrlNormalizer",
    "DownloadManager",
    "PageCache",
    "MarkdownAnalyzer",
    "PageProcessor",
    "ImageLibrary",
    "RankScorer",
    "pixiv_mode_parts",
    "GitRepoCache",
    "PageFetcher",
    "PixivClient",
    "SearxngClient",
    "SearxngServer",
    "mcp",
    "app",
    "web_search",
    "image_search",
    "web_fetch",
    "web_fetch_section",
    "web_fetch_full",
    "pixiv_search",
    "pixiv_user_illusts",
    "pixiv_ranking",
    "pixiv_illust_detail",
    "pixiv_local_rank",
    "main",
]
