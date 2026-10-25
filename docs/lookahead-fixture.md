# lookahead 偏差审计夹具（批次 20261025glookahead · -A-#3 P1）

> **仅供研究，不构成投资建议；执行须人工确认。** 红涨绿跌。

本文件描述 `backend/app/backtest/lookahead_fixture/` 这个包**做了什么**、
**不做什么**，以及为什么它给出的结论句式是「未检出前视」而不是「已无前视」。

---

## 1. 它回答什么问题

> 一个只看到 **≤ t** 数据的重跑，能不能复现出「看到全部数据的那次跑」里
> **时点 ≤ t** 的那部分产出？

不能 → 原来的那次跑在 ≤ t 的时点上用了 t 之后才知道的东西。这就是 lookahead。

这是一个**必要条件**检验，覆盖范围明确有限：

| 它覆盖 | 它不覆盖 |
|---|---|
| 被注册的入口（本拍只有一个） | 未注册入口的任何行为 |
| `key_spec.compare` 里列出的字段 | 没列进去的字段 |
| 传入的那个 `t` | 其他截断点 |
| 「同一条记录在两种数据可见性下是否一致」 | 记录**本身**的因果合理性（另有 `assert_causal_order`） |

所以它能给出的结论上限是：**「在被注册的入口、被选中的截断点、被列入比对的字段上，
未检出跨截断点的前视。」** 再往前一步就是空喊。

---

## 2. 与 walkforward 是什么关系

**互补，不能互相替代。**

| | walkforward | 本夹具 |
|---|---|---|
| 切的是什么 | OOS 折（训练段 / 验证段的时间隔离） | 某一次跑的**每个时点**用了哪些数据 |
| 回答的问题 | 样本外是否还有效 | 样本内是否偷看了未来 |
| 典型失效 | 折间隙泄漏、参数在验证段调 | 用收盘价回填开盘 VWAP、出场早于确认 |

walkforward 全绿而两次跑的 ≤ t 产出不一致，是完全可能的 —— 反之亦然。用其中一个
冒充另一个，两者都不会被真正验证。本拍**不改** walkforward 的折切分语义。

---

## 3. 契约（写死）

```
给定：entry_id + run_config（参数冻结）+ 截断时点 t（入口内写死一种口径）
动作：用「仅含 ≤ t 的数据」重跑同一入口
比对：原跑中「产出时点 ≤ t」的记录，必须与截断重跑的对应记录逐字段相等
```

| 规则 | 钉死 |
|---|---|
| 数据可见性 | 重跑输入**禁止**出现 `> t` 的行；`assert_truncated` 复核 |
| 比对窗 | 只比产出时点 ≤ t 的记录；`> t` 的产出不在不变性集合内 |
| 不变性比较 | 规范化后**结构化逐字段**相等；禁止只比条数/哈希 |
| 允许差集 | **默认空白名单**（`()`）；非空必须由任务书写死列名 |
| 未注册 | `UnregisteredEntryError`（fail-closed，不猜默认键） |
| 比对窗为空 | 抛错，**不**当作「集合相等即通过」 |

### 差集白名单为什么默认空

不变性被打破时，唯一正确的反应是去看代码，不是往白名单里加一列。

将来**可能**合法进白名单的只有非语义标识符（如 `run_id` 里的随机后缀、`created_at`
取系统时钟）—— 特征是「它变化时不携带任何关于『策略看到了什么数据』的信息」。
**任何价格、时点、状态字段都永远不能进白名单。**

这条不由代码强制（白名单是自由字符串），由 review 和任务书守。本拍白名单为 `()`，
**没有任何字段在白名单里**。

---

## 4. 本拍注册的入口与比对键

### 4.1 `t_rounds.offline`

做 T 的离线纸面回测（`run_mode_a_backtest`）。`t` 口径 = **交易日**（`date`）。

| 角色 | 字段 |
|---|---|
| `identity`（定位一轮） | `symbol` · `trade_date` · `mode` · `path` |
| `compare`（逐字段比） | `leg1_ts` · `leg2_ts` · `round_status` · `leg1_price` · `leg2_price` · `gross_spread` · `net_spread` |
| `t_field`（产出时点） | `leg1_ts` |

