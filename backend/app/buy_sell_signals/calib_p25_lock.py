"""P2.5 口径预登记锁面(批次 20261028gp25 · P-bss-p2.5)。

## 这是什么

一组**纯常量 + 纯函数**的「口径锁」: 在主 horizon、辅 horizon、成本、显著性判据、
样本下限、对齐键、多重检验方案、失败状态、主指标换轨、定序这九个维度上, 把
**评估开跑之前**就该定下来的口径钉死。

它**不是**评估器, **不是**回放引擎, **不是** calib 写路径, 更**不是**放量开关。
本模块没有任何 I/O: 不读文件、不写文件、不碰 `HitStore`、不调 `calib.upsert`。

## 为什么先锁口径, 后跑评估

一件事的后验, 只要指标可以换, 就一定能挑出一个「过」的。T+1 不过看 T+5, T+5 不过看
T+20; 命中率不过看 Wilson 下界, 下界不过看均值; 显著性不过就把 Bonferroni 换成
Holm 或 BH。每一步单独看都「只是换个更合适的口径」, 连起来就是**事后选指标**。

事后选指标的问题不在于算出假数, 而在于**算出的每一个数都是真的**, 只是它们是
「在所有可选项里挑了最好的那个」之后的数, 而报告里不会写「我挑了 17 次」。读的人
看到的是「主 horizon T+5, 命中率显著」, 看不到那 16 个没过的。

所以本模块把选择**在开跑之前**做掉并写进代码: 主指标只能有一个, 辅指标只能当辅,
换主指标 = 让测例红。

## 锁面 ≠ 可放量

**这一条最容易读错, 所以单列。**

`P25_*` 常量齐备只说明「口径定了」。它**不**说明:

- 样本凑够了(影子写入是 P3b, 本拍没做)
- 评估跑过了(P2, 本拍没做)
- calib 过了(`CALIB_STATUS_PASSED` 要 `CalibRecord.status == "passed"`, 本拍不写)
- 可见性放开了(仍然要过 `hits_visibility` 的全部闸)
- `TRIGGERING_ENABLED` 该开(本拍一个都没动)

一句话: **口径锁 ≠ 可放量**。锁面齐备的下一步是**跑评估**, 不是**上线**。

## 引用既有钉, 不另造权威源

18.2 / 30 / m=6 这三个数在本仓已经有权威定义, 分别在 `constants` 的
`CALIB_COST_BP_ROUNDTRIP` / `CALIB_SAMPLE_N_MIN` / `BONFERRONI_M`。

本模块**再导出**它们(同一个对象, `is` 成立), 不重新写一遍字面量。理由不是洁癖:
字面量抄第二遍之后, 有人改第一处、有人改第二处, 两个数字都「在代码里」, 但只有一处
被评估器读。那种不一致在 diff 里是一行 `18.2 → 19.0`, 在结论上是「成本口径悄悄换过」。

`18.2` 是本仓实测的**来回**成本(双边佣金 + 印花税 + 过户费), 不是单边。拿单边成本
去算 T+5 收益, 等于每笔多算一倍成本 —— 方向恒对空头有利, 也就是恒对「这信号不行」
有利。锁死它既防乐观也防悲观。

## 与 P2 的定序

`P25_BEFORE_P2 is True`: P2.5 是 P2 的**前置**。P2 真跑回放时应当**读**这里的常量,
而不是自己再定一套主 horizon。定序反过来(先跑 P2 再补锁面)就等于先看了结果再定口径,
锁面失去意义。

批次 20261028gp25 · P-bss-p2.5。
仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""
from __future__ import annotations

import math
from typing import Final

from .constants import (
    BONFERRONI_FAMILY_ALPHA,
    BONFERRONI_M,
    BONFERRONI_SCHEME_ID,
    CALIB_COST_BP_ROUNDTRIP,
    CALIB_EVAL_WINDOW_UNIT,
    CALIB_SAMPLE_N_MIN,
    CALIB_STATUSES,
    CALIB_STATUS_PASSED,
)

__all__ = [
    "CALIB_AUX_HORIZONS_TD",
    "CALIB_COST_BP_ROUNDTRIP",
    "CALIB_EVAL_WINDOW_UNIT",
    "CALIB_PRIMARY_HORIZON_TD",
    "CALIB_SAMPLE_N_MIN",
    "CALIB_STATUSES",
    "CALIB_STATUS_PASSED",
    "P25_ALIGN_KEY_FIELDS",
    "P25_ANY_GATE_FAIL_STATUS",
    "P25_ANY_GATE_FAIL_STATUS_IS_CALIB_FAILED",
    "P25_AUX_MANDATORY_REPORTED",
    "P25_BEFORE_P2",
    "P25_BONFERRONI_FAMILY_ALPHA",
    "P25_BONFERRONI_M",
    "P25_BONFERRONI_SCHEME_ID",
    "P25_COST_BP_ROUNDTRIP",
    "P25_COST_IS_CALIB_PIN",
    "P25_CROSS_VERSION_MERGE_FORBIDDEN",
    "P25_EPS",
    "P25_HOLM_BH_SUBSTITUTE_FORBIDDEN",
    "P25_LOCK_NOTE",
    "P25_PRIMARY_SWAP_FORBIDDEN",
    "P25_SAMPLE_IS_CALIB_PIN",
    "P25_SAMPLE_N_MIN",
    "P25_SIGNIFICANCE_REQUIRED",
    "WILSON_CONFIDENCE",
    "WILSON_HIT_RATE_LB_MIN",
    "WILSON_POSITIVE_RULE",
    "p25_gate_verdict",
    "p25_positive_verdict",
    "wilson_hit_rate_lower_bound",
]

# ---------------------------------------------------------------- 主 / 辅 horizon

CALIB_PRIMARY_HORIZON_TD: Final = 5
"""**主**评估 horizon: T+5 交易日。**唯一**的主指标。

