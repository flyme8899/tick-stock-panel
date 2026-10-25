# 做 T 轮次骨架（批次 20261024h）

> **仅供研究，不构成投资建议；执行须人工确认。**

本文说明 `backend/app/t_rounds/` 这个包**做了什么**、**明确不做什么**，以及为什么几条
硬规则写进了构造函数而不是文档。

## 一句话

实现了做 T 轮次的**记录字段 + 状态机 + 净价差结算 + 轮次上限 + 持仓三态标签**，
Store 用**内存实现，默认不落盘**，成本档位按白名单 `design_pending` ∪ `{0bp, 5bp,
10bp, 15bp, 20bp}` 登记（批次 20261025gcost 放开 5b 契约）。

**没有**接任何 HTTP 路由、**没有**接日管道、**没有**接推送、**没有**做分钟评估。

## 包结构

| 文件 | 职责 |
|---|---|
| `constants.py` | 命名空间 / ε / 轮次上限 / 模式与状态枚举 / 六条软提醒常量 |
| `models.py` | `TRoundRecord` + `LegState`（frozen dataclass），硬规则在 `__post_init__` |
| `state_machine.py` | 转移表、`complete_round()` 结算、`check_round_quota()` 轮次上限 |
| `store.py` | `TRoundRegistryStore` 协议 + 内存实现 + 默认不可用的磁盘空壳 |
| `position_tags.py` | `tag_position()`：老底仓 → `executable`；当日买入 → `tomorrow_ref`；否则 → `observe_only` |
| `disclaimer.py` | 合规脚注常量 + 恒 `True` 的 `NOT_INVESTMENT_ADVICE` / `MCP_TRADE_FORBIDDEN` |

## 三条硬规则，写在构造函数里

文档里的「应当」没人执行。这三条一旦被违反，**从结果上几乎看不出来**，所以直接抛错。

### 1. 非 `completed` 不得进已实现 KPI

```python
if self.round_status not in C.REALIZED_KPI_ELIGIBLE_STATUS:
    _require(self.count_in_realized_kpi is False, ...)
    _require(self.net_spread_realized is False, ...)
```

`REALIZED_KPI_ELIGIBLE_STATUS = frozenset({"completed"})` 写成集合而不是 if 链：
「哪些状态能进 KPI」只有一个定义处，将来加状态不改这里就不会误入。

一个 `incomplete` 的轮次如果把腿 1 的卖出价当成已实现收益，胜率统计会凭空变好——
因为亏的那一轮被算成"没做成"，赚的那半腿被算成"做成了"。

### 2. 正 T 第二腿必须是老底仓

```python
if self.mode == "zheng_t" and self.leg2_source_lot is not None:
    _require(self.leg2_source_lot == "old_lot", ...)
```

正 T 是「先卖后买」。卖出的必须是**当日之前就持有**的份额；买回的若是当日买入的票，
那就是当日买入当日卖出，T+1 违规。

这条在三处各拦一次（刻意的重复，任何一处被绕过都还有下一处）：

1. `TRoundRecord.__post_init__`
2. `state_machine.open_leg2(...)`
3. `state_machine.complete_round(...)`

### 3. `completed` 无出边，`void_data` 不占轮次

- `completed` 是**无出边终态**。这一点与任务书「`* → void_data`」的字面语法冲突，
  我选了更保守的解释：已实现 KPI 一旦计数，就不该被事后改写。要作废只能**重新构造**
  一条 `void_data` 记录。
- `void_data` **不消耗**轮次名额（那一轮什么都没发生）。其余真开过腿的状态
  （`open_leg1` / `open_leg2` / `completed` / `incomplete` / `abandoned` / `expired_review`）
  都消耗——否则「一轮失败就重来」会让 `MAX_ROUNDS_PER_DAY = 1` 形同虚设。

## 净价差方向：正 T 与倒 T 相反

```python
gross = (leg1_price - leg2_price) * qty if mode == "zheng_t" \
        else (leg2_price - leg1_price) * qty
```

- 正 T（先卖后买）：卖得贵、买得便宜才赚 → `leg1 - leg2`
- 倒 T（先买后卖）：买得便宜、卖得贵才赚 → `leg2 - leg1`

写错方向的话全部符号反过来，一个**永远亏**的策略会看起来很诚实。测试里两个方向各断言
一次符号，不只测绝对值。

`net = gross - cost.cost_amount`。`cost_bp_tier` 取值由 `constants.COST_BP_TIERS`
单一定义（批次 20261025gcost 放开 5b 契约），白名单之外一律抛错：

| 档位 | 含义 | `cost_amount` |
|---|---|---|
| `design_pending` | 占位，**不**宣称已按费率扣成本 | `>= 0`（含 0） |
| `0bp` | 显式零成本 | **必须 == 0** |
| `5bp` / `10bp` / `15bp` / `20bp` | 按该档扣 | **必须 > 0** |

