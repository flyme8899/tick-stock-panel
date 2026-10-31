# 买卖信号 · hits schema 真推 live 开档契约

> 批次 20261031gschemalive · P-bss-schema-live · #-A-#9 / gschema #263 下一档
> 锁面版本：`schema_push_live_v1` · 旁注「研究档 · 未放量」· `HITS_SCHEMA_VERSION` 仍 `"1"`
> 两 TRIGGERING 默认 False · COMPLETE 仍 False
> 只读交叉：`schema_push_v1` / `trigger_gate_v1` / `shadow_obs_v1` / T1 `tier_cut_v1` / T2 `tier_display_v1`
> tip = prod = `5ca4409802a0ee9078f2fb345f5b7821bad67964`（#263+#262+#261+#260+#259… live）；rollback `pre263_20261031_220404`
> **仅供研究，不构成投资建议；执行须人工确认。** 红涨绿跌。成本来回 18.2bp。

---

## 1. 产品语义

本文件是 hits schema 真推 live 的**开档/落地层**契约：把「真推之前要核什么」写成表、把「真推必须带什么」落到模板、把「哪些事不许做」焊成断言。

**开档 ≠ 跑起来。** 这是本条最容易被读错的地方：

| 事项 | 是什么 | 本拍 |
|---|---|---|
| **开档 / 落地层** | 前置可核面 + 旁注落地 + 薄锁面可验 | ✅ **本拍交付** |
| bump `HITS_SCHEMA_VERSION` | 真把 schema 版本推上去 | ❌ **另口令** |
| 新增生产写 `mode=live` 路径 | 让命中真的落盘成 live | ❌ **另口令**（本拍不新增） |
| 调度 / crontab / lifespan 扫写 | 定时自动跑 | ❌ **另口令** |
| 翻 `*_TRIGGERING_ENABLED` | 真开触发 | ❌ **另口令** |

所以本文件**不是** bump schema；**不是** 新增 live 写入/调度；**不是** 翻 TRIGGERING；**不是** 可放量。

> **开档/旁注落地 ≠ 可放量 ≠ 翻闸 ≠ 已有 live 写入。**

owner「开：schema 真推 live」授权的正是**开档**这一层 —— 把门槛写成可核表、把旁注落到
模板、把边界写成断言。它不授权跨过门槛，也不代表生产已经在写 live。

---

## 2. 现状锚（门禁 PRESENT ≠ 开档层已齐）

| 锚 | tip 事实 | 本拍 |
|---|---|---|
| `schema_push_v1` / `docs/buy-sell-signal-schema-push.md` | **PRESENT**（#263 已 live） | **只读交叉**；文末仅追加旁链 |
| `HITS_SCHEMA_VERSION` | `constants.py` `Final = "1"` | **只读断言仍 `"1"`**；**不 bump** |
| `hits_models` | schema_version 校验 · 禁价量仓单 · DIRECTION 封闭 | **零碰字段集** |
| `hits_dingtalk.build_hit_dingtalk_text` | 原 **无**「研究档 · 未放量」 | **仅追加旁注行**（本拍唯一业务代码触碰） |
| `try_push_hit` | 门序完整；显式调用、不自动扫、P3a 闸 | **零碰门序** |
| 生产写 `mode=live` 路径 | **ABSENT** | **本拍不新增** |

**schema 门禁（#263）在场，不等于开档层已齐。** 前者是「门槛写清楚了」，后者是「真推之前
该核的表、该带的旁注、该验的边界，都到位了」。本拍补的是后者。

一个佐证：T1 `tier-cut.md`、T2 `tier-display.md`、R2-3 `shadow-obs.md` 三份文档**早就**
写着「真推须带旁注『研究档 · 未放量』」—— 但这句话此前**只存在于文档**，
`build_hit_dingtalk_text` 里没有。契约里写了、代码里没有，是最典型的「文档在描述一个
不存在的世界」。本拍把它落地。

---

## 3. 前置核对清单（表齐 ≠ 可放量 ≠ 可翻闸 ≠ 可调度写 live）

下表是「讨论真推 / 调度写 live 之前要核对什么」。**它是否决清单，不是通行证**：
任何一行没核清就不该往下走；但**全部核清也不等于可以放量、可以翻闸、可以调度写 live**。

