# 多源资讯与热门事件

侧栏「热门事件」读取本地资讯库。主列表是当天聚类出的具体事件；近 24 小时相对前 4 日基线升温的板块和个股仍在后面的页签里。市场环境页的主线仍按价格和涨停梯队计算，两套结果不互相覆盖。

采集默认全部关闭。现有部署不会因为升级就开始出网。

## 来源

| 来源 | 谁去拉 | 内容 |
| --- | --- | --- |
| 钉钉作文实时 | 宿主机 `dws`，只读 | 群号来自 `NEWS_DWS_GROUP_ID`，留空则未配置。图片只存 mediaId |
| 知识星球纳指星球调研 | 宿主机 `zsxq-cli`，只读 | 星球号来自 `NEWS_ZSXQ_GROUP_ID`，留空则未配置。话题标签不拼回正文 |
| ima爱分享 | TSP 进程 | 知识库「【爱分享】的财经资讯」。只读标题，不请求正文 |
| 财联社 | TSP 进程 | 电报。保留级别、个股和板块 |
| 华尔街见闻 | TSP 进程 | 全球和 A 股快讯。保留标的、主题和热度 |
| ETF领航者 | TSP 进程 | 每日 ETF 申购赎回。来源键 `etf_flow`。优先网易号，搜狗微信作备份 |
| CNBC | TSP 进程 | 头条、市场 RSS。只存标题、摘要、链接和时间 |
| MarketWatch | TSP 进程 | 头条 RSS。只存标题、摘要、链接和时间 |
| 华尔街日报市场 | TSP 进程 | 市场 RSS。只存标题和摘要，不抓付费正文 |
| 彭博 | TSP 进程 | 市场、科技 RSS。只存标题和摘要，不抓付费正文 |
| 南华早报 | TSP 进程 | 商业、中国经济 RSS。只存标题、摘要和链接，不抓付费正文 |
| 路透 | TSP 进程 | 商业、市场、国际 sitemap。只存标题和链接。主源失败时用 Google News |
| Reddit | TSP 进程 | wallstreetbets、stocks、investing 的新帖 Atom。只存标题、摘要、链接、作者和时间。每次只请求一个子版 |
| SEC 8-K | TSP 进程 | 最新 8-K Atom。只存标题、摘要和申报链接 |

钉钉和知识星球的登录态在宿主机，不在容器里。宿主机脚本把 JSON 写到 `data/news/inbox/`，TSP 再入库。不开放无鉴权的 HTTP 写入接口。

财联社、华尔街见闻、ima 由面板进程里的单线程轮询。交易日 09:15–15:05 电报和快讯约 45 秒一次，钉钉和知识星球约 5 分钟一次；白天其余时间更疏，ima 约 6 小时一次。ETF领航者不走这套间隔，见下文。

## ETF领航者

公众号「ETF领航者」每个交易日收盘后，次日早晨约 07:30（北京时间）发一篇「X月X日ETF基金申购和赎回」。标题里的日期是交易日，发布时间是下一个自然日，周五的稿在周六早上。正文主要是表格图片。

主源是同一作者的网易号列表：<https://www.163.com/dy/media/T1730214999977.html>。列表里没有当天这篇，或列表请求失败时，才用搜狗微信搜索公众号「ETF领航者」，再打开 `mp.weixin.qq.com` 文章。不登录微信。搜狗一天最多 4 次，两次至少隔 20 分钟。

表格交给视觉模型，走 OpenAI 兼容的 `chat/completions`，图片放在 `image_url`。tokenhub 拉不到网易图片代理 `nimg.ws.126.net`（没有扩展名，直接给远程地址会 400），所以先在允许的图片域名里把图下载下来，核对 Content-Type 是 jpeg、png、webp、gif 或 bmp，并且不超过约 5MB，再写成 `data:image/...;base64,...`。网易图的 Referer 是 `https://www.163.com/`，微信图是 `https://mp.weixin.qq.com/`。下载失败则这篇表格抽取失败。入库的 `media_ids` 仍是原来的图片地址。只用下面三个变量，不读取文本模型的 `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`，也不走 `NEWS_LLM_EXTRACT`。默认模型 `deepseek/deepseek-v4-flash-vision-exp` 会读图片，单张表大约 16 秒。`glm-5.3-flash` 大约 23 秒，`mimo-v2.6-flash` 也能读图，但表格识别并不更好，所以这两个只作备选，不改默认。

