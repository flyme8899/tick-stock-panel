# 决策工作台（daily_stock_analysis）

TSP 保留自己的界面和数据链路。 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)（MIT，提交 `ce364e457aab288863a5707e7b3df79786ad07f2`）以源码快照放在 `vendor/daily_stock_analysis/`，作为旁路服务运行。浏览器只访问 TSP，由 `backend/app/custom/dsa/` 转发到 DSA 的 `/api/v1`。

DSA 自带的 Web 和桌面界面没有打包进来。决策相关页面在 `frontend/src/custom/dsa/`，沿用 TSP 的布局、菜单、颜色和浅色 / 深色主题。

## 怎么跑

只启动 TSP 时，侧栏「决策」可以打开，并标明服务未连接。样例只用于看版式。

一起启动 sidecar：

```bash
DSA_AUTOSTART=1 ./dev.sh
```

或单独：

```bash
./scripts/dsa.sh
```

Windows：

```powershell
$env:DSA_AUTOSTART='1'; .\dev.ps1
# 或
.\scripts\dsa.ps1
```

Docker 在现有 compose 里加了可选服务，不改变原来的单服务启动：

```bash
# 在 .env 中设置 DSA_BASE_URL=http://dsa:8000
docker compose --profile dsa up --build
```

sidecar 默认监听 `127.0.0.1:8000`，数据库放在 `data/dsa/stock_analysis.db`。`ENV_FILE` 指向仓库根目录的 `.env`，所以 TSP 和 DSA 共用一份配置。镜像时区是 `Asia/Shanghai`，本地 `scripts/dsa.sh` / `dsa.ps1` 在未设置 `TZ` 时也使用这个时区。

Docker 服务带健康检查：容器内 `curl -fsS http://127.0.0.1:8000/api/v1/health`。这个接口免登录，HTTP 失败或连不上都会让容器变为 unhealthy。决策页徽标每 30 秒请求 TSP 的 `/api/dsa/status`，悬停可看到同样的探测结果。

## ETF 轮动

决策页「运行轮动」调用 `POST /api/dsa/jobs/etf-rotation`。命令固定为 `python main.py --etf-rotation --no-notify`，股票池和成本只读 `ETF_ROTATION_*`，请求不能改参数。

TSP 的 app 镜像里没有 `vendor/daily_stock_analysis`。在这个镜像里，接口不会在 app 容器中找解释器，而是请求 sidecar 的 `POST /api/v1/tsp/etf-rotation`。该入口由 `dsa_bootstrap.py` 在 DSA 导入自己的 API 时挂上，并在 sidecar 的工作目录里执行上面的命令。因此需要 `docker compose --profile dsa`，且 `DSA_BASE_URL=http://dsa:8000`。app 服务不挂载 Docker 套接字。

本机仓库里已经有 `vendor/daily_stock_analysis/main.py` 和解释器时，仍由 TSP 进程直接执行。没有这份源码、sidecar 也没连上时，接口返回 `ok: false` 和原因，不会改用系统里的其他 Python。单次运行超过 180 秒会按超时失败。

上游没有单独的 ETF 轮动 HTTP 接口，所以没有改 `vendor/daily_stock_analysis`。

## 量化回测证据

`backend/app/custom/dsa/quant_evidence.yaml` 保存 TSP 机械回测对六类 DSA 技能的对照（均线金叉、缩量回踩、放量突破、多头趋势、龙头、底部放量）。窗口是 2026-07-08 至 2026-10-08，全市场 5882 只，同期基准 -4.00%。数字来自当时的回测报告，负收益是无差别执行的基准线，不是禁用令。

`scripts/dsa.sh`、`scripts/dsa.ps1` 和 `docker compose --profile dsa` 会在启动 `main.py` 之前装上钩子，只给同名技能的提示末尾追加一段「量化回测参考」。`wave_theory` 等未收录技能保持原文。本地脚本在变量未设置时使用仓库内文件；`.env` 或环境里写成 `off`，或文件缺失时，不追加，也不影响 sidecar 启动。Compose 把该变量固定成容器内的 `/opt/tsp/quant_evidence.yaml`，避免 `.env` 里的宿主机路径进容器；要在 Docker 里关闭，把这一项改成 `off`。证据文件不放进 `vendor/daily_stock_analysis/strategies/`，那个目录会被当成策略加载。

决策仪表盘读取 `GET /api/dsa/quant-evidence` 展示同一份摘要。直接在 vendor 目录里执行 `python main.py` 不会经过这个钩子。

## 资讯桥

热门事件采集的资讯可以进入 DSA 情报库，供个股分析、情报页、大盘复盘和定时推送使用。不改 `vendor/daily_stock_analysis`。`dsa_bootstrap.py` 启动时加载同目录的 `news_bridge.py`，运行时放行情报源类型 `tsp`，并只允许访问配置好的 TSP feed 地址。