**为什么 `path` 在 identity 而不在 compare**：A 与 B 是两格独立的回测（同一天同一票
在两个格子里可以各开一轮）。混成一个键会让两轮记录在比对时互相覆盖。

**为什么 `t_round_id` 不比对**：它把确认时刻拼进了串里
（`tr._make_record` 的 `tag` 参数），而确认时刻恰恰是要审的东西。用它当键，等于
「入场时刻不同 → 认成两行 → 一边缺失一边多出」，报出来的错是 `missing_in_replay`，
而真正的病灶（成交价取了确认 bar 自己的价）被掩盖。真正的语义在 `leg1_ts` 里，
那个字段已经在 compare 里了。

**为什么 `cost_amount` / `cost_bp_tier` 不比对**：本夹具审的是「看到了什么数据」；
成本口径由 5b/5c 闸门管，本拍**零碰** 5c 的 `_settle` / 跑表 / 闸门行为。

### 4.2 其他入口 —— `design_pending`

| 入口 | 状态 | 说明 |
|---|---|---|
| `POST /api/backtest/strategy/run`（策略回测） | **`design_pending`** | 需要 `(strategy_id, symbol, signal_ts, side)` 一套键；本拍未钉 |
| 其它注册策略 | **`design_pending`** | 未钉键不得宣称已过夹具 |

**不得**因为「t_rounds 过了」就推断其他入口也过。它们连键都没定。

---

## 5. 绿测 / 红测

| 类型 | 内容 | 位置 |
|---|---|---|
| **绿** | 干净截断重放 → 不变性成立 | `test_t1_clean_truncation_replay_passes` |
| **绿** | 同一输入两次跑，逐字段与顺序都相同 | `test_t1b_...` |
| **红·注入未来** | 结果里仍有 `> t` 的行 | `test_t9a_...` |
| **红·注入未来** | 该留下的行被丢掉 / 行数不对 | `test_t9b_...` / `test_t9b2_...` |
| **红·未来改写 ≤t 产出** | 截断干净，但 ≤ t 那几轮的价/状态变了 | `test_t2c_...` / `test_t2d_...` |
| **红·路径 A 类** | 出场早于入场（确认晚于成交） | `test_t3_leg2_before_leg1_is_red` |
| **红·路径 A 类边界** | 记录自洽但入场取错 → 夹具**抓不到**，由行为测试守 | `test_t3b_...` |
| **fail-closed** | 未注册 / 缺键 / 口径不符 / 窗内空集 | `test_t4*` 一组 |

### 路径 A 类反例到底抓的是什么

旧实现的两处超前（已在批次 20261025gtrounds 修掉）：

1. 入场点取「边沿那根」，而「边沿之后确实进到 ε 带」要到更晚才成立 →
   **进场时刻早于它自己的确认依据**；
2. 出场搜索从「边沿那根」之后开始 → **出场可以发生在确认之前**。

第 1 类的记录症状是 `leg2_ts < leg1_ts`（一轮 T 在买入前就卖出了），
`assert_causal_order` 抓得住。第 2 类**不一定**：入场记在边沿、出场记在确认之前，
时刻是递增的，记录自洽 —— 这类由 `tests/test_t_rounds_offline_backtest.py` 里守
`run_mode_a_backtest` **行为**的三条用例守（`test_path_a_never_completes_before_its_confirmation`
等）。`test_t3b` 把这个边界写成了测试，就是不让「夹具绿」被读成「没有前视」。

---

## 6. 主门禁 = 仓内 CI

- pytest 节点：`backend/tests/backtest/test_lookahead_fixture.py`
- CI：`.github/workflows/ci.yml` 的 backend job 跑 `uv run pytest tests -q`，
  该文件落在 `tests/` 下，**自动被收集**。夹具红 → CI 红。
- **无** `xfail`、**无** 默认 `skip`、**无** `continue-on-error`。
  `test_t7*` 两条用例守着这件事（扫源码 + 扫 ci.yml）。

### 不在 MCP 热路径重跑

