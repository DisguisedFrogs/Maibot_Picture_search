# mcp-searxng — 自建 MCP 搜索服务器

基于本地 SearXNG 实例的 MCP（Model Context Protocol）服务器，向 opencode、MaiBot 等客户端暴露 9 个工具：

| 工具 | 功能 |
| --- | --- |
| `web_search(query, language, time_range, pageno, safesearch)` | 通过 SearXNG 聚合搜索，返回结果 markdown 列表 |
| `image_search(query, language, pageno, safesearch, include_images, include_thumbnails)` | 通过 SearXNG 图片引擎聚合搜索图片；`include_images=N` 并行下载前 N 张原图永久保存到本地图库 `image_library/` 并以 MCP 图片内容块返回，`include_thumbnails=N` 额外下载缩略图预览（不落盘） |
| `web_fetch(url, use_proxy, preview_chars)` | 结构化预览：标题、最终 URL、标题目录、正文前 N 字符 |
| `web_fetch_section(url, query, use_proxy)` | 按需抽取包含指定关键词的标题块及段落 |
| `web_fetch_full(url, max_chars, use_proxy)` | 抓取正文全文（markdown），超长截断并标记 |
| `pixiv_search(keyword, pageno, mode, s_mode, order, include_images, include_thumbnails)` | Pixiv 关键词搜索插画（支持 R18 与热度排序），可下载原图入图库 |
| `pixiv_user_illusts(user_id, max_works, include_images, include_thumbnails)` | Pixiv 画师插画列表 |
| `pixiv_ranking(mode, content, pageno, include_images, include_thumbnails)` | Pixiv 插画/漫画排行榜 |
| `pixiv_illust_detail(artwork_id, include_images, include_thumbnails)` | Pixiv 作品详情，多页作品默认下载全部页面原图入图库 |
| `pixiv_local_rank(mood_score, n, mode, prefer)` | 按心情从本地图库概率选图（发图链路核心） |

- 语言：Python 3.10+ + 官方 `mcp` SDK；systemd 托管，Streamable HTTP 传输（`127.0.0.1:8765/mcp`），stdio 模式保留（`server.py` 不带参数）
- 搜索后端：本地 SearXNG JSON API（`http://127.0.0.1/searxng/search`），图片搜索走其 `categories=images`
- 网页抓取：`httpx` 独立抓取 + `trafilatura` 提取 markdown；图片下载走代理候选 failover
- 缓存：进程内存缓存（同 URL 10 分钟内命中，LRU 上限 5 条）
- 图库：原图永久保存于 `image_library/`（SHA256 去重 + jsonl 索引；Pixiv 图库为 SQLite 三级结构），永不自动清理
- git 仓库本地化：github/gitlab/gitee/bitbucket/codeberg 仓库 URL 自动全量 clone 到 `git_cache/` 本地分析，避开托管站限流
- 代理：通过环境变量 `SEARXNG_PROXY_CANDIDATES` 配置代理候选（逗号分隔，按序 failover），如 `SEARXNG_PROXY_CANDIDATES=http://127.0.0.1:<代理端口>,http://127.0.0.1:<备用端口>`；未设置则直连

## 前置条件

- Python 3.10+
- 本地 SearXNG 实例（含 valkey、nginx），JSON API 已启用
- 可选：v2rayA 等本地 HTTP 代理（如 `http://127.0.0.1:<代理端口>`，经 `SEARXNG_PROXY_CANDIDATES` 配置），用于抓取国际站点与 Pixiv
- 可选：`PIXIV_PHPSESSID` 环境变量（Pixiv 登录 cookie，登录态可访问 R18 内容并显著降低 429 限流）

## 安装

```bash
cd ~/mcp-searxng
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### SearXNG 侧一次性配置

**a. 启用 JSON API 格式**（`/etc/searxng/settings.yml`）：

```yaml
search:
  formats:
    - html
    - json
```

**b. 本机限流白名单**（新建 `/etc/searxng/limiter.toml`，放行本机程序化请求）：

```toml
[botdetection.ip_lists]
pass_ip = ['127.0.0.1']
```

**c. 国外引擎走代理**（中国网络环境必做）：在 `settings.yml` 中为国外引擎逐一配置 `proxies` 并调大超时（国内引擎保持直连）：

```yaml
outgoing:
  request_timeout: 8.0
  max_request_timeout: 12.0

