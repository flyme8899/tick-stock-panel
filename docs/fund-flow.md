# A 股资金进出

本地落盘的资金数据。选股因子、热门事件和决策工作台的大盘复盘读的是这里的 parquet，不在页面请求里打外部接口。

代码默认关闭。环境变量没有设置时，后台线程会起来，但不会出网。`.env.example` 里为本 HK 部署写了打开值，复制到 `.env` 后才按下面的时刻拉取。

## 不使用的接口

HK 服务器上这些入口不可用，调用表里没有它们：

- `ak.stock_individual_fund_flow`
- `ak.stock_individual_fund_flow_rank`
- `ak.stock_main_fund_flow`
- 东财 push2 板块资金流（含 `ak.stock_sector_fund_flow_rank`）

个股不用 akshare。板块用同花顺 `stock_fund_flow_industry` / `stock_fund_flow_concept`。

## 来源和时刻

时间都是 Asia/Shanghai。非交易日不跑。交易日探针不确定时，工作日仍会跑。

| 数据 | 源 | 时刻 |
| --- | --- | --- |
| 个股主力 / 大单 / 超大单 | efinance `get_history_bill`。缺的代码用妙想 `MX_APIKEY` | 交易日 16:30 之后。单线程，约 2.5 次/秒。本轮失败率超过 5%（至少 20 次）后降到 1 次/秒。第一次每个代码留最近 120 个日期，之后只补还没有的代码 |
| 行业 / 概念资金 | akshare 同花顺，`symbol="即时"` | 交易日 9:30–15:00 每 3 分钟一份盘中快照，60 秒内不重复请求。15:05 之后再写一份收盘快照 |
| 融资融券 | `stock_margin_sse`、`stock_margin_detail_sse`、深交所对应接口 | 次日约 08:30 拉上一交易日。20:00 若那天还没落盘再试一次 |
| ETF 份额 | `fund_etf_scale_sse` | 交易日 16:40。份额按万份换成份。`data/etf_flow` 里已有的 ETF领航者净流入按代码和日期接上，对不上就空着 |
| 龙虎榜 | 现有 fuyao `data/dragon_tiger` 仍是主源。没有结果时读 akshare `stock_lhb_detail_em` 备份 | 备份在 17:30 之后拉当天 |
| 南向 | `stock_hsgt_hist_em(symbol="南向资金")` | 16:40。净流入入库，裸数字按亿元 |
| 北向成交额 | 同一接口，`symbol="北向资金"` | 只在 `FUND_FLOW_NORTHBOUND_TURNOVER` 打开时拉。只存买入加卖出的成交额，不存净流入。北向净流入自 2024-08-16 起没有数据 |

妙想没有密钥就跳过。单轮默认最多 30 次；响应里如果有剩余次数，用剩余次数和这个上限里更小的那个。只给 efinance 没拿到的代码补主力净流入，大单和超大单留空。

每个源连续 8 次调用失败后熔断 10 分钟。失败后的重试间隔是 2 秒、4 秒、8 秒，最多 3 次重试。

## 落盘

```text
data/fund_flow/<kind>/date=YYYY-MM-DD/part.parquet
```

`kind` 是 `stock`、`industry`、`concept`、`margin`、`etf_shares`、`southbound`、`northbound_turnover`、`lhb`。

写入先写临时文件再替换。同一主键后写覆盖先写。金额进库后是元，份额是份。板块排名 1 表示净流入最大。

这套拉取不进界面上的数据任务槽，也不占重任务执行槽。调度器每 30 秒只唤醒后台线程。日 K 管道或分钟同步正在写盘时，这一轮让路。

## 接口

需要登录，和别的 `/api` 一样。开放令牌的 `read:analysis` 可以读下面前五个，不能读 `dsa-context`。

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | `/api/fund-flow/health` | 开关、各分区最新日期、最近错误、熔断状态 |
| GET | `/api/fund-flow/stock/{symbol}` | 该股账单，以及主力净流入 5 日 |
| GET | `/api/fund-flow/sectors?kind=industry\|concept` | 行业或概念排名。有收盘快照用收盘，否则用当天最后一次盘中 |
| GET | `/api/fund-flow/margin` | 两融汇总，明细默认最多 100 行 |
| GET | `/api/fund-flow/etf-shares` | ETF 份额，能接上领航者时带 `flow_net` |
| GET | `/api/fund-flow/dsa-context` | 给 DSA 的短摘要。请求头 `X-News-Feed-Token` 与资讯桥相同，不对则 404 |

没有数据时 `items` 为空，不把缺失写成 0。

## 因子

选股条件里可以引用：

- `ff_main_net_5d`：最近 5 个已落盘交易日的主力净流入合计，单位元。不足 5 日为空。按交易日对齐，不用更晚的日期。
- `ff_sector_net_inflow_rank`：行业净流入排名。行业名取扩展表「所属同花顺行业」横杠最后一段，对不上为空。只贴在当日帧上，因为行业归属是最新快照。

这两个因子不进入挖掘用的因子目录，避免改动原有目录顺序。热门事件个股行显示 5 日主力净流入，板块行显示净流入排名。

## 开关

| 变量 | 代码默认 | `.env.example` |
| --- | --- | --- |
| `FUND_FLOW_ENABLED` | 关 | 本部署打开 |
| `FUND_FLOW_STOCK` | 关 | 打开 |
| `FUND_FLOW_SECTOR` | 关 | 打开 |
| `FUND_FLOW_MARGIN` | 关 | 打开 |
| `FUND_FLOW_ETF_SHARES` | 关 | 打开 |
| `FUND_FLOW_SOUTHBOUND` | 关 | 打开 |
| `FUND_FLOW_NORTHBOUND_TURNOVER` | 关 | 打开（只成交额） |
| `FUND_FLOW_LHB_BACKUP` | 关 | 打开 |
| `MX_APIKEY` | 空 | 注释，需自行填写 |
| `FUND_FLOW_MX_MAX_CALLS` | 30 | 注释 |

总开关关掉时，子开关即使写了 true 也不拉。