申赎表格保持思考，`max_tokens` 为 8192。低于这个值时预算被思考用完，`content` 为空。deepseek 视觉模型可以用 `thinking.type=disabled` 或 `reasoning_effort=none` 关掉思考，但表格识别会变差，所以这条链路不关。非表格的短视觉请求才关：deepseek 和 mimo 传 `thinking.type=disabled`；glm 传这个字段会 400，只传 `reasoning_effort=low`。每张图单独请求。空内容时同一张图再试一次；若 `finish_reason` 是 `length` 且正文仍为空，这一次把 `max_tokens` 提高到 16384，思考仍然打开。第一次已经抽出数字时，再请求一次核对正负号。同一格两次正负号相反就留空，不猜哪一次对。代码只保留恰好 6 位的数字。

```ini
VISION_AI_API_KEY=
VISION_AI_BASE_URL=https://tokenhub.tencentmaas.com/v1
VISION_AI_MODEL=deepseek/deepseek-v4-flash-vision-exp
```

没填 `VISION_AI_API_KEY` 时来源保持未配置，页面上不能打开。数字按图中印刷保存，单位写在 `unit`（常见是亿元），净申购为正、净赎回为负，程序不再做单位换算。全市场、宽基、分类和单只 ETF 的当日 / 5 日 / 20 日净申购放在 `raw_json`。图片地址记在 `media_ids`。同一篇文章用网易 docid 或微信 `sn` 去重。

热门事件只收当日净申购绝对值最大的 8 只有代码 ETF，以及最多 6 个宽基或分类名称。DSA 情报源名称是「ETF领航者」，feed 的 `source=etf_flow`。

轮询在面板进程里，不需要宿主机 systemd。北京时间 07:35–09:00：

- 昨天开市（含周五之后的周六）时，每 15 分钟看一次，直到当天发布的申赎稿入库。09:00 仍没有稿，或稿已出现但表格仍抽不出来，健康检查记失败，并按已配置的 `DINGTALK_WEBHOOK_URL` 提醒一次。提醒正文不含表格数字。
- 今天开市但昨天休市（例如周一）时，补抓列表里还没入库的最新一篇。不因为「今天没有新标题」报警，补抓时表格抽取失败也只记健康检查，不发当日缺稿提醒。
- 搜狗结果若只有「月日」没有年份，且按今年会落到今天之后，则记到上一年，避免把发布时间写成未来。
- 昨天和今天都休市（例如周日）不请求。
- 富尧交易日历没接上时，周一到周五视作开市，节假日可能误报一次。

进程如果在 09:00 之后才起来，当天补看一次，然后等到下一个 07:35。

CNBC、MarketWatch、华尔街日报市场、彭博约 5 分钟一次，南华早报和路透约 15 分钟一次，SEC 8-K 约 3 分钟一次。Reddit 每次只请求一个子版，两次请求至少隔 75 秒。这几档固定间隔，不按 A 股交易时段加快或放慢。请求带上次响应的 `ETag` / `Last-Modified`，返回 304 时不再解析。采集只请求 feed 地址，不打开条目链接，也不保存 `content:encoded` 或 Atom `content`。Reddit 是例外，见下文。

南华早报的地址必须带结尾斜杠：<https://www.scmp.com/rss/92/feed/>（商业）和 <https://www.scmp.com/rss/318421/feed/>（中国经济）。没有斜杠会 301。摘要用 RSS 的 `description`，付费正文不入库。

路透官网对普通抓取返回 401，所以不打开文章。主源先请求 <https://www.reuters.com/arc/outboundfeeds/sitemap-index/?outputType=xml>，再取最新一页 news sitemap。`from` 缺省或最小的那页是最新；索引里若给出的是普通 sitemap，改读同一偏移的 `news-sitemap`，因为标题在那里。只保留路径第一节是 `business`、`markets`、`world` 的条目，字段是标题、链接和 `news:publication_date`。索引或这一页失败时，才改拉 Google News RSS <https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en>，去掉标题末尾的 ` - Reuters`，只存标题和链接。这条 RSS 的 `<source url>` 只是路透首页，条目链接是 Google News 地址，不再逐条打开。主源返回 304 时不改走备用源。最新 news sitemap 还没成功之前，不记录索引的 `ETag`，下一轮仍会重试主源。

