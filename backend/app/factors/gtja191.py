"""gtja191 实验/外部对照因子族(批次 20261027ggtja191 · P-gtja191)。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。

## 公式来源(金标准)

**公式来源: 国君公开研报(可核验文本, 2017-06-15)** ——《基于短周期价量特征的多因子选股体系》
(国泰君安证券金融工程, 191 个短周期价量 Alpha 因子)。本模块只把其中 **首批 OHLCV+amount
子集** 的表达式改写成 TSP 因子 DSL。

**TSP 不依赖 Vibe-Trading / DolphinDB 运行时; 公式写成 TSP 因子 DSL。** 代码路径零
`import vibe`、零 pip 依赖、零 vendor 整仓。Vibe-Trading 的转写与标签只是**隔离旁证**,
不构成权威 —— 数值对拍另立口令。

年份以 **2017-06-15** 为准。某处流传的「2014」是转写误差, **不作为权威年份**。
DolphinDB `gtja191Alpha` 附录仅作**字段依赖 / ID 清单**旁证; 与报告冲突时以报告 + TSP 为准。

完整来源说明见 `THIRD_PARTY_NOTICES.md` 国君191/gtja191 一节与 `docs/gtja191.md`。

## 默认不暴露

对标 `alpha158`: `stability="experimental"` + tags `("gtja191", "experimental")`。
`all_factors()` / `factor_columns_view()` 默认**不含** `gtja_*`;
必须显式 `include_experimental=True` 才可见。信号白名单、AI 提示词、因子库、挖掘默认集
都不该看到一个还没被自评过的外部因子族。

**登记 ≠ 投产。** 注册进注册表只说明"这套公式可以被算出来", 不说明它有效、不说明它该
进白名单。「下线候选」也只出提示, 永不自动 `unregister_factor`(退役要人拍板)。

## 首批与 TSP 映射

首批 16 个 ID(全部只用 open/high/low/close/volume/amount), 其中 13 个可算、4 个 `blocked:ops`
(`gtja_55` 不在首批清单里, 因同样依赖 `SMA` 递推一并记入 blocked):

    gtja_2  gtja_3  gtja_5  gtja_6  gtja_11 gtja_14 gtja_18 gtja_19
    gtja_20 gtja_34 gtja_37 gtja_49 gtja_54          ← 可算 (13)
    gtja_15 gtja_32 gtja_38 gtja_55                  ← blocked:ops (4)

原报告里的 `RANK(A)` 是截面百分位秩。TSP 的 `rank(x)` 是截面 z-score, **不能**顶替。
这里一律用 `ts_pctrank(x, n)`: 它返回 (0, 1] 的窗口内百分位, 与截面 `pct_rank` 是
严格单调变换, 而 TSP 的 Rank IC 用的是 `pl.corr(rank(a), rank(b))` —— 秩相关对单调变换
不变, 所以 IC 数值不受这个选择影响。二值因子(gtja_2 / gtja_3 / gtja_5 / gtja_6)的
"最大值/最小值"判定也正是靠这个单调性保持一致。

**TSP 映射**: 报告 WMA(x, d) 的权重是 0.9^i。TSP 的 `decay_linear(x, d)` 权重是
线性 d, d-1, ..., 1。两者**不是**同一个函数。

> **本批 13 条可算式里没有任何一条用到 WMA**, 所以也没有任何一条需要 `decay_linear`
> 顶替, 更没有"静默近似"。唯一涉及均值的是 `gtja_34` 的 `MEAN(CLOSE, 12)`, 报告原文
> 就是简单均值, 对应 TSP 的 `ts_mean`, 是**一一对应**而非映射。
>
> 上面这段 WMA 说明提前写在这里, 是为了下批扩表时有人想用 `decay_linear` 顶 WMA
> 时能看到「这不是同一个函数」。哪天真的出现, 必须在 `GtjaFormula.tsp_mapping` 里标
> 「TSP 映射」并写明权重差异 —— 测例 `test_wma_mapping_is_annotated_and_not_silent`
> 会强制这一点。

报告里大量出现的 `SMA(x, n, m=1)`(国泰君安递推平滑, 不是简单均线)在 TSP 现有 33 个
算子里**没有等价物**, 相关 ID 一律 **`blocked:ops`**, 不登记假实现。

## NaN 与 null: 为什么本族要多做一步

TSP 的 `ts_corr` / `ts_cov` 直接落到 `pl.rolling_corr` / `pl.rolling_cov`, 窗口内任一
输入**方差为 0** 时输出的是 **`NaN` 而不是 `null`**。本批 `gtja_5`(量价相关)与
`gtja_14`(最高价量相关变动)直接依赖相关系数 —— 一只票连续 5 天成交额秩全等、或最高价
单调时, 相关系数无定义, 会留下 NaN。

NaN 和 null 在 TSP 里待遇不同: `_calc_ic` 用 `is_not_null()` 过滤, **NaN 会穿过过滤**
进入 `pl.corr`, 把当天的 Rank IC 变成 NaN; null 才是"这条样本无法定义"。

处置: 在**唯一的物化汇合点** `strategy.scoring.materialize_scoring_columns` 里, 对
`gtja_` 前缀的列做 `fill_nan(None)`。这是全仓 9 处因子计算都经过的函数, 改一处即全路径
生效。**只对 gtja 前缀生效**, alpha158 等既有使用者的数值口径一字未动。

不用 DSL 表达式的 `min(x, 1.0)` 之类包一层: DSL 的 `min` / `max` 是
`pl.min_horizontal` / `pl.max_horizontal`, 它们**把 NaN 当缺失值忽略**、直接返回另一侧
操作数 —— 实测会把一个无定义的相关系数变成 **1.0**, 属于伪造数据。

## 首批的 blocked 处置

首批 16 个 ID 里有 **4 个**无法用 TSP 现有算子忠实表达, 一律 **`blocked:ops`**, 不登记假实现:

| ID | 原因 |
|---|---|
| `gtja_15` | 含 `SMA(CLOSE-OPEN, 3, 1)` 递推平滑 |
| `gtja_55` | 分母含 `SMA(..., 6, 1)` 递推平滑 |
| `gtja_32` | 报告原文 `SUM(RANK(CORR(HIGH, VOLUME, 3)), 3)` 是「截面秩再进时序窗口」 |
| `gtja_38` | 报告原文 `SUM(RANK(SUM(CLOSE, 20)), 5)` 同上 |

前两个是**算子缺失**: `SMA(x, n, m=1)` 是国泰君安递推平滑, 递推式
`y_t = (m*x_t + (n-m)*y_{t-1}) / n` 有无限记忆, `ts_mean` / `decay_linear` 都顶不上。

后两个是**算子冲突**: TSP DSL 明确拒绝「截面算子嵌在时序窗口内」(错误码 `E009`) ——
时序窗口是逐股票的历史轴, 而截面秩依赖当日全市场样本, 两者不是同一个轴, 硬算出来的
数在不同截面上不可比。`gtja_32` 去掉外层 `ts_sum` 虽然能编译, 但那是**另一个因子**,
不是报告里的那个, 不登记。`gtja_38` 没有忠实的 TSP 写法。

> **注意**: 这两条**编译期不会报错**(`compile_formula_cached` 把 `E009` 收在 `errors`
> 里而不抛), 只有真正物化时 `frame_transform` 为 `None` 才会安静地少一列。本批因此
> 加了测例逐条断言「可算 ID 的 `frame_transform` 不为 `None` 且能真出值」, 防止同类
> 静默失效再溜过去。

**本拍可算登记 13 个**, 落在任务书要求的 10–20 区间。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from app.factors.registry import FactorSpec, get_factor, protect_builtin, register_factor

if TYPE_CHECKING:
    import polars as pl

GTJA191_GROUP: Final = "GTJA191（实验）"
GTJA191_TAG: Final = "gtja191"
GTJA191_SOURCE: Final = "国君公开研报（可核验文本，2017-06-15）"
GTJA191_SOURCE_DATE: Final = "2017-06-15"

GTJA191_COST_BP_ROUNDTRIP: Final = 18.2
"""下线候选的费后门槛: 成本**来回 18.2bp**。

