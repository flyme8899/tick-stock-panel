"""轨①独立 P2.5 口径预登记锁面(批次 20261030ga3 · P-bss-a3-evt-p25 · -A-#11 A3)。

## 这是什么

一组**纯常量**的「轨①口径锁」。在主 horizon、成本三档、样本、三键、Bonferroni
引用、失败状态、禁换主、定序、与 A2 首见依赖这几个维度上, 把**评估开跑之前**
就该定下来的轨①口径钉死。

它**不是**评估器, **不是**回放引擎, **不是** calib 写路径, 更**不是**放量开关。
本模块没有任何 I/O: 不读文件、不写文件、不碰 `HitStore`、不调 calib 写口。

## 与日频 P2.5 正交(写死)

日频锁面在 `calib_p25_lock.py`, 主 horizon = `CALIB_PRIMARY_HORIZON_TD = 5`(T+5 交易日)。
本模块是**轨①(minute_event / 事件轨)独立**锁面, 主 horizon = `T+1_open`。

**禁止**改写日频 `CALIB_PRIMARY_HORIZON_TD`;**禁止**把日频 T+5 套作轨①主指标。

### 为什么轨①不能套 T+5

日频信号的信息是慢变量(估值、财务质量), 5 个交易日才让它走完; 事件轨信号的信息
是**瞬时的**——一条隔夜公告、一次盘中突发, 影响在开盘那一刻就释放完了。

用 T+5 去量事件, 量到的主要不是事件本身, 而是事件之后那五天里发生的**别的事**。
窗越长, 掺进来的东西越多, 而掺进来的东西不会让结论变噪音, 只会让结论变得
"看起来有信号" —— 因为五天的累计涨跌里, 总有一段能对上方向。

所以轨①最短 horizon 就是 **T+1 开盘**: 信号之后第一个可成交的时点。再短就不是
horizon 而是无风险套利了(假设你能在消息公开的同一毫秒成交), 再长就开始掺噪声。

## 口径锁 ≠ 可放量

`EVT_P25_*` 齐备只说明「轨①口径定了」——**仅此而已**。它**不**说明:

| 不等于 | 为什么 |
|---|---|
| 样本凑够 | `EVT_P25_SAMPLE_N_MIN` 只是门槛值, 不是已得样本数 |
| 评估跑过 | 评估器属轨① P2, 另口令; 本拍定序在其之前 |
| calib 过了 | calib 结论由评估器按锁定口径算出, 与"口径写在哪个文件"无关 |
| 可写影子 | 事件轨写影子仍被 A2 的 COMPLETE 门挡着 |
| TRIGGERING 该开 | 两处 `*_TRIGGERING_ENABLED` 仍 `False`, 一个字都没碰 |

一句话: **口径锁 ≠ calib ≠ 可放量。**

## 与 A2 的依赖(写死)

**A2 ≺ A3**: 轨①的首见口径是 A2 的交付物, horizon 起点只能锚在它上面。

horizon **起点 = A2 `first_seen_at`(我方首次可得)**, 不是 `published_at`。

缺首见 → **fail-closed**(跳过 / 标 `first_seen_missing`);**不得**用 `published`
顶上。顶上看起来"补全了", 实际是把前视风险重新引进来 —— 而缺首见至少还是个能被
统计到的洞。

当 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False` 时,**禁止冒充**事件轨
可写影子 / 可 calib 跑通。本模块**只钉这条依赖, 不把 COMPLETE 翻 True**
(翻旗是 A2 完成的另一次验收)。

**COMPLETE 仍 False ≠ A2 完成。** 前者是当前状态, 后者是一次要另行验收的结论。

## 定序

轨① P2.5 ≺ 轨① P2(评估器 + 截断回放, 另口令)。口径必须先于评估锁定: 评估跑完
再定口径, 等于用结果挑口径。

## 禁事后换主

主 horizon / 判据 / 命中率定义,**任一**事后更换 → 不过。任一门不过 →
`failed`(不是 `pending`)。`pending` 是个可以被无限拖着的状态; `failed` 要求
有人去看为什么。

## 成本三档

- **权威门**: `EVT_P25_COST_BP_ROUNDTRIP is CALIB_COST_BP_ROUNDTRIP`(现 18.2)
- **敏感性分报**: 30bp / 50bp,**只报不当权威门**

分报的意思是: 同一个策略在 18.2 / 30 / 50 三档成本下各出一组数, 让人看清结论对
成本的敏感程度。**不**是"哪档好看用哪档"—— 权威门永远是 18.2, 因为它是规范 v3
钉的回测口径。用 30 或 50 去"证明"策略能过, 是把成本假设调到能过为止。

## Bonferroni 只引用

`m = 6` / `independent_family_v1` 是**全局**口径。本模块只引用, 不占用新定义名额,
不 bump scheme。**禁止**用 Holm / BH 或任何别的多重检验方案替换 Bonferroni ——
换方案不是"改进", 是换了一把尺子去量同一批数。

## 本模块不做什么

- **不**改 `CALIB_PRIMARY_HORIZON_TD` / 日频主指标语义
- **不**改 `BONFERRONI_M` / `BONFERRONI_SCHEME_ID`
- **不**翻两处 `*_TRIGGERING_ENABLED`
- **不**翻 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE`
- **不**改 A1 预算格数值 / A2 影子闸语义
- **不**实现 E1/E2/E3 信号定义或评价器; **不**跑评估器

## 合规

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。
成本来回 18.2bp(轨①敏感性 18.2 / 30 / 50bp 分报)。
"""

