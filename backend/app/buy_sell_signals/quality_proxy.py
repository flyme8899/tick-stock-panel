"""日线质量代理: 用**已有列**拼出一个"这家公司基本面是否可评估"的粗判。

## 这一层不做的事

- 不新造因子。只用 `financials_metrics` 里已经存在的列(`roe` / `gross_margin` /
  `debt_to_asset_ratio` / `net_income_yoy` / `revenue_yoy` / `eps` / `bps`)。
- 不定权重。`w_quality` / `w_value` 留 `None` + `weighting_status="design_pending"`。
  权重是要靠回测定的, 骨架阶段随手填一个 0.3 / 0.7, 之后所有人都会把它当成结论。
- 不打分。只产出一个 `q_score` 的**原始分**(0–7, 七列里取值为正的项数), 并明示
  它是代理而非质量因子。

## 这一层必须做对的事: 财务的 PIT

`financials_metrics` 每行有一个 `announce_date`(公告日)和一个 `period_end`(报告期)。
回看 `asof` 那天时, 只能用 **`announce_date <= asof`** 的行。

用 `period_end <= asof` 是错的, 而且错得很隐蔽: 三季报 9 月 30 日截止, 但 10 月底
才公告。用 period_end 判, 你在 10 月 1 日就知道了一家 10 月 28 日才公布的好业绩。
这个偏差在回测里表现为"策略总是恰好在财报前买入", 而实盘复现不出来。

公告前字段一律 `None`, **绝不填 0**: 0 是一颗真实的数字(ROE=0 表示不赚钱),
`None` 才是"还不知道"。混起来之后 `q_missing_flag` 就失去意义了。

## 日线部分

日线只用于判断"有没有可用的行情"和取 `asof` 当天收盘。行情缺失同样标
`q_missing_flag`, 而不是用 0 或前值补 —— 停牌与数据缺口是两回事, 而这一层
分不清就不要假装分得清。

## 字段契约(批次 20261025gval 对齐 PHASE2 §3.3)

本批次**只加深契约, 不新造因子**。`QUALITY_METRIC_COLUMNS` 七列一列未加,
权重仍 `None` + `design_pending`。下表把每个字段"是什么 / 不是什么都"钉死,
免得下游拿它当质量因子:

| 字段 | 是什么 | **不是**什么 |
|---|---|---|
| `q_score` | 七项指标里"为正"的项数, 0–7 的计数 | **不是**质量因子值, 不能排序、不能当 alpha |
| `score_emitted` | `q_score is not None` | 不表示"分数可用", 只表示"算出来了" |
| `q_missing_flag` | 有任一原因码, **或**`q_score is None` | 不表示"质量差", 只表示"数据不足以判断" |
| `q_missing_reasons` | 缺失/异常的**闭集**原因码(见下) | 不是自然语言说明, 不做文案展示 |
| `metrics` | 七列在 `asof` 的值, 不可知为 `None` | **不是**填过默认值的完整向量 |
| `metrics_asof_announce` | 取到的那期财报的公告日 | 不是报告期(`period_end`) |
| `metrics_period_end` | 取到的那期财报的报告期末 | 不是可知日 —— 二者不可互换 |
| `price_coverage` | 近窗口工作日里有收盘价的比例 | 不是动量、不是波动率 |
| `last_close` | 窗口内最后一根收盘价 | 不是 `asof` 复权后的可比价 |
| `w_quality` / `w_value` | 恒 `None` | 不是"待填的 0", 不是暂定 0.5 |
| `weighting_status` | 恒 `design_pending` | 不是 `draft` / `trial` 之类的中间态 |

`q_missing_reasons` 是**闭集**, 新增码要同时改这里的文档与测例:

| 原因码 | 触发条件 |
|---|---|
| `no_financials_known_at_asof` | `announce_date <= asof` 的行一行都没有 |
| `missing_column:<col>` | 表里根本没有这一列 |
| `null_metric:<col>` | 有这一列, 但这期为 `None` |
| `bad_metric:<col>` | 有值但不能转成 float |
| `no_kline_source` | 调用方没给 kline(或给了空表) |
| `no_kline_rows` | 有来源, 但窗口内一个收盘价都没有 |
| `kline_coverage_low:<x>` | 窗口覆盖率 < `MIN_PRICE_COVERAGE` |

注意 `q_missing_flag` 与 `score_emitted` 是**两个方向**的标记: 前者说"数据够不够",
后者说"分出来没有"。二者可能同时为真(数据缺失导致没出分), 但不能互相推导 ——
所以分开存, 不要用一个字段兼表两件事。

## 这一层**不能**做的事

- **不能**在没有 `announce_date` 的表上跑 PIT。缺列直接返回空表, 不退回按
  `period_end` 近似 —— 那是偷看, 而且偷看的方向永远对策略有利。
- **不能**把 `metrics` 里缺失的值补成行业均值 / 中位数再算 `q_score`。补完之后
  `q_missing_flag` 就永远是 False, 这一层也就没有存在的意义了。
- **不能**用 `q_score` 直接排序选股。它是计数不是强度: `roe=0.1%` 与 `roe=80%`
  都只贡献 1 分, 拿它排序等于把所有正数当成一样好。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import polars as pl

from .models import asof_date

logger = logging.getLogger(__name__)

QUALITY_METRIC_COLUMNS: Final[tuple[str, ...]] = (
    "roe",
    "gross_margin",
    "debt_to_asset_ratio",
    "net_income_yoy",
    "revenue_yoy",
    "eps",
    "bps",
)
"""参与质量代理的财务列。全部来自 `financials_metrics` 现有列, 不新增因子。

