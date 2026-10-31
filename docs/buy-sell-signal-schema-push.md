# 买卖信号 · hits schema 真推门禁契约

> 批次 20261031gschema · P-bss-schema-push · #-A-#9 / gtrig N5 下一档
> 锁面版本：`schema_push_v1` · `HITS_SCHEMA_VERSION` 仍 `"1"` · 两 TRIGGERING 默认 False · COMPLETE 仍 False
> 只读交叉：`trigger_gate_v1` / `shadow_obs_v1` / T1 `tier_cut_v1` / T2 `tier_display_v1`
> tip = prod = `5569c8b50204cac8d656e85fb6be45f3cb869850`（#262+#261+#260+#259… live）；rollback `pre262_20261031_191850`
> **仅供研究，不构成投资建议；执行须人工确认。** 红涨绿跌。成本来回 18.2bp。

---

## 1. 产品语义

本文件是 hits schema 的**真推门禁契约**：写清「什么时候可以讨论把命中写进 schema / 与 live 钉钉模板的边界在哪 / 哪些事不许做」。

**它是三件互不相同的事，这里只做第一件的一半：**

| 事项 | 是什么 | 本拍 |
|---|---|---|
| 门禁**契约** | 把「讨论真推进 schema 前必须核对什么」写清楚、可验 | ✅ 本拍交付 |
| 真推 **live** | 真把命中按 live 模板发到钉钉、真 bump `HITS_SCHEMA_VERSION` | ❌ **另口令** |
| 完整 **schema 迁移** | 存量数据迁移、回填、旧版本兼容层 | ❌ **另口令** |

所以本文件**不是**立刻真推上线；**不是**完整 schema 迁移；**不是**授权真推 live；**不是**授权翻闸。

写清门槛不等于跨过门槛。这是本条契约最容易被误读的地方，也是本轮唯一要防的事。

---

## 2. 现状锚（基础设施 PRESENT ≠ 门禁契约已齐）

tip 上这些都在，且都是**只读**对待：

| 锚 | tip 事实 | 本拍 |
|---|---|---|
| `HITS_SCHEMA_VERSION` | `constants.py` `Final = "1"` | **只读断言仍 `"1"`**；**不 bump** |
| `hits_models` | schema_version 校验 · 禁价量仓单 · DIRECTION 封闭 | **零碰**字段集 |
| `hits_dingtalk` | 显式 `try_push_hit` · **不自动扫** · P3a 闸 · 独立 webhook | **零碰**语义，尤其 `build_hit_dingtalk_text` |
| 三 env | `BUY_SELL_SIGNALS_PERSIST` / `PUSH` / `DINGTALK` | **零碰**（含生产值） |

**schema 基础设施在场，不等于门禁契约层已齐。** 前者是「能力存在」，后者是「门槛写清」。
本拍补的是后者。把前者读成后者，就会得出「已经可以真推了」—— 这个结论没有任何一句话支持它。

---

## 3. 真推另口令

本拍及本 PR：

- **不得**改 `build_hit_dingtalk_text` 主路径语义；
- **不得** bump `HITS_SCHEMA_VERSION` 当真推上线；
- **不得**把真推旁注落地代码塞进本 PR。

真推 / 旁注落地须**所有者明示口令 + 另 ALIGN/FIX**。gtrig N5 与 X4 都已写死这一点，本文件不重开该边界，只是把它挂到 schema 这一侧。

> **schema 门禁契约 ≠ 授权真推 live。**
> **门禁契约 ≠ 授权翻闸。**

---

## 4. 前置条件表（表 ≠ 已可真推）

下表是「**讨论**真推 / 进 schema 之前要核对什么」。**它不是「本拍已满足可真推」的清单**，也不是「打完勾就能上线」的作业单。

