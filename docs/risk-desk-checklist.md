# 信号层准入 / 风控桌 Checklist（批次 20261027grisksop · -A-#6 P-risk-sop）

> 状态：**文档索引**。本文不新造任何闸门、不改变任何一条既有闸的行为。
> 它只做一件事：把散在各批次里的风控前置条件，拼成一张能逐项核对的准入表。

**仅供研究，不构成投资建议；执行须人工确认。** 红涨绿跌。成本来回 **18.2bp**。

---

## 1. 目的与边界

### 1.1 这篇文档解决什么

「信号层准入」问的是一个很窄的问题：

> 一条信号产线，在**允许被当作参考信号展示/讨论**之前，仓内到底有哪些既成的前置条件？这些条件分别落在哪个文件、哪条测例上？

本文的答案形态是**索引**，不是**引擎**。四道闸各自早已存在，本文把它们逐项链回实现与测例，让「还差什么」显形。

### 1.2 三组容易混淆的概念

| 概念 | 含义 | 本文态度 |
|---|---|---|
| **信号层准入** | 这条线的产出能不能被拿出来讨论/展示 | ✅ 本文主题 |
| **仓位 / 执行** | 拿多少钱、怎么下单、何时止损 | ❌ **不在本文范围**；见 §4 的独立区块 |
| **文档索引 ≠ 新引擎** | 索引既有闸，不新增运行时逻辑 | ✅ 全文红线 |

**准入 ≠ 允许下单。** 通过本文任何一行，都不代表该信号可以进入交易环节，也不代表胜率已获验证。准入表回答的是「证据链是否齐全」，**不是**「这条线能不能赚钱」。

### 1.3 边界

- **只索引既有闸**：四道闸的实现、测例、口径数字全部以既有为准。本文**不覆盖**任何一条线自己的样本口径（见 §3）。
- **自动化 / 改门禁 = 另口令**：本文不新增自动化门闩脚本，不修改门禁代码与频率常量，不接管任何判定。要改，走另一次口令。
- **缺项显形**：有些条件仓内确实没有（例如统一的「纸面 ≥ N 天」字面闸）。本文**如实标缺项**，**不发明天数**去填补。
- **仓位对照模板非准入**：§4 的 1% / 20% / P10 / 2R 是**对照文案**，不是勾选项，视觉上与准入表分开。

---

## 2. 信号层准入 Checklist（四闸）

> 状态列口径：**已有** = 实现与测例都在仓内且可定位；**部分已有** = 相关机制存在但未形成统一闸；**缺项** = 仓内无对应实现，需另口令。

| 闸门 | 既有实现路径 | 测例 / 文档 | 状态 |
|---|---|---|---|
| **Lookahead（前视偏差）** | `backend/app/backtest/lookahead_fixture/`：`truncate.py` 的 `truncate_frame` / `assert_truncated`；`api.py` 的 `run_truncated_replay` / `assert_invariant` / `assert_causal_order`；`registry.py` 未注册入口 fail-closed；`gate.py` 的 `is_cleared_for_external_claim` | `backend/tests/backtest/test_lookahead_fixture.py`；[`docs/lookahead-fixture.md`](./lookahead-fixture.md)；BSS 发布清单 `no_lookahead` 项 | **已有**（CI 夹具 + 契约）。**注意**：对外查询桩 `gate.py` 的 `GATE_STATUS_PENDING = "design_pending"`，**恒不放行**——运行时拦截中间件**仍挂档**，不得假称已上线 |
| **纸面门（纸面先行）** | BSS：`backend/app/buy_sell_signals/state_machine.py` 的 `MANUAL_PUBLISH_CHECKLIST`（含 `backtest_done` / `sample_adequate`）；DSA：`backend/app/custom/dsa/ledger_audit.py`（`argument_quality_eligible` / `is_snapshot_late`）；t_rounds：`backend/app/t_rounds/offline/` | `backend/tests/` 内 BSS 与 `test_t_rounds_offline_*` 系列；[`docs/dsa-ledger-audit.md`](./dsa-ledger-audit.md)；[`docs/t-rounds-offline-hfeat.md`](./t-rounds-offline-hfeat.md) | **部分已有**。三条线各自的样本充分性判据都在，但**统一的「纸面 ≥ N 天」字面闸：缺项**（见下行）。**注意**：[`docs/paper-trading-plan.md`](./paper-trading-plan.md) 自述「**设计稿（未实现）**」，不得当作已上线的纸面闸 |
| **纸面「≥ N 天」字面闸** | **无**（仓内无此实现） | **无** | **缺项 / 另口令**。本文**不发明天数**填补；需要天数门限时另立口令，并先定清「什么算一天、缺席怎么算」 |
| **三 env / push_gate** | env **名占位**（勿在此写真实值）：`BUY_SELL_SIGNALS_PERSIST` / `BUY_SELL_SIGNALS_PUSH` / `BUY_SELL_SIGNALS_DINGTALK`，定义于 `backend/app/buy_sell_signals/constants.py`（`PERSIST_ENV_NAME` / `PUSH_ENV_NAME` / `DINGTALK_ENV_NAME`）；`PUSH_GATES = {blocked, manual_pending, armed, cooldown, offline}` 定义于 `backend/app/buy_sell_signals/models.py`；`set_push_gate` / `manual_publish` → `armed` | BSS 测例系列；[`docs/buy-sell-signals-skeleton.md`](./buy-sell-signals-skeleton.md) | **已有**（闸机制本身）。本拍**零碰生产值**，字面量与语义均不改 |
| **t_rounds KPI** | 期望值分母＝实际开出轮次（含 `limit_locked`，**不含** `skipped_quota`），见 `backend/app/t_rounds/offline/backtest.py`；`EPSILON_VALUE_PCT = 0.30`（≈30bp）定义于 `backend/app/t_rounds/constants.py` | `test_t_rounds_offline_*` 系列；[`docs/t-rounds-skeleton.md`](./t-rounds-skeleton.md)；[`docs/t-rounds-offline-hfeat.md`](./t-rounds-offline-hfeat.md) | **已有**（分母定义与 ε 均已钉死）。本拍**零碰分母定义** |