engines:
  - name: bing
    proxies:
      http: [http://127.0.0.1:<代理端口>]
      https: [http://127.0.0.1:<代理端口>]
  # ...其余国外引擎结构相同
```

**d. 生效并验证**：

```bash
sudo systemctl reload uwsgi@searxng
curl 'http://127.0.0.1/searxng/search?q=test&format=json'   # 期望 200 + 非空 results
```

### 注册为 systemd 服务

`/etc/systemd/system/mcp-searxng.service`（`User` 与 `WorkingDirectory` 按实际部署路径调整）：

```ini
[Unit]
Description=MCP SearXNG server (Streamable HTTP)
After=network.target valkey.service
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/mcp-searxng
Environment=PIXIV_PHPSESSID=<你的 Pixiv cookie>    # 可选；不填则匿名访问，易 429 限流
Environment=SEARXNG_PROXY_CANDIDATES=http://127.0.0.1:<代理端口>,http://127.0.0.1:<备用端口>    # 可选；未设置则直连
ExecStart=/home/youruser/mcp-searxng/.venv/bin/python /home/youruser/mcp-searxng/server.py --http
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-searxng
```

### 客户端接入

服务暴露标准 MCP **Streamable HTTP** 端点（stateless，不追踪会话 ID），任意支持远程 MCP 的客户端均可接入：

- 端点：`http://127.0.0.1:8765/mcp`（仅绑定回环、无鉴权）

```json
{
  "mcpServers": {
    "searxng": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

接入成功后客户端即可获得 9 个工具。MaiBot 需在**重启或配置触发热重载**后才会发现新工具。

## Pixiv 图库（可选，发图链路）

- 每日排行入库：`scripts/pixiv_daily_rank.py`（六榜：日/周/月 + r18，按作品 ID 判重、性别标签过滤、评分累积），由 services/ 下 systemd timers 定时触发：
  - 日榜每日 22:00，周榜每周日 22:30，月榜每月 1 号 22:30，r18 榜对应顺延
- 图片描述预生成：`scripts/pixiv_describe.py` 调用 dashscope 为图库图片生成中文描述（模型取 MaiBot「为模型分配功能」配置的 VLM 模型，未配置时脚本提醒退出；每小时自动补齐，R18 图被审核拒答时本次放弃该图，下次运行自动重试）
- 图库结构：`image_library/pixiv/`（SQLite 单库 + `files/` 物理单份存储 + SFW/NSFW 榜目录元数据）
- `pixiv_local_rank` 按心情分数（1-100）分档选榜 + Beta 偏态概率加权选图，与 MaiBot 心情插件（本仓库根目录的 maibot-mood）配合发图：
  1. 模型调 `get_maibot_mood()` 取心情与发图意愿
  2. 调 `pixiv_local_rank(mood_score=willingness)` 选图
  3. 经 `send_image(media_index="tool_result:<调用ID>:<序号>")` 发送

安装 timers：`sudo ./services/install.sh`；卸载：`sudo ./services/uninstall.sh`（安装后需手动编辑 `/etc/systemd/system/` 下单元文件中的 `Environment` 凭证行）。

## 自测

```bash
.venv/bin/python -c "
import sys
sys.path.insert(0, '.')
import server

print(server.web_search('test'))
"
```

## 卸载

```bash
sudo systemctl disable --now mcp-searxng
sudo rm /etc/systemd/system/mcp-searxng.service
sudo systemctl daemon-reload
```

## 维护与故障排查

- 状态：`systemctl status mcp-searxng`、`journalctl -u mcp-searxng -f`
- 存活探测：`curl -X POST http://127.0.0.1:8765/mcp -H 'Content-Type: application/json' -d '{}'` 应回 400
- `web_fetch` 国际页报"连接失败"：v2rayA 线路问题，面板切换节点
- 搜索返回 429/403：limiter.toml 白名单缺失，或外网 IP 访问（设计如此，仅放行本机）
- `pixiv_*` 429：cookie 过期（更新单元文件 `Environment` 行后 `daemon-reload && restart`）或代理线路波动

## 安全说明

- `web_fetch` 系列仅接受 http/https URL；MCP 端点仅绑定 `127.0.0.1:8765`，不对外暴露；SearXNG 限流白名单仅含本机
- 图库/缓存目录永不自动清理，删除 `image_library/`、`git_cache/` 即可清空

## License

MIT
