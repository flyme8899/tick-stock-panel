# h 特征回测 · 离线同尺（批次 20261025hfeat）

> **仅供研究，不构成投资建议；执行须人工确认。**
>
> 本文件描述的是**离线纸面回测**。它不接生产链路、不推送、不部署。
> 合入本批次 **不等于** 任何生产能力上线，也**不等于**「ε 门禁 / 可卖老股 /
> 分档成本 / 正式推送闸」被验证通过 —— 这四项**都还没测通**。
>
> **成本侧更新（批次 20261025gcost，5b）**：`cost_bp_tier` 的契约已放开为白名单
> `design_pending` ∪ `{0bp, 5bp, 10bp, 15bp, 20bp}`，非零档位必须配非零金额。
> 这改的是**闸门**，不是**结论**：0 / 0.05% / 0.10% / 0.15% / 0.20% 的成本敏感性
> 跑表属 **5c**，另一拍，**本文件里的任何数字都不是在扣成本之后得到的**。
> 散户双边成本 0.1%–0.15% 只是**假设口径**，不是唯一真实成本。

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
  backtest.py     sell_first / buy_first × 路径 A / B 的纸面配对与分桶
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

## 4. 路径 A 与路径 B：A 是 B 的更严变体，不是平行信号

> **本拍（20261025gtrounds ①）修正过这一节的结论。** 原来写的是「平行，不合并」，
> 现在是「A = B + `require_prior_cross` + 一根延迟」。理由见下。

| | 路径 A | 路径 B |
|---|---|---|
| 定义 | 进入 VWAP ± ε 带，**且**这一带之前价格穿越过均价 | 价格**首次进入** VWAP ± ε 带 |
| 列名 | `signal_intraday_avg_cross_up/down`（边沿）+ `gate_enter_band_up/down`（确认） | `gate_enter_band_up/down` |
| 确认 bar | 边沿之后**第一根**进带 | 进带那一根本身 |
| 成交 | 确认 bar 的**下一根**，价取 `price` | 同左 |
| 出场 | 成交之后第一根越过均价另一侧 | 同左 |

**两者仍不合成一个「门禁通过」的布尔值** —— 分列汇报、各自计数。

### 因果：成交在确认 bar 的下一根，出场只在成交之后找

这是本拍修掉的真 bug，属于**前视（look-ahead）**，不是口径争议。

旧实现有两处超前：

1. `_confirmed_entry_bars` 的返回值是**边沿那根**，而"边沿之后确实进到 ε 带"这件
   事要到更晚才成立 —— 于是进场时刻早于它自己的确认依据。
2. `_simulate_one` 从**边沿那根之后**开始找出场，于是出场可以早于确认。

极端情形：13:00 才确认，却记了一笔 10:05 的"出场"，一轮实际不可能发生的 T 就这么
成交了，还带着正价差进了已实现 KPI。

现在的规则（两条路径同一口径）：

```
边沿/进带 出现在 bar[i]
   → 确认 bar = bar[i]（A 取"边沿之后第一根进带"，B 取"进带那根"）
   → 成交 bar = bar[i+1]，成交价 = bar[i+1]["price"]
   → 出场只在 bar[i+1] 之后找
```

- 确认 bar 是当日最后一根 → 没有下一根可成交 → **这一轮不成立**，只记
  `no_fill_bar`，不建记录、不进分母。它**不是** `data_truncated`（数据没断），
  更不是一轮持仓。
- 成交价用下一根的 `price`，两条路径同一口径 —— 否则 A/B 的差额里会混进"在哪儿
  成交"的差别，而不是信号定义的差别。

> **`price` 是什么：`price := 该 bar 的 close`。**
>
> 这个字段不是本包算的。`build_feature_frame` 里就一句
> `cols = {"price": pl.col("close").cast(pl.Float64)}`
> （`backend/app/strategy/intraday_features.py` **L101**，**已核**）。
> 所以"成交价 = `bar[i+1]["price"]`"的完整读法是：**以下一根 bar 的收盘价成交**。
>
> `price` 这个名字听起来像成交价或中间价，实际是那根 bar 的收盘价。这个口径与
> "下一根走完才成交"的假设是自洽的（收盘价本来就要等那根走完才知道）。写在这里
> 是为了让读数字的人不必再去翻生成处——**不要再把它标成"未核实"**。

### 所以 A 与 B 该怎样读

A 的候选集是 B 的子集（额外要求先穿越，且进场晚一根），因此：

- **A 的样本量必然 ≤ B**。若 A 比 B 多，说明因果又坏了 —— 有测试守着
  （`test_a_never_has_more_rounds_than_b`）。
