"""Static map of daily_stock_analysis capabilities onto the TSP decision workspace.

The sidecar remains the implementation. This catalog is what the TSP UI and
docs agree on, including overlaps that stay on the existing TSP pages.
"""
from __future__ import annotations

UPSTREAM_COMMIT = "ce364e457aab288863a5707e7b3df79786ad07f2"
UPSTREAM_REPO = "https://github.com/ZhuLinsen/daily_stock_analysis"

MARKETS = (
    {"id": "cn", "label": "A 股", "examples": "600519、000858、300750"},
    {"id": "hk", "label": "港股", "examples": "hk00700、hk09988"},
    {"id": "us", "label": "美股", "examples": "AAPL、NVDA、TSLA"},
    {"id": "jp", "label": "日股", "examples": "7203.T、6758.T"},
    {"id": "kr", "label": "韩股", "examples": "005930.KS、035720.KQ"},
    {"id": "tw", "label": "台股", "examples": "2330.TW"},
    {"id": "etf", "label": "ETF", "examples": "510300、159915"},
)

FEATURES = (
    {
        "id": "decision_dashboard",
        "label": "决策仪表盘",
        "section": "dashboard",
        "summary": "自选股 AI 结论、评分、趋势和操作建议汇总。",
        "upstream": "GET /api/v1/history",
        "overlap": "TSP 看板继续负责行情与市场总览。这里只汇总 DSA 的决策报告，不替换看板。",
    },
    {
        "id": "stock_reports",
        "label": "个股研报",
        "section": "reports",
        "summary": "单标的 LLM 决策报告、Markdown 全文和分享图。",
        "upstream": "POST /api/v1/analysis/analyze",
        "overlap": "TSP 个股分析保留关键价位和四维流式分析。DSA 报告从个股预览底部进入，不另开一套 K 线页。",
    },
    {
        "id": "intelligence",
        "label": "情报",
        "section": "intelligence",
        "summary": "新闻与情报源拉取、模板和已入库条目。",
        "upstream": "GET /api/v1/intelligence/items",
        "overlap": "TSP 没有独立情报流。本页承接 DSA 的情报源，不并入异动监控。",
    },
    {
        "id": "markets",
        "label": "多市场",
        "section": "markets",
        "summary": "A 股、港股、美股、日股、韩股、台股和 ETF 的代码口径与数据能力。",
        "upstream": "GET /api/v1/data/capabilities",
        "overlap": "TSP 的行情、选股和回测仍以 A 股 / ETF 本地库为主。跨市场分析走 DSA 数据源。",
    },
    {
        "id": "screening",
        "label": "多市场选股",
        "section": "screening",
        "summary": "DSA 规则选股、热点和历史运行。",
        "upstream": "POST /api/v1/screening/screen",
        "overlap": "TSP 策略页仍是全 A 向量选股。DSA 选股是另一套规则引擎，放在决策工作台里。",
    },
    {
        "id": "etf_rotation",
        "label": "ETF 轮动",
        "section": "etf",
        "summary": "规则化 ETF 双动量轮动信号与回测，不调用大模型。",
        "upstream": "python main.py --etf-rotation",
        "overlap": "TSP 板块轮动看概念和行业。ETF 双动量是 DSA 的独立规则，不并入板块页。",
    },
    {
        "id": "chat",
        "label": "问股",
        "section": "chat",
        "summary": "Agent 多轮问股，可指定内置策略技能。",
        "upstream": "POST /api/v1/agent/chat",
        "overlap": "TSP AI 助手继续操作面板里的因子、策略和回测。问股只对话 DSA Agent。",
    },
    {
        "id": "bot",
        "label": "机器人",
        "section": "bot",
        "summary": "与飞书、钉钉、Telegram、Discord、Slack 相同的斜杠命令。",
        "upstream": "bot/commands",
        "overlap": "平台长连接仍由 DSA 进程持有。本页是同一套命令的面板入口。",
    },
    {
        "id": "schedule",
        "label": "定时推送",
        "section": "schedule",
        "summary": "Asia/Shanghai 的交易日定时分析。结果进决策仪表盘，并走 DSA 自己的通知渠道。",
        "upstream": "GET /api/v1/system/scheduler/status",
        "overlap": "TSP 设置里的飞书、企微、邮件继续服务监控和复盘。DSA 定时报告使用自己的渠道配置。",
    },
    {
        "id": "alerts",
        "label": "决策预警",
        "section": "alerts",
        "summary": "由分析结论生成的价格与决策规则、触发记录。",
        "upstream": "GET /api/v1/alerts/rules",
        "overlap": "监控中心继续评估 TSP 行情规则。决策预警不写入那套规则引擎。",
    },
    {
        "id": "signals",
        "label": "决策信号",
        "section": "signals",
        "summary": "历史建议的结果复盘、反馈和再评估。",
        "upstream": "GET /api/v1/decision-signals/outcomes/stats",
        "overlap": "TSP 信号库是策略条件。这里追踪的是 AI 决策事后表现。",
    },
    {
        "id": "portfolio",
        "label": "持仓风险",
        "section": "portfolio",
        "summary": "账户、成交、快照和组合风险暴露。",
        "upstream": "GET /api/v1/portfolio/risk",
        "overlap": "持仓提醒和模拟盘保持不变。DSA 组合账本是另一套风险视图。",
    },
    {
        "id": "decision_backtest",
        "label": "决策回测",
        "section": "backtest",
        "summary": "用后续行情评估历史 AI 建议，而不是重跑 TSP 策略。",
        "upstream": "POST /api/v1/backtest/run",
        "overlap": "TSP 回测页继续做 T+1 策略回测。本页只评估已经生成的决策记录。",
    },
    {
        "id": "share_image",
        "label": "分享图",
        "section": "reports",
        "summary": "把单份报告渲染成 PNG，供通知渠道或手动保存。",
        "upstream": "GET /api/v1/history/{id}/share-image",
        "overlap": "附在个股研报上，不单独占菜单。",
    },
    {
        "id": "import_codes",
        "label": "智能导入",
        "section": "import",
        "summary": "图片、CSV、Excel 和剪贴板识别股票代码。",
        "upstream": "POST /api/v1/stocks/parse-import",
        "overlap": "TSP 自选导入保持原入口。这里把识别出的代码送去 DSA 分析。",
    },
    {
        "id": "usage",
        "label": "用量与配置",
        "section": "settings",
        "summary": "模型用量、调度配置和本集成需要的环境变量。",
        "upstream": "GET /api/v1/usage",
        "overlap": "密钥仍写在仓库根目录 .env，不在页面里回显。",
    },
)

