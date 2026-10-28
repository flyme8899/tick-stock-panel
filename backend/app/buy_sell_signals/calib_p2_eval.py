"""P2 评估器 + 截断回放(批次 20261028gp2 · P-bss-p2 · R2-2 / R2-7①)。

## 这个模块做什么

按**已合入**的 P2.5 锁面(`calib_p25_lock`), 对 #241 注册的真实日频定义
`bss.daily_quality_value` 做一次**可复现的截断回放**:

1. 按 PIT(`announce_date <= asof`)截断样本 —— 只看得见截至 `asof` 已经公告的数字
2. 按**全市场影子**口径统计样本量 —— 不按自选/持仓 scope 过滤
3. 算主指标(T+5 净收益, 扣 18.2bp 来回成本)与辅指标(T+1 / T+20, 只报不当门)
4. 算命中率的 **Wilson 95% 下界**, 调锁面的 `p25_gate_verdict` 出结论
5. 产出带 `fingerprint` 的结果, 同一输入跑两次**逐字段一致**

## 这个模块**不**做什么

- **不**写 `HitStore`(生产写路径一条都没有)
- **不**做 P3b 影子生产写入
- **不**翻任何 `TRIGGERING_ENABLED`
- **不**调 `calib.upsert`(评估结果留在研究态, 不落进 `calib.json`)
- **不**读 env、**不**新增开关

## 大字:路径已通 ≠ calib 已过 ≠ 可放量 ≠ TRIGGERING

**这一条最容易读错, 所以单列。**

本模块跑通只说明「按已合锁面, 仓内能对这条定义跑一次可复现的评估」。它**不**说明:

| 不等于 | 为什么 |
|---|---|
| calib 已过 | 样本若 <30, 结论就是 `failed`; 即便 ≥30, 结论是**研究态**结论, 不是 `calib.json` 里的 `passed` |
| 可放量 | 放量要过可见性闸 + calib 登记 + 人工发布, 三样本拍一样没动 |
| TRIGGERING 该开 | 两处 `*_TRIGGERING_ENABLED` 仍 `False`, 本模块一个字节都没碰 |
| 影子已生产写入 | P3b 未开; 样本来自调用方传入的行, 不是生产影子库 |

**口径锁已合 ≠ 评估已过门。** #242 把「用什么口径判」钉死了; 本模块只是**按那个口径算了一次**。
算出来是 `passed` 也只是「这一次、这批样本、形式上满足」, 仍不等于上面任何一项。

## 为什么评估器必须 import 锁面而不是自己写数

主 horizon 5、成本 18.2、样本 30、Wilson 阈值 0.5、`m=6` —— 这些数字在 `calib_p25_lock`
里已经有权威定义。评估器里再抄一遍, 两个地方就会各自演进: 有人把锁面的主 horizon 从
T+5 改成 T+7, 评估器还按 T+5 算, 而两边都「在代码里」, 只有一边被真正读。

更隐蔽的是判据本身: 如果评估器自己写 `if lb > 0.5`, 那么将来锁面把判据从「下界 > 0.5」
改成「下界 > 0.55」时, 评估器会**继续按旧判据放行**。所以本模块过门一律调
`p25_gate_verdict` / `p25_positive_verdict`, 不写第二套 if 链。

## 为什么回放必须可复现

评估结论的价值取决于「别人能不能算出同一个数」。如果同样的输入跑两次结果不同, 那么
任何一次「过了」都不能复核 —— 你没法区分「信号真的有效」和「这次随机到了好样本」。

本模块的可复现性靠三件事:

1. **纯函数**: 不读时钟、不读随机数、不读文件、不读 env; 输入全从参数来
2. **定序**: 样本按 `(symbol, asof)` 排序后再统计, 不受传入顺序影响
3. **指纹**: `fingerprint` 走 `freq_lock_fingerprint`(sha256 全 64 位), 不另造弱哈希

测例里 `truncated_replay_twice_equal` 跑两次逐字段比对, 包括 `fingerprint`。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Iterable, Mapping, Sequence

from .calib_p25_lock import (
    CALIB_AUX_HORIZONS_TD,
    CALIB_PRIMARY_HORIZON_TD,
    P25_ALIGN_KEY_FIELDS,
    P25_ANY_GATE_FAIL_STATUS,
    P25_BEFORE_P2,
    P25_COST_BP_ROUNDTRIP,
    P25_CROSS_VERSION_MERGE_FORBIDDEN,
    P25_HOLM_BH_SUBSTITUTE_FORBIDDEN,
    P25_PRIMARY_SWAP_FORBIDDEN,
    P25_SAMPLE_N_MIN,
    WILSON_POSITIVE_RULE,
    p25_gate_verdict,
    wilson_hit_rate_lower_bound,
)
from .constants import CALIB_STATUS_PASSED, CALIB_STATUSES
from .daily_fundamental_definition import (
    PIT_CUTOFF_VALUE,
    PIT_RULE_TEXT,
    REAL_DAILY_DEFINITION_VERSION,
    REAL_DAILY_SIGNAL_ID,
    SAMPLE_COUNT_DOMAIN_VALUE,
)
from .freq_lock import FREQ_LOCK_VERSION, freq_lock_fingerprint

# ---------------------------------------------------------------- 编译期自检

#: 定序: P2.5 必须在 P2 之前。反向定序(先跑评估再补锁面)等于先看结果再定口径。
assert P25_BEFORE_P2 is True

#: PIT 截断键必须是公告日。`period_end` 会让评估在前视下跑, 而前视恒对多头有利。
assert PIT_CUTOFF_VALUE == "announce_date"

#: 样本计数域必须是全市场影子。scope 后计数会让「样本够不够」随用户自选变化。
assert SAMPLE_COUNT_DOMAIN_VALUE == "universe_shadow"

#: 评估对象必须是 #241 的真实定义, 不是夹具。
assert REAL_DAILY_SIGNAL_ID == "bss.daily_quality_value"

#: 失败状态必须落在封闭集内且不是 `passed`。
assert P25_ANY_GATE_FAIL_STATUS in CALIB_STATUSES
assert P25_ANY_GATE_FAIL_STATUS != CALIB_STATUS_PASSED

# ---------------------------------------------------------------- 身份

P2_EVAL_SIGNAL_ID: Final = REAL_DAILY_SIGNAL_ID
"""评估对象: `bss.daily_quality_value`。

