# 业绩预告 / 业绩快报 / 业绩报表

模块：`backend/app/services/earnings_sync.py`。数据来自东财数据中心，与 akshare 的 `stock_yjyg_em` / `stock_yjkb_em` / `stock_yjbb_em` 是同一批接口。这里直接走 `em_get`，与资金流共用东财限流。

## 存储

与 TickFlow 财务表放在同一个 `data/financials/` 目录下，按报告期分区：

| 目录 | 内容 | 东财报表 | 去重键 |
|---|---|---|---|
| `financials/em_forecast/report_period=YYYY-MM-DD/` | 业绩预告 | RPT_PUBLIC_OP_NEWPREDICT | code, report_period, source, announce_date, indicator_code |
| `financials/em_express/report_period=YYYY-MM-DD/` | 业绩快报 | RPT_FCI_PERFORMANCEE | code, report_period, source, announce_date |
| `financials/em_report/report_period=YYYY-MM-DD/` | 业绩报表 | RPT_LICO_FN_CPD | code, report_period, source, announce_date |

- `announce_date` 是公告日（NOTICE_DATE），`report_period` 是报告期，另外保留 `update_date`、`eitime` 和 `fetched_at`。
- 同一个键出现多次时，按 `update_date` → `eitime` → `fetched_at` 保留最新的一行。重复写入是幂等的；接口里已经消失的旧行不会被删。
- 一份预告会按指标拆成多行（净利润、扣非净利润、营收、EPS 等），所以预告的去重键多一个 `indicator_code`。
- 金额单位是元，同比、环比和增幅单位是百分数。代码以文本保存，带交易所后缀的 `symbol` 来自 SECUCODE。
- 接口还会返回非 A 股的行，原样保留。例如 2026 半年报的业绩报表共 11,445 行：`security_type_code` 为 058001001（A 股）的 5,581 行、058001005（非 A 股挂牌公司，约 5,785 行）、058001002（B 股）78 行。研究 A 股时请按 `security_type_code == "058001001"` 筛选。

**注意：业绩快报只是库内当前版本。** 东财接口只返回快报的当前状态：公司发正式报告后，快报行可能被改写，比如数值清空、`update_date` 变新。所以回补拿到的是今天看到的样子，并不是当年公告时的原值；只有从每日增量上线起，才会逐日留存当时的版本。业绩报表遇到更正公告时同理。做时点研究时，用 `announce_date` 作为可交易日（T+1），同时注意这一点。

## 每日增量

调度器在工作日 18:40（北京时间）拉最近 2 个报告期，比如 10 月拉 06-30 和 09-30，1 月拉 09-30 和 12-31。
- 设 `EARNINGS_SYNC_ENABLED=0` 可以关闭；测试进程里不会出网。
- 和历史回补共用一把文件锁（`financials/.em_earnings.lock`），拿不到锁的一方本轮跳过。

## 历史回补（2018Q1 至今，35 个报告期）

```bash
cd ~/tick-stock-panel
nice -n 19 ionice -c3 docker compose run -d --rm --no-deps --name earnings_backfill app sh -c \
  "uv run --no-sync python -u -m app.services.earnings_sync --from 2018-03-31 --spacing 1.0 \
   > /app/data/financials/earnings_backfill.log 2>&1"
```

- 请求按报告期、数据集顺序逐个发，每次间隔至少 1 秒，另外还受东财共用限流约束。
- 某一期失败只是不写这一期，不影响其它期，日志里会有 `error` 行；重跑同一命令即可补齐。
- 只补某几张表可以加 `--datasets em_forecast,em_report`。

快照导出脚本会把三张表导出为 `financial_forecast` / `financial_express` / `financial_report`（parquet + CSV，全部报告期）。