**批次 20261025gval 一列未加。** 想加列之前先回答"它的 PIT 怎么做": 没有
`announce_date` 的列加进来, 整条腿的 PIT 就漏了。
"""

QUALITY_MISSING_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "no_financials_known_at_asof",
        "no_kline_source",
        "no_kline_rows",
    }
)
"""`q_missing_reasons` 里**不带参数**的原因码(闭集的一部分)。

带参数的两类(`missing_column:<col>` / `null_metric:<col>` / `bad_metric:<col>` /
`kline_coverage_low:<x>`)前缀也在这里列一次, 便于测例按前缀断言:

- `missing_column:`
- `null_metric:`
- `bad_metric:`
- `kline_coverage_low:`

新增原因码必须同时改这份集合、`QUALITY_MISSING_REASON_PREFIXES` 与文档表。
"""

QUALITY_MISSING_REASON_PREFIXES: Final[tuple[str, ...]] = (
    "missing_column:",
    "null_metric:",
    "bad_metric:",
    "kline_coverage_low:",
)
"""带参数的原因码前缀。与 `QUALITY_MISSING_REASON_CODES` 合起来才是全闭集。"""

QUALITY_WEIGHT_STATUS: Final = "design_pending"
"""质量/估值权重状态。

本批次**不定权重**。任何把 `w_quality` / `w_value` 填成具体数字的改动, 都应该先有
回测支撑, 并把这里改成对应的已定版标识。

「健康不进权重」同理: 没有 `w_health` 这个字段, 也不允许出现 —— 健康是 veto
(一票否决), 不是加权项。把否决权摊成权重, 等于允许"用高估值抵消财务异常"。
"""

PRICE_LOOKBACK_DAYS: Final = 20
"""日线代理看最近多少个交易日: 用来判"有没有行情", 不做动量计算。"""

MIN_PRICE_COVERAGE: Final = 0.6
"""最近窗口里至少要有六成交易日有数据, 否则视为行情缺失。