**不是** `fx.pub.daily_quality_value`(夹具), **不是** `volume_breakout`(minute_event 桩),
**不是** `auction_abnormal`(竞价桩)。三者都不在本模块的输入契约内。
"""

P2_EVAL_BATCH_TAG: Final = "20261028gp2"

P2_EVAL_SPEC_REF: Final = "P-bss-p2"

P2_EVAL_NOTE: Final = (
    "P-bss-p2 评估器+截断回放: 读 calib_p25_lock; 对象 bss.daily_quality_value; "
    "PIT=announce_date; universe_shadow; 两次回放一致含 fingerprint; "
    "路径已通!=calib!=可放量!=TRIGGERING; 口径锁已合!=评估已过门"
)
"""一行摘要, 供日志与 PR 指认。

刻意把两条否定句都写进字符串: 任何把它贴出去的地方都会连带把这两句话带出去。
"""

# ---------------------------------------------------------------- 路径状态

P2_PATH_STATUS_REACHED: Final = "path_reached"
"""路径已通: 回放跑完了, 但样本未达 30 或某一门未过。

**不是** `passed`。这个值刻意与 `CALIB_STATUSES` 不重叠 —— 它是本模块的路径状态,
不是 calib 状态。混用会让读的人把「路径跑通」当成「校准通过」。
"""

P2_PATH_STATUS_GATE_MET: Final = "gate_formally_met"
"""锁面门槛**形式上**满足(n≥30 且 Wilson 下界过)。

**仍不是** `passed`。要变成 `calib.json` 里的 `passed`, 还要走人工发布闸 —— 那不在本拍。
"""

# ---------------------------------------------------------------- 输入契约

_REQUIRED_ROW_FIELDS: Final = ("symbol", "asof", "announce_date", "period_end")
"""评估行必须带的字段。缺任何一项直接拒, 不做「缺了就当空」的兜底。