| 项 | 核对含义 | 本拍状态（参考） |
|---|---|---|
| 路径已通 | 主链 / 观测口 / 门禁路径是否在场 | 路径在场 **≠ 可放量** |
| calib | 校准是否过门 | calib 过 **≠ 可放量** |
| K8 | 产线 K8 | K8 PASS **≠ 可放量 ≠ 可开 TRIGGERING** |
| COMPLETE | A2 完成门 | **仍 False**；本拍不翻 |
| trigger_gate | 门禁在场且两 TRIGGERING False | 在场 **≠ 可翻闸** |
| 影子观测口 | 契约在场且默认关取向 | 默认关 **≠ 放量** |
| 三 env | PERSIST / PUSH / DINGTALK | 本拍零碰；生产值未擅改 |
| P3a 闸 | `NON_LIVE_MODE` / 无 `allow_shadow` | 仍绿；不放宽 |
| schema 基础设施 | version `"1"` · models · dingtalk | PRESENT **≠ 可写 live** |
| schema_push 门禁 | `schema_push_v1` PRESENT | 门禁齐 **≠ 授权真推放量** |
| 真推旁注 | 模板是否已带「研究档 · 未放量」 | 本拍已落地；**旁注在场 ≠ 可放量** |

这张表的价值在于**否决**。表里没有任何一行的状态写着「已可放量」或「已可写 live」——
因为本拍没有那个状态。

---

## 4. 与 hits_models / build_hit_dingtalk_text / try_push_hit 的边界

三层，改动权限逐层收紧：

```
hits_models（字段集）   →   build_hit_dingtalk_text（文案）   →   try_push_hit（门序）
   零碰                        仅追加旁注一行                     零碰
```

| 对象 | 本拍处置 | 为什么 |
|---|---|---|
| `hits_models` | **零碰字段集** | 加字段就是改 schema 形状，属 bump 范畴 |
| `build_hit_dingtalk_text` | **仅追加**旁注「研究档 · 未放量」 | 旁注是内容定性，不是新字段 |
| `try_push_hit` | **零碰门序 / 冷却 / webhook** | 旁注经模板透传即可，无需动门 |

**旁注的最小触碰，具体是不碰这些：**

- 不改 title 前缀与 64 字截断语义
- 不增删禁字段（价量仓单）
- 不改冷却 / dedupe key / 在途标记
- 不改 webhook 发送逻辑、失败重试、缺配拒发
- 不引入 `allow_shadow`
- 不引入六词动作指令式
- 旁注里不写价格 / 数量 / 仓位 / 订单

旁注文案**固定**、不接受参数、不做拼接。可配置的旁注等于没有旁注 —— 哪天有人传空串，
模板就静默退化成"无旁注"，而没有任何一处会报错。

---

## 5. 方向与禁词

方向仍 `DIRECTION_LABELS` = `{research_buy, research_sell, research_watch}` —— **三值，`research_watch` 仍在**。
本拍不改 DIRECTION、不删 `research_watch`、不新增方向。

**禁六词动作指令式**（及同义）：可以买入 / 建议买入 / 强烈买入 / 可以卖出 / 建议卖出 / 强烈卖出。
也禁 买 / 卖 / 加仓 / 减仓 / 开多 / 开空 这类动作词。

**禁价量仓单**：schema、载荷、**以及旁注本身**都不得写价格、数量、仓位、订单字段。

方向是 `research_*`，不是买卖指令。把研究结论写成动作指令，读者会把它当操作建议 ——
这套东西从来没有那个授权。

旁注尤其要干净：它是本拍**唯一新增的文字**。如果禁项只查载荷不查旁注，旁注就成了
绕过禁词的合法入口。

---

## 6. 门禁（大字）

- 路径已通 **≠** calib **≠** 可放量 **≠** TRIGGERING
- K8 PASS **≠** 可放量 **≠** 可开 TRIGGERING
- 讨论 **≠** 派单
- 影子写入 **≠** 观测口 **≠** 放量
- 合入 **≠** 部署（tip = prod，已记）
- **门禁契约 ≠ 授权翻闸**
- **schema 门禁契约 ≠ 授权真推 live**
- **开档/旁注落地 ≠ 可放量 ≠ 翻闸 ≠ 已有 live 写入**
- COMPLETE 仍 False **≠** 可翻 COMPLETE