`mcp-server` 的 `run_backtest` **不**调用本夹具。理由：

- 每次 API 调用都重跑一遍完整夹具是性能事故；
- 更糟的是「门禁」会退化成「每次调用都可能不同的随机结论」——
  同一个策略两次调用一个过一个不过，那还叫门禁吗。

---

## 7. 对外前置契约（fail-closed）

> **某入口/策略在对外宣称「已过 lookahead 审计」、或对第三方开放
> `run:backtest` 的结果之前，必须有仓内夹具的通过记录。**
> **未过不得宣称；无法判定同样不得宣称。**

### 7.1 检测点

| # | 检测点 | 本拍状态 |
|---|---|---|
| 1 | 本文档 + `open-platform-plan.md` + `features.md` + `mcp-server/README.md` 的前置句 | **已写** |
| 2 | 代码侧 `lookahead_fixture/gate.py`（`gate_status` / `is_cleared_for_external_claim`） | **桩已落，恒不放行** |
| 3 | 发布流程钩子 | **`design_pending`**（见 §8） |

`gate.py` 的 `is_cleared_for_external_claim()` **恒返回 `False`**。这不是偷懒：

- 仓内 CI 的结果是 GitHub Actions 的绿勾，**不落到仓里**，运行时读不到；
- 若要「CI 绿过就放行」，需要一份持久通过记录，而它的写入端也在本拍范围外；
- 任何 `True` 都只能是写死的 —— 而写死的 `True` 会在夹具明明红了的时候继续放行。

所以宁可让查询直接回答「查不了」（fail-closed 最强形式），也不给一个看起来能用的
`True`。`test_t8_gate_is_fail_closed` 守着这条：谁把它改成 `return True`，CI 立刻红。

### 7.2 与人工清单的关系

`buy_sell_signals.state_machine.MANUAL_PUBLISH_CHECKLIST` 里的 `no_lookahead`
是**人工勾选项**，本夹具落地后文档可声明它是「自动化证据」。但本拍
**不强制改** `buy_sell_signals` 的行为 —— 两者分列，不互相替代。

---

## 8. `/api/backtest/*` 挂钩点 —— `design_pending`

tip 上 `POST /api/backtest/strategy/run` 等路由存在，但**没有**一个统一的「发布」
运行时钩子可以挂夹具。所以：

| 项 | 本拍 |
|---|---|
| 运行时 API 拦截中间件 | **`design_pending`**（不做不挡本拍） |
| 最小可验方式 | CI 注册测例（§6）+ 文档前置句（§7） |
| **禁止** | 假称 tip 已有运行时 lookahead 拦截中间件 |

把挂钩点标成 `design_pending` 而不是「接上」，是因为**接上一个空钩子比标 pending
更坏** —— 它看起来存在，实际不拦任何东西。

---

## 9. 明确不做

- 拷贝 **freqtrade / Fincept** 任何源码（**GPL 零集成**）。本包只借了「截断重放 +
  逐字段比对」这个思路，实现全部原创。
- MCP 下单；对第三方真开放 `run:backtest` 的落地（本拍只钉前置契约）。
- 在 MCP 热路径重跑夹具当主门禁。
- 改 walkforward 折切分语义；用 walkforward 冒充本夹具。
- 改 5b/5c 成本闸门 / 跑表 / `_settle` 行为。
- 碰三个生产 env（`BUY_SELL_SIGNALS_PERSIST` / `PUSH` / `DINGTALK`）。
- 搅 `custom/dsa/**` 行为、`buy_sell_signals/**` 行为。
- P2 Skills / P3 Token / P4 OpenBB / P5 DSA / P6 Kronos。

---

## 10. 本地跑

```bash
cd backend
PYTHONPATH=. python -m pytest tests/backtest/test_lookahead_fixture.py -q
# 确认红测没被 xfail/skip 掩盖：
PYTHONPATH=. python -m pytest --collect-only -q tests/backtest/test_lookahead_fixture.py
# CI 等价：
uv run pytest tests -q
```

---

*参数以 ALIGN/DISPATCH / 既有旁支口径为准。仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。*
