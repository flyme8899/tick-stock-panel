"""轨②子定义命名 + Bonferroni 预算表(批次 20261030ga1 · P-bss-a1 · -A-#11 A1)。

## 这个模块做什么

它**只钉命名与预算**。给出一组写死的 signal_id 串、一条前缀规则、一张 6 格预算
占用表, 以及三个可在测例里直接调用的不变量检查。

它**不**实现任何评价器、**不**回放、**不**写 hits、**不**写影子分桶。MACD 怎么算、
KDJ 怎么算, 属信号层, 是本拍之后的事 —— **数据层 ≺ 信号层**。

## 前缀规则(写死)

| 前缀 | 用途 | 谁能用 |
|---|---|---|
| `bss.daily_quality_value` | **D1 祖父键**(#9 / #241) | 唯一定义, **禁止改名** |
| `bss.day.<slug>` | 轨②新 D2 子定义 | D2a / D2b |
| `bss.evt.<slug>` | 轨①新 E 定义 | E1 **仅预算占位** |
| `fx.*` | 夹具 only | **永不**占生产预算格 |
| 无点桩名(`volume_breakout` / `auction_abnormal`) | 历史 `minute_event` 桩 | **不入**本预算四格 |

D1 用的是**完整键**而不是 `bss.daily.` 前缀族。这不是疏漏: D1 已经用
`bss.daily_quality_value` 落了 calib 三键, 换前缀等于换键, 已写出的样本会突然
"找不到" —— 文件还在磁盘上, 只是没人按新路径去读它。所以 D1 单独冻结, D2 才另开
`bss.day.` 族。两族并存看着不整齐, 但它诚实地反映了一段历史: D1 先来, 前缀规范后到。

## D1 冻结

```
signal_id    = bss.daily_quality_value    # 禁止改名
family       = daily_fundamental
event_class  = daily_quality_value
version      = v1
```

这四串与 `daily_fundamental_definition.REAL_DAILY_*` 逐字相同, 测例直接对两者做
同串断言。**抄一份在这里是故意的**: 若哪天有人改了那四个常量, 本模块的断言会红,
于是"改名"这件事就不会悄悄发生。

## D2a / D2b —— 命名位, 不是实现

| 键 | 家族 | event_class | 含义 |
|---|---|---|---|
| `bss.day.macd_hist_cross` | `daily_fundamental` | `daily_macd` | 日线 MACD 柱穿越 |
| `bss.day.kdj_gold_cross` | `daily_fundamental` | `daily_kdj` | 日线 KDJ 金叉 |

**D2 不骑 D1。** 同 `family`, 但**不同** `signal_id`、**不同** calib 记录。
禁止读 / 继承 D1 的 calib passed 来冒充 D2 过闸。理由很直白: calib 是按定义算的,
D1 的口径过了, 只能说明 D1 的口径过了; 拿它给 D2 背书, 等于用一份体检报告证明
另一个人健康 —— 而这份报告还盖着章, 看起来能过。

本模块对 D2 只做三件事: 给键名、给 event_class、给"不骑 D1"的检查函数。

## E1 —— 只占预算格

`bss.evt.overnight_announce` 占下第 4 格, 仅此而已:

- **不**实现信号
- **不**为它设生产用的 `event_class` 常量(E 定义另口令; 现在新增一个没有评价器的
  `event_class`, 只会让 `EVENT_CLASSES` 里多出一个永远不会有命中的值)
- **不**写影子

事件轨写影子仍受 A2 的 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` 门(默认 `False`,
本拍零碰)。预算占位 ≠ 定义 ≠ 可写。

## Bonferroni 预算(6 格)

```
BONFERRONI_M = 6                      # 全局; 本拍不改数值
BONFERRONI_SCHEME_ID = independent_family_v1   # 本拍不 bump
```

| 格 | 角色 | 状态 |
|---|---|---|
| 0 | D1 | `occupied` |
| 1 | D2a | `reserved` |
| 2 | D2b | `reserved` |
| 3 | E1 | `reserved_budget_only` |
| 4 | — | `empty` |
| 5 | — | `empty` |

占用 + 预留 = **4**, 留白 = **2**。

`m = 6` 是**分母**: α_family = 0.05 / 6。它把"将来算 p 值时该除以几"固定下来。
占用表是**分子侧**的账: 这 6 格里, 哪几格已经有主。两者一起才说得清"现在这套
多重检验口径承担了几只信号"。

### 第 7 个: 必须先 bump scheme

预算满了再加一只, 有两种做法:

1. **bump** `BONFERRONI_SCHEME_ID`(如 `independent_family_v2`), 让
   `design_version()` 随之可见变化 —— 于是新旧两批样本在任何按版本对齐的地方都
   自动分开
2. **静默**塞进第 7 格 —— 分母还是 6, 但实际算了 7 只

第 2 种不会报错, 不会崩, 只会让每个 p 值都偏小一点点。而偏小的方向恰好是"更容易
显著"。所以这里立一条守卫: 占用 + 预留 ≥ 7 且 scheme 仍是 `independent_family_v1`
→ 直接 `AssertionError`。

## 这个模块不做什么

- **不**改 `BONFERRONI_M` / `BONFERRONI_SCHEME_ID`
- **不**翻两处 `*_TRIGGERING_ENABLED`
- **不**碰 A2(`COMPLETE` 仍 `False`) / A3
- **不**实现 MACD / KDJ / 形态 / E 评价器、回放、hits 写入
- **不**新增 `SIGNAL_FAMILIES` 成员
- **不**改 `fx.*` 夹具、**不**碰 D1 定义文件

## 合规

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

from __future__ import annotations

from typing import Final, Mapping, Sequence

from .constants import BONFERRONI_M, BONFERRONI_SCHEME_ID

# ---------------------------------------------------------------- D1 冻结

#: D1 祖父键。承接 #9 / #241, **禁止改名**。
A1_D1_SIGNAL_ID: Final = "bss.daily_quality_value"
A1_D1_FAMILY: Final = "daily_fundamental"
A1_D1_EVENT_CLASS: Final = "daily_quality_value"
A1_D1_VERSION: Final = "v1"
A1_D1_SLOT_STATUS: Final = "occupied"

# ---------------------------------------------------------------- D2a MACD(命名位)

A1_D2A_SIGNAL_ID: Final = "bss.day.macd_hist_cross"
A1_D2A_FAMILY: Final = "daily_fundamental"
A1_D2A_EVENT_CLASS: Final = "daily_macd"
A1_D2A_VERSION: Final = "v1"
A1_D2A_SLOT_STATUS: Final = "reserved"

#: D2 初态。**不**是 `passed`, 更不是 `可放量` —— 命名到位 ≠ 评价器已通 ≠ calib 已过。
A1_D2_INITIAL_CALIB_STATUS: Final = "candidate"

# ---------------------------------------------------------------- D2b KDJ(命名位)

A1_D2B_SIGNAL_ID: Final = "bss.day.kdj_gold_cross"
A1_D2B_FAMILY: Final = "daily_fundamental"
A1_D2B_EVENT_CLASS: Final = "daily_kdj"
A1_D2B_VERSION: Final = "v1"
A1_D2B_SLOT_STATUS: Final = "reserved"

# ---------------------------------------------------------------- E1 预算占位

A1_E1_SIGNAL_ID: Final = "bss.evt.overnight_announce"
A1_E1_FAMILY: Final = "minute_event"
A1_E1_SLOT_STATUS: Final = "reserved_budget_only"

#: 本拍**不**为 E1 设 `event_class` 生产常量。
#:
#: 记在这里而不只是写在注释里: 一个没有评价器的 `event_class` 一旦进了
#: `EVENT_CLASSES`, 它就会永远是一个"合法但永不产生命中"的取值 —— 那种值读起来像
#: 功能已经做了。E 定义另口令, 届时连同 `EVENT_CLASSES` 一起加。
A1_E1_HAS_EVENT_CLASS: Final = False

# ---------------------------------------------------------------- 前缀规则钉

A1_PREFIX_D1_GRANDFATHER: Final = "bss.daily_quality_value"
A1_PREFIX_DAY: Final = "bss.day."
A1_PREFIX_EVT: Final = "bss.evt."
A1_PREFIX_FIXTURE: Final = "fx."

#: D2 不骑 D1: 不得继承 D1 的 calib passed。
A1_D2_MUST_NOT_INHERIT_D1_CALIB_PASSED: Final = True

#: 桩不入预算四格。
A1_STUB_NAMES: Final = ("volume_breakout", "auction_abnormal")

# ---------------------------------------------------------------- 预算表

#: 格数 == `BONFERRONI_M`。写死成 6 而不是 `= BONFERRONI_M`: 若将来有人 bump 了 m,
#: 这个常量**不该**跟着悄悄变大 —— 预算表要不要扩, 是要人做决定的事, 不是让赋值语句
#: 替他做。
A1_BUDGET_SLOT_COUNT: Final = 6

A1_BUDGET_OCCUPIED_OR_RESERVED: Final = 4
A1_BUDGET_EMPTY: Final = 2

A1_BUDGET_SLOTS: Final[Sequence[Mapping[str, str]]] = (
    {"slot": "0", "role": "D1", "signal_id": A1_D1_SIGNAL_ID, "status": A1_D1_SLOT_STATUS},
    {"slot": "1", "role": "D2a", "signal_id": A1_D2A_SIGNAL_ID, "status": A1_D2A_SLOT_STATUS},
    {"slot": "2", "role": "D2b", "signal_id": A1_D2B_SIGNAL_ID, "status": A1_D2B_SLOT_STATUS},
    {"slot": "3", "role": "E1", "signal_id": A1_E1_SIGNAL_ID, "status": A1_E1_SLOT_STATUS},
    {"slot": "4", "role": "EMPTY", "signal_id": "", "status": "empty"},
    {"slot": "5", "role": "EMPTY", "signal_id": "", "status": "empty"},
)

#: 第 7 个占用格须先 bump scheme。
A1_SEVENTH_REQUIRES_SCHEME_BUMP: Final = True
A1_SEVENTH_BUMP_TOKEN: Final = "bonferroni_seventh_requires_scheme_bump"

# ---------------------------------------------------------------- 不变量检查


def assert_budget_invariants(
    *,
    bonferroni_m: int = BONFERRONI_M,
    scheme_id: str = BONFERRONI_SCHEME_ID,
) -> None:
    """预算不变量: 占用+预留 == 4, 留白 == 2, M == 6, scheme 未 bump。

    参数默认取 `constants` 的现值(而不是写死字面量): 于是"实际生效的 m 与 scheme"
    与"预算表描述的 m 与 scheme"必须一致。若有人改了 `BONFERRONI_M`, 这里的默认值
    跟着变, 而 `A1_BUDGET_SLOT_COUNT` 不变 —— 两者一撞就炸, 正好把"改 m 却没扩预算表"
    挡下来。
    """
    if bonferroni_m != A1_BUDGET_SLOT_COUNT:
        raise AssertionError(
            f"BONFERRONI_M={bonferroni_m} != 预算格数 {A1_BUDGET_SLOT_COUNT};"
            " 本拍禁止改 m"
        )
    if scheme_id != "independent_family_v1":
        raise AssertionError(
            f"scheme={scheme_id!r} 已被 bump; 本拍禁止 bump(第 7 个占用格才允许)"
        )
    filled = sum(1 for slot in A1_BUDGET_SLOTS if slot["status"] != "empty")
    empty = sum(1 for slot in A1_BUDGET_SLOTS if slot["status"] == "empty")
    if filled != A1_BUDGET_OCCUPIED_OR_RESERVED or empty != A1_BUDGET_EMPTY:
        raise AssertionError(f"budget filled={filled} empty={empty}; 期望 4/2")
    if len(A1_BUDGET_SLOTS) != A1_BUDGET_SLOT_COUNT:
        raise AssertionError(
            f"预算表长度 {len(A1_BUDGET_SLOTS)} != {A1_BUDGET_SLOT_COUNT}"
        )


def assert_no_seventh_without_bump(
    occupied_or_reserved_count: int,
    scheme_id: str = BONFERRONI_SCHEME_ID,
) -> None:
    """第 7 守卫: 占用+预留 ≥ 7 且 scheme 未 bump → 失败。

    参数是"数量"而不是让它自己数表: 这样调用方可以先试算"再加一格会怎样", 而不必真
    改表。测例也正是这么用的。
    """
    if occupied_or_reserved_count >= 7 and scheme_id == "independent_family_v1":
        raise AssertionError(
            f"{A1_SEVENTH_BUMP_TOKEN}: 第 7 个占用格须先 bump BONFERRONI_SCHEME_ID"
        )


def assert_d2_not_riding_d1(
    *,
    d2_signal_id: str,
    d2_family: str,
    d1_signal_id: str = A1_D1_SIGNAL_ID,
    d1_family: str = A1_D1_FAMILY,
) -> None:
    """D2 不骑 D1: 同 family 允许; signal_id 必须不同; 且须落在 `bss.day.` 族。

    三条各自挡一种错法:

    - 同 id → 直接就是"骑 D1 键", 于是 calib 记录、样本桶、可见性全都会认错对象
    - 不同 family → D2 就不该在日频这一族里了, 那是另一个决定, 不该顺手滑过去
    - 前缀不对 → 键名从哪来变得不可推断, 预算表的 role 与 signal_id 就对不上号
    """
    if d2_signal_id == d1_signal_id:
        raise AssertionError("D2 signal_id 不得等于 D1(禁止骑 D1 键)")
    if d2_family != d1_family:
        raise AssertionError("D2 family 必须 daily_fundamental(与 D1 同族)")
    if not d2_signal_id.startswith(A1_PREFIX_DAY):
        raise AssertionError(f"D2 signal_id 须以 {A1_PREFIX_DAY!r} 开头")


def budget_table() -> tuple[dict[str, str], ...]:
    """预算表的**副本**。给调用方读, 不给改。

    返回副本而不是原对象: `A1_BUDGET_SLOTS` 是 Final 的, 但 Final 只挡住了重新赋值,
    挡不住 `A1_BUDGET_SLOTS[0]["status"] = "empty"`。一份可被就地篡改的"权威表", 在
    被篡改之后仍然会通过所有基于它的检查 —— 这种自相一致的错最难查。
    """
    return tuple(dict(slot) for slot in A1_BUDGET_SLOTS)


__all__ = [
    "A1_BUDGET_EMPTY",
    "A1_BUDGET_OCCUPIED_OR_RESERVED",
    "A1_BUDGET_SLOTS",
    "A1_BUDGET_SLOT_COUNT",
    "A1_D1_EVENT_CLASS",
    "A1_D1_FAMILY",
    "A1_D1_SIGNAL_ID",
    "A1_D1_SLOT_STATUS",
    "A1_D1_VERSION",
    "A1_D2A_EVENT_CLASS",
    "A1_D2A_FAMILY",
    "A1_D2A_SIGNAL_ID",
    "A1_D2A_SLOT_STATUS",
    "A1_D2A_VERSION",
    "A1_D2B_EVENT_CLASS",
    "A1_D2B_FAMILY",
    "A1_D2B_SIGNAL_ID",
    "A1_D2B_SLOT_STATUS",
    "A1_D2B_VERSION",
    "A1_D2_INITIAL_CALIB_STATUS",
    "A1_D2_MUST_NOT_INHERIT_D1_CALIB_PASSED",
    "A1_E1_FAMILY",
    "A1_E1_HAS_EVENT_CLASS",
    "A1_E1_SIGNAL_ID",
    "A1_E1_SLOT_STATUS",
    "A1_PREFIX_DAY",
    "A1_PREFIX_D1_GRANDFATHER",
    "A1_PREFIX_EVT",
    "A1_PREFIX_FIXTURE",
    "A1_SEVENTH_BUMP_TOKEN",
    "A1_SEVENTH_REQUIRES_SCHEME_BUMP",
    "A1_STUB_NAMES",
    "assert_budget_invariants",
    "assert_d2_not_riding_d1",
    "assert_no_seventh_without_bump",
    "budget_table",
]
