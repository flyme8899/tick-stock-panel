# gtja191 实验组

国泰君安 191 个短周期价量 Alpha 因子（业内通称 GTJA191）的首批移植。公式来自公开研究报告《基于短周期价量特征的多因子选股体系》，**公开发布日期 2017-06-15**。

网络上部分转写把这个年份写成「2014」，那是转写误差，不作为权威。本仓库一律以 2017-06-15 为准。来源与合规说明见 `THIRD_PARTY_NOTICES.md` 的「国君191 / gtja191」一节。

公式写在 `backend/app/factors/gtja191.py`。**TSP 不依赖 Vibe-Trading 或 DolphinDB，也不 vendor 任何第三方源码树。**

## 默认不暴露

分组名 `GTJA191（实验）`，id 前缀 `gtja_`，只覆盖股票，`stability="experimental"`，tags 是 `("gtja191", "experimental")`。

`all_factors()`、`factor_columns_view()`、`/api/factors`、信号白名单、AI 提示词、工具目录**默认都不返回这组**。因子库和因子发现要打开「显示实验组」。挖掘候选默认也不含这组。

> **登记 ≠ 投产。** 注册进注册表只说明"这套公式能被 TSP 算出来"，不说明它有效，更不说明它该进白名单。本批没有改动任何策略默认值、没有改信号白名单、没有动挖掘配置。

## 首批：13 个可算，4 个 blocked

首批清单 16 个 ID，全部只用 OHLCV + amount。其中 13 个可算登记，落在任务书要求的 10–20 区间。

| ID | 名称 | 报告原文 |
|---|---|---|
| `gtja_2` | 影线相对位置-一阶差分 | `-1 * DELTA(((CLOSE-LOW)-(HIGH-CLOSE))/(HIGH-LOW), 1)` |
| `gtja_3` | 收盘触极值次数(6日) | `SUM((CLOSE==HIGH?1:0), 6) + SUM((CLOSE==LOW?1:0), 6)` |
| `gtja_5` | 量价相关-成交额秩-窗口极大 | `-1 * TSMAX(RANK(CORR(RANK(VOLUME), RANK(HIGH), 5)), 3)` |
| `gtja_6` | 开盘收盘加权-截面秩差分 | `-1 * RANK(DELTA((0.85*OPEN+0.15*HIGH), 4))` |
| `gtja_11` | 影线比-成交量加权-秩 | `(RANK(SUM(...*VOLUME, 6)) + 1) * 100`（分式改写） |
| `gtja_14` | 收盘变动-最高价量相关变动 | `CLOSE - DELAY(CLOSE, 5) + DELTA(CORR(HIGH, VOLUME, 10), 1)` |
| `gtja_18` | 收盘动量-成交量秩 | `CLOSE / DELAY(CLOSE, 5) * RANK(VOLUME)` |
| `gtja_19` | 收盘涨跌符号(6日) | 嵌套条件求和 |
| `gtja_20` | 收盘6日变动率 | `(CLOSE - DELAY(CLOSE, 6)) / DELAY(CLOSE, 6) * 100` |
| `gtja_34` | 收盘均值比(12日) | `MEAN(CLOSE, 12) / CLOSE` |
| `gtja_37` | 开盘变动秩-成交量秩比 | `(-1*RANK(DELTA(OPEN, 1))) * (RANK(SUM(VOLUME, 15)) + 1)` |
| `gtja_49` | 中间价变动-量比(12日) | `SUM(...*DELTA(VOLUME,1), 12) / SUM(VOLUME, 12)` |
| `gtja_54` | 收盘相对窗口高点(10日) | `(-1*(TSMAX(CLOSE,10)-CLOSE)) / ((TSMAX(CLOSE,10)+CLOSE)*CLOSE)` |

完整原文与逐条 TSP 映射说明见模块里的 `GTJA191_FORMULAS`。

### blocked:ops 的 4 个

**不登记假实现。** 原因分两类：

| ID | 类别 | 原因 |
|---|---|---|
| `gtja_15` | 算子缺失 | 含 `SMA(CLOSE-OPEN, 3, 1)` 递推平滑 |
| `gtja_55` | 算子缺失 | 分母含 `SMA(..., 6, 1)` 递推平滑 |
| `gtja_32` | 算子冲突 | `SUM(RANK(CORR(HIGH, VOLUME, 3)), 3)`：截面秩再进时序窗口 |
| `gtja_38` | 算子冲突 | `SUM(RANK(SUM(CLOSE, 20)), 5)`：同上 |

**算子缺失**：`SMA(x, n, m=1)` 是国泰君安递推平滑，递推式 `y_t = (m*x_t + (n-m)*y_{t-1}) / n` 有无限记忆。`ts_mean` / `decay_linear` 都顶不上。

