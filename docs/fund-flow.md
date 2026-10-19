# A 股资金进出

本地落盘的资金数据。选股因子、热门事件和决策工作台的大盘复盘读的是这里的 parquet，不在页面请求里打外部接口。

代码默认关闭。环境变量没有设置时，后台线程会起来，但不会出网。`.env.example` 里为本 HK 部署写了打开值，复制到 `.env` 后才按下面的时刻拉取。

## 不使用的接口

HK 服务器上这些入口不可用，调用表里没有它们：

- `ak.stock_individual_fund_flow`（逐只，仍走东财 push2his 的代码搜索）
- `ak.stock_main_fund_flow`
- 东财 push2 板块资金流（含 `ak.stock_sector_fund_flow_rank`）

个股日频账单用显式 secid 直接请求东财 push2his，不再经 efinance 搜索代码。全市场当日 / 5 日排名才用 akshare：同花顺 `stock_fund_flow_individual` 优先，东财 `stock_individual_fund_flow_rank` 作备选。盘中个股主力净流入相反，默认先打东财当日排名，不够再退回同花顺，见下表。同花顺个股榜缺深市主板和北交所时，用东财数据中心 ``RPT_DMSK_TS_FUNDFLOW`` 分页补一次当日主力、大单和超大单，走统一限速和超时。深市主板按交易日拉全市场后只留 000/001/002/003。北交所同花顺榜没有，按 430、8 字头和 920 三段代码过滤后再翻页，不把沪深再拉一遍。这套接口和龙虎榜、两融、PE 相同。行情列表 ``push2`` 在部分云服务器上返回 502，这里不再用它补板块，也不再对这两个板块逐只打历史账单。数据中心这三个净额字段的单位是万元，落盘前换成元。同花顺兜底只补还没有东财行的代码；分区里已经是东财或数据中心来源的行，后到的 `akshare_ths` 不会覆盖。全市场历史回补见下文。日志里的「整段缺板块」按缺失只数占该板块应有只数达到 80% 才写；只缺几只科创板不会报整段缺失。板块用同花顺 `stock_fund_flow_industry` / `stock_fund_flow_concept`。东财 push2 的板块资金流接口仍然不调用。

## 来源和时刻

时间都是 Asia/Shanghai。到点任务只在交易日跑。交易日探针不确定时，工作日仍会跑。开关在非交易日、或交易日收盘前提前打开时，会补最近一个还缺分区的交易日；30 分钟内不重复尝试。盘中板块快照和个股即时快照不在这批补数里。