`announce_date` 与 `period_end` 都要求带, 但**只有 `announce_date` 参与截断**。
要求带 `period_end` 是为了能在测例里**证明**它没被用 —— 一个不出现的数据, 你无法证明
它没参与判定。
"""


class P2EvalInputError(ValueError):
    """输入不满足评估契约。**不静默跳过**: 跳过一条脏数据等于悄悄改了样本量。"""


def _parse_day(text: Any, *, field: str) -> str:
    """把日期规整成 `YYYY-MM-DD` 字符串。只接受 ISO 日期, 不做宽松解析。"""
    if not isinstance(text, str):
        raise P2EvalInputError(f"{field} 必须是字符串日期, 收到 {type(text).__name__}")
    value = text.strip()
    if len(value) < 10:
        raise P2EvalInputError(f"{field} 必须是 YYYY-MM-DD, 收到 {text!r}")
    head = value[:10]
    if head[4] != "-" or head[7] != "-":
        raise P2EvalInputError(f"{field} 必须是 YYYY-MM-DD, 收到 {text!r}")
    return head


def _require_finite(value: Any, *, field: str) -> float:
    """取数值, 拒绝 bool / 非数 / NaN / inf。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise P2EvalInputError(f"{field} 必须是数值, 收到 {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise P2EvalInputError(f"{field} 必须是有限数, 收到 {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class EvalRow:
    """一条参与回放的样本行(研究输入, 非生产影子)。

    ## 字段含义

    - `symbol` 标的; `asof` 评估时点
    - `announce_date` 公告日 / 可得日 —— **唯一**的 PIT 截断键
    - `period_end` 报告期末 —— **不参与**截断, 只带着以便测例证明它没被用
    - `ret_t1` / `ret_t5` / `ret_t20` 三个 horizon 的**毛收益**(未扣成本), 小数形式
      (0.012 = 1.2%)
    - `shadow` 是否来自全市场影子口径 —— `False` 的行不入样本

    ## 为什么把收益按 horizon 拆成三个字段, 而不是一个 dict

    拆开之后「主 horizon 用哪一列」在类型层面就写死了; dict 形态下 `rows[0]["ret_t5"]`
    与 `rows[0]["ret_5"]` 只差一个字符, 打错会静默变成 `KeyError` 或更糟的空值。
    """

    symbol: str
    asof: str
    announce_date: str
    period_end: str
    ret_t1: float
    ret_t5: float
    ret_t20: float
    shadow: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> EvalRow:
        missing = [name for name in _REQUIRED_ROW_FIELDS if name not in raw]
        if missing:
            raise P2EvalInputError(f"评估行缺字段: {missing}")
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise P2EvalInputError(f"symbol 不能为空, 收到 {symbol!r}")
        return cls(
            symbol=symbol.strip(),
            asof=_parse_day(raw.get("asof"), field="asof"),
            announce_date=_parse_day(raw.get("announce_date"), field="announce_date"),
            period_end=_parse_day(raw.get("period_end"), field="period_end"),
            ret_t1=_require_finite(raw.get("ret_t1"), field="ret_t1"),
            ret_t5=_require_finite(raw.get("ret_t5"), field="ret_t5"),
            ret_t20=_require_finite(raw.get("ret_t20"), field="ret_t20"),
            shadow=bool(raw.get("shadow", True)),
        )


# ---------------------------------------------------------------- 指标

def _net_return(gross: float, *, cost_bp: float) -> float:
    """毛收益扣**来回**成本 → 净收益。

    成本单位 bp(1bp = 0.0001)。`18.2bp` → `0.00182`。**来回**而非单边: 单边成本会让
    每一笔都少算一倍成本, 而方向恒对「信号不行」有利 —— 既防乐观也防悲观地扣一次。
    """
    return gross - cost_bp / 10000.0


def _hit_count(nets: Sequence[float]) -> int:
    """净收益 > 0 的样本数。

    严格大于: 净收益恰好 0 是「不赚不亏」, 把它算命中等于把打平说成赢了。
    """
    return sum(1 for value in nets if value > 0)


def _horizon_summary(
    rows: Sequence[EvalRow],
    *,
    horizon_td: int,
    cost_bp: float,
) -> dict[str, Any]:
    """单个 horizon 的摘要: 样本数 / 命中数 / 均值净收益 / Wilson 下界。

    `horizon_td` 只接受锁面里出现过的值(主 5 / 辅 1, 20); 其它值抛错 —— 这挡住
    「临时加一个 T+3 看看」这种事后调参。
    """
    allowed = {CALIB_PRIMARY_HORIZON_TD, *CALIB_AUX_HORIZONS_TD}
    if horizon_td not in allowed:
        raise P2EvalInputError(
            f"horizon {horizon_td} 不在锁面取值内 {sorted(allowed)}; 加减 horizon 须先改锁面"
        )
    field = f"ret_t{horizon_td}"
    nets = [_net_return(getattr(row, field), cost_bp=cost_bp) for row in rows]
    n = len(nets)
    hits = _hit_count(nets)
    mean_net = (sum(nets) / n) if n else 0.0
    lb = wilson_hit_rate_lower_bound(hits, n) if n else None
    return {
        "horizon_td": horizon_td,
        "is_primary": horizon_td == CALIB_PRIMARY_HORIZON_TD,
        "sample_n": n,
        "hits": hits,
        "mean_net_return": mean_net,
        "wilson_lb": lb,
        "wilson_rule": WILSON_POSITIVE_RULE,
    }


# ---------------------------------------------------------------- 回放

def _select_shadow_rows(
    rows: Iterable[EvalRow],
    *,
    asof: str,
) -> list[EvalRow]:
    """PIT 截断 + 影子口径筛选, 然后**定序**。

    ## 截断只用 `announce_date`

    `announce_date <= asof`。`period_end` 是报告期末, 拿它截断是前视: 三季报 9 月 30 日
    截止、10 月底才公告, 按 `period_end <= asof` 判的话 10 月 1 日就能「知道」一个月后
    才公布的数字。方向永远对策略有利, 所以它是唯一一处「错得很隐蔽且永远帮多头」的错。

    本函数**不读** `period_end` —— 它只做 `announce_date` 比较。

    ## 只数影子行

    `shadow=False` 的行是「已经在 scope 里被人看见过的」, 它们的出现概率受用户自选影响。
    把它们算进样本, 等于让「样本够不够」随用户配置变化。

    ## 为什么必须定序

    调用方传入的顺序不该影响结果。不定序的话, 同一批数据换个顺序就可能因为浮点累加顺序
    不同而算出不同均值 —— 那正是「两次回放不一致」的来源之一。按 `(asof, symbol)` 排,
    与传入顺序无关。
    """
    kept = [row for row in rows if row.shadow and row.announce_date <= asof]
    kept.sort(key=lambda row: (row.asof, row.symbol, row.announce_date))
    return kept


def _align_key_payload(
    *,
    definition_version: str,
    freq_lock_version: str,
) -> dict[str, str]:
    """三键 → dict。键名与顺序取自锁面的 `P25_ALIGN_KEY_FIELDS`。"""
    values = (P2_EVAL_SIGNAL_ID, definition_version, freq_lock_version)
    if len(values) != len(P25_ALIGN_KEY_FIELDS):
        raise AssertionError("三键字段数与锁面不一致")
    return dict(zip(P25_ALIGN_KEY_FIELDS, values))


def truncated_replay_once(
    *,
    rows: Iterable[Mapping[str, Any] | EvalRow],
    asof: str,
    definition_version: str = REAL_DAILY_DEFINITION_VERSION,
    freq_lock_version: str = FREQ_LOCK_VERSION,
    cadence: str = "daily_fundamental",
    params: Mapping[str, Any] | None = None,
    signal_id: str = P2_EVAL_SIGNAL_ID,
    cross_version_merged: bool = False,
    substituted_test: str | None = None,
    primary_swapped: bool = False,
) -> dict[str, Any]:
    """单次截断回放。**纯函数**: 同样的入参一定得到同样的出参。

    ## 流程

    1. 校验评估对象是 `bss.daily_quality_value`(夹具/桩一律拒)
    2. 规整样本行 → `EvalRow`; 按 `announce_date <= asof` 截断 + 只留影子行 + 定序
    3. 主 horizon(`CALIB_PRIMARY_HORIZON_TD`)与辅 horizon(`CALIB_AUX_HORIZONS_TD`)摘要
    4. 过门: 调锁面 `p25_gate_verdict`, **不**自写 if 链
    5. 算 `fingerprint`(`freq_lock_fingerprint`, sha256 全 64 位)

    ## 几个参数是「违规申报口」, 默认全关

    `cross_version_merged` / `substituted_test` / `primary_swapped` 默认 `False` / `None` /
    `False`, 只有在**调用方主动声称**做过那件事时才置真。它们不是开关, 是**自报**:
    一旦置真, 结论直接 `failed`, 不看数字。存在的原因是让「我们合并了跨版本样本」这种
    事有一个能被记录、能被判负的入口 —— 而不是只能在代码里悄悄做掉。

    ## 返回值

    `status` 是**路径状态**(`P2_PATH_STATUS_*`), 不是 calib 状态; `gate_status` 是
    锁面判定(`passed` / `failed`)。两者分开: 把路径状态和校准状态混成一个字段, 是
    「路径已通」被读成「calib 已过」的主要途径。
    """
    if signal_id != P2_EVAL_SIGNAL_ID:
        raise P2EvalInputError(
            f"评估对象必须是 {P2_EVAL_SIGNAL_ID!r}, 收到 {signal_id!r}; "
            "夹具(fx.*) / volume_breakout / auction 均不在本评估器契约内"
        )
    asof_day = _parse_day(asof, field="asof")

    normalized: list[EvalRow] = [
        row if isinstance(row, EvalRow) else EvalRow.from_mapping(row) for row in rows
    ]
    selected = _select_shadow_rows(normalized, asof=asof_day)

    cost_bp = P25_COST_BP_ROUNDTRIP
    primary = _horizon_summary(
        selected, horizon_td=CALIB_PRIMARY_HORIZON_TD, cost_bp=cost_bp
    )
    aux = [
        _horizon_summary(selected, horizon_td=td, cost_bp=cost_bp)
        for td in CALIB_AUX_HORIZONS_TD
    ]

    sample_n = primary["sample_n"]
    hits = primary["hits"]

    # 过门: 一律走锁面, 不写第二套判据
    gate_status = p25_gate_verdict(
        sample_n=sample_n,
        hits=hits,
        cross_version_merged=cross_version_merged,
        substituted_test=substituted_test,
        primary_swapped=primary_swapped,
    )

    # 路径状态: 跑完了 = path_reached; 只有当锁面判定 passed 才是 gate_formally_met
    path_status = (
        P2_PATH_STATUS_GATE_MET
        if gate_status == CALIB_STATUS_PASSED
        else P2_PATH_STATUS_REACHED
    )

    effective_params = dict(params or {})
    fingerprint = freq_lock_fingerprint(
        signal_id=P2_EVAL_SIGNAL_ID,
        definition_version=definition_version,
        cadence=cadence,
        params=effective_params,
    )

    return {
        "schema_version": "p2_eval_v1",
        "batch": P2_EVAL_BATCH_TAG,
        "signal_id": P2_EVAL_SIGNAL_ID,
        "align_key": _align_key_payload(
            definition_version=definition_version,
            freq_lock_version=freq_lock_version,
        ),
        "definition_version": definition_version,
        "freq_lock_version": freq_lock_version,
        "cadence": cadence,
        "asof": asof_day,
        "pit_cutoff": PIT_CUTOFF_VALUE,
        "pit_rule": PIT_RULE_TEXT,
        "sample_count_domain": SAMPLE_COUNT_DOMAIN_VALUE,
        "cost_bp_roundtrip": cost_bp,
        "primary_horizon_td": CALIB_PRIMARY_HORIZON_TD,
        "aux_horizons_td": list(CALIB_AUX_HORIZONS_TD),
        "primary": primary,
        "aux": aux,
        "sample_n": sample_n,
        "hits": hits,
        "hit_rate": (hits / sample_n) if sample_n else 0.0,
        "wilson_lb": primary["wilson_lb"],
        "wilson_rule": WILSON_POSITIVE_RULE,
        "sample_n_min": P25_SAMPLE_N_MIN,
        "cross_version_merge_forbidden": P25_CROSS_VERSION_MERGE_FORBIDDEN,
        "holm_bh_substitute_forbidden": P25_HOLM_BH_SUBSTITUTE_FORBIDDEN,
        "primary_swap_forbidden": P25_PRIMARY_SWAP_FORBIDDEN,
        "gate_status": gate_status,
        "path_status": path_status,
        "fingerprint": fingerprint,
        "note": P2_EVAL_NOTE,
    }


def truncated_replay_twice_equal(
    **kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """同一输入跑两次, 返回两份结果供逐字段比对(含 `fingerprint`)。

    两处比对放在一起(而不是只返回第二次)是为了让测例能对**任何**字段做 diff:
    只比 `status` 的话, 采样顺序漂移、浮点累加顺序变化、指纹不稳定 —— 这三种
    「两次结果不一致」的典型成因都抓不到。
    """
    first = truncated_replay_once(**kwargs)
    second = truncated_replay_once(**kwargs)
    return first, second


__all__ = [
    "EvalRow",
    "P2_EVAL_BATCH_TAG",
    "P2_EVAL_NOTE",
    "P2_EVAL_SIGNAL_ID",
    "P2_EVAL_SPEC_REF",
    "P2_PATH_STATUS_GATE_MET",
    "P2_PATH_STATUS_REACHED",
    "P2EvalInputError",
    "truncated_replay_once",
    "truncated_replay_twice_equal",
]