**算子冲突**：TSP DSL 明确拒绝「截面算子嵌在时序窗口内」（错误码 `E009`）。时序窗口是逐股票的历史轴，截面秩依赖当日全市场样本，两者不是同一个轴，硬算出来的数在不同截面上不可比。

> `gtja_32` 去掉外层 `ts_sum` 虽然能编译，但那是**另一个因子**，不登记。`gtja_38` 没有忠实的 TSP 写法。

**一个容易踩的坑**：这两条**编译期不会报错** —— `compile_formula_cached` 把 `E009` 收在 `errors` 里而不抛，`frame_transform` 为 `None`。结果是编译全绿、运行时安静地少两列。本批因此加了测例 `test_computable_ids_actually_materialize`，逐条断言"真的能出值"，而不是只断言 `compiled.ok`。

## TSP 映射

### RANK 用 `ts_pctrank`，不用 `rank`

报告里的 `RANK(A)` 是**截面百分位秩**。TSP 的 `rank(x)` 是**截面 z-score**，两者不能互相顶替。

本批一律用 `ts_pctrank(x, n)`：它返回 (0, 1] 的窗口内百分位，与截面 `pct_rank` 是严格单调变换。而 TSP 的 Rank IC 用 `pl.corr(rank(a), rank(b))`（见 `backtest/factor.py` 的 `_calc_ic`），秩相关对单调变换不变，所以 **IC 数值不受这个选择影响**。

二值因子（`gtja_2` / `gtja_3` / `gtja_5` / `gtja_6`）的"最大值/最小值"判定也正是靠这个单调性保持一致。

### WMA 与 decay_linear

报告 `WMA(x, d)` 的权重是 `0.9^i`；TSP 的 `decay_linear(x, d)` 权重是线性 `d, d-1, ..., 1`。**两者不是同一个函数。**

**本批 13 条可算式里没有任何一条用到 WMA**，所以也没有任何一条需要 `decay_linear` 顶替，更没有静默近似。唯一涉及均值的是 `gtja_34` 的 `MEAN(CLOSE, 12)`，报告原文就是简单均值，对应 `ts_mean`，是**一一对应**而非映射。

这条规矩仍然写在这里：下批扩表时，谁想用 `decay_linear` 顶 WMA，必须在 `GtjaFormula.tsp_mapping` 里标「TSP 映射」并写明权重差异。测例 `test_no_decay_linear_substitution_in_this_batch` 会强制这一点。

## NaN 与 null

TSP 的 `ts_corr` / `ts_cov` 直接落到 `pl.rolling_corr` / `pl.rolling_cov`，窗口内任一输入**方差为 0** 时输出 **`NaN` 而不是 `null`**。

本批 `gtja_5`（量价相关）与 `gtja_14`（最高价量相关变动）直接依赖相关系数 —— 一只票连续 5 天成交额秩全等、或最高价单调时，相关系数无定义，会留下 NaN。

NaN 和 null 在 TSP 里待遇不同：`_calc_ic` 用 `is_not_null()` 过滤，**NaN 会穿过过滤**进入 `pl.corr`，把当天的 Rank IC 变成 NaN；null 才是"这条样本无法定义"。

**处置**：在唯一的物化汇合点 `strategy.scoring.materialize_scoring_columns` 里，对 `gtja_` 前缀的列做 `fill_nan(None)`。这是全仓因子计算都经过的函数，改一处即全路径生效。**只对 gtja 前缀生效**，alpha158 等既有使用者的数值口径一字未动。

不用 DSL 表达式的 `min(x, 1.0)` 包一层：DSL 的 `min` / `max` 是 `pl.min_horizontal` / `pl.max_horizontal`，它们**把 NaN 当缺失值忽略**、直接返回另一侧操作数 —— 实测会把一个无定义的相关系数变成 **1.0**，那是伪造数据。

## 截面因子不能按股票分块

`gtja_6` / `gtja_11` / `gtja_37` 含截面算子 `rank(x)`，分母是**当天的股票数**。

按股票分块会把同一天的截面切成几块，每块的 rank 分母都不一样，**同一只股票在不同分块大小下会算出不同的值**（实测 `gtja_6` 在 chunk=3 与 chunk=100 下差一倍）。这不是精度问题，是算错。

所以 `compute_gtja191` 检测到本族存在截面式时**直接不分块**，整帧一次算完。`cross_sectional_ids()` 返回需要全市场一起算的 ID 清单。回测路径本来就不分块，不受影响。

## N2 标签映射与阈值

每个因子跑完检验后会被贴一个标签。标签逻辑在 `backend/app/factors/gtja191_labels.py`，是一个**薄映射纯函数**：输入 `FactorBatchItem`（回测已经算好的结果）+ IC 有效日数 + 费后多空收益 + 可选 `blocked_reason`，输出一个标签。

