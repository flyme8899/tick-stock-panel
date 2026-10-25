# h 特征回测 · 离线同尺（批次 20261025hfeat）

> **仅供研究，不构成投资建议；执行须人工确认。**
>
> 本文件描述的是**离线纸面回测**。它不接生产链路、不推送、不部署。
> 合入本批次 **不等于** 任何生产能力上线，也**不等于**「ε 门禁 / 可卖老股 /
> 分档成本 / 正式推送闸」被验证通过 —— 这四项**都还没测通**。

---

## 1. 这一批做了什么

在 `backend/app/t_rounds/offline/` 下建了一个**离线、同尺**的特征派生 + 模式 A
纸面回测评估包。

```
offline/
  __init__.py     公开 API 与六条软提醒的落实对照
  loader.py       同尺判定 → 对齐 → 读导出（含 09:25 竞价守门）
  features.py     调 build_feature_frame + 路径 A 边沿 + 路径 B 进带标记
  gates.py        ε 主钉 + 敏感性分档（`_sens_` 前缀，不进主判定）
  backtest.py     正 / 倒 T × 路径 A / B 的纸面配对与分桶
  report.py       特征表 Parquet + 摘要 JSON + 元数据
```

产物是**两个文件**：`features.parquet`（特征表）与 `summary.json`（摘要 + 元数据）。
不落生产分区，不调 `partition_writer`。

## 2. 「同尺」是这一批的全部前提

分钟导出里 `close` 可能是前复权，而 `amount` 仍是原始成交额。两者混着算
`Σamount / (Σvolume × 100)` 得到的不是那天真实均价，而**从结果上看不出来** ——
它只是让 ε 带的位置偏了几个 bp。

判定方法：**按 `(symbol, 交易日)` 逐日判**，不整份文件给一个结论。

```
ratio = Σamount / (Σvolume × 100 × VWAP_close)
|ratio − 1| ≤ 1e-3  → single
否则               → mixed
```

### 对齐只缩 `amount`

`volume` 是手数，不随复权变；OHLC 属于"价格口径"问题，不在本模块职责内。
**因子必须由调用方通过 `align_hint` 显式给出**，绝不自己按 `1/ratio` 反推 ——
那是循环论证（用结论算因子，再拿因子证明结论）。

合法引子三种形态：

| 形态 | 含义 |
|---|---|
| `{"factor": 0.5}` | 所有混尺日共用一个因子 |
| `{"factor_by_date": {"2026-01-05": 0.5}}` | 逐日给因子（复权因子逐日变动的常见情形） |
| `{"raw_basis": True}` | 以 `amount` 为准，不缩放（价才是被复权过的那一侧） |

缺一天、或没给引子 → **抛 `MixedCaliberError`**，不静默跳过。

## 3. 六条软件提醒的落实位置

| # | 提醒 | 本包怎么落实 | 守它的测试 |
|---|---|---|---|
| 1 | 同尺后再 VWAP | `loader.align_minute_frame()` 是进特征的唯一入口 | `test_unaligned_vwap_differs_from_aligned` |
| 2 | 近端 ratio≈1 不能推断历史日可用 | 逐日给 ratio，报告按日列；不做全局短路 | `test_mixed_caliber_is_detected_per_day` |
| 3 | `sanitize_minute_amount` 会掩盖混尺 | 只在判为 single 之后才进 `build_feature_frame` | `test_mixed_caliber_is_rejected_before_features` |
| 4 | 虚拟底仓须标注 | 报告强制 `virtual_lot_assumption`；记录 `virtual_position_flag=True` | `test_report_contains_required_metadata` |
| 5 | 默认不自动部署 | 本包纯离线；合入 ≠ 部署 | 本文件 + 包 docstring |
| 6 | 竞价 / 估值健康 / 白名单锁定 / 计划表 17–19 暂缓 | 零实现；`AUCTION_IMPERSONATION_FORBIDDEN` 有测试守着 | `test_auction_impersonation_is_forbidden` |

### 第 3 条为什么值得单独说

`build_feature_frame` 内部会调 `sanitize_minute_amount`。那个函数在混尺时会把
"对不上收盘价"的额改写成 `close × volume × 100` —— **于是脏数据被"修好"了，
ratio 变回 1，问题消失得无影无踪**。

所以顺序必须是：先判口径 → 对齐 → 再进 `build_feature_frame`。反过来做，
你永远发现不了数据是混尺的。

## 4. 路径 A 与路径 B：平行，不合并

| | 路径 A | 路径 B |
|---|---|---|
| 定义 | 价格**穿越** VWAP 的边沿（`avg_cross_up/down`） | 价格**首次进入** VWAP ± ε 带 |
| 列名 | `signal_intraday_avg_cross_up/down` | `gate_enter_band_up/down` |
| 入场点 | **边沿那根**（进带只作确认） | 首次进带那根 |
| 语义 | "价格动了，且确实离开了均价" | "价格到了有价差可做的位置" |

