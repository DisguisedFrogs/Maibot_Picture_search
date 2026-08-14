#!/usr/bin/env python3
"""兼容薄壳：实现已拆分至 mcp_searxng/ 包（面向对象模块化）。

保留本文件以维持对外接口不变：`import server`（README 自测）、
systemd ExecStart `server.py --http`、pixiv_daily_rank.py 的
`from server import ...` 均零改动。
"""

from mcp_searxng import *  # noqa: F401,F403
from mcp_searxng import __all__, main  # noqa: F401

if __name__ == "__main__":
    main()
