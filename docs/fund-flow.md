# A 股资金进出

本地落盘的资金数据。选股因子、热门事件和决策工作台的大盘复盘读的是这里的 parquet，不在页面请求里打外部接口。

代码默认关闭。环境变量没有设置时，后台线程会起来，但不会出网。`.env.example` 里为本 HK 部署写了打开值，复制到 `.env` 后才按下面的时刻拉取。

## 不使用的接口

HK 服务器上这些入口不可用，调用表里没有它们：

- `ak.stock_individual_fund_flow`（逐只，仍走东财 push2his 的代码搜索）
- `ak.stock_main_fund_flow`
- 东财 push2 板块资金流（含 `ak.stock_sector_fund_flow_rank`）

个股日频账单用显式 secid 直接请求东财 push2his，不再经 efinance 搜索代码。全市场当日 / 5 日排名才用 akshare：同花顺 `stock_fund_flow_individual` 优先，东财 `stock_individual_fund_flow_rank` 作备选。同花顺个股榜缺深市主板和北交所时，用东财数据中心 ``RPT_DMSK_TS_FUNDFLOW`` 分页补一次当日主力、大单和超大单，走统一限速和超时。深市主板按交易日拉全市场后只留 000/001/002/003。北交所同花顺榜没有，按 430、8 字头和 920 三段代码过滤后再翻页，不把沪深再拉一遍。这套接口和龙虎榜、两融、PE 相同。行情列表 ``push2`` 在部分云服务器上返回 502，这里不再用它补板块，也不再对这两个板块逐只打历史账单。数据中心这三个净额字段的单位是万元，落盘前换成元。日志里的「整段缺板块」按缺失只数占该板块应有只数达到 80% 才写；只缺几只科创板不会报整段缺失。板块用同花顺 `stock_fund_flow_industry` / `stock_fund_flow_concept`。东财 push2 的板块资金流接口仍然不调用。

## 来源和时刻

时间都是 Asia/Shanghai。到点任务只在交易日跑。交易日探针不确定时，工作日仍会跑。开关在非交易日、或交易日收盘前提前打开时，会补最近一个还缺分区的交易日；30 分钟内不重复尝试。盘中板块快照和个股即时快照不在这批补数里。