| 数据 | 源 | 时刻 |
| --- | --- | --- |
| 个股主力 / 大单 / 超大单 | 东财 push2his 日 K，secid 由代码推导（沪市 `1.`，深市和北交所 `0.`）。已经请求过但没拿到的代码用妙想 `MX_APIKEY`。熔断后还没轮到的代码不进妙想，改走全市场排名 | 交易日 16:30 之后。单线程，约 2.5 次/秒，带浏览器请求头。本轮失败率超过 5%（至少 20 次）后降到 1 次/秒。第一次每个代码留最近 120 个日期。当天分区不足全市场时会整表重拉。排名兜底只写当日主力，以及单独一列的 5 日合计 |
| 个股盘中主力净流入 | 默认东财 akshare `stock_individual_fund_flow_rank(indicator="今日")`。`今日主力净流入-净额` 以及超大单、大单、中单、小单净额都已是元，正数净流入、负数净流出，不乘万、不改符号。失败、空表或有效行不足 1000 时记日志，改走同花顺 `stock_fund_flow_individual(symbol="即时")` 的「净额」（流入减流出，带「亿」「万」按后缀，否则按元）。`FUND_FLOW_INTRADAY_PRIMARY=ths` 时只走同花顺 | 交易日连续竞价 9:30–11:30、13:00–15:00 每 6 分钟一份快照。午休、收盘后、周末不拉，也不补历史。东财只试一次（分页加 1 秒限流大约要一分多钟，不再做 2/4/8 秒重试）。同花顺仍按 2 秒、4 秒、8 秒退避，最多 3 次重试。两边都走东财共用限流，单次连接 10 秒、读取 30 秒。这次拉取在旁路线程，不挡住同一轮的板块和其它资金任务。空表不覆盖已有快照 |
| 行业 / 概念资金 | akshare 同花顺，`symbol="即时"` | 交易日 9:30–15:00 每 3 分钟一份盘中快照，60 秒内不重复请求。15:05 之后再写一份收盘快照 |
| 融资融券 | `stock_margin_sse`、`stock_margin_detail_sse`、深交所对应接口。深市空表（pandas `Length mismatch`）不当成源故障，保留已拿到的沪市，并再试上一交易日的深市 | 次日约 08:30 拉上一交易日。20:00 若那天还没落盘再试一次。融资余额按 akshare 的元入库，页面除以 1 亿显示为亿元。2026-10-09 沪市融资余额 12914.37 亿元，约 1.29 万亿元 |
| ETF 份额 | 上交所 `fund_etf_scale_sse`；深交所 `fund_scale_daily_szse`（[ETF规模](https://www.szse.cn/market/fund/volume/etf/index.html)，`symbol="ETF"`） | 交易日 16:40。两边的裸数字都是份：上交所接口在 akshare 1.19.1 里已经把万份乘了 10000；深交所列名是「基金规模(份)」，入库不再乘。带「万」「亿」的文本仍按后缀换。`fund_etf_scale_szse` 只有当前快照、没有交易日，不用它回补。深市不走东财，所以不进东财限流器。一边失败时另一边仍落盘，当天记错误并稍后重试。净流入（亿元）按 `(当日份额 − 上一份落盘份额) × 当日收盘价 / 1e8` 计算，收盘价读 `kline_etf_daily`。两份落盘之间隔了超过 1 个交易日，或 `adj_factor_etf` 出现拆分级别的因子跳变时，这一日留空。5 日 / 20 日是这些 1 日值按落盘份数滚动相加，中间有空日则合计留空。ETF领航者金额在 `data/news/news.sqlite` 的 `etf_flow` 稿里（`raw_json.extracted` 的 `net_1d` / `net_5d` / `net_20d`，单位已经是亿元），按标题交易日和 6 位代码接上，对不上就空着。推算的 1 日和领航者 1 日相对差超过 20% 时，页面把该行标出来 |
| 龙虎榜 | 现有 fuyao `data/dragon_tiger` 仍是主源。没有结果时读 akshare `stock_lhb_detail_em` 备份 | 备份在 17:30 之后拉当天 |
| 南向 | `stock_hsgt_hist_em(symbol="南向资金")` | 16:40。净流入入库，裸数字按亿元 |
| 北向成交额 | 同一接口，`symbol="北向资金"` | 只在 `FUND_FLOW_NORTHBOUND_TURNOVER` 打开时拉。只存买入加卖出的成交额，不存净流入。北向净流入自 2024-08-16 起没有数据 |

ETF 净流入用的 ``adj_factor_etf`` 是单次除权比，不是行情源的累计后复权因子。现金分红和份额折算都来自新浪 ``hfq.js``：累计分红做差，累计份额因子做比，除权价按「先折算、再扣折算后每股现金」。510500 这类登记日早于价格跳空的折算，除权日跟到原始收盘真正跳空的那天。``|ex_factor-1|`` 达到 0.05 才当成拆分并把当日净流入留空，普通现金分红仍参与申赎计算。盘后管道每周才拉一次全市场新浪记录，不请求腾讯，本地收盘分批读入。只为本地日 K 最早分区当天及之后的事件要价；更早的事件保留已存行，没存过就跳过，避免 510300、510050、510500 这类早年折算把代码永远留在重试名单里。新浪 ``data`` 为空、腾讯失败或起点之后的价格对不上时，该代码保留旧行，次日只重试失败代码。因子表被清掉（``all.parquet`` 不在，清理时也会删掉 ``sync_state.json``）后，下一次管道立即全量，而不是再空等 7 天。已存的错误因子用 ``python -m scripts.rebuild_etf_adj_factor --symbols 510500.SH,512100.SH`` 重建（镜像里同一命令）：先备份到与因子目录并列的 ``backup/adj_factor_etf/``（不放进 ``adj_factor_etf/``，避免 ``**/*.parquet`` 把旧因子再并进净流入），再按标的整表替换。``--symbols`` 只重跑列出的代码。重复执行不改已经正确的内容。

妙想没有密钥就跳过，并记一条日志。单轮默认最多 30 次。响应里如果带剩余次数，用这个剩余次数继续判断，同时仍受单轮上限约束。只给已经请求过、东财没拿到的代码补主力净流入，大单和超大单留空。

efinance 导入时会在包内 `data/` 建搜索缓存。app 镜像构建把该目录设成 `1777`。进程启动后把搜索缓存改写到 `data/efinance`，避免非 root 用户写不进 site-packages。个股账单本身不再调用 `get_history_bill`。

每个源连续 8 次调用失败后熔断 10 分钟。失败后的重试间隔是 2 秒、4 秒、8 秒，最多 3 次重试。熔断打开后，这一轮已经拿到的个股行仍写入；全市场排名不走这条熔断，避免东财被拦时把同花顺板块也停掉。

资金页的个股榜需要接近全市场的样本（至少 1000 只；股票池更小时以池子为准）。不足这个数的分区标成样本不完整，不展示成排名。样本够时分别列出主力净流入前 10 和净流出前 10，名称优先用落盘列，否则用 `instruments.parquet`。两融按交易日给出沪深合计；只有一边时标成「仅沪市」或「仅深市」，不把缺的一边写成 0。

## 落盘

```text
data/fund_flow/<kind>/date=YYYY-MM-DD/part.parquet
```

`kind` 是 `stock`、`stock_intraday`、`industry`、`concept`、`margin`、`etf_shares`、`southbound`、`northbound_turnover`、`lhb`。

`stock_intraday` 按 `code` + `captured_at` 留多份快照。每行有 `source`：东财是 `akshare_em_rank`，同花顺备用是 `akshare_ths`。东财行另外有 `super_net`、`large_net`、`medium_net`、`small_net`，单位和符号与 `main_net` 相同；同花顺行这四列为空。页面只读当天最近一份。东财快照的标签是「盘中主力净流入」，同花顺快照的标签是「盘中资金净额(同花顺)」。昨天的快照不会显示成今天的即时数。没有该代码时留空，不写成 0。自选表和个股详情红为正、绿为负。

写入先写临时文件再替换。同一主键后写覆盖先写。金额进库后是元，份额是份。板块排名 1 表示净流入最大。

`etf_shares` 每行另有 `ingested_at` 和 `updated_at`，类型是带 Asia/Shanghai 时区的时间戳（秒级）。`ingested_at` 是这个主键第一次写入本表的时刻，同一天再次采集不覆盖。份额、名称、代码或来源变了，才把 `updated_at` 写成这次写入的时刻；没变过则为空。调用方自己带的时刻会被丢掉，回补不能把执行时间写成首次可见时间。

这两个字段给时点研究用：`trade_date` 是份额所属交易日，`ingested_at` 才是这份份额什么时候出现在库里。此改动上线前已经落盘、之后没有再写过的分区文件里没有这两列。读取、DuckDB 视图（`etf_shares`，`union_by_name=true`）和研究快照导出都把缺列当成空，不回填，也不用文件修改时间冒充入库时间。研究快照另加派生列 `source_mode`（`backfill` / `daily` / `late`），只在导出文件里，不进这张表：空的 `ingested_at` 是 `backfill`；早于数据日下一交易日北京时间 09:30 是 `daily`，否则是 `late`。这样的历史行不能当成「当天收盘后已经可见」。某天后来被重写、但份额等业务列没变时，`ingested_at` 仍然留空。业务列变了则只填 `updated_at`，首次可见时间依旧未知。净流入计算只读份额，不读这两列。资金页看板也不下发它们；`GET /api/fund-flow/etf-shares` 会带上，空值仍是 JSON `null`。

历史份额从 2025-10-10 补到 ``end``（默认今天）。没传交易日列表时，本地日 K 分区能盖住起点就只补这些交易日，避开节假日；盖不住才按工作日补。休市日上交所空表（akshare 的列名 KeyError）当成正常空结果，不计入熔断。这一年按日拉上交所，不挂在 30 秒调度上，需要时手动跑：

```python
from pathlib import Path

from app.fund_flow.circuit import CircuitBreaker
from app.fund_flow.jobs import backfill_etf_share_history
from app.fund_flow.sources import live_clients

backfill_etf_share_history(Path("data"), live_clients(), breaker=CircuitBreaker("akshare"))
```

深交所报表一次超过 6 个月会返回带表头的空表，日历跨度仍限制在 150 天内。同一窗口还按行数再切：xls 含表头最多 65536 行，单次请求按每天 2000 行的保守估计留在 2 万行以内，远低于 65535 条数据。返回行数达到 65000 时当成已经截断，报错并且不写入，再把窗口拆短重拉。落盘后比较每个交易日的深市行数（代码以 15 开头），比前一日或后一日少了超过 5% 就记警告并记入错误；整日没有分区，或分区在但行数这样掉下去的，缺日回补都会重拉。只补缺日时用 `backfill_etf_share_history(..., gaps_only=True)`。

ETF 名称另有一份参考表 `data/fund_flow/etf_meta/meta.parquet`，字段是 `code`、`full_name`、`short_name`、`index_code`、`index_name`、`company`、`source`。它不是按日分区，份额历史文件里的 `name` 仍是交易所原文。`sync_etf_shares` 在写完当日份额后刷新这张表：上交所 `commonSoaQuery.do?sqlId=FUND_LIST&fundType=00`（`secNameFull` / `fundAbbr` / `INDEX_CODE` / `INDEX_NAME` / `companyName`），深交所 `ShowReport` `CATALOGID=1105` 里基金类别为 ETF 的行，新浪 `etf_hq_fund` 只补两边都没有的代码。某个源失败或返回空表时，该源沿用上一版，不会把整张表清空。

看板和 `GET /api/fund-flow/etf-shares` 的 `name` 用 `full_name`。参考表没有这只、代码又是 5 开头时，从份额里的简称去掉开头的 `XD` / `XR` / `DR` / `UC` / `N`。响应另有 `short_name`，没有短简称时为 `null`。

宽基按跟踪指数分组，不再靠名称子串。沪市认指数代码：`000300` 沪深300，`000688` 科创50，`399006` 创业板。深市没有指数代码，名称要严格对上「沪深300」「科创50」「创业板ETF / 创业板50ETF / 创业板指」，并且名称里带成长、价值、增强、红利、ESG 的不算。所以 588020（科创成长，指数 `000690`）不是科创50，159656（沪深300成长）不是沪深300。沪深300和科创50标成国家队代理，创业板只标指数名。同一指数下的产品很多，页面按指数分组，列表限制高度，避免卡片被拉得很长。

这套拉取不进界面上的数据任务槽，也不占重任务执行槽。调度器每 30 秒只唤醒后台线程。日 K 管道或分钟同步正在写盘时，这一轮让路。

## 历史回补（东财数据中心）

个股日频的历史回补不走 push2his，也不走同花顺。push2his 在部分出口会 403。同花顺 `akshare_ths` 的净额经常符号相反、量级差几个数量级，和东财对不上；它曾经以后写覆盖的方式把已经对齐的东财行盖掉。

接口（与当日深市主板 / 北交所补缺同一张报表）：

```text
GET https://datacenter-web.eastmoney.com/api/data/v1/get
    ?reportName=RPT_DMSK_TS_FUNDFLOW
    &columns=SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,NET_INFLOW,BIGDEAL_NET,SUPERDEAL_NET
    &pageSize=500&pageNumber=1
    &sortColumns=SECURITY_CODE&sortTypes=1
    &filter=(TRADE_DATE='YYYY-MM-DD')
```

`NET_INFLOW` 是主力净流入，`BIGDEAL_NET` 是大单，`SUPERDEAL_NET` 是超大单。金额单位是万元，落盘前乘 10000 换成元，写入现有 `stock` 分区的 `main_net` / `large_net` / `super_net`。接口还有各档净占比，现有表没有这些列，回补不新增字段。`SECUCODE` 已带交易所后缀（`600519.SH`），`symbol` 沿用它。来源写成 `eastmoney_datacenter`。5 日合计不从这张报表来，回补行的 `main_net_5d` 留空；因子仍按已落盘的日净额滚动。

按交易日拉全市场再翻页，一天大约 12 页。出网走东财共用限流（`em_get`，默认间隔 1 秒，`EASTMONEY_MIN_INTERVAL` 最快 0.5 秒）。`--concurrency` 默认 3，只让多天并行排页；实际请求仍被这把限流串起来。

镜像里已经包含 `app`，在容器内执行：

```bash
python -m app.fund_flow.backfill --days 120 --data-dir /app/data
python -m app.fund_flow.backfill --from 2026-04-01 --to 2026-10-15 --data-dir /app/data
```

有本地日 K 分区（`kline_daily/date=`）时只补这些交易日；没有时按工作日。`--days` 取不晚于今天的最近 N 个。某一天请求失败不写该分区，避免半截数据被当成全市场。休市空表跳过。写进去的行会盖掉同一天的 `akshare_ths`。

同花顺覆盖规则：同一主键（`code` + `trade_date`）只要已有 `efinance`、`eastmoney_sz_main`、`eastmoney_bj`、`eastmoney_datacenter` 或 `akshare_em_rank`，后到的 `akshare_ths` 整行丢弃。还没有东财行的代码仍可用同花顺补缺。妙想（`mx`）不在这组保护里。

## 接口

需要登录，和别的 `/api` 一样。开放令牌的 `read:analysis` 可以读下表里除 `dsa-context` 以外的路径。

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | `/api/fund-flow/health` | 开关、各分区最新日期、最近错误、熔断状态 |
| GET | `/api/fund-flow/board` | 资金流向页用的排行和短趋势。没有的块是空列表。个股带 `inflow` / `outflow`，样本不够时 `partial` 为真且名单为空。两融 `days` 带合计和「沪深合计 / 仅沪市 / 仅深市」 |
| GET | `/api/fund-flow/stock/{symbol}` | 该股账单，以及主力净流入 5 日 |
| GET | `/api/fund-flow/intraday?symbols=` | 当天最近一份盘中快照。响应和每一行都有 `source`（`akshare_em_rank` 或 `akshare_ths`）。东财行带主力、超大单、大单、中单、小单净额，单位元。`symbols` 为逗号分隔代码，最多 2000 只。开放令牌的 `read:analysis` 可以读 |
| GET | `/api/fund-flow/sectors?kind=industry\|concept` | 行业或概念排名。有收盘快照用收盘，否则用当天最后一次盘中 |
| GET | `/api/fund-flow/margin` | 两融汇总，明细默认最多 100 行 |
| GET | `/api/fund-flow/etf-shares` | ETF 份额。`name` 是参考表里的扩位全称，没有时沪市代码去掉 `XD`/`XR`/`DR`/`UC`/`N` 前缀。`short_name` 是短简称，没有则为空。`net_inflow_1d` / `net_inflow_5d` / `net_inflow_20d` 是份额推算的净流入，单位亿元。能接上领航者时带 `flow_net`（当日）、`flow_net_5d`、`flow_net_20d`，单位同样是亿元。`flow_diverged` 表示 1 日推算和领航者相对差超过 20%。`change_1d` / `change_5d` / `change_20d` 是相对前 1 / 5 / 20 份落盘的份额差（份），换算成当前份额口径：基准日之后到当日为止、已确认的登记日折算倍数连乘到基准份额上。没确认的因子（例如只改价格的现金分红）不乘。与看板 `etf_shares` 同一口径，不足该窗口时为空 |
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
| `FUND_FLOW_INTRADAY_PRIMARY` | `eastmoney` | `eastmoney` 或 `ths`。无法识别时按东财并记日志 |
| `FUND_FLOW_SECTOR` | 关 | 打开 |
| `FUND_FLOW_MARGIN` | 关 | 打开 |
| `FUND_FLOW_ETF_SHARES` | 关 | 打开 |
| `FUND_FLOW_SOUTHBOUND` | 关 | 打开 |
| `FUND_FLOW_NORTHBOUND_TURNOVER` | 关 | 打开（只成交额） |
| `FUND_FLOW_LHB_BACKUP` | 关 | 打开 |
| `MX_APIKEY` | 空 | 注释，需自行填写 |
| `FUND_FLOW_MX_MAX_CALLS` | 30 | 注释 |

总开关关掉时，子开关即使写了 true 也不拉。

## 个股资金流 symbol 口径（2026-10）

- `stock` / `stock_intraday` 分区的 `symbol` 统一为带交易所后缀（`600000.SH`、`000001.SZ`、`920001.BJ`），`code` 始终是 6 位裸代码。写入时补后缀；读取（`store.read_partition`）时同样收口，旧分区不重写也能读到统一口径。
- 一次性重写历史分区：`python -m scripts.normalize_fund_flow_symbols [--dry-run]`。只改 `symbol` 列，不增删行。行数不足全市场的分区（如 `2026-10-08` 只有 6 行 mx）只标记，不补数。

## 盘中个股快照与分钟同步的让路

交易时段内资金流调度只礼让日 K 管道，不再礼让分钟增量同步（分钟增量每 10 秒一轮、几乎常驻持锁，礼让会让下午的 `stock_intraday` 快照整段缺失）。非交易时段仍两者都礼让。
