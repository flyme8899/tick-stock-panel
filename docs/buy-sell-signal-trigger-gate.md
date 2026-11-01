# 买卖信号 · 触发/放量门禁契约

> 批次 `20261031gtrig` · P-bss-trigger · 上游 Issue [#-A-#9](https://github.com/flyme8899/-A-/issues/9) 挂档下一档（**closed · 终锁**）
> 锁面版本：`trigger_gate_v1` · **两 TRIGGERING 默认 False** · **COMPLETE 仍 False**
> 只读交叉：`shadow_obs_v1` / T1 `tier_cut_v1` / T2 `tier_display_v1` / P3a / P3b
> **tip = prod = `c1b19fc8dc684f118db897b5d38902207d0d0777`**（#261 gshadow + #260 T2 + #259 T1 + #254–#258 **已合且已部署 live**）
> rollback `pre261_20261031_172351` · 本拍新 PR **不部署**
>
> **后续批次增补（只增不删，原语义不变）**：#265 `20261101gevid`（触发表契约）、
> #266 `20261101gevid3`（本机分钟库探针契约）、**`20261101gthresh`（阈值负锁语义 · §4.1.1 / §11.2，`threshold_neg_lock_v1`，标签仍开、COUNT 仍 6）**。
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
| **证据缺口** | 两族待补证据是否清零 | **已清 2 / 剩 6**（见 §4.1）—— VOLUME 剩 2、AUCTION 仍 4；已清项＝VOLUME `trigger_evidence_table_data_pending`（#265 触发表契约）+ `local_minute_lib_data_pending`（#-A-#9 本机分钟库探针契约 + 运行时路径）。**清零一刀 ≠ 可翻闸**；**落盘 ≠ 可开 TRIGGERING** |

### 4.1 证据缺口现 6 条（已诚实清 2），不是一句「待补」

两族各有一个 `frozenset` 常量，是**代码里就能读到的**缺口清单，不是文档里的口述：

| 族 | 常量 | 条数 | `*_STATUS` |
|---|---|---|---|
| 量能突破 | `VOLUME_BREAKOUT_EVIDENCE_PENDING` | **2** | `data_pending` |
| 竞价异动 | `AUCTION_ABNORMAL_EVIDENCE_PENDING` | **4** | `data_pending` |

本拍（20261101gevid3）清了 VOLUME 标签 `local_minute_lib_data_pending`：
交付本机分钟库路径契约 + 探针薄模块 `local_minute_lib`（version
`local_minute_lib_v1`），约定 `settings.data_dir / "kline_minute"`
（Hive `date=*/part.parquet`），只读 `probe_*`；测例用 `tmp_path` 造最小分区夹具，
**未**把 5.1G 分钟数据塞进仓（运行时 `data/kline_minute` 由 gitignore `data/**` 挡住）。

**已清（VOLUME）**
- `trigger_evidence_table_data_pending` —— 已由触发表契约 + 既有负证据形式化消解；
  **清零 ≠ 可翻闸**；**表 PRESENT ≠ 可开 TRIGGERING**；**strongly_negative ≠ 阈值已锁**
- `local_minute_lib_data_pending` —— 已由本机分钟库探针契约 + 运行时路径（gitignore）消解；
  **清零 ≠ 可翻闸**；**落盘 ≠ 可开 TRIGGERING**；**探针 PRESENT ≠ 可开 TRIGGERING**；
  **路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING**

缺口明细（只读引自 tip，**本拍只清上面 `local_minute_lib_data_pending` 一条，其余不动**）：

**`VOLUME_BREAKOUT_EVIDENCE_PENDING`（仍 2 条）**
- `http_minute_listing_auth_pending` —— 分钟列表鉴权未通
- `threshold_lock_data_pending` —— 量比 / 突破幅度 / 窗口分钟数均未锁定
  （**负锁语义已契约化，见 §4.1.1 / `threshold_neg_lock`**；可钉证据路径清单见 §4.1.1；**仍开 ≠ 已锁**）

**`AUCTION_ABNORMAL_EVIDENCE_PENDING`（本拍不抢，仍 4 条）**
- `auction_price_not_persisted` —— 落盘无 `auction_price` / `prev_close`
- `threshold_lock_data_pending` —— UI 阈值 ≠ 已锁回测门禁（**本拍零碰**；负锁语义在 VOLUME 侧，见 §4.1.1）
- `backtest_evidence_data_pending` —— 无竞价异动回测 / 触发表
- `minute_0925_still_absent` —— 09:25 分钟数据仍缺

> **清掉一条不改变闸的状态。** 四个突破口径的回测收益仍然是
> `-23.86 / -32.44 / -13.80 / -19.81`（**全负**）。触发表存在只说明
> 「我们知道自己过去亏在哪」，不说明「现在可以放量」。
> 负号没解释，表再完整也只是把问题钉在墙上。
>
> **分钟库落盘也不改变闸的状态。** `data/kline_minute` 里有 362 个分区、
> 5.1G、`2025-05-07`…`2026-10-30`，那只说明「本机有这份数据」，
> 不说明「阈值标定了」「负号解释清楚了」「可以放量了」。
> **落盘 ≠ calib ≠ 可放量 ≠ TRIGGERING。**

### 4.1.1 `threshold_lock_data_pending` 是**负锁**，不是「待办」（批次 20261101gthresh）

> **本小节只交付语义契约，不执行清标签。** 标签 `threshold_lock_data_pending`
> **仍开**（VOLUME 与 AUCTION 各一条，两条都不删）；COUNT **仍 6**（已清 2 / 剩 6 不变）；
> **本拍 ≠ 清零刀。**

**披露句（锁面状态）**

| 项 | 状态 |
|---|---|
| 阈值负锁语义锁面 | **PRESENT** —— `threshold_neg_lock`（`threshold_neg_lock_v1`） |
| `threshold_lock_data_pending`（VOLUME） | **仍开**，仍在 `VOLUME_BREAKOUT_EVIDENCE_PENDING` 里 |
| `threshold_lock_data_pending`（AUCTION） | **仍开**，本拍零碰 |
| COUNT（`TRIGGER_GATE_EVIDENCE_PENDING_COUNT`） | **仍 6**（派生，VOLUME 2 + AUCTION 4） |
| 两族 `*_STATUS` | **仍 `data_pending`** |
| 两 `*_TRIGGERING_ENABLED` / `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` | **仍 `False`** |

**为什么「负锁」要跟「待办」分开说**

这条标签和 `http_minute_listing_auth_pending` 并排放在同一个 `frozenset` 里，
但两者缺的东西不一样：

| 标签 | 缺的是什么 | 补齐后能不能清 |
|---|---|---|
| `http_minute_listing_auth_pending` | 一个**外部凭证** | 拿到凭证、跑通，就可以考虑清 |
| `threshold_lock_data_pending` | **一个解释** | 负号解释清楚之前，**补什么都不能清** |

前者是「还没到」，后者是「到了也不能用」。四个突破口径的回测收益
`-23.86 / -32.44 / -13.80 / -19.81` 全负 —— 在这条负号被解释掉之前，
量比 / 突破幅度 / 窗口分钟数标定出来的只会是**更精确的亏损**。
所以越确定 `strongly_negative`，阈值这一格**越该锁着**。

**可钉证据路径清单（研究档 · 本拍不执行清标签）**

下面是「将来另一个口令才**可能考虑**清这条标签」的前置地图。
**逐条满足 ≠ 自动可清 ≠ 可翻闸** —— 每一条都只是「有资格被讨论」，不是「已成立」。

| # | 前置 | 现状 | 说明 |
|---|---|---|---|
| 1 | 负号可解释 | **未成立** | 需要一个能说明「四个口径为什么全负、负在哪」的研究口径（不是「市场不好」这类空话）；解释不清 → 阈值无论怎么钉都是伪的 |
| 2 | 研究档阈值标定（若采用）须另口令明示 | **未发生** | 即便只做研究档标定，也必须另口令写明「这不是可交易锁」；**不得**静默冒充可交易阈值 |
| 3 | 真可交易标定的前置已另拍闭合 | **未闭合** | 分钟列表鉴权（`http_minute_listing_auth_pending`）等硬前置仍在；本机分钟库已落盘 ≠ 鉴权已通 |
| 4 | 人读「均未锁定」被诚实改写的事实基础 | **未具备** | 现在这句「均未锁定」是准确的；要改写它，得先有 1–3 的事实，否则就是改文案冒充进度 |
| 5 | 测与 COUNT 派生同步 | **长期要求** | 清一条就同步文档 / 锁面 NOTE / 测期望；COUNT 永远派生、绝不手抄；清完仍非空时断言照过，靠人同步 |

**这张清单不能被当成授权。** 它是「清标签这件事有多难」的地图，
不是「照着做完就能清」的施工图。**在另口令之前，任何一条「已满足」的口头结论
都不构成清标签的理由。**

**大字**

- **负锁 ≠ 删标签**
- **钉数 ＝ fabricate**（量比 / 突破% / 窗口分钟数一旦钉值，钉出来的不是标定）
- **本拍 ≠ 清零刀**
- **清零一刀 ≠ 可翻闸**
- **语义锁面 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING**
- **strongly_negative ≠ 阈值已锁**
- **FreqLockStamp ≠ 可清标签理由**（写下数值 ≠ 证据支持）
- **门禁契约 ≠ 授权翻闸**；**合入 ≠ 部署**

模块侧对应：`threshold_neg_lock.assert_threshold_lock_still_pending` 咬住「标签必须还在」，
`assert_no_tradeable_threshold_nail` 咬住「禁钉可交易数」，
`assert_strongly_negative_does_not_lock_threshold` / `assert_freq_lock_stamp_is_not_clear_reason`
把两条交叉写成**能被测例调用**的边 —— 写在 docstring 里的话改代码时没人看见，写在断言里的话会红。

### 4.2 为什么把缺口条数写进文档

因为「证据缺口仍在」这句话太软，读者会当成客套。**现 6 条、已清 2 条**是一个可数的数字：

- 它说明**不是差一点**，是还差 6 件具体的事，每件都有名字
- 它可被断言（本锁面只读引用这两个集合，断言 `*_STATUS == "data_pending"` 且两集合非空）
- 缺口被整族清空时，`assert` 会提醒「契约描述过时了」，而不是让文档慢慢腐烂

**但断言抓不住"只清了一两条"。** 清掉 VOLUME 那两条之后，VOLUME 仍非空 2 条、
STATUS 仍 `data_pending`、COUNT 仍是派生值 —— `assert_evidence_pending_still_open`
**照过**。所以「8 变 7 变 6」这件事必须靠人同步文档、锁面 NOTE 与测例期望。
把它当成自动兜底，就会在真清空之前一直读到过期的"0/8"。

**清零进度看板"绿" ≠ 可翻闸。** 缺口条数是一个**披露**指标，不是**授权**指标：
它变小的唯一含义是"文档与代码更诚实了"。**落盘 ≠ 可开 TRIGGERING**。

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
| `volume_breakout_stub` | `VOLUME_BREAKOUT_TRIGGERING_ENABLED is False` + `EVIDENCE_PENDING`（**2** 条）+ `STATUS=="data_pending"` | 只读，**不改取值**；本拍**只在人读说明增补负锁一句**，**不删 pending 键** |
| `auction_abnormal_stub` | `AUCTION_ABNORMAL_TRIGGERING_ENABLED is False` + `EVIDENCE_PENDING`（4 条）+ `STATUS=="data_pending"` | 只读，**不改取值** |
| `first_seen_audit` | `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False` | 只读，**不翻** |
| `shadow_obs_lock` | `SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"`；默认关 | 只读交叉，**不重写规则** |
| `tier_cut_lock` | `TIER_CUT_RULE_VERSION == "tier_cut_v1"` | 只读交叉 |
| `tier_display_lock` | `TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"` | 只读交叉 |
| `constants` | `DIRECTION_LABELS` 三值；`BONFERRONI_M == 6` | **零碰** |
| `threshold_neg_lock` | `THRESHOLD_NEG_LOCK_VERSION == "threshold_neg_lock_v1"`；标签仍须在 pending；禁钉可交易数 | 本拍**新建**；只读引用 stub / 触发表 / freq_lock，不 import 后两者 |

**不算行情、不改 stub 取值、不调度、不真推、不重写 T1/T2/shadow_obs。**

---

## 11. 锁面模块

见 [`backend/app/buy_sell_signals/trigger_gate_lock.py`](../backend/app/buy_sell_signals/trigger_gate_lock.py)。

纯常量 + 纯断言，**无写路径 I/O**：不读行情、不写文件、不碰 `HitStore`、不调推送、无调度。断言助手由测例调用，**模块 import 时不硬 raise**。

### 11.1 缺口清单**派生**而非复制

锁面里 `TRIGGER_GATE_EVIDENCE_PENDING_COUNT` 由两个 `*_EVIDENCE_PENDING` **只读求和**，不写 `8` / `7` / `6` 这个字面量。理由同 §4.2：手抄的数字会在缺口被补掉后继续骗人 —— 批次 20261101gevid 清了 VOLUME 一条（8 → 7）、批次 20261101gevid3 又清了一条（7 → 6），这个常量**两次都没改一行代码**。

### 11.2 阈值负锁语义锁面（批次 20261101gthresh）

见 [`backend/app/buy_sell_signals/threshold_neg_lock.py`](../backend/app/buy_sell_signals/threshold_neg_lock.py)（`threshold_neg_lock_v1`）。

**这一层不是缺口清单，是"某条缺口为什么必须继续待着"的契约。** §11 的锁面回答「还剩几条」，
这里回答「其中 `threshold_lock_data_pending` 那一条，不许被顺手清掉」。

| 常量 / 断言 | 咬住什么 |
|---|---|
| `THRESHOLD_LOCK_TAG_MUST_REMAIN = "threshold_lock_data_pending"` | 这条标签的名字，被写成一个**可被引用的名字**，而不是散在文档里的字符串 |
| `assert_threshold_lock_still_pending(pending)` | 标签**必须还在** pending 里；不在 → raise（`ThresholdTagRemovedError`，继承 `AssertionError`） |
| `assert_no_tradeable_threshold_nail(...)` | 量比 / 突破% / 窗口分钟数**禁止**被钉成「已锁定」；任一非空 → raise（`TradeableThresholdNailError`） |
| `assert_strongly_negative_does_not_lock_threshold(verdict)` | 触发表 `strongly_negative` **不**构成阈值已锁 |
| `assert_freq_lock_stamp_is_not_clear_reason(has_freq_lock_stamp=...)` | 有 `FreqLockStamp` **不是**清标签的理由（写下数值 ≠ 证据支持） |

**本模块刻意不 import `freq_lock` / 不从 `volume_breakout_evidence_table` import verdict。**
交叉是语义层面的，不是符号层面的 —— 为了一条断言去 import 一整个模块，会让"零碰"
变成"零碰但依赖"；而跟着那边改名，等于"表变了规矩也自动变"，那这条规矩就没人守了。
两边取值相等由测例咬住，而不是由 import 咬住。

**version 未 bump 的说明**：`TRIGGER_GATE_LOCK_VERSION` 本拍**显式不 bump**，
仍为 `trigger_gate_v1`（同 #265 / #266）。理由：bump 会迫使
`schema_push_lock` / `schema_push_live_lock` 的 `EXPECTED_TRIGGER_GATE_LOCK_VERSION`
同步改字面量，而那两个文件本拍标零碰 —— 为一个披露变更去动 schema 门禁常量，
风险大于收益。**未 bump ≠ 未披露**：`TRIGGER_GATE_LOCK_NOTE` 已加旁注，
本文件 §4.1.1 已写全，测例 T3 钉住负锁 version 非空。交叉 bump 留另拍同步。

**语义锁面 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING。** 这个模块在场，
恰恰说明这一格**还没清**。

---

## 12. 建议路径（非本拍必须交付）

- 翻闸前核对清单自动化（把 §4 表变成可跑脚本）→ 可挂建议；**≠ 已授权翻闸**
- 完整触发引擎 / 调度 / 触发表 → **另口令**
- schema 真推 / 真推旁注落地 → **另口令**
- 缺口清零进度看板 → 可挂建议；**看板绿 ≠ 可翻闸**

---

> **hits schema 真推门禁契约已开（批次 20261031gschema）**：门禁契约见
> [buy-sell-signal-schema-push.md](./buy-sell-signal-schema-push.md)
> （锁面 `schema_push_lock.py`）。本文件规则正文语义不变；schema 门禁只读交叉本门禁，
> **不是**授权真推 live，**不是**授权翻闸，**不是**可放量。

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*
