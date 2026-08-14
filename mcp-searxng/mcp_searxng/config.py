#!/usr/bin/env python3
"""服务配置：全局参数（路径/超时/代理/UA/cookie）。"""

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_proxy_candidates(value: str) -> tuple[str, ...]:
    """从环境变量解析代理候选（逗号分隔、去空白、过滤空项），未设置时返回空元组（=直连）。"""
    return tuple(p.strip() for p in value.split(",") if p.strip())


@dataclass(frozen=True)


class ServerConfig:
    searxng_base: str = "http://127.0.0.1/searxng"
    proxy_candidates: tuple[str, ...] = _parse_proxy_candidates(
        os.environ.get("SEARXNG_PROXY_CANDIDATES", "")
    )
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    max_body_bytes: int = 2 * 1024 * 1024
    max_image_bytes: int = 10 * 1024 * 1024
    cache_ttl: float = 600
    cache_max: int = 5
    image_library_dir: Path = Path(__file__).resolve().parent.parent / "image_library"
    git_cache_dir: Path = Path(__file__).resolve().parent.parent / "git_cache"
    git_hosts: tuple[str, ...] = (
        "github.com",
        "gitlab.com",
        "gitee.com",
        "bitbucket.org",
        "codeberg.org",
        "raw.githubusercontent.com",
    )
    git_clone_timeout: float = 300.0
    git_pull_timeout: float = 60.0
    git_tree_max_entries: int = 200
    git_readme_chars: int = 4000
    git_file_chars: int = 20000
    page_timeout_total: float = 35.0
    page_timeout_connect: float = 15.0
    page_timeout_read: float = 15.0
    page_timeout_write: float = 5.0
    page_timeout_pool: float = 5.0
    image_timeout_total: float = 30.0
    image_timeout_connect: float = 15.0
    image_timeout_read: float = 15.0
    image_timeout_write: float = 5.0
    image_timeout_pool: float = 5.0
    search_timeout_total: float = 45.0
    search_timeout_connect: float = 3.0
    search_timeout_read: float = 40.0
    search_timeout_write: float = 5.0
    search_timeout_pool: float = 5.0
    pixiv_cookie: str = os.environ.get("PIXIV_PHPSESSID", "")
    pixiv_referer: str = "https://www.pixiv.net/"
    pixiv_ajax_delay: float = 0.3
    pixiv_mood_sfw_bias: float = 0.7