| 项 | 核对含义 | 本拍状态（参考） |
|---|---|---|
| 路径已通 | 主链 / 观测口 / 门禁路径是否在场 | 路径在场 **≠ 可真推** |
| calib | 校准是否过门 | calib 过 **≠ 可真推** |
| K8 | 产线 K8 | K8 PASS **≠ 可放量 ≠ 可开 TRIGGERING**，更 ≠ 可真推 |
| COMPLETE | A2 完成门 | **仍 False**；本拍不翻 |
| trigger_gate | 门禁在场且两 TRIGGERING False | 在场 **≠ 可翻闸 ≠ 可真推** |
| 影子观测口 | 契约在场且默认关取向 | 默认关 **≠ 放量**；影子 **≠ live** |
| 三 env | PERSIST / PUSH / DINGTALK | 本拍零碰；生产值未擅改 |
| P3a 闸 | `VisibilityReject.NON_LIVE_MODE` / 无 `allow_shadow` | 仍绿；不放宽 |
| schema 基础设施 | version 仍 `"1"` · models · dingtalk | PRESENT **≠ 门禁契约 = 可真推** |

这张表的价值在于**否决**：任何一行没核清，就不该往下讨论。它不提供任何一行的「已核清即通行」。
表里没有一行的状态写着「已可真推」—— 因为本拍没有那个状态。

---

## 5. 与 P3a / shadow / trigger_gate / live 模板

四层边界，从里到外，**每往外一层都要另口令**：

```
P3a 闸（可见性/推送准入）  →  shadow 观测口（默认关）  →  trigger_gate 门禁  →  schema 真推（live）
   已合 ≠ 可放影子进钉钉      写入 ≠ 观测口 ≠ 放量         契约 ≠ 授权翻闸       契约 ≠ 授权真推 live
```

- **影子 ≠ live**：影子命中的形状与生产命中一致，不等于它已经进了钉钉。
- **P3a 已合 ≠ 可放影子进钉钉**：闸存在只说明「能拦」，不说明「该放」。
- **门禁契约 ≠ 授权翻闸**：trigger_gate 契约把门槛写清了，门槛没降。
- **schema 门禁 ≠ 授权真推**：本文件同理。
- **live 模板主路径本拍零碰**：`build_hit_dingtalk_text` 语义不变，不加旁注、不改格式、不改去重与冷却。

---

## 6. 方向与禁词

方向仍 `DIRECTION_LABELS` = `{research_buy, research_sell, research_watch}` —— **三值，`research_watch` 仍在**。
本拍不改 DIRECTION，不删 `research_watch`，不新增方向。

**禁六词动作指令式**（及同义）：可以买入 / 建议买入 / 强烈买入 / 可以卖出 / 建议卖出 / 强烈卖出。
也禁 买 / 卖 / 加仓 / 减仓 / 开多 / 开空 这类动作词。

**禁价量仓单**：schema 与载荷内**不得**写价格、数量、仓位、订单字段。
schema 存的是「研究了什么、结论方向、时点」，不是「去下什么单」。

方向是 `research_*`，不是买卖指令。把研究结论写成动作指令，读者会把它当操作建议 —— 这套东西从来没有那个授权。

---

## 7. 门禁（大字）

- 路径已通 **≠** calib **≠** 可放量 **≠** TRIGGERING
- K8 PASS **≠** 可放量 **≠** 可开 TRIGGERING
- 讨论 **≠** 派单
- 影子写入 **≠** 观测口 **≠** 放量
- 合入 **≠** 部署（tip = prod，已记）
- COMPLETE 仍 False **≠** 可翻 COMPLETE
- **门禁契约 ≠ 授权翻闸**
- **schema 门禁契约 ≠ 授权真推 live**

---

## 8. Bonferroni

门禁边**不占** `m = 6`。展示边、门禁边、schema 边都不吃主预算。

但「不占 m」**不是**永久豁免：它只是说这条边现在不在主预算里，不构成「因此可以放量 / 因此可以真推」的理由。
本拍**不改** `BONFERRONI_M`；禁止把门禁边冒充成永久豁免来支撑可放量或可真推的结论。

