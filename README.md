# maibot-mood 心情插件

按群 LLM 分析聊天氛围心情（差/中/好），输出发图意愿分数并持久化；供模型按心情从本地 Pixiv 图库选图发图。

## 功能

- `@Tool get_maibot_mood`：分析当前群最近 N 条消息（默认 20 条），输出心情档位（差/中/好）+ 发图意愿（0-100）+ 理由 + 语境关键词
- 持久化：每群心情/意愿结果原子写入统一持久化目录 `data/plugins/<插件ID>/mood_store.json`，插件/进程重启不丢失
- 更新策略：无新消息直接复用持久化值；新增消息不足门槛（默认 3 条）时复用并等待累计；发图意愿新旧值按 α=0.5 加权平滑防跳变；心情档位切换需连续两次分析一致才生效（防抖）
- webui 图库信息：配置页展示 Pixiv 图库图片存储数量（数据库总数、SFW/NSFW 及各榜明细、物理文件数）

## 前置依赖

本插件只负责分析心情，发图链路需要以下外部组件（自行部署）：

1. **mcp-searxng MCP 服务器**：提供 `pixiv_local_rank`（按心情从本地图库选图）、`pixiv_search` 等工具；源码在本仓库 [`mcp-searxng/`](mcp-searxng/) 子目录（SearXNG 实例 + MCP 服务器，Streamable HTTP 接入 MaiBot）
2. **Pixiv 图库数据库（pixiv.db）**：由 mcp-searxng 的 `pixiv_daily_rank.py` 定时爬取榜图生成（SQLite，含图片评分）
3. **图库描述预生成**（可选）：`pixiv_describe.py` 为图库图片预生成中文描述，供 R18 图免临时 VLM

## 安装

将本仓库 `plugin.py`、`_manifest.json`、`config.toml` 放入 MaiBot `plugins/maibot-mood/`（或通过插件市场安装），重启 MaiBot 或等待热重载。

## 配置

配置页（webui → 插件 → maibot-mood）可调：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `window` | 20 | 分析最近多少条消息 |
| `min_new_messages` | 3 | 距上次分析新增多少条消息才触发重新分析 |
| `willingness_alpha` | 0.5 | 发图意愿平滑系数（新分析结果权重） |
| `level_confirm_times` | 2 | 心情档位切换需连续多少次分析一致才生效 |
| `model` | 空 | 分析所用模型任务名（`model_config.toml` 中 `[model_task_config.<任务名>]` 的键，**不能填模型名**；留空使用默认） |
| `max_keywords` | 5 | 语境关键词数量上限 |
| `prompt_template` | 内置 | 分析 Prompt 模板，`{messages}` 为消息占位符 |
| `pixiv_db_path` | 空 | Pixiv 图库数据库路径（pixiv.db），用于配置页展示图库信息 |

## 使用链路

模型按以下流程发图：

1. 调用 `get_maibot_mood()` 取心情（档位 + willingness 分数）
2. 调用 MCP 工具 `pixiv_local_rank(mood_score=willingness)` 自动分档选榜 + 概率预选候选
3. 结合氛围挑 1 张（群里求涩图可 `prefer="nsfw"` 指定落点）
4. 经 `send_image(media_index="tool_result:<调用ID>:<序号>")` 发送

## License

MIT