为什么是 5 而不是 1 或 20: 1 日被当日噪声吃掉(涨跌停、竞价、尾盘异动), 20 日
跨过一个月度再平衡窗口、会把别的信号的效应掺进来。5 日是「信号发出后的一个完整
交易周」, 既够让基本面/技术面事件走完, 又不至于撞上下一个调仓事件。

**这个数字不是本拍新定的**, 它是既有口径的显式化。以前它散在文档与口口相传里,
现在它在代码里。以后要改它, 必须同时改 `test_buy_sell_signal_calib_p25_lock.py`
里的具体值断言 —— 也就是必须在 diff 里被看见。
"""

CALIB_AUX_HORIZONS_TD: Final = (1, 20)
"""**辅** horizon: T+1 与 T+20。**只报不当门**。

辅指标的作用是「说明信号衰减得快不快」, 不是「主指标没过就换它」。

T+1 报了正、T+5 报负, 这本身是有信息量的(信号是短脉冲, 5 日太长); 但正确的处理
是**承认主指标没过**, 而不是把主指标改成 T+1。前者产出一条「T+5 未过」的诚实结论,
后者产出一条「T+1 显著」的结论 —— 而后者一旦发出去, 第二天就会变成「建议买入」。
"""

P25_AUX_MANDATORY_REPORTED: Final = True
"""辅 horizon **必须全部报出**(`(1, 20)` 两个都要), 不准只挑好看的那个报。

只报一个辅 horizon 和换主指标是同一件事的温和版本: 读者看到「T+1 显著」,
默认你会把不显著的都报出来, 实际上你只报了一个。
"""

# ---------------------------------------------------------------- 成本

P25_COST_BP_ROUNDTRIP: Final = CALIB_COST_BP_ROUNDTRIP
"""成本口径: **同一个对象**, 不是同一个数字。

`P25_COST_BP_ROUNDTRIP is CALIB_COST_BP_ROUNDTRIP` 保持 `True`。抄成字面量
`18.2` 会让两处各自演进; 用同一对象则改 `constants` 一处, 这里跟着变,
而且测例用 `is` 断言能当场抓住「有人把它俩拆开了」。
"""

P25_COST_IS_CALIB_PIN: Final = True
"""标志: 上面的成本是**引用**既有钉, 不是本模块自建的第二个权威源。"""

# ---------------------------------------------------------------- 显著性: Wilson 95% 下界

WILSON_CONFIDENCE: Final = 0.95
"""Wilson 区间置信水平 95%。"""

WILSON_HIT_RATE_LB_MIN: Final = 0.5
"""命中率门: **Wilson 95% 下界严格大于 0.5**。