**两者绝不合成一个「门禁通过」的布尔值。** 合成的那一刻，就等于放弃了
"边沿型信号"与"阈值型信号"的区分 —— 而这两类信号在实盘上的滑点表现完全不同。

### 路径 A 的确认是因果的，不是同根的

这一点本批次修过一个真 bug，值得写下来。

早先的实现要求「边沿列」与「首次进带列」**在同一根 bar 同时成立**。而这两个条件
在定义上几乎互斥：

- `cross_up` 成立那根，price 只比 vwap 高一点点（它刚越过去），够不到 +0.3% 带；
- 等 price 真到了 +0.3% 带，`cross_up` 早就过去了。

结果：**路径 A 在结构上恒为空集**。它不报错、不告警，只是永远 0 个信号 ——
最容易被误读成"行情里确实没这种机会"。

正确语义是：**边沿那根在先，当日之内之后某一根进到 ε 带，这次边沿才算数**。
实现见 `backtest._confirmed_entry_bar()`。因果方向必须是这样 —— 边沿**之前**
就进过带不算确认，因为那时价格还没穿越，那根带是上一段行情的尾巴。

## 5. ε 与敏感性

- 主钉：`EPSILON_VALUE_PCT = 0.30`（%），即 0.003 小数
- 敏感性：`0.20 / 0.40 / 0.50`，只出现在 `_sens_` 前缀列里
- 版本串：`design_pin_v1+eps_pct:0.30`，唯一来源是 `constants.design_version()`

**敏感性档位不覆盖主钉**：打开 `with_sensitivity=True` 后，主判定列必须一模一样。
这条有测试直接比对（`test_sensitivity_columns_do_not_override_main_pin`）——
如果敏感性可以改主列，那份报告的主结论就被旁证污染了。

`offline/` 里不允许出现写死的 `0.003` / `0.015` / `0.3` / `60`，由 AST 扫描守着
（不用文本搜索，否则注释里提到这些数字会误伤自己）。

## 6. 三个结局桶，以及为什么只能这样分

| 结局 | `round_status` | 进已实现 KPI |
|---|---|---|
| 两腿都成交 | `completed` | ✅（且必须过 `complete_round()`） |
| 入场了但当日收盘前没等到出场 | `incomplete` | ❌ |
| 尾盘未等到出场，主动放弃 | `abandoned` | ❌ |

`count_in_realized_kpi` 只在 `completed` 为 True —— 这**不是**本包新加的规则，
而是 `TRoundRecord.__post_init__` 与 `complete_round()` 早就钉死的。本包只是
不绕过它：所有结算都走 `state_machine.complete_round()`。

> 注意：仅断言 `count_in_realized_kpi is False` 是**不够的**（那条由模型层强制，
> 测不出本包自己的汇总逻辑出错）。所以另外有一条行为级测试直接锁汇总**数字**：
> `test_realized_aggregate_excludes_incomplete_rounds`。

### 正 T 的 leg2 必须是老底仓

正 T 是「先卖后买」，卖出的份额必须是当日之前就持有的。本包用虚拟底仓做纸面记录，
来源标 `old_lot`。标成 `same_day_buy` 时 `complete_round()` 会抛，那一轮进不了
`completed`。

### 一日一票一轮：四个格子各是独立世界

`MAX_ROUNDS_PER_DAY = 1`，与 `InMemoryTRoundRegistryStore.upsert()` 同一口径。
**不放宽**。

这条不是"为了不让数字难看"：一天做多轮 T 在纸面回测里几乎总是更好看（多做几次
总能摊到一次成功的），但它要求能在同一个日内反复进出，实盘上滑点和冲击成本会把
这点优势吃光。

回测里有四个格子（正 T / 倒 T × 路径 A / 路径 B）。**四个格子是四个反事实世界**，
不是一个世界里抢名额的四个人：

- 它们本来就不会同时发生 —— 同一个交易日、同一只票，你只能在正 T 和倒 T 里选一
  种做；路径 A（价格穿 vwap 的边沿那根入场）和路径 B（首次进 ε 带入场的）描述的
  是同一段行情的两种择时口径，不是两笔可以并存的仓。
- 所以四个世界**各自独立计数**：每个世界内部仍然是「同票同日只做一轮」，世界之间
  不互相排队。

这样做的两个后果，都得讲清楚：

1. **硬规则没有放宽**。同一个世界里（同模式、同路径、同票、同日）出现第二个候选，
   照旧被拒，记 `skipped_quota`（它**不是**一轮 T，只计数）。也就是说"四格独立"
   并没有变成"一天做四轮 T" —— 任何一个世界的样本里，一天最多仍是一轮。