from __future__ import annotations

from typing import Final

from .constants import (
    BONFERRONI_FAMILY_ALPHA,
    BONFERRONI_M,
    BONFERRONI_SCHEME_ID,
    CALIB_COST_BP_ROUNDTRIP,
    CALIB_SAMPLE_N_MIN,
    CALIB_STATUS_PASSED,
    CALIB_STATUSES,
)
from .first_seen_audit import EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE

# ---------------------------------------------------------------- 主 / 辅 horizon(轨①)

#: **主**评估 horizon: T+1 开盘。**唯一**的轨①主指标。
#:
#: 类型是字符串而不是整数 TD —— 这个差别是刻意的。日频用整数(`5`), 轨①用
#: 带语义的字符串(`"T+1_open"`), 于是"把日频 5 套过来"这件事在类型层面就不可能
#: 悄悄发生: `"T+1_open" != 5`, 而且没有任何整数能等于它。
#:
#: 最短 horizon ≥ T+1 开盘。再短假设你能在消息公开的同一毫秒成交; 再长就掺进
#: 别的事(见模块头)。
EVT_CALIB_PRIMARY_HORIZON: Final = "T+1_open"

#: **辅** horizon。建议可报, **只报不当门**, 不得替主。
EVT_CALIB_AUX_HORIZONS: Final = ("T+1_close", "T+5_open")

# ---------------------------------------------------------------- 成本三档

#: 权威成本: **同一个对象**, 不是同一个数字。
#:
#: `EVT_P25_COST_BP_ROUNDTRIP is CALIB_COST_BP_ROUNDTRIP` 保持 `True`。抄字面量
#: `18.2` 会让两处各自演进; 用同一对象则改 `constants` 一处, 这里跟着变, 而 `is`
#: 断言能当场抓住"有人把它俩拆开了"。
EVT_P25_COST_BP_ROUNDTRIP: Final = CALIB_COST_BP_ROUNDTRIP

EVT_P25_COST_IS_CALIB_PIN: Final = True

#: 敏感性分报档: 30bp / 50bp。**只报不当权威门**。
#:
#: 写成 `(30.0, 50.0)` 而不是引用某个常量: 这两个数不是"钉", 是一次性的分报档,
#: 将来想加档也只改这里, 不必动全局常量 —— 加了档, 权威门还是 18.2。
EVT_P25_COST_BP_SENSITIVITY: Final = (30.0, 50.0)

# ---------------------------------------------------------------- 样本 / 三键

#: 样本下限: **同一个对象**, 引用 `CALIB_SAMPLE_N_MIN`(现 30)。
EVT_P25_SAMPLE_N_MIN: Final = CALIB_SAMPLE_N_MIN

EVT_P25_SAMPLE_IS_CALIB_PIN: Final = True

#: 三键对齐字段名, 顺序即语义顺序。
#:
#: 与 `constants.SHADOW_BUCKET_KEY_FIELDS` / `P25_ALIGN_KEY_FIELDS` 同序:
#: `signal_id` → `definition_version` → `freq_lock_version`。换个顺序不会报错,
#: 只会让按位对齐的地方全部错位一格。
EVT_P25_ALIGN_KEY_FIELDS: Final = (
    "signal_id",
    "definition_version",
    "freq_lock_version",
)