五个来源各建一个情报源：钉钉作文实时、知识星球纳指星球调研、ima爱分享、财联社、华尔街见闻。每条资讯除市场范围外，按股票和板块再写一行，标签因此能被个股分析命中。另有「TSP热门候选」，大盘复盘合并新闻时插到前面。

`NEWS_DSA_FEED_TOKEN` 留空、TSP 没开或网络失败时只记日志，DSA 照常启动。不打开 `NEWS_INTEL_AUTO_FETCH_ENABLED`。Docker 设置 `TSP_NEWS_BASE_URL=http://app:3018` 并只读挂载桥文件；本地脚本默认 `http://127.0.0.1:3018`。采集开关和宿主机步骤见 [news-sources.md](./news-sources.md)。

## 定时分析

不使用 GitHub Actions。每日任务由 sidecar 进程内的调度器执行：`python main.py --serve-only` 在 `SCHEDULE_ENABLED=true` 时会恢复任务，但不会在启动时立刻分析。

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `SCHEDULE_ENABLED` | `false` | `true` 后按下面的时刻跑 |
| `SCHEDULE_TIME` | `18:00` | 上海时间，24 小时制 `HH:MM` |
| `SCHEDULE_RUN_IMMEDIATELY` | `false` | 保持 `false`，避免重启容器就打一轮 |
| `TRADING_DAY_CHECK_ENABLED` | `true` | 非交易日跳过 |
| `MARKET_REVIEW_REGION` | `cn` | 大盘复盘市场。自托管 A 股用 `cn` |
| `STOCK_LIST` | 空 | 要分析的代码，逗号分隔 |

决策页「定时推送」可以改这些值。保存会写回 `.env`，并让 sidecar 重新加载调度，所以 Docker 里该文件是可写挂载。多个时点以页面上的一个时刻为准，保存后 `SCHEDULE_TIMES` 与 `SCHEDULE_TIME` 相同。

结果写入 DSA 历史，决策仪表盘能看到。推送走 `.env` 里的 `FEISHU_WEBHOOK_URL`、`WECHAT_WEBHOOK_URL`、Telegram、Discord、Slack、邮件等，不改 TSP 设置页里的监控渠道。页面上的「生成研报」仍然 `notify=false`；定时任务和「立即跑一轮」按这些渠道发送。

## 分享图

`docker compose --profile dsa up --build` 使用的镜像安装了 `wkhtmltopdf`（含 `wkhtmltoimage`）和 `fonts-noto-cjk`，并执行 `fc-cache`。个股研报里的「分享图」在页内生成 PNG。工具缺失或渲染失败时，按钮下方显示上游返回的原因，不会把浏览器带到一份 JSON。

## 环境变量

集成自身：

| 变量 | 作用 |
| --- | --- |
| `DSA_BASE_URL` | 上游地址。默认 `http://127.0.0.1:8000`。设为空字符串则页面保持未启用 |
| `DSA_AUTOSTART` | `1` 时 `dev.sh` / `dev.ps1` 拉起 sidecar |
| `DSA_PORT` | sidecar 端口，默认 8000 |
| `DSA_PYTHON` | 本机跑 ETF 轮动所用的解释器。未设置时用 `vendor/daily_stock_analysis/.venv`。Docker 下由 dsa 服务执行，不读这一项 |
| `SCREENING_ENABLED` | 默认 `false`。设为 `true` 后决策页「多市场选股」可用。关闭时上游返回 `screening_disabled` |
| `EFINANCE_PRIORITY` | efinance 在日 K 路由中的优先级，数字越小越优先。建议 `EFINANCE_PRIORITY=3`：TickFlow 默认优先级是 2，efinance 默认 0 会排在它前面；设为 3 后 TickFlow 先于 efinance。AkShare 默认仍是 1。已登记指数的日 K，以及实时行情，不使用这个值。改完后重启 sidecar |
| `DSA_UPSTREAM_COOKIE` | DSA 打开 `ADMIN_AUTH_ENABLED` 后转发给上游的 Cookie。未配置 `DSA_PASSWORD` 时只使用这一项 |
| `DSA_PASSWORD` | 上游管理密码。配置后由转发层登录并缓存会话，过期时间跟随 Set-Cookie，提前刷新。留空则行为与只配 Cookie 时相同。不要把真实密码写进仓库 |
| `TSP_QUANT_EVIDENCE_FILE` | 机械回测证据 YAML。启动脚本和 compose 默认指向仓库内文件。`off` 关闭注入 |
| `TSP_NEWS_BASE_URL` | DSA 拉取 TSP 资讯的地址。Compose 固定为 `http://app:3018`，本地默认 `http://127.0.0.1:3018` |
| `NEWS_DSA_FEED_TOKEN` | 资讯 feed 令牌。TSP 与 DSA 用同一份 `.env`。留空则不拉取 |
| `DSA_TIMEOUT_SECONDS` | 转发超时。分析、问股、选股、回测默认更长 |