## 为什么是下界, 不是点估计

点估计 `k/n > 0.5` 在 n=30 时几乎不构成证据: 18/30 的点估计是 0.60, 看起来「过六成」,
但 95% 下界只有 0.423 —— 换成一次重抽就可能掉到 0.42。用点估计当门, 等于用
「一次投硬币的结果」当「硬币不偏」的证据。

Wilson 下界把样本量算进判据: 同样 60% 的命中率, n=30 下界 0.423(不过),
n=300 下界约 0.543(过)。**样本少就自动不显著**, 这是它比点估计好的地方。

## 为什么严格大于, 不是大于等于

边界 `下界 == 0.5` 意味着「有 5% 的概率真实命中率不到 50%」, 那是个抛硬币的信号,
不构成「优于随机」。判据写成 `> 0.5` 而不是 `>= 0.5`, 不留「刚好卡线算过」这个口子。

## 30 个样本时它要求什么

n=30 时: 20/30 下界 0.488(**不过**), 21/30 下界 0.521(**过**)。
也就是说 30 个样本里至少要对 21 个 —— 70% 的**实际**命中率才够。这个门槛
刻意比「看起来合理」高, 因为 30 样本本来就是下限而非目标。
"""

WILSON_POSITIVE_RULE: Final = "wilson_95_lb_gt_0.5"
"""判据的**名字**, 便于日志/文档指认是哪一个规则。

写成可指认的字符串, 而不是只有数值: 将来若有人搞出「Wilson 90%」或
「Clopper-Pearson 95%」, 日志里出现的规则名不同, 一眼能看出来口径被换了。
"""

P25_EPS: Final = 1e-12
"""判 `> 0.5` 时的浮点容差。

**只在下界这一侧加容差, 不跨过 0.5。** 意思是 `0.5 + 1e-12` 以下的都算不过,
不让「算出来 0.4999999999999 因为浮点被当成 0.5」这种情况变成过。
"""

P25_SIGNIFICANCE_REQUIRED: Final = True
"""显著性判据**必过**, 不接受「样本少所以先跳过显著性」。"""


def _z_for(confidence: float) -> float:
    """双侧正态分位数。**写死常用值**, 不引 scipy。

    只为 0.95 服务: 其它置信水平直接抛错, 避免有人传 0.90 就悄悄变成
    「Wilson 90%」而 `WILSON_POSITIVE_RULE` 还写着 95。
    """
    table = {0.95: 1.959963984540054}
    if confidence not in table:
        raise ValueError(
            f"只支持 confidence=0.95(当前口径), 收到 {confidence!r}; "
            "换置信水平必须同时改 WILSON_POSITIVE_RULE 与测例"
        )
    return table[confidence]


def wilson_hit_rate_lower_bound(
    hits: int,
    n: int,
    confidence: float = WILSON_CONFIDENCE,
) -> float:
    """Wilson score 区间的**下界**。

    `hits` 命中数, `n` 样本数。`n <= 0` 抛 `ValueError`(空样本不是 0 命中,
    是**没有结论**, 不能算出一个 0.0 去和一个门槛比)。

    纯函数, 无状态, 无 I/O。本模块只提供这一个算式, **不**做评估调度:
    谁调用、什么时候调用、拿什么当 hits, 都是 P2 的事。
    """
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError(f"n 必须是 int, 收到 {type(n).__name__}")
    if not isinstance(hits, int) or isinstance(hits, bool):
        raise ValueError(f"hits 必须是 int, 收到 {type(hits).__name__}")
    if n <= 0:
        raise ValueError(f"n 必须为正(空样本无结论), 收到 {n}")
    if hits < 0 or hits > n:
        raise ValueError(f"hits 必须在 [0, n] 内, 收到 hits={hits} n={n}")

    z = _z_for(confidence)
    p = hits / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (center - margin) / denom


def p25_positive_verdict(
    hits: int,
    n: int,
    confidence: float = WILSON_CONFIDENCE,
) -> bool:
    """Wilson 95% 下界是否**严格大于** 0.5(含浮点容差)。

    `下界 > 0.5 + P25_EPS` 才算过。容差只在上方留出, 不会把 0.4999 算成过。
    """
    lb = wilson_hit_rate_lower_bound(hits, n, confidence=confidence)
    return lb > WILSON_HIT_RATE_LB_MIN + P25_EPS


# ---------------------------------------------------------------- 样本下限

P25_SAMPLE_N_MIN: Final = CALIB_SAMPLE_N_MIN
"""样本下限: **同一个对象**, 引用 `CALIB_SAMPLE_N_MIN`(现 30)。

