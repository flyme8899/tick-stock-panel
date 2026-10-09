# 多源资讯与热门事件

侧栏「热门事件」读取本地资讯库，列出近 24 小时相对前 4 日基线升温的板块和个股。市场环境页的主线仍按价格和涨停梯队计算，两套结果不互相覆盖。

采集默认全部关闭。现有部署不会因为升级就开始出网。

## 来源

| 来源 | 谁去拉 | 内容 |
| --- | --- | --- |
| 钉钉作文实时 | 宿主机 `dws`，只读 | 群号来自 `NEWS_DWS_GROUP_ID`，留空则未配置。图片只存 mediaId |
| 知识星球纳指星球调研 | 宿主机 `zsxq-cli`，只读 | 星球号来自 `NEWS_ZSXQ_GROUP_ID`，留空则未配置。话题标签不拼回正文 |
| ima爱分享 | TSP 进程 | 知识库「【爱分享】的财经资讯」。只读标题，不请求正文 |
| 财联社 | TSP 进程 | 电报。保留级别、个股和板块 |
| 华尔街见闻 | TSP 进程 | 全球和 A 股快讯。保留标的、主题和热度 |

钉钉和知识星球的登录态在宿主机，不在容器里。宿主机脚本把 JSON 写到 `data/news/inbox/`，TSP 再入库。不开放无鉴权的 HTTP 写入接口。

财联社、华尔街见闻、ima 由面板进程里的单线程轮询。交易日 09:15–15:05 电报和快讯约 45 秒一次，钉钉和知识星球约 5 分钟一次；白天其余时间更疏，ima 约 6 小时一次。

## 开关

每个来源独立。页面上的开关写到 `data/user_data/preferences.json` 的 `news_sources`。环境变量优先，设了之后页面不能改：

```ini
NEWS_DWS_ENABLED=false
NEWS_ZSXQ_ENABLED=false
NEWS_IMA_ENABLED=false
NEWS_CLS_ENABLED=false
NEWS_WSCN_ENABLED=false
```

ima 还要 `IMA_CLIENT_ID` 和 `IMA_API_KEY`，缺一则保持关闭。可选 `IMA_KB_ID`；留空时按知识库名称查找。

钉钉群号和知识星球号留空表示未配置，对应来源无法开启：

```ini
NEWS_DWS_GROUP_ID=
NEWS_ZSXQ_GROUP_ID=
```

登录失效时，若配置了 `DINGTALK_WEBHOOK_URL`（可选 `DINGTALK_SECRET`），6 小时内对同一来源只发一条提醒，正文不含资讯内容。

可选 `NEWS_LLM_EXTRACT=true` 时，词典抽不到股票或板块才会调用现有 AI 客户端，每小时最多 10 次，并且只接受词典里已有的名称或代码。默认关闭。

## 宿主机采集

在仓库根目录、已登录 `dws` 和 `zsxq-cli` 的机器上：

```bash
python scripts/news_host_collector.py --data-dir ./data
```

脚本只允许 `dws auth status`、`dws chat +chat-messages`、`dws chat message list`、`zsxq-cli auth status`、`zsxq-cli group +topics`。发帖、评论、发送会被拒绝。

知识星球补历史（可选，从 2025-08-23 起）：

```bash
python scripts/news_host_collector.py --data-dir ./data --backfill-since 2025-08-23
```

示例 systemd 单元在 `deploy/tsp-news-collector.service` 和 `.timer`，默认每 5 分钟跑一次。单元以用户 `ubuntu` 运行，`HOME=/home/ubuntu`，工作目录是 `/home/ubuntu/tick-stock-panel`。`TimeoutStartSec=3600` 留给知识星球长回补。`ProtectSystem=strict` 下只有 `data/news` 可写。仓库不在这个路径时，改 `WorkingDirectory`、`Environment=DATA_DIR`、`EnvironmentFile`、`ExecStart` 和 `ReadWritePaths`。

`dws` 如果把登录态放在系统钥匙串或 Secret Service，systemd 没有用户的 D-Bus 会话，服务里会看成未登录。部署后在同一单元环境里执行一次 `dws auth status` 确认。Docker 把 `./data` 挂进容器，宿主机写入的收件箱会被面板读到。

## 库和接口

SQLite 在 `data/news/news.sqlite`，保留约 45 天。同一来源的 `source_id` 只入库一次。跨源用正文指纹合并成一条故事；短文本不按正文合并，避免纯图片互相撞车。

对外接口只返回摘录，不返回 `raw_json`：

| 方法 | 路径 | 谁能调 |
| --- | --- | --- |
| GET | `/api/news/hot` | 面板会话，或 `read:analysis` Token |
| GET | `/api/news/messages` | 同上 |
| GET | `/api/news/stocks/{symbol}` | 同上 |
| GET | `/api/news/health` | 仅面板会话 |
| PUT | `/api/news/sources` | 仅面板会话 |
| GET | `/api/news/dsa-feed` | 仅请求头 `X-News-Feed-Token`。令牌用至少 32 字节的随机串，不要放进 URL。不匹配时 404 |

这三条 GET 进入开放契约（Tier A，`read:analysis`）。feed、健康检查和开关不开放。

## 接到 DSA

不修改 `vendor/daily_stock_analysis`。`backend/app/custom/dsa/news_bridge.py` 在 DSA 进程启动时由 `dsa_bootstrap.py` 装上，属于运行时补丁：

- 增加情报源类型 `tsp`。
- 只放行指向 `/api/news/dsa-feed`、且主机在允许名单里的地址（`localhost`、`127.0.0.1`、`::1`、`host.docker.internal`、`app`、`tsp`，以及 `TSP_NEWS_BASE_URL` 的主机）。
- 为五个采集源和「TSP热门候选」各建一个情报源。
- 每条资讯写成市场范围一行，再按股票（最多 8 个，规范代码如 `600519.SH`）和板块（最多 6 个）各写一行，个股分析才能按标签命中。
- 情报源关闭时拒绝拉取，不把状态记成失败。写入后按 DSA 的 `news_intel_retention_days` 删过期行，返回值带最多 5 条 `sample_items`。
- 大盘复盘合并本地情报时，把最多 4 条热门候选插到前面，避免电报占满 6 条窗口。
- 不依赖 `NEWS_INTEL_AUTO_FETCH_ENABLED`，因此不会顺带打开 NewsNow 公共源。

Docker：`docker compose --profile dsa` 把桥文件只读挂到 `/opt/tsp/news_bridge.py`，并设置 `TSP_NEWS_BASE_URL=http://app:3018`。本地 `scripts/dsa.sh` 默认访问 `http://127.0.0.1:3018`。

`NEWS_DSA_FEED_TOKEN` 留空时桥直接返回，DSA 照常启动。TSP 未开、网络失败或当前不是 DSA 进程，都只记日志。面板采集不依赖 DSA 是否在跑。