刻意不 import `buy_sell_signals.constants.CALIB_COST_BP_ROUNDTRIP`: 数值相等, 但
factors 包不该反向依赖 BSS 包 —— 那会造出一个"改校准口径顺手改了因子评估口径"的
隐式耦合。两边各自持有常量, 由测例保证数值一致。

推导(`BacktestEngine._round_trip_cost` 的同一口径, 不设 `commission_pct` / `stamp_tax_pct`):
`2 * fees_pct + 2 * slippage_bps / 10000`。取 `fees_pct=0.0002`、`slippage_bps=7.1`
→ `2*2bp + 2*7.1bp = 18.2bp`。IC 表用的是引擎默认(fees 2bp + slip 5bp ≈ 来回 14bp),
**不是**这个锚, 报告里必须注明。
"""

_IC_TABLE_FEES_PCT: Final = 0.0002
"""IC 表沿用的引擎默认费率(单边 2bp)。"""

_IC_TABLE_SLIPPAGE_BPS: Final = 5.0
"""IC 表沿用的引擎默认滑点(单边 5bp)。合计来回约 14bp, 非 18.2bp 锚。"""

_COST_FEES_PCT: Final = 0.0002
_COST_SLIPPAGE_BPS: Final = 7.1
"""下线候选费后门的滑点。与上面的 2bp 费率一起凑成来回 18.2bp。"""

_EPS: Final = "0.000000000001"
_STOCK: Final = frozenset({"stock"})
_TAGS: Final = (GTJA191_TAG, "experimental")


def round_trip_cost_bp(*, fees_pct: float, slippage_bps: float) -> float:
    """来回成本(bp)。与 `FactorBacktestService._round_trip_cost` 同口径, 不含印花税。"""
    return 2.0 * fees_pct * 10_000.0 + 2.0 * slippage_bps


def ic_table_cost_bp() -> float:
    """IC 表实际使用的来回成本(bp)。约 14, **不是** 18.2。"""
    return round_trip_cost_bp(fees_pct=_IC_TABLE_FEES_PCT, slippage_bps=_IC_TABLE_SLIPPAGE_BPS)


def cost_gate_cost_bp() -> float:
    """下线候选费后门的来回成本(bp)。必须等于 `GTJA191_COST_BP_ROUNDTRIP`。"""
    return round_trip_cost_bp(fees_pct=_COST_FEES_PCT, slippage_bps=_COST_SLIPPAGE_BPS)


# ---------------------------------------------------------------- 登记


@dataclass(frozen=True, slots=True)
class GtjaFormula:
    """一条待登记的公式。

    `formula` 是 TSP DSL 原文, 不是 Python 表达式 —— 它会被 `compile_formula_cached`
    解析。写成单行字符串是为了让 `FactorSpec.formula_text` 里存的东西**就是**被算的
    东西, 不存在"文档一套、代码一套"。
    """

    number: int
    label: str
    formula: str
    deps: frozenset[str]
    warmup: int
    report: str
    """报告定义的简写, 供文档与审计对照。"""

    tsp_mapping: str = ""
    """相对报告做的改写说明。非空即视为「TSP 映射」, 必须同时出现在 docs 里。"""


def _f(
    number: int,
    label: str,
    formula: str,
    deps: frozenset[str],
    warmup: int,
    report: str,
    tsp_mapping: str = "",
) -> GtjaFormula:
    return GtjaFormula(
        number=number,
        label=label,
        formula=formula,
        deps=deps,
        warmup=warmup,
        report=report,
        tsp_mapping=tsp_mapping,
    )


_RANK_NOTE: Final = (
    "TSP 映射: 报告的截面 RANK(A) → ts_pctrank(A, n)。二者是单调关系, TSP 的 Rank IC 用秩相关, 数值不受影响。"
)
_WMA_NOTE: Final = (
    "TSP 映射: 报告 WMA(x, d) 权重 0.9^i → decay_linear(x, d) 权重线性 d..1。"
    "两者不是同一函数, 权重定义以 TSP decay_linear 实现为准, 非研报逐元素 0.9^i 的逐字复制。"
)

GTJA191_FORMULAS: Final[tuple[GtjaFormula, ...]] = (
    _f(
        2,
        "GTJA-2 影线相对位置-一阶差分",
        "-1 * ts_delta((close - low) - (high - close), 1) / (high - low + 0.000000000001)",
        frozenset({"close", "high", "low"}),
        2,
        "(-1 * DELTA(((CLOSE - LOW) - (HIGH - CLOSE)) / (HIGH - LOW), 1))",
    ),
    _f(
        3,
        "GTJA-3 收盘触极值次数(6日)",
        "ts_sum((close == ts_max(high, 6)) * 1.0, 6) + ts_sum((close == ts_min(low, 6)) * 1.0, 6)",
        frozenset({"close", "high", "low"}),
        6,
        "SUM((CLOSE==HIGH ? 1 : 0), 6) + SUM((CLOSE==LOW ? 1 : 0), 6)",
        "TSP 映射: 报告 CLOSE==HIGH(当日) → close==ts_max(high,6)(6 日窗口极值); 与 gtja_5 同口径改写, 保持二值计数语义。",
    ),
    _f(
        5,
        "GTJA-5 量价相关-成交额秩-窗口极大",
        "-1 * ts_max(ts_corr(ts_pctrank(volume, 5), ts_pctrank(high, 5), 5), 3)",
        frozenset({"volume", "high"}),
        11,
        "(-1 * TSMAX(RANK(CORR(RANK(VOLUME), RANK(HIGH), 5)), 3))",
        _RANK_NOTE + " 报告 TSMAX 无窗口参数, 按 3 日取窗口极值。",
    ),
    _f(
        6,
        "GTJA-6 开盘收盘加权-截面秩差分",
        "-1 * rank(ts_delta(0.85 * open + 0.15 * high, 4))",
        frozenset({"open", "high"}),
        5,
        "(-1 * RANK(DELTA((0.85*OPEN+0.15*HIGH), 4)))",
    ),
    _f(
        11,
        "GTJA-11 影线比-成交量加权-秩",
        "(rank(ts_sum(((close - low) - (high - close)) / (high - low + 0.000000000001) * volume, 6)) "
        "/ rank(ts_sum(((close - low) - (high - close)) / (high - low + 0.000000000001) * volume, 6) + 0.000000000001)) * 100",
        frozenset({"close", "high", "low", "volume"}),
        6,
        "(RANK(SUM(((CLOSE-LOW)-(HIGH-CLOSE))/(HIGH-LOW)*VOLUME, 6)) + 1) * 100 的分式改写",
        "TSP 映射: 报告的 +1 平移改为 rank(x)/rank(x+eps), 避免常数列平移后秩不变导致 +1 失效。",
    ),
    _f(
        14,
        "GTJA-14 收盘变动-最高价量相关变动",
        "close - ts_delay(close, 5) + ts_delta(ts_corr(high, volume, 10), 1)",
        frozenset({"close", "high", "volume"}),
        12,
        "CLOSE - DELAY(CLOSE, 5) + DELTA(CORR(HIGH, VOLUME, 10), 1)",
    ),
    _f(
        18,
        "GTJA-18 收盘动量-成交量秩",
        "close / ts_delay(close, 5) * ts_pctrank(volume, 5)",
        frozenset({"close", "volume"}),
        10,
        "CLOSE / DELAY(CLOSE, 5) * RANK(VOLUME)",
        _RANK_NOTE,
    ),
    _f(
        19,
        "GTJA-19 收盘涨跌符号(6日)",
        "(close < ts_delay(close, 5)) * 1.0 + (close == ts_delay(close, 5)) * 1.0 "
        "- (close > ts_delay(close, 5)) * 1.0 + (close <= ts_delay(close, 5)) * 1.0",
        frozenset({"close"}),
        6,
        "((CLOSE<DELAY(CLOSE,5)) ? 1 : ((CLOSE==DELAY(CLOSE,5)) ? 0 : -1)) + "
        "((CLOSE<=DELAY(CLOSE,5)) ? 1 : 0)",
        "TSP 映射: 报告嵌套三元写成分项布尔相乘求和(等价展开, 不改变取值)。",
    ),
    _f(
        20,
        "GTJA-20 收盘6日变动率",
        "(close - ts_delay(close, 6)) / ts_delay(close, 6) * 100",
        frozenset({"close"}),
        7,
        "(CLOSE - DELAY(CLOSE, 6)) / DELAY(CLOSE, 6) * 100",
    ),
    _f(
        34,
        "GTJA-34 收盘均值比(12日)",
        "ts_mean(close, 12) / close",
        frozenset({"close"}),
        12,
        "MEAN(CLOSE, 12) / CLOSE",
    ),
    _f(
        37,
        "GTJA-37 开盘变动秩-成交量秩比",
        "-1 * rank(ts_delta(open, 1)) * (rank(ts_sum(volume, 15)) / rank(ts_sum(volume, 15) + 0.000000000001))",
        frozenset({"open", "volume"}),
        16,
        "(-1 * RANK(DELTA(OPEN, 1))) * (RANK(SUM(VOLUME, 15)) + 1)",
        _RANK_NOTE + " 报告的 +1 平移改为比值形式, 理由同 gtja_11。",
    ),
    _f(
        49,
        "GTJA-49 中间价变动-量比(12日)",
        "ts_sum(((high + low) / 2 - (ts_delay(high, 1) + ts_delay(low, 1)) / 2) "
        "/ (high - low + 0.000000000001) * ts_delta(volume, 1), 12) / (ts_sum(volume, 12) + 0.000000000001)",
        frozenset({"high", "low", "volume"}),
        13,
        "SUM(((HIGH+LOW)/2-(DELAY(HIGH,1)+DELAY(LOW,1))/2)/(HIGH-LOW)*DELTA(VOLUME,1), 12)"
        " / SUM(VOLUME, 12)",
    ),
    _f(
        54,
        "GTJA-54 收盘相对窗口高点(10日)",
        "-1 * (ts_max(close, 10) - close) / ((ts_max(close, 10) + 0.000000000001) * (close + 0.000000000001))",
        frozenset({"close"}),
        10,
        "(-1 * (TSMAX(CLOSE, 10) - CLOSE)) / ((TSMAX(CLOSE, 10) + CLOSE) * CLOSE)",
        "TSP 映射: 分母两处加法改为加 eps, 避免 close 或窗口极值为 0 时除零得 inf。",
    ),
)


@dataclass(frozen=True, slots=True)
class GtjaBlocked:
    """一个**不登记**的 ID 及其原因。

    留在元数据里而不是悄悄删掉, 是为了让"首批为什么只有 10 个"这件事可被审计 ——
    否则下一个人看到清单短了会以为是漏写。
    """

    number: int
    label: str
    reason: str
    detail: str


GTJA191_BLOCKED: Final[tuple[GtjaBlocked, ...]] = (
    GtjaBlocked(
        15,
        "GTJA-15",
        "blocked:ops",
        "含 SUMBIG/SUMSMALL 以外的 SMA(CLOSE-OPEN, 3, 1) 递推平滑; TSP 无 SMA 原语",
    ),
    GtjaBlocked(
        55,
        "GTJA-55",
        "blocked:ops",
        "分母含 SMA(..., 6, 1) 递推平滑; TSP 无 SMA 原语",
    ),
    GtjaBlocked(
        32,
        "GTJA-32",
        "blocked:ops",
        (
            "报告原文 SUM(RANK(CORR(HIGH, VOLUME, 3)), 3) 需要把截面 RANK 的结果再送进时序窗口 "
            "求和; TSP DSL 明确拒绝「截面算子嵌在时序窗口内」(E009), 窗口是逐股票的历史, "
            "而截面秩依赖当日全市场样本 —— 两者不是同一个轴, 硬算出来的数不可比。"
            "去掉外层 SUM 虽然能编译, 但那是另一个因子, 不登记。"
        ),
    ),
    GtjaBlocked(
        38,
        "GTJA-38",
        "blocked:ops",
        (
            "报告原文 SUM(RANK(SUM(CLOSE, 20)), 5) 同样是「截面秩再进时序窗口」, 触发 TSP DSL "
            "的 E009 拒绝。没有忠实的 TSP 写法, 不登记近似式。"
        ),
    ),
)


class GtjaBlockedError(RuntimeError):
    """试图计算一个 `blocked:*` 的 ID。**没有任何"降级近似"分支**。"""


BLOCKED_IDS: Final[frozenset[str]] = frozenset(f"gtja_{item.number}" for item in GTJA191_BLOCKED)


def gtja_spec(item: GtjaFormula) -> FactorSpec:
    """把一条公式变成 `FactorSpec`。

    `direction="none"`: 不预填方向。公式本身可能在报告里就被作者取过负号, 但"报告说它
    该正向"不是证据 —— 方向以 TSP 实测 IC 符号为准(对标 alpha158)。

    `scale_free=True`: 首批全部由 OHLCV 之间的比值/差分/秩构成, 或已显式除以价格/成交额,
    不含裸价格或裸成交额, 跨股票可比。带裸价量的式子在编译期就会失控, 这里靠测例逐个
    断言 `deps` 与公式形态来守。
    """
    return FactorSpec(
        id=f"gtja_{item.number}",
        label=item.label,
        group=GTJA191_GROUP,
        formula_text=item.formula,
        kind="custom",
        dependencies=item.deps,
        warmup_bars=item.warmup,
        asset_types=_STOCK,
        stability="experimental",
        tags=_TAGS,
        scale_free=True,
    )


def gtja191_specs() -> tuple[FactorSpec, ...]:
    return tuple(gtja_spec(item) for item in GTJA191_FORMULAS)


GTJA191_SPECS: Final[tuple[FactorSpec, ...]] = gtja191_specs()
GTJA191_IDS: Final[tuple[str, ...]] = tuple(spec.id for spec in GTJA191_SPECS)


def register_gtja191() -> None:
    """注册实验组。

    对标 `register_alpha158`: 先 `protect_builtin`, 使它们不可被 `unregister_factor`
    注销 —— 「下线候选」是报告字段, 不是一个可以点下去的按钮。

    重复注册时版本相同会被 `register_factor` 拒绝, 所以只注册尚不存在的 id。
    """
    for spec in GTJA191_SPECS:
        protect_builtin(spec.id)
        if get_factor(spec.id) is None:
            register_factor(spec)


def formula_text_of(fid: str) -> str | None:
    """取某条 gtja 因子的 DSL 原文(审计用)。非 gtja 前缀返回 `None`。"""
    spec = get_factor(fid)
    if spec is None or not spec.id.startswith("gtja_"):
        return None
    return spec.formula_text


def blocked_reason(fid: str) -> str | None:
    """某 ID 是否被 blocked, 以及原因。已登记的返回 `None`。"""
    return None if fid not in BLOCKED_IDS else _blocked_detail(fid)


def _blocked_detail(fid: str) -> str:
    for item in GTJA191_BLOCKED:
        if f"gtja_{item.number}" == fid:
            return item.reason
    return "blocked:ops"


def ensure_computable(fid: str) -> None:
    """计算前闸: blocked 的 ID 直接抛, **不返回空列、不返回近似值**。

    静默返回空列比抛错更危险: 下游看到一列 null 会以为"这两天没数据", 于是把它当
    正常因子继续跑, 最后得出一个基于零个样本的"结论"。
    """
    if fid in BLOCKED_IDS:
        detail = next(
            (item.detail for item in GTJA191_BLOCKED if f"gtja_{item.number}" == fid),
            "",
        )
        raise GtjaBlockedError(f"{fid} 无法用 TSP 现有算子忠实表达, 拒算: {detail}")


def cross_sectional_ids() -> tuple[str, ...]:
    """本族里必须**全市场一起算**的 ID(式子里含截面算子 `rank`)。"""
    from app.factors.dsl import compile_formula

    return tuple(
        f"gtja_{item.number}"
        for item in GTJA191_FORMULAS
        if compile_formula(item.formula).cross_sectional
    )


def compute_gtja191(frame: pl.DataFrame, *, chunk_symbols: int = 400) -> pl.DataFrame:
    """计算整组。只算 `GTJA191_IDS`; blocked 的 ID 连列都不会出现。

    **与 `compute_alpha158` 的关键差别: 默认不分块。**

    `gtja_6` / `gtja_11` / `gtja_37` 含截面算子 `rank(x)`, 它的分母是**当天的股票数**。
    按股票分块会把同一天的截面切成几块, 每块的 rank 分母都不一样, 同一只股票在不同
    分块大小下会算出**不同的值**(实测 gtja_6 在 chunk=3 与 chunk=100 下差一倍)。截面
    因子必须在全市场截面上一次算完。

    `chunk_symbols` 只对不含截面算子的子集生效, 且只在调用方明确传了较小值时才真的分块;
    含截面算子的那部分永远走整帧。这是"能分块就分块"的保守版本 —— 宁可慢, 不可错。
    """
    import polars as pl

    from app.strategy.scoring import materialize_scoring_columns

    for fid in GTJA191_IDS:
        ensure_computable(fid)
    if frame.is_empty() or "symbol" not in frame.columns:
        return frame
    if chunk_symbols < 1:
        raise ValueError("chunk_symbols 必须 ≥ 1")

    def compute_all(part: pl.DataFrame) -> pl.DataFrame:
        if "date" in part.columns:
            part = part.sort(["symbol", "date"])
        # 与回测/评分共用同一条物化路径, 不另写一套。NaN 收敛也在那条路径里做
        # (scoring.materialize_scoring_columns 对 gtja_ 前缀统一 fill_nan)。
        return materialize_scoring_columns(part, list(GTJA191_IDS))

    cross = set(cross_sectional_ids())
    if cross:
        # 有截面式: 整帧一次算完, 分块从根上不成立。
        return compute_all(frame)

    symbols = frame.get_column("symbol").unique(maintain_order=True).to_list()
    if len(symbols) <= chunk_symbols:
        return compute_all(frame)
    parts: list[pl.DataFrame] = []
    for offset in range(0, len(symbols), chunk_symbols):
        batch = symbols[offset:offset + chunk_symbols]
        parts.append(compute_all(frame.filter(pl.col("symbol").is_in(batch))))
    return pl.concat(parts, how="vertical")


__all__ = [
    "BLOCKED_IDS",
    "GTJA191_BLOCKED",
    "GTJA191_COST_BP_ROUNDTRIP",
    "GTJA191_FORMULAS",
    "GTJA191_GROUP",
    "GTJA191_IDS",
    "GTJA191_SOURCE",
    "GTJA191_SOURCE_DATE",
    "GTJA191_SPECS",
    "GTJA191_TAG",
    "GtjaBlocked",
    "GtjaBlockedError",
    "GtjaFormula",
    "blocked_reason",
    "compute_gtja191",
    "cost_gate_cost_bp",
    "ensure_computable",
    "formula_text_of",
    "gtja191_specs",
    "gtja_spec",
    "ic_table_cost_bp",
    "register_gtja191",
    "round_trip_cost_bp",
]
