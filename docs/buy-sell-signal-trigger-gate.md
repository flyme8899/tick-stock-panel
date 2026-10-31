# 买卖信号 · 触发/放量门禁契约

> 批次 `20261031gtrig` · P-bss-trigger · 上游 Issue [#-A-#9](https://github.com/flyme8899/-A-/issues/9) 挂档下一档（**closed · 终锁**）
> 锁面版本：`trigger_gate_v1` · **两 TRIGGERING 默认 False** · **COMPLETE 仍 False**
> 只读交叉：`shadow_obs_v1` / T1 `tier_cut_v1` / T2 `tier_display_v1` / P3a / P3b
> **tip = prod = `c1b19fc8dc684f118db897b5d38902207d0d0777`**（#261 gshadow + #260 T2 + #259 T1 + #254–#258 **已合且已部署 live**）
> rollback `pre261_20261031_172351` · 本拍新 PR **不部署**
>
> **仅供研究，不构成投资建议；执行须人工确认。** 红涨绿跌。成本来回 **18.2bp**。

---

## 0. 这份文档是什么 / 不是什么

**是**：触发/放量**门禁**的契约。回答「讨论翻闸之前要核对什么、现有哪些缺口、哪些话不成立」。

**不是**：

| 不是 | 说明 |
|---|---|
| **不是**触发引擎 | 没有量比计算、没有突破判定、没有窗口逻辑、没有调度。本拍一行算子都不写 |
| **不是**放量开闸 | 没有把任何 `*_TRIGGERING_ENABLED` 翻 True |
| **不是**授权翻闸 | 契约齐备 ≠ 已获准翻闸。**门禁契约 ≠ 授权翻闸** |
| **不是**可放量声明 | 本拍不宣称可放量，也不为将来宣称铺路 |
| **不是** schema 改动 | 不动 hits 字段集 |
| **不是**真推落地 | 不动钉钉模板、不写旁注代码 |
| **不是**改三 env | `PERSIST` / `PUSH` / `DINGTALK` 零触碰 |

> **一句话**：本文件把「翻闸前必须成立的事」写下来并落成可验断言；它**不**制造那些事成立。

---

## 1. 产品语义

触发/放量门禁＝「**何时可以开始讨论翻闸**」的前置契约。三层严格分开：

| 层 | 内容 | 本拍 |
|---|---|---|
| **门禁层**（契约） | 前置条件、禁项、缺口披露、大字 | **本拍交付** |
| 授权层（口令） | 所有者明示口令 + 另 ALIGN/FIX | 另口令 |
| 执行层（引擎） | 量比/突破/窗口/调度/触发表 | 另口令 |

三层不可跳：**有契约 ≠ 有授权 ≠ 有实现**。任何一层缺失时，前一层都不构成后一层的理由。

---

## 2. 两开关（`Final` 常量 · **非 env**）

| 常量 | 位置 | 当前值 |
|---|---|---|
| `VOLUME_BREAKOUT_TRIGGERING_ENABLED` | `volume_breakout_stub.py:131` | `Final = False` |
| `AUCTION_ABNORMAL_TRIGGERING_ENABLED` | `auction_abnormal_stub.py:155` | `Final = False` |

**两者都是仓内 `Final` 常量，不是环境变量。** 这个区别是实质的：

- 环境变量可以在部署时改，`Final` 常量只能改代码 + 走 PR
- 「非 env」意味着**没有任何运行时路径可以打开它们**——想开就得进 diff，进 diff 就会被人看见

本拍**只读断言，不改取值**。

两处 stub 的 docstring 已经把话说尽了：

> 「是否允许产出"已触发"结论。**恒 `False`**。没有任何理由在本拍把它改成 `True` —— 改了也只是让一个空桩撒谎。真开放量需要另拍（阈值标定 + 触发表 + 负号解释），不是一个开关的事。」（volume）

> 「改成 `True` 只会让一个空桩撒谎。真开竞价需要另拍：补价字段 / 阈值标定 / 回测证据 / 分钟 09:25，四件事一件都不能少。」（auction）

---

## 3. 翻闸另口令

**本拍及本 PR 不得把任一 `*_TRIGGERING_ENABLED` 翻 True。**

真翻须：**所有者明示口令** + **另 ALIGN / FIX**。上游 #-A-#9 R2-10 本议题**不拍翻闸**——终锁只把「触发/放量」挂档，没有授权打开。

---

## 4. 前置条件表（**表 ≠ 已可翻**）

> **这张表是「讨论翻闸前要核对什么」，不是「本拍已满足可翻」。**
> 表里每一行的「当前」列都指向一个**尚不成立**或**只成立了一半**的事实。

| 项 | 核对含义 | 当前（截至本拍） |
|---|---|---|
| 路径已通 | 主链 / 观测口路径是否在场 | 在场 —— 但**路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING** |
| calib 状态 | 是否跑出过过门槛的回测 | 需逐族核；**calib 过 ≠ 可开 TRIGGERING** |
| K8 | 产线 K8 结论 | **K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING** |
| COMPLETE | A2 完成门 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` | **仍 `False`**；本拍不翻；与 TRIGGERING 正交 |
| 影子观测口 | 契约在场且**默认关**取向一致 | `shadow_obs_v1` 在场、默认关；**默认关 ≠ 放量** |
| 三 env | `PERSIST` / `PUSH` / `DINGTALK` | 本拍**零触碰**；不得以翻闸为由改 |
| P3a 闸 | `NON_LIVE_MODE` 仍拦、无 `allow_shadow` | **仍绿**；本拍不放宽 |
| **证据缺口** | 两族待补证据是否清零 | **0 / 8 清零**（见 §4.1）—— 两族各自 4 条，一条都没清 |

### 4.1 证据缺口是 8 条，不是一句「待补」

两族各有一个 `frozenset` 常量，是**代码里就能读到的**缺口清单，不是文档里的口述：

| 族 | 常量 | 条数 | `*_STATUS` |
|---|---|---|---|
| 量能突破 | `VOLUME_BREAKOUT_EVIDENCE_PENDING` | **4** | `data_pending` |
| 竞价异动 | `AUCTION_ABNORMAL_EVIDENCE_PENDING` | **4** | `data_pending` |

缺口明细（只读引自 tip，**本拍不动**）：

**`VOLUME_BREAKOUT_EVIDENCE_PENDING`**
- `local_minute_lib_data_pending` —— 本机无分钟库
- `trigger_evidence_table_data_pending` —— 无触发表 / 回测表
- `threshold_lock_data_pending` —— UI 阈值 ≠ 已锁回测门禁
- `http_minute_listing_auth_pending` —— 分钟列表鉴权未通

**`AUCTION_ABNORMAL_EVIDENCE_PENDING`**
- `auction_price_not_persisted` —— 落盘无 `auction_price` / `prev_close`
- `threshold_lock_data_pending` —— UI 阈值 ≠ 已锁回测门禁
- `backtest_evidence_data_pending` —— 无竞价异动回测 / 触发表
- `minute_0925_still_absent` —— 09:25 分钟数据仍缺

### 4.2 为什么把「8 条」写进文档

因为「证据缺口仍在」这句话太软，读者会当成客套。**8 条、清零 0 条**是一个可数的数字：

- 它说明**不是差一点**，是差 8 件具体的事，每件都有名字
- 它可被断言（本锁面只读引用这两个集合，断言 `*_STATUS == "data_pending"` 且两集合非空）
- 缺口哪天被补掉，`assert` 会**在补掉的那一刻**提醒「契约描述过时了」，而不是让文档慢慢腐烂

**文档里的缺口清单必须由常量派生，不能手抄。** 手抄的话，stub 补了一条、表里还是旧的，表就成了「已经补完了」的假象——这比没有表更坏。

---

## 5. 与 COMPLETE / calib / K8 / 影子观测口

| 关系 | 结论 |
|---|---|
| COMPLETE ↔ TRIGGERING | **正交**。COMPLETE 仍 `False`，本拍不翻；翻 COMPLETE 也不能替代翻 TRIGGERING，反之亦然 |
| calib ↔ 可放量 | **calib 过 ≠ 可放量 ≠ 可开 TRIGGERING**。calib 证明「口径跑得通」，不证明「该放量」 |
| K8 ↔ 可开 TRIGGERING | **K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING**。K8 是产线结论，不是放量授权 |
| 影子观测口 ↔ 放量 | 默认关 ≠ 放量；**影子写入 ≠ 观测口 ≠ 放量**。观测口是**看**的，不是**放**的 |

一句更硬的：**没有任何单项结论可以单独构成翻闸理由。** 前置表是全表核对，不是任一项达标即可。

---

## 6. 方向与禁词

方向仍 `DIRECTION_LABELS` = `{research_buy, research_sell, research_watch}`。

- **保留 `research_watch`**（它不是「无方向」的兜底，是独立的一档）
- **禁六词动作指令式**：可以买入 / 建议买入 / 强烈买入 / 可以卖出 / 建议卖出 / 强烈卖出（及同义）
- **短动作词**（买/卖/加仓/减仓/开多/开空）**只查档名**，不扫全文 —— 理由同 T2 §4.2：「买卖信号」是本项目名
- **禁价量仓单**字段：`price` / `quantity` / `position` / `order` + 中文

本拍**不改 DIRECTION**，也不新增强度表示。

---

## 7. 门禁（大字）

> **路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING**
>
> **K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING**
>
> **讨论 ≠ 派单**
>
> **影子写入 ≠ 观测口 ≠ 放量**
>
> **门禁契约 ≠ 授权翻闸**
>
> **COMPLETE 仍 `False` ≠ 可翻 COMPLETE**
>
> **合入 ≠ 部署**；**tip = prod**（#261 已 live）

**门禁契约 ≠ 授权翻闸** 是本文件最容易被读错的一句。写完这份契约，翻闸的门槛**一点都没降低**——只是把门槛写清楚了。写清楚门槛不等于跨过门槛。

---

## 8. Bonferroni

门禁/触发边 **不占** `m = 6`。

**非永久豁免**：这是「当前把预算花在更该花的地方」的取舍，不是「触发边永远不算多重比较」的定理。

本拍**不改** `BONFERRONI_M`，也不 bump scheme。**禁止把门禁边冒充为永久豁免以支撑可放量。**

---

## 9. 挂点与另口令

**挂点** ＝ 门禁契约文档（本文）+ 薄锁面（`trigger_gate_lock.py`，可验）。

| 项 | 状态 |
|---|---|
| 真翻 TRIGGERING / 可放量开闸 | **另口令**（所有者明示 + 另 ALIGN/FIX） |
| 完整触发引擎 / 量比·突破·窗口 / 调度 / crontab / 扫表 | **另口令** |
| schema 进 hits / 真推旁注落地 / 改钉钉模板 | **另口令** |
| 翻 COMPLETE / 改三 env | **另口令 / 禁止冒充** |
| Opt-A / auction·17–19 实施 / PanWatch / 做T | **另口令 / 零碰** |
| 部署本 PR | **另口令**；本拍不部署 |

---

## 10. 与既有接线

| 只读交叉对象 | 交叉内容 | 本拍 |
|---|---|---|
| `volume_breakout_stub` | `VOLUME_BREAKOUT_TRIGGERING_ENABLED is False` + `EVIDENCE_PENDING`（4 条）+ `STATUS=="data_pending"` | 只读，**不改取值** |
| `auction_abnormal_stub` | `AUCTION_ABNORMAL_TRIGGERING_ENABLED is False` + `EVIDENCE_PENDING`（4 条）+ `STATUS=="data_pending"` | 只读，**不改取值** |
| `first_seen_audit` | `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False` | 只读，**不翻** |
| `shadow_obs_lock` | `SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"`；默认关 | 只读交叉，**不重写规则** |
| `tier_cut_lock` | `TIER_CUT_RULE_VERSION == "tier_cut_v1"` | 只读交叉 |
| `tier_display_lock` | `TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"` | 只读交叉 |
| `constants` | `DIRECTION_LABELS` 三值；`BONFERRONI_M == 6` | **零碰** |

**不算行情、不改 stub 取值、不调度、不真推、不重写 T1/T2/shadow_obs。**

---

## 11. 锁面模块

见 [`backend/app/buy_sell_signals/trigger_gate_lock.py`](../backend/app/buy_sell_signals/trigger_gate_lock.py)。

纯常量 + 纯断言，**无写路径 I/O**：不读行情、不写文件、不碰 `HitStore`、不调推送、无调度。断言助手由测例调用，**模块 import 时不硬 raise**。

### 11.1 缺口清单**派生**而非复制

锁面里 `TRIGGER_GATE_EVIDENCE_PENDING_COUNT` 由两个 `*_EVIDENCE_PENDING` **只读求和**，不写 `8` 这个字面量。理由同 §4.2：手抄的数字会在缺口被补掉后继续骗人。

---

## 12. 建议路径（非本拍必须交付）

- 翻闸前核对清单自动化（把 §4 表变成可跑脚本）→ 可挂建议；**≠ 已授权翻闸**
- 完整触发引擎 / 调度 / 触发表 → **另口令**
- schema 真推 / 真推旁注落地 → **另口令**
- 缺口清零进度看板 → 可挂建议；**看板绿 ≠ 可翻闸**

---

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*
