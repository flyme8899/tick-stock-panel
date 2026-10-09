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

Docker 在现有 compose 里加了**可选**服务，不改变原来的单服务启动：

```bash
# 在 .env 中设置 DSA_BASE_URL=http://dsa:8000
docker compose --profile dsa up -d --build
```

`--profile dsa` 不能省。`dsa` 服务挂在 `dsa` 这个 profile 下，裸 `docker compose up`
只会起 `app`，决策页会断（`docker compose config --services` 只列出 `app`，加上
`--profile dsa` 才同时列出 `app` 和 `dsa`）。

两个服务走 compose 的默认网络（按项目名自动命名，如 `daily-stock-analysis_default`
之类的前缀名），app 才能按服务名 `dsa` 解析到 sidecar。**不需要**手工建网，也**不要**
在 `docker-compose.yml` 里声明固定名字的网络——那会引入一个隐藏前置条件，详见下节。

`.env` 里的 `DSA_BASE_URL` 在 compose 下要填 `http://dsa:8000`（服务名），
不是 `http://127.0.0.1:8000`——容器里的 `127.0.0.1` 指自己。

## 接入独立 DSA 项目

DSA 有自己的仓库和 compose 文件，实际部署时很可能**不**通过本仓库的 `dsa` profile，
而是在别处 `docker compose up` 起 `stock-server` / `analyzer` / alphafeed。这时 TSP 的
`app` 容器不在 DSA 项目的网络里，按容器名解析不到 `stock-server`。

用 override 文件把自己加进去，不要改动主 compose：

```bash
docker compose -f docker-compose.yml -f docker-compose.dsa-external.yml up -d
```

`docker-compose.dsa-external.yml` 把 `app` 接到 DSA 项目的网络上（`external: true`，
声明「连上去」而不创建）。默认网络名是 `daily-stock-analysis_default`，可用 `.env`
里的 `DSA_NETWORK` 覆盖。查实际名字：

```bash
docker network ls --filter label=com.docker.compose.project=daily-stock-analysis --format '{{.Name}}'
```

### 为什么不在主 compose 里写死网络

曾经试过在主 compose 里声明一张固定名字的网络（`name: dsa-net`）让两边共用。这条路
有三个坑，逐个踩过：

1. **`external: true` 会让裸 `up` 失败。** 没先手工建网时，`docker compose up` 直接
   报错退出——哪怕不启 `dsa` profile 也一样，因为 `app` 就挂在网上。
2. **改成本地声明（`name:` + 让 compose 自建）也不行。** 如果机器上已经有一张同名
   网络是手工 `docker network create` 建的（没有 compose 标签），compose 不会接管，
   直接报错退出，容器一个都不创建：
   ```
   network dsa-net was found but has incorrect label com.docker.compose.network set to "" (expected: "dsa-net")
   ```
3. **手工 `docker network connect` 不是长久之计。** 实测：对容器 `stop` + `start`
   （重启）**保留**连接；对 `rm` + `run`（重建）**丢失**连接。重建容器后要重新
   `connect`，否则又断。所以要么让部署脚本每次都带上正确的 `--network`，要么尽快
   上 compose——别指望一次 `connect` 能一直有效。

结论：主 compose 保持「零前置条件、裸 `up` 能跑」；要连外部网络就用 override 文件，
把额外的前置条件隔离在需要它的人那里。

### 顺序：先接网，再谈拆旧网

机器上曾有一张手工建的 `dsa-net`，TSP 的容器和 DSA 的几个容器都连在上面。这张网
**不能顺手删**——它承载着 TSP 的行情数据链路：`data/data_sources/alphafeed.yaml` 里
五类数据集（`daily` / `adj_factor` / `realtime` / `minute` / `full_minute`）全部指向
`dsa-alphafeed-source:3021`。删网断的是**行情源**，比决策页断严重得多。

安全顺序：

```bash
# 1. 先把两边接进 DSA 项目的网络
docker network connect daily-stock-analysis_default tsp
docker network connect daily-stock-analysis_default dsa-alphafeed-source

# 2. 从 tsp 里验证两条都通（期望都是 200）
docker exec tsp curl -fsS -o /dev/null -w '%{http_code}\n' http://stock-server:8000/api/v1/health
docker exec tsp curl -fsS -o /dev/null -w '%{http_code}\n' http://dsa-alphafeed-source:3021/health

# 3. 确认 alphafeed 的部署脚本已改用新网络、或已上 compose 之后，再拆 dsa-net
```

第 3 步之前 `dsa-net` 留着无害。注意上一条的坑：**这些 `connect` 会被容器重建冲掉**，
所以第 3 步之前必须先把「重建后自动接网」这件事落到脚本或 compose 里，否则拆网只是
把问题推迟到下一次重建。

### 回滚

override 起不来时先 `docker compose down`，再按 DSA 自己的 compose 恢复：

```bash
cd /workspace/dsa/docker && docker compose up -d server analyzer
```

