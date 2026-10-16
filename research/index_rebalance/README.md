# research/index_rebalance — 指数定期调仓的纳入/剔除效应

研究目录（**research-only**）。回答一个问题：**能不能拿到足够的历史数据，研究沪深300 /
中证500 / 中证1000 定期调仓的纳入与剔除效应。**

**不做产品、不改 `backend/`、不写生产表、不产出交易信号、不接面板买卖点。**

---

## 目录

```
research/index_rebalance/
├── probe/                        # 任务①：数据可得性探测（只读）
│   ├── csindex_source.py         #   中证官网只读采集与解析（两代附件格式）
│   ├── probe_index_rebalance.py  #   主探测：覆盖统计 + 自动判定（当前用 --full 覆盖月份经调整）
│   ├── verify_consistency.py     #   名单一致率核对（导出 CSV）
│   ├── consistency_check.csv     #   核对明细
│   ├── PROBE_REPORT.md           #   探测报告（含数据源表、判定）
│   └── _cache/                   #   公告详情 / 附件 / 名单缓存（派生，可删）
├── backtest/
│   ├── check_kline_coverage.py   #   任务②：验 kline_daily 覆盖（**在服务器上跑**）
│   ├── build_samples.py          #   任务③：清洗成 events/members 表
│   ├── make_synth_kline.py       #   合成日K生成器（**仅用于测通管道**）
│   └── run_panel_backtest.py     #   任务④：面板粗回测骨架
├── out/                          # 事件表 / 成员表 / 回测输出
└── REPORT_REBALANCE_BT.md        # 回测报告（覆盖确认后再填正式数字）
```

---

## 一、先说结论：数据可得性

| 项 | 结果 |
| --- | --- |
| 唯一能拿到**历史**调入/调出名单的源 | **中证指数官网公告**（列表 → 详情 → 正文表格/附件） |
| akshare `index_stock_cons_csindex` | ❌ 仅当日快照（三指数日期唯一值均为当天） |
| akshare `index_detail_hist_adjust_cni` | ❌ 只覆盖国证系列；沪深300/中证500/中证1000 全为 0 行 |
| 东财 `RPT_INDEX_TS_COMPONENT` | ❌ 无日期维度，`MAXTRADEDATE` 全是当天 |
| 可用的完整名单区间 | **2021-05 起**（2018–2020 官网只发新闻稿摘要，无完整名单） |
| 清洗后样本 | **32 个「指数×事件」**、3,468 条成员记录 |

详见 [`probe/PROBE_REPORT.md`](probe/PROBE_REPORT.md)。

---

## 二、服务器上跑覆盖检查（**任务 1，必须先做**）

```bash
# 1) 把脚本**和成员表**一起拷进容器
#    （脚本要读 out/members.parquet 抽样本；probe/_cache 是 gitignore 的派生物，容器里没有）
docker compose cp research/index_rebalance/backtest/check_kline_coverage.py app:/tmp/
docker compose cp research/index_rebalance/out/members.parquet app:/tmp/

# 2) 跑覆盖检查（compose 已设 DATA_DIR=/app/data）
docker compose exec app python /tmp/check_kline_coverage.py \
    --data-dir /app/data --members /tmp/members.parquet

# 3) 若报「目录不存在」，先确认数据在哪
docker compose exec app sh -c 'ls -d /app/data/* | head -20'

# 4) 若报「没有 date= 分区」，说明日 K 还没同步
docker compose exec app sh -c 'ls /app/data/kline_daily | head -3; ls /app/data/kline_daily | tail -3'
```

> ⚠️ **不要只拷脚本**。早期版本的脚本从 `probe/_cache/*.lists.json` 读名单，
> 而那份缓存在 `.gitignore` 里 —— 单独拷进容器会抽不到样本，
> 出现「抽样 0 个事件 → 判定不通过」的**假失败**。
> 现已改为读已入库的 `out/members.parquet`。

**判定标准**：能否覆盖 `2020-10-01 ~ 2026-06-30`
（起点 = 最早事件公告日前约 20 交易日；终点 = 最近生效日后 10 交易日）。
脚本会抽 10 个「指数×事件」核对纳入股日 K 完整性，**缺数率 >5% 判失败**。

抽样点名的时点是 **2021-05-28 / 2021-11-26 / 2024-11-29 / 2026-05-29**。
任务书原点的 2020-11-27 那条（公告 11514）因沪深300 与中证香港100 名单混排
**已在样本侧剔除**，成员表里最早的事件是 2021-05-28 ——
它的 20 交易日前置窗口落在 2021-04，已代表「最早一段」，
**区间门槛仍按 `2020-10-01` 原样卡，不因缺 2020-11 而放宽**。

退出码：`0` 通过 / `1` 不通过 / `2` 数据目录或成员表缺失。

> ⚠️ 抽样核对时要区分「**真缺数**」与「**股票已退市**」—— 不少调出股已退市，
> 其日 K 天然不存在，不影响回测（窗口内通常仍有数据）。

---

## 三、复现流程

```bash
# ① 探测（只读中证官网；首次约 3 分钟，受官网 WAF 限速，之后命中 _cache 秒级）
python research/index_rebalance/probe/probe_index_rebalance.py --year 2018 --full
python research/index_rebalance/probe/verify_consistency.py

# ② 清洗样本（不依赖行情）
python research/index_rebalance/backtest/build_samples.py

# ③ 合成自测（验证管道；**不是真实结论**）
python research/index_rebalance/backtest/make_synth_kline.py --out /tmp/synth
python research/index_rebalance/backtest/run_panel_backtest.py \
    --data-dir /tmp/synth \
    --events  research/index_rebalance/out/events.parquet \
    --members research/index_rebalance/out/members.parquet \
    --out     research/index_rebalance/out/synth

# ④ 真数据（**覆盖确认通过后**才跑，出正式结论）
python research/index_rebalance/backtest/run_panel_backtest.py \
    --data-dir /app/data \
    --events  research/index_rebalance/out/events.parquet \
    --members research/index_rebalance/out/members.parquet \
    --out     research/index_rebalance/out/real
```