阈值本身是代理的一部分, 不精确; 它只负责区分"这只票有正常交易"和"长期停牌/没有
行情"。
"""


@dataclass(frozen=True, slots=True)
class QualityProxy:
    """一只票在 `asof` 的质量代理结果。

    字段语义见模块 docstring 的「字段契约」表。**`q_score` 是计数不是因子值。**
    """

    symbol: str
    asof: date
    q_score: int | None = None
    """满足的质量项数量(0–len(QUALITY_METRIC_COLUMNS)), 未评分时为 None。

    **不是**质量因子值, 不要拿它当 alpha。要看的是 `q_missing_flag` 与各项明细。

    判据是"为正"而不是"多高": `roe=0.1%` 与 `roe=80%` 都算 1 分。所以这个数
    只能用来做粗筛, 排序是它做不到的事。
    """
    metrics: dict[str, float | None] = field(default_factory=dict)
    """七列在 `asof` 的值。不可知一律 `None`, **绝不填 0 / 行业均值 / 中位数**。"""
    metrics_asof_announce: date | None = None
    """取到的那期财报的**公告日**。`None` 表示当期没有已知财报。"""
    metrics_period_end: date | None = None
    """取到的那期财报的**报告期末**。与公告日不是一回事, 不可互换。"""
    q_missing_flag: bool = True
    """数据是否不足以判断。默认 True —— 没跑过的对象不默认健康。"""
    q_missing_reasons: tuple[str, ...] = ()
    """闭集原因码, 见 `QUALITY_MISSING_REASON_CODES` + `QUALITY_MISSING_REASON_PREFIXES`。"""
    price_coverage: float | None = None
    """窗口覆盖率。`None` = 没有行情来源; `0.0` = 有来源但窗口内没数据。"""
    last_close: float | None = None
    w_quality: float | None = None
    """质量权重。**恒 None** —— 权重未标定(`QUALITY_WEIGHT_STATUS`)。"""
    w_value: float | None = None
    """估值权重。**恒 None** —— 估值腿仍 `data_pending`, 没有可加权的东西。"""
    weighting_status: str = QUALITY_WEIGHT_STATUS

    @property
    def score_emitted(self) -> bool:
        """是否产出了分数。缺数据时为 False —— 下游据此决定"这条信号能不能算"。"""
        return self.q_score is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat(),
            "q_score": self.q_score,
            "score_emitted": self.score_emitted,
            "q_missing_flag": self.q_missing_flag,
            "q_missing_reasons": list(self.q_missing_reasons),
            "metrics": dict(self.metrics),
            "metrics_asof_announce": _iso(self.metrics_asof_announce),
            "metrics_period_end": _iso(self.metrics_period_end),
            "price_coverage": self.price_coverage,
            "last_close": self.last_close,
            "w_quality": self.w_quality,
            "w_value": self.w_value,
            "weighting_status": self.weighting_status,
        }


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


# ---------------------------------------------------------------------------
# 财务 PIT
# ---------------------------------------------------------------------------


def _to_date_column(frame: pl.DataFrame, column: str) -> pl.DataFrame:
    if column not in frame.columns:
        return frame
    dtype = frame.schema[column]
    if dtype == pl.Date:
        return frame
    if dtype in (pl.Utf8, pl.Categorical):
        return frame.with_columns(
            pl.col(column).str.slice(0, 10).str.strptime(pl.Date, "%Y-%m-%d", strict=False).alias(column)
        )
    return frame.with_columns(pl.col(column).cast(pl.Date, strict=False).alias(column))


def financials_asof(
    financials: pl.DataFrame,
    symbol: str,
    asof: date | datetime,
) -> pl.DataFrame:
    """取某只票在 `asof` **当时已可知**的最新一期财务行。

    已可知 = `announce_date <= asof`。没有 `announce_date` 的行**不参与** —— 一个
    不知道什么时候公告的财务数字, 无法证明它在 `asof` 那天已经公开。宁可少用一个
    数据点, 也不要在回测里偷看。

    同一 `announce_date` 有多期时(修复公告、追溯调整), 取 `period_end` 更新的那一期。
    """
    day = asof_date(asof)
    if financials.is_empty() or "symbol" not in financials.columns:
        return pl.DataFrame()

    frame = _to_date_column(financials, "announce_date")
    if "period_end" in frame.columns:
        frame = _to_date_column(frame, "period_end")

    subset = frame.filter(pl.col("symbol") == symbol)
    if subset.is_empty():
        return pl.DataFrame()
    if "announce_date" not in subset.columns:
        # 整张表没有公告日: 无法做 PIT, 直接返回空。静默按 period_end 处理是
        # 本模块最不该犯的错。
        logger.debug("financials 表缺 announce_date 列, 无法做 PIT: 跳过 %s", symbol)
        return pl.DataFrame()

    known = subset.filter(pl.col("announce_date").is_not_null() & (pl.col("announce_date") <= day))
    if known.is_empty():
        return pl.DataFrame()

    sort_keys = ["announce_date"] + (["period_end"] if "period_end" in known.columns else [])
    return known.sort(sort_keys).tail(1)


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------


def _price_window(
    kline: pl.DataFrame | None,
    symbol: str,
    asof: date,
) -> tuple[float | None, float | None]:
    """返回 (近窗口覆盖率, 窗口内最后收盘价)。

    kline 为空(None 或空表)时返回 (None, None) —— 表示"没有行情来源", 与"有来源但
    这只票没数据"(coverage=0.0)区分开。

    覆盖率的分母是**该票在 asof 之前的第一行到 asof 的工作日跨度**(封顶
    `PRICE_LOOKBACK_DAYS`), 分子是这段时间里实际有收盘价的行数。不能拿"出现过的
    行数"当分母 —— 那样一只只出现过 3 天的票也是 100% 覆盖, 正好把最需要标出来的
    长期停牌/退市前个股判成健康。

    节假日不做扣除(没有交易日历), 所以覆盖率会略微偏保守 —— 宁可多标几个缺失,
    也不要把缺口藏起来。
    """
    if kline is None or kline.is_empty():
        return None, None
    frame = _to_date_column(kline, "date")
    if "date" not in frame.columns or "symbol" not in frame.columns:
        return None, None
    subset = frame.filter((pl.col("symbol") == symbol) & (pl.col("date") <= asof))
    if subset.is_empty():
        return 0.0, None
    if "close" not in subset.columns:
        return 0.0, None

    priced = subset.filter(pl.col("close").is_not_null()).sort("date")
    if priced.is_empty():
        return 0.0, None

    # 窗口固定以 asof 为终点往回推 PRICE_LOOKBACK_DAYS 个自然日。
    #
    # 这里**不能**从该票的第一行开始算跨度: 那只票第一次出现的那天, 跨度是 1 天,
    # 覆盖率恒为 100% —— 一只三年没交易的票恰好在这天有个价格, 就会被判成"行情
    # 覆盖完整"。窗口必须由查询侧给定, 不能让数据自己决定看多长。
    window_start = asof - timedelta(days=PRICE_LOOKBACK_DAYS - 1)
    in_window = priced.filter(pl.col("date") >= window_start)
    if in_window.is_empty():
        # 窗口内一个价格都没有: asof 之前虽然有过行情, 但最近 PRICE_LOOKBACK_DAYS
        # 天是空的(长期停牌 / 退市整理)。覆盖率 0, 且没有"最近的收盘价"可言。
        return 0.0, None

    # 分母按工作日估: 周一至周五。节假日会让分母偏大, 结果是覆盖率偏低(保守)。
    weekdays = sum(
        1
        for offset in range(PRICE_LOOKBACK_DAYS)
        if (window_start + timedelta(days=offset)).weekday() < 5
        and (window_start + timedelta(days=offset)) <= asof
    )
    coverage = (in_window.height / weekdays) if weekdays else 0.0
    coverage = min(coverage, 1.0)
    last_close = float(in_window["close"][-1])
    return coverage, last_close


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def evaluate_quality(
    symbol: str,
    asof: date | datetime,
    *,
    financials: pl.DataFrame | None,
    kline: pl.DataFrame | None,
) -> QualityProxy:
    """算一只票在 `asof` 的质量代理。

    `financials` / `kline` 由调用方传入(而不是这里读盘), 是为了让这一层是纯函数:
    测试直接喂小表, 不依赖任何本地数据目录。
    """
    day = asof_date(asof)
    reasons: list[str] = []

    metrics: dict[str, float | None] = {col: None for col in QUALITY_METRIC_COLUMNS}
    announce: date | None = None
    period_end: date | None = None
    q_score: int | None = None

    row_df = financials_asof(financials, symbol, day) if financials is not None else pl.DataFrame()
    if row_df.is_empty():
        reasons.append("no_financials_known_at_asof")
    else:
        row = row_df.row(0, named=True)
        announce = row.get("announce_date")
        period_end = row.get("period_end")
        for col in QUALITY_METRIC_COLUMNS:
            if col not in row:
                reasons.append(f"missing_column:{col}")
                continue
            value = row.get(col)
            if value is None:
                reasons.append(f"null_metric:{col}")
                continue
            try:
                metrics[col] = float(value)
            except (TypeError, ValueError):
                reasons.append(f"bad_metric:{col}")
                continue
        present = [col for col, value in metrics.items() if value is not None]
        # 至少一半指标可用才给分。只有 eps 一个数字就打出 0–7 的分, 那个分没有意义。
        if len(present) * 2 >= len(QUALITY_METRIC_COLUMNS):
            q_score = sum(1 for col in present if metrics[col] > 0)  # type: ignore[operator]

    coverage, last_close = _price_window(kline, symbol, day)
    if coverage is None:
        reasons.append("no_kline_source")
    elif coverage <= 0.0:
        # 有行情来源, 但这只票在 asof 之前窗口内一个收盘价都没有: 停牌 / 退市 / 从未交易。
        reasons.append("no_kline_rows")
    elif coverage < MIN_PRICE_COVERAGE:
        reasons.append(f"kline_coverage_low:{coverage:.2f}")

    return QualityProxy(
        symbol=symbol,
        asof=day,
        q_score=q_score,
        metrics=metrics,
        metrics_asof_announce=announce,
        metrics_period_end=period_end,
        q_missing_flag=bool(reasons) or q_score is None,
        q_missing_reasons=tuple(reasons),
        price_coverage=coverage,
        last_close=last_close,
    )


def evaluate_quality_many(
    symbols: Iterable[str],
    asof: date | datetime,
    *,
    financials: pl.DataFrame | None,
    kline: pl.DataFrame | None,
) -> dict[str, QualityProxy]:
    """批量版本。"""
    return {
        sym: evaluate_quality(sym, asof, financials=financials, kline=kline) for sym in symbols
    }


# ---------------------------------------------------------------------------
# 读盘适配(只读, 不写)
# ---------------------------------------------------------------------------


def load_financials(data_dir: Path, table: str = "metrics") -> pl.DataFrame:
    """读本地财务 parquet, 口径与 `financial_sync.get_financial_df` 一致。

    直接复用那个函数而不是自己拼路径: 路径规则只有一份, 两份迟早会分叉。
    """
    from app.services.financial_sync import get_financial_df

    return get_financial_df(Path(data_dir), table)


def load_kline_daily(source: Path | Sequence[Path] | str) -> pl.DataFrame:
    """读日线 parquet(支持分区目录 / 文件 / glob), 返回 DataFrame。

    只读。生产路径下真实分区很大, 因此调用方应当尽量先按日期裁剪; 本函数不做裁剪,
    也不缓存 —— 骨架阶段先把"能读"和"读的是什么"说清楚, 性能留给真正接线的批次。
    """
    from app.parquet import scan_enriched_parquet

    lazy = scan_enriched_parquet(source)
    return lazy.collect()


__all__ = [
    "MIN_PRICE_COVERAGE",
    "PRICE_LOOKBACK_DAYS",
    "QUALITY_METRIC_COLUMNS",
    "QUALITY_MISSING_REASON_CODES",
    "QUALITY_MISSING_REASON_PREFIXES",
    "QUALITY_WEIGHT_STATUS",
    "QualityProxy",
    "evaluate_quality",
    "evaluate_quality_many",
    "financials_asof",
    "load_financials",
    "load_kline_daily",
]