### 2.1 四闸之外的旁注

- **`MANUAL_PUBLISH_CHECKLIST` 实际为 6 项**（`design_reviewed` / `backtest_done` / `no_lookahead` / `sample_adequate` / `risk_reviewed` / `disclaimer_ack`），缺任一项即拒绝发布。本表只索引其中与本准入相关的项，**不改项集**。
- **#234 的四字段（bull/bear/veto/snapshot）是「论据质量」**，衡量一次建议的论据是否成立；它**不是**信号层准入。两者的关系是：论据质量可以很完整而信号层仍未准入；信号层准入也不要求每条建议都有 bull/bear。参见 [`docs/dsa-ledger-audit.md`](./dsa-ledger-audit.md) §9。

> **shadow ≠ 纸面。** hits 的 `source.mode ∈ {fixture, shadow, live}`（`backend/app/buy_sell_signals/hits_models.py`，`HIT_SOURCE_MODES`）里，`shadow` 指的是**影子评估**——记录「如果触发会怎样」，属于观察证据；它**不是**「纸面先行」闸，也不构成纸面成交。影子调度**仍为关闭**状态，`live` 的写入等待 P-calib。**不得**把 shadow 记录当作纸面战绩计入准入。

---

## 3. 禁直抄「30 天」与各线样本口径（只引用、不覆盖）

### 3.1 明确拒绝的写法

外部材料（carm1nee 报告 A3 等）提到「纸面 ≥ 30 天」这条经验门槛。**本文显式拒绝把它采纳为 TSP 仓内硬门**，理由有三：

1. **「30 天」不是仓内既有口径**——它是别处的经验值，与本仓的结算周期（T+20）、样本构造方式都不同源；
2. **天数不是充分性判据**——30 个交易日可以全是同一个市场状态，也可以是三个完整周期，信息量天差地别；
3. **抄进来会覆盖各线自己的口径**——Opt-A / gtja191 / BSS 三条线已各自定义了「样本够不够」，再加一层全局天数门限会造成双重口径冲突。

因此：**本文不出现「TSP 要求纸面 ≥ 30 天」这类硬门表述，也不把任何天数写成准入勾选。**

### 3.2 各线样本口径——引用方式

下表**只引用**各线自己的判断方式，**不统一、不覆盖、不折算成单一天数**：

| 线 | 该线自己的口径（仅引用） | 出处 |
|---|---|---|
| **Opt-A** | ≥30 条**已结算** T+20 样本 | Opt-A 方案（**仍 defer**，本拍零碰；文档未入库，随口径评审一并提交） |
| **gtja191** | 因子显著性三门槛（效应量 IR ≥ 0.50、NW-t ≥ 1.96、BH-FDR q ≤ 0.10，三者缺一不可）；样本不足（`n_ic_dates < 30`，口径用 `len(ic_series)` 而非 `n_dates`）时落 `underpowered`，**明示不作结论** | [`docs/gtja191.md`](./gtja191.md) · [`docs/factor-system-design.md`](./factor-system-design.md) |
| **BSS** | `sample_adequate` 勾选项 + `backtest_done`（`MANUAL_PUBLISH_CHECKLIST`） | [`docs/buy-sell-signals-skeleton.md`](./buy-sell-signals-skeleton.md) |
| **DSA 账本** | 论据质量分母（排除快照迟到与缺快照），本拍不给百分比 | [`docs/dsa-ledger-audit.md`](./dsa-ledger-audit.md) §9 |
| **t_rounds 离线** | 期望值分母＝实际开出轮次（含 `limit_locked`，不含 `skipped_quota`） | [`docs/t-rounds-offline-hfeat.md`](./t-rounds-offline-hfeat.md) |

