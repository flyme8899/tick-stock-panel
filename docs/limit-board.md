# 打板与研报

本地落盘的涨停池、涨停原因和机构研报。连板梯队、个股分析和热门事件读的是这里的 parquet，页面请求不出网。

字段解析和东财请求节奏改写自 [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) 的 `SKILL.md`（Apache-2.0，Copyright 2026 Simon Lin）。那个仓库不是依赖。归因写在 `backend/app/eastmoney/NOTICE`。不下载 PDF。

## 落盘

```
data/limit_board/{zt,zb,dt,yzt,sentiment,ths}/date=YYYY-MM-DD/part.parquet
data/research/{stock,industry}/date=YYYY-MM-DD/part.parquet
data/market_feeds/state.json
data/research/state.json
```

涨停、炸板、跌停、昨日涨停是当天快照，后写整表替换先写。开板的股票不会留在上一份涨停池里。接口明确返回空池时写成空分区；请求失败不写。

研报按发布日合并，同一 `info_code` 后写覆盖先写。空列表不创建分区。

金额单位是元。东财池子里的涨跌幅、换手率沿用百分数（10 表示 10%）。价格在东财响应里是 ×1000 的整数，落盘前除以 1000。炸板率 `break_rate` 和同花顺封板率 `seal_rate` 存成 0–1 的比例。页面只在 0 到 1 之间把它格式化成百分数，大于 1 的原值不除以 100。

同花顺 `first_limit_up_time` 是 Unix 秒，按北京时间格式化。

## 调度

一条后台线程，名字是 `market-feeds`，不占数据管道的任务槽。日 K 同步占着写盘时这一轮让路。

| 时刻（北京时间，工作日） | 做什么 |
| --- | --- |
| 09:35–15:00，每 10 分钟 | 刷新东财四池。同花顺探活成功才拉涨停揭秘 |
| 15:10 之后，每个交易日一次 | 收盘快照。涨停、炸板、跌停都成功才写情绪汇总 |
| 16:00 之后 | 研报。每 30 秒醒一次，每轮只做行业列表或一小批个股 |
| 次日 09:35 之前 | 补上一个工作日还没写成的收盘快照和研报 |

周末不拉。交易日日历读不到时，工作日按交易日处理。

情绪汇总里的炸板率是炸板数 /（涨停数 + 炸板数）。昨日涨停里涨跌幅达到 9.8 个百分数的只数记为晋级家数。连板梯队按涨停池的连板数计数，存在 `ladder_json`。

研报每轮默认 8 只股票，一天最多 40 只。顺序是热门事件里的股票，然后是自选。每只股票翻到已经见过的 `infoCode` 就停，最多 2 页，每页 50 条。行业列表默认只回溯 14 天，同样最多 2 页。失败的代码记下来，不在当天反复重试。

近 3 个发布日的研报写入资讯库，来源是 `research`。资讯轮询本身不拉研报。PDF 地址按 `https://pdf.dfcfw.com/pdf/H3_{infoCode}_1.pdf` 拼出来，请求不会打到这个域名。

北交所老号段 43、83、87 以及沪市 `000` 开头的指数代码会拒绝，不把它们当成「没有研报」。

## 同花顺

先请求 `limit_up/limit_up_pool`，`page=1`、`limit=1`、超时 4 秒。探不到、状态码异常或响应没有 `data` 对象时，记为不可用，东财池子照常落盘。探活成功后再拉最多 2 页。这台机器不在国内时，探活失败是正常情况。

## 热门事件

聚类完成之后、盘面验证之前，用当天同花顺涨停原因核对事件：

- 事件里已经有的概念，如果某只提及股票的涨停题材对得上，记入 `limit_reason.verified`。宽基行业名已经在事件上时也可以被核对。
- 更细的题材、并且不在宽基名单里的，最多补 3 个，来源记为 `limit_reason`。人工智能、白酒这类宽基名称不会新加进去。
- 盘面验证的成分股把这些涨停股放在概念成分前面，每个概念最多 30 只。没有概念成分表时，仍然用涨停原因对上的股票。
- 确认分公式不变。核对上的概念个数放在 `confirmation.detail.limit_reason_hits`，不另加分。

## 东财限流

打板四池、研报列表，以及资金进出的东财日频账单，走同一把进程内锁。默认最小间隔 1 秒，加上 0.05–0.2 秒抖动。间隔低于 0.5 秒时仍按 0.5 秒，也就是最快 2 次/秒。HTTP 403 立刻熔断 600 秒。其他失败连续 5 次也熔断。非东财域名和任何 PDF 地址直接拒绝。

测试进程设置了 `PYTEST_CURRENT_TEST` 时，后台线程空转，不出网。

## 开关

未设置环境变量时默认打开。`.env.example` 里同样写成 true。设为 false 才关闭。

| 变量 | 作用 |
| --- | --- |
| `LIMIT_BOARD_ENABLED` | 打板总开关 |
| `LIMIT_BOARD_EASTMONEY` | 东财四池和情绪汇总 |
| `LIMIT_BOARD_THS` | 同花顺涨停揭秘 |
| `RESEARCH_ENABLED` | 研报总开关 |
| `RESEARCH_STOCK` | 自选和热门事件个股 |
| `RESEARCH_INDUSTRY` | 行业研报列表 |
| `RESEARCH_BATCH_SIZE` | 每轮个股数，默认 8，最大 20 |
| `RESEARCH_MAX_SYMBOLS` | 每天个股数，默认 40，最大 80 |
| `EASTMONEY_MIN_INTERVAL` | 东财最小间隔秒数，默认 1，下限 0.5 |
| `NEWS_RESEARCH_ENABLED` | 是否把研报写入资讯库。设了之后页面不能改这个来源 |

## 只读接口

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | `/api/screener/limit-ladder` | 原连板梯队。每只股票多 `limit_reason`、`limit_themes`、`seal_rate`。响应多 `sentiment` |
| GET | `/api/limit-board/health` | 开关、各分区最新日期、东财熔断状态 |
| GET | `/api/limit-board/sentiment` | 最近一份或指定日期的情绪汇总 |
| GET | `/api/research/health` | 研报开关和最新发布日 |
| GET | `/api/research/stock/{symbol}` | 该股研报，默认 20 条 |
| GET | `/api/research/industry` | 行业研报，默认 30 条 |

个股分析页读 `/api/research/stock/{symbol}`。热门事件把 `source=research` 的资讯和其他来源一起聚类。

同一接口另有 `ima_reports`：个人 ima 知识库里提及该股的券商研报，带正文。全文不在东财 parquet 里，落在 `data/research/ima_reports.sqlite`。调度、签名链接和热门事件只收短摘要的约定见 [多源资讯](./news-sources.md)。