Reddit 的 JSON 接口会 403，只请求 Atom：`https://www.reddit.com/r/{子版}/new/.rss`。默认子版是 wallstreetbets、stocks、investing，可用 `NEWS_REDDIT_SUBREDDITS` 改成逗号分隔的列表，可以带 `r/` 前缀，非法名字丢掉。列表写了但一个合法名字都没有时，这一轮不请求。每次轮询只拉一个子版，按顺序轮转；请求成功或返回 304 才轮到下一个，失败仍重试同一个。匿名频率大约每分钟 1 次，所以两次 Reddit 请求至少隔 75 秒。这个间隔记在本地，进程重启后也不会立刻再打。返回 429 时读取 `Retry-After`（秒数或 HTTP 日期），等待时间不小于 75 秒；没有这个头则从 75 秒起倍增，上限 3600 秒。成功或 304 之后回到 75 秒。User-Agent 为 `tsp-news/1.0 (contact astock888888@mail.grokbot.com)`。保存标题、摘要、评论链接、作者和时间。摘要来自 Atom `content`：去掉 HTML、`submitted by` 页脚、`[link]` / `[comments]` 和 redd.it 图片地址。图片帖没有正文时摘要用标题。不打开评论链接，不保存缩略图。`REDDIT_CLIENT_ID` 和 `REDDIT_CLIENT_SECRET` 可以先写上，供以后 OAuth 使用。当前采集不换 token，请求里也不带 Authorization。

财联社电报按 `last_time` 往更早翻，直到这一页里出现已经入库的 id，最多 10 页。华尔街见闻两个频道各自用 `next_cursor` 做 `cursor` 翻页，同样见到已入库 id 就停，每个频道最多 10 页。重启或中间停过之后，下一轮用同一套规则补上缺口。

## 开关

每个来源独立。页面上的开关写到 `data/user_data/preferences.json` 的 `news_sources`。环境变量优先，设了之后页面不能改：

```ini
NEWS_DWS_ENABLED=false
NEWS_ZSXQ_ENABLED=false
NEWS_IMA_ENABLED=false
NEWS_CLS_ENABLED=false
NEWS_WSCN_ENABLED=false
NEWS_ETF_FLOW_ENABLED=false
NEWS_CNBC_ENABLED=false
NEWS_MARKETWATCH_ENABLED=false
NEWS_WSJ_ENABLED=false
NEWS_BLOOMBERG_ENABLED=false
NEWS_SCMP_ENABLED=false
NEWS_REUTERS_ENABLED=false
NEWS_REDDIT_ENABLED=false
NEWS_SEC_ENABLED=false
```

ima 还要 `IMA_CLIENT_ID` 和 `IMA_API_KEY`，缺一则保持关闭。可选 `IMA_KB_ID`；留空时按知识库名称查找。

SEC 8-K 还要 `SEC_USER_AGENT`，字符串里必须有联系邮箱，例如 `TSP-News ops@example.com`。没有邮箱时来源保持未配置，不会请求 sec.gov。这一批外文源没有 API key。

钉钉群号和知识星球号留空表示未配置，对应来源无法开启：

```ini
NEWS_DWS_GROUP_ID=
NEWS_ZSXQ_GROUP_ID=
```

登录失效时，若配置了 `DINGTALK_WEBHOOK_URL`（可选 `DINGTALK_SECRET`），6 小时内对同一来源只发一条提醒，正文不含资讯内容。

可选 `NEWS_LLM_EXTRACT=true` 时，词典抽不到股票或板块才会调用现有 AI 客户端，每小时最多 10 次，并且只接受词典里已有的名称或代码。外文来源（含南华早报、路透和 Reddit）用同一次调用补一句中文摘要，模型只看到标题和 feed 摘要。路透没有摘要时，送进去的就是标题。Reddit 送进去的是去掉 HTML 和页脚后的摘要，图片地址不在里面。同一次轮询最多处理 10 条新资讯，其余只存标题和摘要。默认关闭。这是文本模型，和 ETF 申赎的 `VISION_AI_*` 不是同一套。

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

示例 systemd 单元在 `deploy/tsp-news-collector.service` 和 `.timer`，默认每 5 分钟跑一次。单元以用户 `ubuntu` 运行，`HOME=/home/ubuntu`，工作目录是 `/home/ubuntu/tick-stock-panel`。`TimeoutStartSec=3600` 留给知识星球长回补。`ExecStart` 用 `~/.venvs/tsp-collector` 里的 Python。`PATH` 带上 `~/.local/bin`，才能找到 `dws` 和 `zsxq-cli`。