---

## 9. 挂点与另口令

挂点 ＝ 本契约文档 + 薄锁面 [`schema_push_lock.py`](../backend/app/buy_sell_signals/schema_push_lock.py)（可验）。

| 事项 | 处置 |
|---|---|
| 真推 live 模板落地 / bump `HITS_SCHEMA_VERSION` / 完整迁移 | **另口令** |
| 调度 / crontab / lifespan 扫写 hits | **另口令** |
| 真翻 `*_TRIGGERING_ENABLED` / 可放量开闸 | **另口令** |
| 翻 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` / 改三 env | **禁止冒充 / 另口令** |
| Opt-A / auction·17–19 实施 / PanWatch / 做T | **另口令 / 零碰** |

---

## 10. 与既有接线

只读交叉，不重定义：

| 交叉对象 | 只读取其什么 |
|---|---|
| `constants.HITS_SCHEMA_VERSION` | 仍 `"1"` |
| `volume_breakout_stub.VOLUME_BREAKOUT_TRIGGERING_ENABLED` | 仍 `Final = False` |
| `auction_abnormal_stub.AUCTION_ABNORMAL_TRIGGERING_ENABLED` | 仍 `Final = False` |
| `first_seen_audit.EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` | 仍 `Final = False` |
| `trigger_gate_lock.TRIGGER_GATE_LOCK_VERSION` | `trigger_gate_v1` |
| `shadow_obs_lock.SHADOW_OBS_LOCK_VERSION` | `shadow_obs_v1`（且默认关取向） |
| `tier_cut_lock.TIER_CUT_RULE_VERSION` | `tier_cut_v1` |
| `tier_display_lock.TIER_DISPLAY_LOCK_VERSION` | `tier_display_v1` |
| `constants.DIRECTION_LABELS` / `BONFERRONI_M` | 三值 / `6` |

**不改 models / 模板、不 bump schema、不算行情、不调度、不真推、不重写既有锁面。**
任何一处版本漂移都会被锁面当场炸出来 —— 那是提醒「契约描述的世界已经变了」，不是提醒「改坏了」。

---

## 11. 锁面模块

见 [`backend/app/buy_sell_signals/schema_push_lock.py`](../backend/app/buy_sell_signals/schema_push_lock.py)。

纯常量 + 纯断言函数：不读行情、不写文件、不碰 `HitStore`、不调推送、无调度、无 I/O 写路径。
断言由测例调用，**模块 import 时不硬 raise**。

全模块用**模块别名**（`from . import constants as _c` 再 `_c.HITS_SCHEMA_VERSION`），
**不是** `from .constants import HITS_SCHEMA_VERSION`。快照式 import 在 import 期把值拷进本模块命名空间，
之后 monkeypatch 改源模块属性时本模块副本纹丝不动 —— 负控会假绿，断言退化成永远为真的守卫。

---

## 12. 建议路径（非本拍必须交付）

- 真推前核对清单自动化（把 §4 表变成可跑脚本）→ 可挂建议；**≠ 已授权真推**
- 缺口清零与 schema 边进度看板 → 可挂建议；**看板绿 ≠ 可真推**
- 真推 live 模板落地 / bump schema / 完整 schema 迁移 → **另口令**

---

> **hits schema 真推 live 开档已开（批次 20261031gschemalive）**：开档契约见
> [buy-sell-signal-schema-push-live.md](./buy-sell-signal-schema-push-live.md)
> （锁面 `schema_push_live_lock.py`；旁注「研究档 · 未放量」已落到模板）。本文件门禁正文语义不变；
> **开档 ≠ 可放量 ≠ 翻闸 ≠ 已有 live 写入**；schema 仍 `"1"`（不 bump）；`try_push_hit` 门序零碰。

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*