不另写 `30`。理由同成本: 两处字面量必然分叉, 而分叉之后「样本够不够」会变成
「看的是哪个 30」。
"""

P25_SAMPLE_IS_CALIB_PIN: Final = True
"""标志: 样本下限是**引用**既有钉。"""

# ---------------------------------------------------------------- 三键对齐 / 跨版本

P25_ALIGN_KEY_FIELDS: Final = (
    "signal_id",
    "definition_version",
    "freq_lock_version",
)
"""三键对齐字段名, **顺序即语义顺序**, 与 `CalibRecord.align_key` /
`FreqLockStamp.align_key` 逐位一致。

测例用 `CalibRecord.align_key` 与 `FreqLockStamp.align_key` 的实际取值反查这里,
确保三者不是「碰巧一样」而是被钉住的。
"""

P25_CROSS_VERSION_MERGE_FORBIDDEN: Final = True
"""**禁止**跨 `definition_version` / `freq_lock_version` 合并样本。

## 为什么不许合并

v1 跑了 15 个样本没过, 改两行参数出 v2 再跑 15 个, 合并算 30 个 —— 这就是
**用两个不同口径的样本凑一个门槛**。合并后的样本来自两个不同分布(参数变了),
「命中率」这个数在合并样本上根本没有定义。

更实际的问题: 如果允许合并, 那么「改参数重跑」就成了一条永远能过的路 ——
每轮凑不够就发个新版本接着凑, 直到凑够。跨版本禁合并把这条路堵死:
每个版本自己凑够 30。

`hits_visibility` 的三键对齐已经隐含这条; 这里把它写成**显式常量**,
让「想合并」成为一个需要显式改常量并让测例红的动作。
"""

# ---------------------------------------------------------------- 多重检验

P25_BONFERRONI_SCHEME_ID: Final = BONFERRONI_SCHEME_ID
P25_BONFERRONI_M: Final = BONFERRONI_M
P25_BONFERRONI_FAMILY_ALPHA: Final = BONFERRONI_FAMILY_ALPHA
"""Bonferroni 三件套, 全部**引用** `constants` 的既有钉。

`m = 6` 是族大小, `alpha_family = 0.05 / 6`。本模块不做 p 值计算 ——
它只钉住「将来算的时候该除以几」和「用的是哪一个族定义」。
"""

P25_HOLM_BH_SUBSTITUTE_FORBIDDEN: Final = True
"""**禁止**用 Holm / Benjamini-Hochberg(或任何别的多重检验方案)替换 Bonferroni。

## 为什么单独禁这一条

Bonferroni 在族内是**最保守**的。想让它过的人的第一反应往往不是伪造数据, 而是
「换成 Holm 吧, 更有效」。Holm 确实更有效(power 更高), 但它也更**容易过** ——
而这里要的恰恰是那个更保守的结论。

更深一层: 一旦方案可变, `m=6` 与 `alpha=0.0083` 这两个钉子就失去了约束力,
因为任何人都可以声称「按 BH, q=0.1 算过了」。方案是口径的一部分, 不是实现细节。
"""

# ---------------------------------------------------------------- 失败状态 / 换主指标 / 定序

P25_ANY_GATE_FAIL_STATUS: Final = "failed"
"""任一子门未过 → 结论状态就是 **`failed`**, 不是 `pending`。

## 为什么不是 pending

`pending` 的语义是「还没有结论」。样本凑够、评估跑完、某一个门没达标 —— 这已经
**有结论**了, 结论是「没过」。写成 `pending` 会让它看起来像「等着就好」,
而它其实在等一个永远不会来的东西。

`CALIB_STATUSES` 是封闭三值(`passed` / `pending` / `failed`), 这里取其中
**已有结论且没过**的那一个。四舍五入成 `pending` 是本仓明文禁止的
「悄悄降级」(见 `CALIB_SAMPLE_N_MIN` 的注释: 门槛不满足就是写入失败, 让他当场知道)。