| 数据 | 源 | 时刻 |
| --- | --- | --- |
| 个股主力 / 大单 / 超大单 | 东财 push2his 日 K，secid 由代码推导（沪市 `1.`，深市和北交所 `0.`）。已经请求过但没拿到的代码用妙想 `MX_APIKEY`。熔断后还没轮到的代码不进妙想，改走全市场排名 | 交易日 16:30 之后。单线程，约 2.5 次/秒，带浏览器请求头。本轮失败率超过 5%（至少 20 次）后降到 1 次/秒。第一次每个代码留最近 120 个日期。当天分区不足全市场时会整表重拉。排名兜底只写当日主力，以及单独一列的 5 日合计 |
| 个股盘中主力净流入 | akshare 同花顺 `stock_fund_flow_individual(symbol="即时")`。金额按「亿」「万」后缀换成元，没有后缀按元 | 交易日连续竞价 9:30–11:30、13:00–15:00 每 6 分钟一份快照。午休、收盘后、周末不拉，也不补历史。走东财共用限流（同花顺分页占用同一间隔），单次连接 10 秒、读取 30 秒。失败后 2 秒、4 秒、8 秒退避，最多 3 次重试。空表不覆盖已有快照 |
| 行业 / 概念资金 | akshare 同花顺，`symbol="即时"` | 交易日 9:30–15:00 每 3 分钟一份盘中快照，60 秒内不重复请求。15:05 之后再写一份收盘快照 |
| 融资融券 | `stock_margin_sse`、`stock_margin_detail_sse`、深交所对应接口。深市空表（pandas `Length mismatch`）不当成源故障，保留已拿到的沪市，并再试上一交易日的深市 | 次日约 08:30 拉上一交易日。20:00 若那天还没落盘再试一次。融资余额按 akshare 的元入库，页面除以 1 亿显示为亿元。2026-10-09 沪市融资余额 12914.37 亿元，约 1.29 万亿元 |
| ETF 份额 | 上交所 `fund_etf_scale_sse`；深交所 `fund_scale_daily_szse`（[ETF规模](https://www.szse.cn/market/fund/volume/etf/index.html)，`symbol="ETF"`） | 交易日 16:40。两边的裸数字都是份：上交所接口在 akshare 1.19.1 里已经把万份乘了 10000；深交所列名是「基金规模(份)」，入库不再乘。带「万」「亿」的文本仍按后缀换。`fund_etf_scale_szse` 只有当前快照、没有交易日，不用它回补。深市不走东财，所以不进东财限流器。一边失败时另一边仍落盘，当天记错误并稍后重试。`data/etf_flow` 里已有的 ETF领航者净流入按代码和日期接上，对不上就空着 |
| 龙虎榜 | 现有 fuyao `data/dragon_tiger` 仍是主源。没有结果时读 akshare `stock_lhb_detail_em` 备份 | 备份在 17:30 之后拉当天 |
| 南向 | `stock_hsgt_hist_em(symbol="南向资金")` | 16:40。净流入入库，裸数字按亿元 |
| 北向成交额 | 同一接口，`symbol="北向资金"` | 只在 `FUND_FLOW_NORTHBOUND_TURNOVER` 打开时拉。只存买入加卖出的成交额，不存净流入。北向净流入自 2024-08-16 起没有数据 |

妙想没有密钥就跳过，并记一条日志。单轮默认最多 30 次。响应里如果带剩余次数，用这个剩余次数继续判断，同时仍受单轮上限约束。只给已经请求过、东财没拿到的代码补主力净流入，大单和超大单留空。

efinance 导入时会在包内 `data/` 建搜索缓存。app 镜像构建把该目录设成 `1777`。进程启动后把搜索缓存改写到 `data/efinance`，避免非 root 用户写不进 site-packages。个股账单本身不再调用 `get_history_bill`。

每个源连续 8 次调用失败后熔断 10 分钟。失败后的重试间隔是 2 秒、4 秒、8 秒，最多 3 次重试。熔断打开后，这一轮已经拿到的个股行仍写入；全市场排名不走这条熔断，避免东财被拦时把同花顺板块也停掉。

资金页的个股榜需要接近全市场的样本（至少 1000 只；股票池更小时以池子为准）。不足这个数的分区标成样本不完整，不展示成排名。样本够时分别列出主力净流入前 10 和净流出前 10，名称优先用落盘列，否则用 `instruments.parquet`。两融按交易日给出沪深合计；只有一边时标成「仅沪市」或「仅深市」，不把缺的一边写成 0。

## 落盘

```text
data/fund_flow/<kind>/date=YYYY-MM-DD/part.parquet
```

`kind` 是 `stock`、`stock_intraday`、`industry`、`concept`、`margin`、`etf_shares`、`southbound`、`northbound_turnover`、`lhb`。

`stock_intraday` 按 `code` + `captured_at` 留多份快照。页面只读当天最近一份的主力净流入。昨天的快照不会显示成今天的即时数。没有该代码时留空，不写成 0。自选表和个股详情用红表示净流入、绿表示净流出。

写入先写临时文件再替换。同一主键后写覆盖先写。金额进库后是元，份额是份。板块排名 1 表示净流入最大。

历史份额从 2025-10-10 补到 ``end``（默认今天）。没传交易日列表时，本地日 K 分区能盖住起点就只补这些交易日，避开节假日；盖不住才按工作日补。休市日上交所空表（akshare 的列名 KeyError）当成正常空结果，不计入熔断。这一年按日拉上交所，不挂在 30 秒调度上，需要时手动跑：

```python
from pathlib import Path

from app.fund_flow.circuit import CircuitBreaker
from app.fund_flow.jobs import backfill_etf_share_history
from app.fund_flow.sources import live_clients

backfill_etf_share_history(Path("data"), live_clients(), breaker=CircuitBreaker("akshare"))
```

深交所报表一次超过 6 个月会返回带表头的空表，回补按不超过 150 天的窗口切。资金页的宽基名单包含沪深 300（含深市 159919）、科创 50，以及创业板（159915，名称像「创业板ETF」「创业板50ETF」）。沪深 300 和科创 50 标成国家队代理，创业板只标指数名。主题产品（例如创业板成长 ETF）不进这份固定名单。

这套拉取不进界面上的数据任务槽，也不占重任务执行槽。调度器每 30 秒只唤醒后台线程。日 K 管道或分钟同步正在写盘时，这一轮让路。

## 接口

需要登录，和别的 `/api` 一样。开放令牌的 `read:analysis` 可以读下表里除 `dsa-context` 以外的路径。

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | `/api/fund-flow/health` | 开关、各分区最新日期、最近错误、熔断状态 |
| GET | `/api/fund-flow/board` | 资金流向页用的排行和短趋势。没有的块是空列表。个股带 `inflow` / `outflow`，样本不够时 `partial` 为真且名单为空。两融 `days` 带合计和「沪深合计 / 仅沪市 / 仅深市」 |
| GET | `/api/fund-flow/stock/{symbol}` | 该股账单，以及主力净流入 5 日 |
| GET | `/api/fund-flow/intraday?symbols=` | 当天最近一份盘中主力净流入。`symbols` 为逗号分隔代码，最多 2000 只。开放令牌的 `read:analysis` 可以读 |
| GET | `/api/fund-flow/sectors?kind=industry\|concept` | 行业或概念排名。有收盘快照用收盘，否则用当天最后一次盘中 |
| GET | `/api/fund-flow/margin` | 两融汇总，明细默认最多 100 行 |
| GET | `/api/fund-flow/etf-shares` | ETF 份额，能接上领航者时带 `flow_net` |
| GET | `/api/fund-flow/dsa-context` | 给 DSA 的短摘要。请求头 `X-News-Feed-Token` 与资讯桥相同，不对则 404 |

没有数据时 `items` 为空，不把缺失写成 0。

## 因子

选股条件里可以引用：

- `ff_main_net`：当日主力净流入，单位元。当日无账单为空。
- `ff_main_net_3d` / `ff_main_net_5d` / `ff_main_net_10d`：最近 3、5、10 个已落盘交易日的主力净流入合计。窗口不足为空，不用 0 填。按交易日对齐，不用更晚的日期。
- `ff_large_net` / `ff_super_net`：当日大单、超大单净流入。源列缺失时为空。
- `ff_sector_net_inflow_rank`：行业净流入排名。行业名取扩展表「所属同花顺行业」横杠最后一段，对不上为空。只贴在当日帧上，因为行业归属是最新快照。

这些资金因子不进入挖掘用的因子目录，避免改动原有目录顺序。热门事件个股行显示 5 日主力净流入，板块行显示净流入排名。

## 开关

| 变量 | 代码默认 | `.env.example` |
| --- | --- | --- |
| `FUND_FLOW_ENABLED` | 关 | 本部署打开 |
| `FUND_FLOW_STOCK` | 关 | 打开 |
| `FUND_FLOW_STOCK_INTRADAY` | 总开关打开时默认开 | 打开。写成 false 才关 |
| `FUND_FLOW_SECTOR` | 关 | 打开 |
| `FUND_FLOW_MARGIN` | 关 | 打开 |
| `FUND_FLOW_ETF_SHARES` | 关 | 打开 |
| `FUND_FLOW_SOUTHBOUND` | 关 | 打开 |
| `FUND_FLOW_NORTHBOUND_TURNOVER` | 关 | 打开（只成交额） |
| `FUND_FLOW_LHB_BACKUP` | 关 | 打开 |
| `MX_APIKEY` | 空 | 注释，需自行填写 |
| `FUND_FLOW_MX_MAX_CALLS` | 30 | 注释 |

总开关关掉时，子开关即使写了 true 也不拉。