BOT_COMMANDS = (
    {"name": "help", "usage": "/help [命令]", "summary": "列出命令", "aliases": ["h", "帮助", "?"]},
    {"name": "analyze", "usage": "/analyze <代码> [full]", "summary": "分析股票或指数", "aliases": ["a", "分析", "查"]},
    {"name": "market", "usage": "/market", "summary": "大盘复盘", "aliases": ["m", "大盘", "复盘", "行情"]},
    {"name": "batch", "usage": "/batch [代码...]", "summary": "批量分析", "aliases": ["b", "批量", "全部"]},
    {"name": "ask", "usage": "/ask <代码> [技能]", "summary": "按技能问股", "aliases": ["问股"]},
    {"name": "chat", "usage": "/chat <问题>", "summary": "自由对话", "aliases": ["c", "问"]},
    {"name": "research", "usage": "/research <代码或主题> [问题]", "summary": "深度研究", "aliases": ["深研"]},
    {"name": "strategies", "usage": "/strategies", "summary": "列出策略技能", "aliases": ["skills", "策略"]},
    {"name": "history", "usage": "/history", "summary": "最近问股会话", "aliases": ["历史", "会话"]},
    {"name": "status", "usage": "/status", "summary": "服务与调度状态", "aliases": ["s", "状态"]},
)