两条容易忽略的地方：

- **禁 silent 0**：非零档位配 `cost_amount=0` 直接拒。允许它的话，一份「按 10bp
  扣成本」的报告与「根本没扣成本」的报告数字完全一致，而前者会被当成已经考虑过
  成本的结论。错误不在数字里，在标签里。
- **换算不在闸门里**：闸门只验「档位合法 + 金额不矛盾」，扣的是绝对额。「费率 ×
  名义金额」的换算与名义价口径留在调用方（离线跑表），免得闸门顺手替人选了口径。

## ε = 0.30%，locked 进 version

`EPSILON_VALUE_PCT = 0.30` 指**相对均价 0.30%**（≈30 bp），来自 PARAM_NAIL `design_pin_v1`。

`design_version()` 拼出 `design_pin_v1+eps_pct:0.30`。一条记录离开它诞生时的代码之后，
仍然能读出"它是在哪个 ε 下判定的"。事后补这个信息是补不回来的。

**本批次只落常量与字段，不跑分钟评估**，所以不涉及「用什么均价」的问题——但注释已经
提前写死了（见下）。

## 六条软提醒

| # | 提醒 | 落地位置 |
|---|---|---|
| 1 | **同尺后再 VWAP** | `VWAP_SAME_BASIS_NOTE`：价格与成交额须先对齐到同一复权口径；优先 `.raw_basis` / `price_basis` |
| 2 | **禁拷提醒常量** | `FORBIDDEN_NAMES` + 测试逐项扫**标识符**（AST，不是全文匹配） |
| 3 | **冷却分列** | 见下节 |
| 4 | **virtual 占位** | `virtual_position_flag` 可为 `True`；`to_dict()` 恒输出，不宣称实盘可卖老股已通 |
| 5 | **研究窗 ≥2025-07-25 且缺日跳过** | `RESEARCH_WINDOW_START`：缺数据填 `null`，**禁止回退到最近分区**，`null ≠ 0` |
| 6 | **禁冒充竞价** | `AUCTION_IMPERSONATION_FORBIDDEN = True`；`MINUTE_EXPORT_EARLIEST = "09:30"` |

### 关于第 2 条：为什么用 AST 而不是全文匹配

`push_dedupe_cooldown_until` 这个名字必须**允许**出现在 docstring 的说明文字里（比如
本文件就是在解释"为什么不用它"）。全文匹配会逼人把说明删掉，最后谁也不知道为什么。
所以测试用 `ast` 只扫**标识符**：`ast.Name` / `ast.Attribute` / `ast.arg` / `ast.keyword`
以及定义名。

## 三种"限制重复"的东西，名字必须分开

| 名字 | 语义 | 归属 |
|---|---|---|
| `T_COOLDOWN_MIN = 60` | 同一只票一天内不再出一**轮**做 T（提醒层说法，分钟级） | 提醒层 |
| `push_dedupe_cooldown_until` | 同一条**信号**别重复播报 | g 提醒层去重 |
| `max_rounds_per_day` / `rounds_consumed_on_trade_date` | 单日每票最多做几**轮 T**（次/日） | 本包 |

`T_COOLDOWN_MIN` 与 `max_rounds_per_day=1` 语义上像是同一件事，但**不是一个东西**：
前者是提醒层的节流参数（分钟），后者是轮次计数（次/日）。硬编码或 import 前者的数值，
会让「调整推送频率」悄悄改掉「一天能做几轮 T」。

**禁止** import / 拷贝 `VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`。

## 持仓三态：只影响能不能执行，不影响进不进候选

```python
tag_position(old_lot_qty_available, same_day_buy_qty=None)
```

| 条件 | 标签 |
|---|---|
| 老底仓 > 0 | `executable` |
| 否则当日买入 > 0 | `tomorrow_ref` |
| 否则 | `observe_only` |

空仓**不给** `executable`——没有可卖的老底仓，正 T 第一腿就卖不出去。

`SIGNAL_UNIVERSE_FILTERED_BY_POSITION = False`，恒 `False`。按持仓过滤会让回测的样本集
随每个人的账户变化——同一个策略在不同人手里跑出不同结论，而谁都说不清差异来自策略还是
来自持仓。

## 存储：默认不落盘，且要落盘得先过两道门

- `InMemoryTRoundRegistryStore` 是默认实现，进程退出即消失
- `DiskTRoundRegistryStore` 构造时 `enable_persist=False`（默认）→ `PersistDisabledError`
- 即便显式 `enable_persist=True`，本批次也只是 `NotImplementedError`

`main.py` / 日管道里**没有**任何 `t_rounds` 引用，有测试守着。

轮次上限的检查放在 `Store.upsert()` 而不是调用方：「最多 1 轮」是一条**不变量**，
不是某个调用点的业务规则。只要有人能绕过 Store 直接塞记录，这条不变量就破了。