---

## 7. Bonferroni

开档边 / 旁注边**不占** `m = 6`。展示边、门禁边、schema 边、开档边都不吃主预算。

但「不占 m」**不是**永久豁免：它只是说这条边现在不在主预算里，不构成「因此可以放量 /
因此可以翻闸」的理由。**开档/旁注冒充永久豁免来支撑可放量或可翻闸，视同越界。**

本拍**不改** `BONFERRONI_M`。

---

## 8. 挂点与另口令

挂点 ＝ 本开档契约文档 + 旁注落地 + 薄锁面 [`schema_push_live_lock.py`](../backend/app/buy_sell_signals/schema_push_live_lock.py)（可验）。

| 事项 | 处置 |
|---|---|
| bump `HITS_SCHEMA_VERSION` | **另口令** |
| 完整 schema 迁移（存量回填 / 兼容层） | **另口令** |
| 调度 / crontab / lifespan 扫写 live | **另口令** |
| 新增生产写 `mode=live` 路径 | **另口令**（本拍不新增） |
| 真翻 `*_TRIGGERING_ENABLED` / 可放量开闸 | **另口令** |
| 翻 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` / 改三 env | **禁止冒充 / 另口令** |
| Opt-A / auction·17–19 实施 / PanWatch / 做T | **另口令 / 零碰** |

---

## 9. 与既有接线

只读交叉，不重定义：

| 交叉对象 | 只读取其什么 |
|---|---|
| `schema_push_lock.SCHEMA_PUSH_LOCK_VERSION` | `schema_push_v1` |
| `constants.HITS_SCHEMA_VERSION` | 仍 `"1"` |
| `volume_breakout_stub.VOLUME_BREAKOUT_TRIGGERING_ENABLED` | 仍 `Final = False` |
| `auction_abnormal_stub.AUCTION_ABNORMAL_TRIGGERING_ENABLED` | 仍 `Final = False` |
| `first_seen_audit.EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` | 仍 `Final = False` |
| `trigger_gate_lock.TRIGGER_GATE_LOCK_VERSION` | `trigger_gate_v1` |
| `shadow_obs_lock.SHADOW_OBS_LOCK_VERSION` | `shadow_obs_v1`（且默认关取向） |
| `tier_cut_lock.TIER_CUT_RULE_VERSION` | `tier_cut_v1` |
| `tier_display_lock.TIER_DISPLAY_LOCK_VERSION` | `tier_display_v1` |
| `constants.DIRECTION_LABELS` / `BONFERRONI_M` | 三值 / `6` |

**不改 models 字段集、不 bump schema、不算行情、不调度、不写 live、不重写既有锁面、不改 `try_push_hit` 门序。**

---

## 10. 锁面模块

见 [`backend/app/buy_sell_signals/schema_push_live_lock.py`](../backend/app/buy_sell_signals/schema_push_live_lock.py)。

纯常量 + 纯断言函数：不读行情、不写文件、不碰 `HitStore`、不调推送、无调度、无 I/O 写路径。
断言由测例调用，**模块 import 时不硬 raise**。

全模块用**模块别名**（`from . import constants as _c` 再 `_c.HITS_SCHEMA_VERSION`），
**不是** `from .constants import HITS_SCHEMA_VERSION`。快照式 import 在 import 期把值拷进本模块，
之后 monkeypatch 改源模块属性时本模块副本纹丝不动 —— 负控会假绿。

**锁面不 import `hits_dingtalk`。** 锁面是契约层，不该依赖有网络/IO 的运行时模块。
旁注一致性（模板那行字面量 ↔ 本锁面常量）**由测例交叉咬住**，不靠 import 图保证。
两处一旦分叉，测试当场红。

---

## 11. 建议路径（非本拍必须交付）

- 真推前核对清单自动化（把 §3 表变成可跑脚本）→ 可挂建议；**≠ 已授权真推**
- bump schema / 完整迁移 / 调度写 live → **另口令**
- 真翻 TRIGGERING / 可放量开闸 → **另口令**
- 旁注按档位细分（弱/中/强）→ 属 T2 展示层语义变更，**另口令**

---

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*