`.env` 里的 `DSA_BASE_URL` 改回 `http://stock-server:8000`（走 DSA 项目网内的容器名）。
另外从主 compose 起 `app` 时，容器名是 `TickFlow_Stock_Panel`，如果之前有手工
`docker run` 起的同名实例（`tsp`），先 `docker rm -f tsp` 再起，否则会撞 3018 端口，
并且两个实例同时写 `./data`（bind mount 没有卷缓冲）。

sidecar 默认监听 `127.0.0.1:8000`，数据库放在 `data/dsa/stock_analysis.db`。`ENV_FILE` 指向仓库根目录的 `.env`，所以 TSP 和 DSA 共用一份配置。镜像时区是 `Asia/Shanghai`，本地 `scripts/dsa.sh` / `dsa.ps1` 在未设置 `TZ` 时也使用这个时区。

Docker 服务带健康检查：容器内 `curl -fsS http://127.0.0.1:8000/api/v1/health`。这个接口免登录，HTTP 失败或连不上都会让容器变为 unhealthy。决策页徽标每 30 秒请求 TSP 的 `/api/dsa/status`，悬停可看到同样的探测结果。

## 量化回测证据

`backend/app/custom/dsa/quant_evidence.yaml` 保存 TSP 机械回测对六类 DSA 技能的对照（均线金叉、缩量回踩、放量突破、多头趋势、龙头、底部放量）。窗口是 2026-07-08 至 2026-10-08，全市场 5882 只，同期基准 -4.00%。数字来自当时的回测报告，负收益是无差别执行的基准线，不是禁用令。

`scripts/dsa.sh`、`scripts/dsa.ps1` 和 `docker compose --profile dsa` 会在启动 `main.py` 之前装上钩子，只给同名技能的提示末尾追加一段「量化回测参考」。`wave_theory` 等未收录技能保持原文。本地脚本在变量未设置时使用仓库内文件；`.env` 或环境里写成 `off`，或文件缺失时，不追加，也不影响 sidecar 启动。Compose 把该变量固定成容器内的 `/opt/tsp/quant_evidence.yaml`，避免 `.env` 里的宿主机路径进容器；要在 Docker 里关闭，把这一项改成 `off`。证据文件不放进 `vendor/daily_stock_analysis/strategies/`，那个目录会被当成策略加载。

决策仪表盘读取 `GET /api/dsa/quant-evidence` 展示同一份摘要。直接在 vendor 目录里执行 `python main.py` 不会经过这个钩子。

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
| `DSA_PYTHON` | 跑 ETF 轮动所用的解释器。未设置时用 `vendor/daily_stock_analysis/.venv` |
| `DSA_UPSTREAM_COOKIE` | DSA 打开 `ADMIN_AUTH_ENABLED` 后转发给上游的 Cookie。未配置 `DSA_PASSWORD` 时只使用这一项 |
| `DSA_PASSWORD` | 上游管理密码。配置后由转发层登录并缓存会话，过期时间跟随 Set-Cookie，提前刷新。留空则行为与只配 Cookie 时相同。不要把真实密码写进仓库 |
| `TSP_QUANT_EVIDENCE_FILE` | 机械回测证据 YAML。启动脚本和 compose 默认指向仓库内文件。`off` 关闭注入 |
| `DSA_TIMEOUT_SECONDS` | 转发超时。分析、问股、选股、回测默认更长 |

DSA 读取的密钥和数据源（写在同一个 `.env`，留空则对应能力失败并给出原因）：

`SCHEDULE_ENABLED`、`SCHEDULE_TIME`、`SCHEDULE_RUN_IMMEDIATELY`、`TRADING_DAY_CHECK_ENABLED`、`MARKET_REVIEW_REGION`、`STOCK_LIST`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`AIHUBMIX_KEY`、`ANSPIRE_API_KEYS`、`TUSHARE_TOKEN`、`TICKFLOW_API_KEY`、`SERPAPI_API_KEYS`、`TAVILY_API_KEYS`、`BOCHA_API_KEYS`、`BRAVE_API_KEYS`、`MINIMAX_API_KEYS`、`SEARXNG_BASE_URLS`、`WECHAT_WEBHOOK_URL`、`FEISHU_WEBHOOK_URL`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`DISCORD_WEBHOOK_URL`、`SLACK_BOT_TOKEN`、`SLACK_CHANNEL_ID`、`EMAIL_SENDER`、`EMAIL_PASSWORD`、`ETF_ROTATION_POOL`、`ETF_ROTATION_SAFE_ASSET`。

sidecar 启动时，如果 `OPENAI_API_KEY` 为空且 TSP 已配置 `AI_API_KEY`，会借用 `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`。`TICKFLOW_API_KEY` 两边同名，直接共用。

页面里的「生成研报」默认 `notify=false`，避免试点时把消息打进群。定时任务和「立即跑一轮」仍按 DSA 自己的通知配置发送。

## 功能落在哪里

| DSA 能力 | TSP 位置 |
| --- | --- |
| AI 决策仪表盘 | 决策 → 决策仪表盘 |
| 个股 LLM 报告、分享图 | 决策 → 个股研报；个股预览底部有入口 |
| 实时新闻 / 情报源 | 决策 → 情报 |
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