**映射内不重算 IC。** 只读 `item.ic_mean` / `item.ir` / `item.error` 这些已有字段。重算会让同一个因子在不同调用点得到不同标签 —— 那比没有标签更糟。

### 阈值表

| 常量 | 值 | 含义 |
|---|---|---|
| `MIN_IC_DATES` | **30** | IC 有效交易日下限。低于此**一律 `underpowered`，不得判 `alive`** |
| `MIN_ABS_IC` | **0.02** | `\|IC\|` 地板。低于此判 `dead`，即使费后碰巧为正 |
| `MIN_NET_LONG_SHORT` | **0** | 费后多空累计收益下限（`> 0` 才过门），必须是按 18.2bp 重扣后的净额 |
| `GTJA191_COST_BP_ROUNDTRIP` | **18.2 bp** | 成本闸口径（N5 成本锚） |

### 标签与优先级

优先级从高到低：`blocked:*` > `error` > `degenerate` > `underpowered` > `reversed` > `dead` / `alive`。

| 标签 | 触发条件 | 说明 |
|---|---|---|
| `blocked:ops` | `blocked_reason` 非空 | 算子缺失/冲突，根本没算。优先级高于一切 |
| `error` | `item.error` 非空，或 `ic_mean` 为 `None` 而 IC 日数 > 0 | 算了但抛错，或结果自相矛盾 |
| `degenerate` | `ic_mean` 为 `None` 且 IC 日数 == 0 | 因子值截面上恒定，Rank IC 无定义 |
| `underpowered` | `n_ic_dates < 30` | 样本太薄。**压在 reversed/dead/alive 之前** |
| `reversed` | `\|IC\| ≥ 0.02` 且费后多空符号与 IC 相反 | 方向反了：因子有用但用反了 |
| `dead` | `\|IC\| < 0.02`，或无分层结果，或费后多空 ≤ 0 | 因子没用 / 费后不成立 |
| `alive` | `\|IC\| ≥ 0.02` 且费后多空 > 0 | 过了全部门槛 |

**为什么 `degenerate` 单独一类。** 因子值在某天截面上全相等（或整个区间恒定）时，Rank IC 的分母是 0，**IC 无定义**，`ic_mean` 是 `None`、有效 IC 日数是 0。这既不是 `error`（代码没出错、算子也支持），也不是 `dead`（一次都没被真正测过），更不是 `underpowered`（不是样本少，是样本全是同一个值）。归成 `error` 会让人去查一个不存在的 bug，归成 `dead` 会误杀。**这是一种测量失败，处置是修因子或换数据，不是退役。**

> 合成面板上实测踩到过：`gtja_3`（收盘触及极值次数）在随机游走数据上恒为 `0.0`，`gtja_11` 恒为 `100.0`，两个都落进 `degenerate`。真实数据上应该不会这样，但标签必须能如实说出"我测不了"。

**样本量的口径陷阱。** 判 `underpowered` 用的是 **`len(result.ic_series)`**（真正算出 IC 的天数），**不是** `result.n_dates`。后者是 `panel["date"].n_unique()`，即面板的交易日数，跟 IC 有没有算出来无关 —— 因子在截面上恒定时 Rank IC 全是 NaN，`n_dates` 依然等于 287，`ic_mean` 却是 `None`。拿 `n_dates` 当 IC 日数会让 V8 那条线彻底失效：29 天的样本被读成 287 天，一路判成 `alive`。测例 `test_eval_script_uses_len_ic_series_not_n_dates` 扫 AST 守住这一处。

**为什么 `underpowered` 压在 `alive` 之前。** 29 个 IC 日、IC=0.15、费后多空 +50%，看起来什么都过门 —— 但 29 天里的 IC 0.15 可能只是运气。判 `alive` 是拿噪声当信号；判 `dead` 是拿噪声当结论。两种都错，唯一正确的处置是承认样本不够。这就是 V8 测例守的那条线。

**为什么 `reversed` 和 `dead` 必须分开。** `dead` 是"因子没用"，该退役；`reversed` 是"因子有用但方向反了"，该改方向重验。判据是**符号相反**：IC 显著为正而费后多空为负（或反之）。两者处置完全不同，合并会让一个可能有用的因子被当废物扔掉。

**一侧贴近 0 时不判 `reversed`。** `|IC| < 0.02` 或费后多空恰好为 0 时，符号本身是噪声，拿它判"方向反了"会误伤。

### 成本口径错了必须抛错

`FactorBatchItem.long_short_return` **已经扣过费**，扣的是 `FactorConfig` 默认值（来回 **14bp**），不是 18.2bp。直接拿这个字段过 18.2bp 的门，等于每天少扣 4.2bp，结果系统性偏乐观 —— 而且**看不出错**，因为数字仍然合理。