这条定时器不是容器调度的重复。容器里的轮询对钉钉和知识星球只调用 `collect_inbox()`，读取 `data/news/inbox`。容器内没有 `dws` / `zsxq-cli`，登录态在宿主机的 `~/.dws` 等目录。停掉 `tsp-news-collector.timer` 之后，这两路资讯不再入库。

采集脚本启动时检查 Python 版本，低于 3.10 会直接退出。面板和容器仍用 Python 3.11。推荐把 `~/.venvs/tsp-collector` 建在 3.11 上，不要长期留在系统自带的 3.10。虚拟环境里至少要有 `pydantic` 和 `pydantic-settings`，因为脚本会导入 `app.config`。

Ubuntu 可以用 deadsnakes：

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv
rm -rf ~/.venvs/tsp-collector
python3.11 -m venv ~/.venvs/tsp-collector
~/.venvs/tsp-collector/bin/pip install pydantic pydantic-settings
```

或者用 uv：

```bash
uv python install 3.11
rm -rf ~/.venvs/tsp-collector
uv venv --python 3.11 ~/.venvs/tsp-collector
uv pip install --python ~/.venvs/tsp-collector/bin/python pydantic pydantic-settings
```

`ProtectSystem=strict` 下，`data/news` 必须属于该服务用户，否则收件箱写不进去。dws 大约每 2 小时刷新一次令牌；`ProtectHome` 只读时还要放开 `~/.dws`、`~/.local/share/dws-cli`、`~/.config/zsxq-cli`、`~/.local/share/zsxq-cli`。仓库不在这个路径时，改 `WorkingDirectory`、`Environment=DATA_DIR`、`EnvironmentFile`、`ExecStart` 和 `ReadWritePaths`。

容器默认不再以 root 运行。已经用 root 写过数据目录的机器，切换前执行一次 `sudo chown -R 1000:1000 data`，让容器用户和上面的 `ubuntu` 用户是同一个 uid。步骤和回滚见 [deployment.md](./deployment.md)。

改完属主或重建虚拟环境后跑一次预检。`data/news` 不可写、采集器 Python 低于 3.10，或 `data/` 里有不属于 `APP_UID`/`APP_GID` 的文件时，退出码不是 0：

```bash
python3 scripts/deploy_preflight.py --data-dir ./data
```

钉钉游标比本次完成时间早 2 分钟，避免拉取过程中新到的消息被跳过。同一条 `messageId` 不会重复入库。

板块名如果是纯数字、汉字不足两个，或不足 4 位的纯字母数字（如 `50`、`A50`、`中`），不参与正文匹配，并且命中两端不能再贴着数字。已经入库的这类板块提及会在打开库时删掉，热门板块读取时也会跳过，所以部署后 `50` 会从热门板块消失。ETF 维表不进入个股候选。名称里带 ETF、LOF 或「基金」的标的也不做简称匹配，避免「中证1000」把「中证1000ETF南方」送上个股榜。

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
- 为每个采集源（含 ETF领航者、外文 RSS、南华早报、路透和 Reddit）和「TSP热门候选」各建一个情报源。外文源同样只传标题和摘要。路透没有摘要时，摘要位置是标题。Reddit 的摘要是去掉 HTML 和页脚后的正文，作者留在资讯库里。
- 每条资讯写成市场范围一行，再按股票（最多 8 个，规范代码如 `600519.SH`）和板块（最多 6 个）各写一行，个股分析才能按标签命中。
- 情报源关闭时拒绝拉取，不把状态记成失败。写入后按 DSA 的 `news_intel_retention_days` 删过期行，返回值带最多 5 条 `sample_items`。
- 大盘复盘合并本地情报时，把最多 4 条热门候选插到前面，避免电报占满 6 条窗口。
- 不依赖 `NEWS_INTEL_AUTO_FETCH_ENABLED`，因此不会顺带打开 NewsNow 公共源。

Docker：`docker compose --profile dsa` 把桥文件只读挂到 `/opt/tsp/news_bridge.py`，并设置 `TSP_NEWS_BASE_URL=http://app:3018`。本地 `scripts/dsa.sh` 默认访问 `http://127.0.0.1:3018`。

`NEWS_DSA_FEED_TOKEN` 留空时桥直接返回，DSA 照常启动。TSP 未开、网络失败或当前不是 DSA 进程，都只记日志。面板采集不依赖 DSA 是否在跑。

