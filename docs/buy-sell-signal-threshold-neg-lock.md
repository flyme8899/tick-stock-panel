# 买卖信号 · 阈值负锁语义（`threshold_neg_lock_v1`）

> 批次 `20261101gthresh` · P-bss-evidence · 对象＝VOLUME `threshold_lock_data_pending` 的**语义**
> 锁面：`backend/app/buy_sell_signals/threshold_neg_lock.py` · version `threshold_neg_lock_v1`
> **标签仍开** · COUNT **仍 6** · 两 TRIGGERING / COMPLETE **仍 `False`** · **本拍 ≠ 清零刀**
>
> **仅供研究，不构成投资建议；执行须人工确认。** 红涨绿跌。成本来回 **18.2bp**。

---

## 0. 一句话

**这条标签缺的不是一个凭证，是一个解释。** 在负号被解释掉之前，
`threshold_lock_data_pending` 必须**继续待着** —— 而"必须继续待着"这件事，
需要一个能被测例调用的名字，于是有了这个模块。

---

## 1. 为什么"待补"和"必须继续待着"要分开

`VOLUME_BREAKOUT_EVIDENCE_PENDING` 里剩两条，并排放着：

| 标签 | 缺的是什么 | 补齐后能不能清 |
|---|---|---|
| `http_minute_listing_auth_pending` | 一个**外部凭证** | 拿到凭证、跑通 → 可以考虑清 |
| `threshold_lock_data_pending` | **一个解释** | 负号解释清楚之前，**补什么都不能清** |

前者是「还没到」，后者是「到了也不能用」。

四个突破口径的回测收益是 `-23.86 / -32.44 / -13.80 / -19.81` —— **全负**。
在这个负号有解释之前，把量比 / 突破幅度 / 窗口分钟数标定出来，
标出来的只是**更精确的亏损**。

> **所以越确定 `strongly_negative`，阈值这一格越该锁着。**

---

## 2. 模块里有什么

| 常量 / 函数 | 咬住什么 | 越界时 |
|---|---|---|
| `THRESHOLD_NEG_LOCK_VERSION` = `threshold_neg_lock_v1` | 本契约的版本；改判据必须 bump | — |
| `THRESHOLD_LOCK_TAG_MUST_REMAIN` = `threshold_lock_data_pending` | 标签的名字被写成**可引用的名字**，而不是散在文档里的字符串 | — |
| `assert_threshold_lock_still_pending(pending)` | 标签**必须还在** pending 里 | `ThresholdTagRemovedError`（继承 `AssertionError`） |
| `assert_no_tradeable_threshold_nail(...)` | 量比 / 突破% / 窗口分钟数**禁止**被钉成"已锁定" | `TradeableThresholdNailError` |
| `assert_strongly_negative_does_not_lock_threshold(verdict)` | 触发表 `strongly_negative` **不**构成阈值已锁 | 本拍不 raise（只占位） |
| `assert_freq_lock_stamp_is_not_clear_reason(has_freq_lock_stamp=...)` | 有 `FreqLockStamp` **不是**清标签的理由 | 本拍不 raise（只占位） |

**诚实空态**＝`(None, False, "", "未锁定")`。其余任何值 —— 包括 `0` —— 都算**宣称已锁**。

`0` 也算，是因为"量比阈值 0"本身荒谬：它要么填错了，要么是某个默认值漏出来了。
放过 `0`，等于给"漏出来的默认值"发通行证。

**正常出口＝什么都不发生。** 本模块**没有**任何合法数值可返回 ——
一个"校验通过"的阈值集合，在这一拍是**不存在的对象**。

---

## 3. 两条只读交叉

### 3.1 触发表：`strongly_negative ≠ 阈值已锁`

`volume_breakout_evidence_table` 的 verdict 回答的是「**过去这四个口径赚钱吗**」，
答案是不赚。它**没有**回答「量比多少才算放量」「突破几个点算突破」「窗口几分钟」。

**表 PRESENT ≠ 阈值已锁。**

### 3.2 freq_lock：`FreqLockStamp ≠ 可清标签理由`

`FreqLockStamp` 是「评估口径身份」的容器（`signal_id` + `definition_version` + `cadence` + `params` 指纹）。
它证明了「有人把一个参数集写下来了」，**不**证明「这个参数集被证据支持」。

`freq_lock` 自己的 docstring 就写着这句话。本模块只是把它变成一条**能被测例调用**的断言 ——
**写在 docstring 里的话，改代码的时候没人会看见；写在断言里的话，改代码会红。**

### 3.3 为什么刻意不 import 对方

本模块**不 import `freq_lock`**，也**不从 `volume_breakout_evidence_table` import verdict**：

- 交叉是**语义层面**的，不是符号层面的。为了一条断言去 import 一整个模块，
  会让"零碰"变成"零碰但依赖"。
- 万一那边哪天改名，本模块的常量应当在测例里红，而不是**悄悄跟着改名**
  —— 跟着改名＝"表变了规矩也自动变"，那这条规矩就没人守了。

两边取值相等由**测例**咬住，而不是由 import 咬住。

---

## 4. 可钉证据路径清单（研究档 · 本拍不执行清标签）

「将来另一个口令才**可能考虑**清这条标签」的前置地图。
**逐条满足 ≠ 自动可清 ≠ 可翻闸。**

| # | 前置 | 现状 |
|---|---|---|
| 1 | 负号**可解释**（非空话的研究口径） | **未成立** |
| 2 | 研究档阈值标定（若采用）须另口令明示，**不得**静默冒充可交易锁 | **未发生** |
| 3 | 真可交易标定的前置（分钟列表鉴权等）已另拍闭合 | **未闭合** |
| 4 | 人读「均未锁定」被诚实改写的事实基础 | **未具备** |
| 5 | 测与 COUNT 派生同步（清一条即同步文档 / NOTE / 期望；COUNT 永不手抄） | **长期要求** |

权威版本见[门禁文档 §4.1.1](./buy-sell-signal-trigger-gate.md)。

---

## 5. version 未 bump 的说明

`TRIGGER_GATE_LOCK_VERSION` 本拍**显式不 bump**，仍 `trigger_gate_v1`（同 #265 / #266）。

理由：bump 会迫使 `schema_push_lock` / `schema_push_live_lock` 的
`EXPECTED_TRIGGER_GATE_LOCK_VERSION` 同步改字面量，而那两个文件本拍标零碰。
**未 bump ≠ 未披露**：`TRIGGER_GATE_LOCK_NOTE` 已加旁注，本文与门禁文档 §4.1.1 / §11.2 已写全。
交叉 bump 留另拍同步。

---

## 6. 大字

- **负锁 ≠ 删标签**
- **钉数 ＝ fabricate**
- **本拍 ≠ 清零刀**
- **清零一刀 ≠ 可翻闸**
- **语义锁面 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING**
- **strongly_negative ≠ 阈值已锁**
- **FreqLockStamp ≠ 可清标签理由**
- **门禁契约 ≠ 授权翻闸**；**合入 ≠ 部署**；**tip = prod**
- **本拍即使可交，也不清标签、不降 COUNT、不翻闸**

---

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*