`upsert` 只在「本次新增消耗名额」时才查配额——把一条已存在的 `abandoned` 改成
`void_data`（释放名额）不该被拦；把记录挪到另一个 `trade_date` 则重新查。

## 隔离：与 g 和提醒层零耦合

`app.t_rounds` 与 `app.buy_sell_signals` **不 import、不共享实例**。两个包的记录形状与
不变量完全不同，共用 Store 只会让两边的约束互相牵制。

测试静态扫描：本包**不** import `buy_sell_signals` / `trade_marks` / `news.push`；
本包常量名与提醒层常量名无交集。

## 测试

```bash
cd backend
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest \
  tests/test_t_round_registry.py -q
```

93 条，不依赖网络 / 真实 `/data/tsp-reports`。覆盖：

- 默认值（`formal_push_allowed=False`、`paper_track_only=True`、`max_rounds_per_day=1`）
- ε 与 `design_version()` 串
- 腿方向（正 T = 卖→买，倒 T = 买→卖）
- KPI 硬禁（6 个非 `completed` 状态参数化）
- 正 T 老底仓（构造期 / transition / complete 三处）
- 净价差方向（两个 mode 各测一次符号）
- 成本档位白名单：非法串拒 / `design_pending`+0 绿 / `0bp`+0 绿 / `0bp`+>0 拒 /
  非零档 +>0 绿 / 非零档 +0 拒（禁 silent 0）/ 负金额全档位拒 / 白名单只此一处定义
- Store 往返与过滤；轮次上限 6 种场景（同日同票拒 / 异票放行 / 次日放行 /
  `completed` 仍占额 / `void_data` 不占 / 挪日重查）
- 持仓三态、空仓不给 `executable`
- 隔离（不 import g / 提醒层 / `trade_marks`）
- 禁止名 AST 扫描
- 脚注可见、六条软提醒常量存在

## ALIGN §4 自检

- [x] 状态机：转移表显式；`completed` 无出边；`void_data` 不占轮次名额
- [x] 默认不落盘；persist 开关默认关；磁盘实现 `NotImplementedError`
- [x] 正 T 第二腿必须老底仓（构造 / 转移 / 结算三处拦）
- [x] 非 `completed` 不得进已实现 KPI（构造期硬校验）
- [x] ε = 0.30% 且 locked 进 version 串
- [x] 与 `push_dedupe_cooldown_until` / 提醒层冷却**分列**（字段名、常量名均不撞）
- [x] 与 g `buy_sell_signals`、`trade_marks`、`news/push` 零 import（静态检查守着）
- [x] 脚注「仅供研究，不构成投资建议；执行须人工确认」出现在包 docstring 与每个模块
- [x] 单测：状态迁移、结算方向、轮次上限、Store 内存往返；不依赖分钟数据

## 本批次明确不做

- 落盘 / persist 开关默认开 / 写生产分区
- 正式 SSE / 飞书推送
- MCP 交易
- 接线真实分钟评估、`build_feature_frame` 在线路径、`minute_replay` 生产评估
- 用 09:30 开盘 1m **冒充** 09:25 竞价
- 全量特征回测 / walk-forward、ε 重标定
- 分档成本实测与成本敏感性跑表（批次 20261025gcost 只放开了 5b **契约**，
  0 / 0.05% / 0.10% / 0.15% / 0.20% 的敏感性表是 **5c**，另一拍）
- 可卖老股实盘字段（`virtual_position_flag` 只是占位）
- Regime 产品规则标定、g2 质量代理加深

**所有「已验证 / 已测通」的说法在本批次都不成立**——`disclaimer.py` 里
`NOT_CLAIMED_VERIFIED` 列出的三项（特征回测、ε 门禁、可卖老股实盘）均未验证。

## 后续接入前要补的东西

1. **分钟评估路径**：接真实分钟数据，先解决「同尺后再 VWAP」（软提醒 1）
2. **ε 重标定**：0.30% 是设计钉，不是标定结果
3. **分档成本实测（5c）**：按成交额分档的滑点 + 印花税 + 冲击成本。
   契约侧 5b 已放开白名单（`design_pending` ∪ `0bp`/`5bp`/`10bp`/`15bp`/`20bp`，
   非零档必须配非零金额），但**跑表与结论属 5c**，本拍一行敏感性表都没跑。
   散户双边成本 0.1%–0.15% 只是**假设口径**，不是唯一真实成本。
   `max_rounds_per_day` 的放开仍要等 5c 结论，本拍未动。
4. **可卖老股实盘字段**：`tag_position` 目前吃的是外部给的 `old_lot_qty_available`，
   真实账户口径（含冻结、待交收）未接
5. **磁盘 Store**：先单独设计（大概率单文件 + 版本号，或进 SQLite），不要硬套分区 parquet
6. **Regime 产品规则**：本轮未标定

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议。*