# ---------------------------------------------------------------- Bonferroni(只引用 · 不改)

EVT_P25_BONFERRONI_SCHEME_ID: Final = BONFERRONI_SCHEME_ID
EVT_P25_BONFERRONI_M: Final = BONFERRONI_M
EVT_P25_BONFERRONI_FAMILY_ALPHA: Final = BONFERRONI_FAMILY_ALPHA

#: 禁止用 Holm / BH(或任何别的多重检验方案)替换 Bonferroni。
EVT_P25_HOLM_BH_SUBSTITUTE_FORBIDDEN: Final = True

# ---------------------------------------------------------------- 失败 / 禁换主 / 定序

#: 任一门不过 → `failed`(不是 `pending`)。
#:
#: `pending` 可以被无限拖着, 而拖着不需要任何人做决定; `failed` 要求有人去看为什么。
#:
#: 这里写 `"failed"` 字面量而不是引用某个 `CALIB_STATUS_FAILED`: 仓里只导出过
#: `CALIB_STATUS_PASSED`, `failed` 一直是 `CALIB_STATUSES` 集合里的一个成员。
#: 本拍**不**为了省一个字面量就去 `constants.py` 新增常量 —— 那会让"本拍零碰
#: constants"这句话变成一句需要解释的话。测例会反过来钉住 `"failed" ∈ CALIB_STATUSES`,
#: 于是字面量至少不会指向一个不存在的状态。
EVT_P25_ANY_GATE_FAIL_STATUS: Final = "failed"

#: 禁止事后更换主指标(主 horizon / 判据 / 命中率定义, 任一)。
EVT_P25_PRIMARY_SWAP_FORBIDDEN: Final = True

#: 定序: 轨① P2.5 ≺ 轨① P2。口径锁在评估之前。
EVT_P25_BEFORE_P2: Final = True

# ---------------------------------------------------------------- 与 A2 依赖

#: horizon 起点 = A2 `first_seen_at`(我方首次可得)。
EVT_P25_HORIZON_STARTS_AT_FIRST_SEEN: Final = True

#: 缺首见 → fail-closed; 不得用 published 顶上。
EVT_P25_MISSING_FIRST_SEEN_FAIL_CLOSED: Final = True

#: 当 COMPLETE is False 时, 禁止冒充可写影子 / 可 calib 跑通。
#:
#: 本模块只钉这条依赖, **不**把 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` 翻 True。
#: 这里不做 import 期 `assert` —— 那会让"COMPLETE 被翻 True"这件事变成一次
#: **服务启动失败**, 而不是一条测例红。前者停机、后者只红一个格子, 而我们需要的是
#: 后者: 翻旗要有人去看 diff, 不是让进程起不来。
EVT_P25_COMPLETE_FALSE_BLOCKS_SHADOW_CALIB_CLAIM: Final = True

#: 口径锁的一句话摘要。给日志 / PR / 界面用。
EVT_P25_LOCK_NOTE: Final = (
    "轨①P2.5口径锁: 主 T+1_open / 成本权威 18.2bp is CALIB_COST_BP_ROUNDTRIP "
    "+ 敏感性 30/50bp 分报 / 样本引用 CALIB_SAMPLE_N_MIN / 三键对齐 / "
    "Bonferroni 引用(禁 Holm/BH) / 任一门未过即 failed / 禁事后换主 / "
    "定序轨①P2.5≺轨①P2 / 起点=first_seen_at / COMPLETE=False 禁冒充可写影子可calib。"
    "**口径锁 != 可放量**: 样本未凑、评估未跑、calib 未过、可见性与 TRIGGERING 一律未动; "
    "本拍不翻 COMPLETE; 不改日频 T+5。"
)

# ---------------------------------------------------------------- 口径自检(纯函数)