- **A 与 B 的盈亏差是敏感度开关**，衡量"是否要求先穿越"这一建模选择的敏感度，
  **不是**信号优劣的结论。这一条写进了报告 JSON 的 `path_a_definition`，不只在
  文档里。
- `run_mode_a_backtest(require_prior_cross=False)` 可以把 A 退化成与 B 同一条件，
  用来验证"A/B 的差别是否只来自这个开关"。

> 更早一版（hfeat）曾要求「边沿列」与「首次进带列」**在同一根 bar 同时成立**，
> 而这两个条件在定义上几乎互斥（刚穿越那根只高出零点几 bp），导致路径 A 在结构上
> 恒为空集 —— 不报错、不告警，只是永远 0 个信号。那一版改成"边沿在前、之后进带"，
> 方向是对的，但把**入场点**留在了边沿那根，于是留下了本拍修掉的前视。

## 5. ε 与敏感性

- 主钉：`EPSILON_VALUE_PCT = 0.30`（%），即 0.003 小数
- 敏感性：`0.20 / 0.40 / 0.50`，只出现在 `_sens_` 前缀列里
- 版本串：`design_pin_v1+eps_pct:0.30`，唯一来源是 `constants.design_version()`

**敏感性档位不覆盖主钉**：打开 `with_sensitivity=True` 后，主判定列必须一模一样。
这条有测试直接比对（`test_sensitivity_columns_do_not_override_main_pin`）——
如果敏感性可以改主列，那份报告的主结论就被旁证污染了。

`offline/` 里不允许出现写死的 `0.003` / `0.015` / `0.3` / `60`，由 AST 扫描守着
（不用文本搜索，否则注释里提到这些数字会误伤自己）。

## 6. 结局桶，以及为什么必须给未回摆轮盯市

> **本拍（20261025gtrounds ②③）改过这一节。** 旧的三个桶是
> `completed` / `incomplete` / `abandoned`，而且名字与实际分支**对调**了。

| 结局 | `round_status` | 可计盈亏 | 进已实现 KPI |
|---|---|---|---|
| 两腿都成交 | `completed` | 净价差 | ✅（且必须过 `complete_round()`） |
| 走到 14:57 仍没回摆，**数据是完整的** | `unreverted_at_cutoff` | **盯市盈亏** | ❌ |
| 14:57 之前数据就断了 | `data_truncated` | 不可计 | ❌ |
| 涨跌停锁死、平不掉 | `limit_locked` | 留在分母 | ❌ |
| 过复核期 / 数据作废 | `expired_review` / `void_data` | — | ❌ |

`count_in_realized_kpi` 只在 `completed` 为 True —— 这**不是**本包新加的规则，
而是 `TRoundRecord.__post_init__` 与 `complete_round()` 早就钉死的。本包只是
不绕过它：所有结算都走 `state_machine.complete_round()`。

> 仅断言 `count_in_realized_kpi is False` 是**不够的**（那条由模型层强制，测不出
> 本包自己的汇总逻辑出错）。所以另有行为级测试直接锁汇总**数字**：
> `test_realized_aggregate_excludes_incomplete_rounds`。

### ② 名字与分支曾经对调

旧 docstring 说 `incomplete` 是"没等到出场"、`abandoned` 是"放弃条件"，但代码里
`last.time() > 14:57`（时段走完了）返回的是 `abandoned`，否则返回 `incomplete`。
**docstring 与代码正好相反。**

改名以**代码分支**为准，不以 docstring 为准：

| 旧名 | 代码里的实际含义 | 新名 |
|---|---|---|
| `abandoned` | 走到 14:57 仍没回摆 | `unreverted_at_cutoff` |
| `incomplete` | 14:57 之前数据就断了 | `data_truncated` |

映射表在 `constants.LEGACY_STATUS_ALIASES`，只用于读历史记录。

**顺序很重要**：不先修名字就做盯市，mtm 会算在"数据断了"的桶上 —— 那些轮次根本
没有截点价格，等于按断点那根硬算。所以本拍先 ② 后 ③。

### ③ 为什么必须盯市：只数 completed 就是幸存者偏差

轮次一旦不回摆就落进 `unreverted_at_cutoff`，而这个桶以前**不算任何盈亏**。于是
"已实现"永远是正的 —— 不是策略好，是亏的那一半从账上消失了。

所以未回摆轮按 `ts <= 14:57:00` 的**最后一根** `price` 平掉，得到 `mtm_pnl`。
不用收盘价：14:57 之后是集合竞价前的三分钟，出场判定本来就不看它，盯市也不能看
—— 否则会出现"策略在 14:57 认输，账却按 14:59 的价格算"。

期望值口径：