---

## 四、回测口径（写死，避免事后调参）

| 项 | 口径 | 说明 |
| --- | --- | --- |
| 收益 | 个股**前复权收盘价**（`close`）简单收益 | 避免除权跳空；`raw_close` 为不复权价 |
| 基准 | **同指数**（`kline_index_daily`） | 调仓效应本身是相对该指数的事件，换基准会混入风格收益。**缺失不替换**，记为 nan |
| 窗口 | `T0-5..T0+5`、`T0..T0+10`、`E-5..E+5`、`E..E+10` | T0=公告日，E=生效日；偏移以**实际交易日**计 |
| 买入持有 | 公告日收盘买 → 生效日收盘卖（`BH_T0_E`） | 模拟「调入预期」 |
| 成本 | 单边 **8 / 13 / 18 bp**，**主结论 13 bp** | 往返 `BH_T0_E` 计 **2×**；其余窗口按单边计 |
| 汇总 | 分组均值 + t（**按公告日聚类**的稳健 t） | cluster < 5 时聚类 t 不可靠，回退普通 t |

**成本口径说明**：三档是**单边**（买或卖一次）的费率假设。
`BH_T0_E` 含一买一卖，扣 **2×**；其余窗口代表一次方向性持有，按 1× 扣。
A 股往返实际约 10–15 bp（佣金双边 + 印花税卖出 5 bp + 过户费），13 bp 属中性偏保守。

> ⚠️ 公告通常在**收盘后**发布，`T0` 锚点用的就是公告日当天收盘价。
> 若要更严格的「公告次日才可交易」口径，把锚点整体后移 1 个交易日即可 ——
> 这属于稳健性检查，**不是**放宽门槛的手段。

---

## 五、判定门槛（研究判定，非上线）

| # | 门槛 |
| --- | --- |
| A | 行情覆盖：任务 1 通过（最早分区 ≤ 2020-10-01） |
| B | 调入侧：公告后或生效窗**净超额（扣 13bp）> 0 且 t ≥ 1.96**（事件聚类） |
| C | 调出侧：同窗**净超额 < 0 且 \|t\| ≥ 1.96**，或明确写「无显著负向」 |
| D1 | 稳健：至少两个指数方向一致 |
| D2 | 稳健：**去掉\|事件均值\|最大的 2 个事件后符号不变** |

**任一不过 → 结论写「暂停，不做落地」。禁止为通过而放宽窗口或换基准。**

> D2 按**事件级均值**排序（同一 `notice_id` 内先取均值）再剔除，
> 避免「观测多」被误当成「影响大」；每组事件数 < 3 时判「无法检验」而非放行。

---

## 六、已知限制

| 限制 | 说明 |
| --- | --- |
| **时间范围** | 完整名单自 **2021-05** 起（2018–2020 官网无完整名单）。样本比预期少约 6 次调整。 |
| **2020-11-27 歧义** | 该公告把沪深300 与中证香港100 名单混排（自述 26 只 / 抽出 55 只），**已在 `build_samples.py` 中剔除**并留痕。 |
| **同一股票跨指数升降级** | 703 例出现「同一公告日、同一股票在某指数调入、在另一指数调出」（如沪深300 调入 = 中证500 调出）。做面板时要意识到这是**同一事件的两种角色**，不能当独立样本重复计数。 |
| **合成数据非真实结论** | `--data-dir /tmp/synth` 只用于验证管道。合成数据里上述升降级股票的 ±漂移会抵消，因此部分窗口 in/out 均值相同 —— 这是**构造性质，不是管道 bug**。 |
| **WAF 限速** | 中证官网详情接口突发 ~20+ 次请求即 403，持续约 10–15 分钟。probe 已放慢到 2–6 秒/次并缓存。 |

---

## 七、数据来源与合规

- 数据源：中证指数官网公告（`www.csindex.com.cn`）与附件（`oss-ch.csindex.com.cn`）。
- **只读**：仅 HTTP GET/POST，不写 TSP 数据目录、不改 `backend/`。
- **不用代理**、**不输出密钥**、**未使用付费库**。
- 未产出任何交易信号，未改动面板买卖点。

---

## 八、产出表结构

**`out/events.parquet`**（一次「指数 × 调整」一行）

| 列 | 含义 |
| --- | --- |
| `notice_id` | 中证公告 id（溯源锚点） |
| `index_code` / `index_name` | 000300/000905/000852 |
| `announce_date` | 公告日 |
| `effective_date` | 生效日（正文解析，可能为空） |
| `in_count` / `out_count` | 纳入 / 剔除只数 |
| `source_url` | 附件 URL |

**`out/members.parquet`**（一只股票在一个事件里的进出）

| 列 | 含义 |
| --- | --- |
| `notice_id` / `index_code` / `index_name` | 归属事件 |
| `announce_date` / `effective_date` | 同上 |
| `direction` | `in` 纳入 / `out` 剔除 |
| `code` | 6 位裸代码 |
| `symbol` | 带交易所后缀（`600519.SH`），**回测匹配用这个** |
| `name` | 证券简称（原样，可能含空格） |

`out/alternates.parquet` 为备选名单，**不进主结论**。