## 钉钉推送

热门事件页可以把结果发到已配置的自定义机器人（`DINGTALK_WEBHOOK_URL`，可选 `DINGTALK_SECRET` 加签）。机器人所在的群由钉钉侧决定。默认关闭：`NEWS_PUSH_ENABLED` 和下面每一类都要打开。环境变量优先于页面上的开关。

页面上的「发送测试消息」只有点击后才发，正文标成【测试】，不含资讯正文。

| 类型 | 环境变量 | 什么时候发 |
| --- | --- | --- |
| 热点候选 | `NEWS_PUSH_HOT_ENABLED` | 交易日 `NEWS_PUSH_PREMARKET`（默认 08:45）和 `NEWS_PUSH_POSTCLOSE`（默认 15:40）各一条。候选新进入前 N 且提及数达到 `NEWS_PUSH_MIN_STORIES`，或分数相对上次升高达到 `NEWS_PUSH_SCORE_JUMP`，再补一条，默认 30 分钟内不重复 |
| 异动监控 | `NEWS_PUSH_ABNORMAL_ENABLED` | 交易日 09:25–11:30、13:00–15:05。只看自选。用实时报价对照昨收和盘前 60 日收盘极值，算涨停、炸板、跌停、翘板、60 日新高、新低。同一标的同一信号默认 30 分钟内不重复 |
| 做T提醒 | `NEWS_PUSH_T_ENABLED` | 交易日 09:35–11:25、13:05–14:55。默认只看自选。偏离当日累计均价 ±1.5%，或贴近日内高低 0.3%，或从 1% 以外回到昨收。均价和回到昨收默认 60 分钟内不重复；贴近高低另有 `NEWS_PUSH_T_RANGE_COOLDOWN_MIN`，默认也是 60 分钟。10:00 之前不推贴近高低 |

交易日优先用交易日历；日历不可用时按周一到周五。三类消息标题分别是【热点候选】【异动监控】【做T提醒】。登录失效仍是单独的短文本，不会套进这些标题。

热点消息先写具体事件（标题、细分概念、提及数、来源个数、首见时间），再写板块和个股升温榜的提及数、来源个数、相对基线。每条最多 3 个出处。财联社、华尔街见闻可以带链接。钉钉、知识星球、ima 只写来源名，不写标题、链接和正文。

异动和做 T 用实时报价。行情服务 3 分钟内刚拉过、并且缓存日期是今天时，直接用那份最新价、日内高低和累计成交，不再打 TickFlow。否则按 `quote.batch` 的批量上限补拉自选，Expert 档大约 300 次/分钟，80 只自选一轮通常是 1 次请求。没有新鲜报价就不发，避免把盘后表里的旧信号推出去。

异动默认最多 80 只自选；做 T 默认最多 40 只自选。可选 `NEWS_PUSH_ABNORMAL_INCLUDE_HOT` 把热门个股并进异动，`NEWS_PUSH_T_INCLUDE_POSITIONS` 把模拟持仓并进做 T。涨跌停、炸板、翘板用原始价对照昨收。60 日新高新低用前复权最新价对照盘前已经算好的前 59 个交易日收盘极值，和指标流水线同一口径。机器人每分钟最多发 20 条，超出的本轮丢掉、下一轮再试。做 T 另有全自选当天条数上限 `NEWS_PUSH_T_DAILY_CAP`，默认 20，设成 0 则不限制。

`NEWS_PUSH_T_RANGE_ONCE_PER_DAY=true` 时，接近日内高点和接近日内低点合计起来每只股票每天最多一条。默认关闭，这两条只受各自的冷却约束。

20 只活跃股、20 个交易日的分钟回放里，贴近高低在 30 分钟冷却下仍有大约每天每只 2–3.5 次，而且把区间从 0.3% 放到 0.5% 几乎不变。默认是均价 ±1.5%、区间 0.3%、冷却 60 分钟，并在开盘后 30 分钟内跳过贴近高低。贴近高低的冷却可以单独再调。仓库根目录仍可以回放别的阈值：

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/replay_push_rules.py --symbols 600519.SH,000001.SZ --days 5
```

分钟分区默认是 `data/kline_minute/date=*/part.parquet`。输出每种规则、每个阈值、每只股票、每个交易日的触发次数。

放量异动、以及「上穿分时均价」那种需要分钟 K 的边沿，默认不推。