2. **不同世界之间不再互相排队**。汇总（`summary.groups`）按 `(mode, path)` 分列，
   从不合并。某个格子的 `skipped_quota` 只可能来自它自己世界里的重复候选，绝不
   可能来自"被别的格子抢了名额"。

> 早先的实现让四个格子共享 `(symbol, trade_date)` 这一个名额，配一个写死的顺序
> （正 T 优先、A 优先）。那等于把扫描顺序写进了比较：同样的行情，正 T 的网格总是
> 先拿到名额，倒 T 的格子系统性吃亏。那样的 A∥B、正∥倒 T 对比**不是**并行的对照，
> 而是"谁排在前面"。所以它不是取舍，是缺陷。
>
> `skipped_quota` 的语义也随之收窄：现在它**只**表示「同一世界、同一路径、同日
> 多个候选撞配额」，不再表示"跨格子竞争落败"。

三条测试分别锁这三件事：
`test_groups_do_not_compete_for_the_same_slot`（四个格子都能各自成轮，
`skipped_quota` 全 0）、`test_skipped_quota_counts_only_within_the_same_world`
（同一世界两个候选 → 第二个被记 `skipped_quota`）、
`test_max_rounds_per_day_is_respected_per_world`（世界内仍是一日一轮）。

## 7. 不许 09:30 冒充 09:25

`assert_no_market_open_impersonation()` 拒绝任何早于 `MINUTE_EXPORT_EARLIEST`
（`"09:30"`）的 bar。如果导出里真有 09:25 的 bar，那不是连续竞价的成交，不能当
正常 bar 用。

实现细节（踩过的坑）：**不能用 `dt.hour() * 60 + dt.minute()` 做比较**。
polars 的 `dt.hour()` / `dt.minute()` 返回 `Int8`，`9*60 = 540` 在 Int8 里回绕成
`28`，于是 09:30 被误判成"早于 09:30"，整批数据被拒；尾盘的 `14:57` 更会溢出成
负数。改用零填充的 `"HH:MM"` 字符串比较（定长字符串的字典序 == 时间序）。

## 8. 虚拟底仓假设

> 每一轮均建在纸面虚拟底仓上：假设开盘即持有 `LOT_SIZE`（100 股）且当日之前买入
> （可卖），`leg2_source_lot=old_lot`；未建模真实持仓 / 冻结 / T+1 可用量，
> **不得据此宣称实盘可卖已通**。

这句话同时写进报告 JSON 的 `virtual_lot_assumption` 字段。它不是免责话术，而是
这一批结论的**适用边界**。

## 9. 隔离性：对生产链路零触碰

| 约束 | 守它的手段 |
|---|---|
| 不 import `buy_sell_signals` | AST 扫 `Import` / `ImportFrom` |
| 不碰 `BUY_SELL_SIGNALS_PERSIST/PUSH/DINGTALK` | 扫标识符 + 字符串常量 |
| 不引推送传输层（SSE / 钉钉 / 飞书 / httpx） | 扫 import |
| 不碰 lifespan / `daily_pipeline` / `partition_writer` / FastAPI | 扫 import 与调用 |
| 生产模块不反向 import `offline` | 扫 `t_rounds/*.py` |
| 不用动态导入绕过检查 | 禁 `__import__` / `import_module` |

静态扫描**不用裸子串**：AST 只比对导入节点与属性访问，注释里写「不接 xxx」
不会误判自己。这条纪律来自 gp 批次的教训。

## 10. 可复现性

同一帧 + 同一 ε + 同一 `prev_close` → 同一特征表。本包不做随机、不做采样、
不依赖系统时间。

浮点比较容差：**1e-9 相对**。测试里逐列逐值比（`test_feature_derivation_is_reproducible`）。

## 11. 明确不做

全量 walk-forward 定稿 · ε 升版重钉 · 分档成本实测 · Regime 产品标定 ·
正式推送闸 · 竞价 / 放量事件 · 估值 / 健康实装 · 白名单锁定 · 计划表 17–19 ·
MCP 交易 · **任何生产 env 改动**。

## 12. 跑测试

```bash
cd backend
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  tests/test_t_rounds_offline_loader.py \
  tests/test_t_rounds_offline_features.py \
  tests/test_t_rounds_offline_backtest.py \
  tests/test_t_rounds_offline_isolation.py \
  -p no:pylama -p asyncio -q
```

---

**仅供研究，不构成投资建议；执行须人工确认。**
本报告仅为离线同尺特征回测；ε 门禁、可卖老股实盘、分档成本、正式推送闸均未测通。