ENV_VARS = (
    {"name": "DSA_BASE_URL", "required": False, "purpose": "DSA 服务地址。留空则决策页只显示未连接。默认 http://127.0.0.1:8000"},
    {"name": "DSA_AUTOSTART", "required": False, "purpose": "设为 1 时，./dev.sh 会一并拉起 sidecar"},
    {"name": "DSA_PORT", "required": False, "purpose": "sidecar 监听端口，默认 8000"},
    {"name": "DSA_PYTHON", "required": False, "purpose": "运行 ETF 轮动的解释器。未设置时使用 vendor 目录里的 .venv"},
    {"name": "DSA_UPSTREAM_COOKIE", "required": False, "purpose": "DSA 打开管理登录后，转发给上游的 Cookie。未配置 DSA_PASSWORD 时只使用这一项"},
    {"name": "DSA_PASSWORD", "required": False, "purpose": "上游管理密码。配置后自动登录并按 Set-Cookie 续期；留空则与只配 Cookie 时相同。不要写入仓库"},
    {"name": "TSP_QUANT_EVIDENCE_FILE", "required": False, "purpose": "机械回测证据 YAML。scripts/dsa.sh 与 Docker 默认指向随仓库分发的文件，并追加到同名 DSA 技能提示。设为 off 则关闭"},
    {"name": "STOCK_LIST", "required": False, "purpose": "定时分析的自选代码，如 600519,000858。页面保存会写回 .env"},
    {"name": "SCHEDULE_ENABLED", "required": False, "purpose": "true 时 sidecar 按 SCHEDULE_TIME 恢复每日任务，启动时不立刻分析"},
    {"name": "SCHEDULE_TIME", "required": False, "purpose": "每日时刻，24 小时制 HH:MM。容器时区是 Asia/Shanghai"},
    {"name": "TRADING_DAY_CHECK_ENABLED", "required": False, "purpose": "true 时非交易日跳过。A 股部署保持 true"},
    {"name": "MARKET_REVIEW_REGION", "required": False, "purpose": "大盘复盘市场。自托管默认 cn"},
    {"name": "OPENAI_API_KEY", "required": False, "purpose": "OpenAI 兼容模型密钥。sidecar 在该项为空时会借用 TSP 的 AI_API_KEY"},
    {"name": "OPENAI_BASE_URL", "required": False, "purpose": "OpenAI 兼容接口地址"},
    {"name": "OPENAI_MODEL", "required": False, "purpose": "模型名"},
    {"name": "GEMINI_API_KEY", "required": False, "purpose": "Gemini 密钥"},
    {"name": "ANTHROPIC_API_KEY", "required": False, "purpose": "Claude 密钥"},
    {"name": "AIHUBMIX_KEY", "required": False, "purpose": "AIHubMix 密钥"},
    {"name": "ANSPIRE_API_KEYS", "required": False, "purpose": "Anspire 模型与搜索密钥"},
    {"name": "TUSHARE_TOKEN", "required": False, "purpose": "Tushare Pro，提升 A 股历史行情稳定性"},
    {"name": "TICKFLOW_API_KEY", "required": False, "purpose": "与 TSP 共用的 TickFlow 密钥，DSA 将其作为行情源之一"},
    {"name": "SERPAPI_API_KEYS", "required": False, "purpose": "新闻搜索"},
    {"name": "TAVILY_API_KEYS", "required": False, "purpose": "新闻搜索"},
    {"name": "BOCHA_API_KEYS", "required": False, "purpose": "中文新闻搜索"},
    {"name": "BRAVE_API_KEYS", "required": False, "purpose": "新闻搜索"},
    {"name": "MINIMAX_API_KEYS", "required": False, "purpose": "结构化搜索"},
    {"name": "SEARXNG_BASE_URLS", "required": False, "purpose": "自建 SearXNG"},
    {"name": "WECHAT_WEBHOOK_URL", "required": False, "purpose": "企业微信机器人"},
    {"name": "FEISHU_WEBHOOK_URL", "required": False, "purpose": "飞书机器人。与 TSP 设置页里的监控渠道相互独立"},
    {"name": "TELEGRAM_BOT_TOKEN", "required": False, "purpose": "Telegram，需同时配置 TELEGRAM_CHAT_ID"},
    {"name": "DISCORD_WEBHOOK_URL", "required": False, "purpose": "Discord Webhook"},
    {"name": "SLACK_BOT_TOKEN", "required": False, "purpose": "Slack，需同时配置 SLACK_CHANNEL_ID"},
    {"name": "EMAIL_SENDER", "required": False, "purpose": "邮件推送，需同时配置 EMAIL_PASSWORD"},
    {"name": "ETF_ROTATION_POOL", "required": False, "purpose": "ETF 轮动池，逗号分隔"},
    {"name": "ETF_ROTATION_SAFE_ASSET", "required": False, "purpose": "避险资产代码，如 511880"},
    {"name": "ADMIN_AUTH_ENABLED", "required": False, "purpose": "DSA 管理登录。本地 sidecar 建议保持关闭"},
)


def catalog_payload() -> dict:
    return {
        "upstream_repo": UPSTREAM_REPO,
        "upstream_commit": UPSTREAM_COMMIT,
        "license": "MIT",
        "markets": list(MARKETS),
        "features": list(FEATURES),
        "bot_commands": list(BOT_COMMANDS),
        "env_vars": list(ENV_VARS),
    }