```
expectancy = (Σ completed 净价差 + Σ 可盯市未回摆轮净价差) / 实际开出的轮次
```

分母 = `completed + unreverted_at_cutoff + data_truncated + limit_locked`，
**不含** `skipped_quota`、**不含** `no_fill_bar`（后者根本没开出持仓）。

> `realized_avg_net_spread` 只数赢的一部分；`expectancy` 才是能拿来谈"赚不赚"的
> 那个数。两个分母不同的平均数混用会得出相反结论 —— 测试里有一个夹具就是
> `realized_avg = +7.00` 而 `expectancy = −4.00`。

### `limit_locked` 留在分母里，不算数据问题

涨跌停锁死时那一轮**真实存在过**，只是没法在那个价位平掉。把它从分母里去掉，
等于把"卡在跌停板上出不来的亏损"从账上抹掉 —— 那正好是最该被看见的一类。所以：

- 它**留在分母**；
- 它**不进** `data_problem`（那不是数据问题，是流动性问题，数据本身是全的）；
- 报告里**不出现**「剔除」这一类的说法。

本拍**没有**涨跌停价数据（特征表只有 `price` / `vwap`），所以：

- 不按百分比阈值反推（那会把普通大跌错记成锁死，而且看起来非常合理、不会有告警）；
- 报告里 `limit_lock_source` 显式写 `data_pending`；
- 桶先建好、计数为 0，等数据接上就能直接填。**不许因为"反正都是 0"删掉它**。

### T+1 约束挂在**卖出腿**上，不挂在"第二腿"上

两种模式的**卖出腿**（`sell_first` 的 leg1 / `buy_first` 的 leg2）份额来源都必须
是 `old_lot`。本包用虚拟底仓做纸面记录，来源标 `old_lot`；标成 `same_day_buy` 时
`complete_round()` 会抛，那一轮进不了 `completed`。

> 旧实现写的是 `if mode == "zheng_t" and leg2_source_lot ...` —— 而 `zheng_t` 的
> leg2 是**买入**腿。结果是：`zheng_t` 的真实卖出腿（leg1）从来没被检查过，
> `buy_first` 的卖出腿也没被检查，而且还有一条测试
> `test_dao_t_allows_same_day_buy_source` 把这个漏洞当成"特性"锁住了。
>
> 一条附带的错误结论「先买后卖只能纸面」也随之下线：`buy_first` 只要卖出腿是
> 老底仓，就是可以真实执行的，不需要打纸面标记。
>
> 模式名同时从 `zheng_t` / `dao_t` 改成 `sell_first` / `buy_first` —— 旧名的
> "正/倒"没有说清哪一腿受 T+1 约束，而这正是护栏装错腿的起因。

### 一日一票一轮：四个格子各是独立世界

`MAX_ROUNDS_PER_DAY = 1`，与 `InMemoryTRoundRegistryStore.upsert()` 同一口径。
**不放宽**。

这条不是"为了不让数字难看"：一天做多轮 T 在纸面回测里几乎总是更好看（多做几次
总能摊到一次成功的），但它要求能在同一个日内反复进出，实盘上滑点和冲击成本会把
这点优势吃光。

回测里有四个格子（`sell_first` / `buy_first` × 路径 A / 路径 B）。**四个格子是四个反事实世界**，
不是一个世界里抢名额的四个人：

- 它们本来就不会同时发生 —— 同一个交易日、同一只票，你只能在先卖后买和先买后卖
  里选一种做；路径 A（进 ε 带且之前穿越过均价）和路径 B（首次进 ε 带）描述的是
  同一段行情的两种择时口径，不是两笔可以并存的仓。
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
> （`sell_first` 优先、A 优先）。那等于把扫描顺序写进了比较：同样的行情，先卖后买
> 的网格总是先拿到名额，先买后卖的格子系统性吃亏。那样的 A∥B 对比**不是**并行的
> 对照，而是"谁排在前面"。所以它不是取舍，是缺陷。
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

其中「分档成本实测」细分一下（批次 20261025gcost）：

| 项 | 状态 |
|---|---|
| 5b 契约放开（白名单 + 金额规则 + 拒非法档位） | **已做**（`constants.COST_BP_TIERS` / `complete_round`） |
| 5c 成本敏感性表（0 / 0.05% / 0.10% / 0.15% / 0.20%） | **未做**，另一拍 |
| offline `_settle` 接实测档 | **未做**，属 5c；仍走 `design_pending` + `0.0` |

所以本批回测的所有净价差仍然是「未扣成本」的口径。闸门能拒非法档位，**不等于**
成本已经被测过 —— 这两件事差一整个 5c。

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