**读法**：每一行是**该线自己的**判断方式，彼此不可通约。看到「样本够不够」的问题时，先问「是哪条线」，再查那一行的口径；**不要**去找一个跨线的统一数字，仓内没有，也不该有。

---

## 4. 仓位对照模板（对照文案，非准入项）

> ⚠️ **对照文案，非准入项。**
>
> 本节与 §2 的准入表**没有任何勾选关系**。它是一组便于口头讨论的示例数字，**不是**准入条件，**不是**风控建议，**不是**可以照抄的交易参数。通过 §2 全部四闸，**也不代表**可以采用下列任何一档。

以下仅为**对照参考文案**，用于讨论「如果要做仓位管理，通常长什么样」：

| 对照项 | 示例文案 | 说明 |
|---|---|---|
| 单笔风险 | ≤ 权益 **1%** | 示例口径，非本仓标准 |
| 单票上限 | ≤ **20%** | 示例口径，非本仓标准 |
| 止损参考 | **P10** 分位 | 示例口径，非本仓标准 |
| 目标盈亏比 | **2R** | 示例口径，非本仓标准 |

**关于这组数字的来源，需如实说明**：本仓 `research/regime_sizing/` 目录实际只含市场宽度（R3）核对脚本 `verify_backend_breadth.py`，**与仓位口径无关**；仓内**未检索到** 1% / 20% / P10 / 2R 的成文出处。因此本表按**纯示例文案**处理，不声称任何仓内依据。若要把它变成有依据的东西，需要另立口令、补出处、补评审。

**为什么单独成节而不是塞进 §2**：一旦和准入表同列，读者会默认「打了勾＝可以按这个仓位做」。这是最危险的一种误读，所以本节在视觉与结构上都与准入表隔开。

---

## 5. 挂档 / 另口令（勿开范围）

以下事项**明确不在本拍**，列出是为了防止被误认为「已经做了」或「现在就可以做」：

| 事项 | 状态 | 开它的前提 |
|---|---|---|
| Opt-A（DSA skill 实调用） | **仍 defer** | 另口令；本拍零碰 |
| #7 | **挂档**（定序下一项） | 另口令，勿提前开 |
| 纸面「≥ N 天」字面闸 | **缺项** | 另口令；先定口径再定天数，禁发明数字 |
| 运行时 lookahead 拦截中间件 | **挂档**（`gate.py` 恒不放行） | 自动化另口令；现有 CI 夹具 + 契约已够用 |
| 自动化门闩脚本 | **非本拍** | 另口令 backlog |
| 触发翻 True / 影子调度上线 | **禁止** | 另口令；现均为关闭 |
| TradingAgents 运行时 | **禁止嵌入** | — |
| MCP 下单 | **禁止** | — |
| 本拍合入 / 部署 | **另口令** | 产线仍为 #232 镜像 |

**关于 freqtrade lookahead-analysis**：本项目在 lookahead 夹具设计中**只借鉴了「截断重放 + 逐字段比对」的概念**，**未拷贝任何源码、未引入任何依赖**（GPL 零集成）。参见 `lookahead_fixture/__init__.py` 模块文档「不拷贝 freqtrade / Fincept 任何源码（GPL 零集成）」。

---

## 6. 相关文档

- [lookahead 偏差审计夹具](./lookahead-fixture.md) —— 第一闸的实现与契约细节
- [BSS 信号骨架](./buy-sell-signals-skeleton.md) —— 发布清单、三 env、push_gate
- [t_rounds 骨架](./t-rounds-skeleton.md) · [t_rounds 离线评测](./t-rounds-offline-hfeat.md) —— 第四闸分母定义与 ε
- [DSA 证据账本审计口径](./dsa-ledger-audit.md) —— 论据质量（≠ 信号层准入）
- [虚拟账户设计方案](./paper-trading-plan.md) —— **设计稿（未实现）**，非已上线纸面闸
- [gtja191](./gtja191.md) · [因子体系设计](./factor-system-design.md) —— `underpowered` 与显著性口径

---

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*
*本文为文档索引，不改变任何门禁行为；合入与部署均为另口令，产线仍为 #232 镜像。*