DSA 读取的密钥和数据源（写在同一个 `.env`，留空则对应能力失败并给出原因）：

`SCHEDULE_ENABLED`、`SCHEDULE_TIME`、`SCHEDULE_RUN_IMMEDIATELY`、`TRADING_DAY_CHECK_ENABLED`、`MARKET_REVIEW_REGION`、`STOCK_LIST`、`SCREENING_ENABLED`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`AIHUBMIX_KEY`、`ANSPIRE_API_KEYS`、`TUSHARE_TOKEN`、`TICKFLOW_API_KEY`、`EFINANCE_PRIORITY`、`SERPAPI_API_KEYS`、`TAVILY_API_KEYS`、`BOCHA_API_KEYS`、`BRAVE_API_KEYS`、`MINIMAX_API_KEYS`、`SEARXNG_BASE_URLS`、`WECHAT_WEBHOOK_URL`、`FEISHU_WEBHOOK_URL`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`DISCORD_WEBHOOK_URL`、`SLACK_BOT_TOKEN`、`SLACK_CHANNEL_ID`、`EMAIL_SENDER`、`EMAIL_PASSWORD`、`ETF_ROTATION_POOL`、`ETF_ROTATION_SAFE_ASSET`。

sidecar 启动时，如果 `OPENAI_API_KEY` 为空且 TSP 已配置 `AI_API_KEY`，会借用 `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`。`TICKFLOW_API_KEY` 两边同名，直接共用。

页面里的「生成研报」默认 `notify=false`，避免试点时把消息打进群。定时任务和「立即跑一轮」仍按 DSA 自己的通知配置发送。

## 功能落在哪里

| DSA 能力 | TSP 位置 |
| --- | --- |
| AI 决策仪表盘 | 决策 → 决策仪表盘 |
| 个股 LLM 报告、分享图 | 决策 → 个股研报；个股预览底部有入口 |
| 实时新闻 / 情报源 | 决策 → 情报。TSP 热门事件采集在令牌配好后写入同一情报库 |
| A 股、港股、美股、日股、韩股、台股、ETF | 决策 → 多市场 |
| 规则选股 | 决策 → 多市场选股 |
| ETF 双动量轮动 | 决策 → ETF 轮动 |
| Agent 问股 | 决策 → 问股 |
| 机器人斜杠命令 | 决策 → 机器人 |
| 定时运行与多渠道通知 | 决策 → 定时推送 |
| 分析预警 | 决策 → 决策预警 |
| 决策信号复盘 | 决策 → 决策信号 |
| 组合风险 | 决策 → 持仓风险 |
| 历史建议回测 | 决策 → 决策回测 |
| 图片 / CSV / 剪贴板导入 | 决策 → 智能导入 |
| 用量与环境变量说明 | 决策 → 用量配置 |

自选页工具栏的「决策研报」打开当前列表第一只股票的研报。

## 重叠时的取舍

- 看板、个股分析、策略选股、回测、监控中心、复盘推送、持仓提醒、模拟盘、AI 助手都保留原实现。
- 个股分析继续做关键价位和四维流式说明。DSA 的买卖点位报告不并进那条计算链路，避免两套分数混在一个接口里。
- 策略页仍是全 A 的 Polars 选股。DSA 选股是另一套规则引擎，只出现在决策工作台。
- 回测页仍做 T+1、费用和滑点。决策回测只评估已经生成的 AI 建议。
- 监控和复盘继续用 TSP 设置里的飞书、企微、邮件。DSA 的定时报告使用上面的 Webhook 环境变量，两套渠道互不改写。
- AI 助手继续操作因子、策略和回测。问股只对话 DSA Agent。
- 飞书、钉钉等平台长连接由 sidecar 进程持有。面板里的机器人页执行同一套命令，并调用相同的 HTTP API。

## 还没接上的部分

- DSA 的 Web / 桌面皮肤不会作为第二个产品出现。
- 上游测试、评测集和文档配图没有放进快照。升级时按 `VENDOR.md` 里的提交重新同步。
- 决策信号在 `ADMIN_AUTH_ENABLED=true` 时还要上游登录态。配置 `DSA_PASSWORD` 后转发层会自动登录并在会话过期前刷新；未配置时仍只转发 `DSA_UPSTREAM_COOKIE`。
- 只跑 `scripts/dsa.sh` 创建的 Python 虚拟环境时，系统里若没有 `wkhtmltoimage` 和中文字体，分享图会在页面上提示失败。Docker 镜像已经带上这两个依赖。
- AlphaSift、AlphaEvo 是 DSA 文档提到的相关项目，不属于这次快照。
