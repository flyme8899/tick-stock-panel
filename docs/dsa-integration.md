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

sidecar 默认监听 `127.0.0.1:8000`，数据库放在 `data/dsa/stock_analysis.db`。`ENV_FILE` 指向仓库根目录的 `.env`，所以 TSP 和 DSA 共用一份配置。

## 环境变量

集成自身：

| 变量 | 作用 |
| --- | --- |
| `DSA_BASE_URL` | 上游地址。默认 `http://127.0.0.1:8000`。设为空字符串则页面保持未启用 |
| `DSA_AUTOSTART` | `1` 时 `dev.sh` / `dev.ps1` 拉起 sidecar |
| `DSA_PORT` | sidecar 端口，默认 8000 |
| `DSA_PYTHON` | 跑 ETF 轮动所用的解释器。未设置时用 `vendor/daily_stock_analysis/.venv` |
| `DSA_UPSTREAM_COOKIE` | DSA 打开 `ADMIN_AUTH_ENABLED` 后转发给上游的 Cookie |
| `DSA_TIMEOUT_SECONDS` | 转发超时。分析、问股、选股、回测默认更长 |

DSA 读取的密钥和数据源（写在同一个 `.env`，留空则对应能力失败并给出原因）：

`STOCK_LIST`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`AIHUBMIX_KEY`、`ANSPIRE_API_KEYS`、`TUSHARE_TOKEN`、`TICKFLOW_API_KEY`、`SERPAPI_API_KEYS`、`TAVILY_API_KEYS`、`BOCHA_API_KEYS`、`BRAVE_API_KEYS`、`MINIMAX_API_KEYS`、`SEARXNG_BASE_URLS`、`WECHAT_WEBHOOK_URL`、`FEISHU_WEBHOOK_URL`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`DISCORD_WEBHOOK_URL`、`SLACK_BOT_TOKEN`、`SLACK_CHANNEL_ID`、`EMAIL_SENDER`、`EMAIL_PASSWORD`、`ETF_ROTATION_POOL`、`ETF_ROTATION_SAFE_ASSET`。

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
- GitHub Actions 定时工作流没有搬进来。本地和 Docker 用 sidecar 的 `--serve-only` 加 DSA 自己的调度。
- 决策信号在 `ADMIN_AUTH_ENABLED=true` 时还要上游登录态，通过 `DSA_UPSTREAM_COOKIE` 转发。
- 分享图依赖 sidecar 镜像里的 `wkhtmltopdf`。只装了 Python 虚拟环境、没装该工具时，接口会返回失败原因。
- AlphaSift、AlphaEvo 是 DSA 文档提到的相关项目，不属于这次快照。