## 为什么这里写的是字面量 `"failed"`

`constants` 里只有 `CALIB_STATUS_PASSED` 有专门常量(它被当作比较基准),
`pending` 与 `failed` 没有。本模块**不新增** `CALIB_STATUS_FAILED` —— 那会动
`constants` 的公共面, 超出本拍范围。取而代之: 字面量写在这里, 由测例断言它
`in CALIB_STATUSES` 且 `!= CALIB_STATUS_PASSED`。闭集校验把「拼错」和
「有人往闭集里加第四个值」都挡住了。
"""

P25_ANY_GATE_FAIL_STATUS_IS_CALIB_FAILED: Final = True
"""标志: 上面的失败状态落在 `CALIB_STATUSES` 闭集内, 且**不是** `passed`。"""

P25_PRIMARY_SWAP_FORBIDDEN: Final = True
"""**禁止**事后更换主指标(主 horizon、判据类型、命中率定义任一)。

换主指标是最难被发现的一种: 报告里永远只写「主指标 T+5」, 而 T+5 是看完 T+1/T+20
之后选的。读者无从判断。所以这条不留「只要声明就允许」的余地 —— 一律禁。
"""

P25_BEFORE_P2: Final = True
"""定序: **P2.5 ≺ P2**。口径锁在评估之前, 不是之后。

反向定序(先跑 P2 再补锁面)等于「先看结果, 再定口径」, 那时锁面已经没法起约束作用。
"""

P25_LOCK_NOTE: Final = (
    "P2.5 口径锁: 主 T+5 / 辅 T+1,T+20 / 成本 18.2bp 来回(引用 CALIB_COST_BP_ROUNDTRIP) / "
    "Wilson 95% 下界 > 0.5 / 样本 >=30(引用 CALIB_SAMPLE_N_MIN) / "
    "三键不跨版本合并 / Bonferroni(禁 Holm 与 BH) / 任一门未过即 failed / 禁事后换主指标 / "
    "P2.5 定序在 P2 之前。**口径锁 != 可放量**: 样本未凑、评估未跑、calib 未过、"
    "可见性与 TRIGGERING 一律未动。"
)
"""一行摘要, 供日志与状态接口指认。

刻意把「口径锁 != 可放量」写进这个字符串: 任何把 `P25_LOCK_NOTE` 贴到对外响应里的
地方, 都会连带把这句话带出去。
"""


def p25_gate_verdict(
    *,
    sample_n: int,
    hits: int,
    cross_version_merged: bool = False,
    substituted_test: str | None = None,
    primary_swapped: bool = False,
) -> str:
    """把 P2.5 的**闸判定**收成一个纯函数, 返回 `CALIB_STATUS_FAILED` 或 `CALIB_STATUS_PASSED`。

    ## 注意这个函数的边界

    它是**口径的判据**, 不是**评估器**。输入全是调用方给的摘要(样本数、命中数、
    几个「有没有违规」的布尔), 输出是一个状态字符串。它不读任何数据、不算收益、
    不查注册表、不写 calib。

    存在的意义: 让「什么算过」这句话有一个可测的实现。将来 P2 真跑评估时, 应当
    调这个函数得出结论, 而不是自己再写一遍 if 链 —— 那样两处判据迟早分叉。

    ## 子门(全部必须过)

    1. `sample_n >= P25_SAMPLE_N_MIN`
    2. Wilson 95% 下界 `> 0.5`(`p25_positive_verdict`)
    3. 未跨版本合并样本
    4. 未替换多重检验方案(`substituted_test` 必须为 `None`)
    5. 未事后更换主指标

    任一不满足 → `failed`。**没有 `pending` 分支**: 走到这里说明有结论了。
    """
    if cross_version_merged:
        return P25_ANY_GATE_FAIL_STATUS
    if substituted_test is not None:
        return P25_ANY_GATE_FAIL_STATUS
    if primary_swapped:
        return P25_ANY_GATE_FAIL_STATUS
    if sample_n < P25_SAMPLE_N_MIN:
        return P25_ANY_GATE_FAIL_STATUS
    if not p25_positive_verdict(hits, sample_n):
        return P25_ANY_GATE_FAIL_STATUS
    return CALIB_STATUS_PASSED