def assert_evt_lock_invariants(
    *,
    primary_horizon: str = EVT_CALIB_PRIMARY_HORIZON,
    cost_pin: object = EVT_P25_COST_BP_ROUNDTRIP,
    sample_pin: object = EVT_P25_SAMPLE_N_MIN,
    scheme_id: str = EVT_P25_BONFERRONI_SCHEME_ID,
    m: int = EVT_P25_BONFERRONI_M,
    complete: bool = EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
) -> None:
    """口径锁自检。五个维度各自挡住一种"被顺手改掉"的方式。

    参数默认取本模块现值(而不是字面量): 于是"实际生效的口径"与"锁面描述的口径"
    必须一致 —— 有人改了常量却忘了同步, 一撞就炸。

    **不翻旗**: `complete` 只用来检查"是否被翻成 True", 不改它。若真被翻了, 这里
    报的是"本拍不该翻", 而不是顺手把它改回来 —— 改回来会掩盖那次翻旗。
    """
    # 1. 先查类型, 再查取值 —— 顺序有讲究: 若先比值, 传进来一个 `5` 只会得到
    #    "5 != 'T+1_open'", 而真正该说的是"整数是日频那套, 不许套用"。错的地方一样,
    #    但前者的报错会让人以为是拼写问题。
    if not isinstance(primary_horizon, str):
        raise AssertionError(
            f"轨①主 horizon 必须是字符串(轨①语义), 收到 {type(primary_horizon).__name__};"
            " 整数是日频那套 TD, 不许套用"
        )
    if primary_horizon != EVT_CALIB_PRIMARY_HORIZON:
        raise AssertionError(
            f"主 horizon={primary_horizon!r} != {EVT_CALIB_PRIMARY_HORIZON!r}"
        )

    # 2. 成本权威必须还是同一个对象。
    if cost_pin is not CALIB_COST_BP_ROUNDTRIP:
        raise AssertionError(
            "成本权威已与 CALIB_COST_BP_ROUNDTRIP 脱钩 —— 抄成字面量会让两处各自演进"
        )

    # 3. 样本下限同理。
    if sample_pin is not CALIB_SAMPLE_N_MIN:
        raise AssertionError("样本下限已与 CALIB_SAMPLE_N_MIN 脱钩")

    # 4. Bonferroni 未 bump、未改 m。
    if scheme_id != "independent_family_v1":
        raise AssertionError(f"scheme={scheme_id!r} 已被 bump; 本拍禁止")
    if m != 6:
        raise AssertionError(f"BONFERRONI_M={m} != 6; 本拍禁止改 m")

    # 5. COMPLETE 不得被本拍翻 True。
    if complete is not False:
        raise AssertionError(
            "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE 已被翻 True;"
            " 翻旗属 A2 完成另验收. 本拍(以及 #254)都不该翻它"
        )


def is_shadow_calib_claim_allowed() -> bool:
    """事件轨此刻能否声称"可写影子 / 可 calib"。

    返回 `False` 是**当前正确答案**, 不是"暂时不支持"。COMPLETE 未翻之前, 事件轨的
    首见口径还没验收, 拿它算出来的 horizon 起点无据可依。
    """
    return bool(EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE)


#: 状态取值集合与"通过"值的只读锚 —— 免得有人在本模块里另立一套状态字面量。
EVT_P25_STATUSES: Final = CALIB_STATUSES
EVT_P25_PASSED_STATUS: Final = CALIB_STATUS_PASSED


__all__ = [
    "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE",
    "EVT_CALIB_AUX_HORIZONS",
    "EVT_CALIB_PRIMARY_HORIZON",
    "EVT_P25_ALIGN_KEY_FIELDS",
    "EVT_P25_ANY_GATE_FAIL_STATUS",
    "EVT_P25_BEFORE_P2",
    "EVT_P25_BONFERRONI_FAMILY_ALPHA",
    "EVT_P25_BONFERRONI_M",
    "EVT_P25_BONFERRONI_SCHEME_ID",
    "EVT_P25_COMPLETE_FALSE_BLOCKS_SHADOW_CALIB_CLAIM",
    "EVT_P25_COST_BP_ROUNDTRIP",
    "EVT_P25_COST_BP_SENSITIVITY",
    "EVT_P25_COST_IS_CALIB_PIN",
    "EVT_P25_HOLM_BH_SUBSTITUTE_FORBIDDEN",
    "EVT_P25_HORIZON_STARTS_AT_FIRST_SEEN",
    "EVT_P25_LOCK_NOTE",
    "EVT_P25_MISSING_FIRST_SEEN_FAIL_CLOSED",
    "EVT_P25_PASSED_STATUS",
    "EVT_P25_PRIMARY_SWAP_FORBIDDEN",
    "EVT_P25_SAMPLE_IS_CALIB_PIN",
    "EVT_P25_SAMPLE_N_MIN",
    "EVT_P25_STATUSES",
    "assert_evt_lock_invariants",
    "is_shadow_calib_claim_allowed",
]