所以 `label_of()` 强制要求调用方传入 `cost_bp`，且必须等于 18.2。传 14 进来直接 `ValueError`。

脚本里的做法是**重跑一遍分层多空**（`FactorConfig` 换成成本锚参数），拿这次干净的 `long_short_stats["total_return"]` 过门。IC 不重算 —— 成本只影响分层收益，不影响 Rank IC。

### 下线候选：只提示，不注销

标签 ∈ `{dead, reversed}` 且按 18.2bp 未过门 → 打印为「下线候选」。

- **只是提示。** 脚本不调用、也不允许调用 `unregister_factor`，不改注册表；
- 退役/下线要人工拍板，走独立的变更流程；
- `underpowered` / `degenerate` / `error` / `blocked:*` **不是**候选 —— 那是缺证据，不是有反证。

测例 `test_eval_script_never_unregisters` 扫脚本 AST，任何 `unregister_factor` 都会让测试红。

## 成本口径

任务书成本锚是**来回 18.2bp**。它和 `FactorBatchConfig` 的历史默认值（`fees_pct=0.0002`、`slippage_bps=5.0`，来回约 14bp）**不是一回事**：

| 口径 | 来回成本 | 用在哪 |
|---|---|---|
| 任务书成本锚 | 18.2 bp | 任何"扣费后还行不行"的判断 |
| 成本闸（`cost_gate_cost_bp()`） | 18.2 bp | 同上 |
| IC 表默认配置（`ic_table_cost_bp()`） | 14.0 bp | `FactorBatchConfig` 历史默认值 |

脚本不偷偷改默认值来凑 18.2，而是把两个数都打出来。**所有 IC 都是未扣费的 Rank IC**，成本只在"扣费后还能不能做"的判断里出现，不要拿 IC 直接和成本比较。扣费后收益看分层多空。

## 自评与对照

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/eval_gtja191.py
```

脚本做四件事：

1. **自评** —— 13 个可算 ID 逐个跑 TSP 因子检验，给 Rank IC / ICIR / 换手 / 覆盖 / 天数；
2. **对照** —— 同区间、同截面门槛跑一遍 alpha158（158 个），给出 gtja191 在 alpha158 `|IC|` 分布里的分位；
3. **成本口径** —— 显式打印两个来回成本并说明不相等；
4. **标签与下线候选** —— 逐个贴 N2 标签（见上节），`{dead, reversed}` 的列为「下线候选」**仅打印**。

自评表格最后多一列「标签」。成本闸那一遍只对 gtja191 跑，alpha158 对照不跑第二遍 —— 对照只需要 `|IC|` 分位，不需要给 158 个因子各贴标签，也省一半时间。

常用参数：

```bash
# 只跑 gtja191, 不做对照
... eval_gtja191.py --no-baseline

# 指定区间与样本过滤
... eval_gtja191.py --end 2026-09-30 --exclude-limit --exclude-st --min-listed-days 60
```

没有 enriched 分区时脚本**直接说明并退出（退出码 2），不写出任何 IC 数值**。截面过薄时同样用退出码或显式警告说明，不退化成"有个数就行"。

**对照不是为了宣布谁赢。** 分位是「同一区间、同一截面门槛下的横向位置」，不是统计显著性。alpha158 内部有大量高度相关的因子，分布本身偏胖，分位 50% 不代表"有用"。

不要在正在提供服务的 tsp 容器里跑完整评估，会把服务拖重启。另开一个容器，挂上同一份数据卷，例如 `docker run --rm --volumes-from <tsp> ...`。

## 一次实跑记录

本节只记录脚本在**合成数据**上的行为验证，**不是**真实 A 股的 IC 结论。

维护者本机没有 A 股 enriched 分区，`scripts/eval_gtja191.py` 走的是"无数据"分支（退出码 2）。为了验证脚本后半段不是死代码，用确定性合成面板（30 只 × 70 个交易日，`kline_daily_enriched` 格式）跑通了完整流程，包括 alpha158 对照的 158 个因子。

合成面板上 gtja191 与 alpha158 的 IC 数值**没有任何金融含义**，已经丢弃，不作为结论。

## 本批明确不做

- 不改三 env（`DSA_REVIEW_*` / `BUY_SELL_SIGNALS_*` / 其它实盘开关）
- 不翻 `TRIGGERING`、不真触发、不影子调度、不加 crontab
- 不 fabricate IC 或频率数值
- 不假绿（skip / xfail / continue-on-error）
- 不把 gtja191 塞进默认白名单与挖掘候选
- 不回退 #207–#232
- 不自行 merge、不部署
