"""申万一级行业 PIT 只读查询: history × l1_map, 按 valid_from / valid_to 取当时归属。

## 为什么要 PIT, 而不是"当前行业"

用当前行业给三年前的市场信号打标签, 是量化里最常见也最隐蔽的偏差之一。某只票
2021 年是电子、2023 年划到计算机, 用今天的分类去回看 2021 年的信号, 等于提前
知道了它会被重新归类 —— 这类信息在回测里表现为"说不清为什么的收益抬升"。

所以这里只认两件事:

- **`history`**: 每个 symbol 在什么时间段属于哪个(申万内部码)行业 —— `valid_from` / `valid_to`
- **`l1_map`**: 内部码 → 一级行业代码 / 名称 / 口径版本(SW2014 / SW2021) —— 同样带 valid 窗

按 `asof` 落在哪个窗口取哪个, 落在空档就返回 `None` 并说明原因, **不填假行业**。

## 明确拒绝同花顺当前态

`ext_hy_ths` / `industry_ths` 是同花顺的**当前快照**, 没有 valid_from/to。拿它当
申万 PIT 是"silent 复用", 而且从结果上完全看不出问题 —— 只有把 2021 年的信号
逐条对着当年的分类翻一遍才会发现。

本模块的 `load_sector_tables` 一旦发现来源里带 `ths` 字样就**直接抛错**, 不做
"降级到 ths" 这种 fallback。`tests/test_fundamental_screens.py::test_m2_missing_industry_file_is_an_error`
是先例: 缺行业就报错, 别猜。

## 列名适配

history 与 l1_map 的实际列名以落地数据为准。`HISTORY_COLUMNS` / `L1_MAP_COLUMNS`
把"业务概念 → 可接受的列名"列出来, 适配时不用改查询逻辑。**任何一项都找不到就
抛错并列出实际列名** —— 静默用 None 顶替会让整批信号悄悄失去行业维度。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

import polars as pl

from .constants import INDUSTRY_STANDARD_SW_L1
from .models import SignalValidationError, asof_date

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_NAME: Final = "sw_industry_history"
DEFAULT_L1_MAP_NAME: Final = "sw_industry_l1_map"

HISTORY_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "symbol": ("symbol", "code", "stock_code", "secu_code"),
    "industry_code": ("industry_code", "ind_code", "sw_code", "industry"),
    "valid_from": ("valid_from", "start_date", "effective_from", "since"),
    "valid_to": ("valid_to", "end_date", "effective_to", "until"),
}
"""history 表的业务概念 → 可接受列名(按优先级)。"""

L1_MAP_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "industry_code": ("industry_code", "ind_code", "sw_code", "industry"),
    "l1_code": ("l1_code", "industry_code_sw_l1", "l1"),
    "l1_name": ("l1_name", "industry_name", "name"),
    "valid_from": ("valid_from", "start_date", "effective_from"),
    "valid_to": ("valid_to", "end_date", "effective_to"),
    "edition": ("mapping_edition", "edition", "version"),
}
"""l1_map 表的业务概念 → 可接受列名。"""

THS_MARKERS: Final = ("ths", "同花顺")
"""来源名里出现这些字样即拒绝。"""


class IndustrySourceRejected(ValueError):
    """行业来源不可用于 PIT 查询(典型: 同花顺当前态)。"""


class IndustrySchemaError(ValueError):
    """表结构不符合预期。"""


@dataclass(frozen=True, slots=True)
class IndustryPit:
    """一次 PIT 查询的结果。

    没查到映射时 `industry_code_sw_l1` 为 `None`, `reason` 说明为什么 —— 调用方
    拿到 None 时能区分"这段历史确实没有行业记录"和"表里根本没有这个 symbol"。
    """

    symbol: str
    asof: date
    industry_code_sw_l1: str | None = None
    industry_name: str | None = None
    mapping_edition: str | None = None
    industry_pit_asof: date | None = None
    source: str = "sw_pit"
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.industry_code_sw_l1 is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat(),
            "industry_code_sw_l1": self.industry_code_sw_l1,
            "industry_name": self.industry_name,
            "mapping_edition": self.mapping_edition,
            "industry_pit_asof": None if self.industry_pit_asof is None else self.industry_pit_asof.isoformat(),
            "industry_standard": INDUSTRY_STANDARD_SW_L1,
            "source": self.source,
            "resolved": self.resolved,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# 加载与适配
# ---------------------------------------------------------------------------


def _is_ths_source(path: Path) -> bool:
    lowered = path.name.lower()
    return any(marker in lowered for marker in THS_MARKERS)


def _read_table(path: Path) -> pl.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"行业表不存在: {path}")
    name = path.name.lower()
    if name.endswith((".csv", ".csv.gz")):
        # polars 1.4x 的 read_csv 不再接受 compression 参数, 压缩格式完全由后缀推断;
        # 两种后缀走同一条路径即可。
        return pl.read_csv(path, infer_schema_length=0, try_parse_dates=True)
    if name.endswith(".parquet"):
        return pl.read_parquet(path)
    raise IndustrySchemaError(f"不支持的行业表格式: {path.name}")


def _resolve_columns(
    frame: pl.DataFrame,
    spec: Mapping[str, tuple[str, ...]],
    *,
    table: str,
    required: Iterable[str],
) -> dict[str, str]:
    """把业务概念映射到实际列名。`required` 里任何一项找不到就抛错。"""
    actual = set(frame.columns)
    mapping: dict[str, str] = {}
    for concept, candidates in spec.items():
        for candidate in candidates:
            if candidate in actual:
                mapping[concept] = candidate
                break
    missing = [c for c in required if c not in mapping]
    if missing:
        raise IndustrySchemaError(
            f"{table} 缺少必要列 {missing}; 实际列: {sorted(actual)}; "
            f"可接受列名见 sw_pit.{('HISTORY_COLUMNS' if table == 'history' else 'L1_MAP_COLUMNS')}"
        )
    return mapping


def _normalize_dates(frame: pl.DataFrame, columns: Iterable[str]) -> pl.DataFrame:
    exprs = []
    for col in columns:
        if col not in frame.columns:
            continue
        dtype = frame.schema[col]
        if dtype == pl.Date:
            continue
        if dtype in (pl.Utf8, pl.Categorical):
            exprs.append(pl.col(col).str.strptime(pl.Date, "%Y-%m-%d", strict=False).alias(col))
        else:
            exprs.append(pl.col(col).cast(pl.Date, strict=False).alias(col))
    return frame.with_columns(exprs) if exprs else frame


@dataclass(frozen=True, slots=True)
class SectorTables:
    """已适配好列名的两张表, 可直接查询。"""

    history: pl.DataFrame
    l1_map: pl.DataFrame
    history_columns: dict[str, str] = field(default_factory=dict, compare=False)
    l1_map_columns: dict[str, str] = field(default_factory=dict, compare=False)
    source_paths: tuple[str, ...] = ()

    def editions(self) -> list[str]:
        if "edition" not in self.l1_map_columns:
            return []
        return sorted({str(v) for v in self.l1_map["_edition"].drop_nulls().to_list()})


def load_sector_tables(history_path: Path, l1_map_path: Path) -> SectorTables:
    """读取并适配 history 与 l1_map。

    两道拒绝:

    - 文件名或来源带 `ths` 字样 → `IndustrySourceRejected`(同花顺当前态不能当 PIT)
    - 缺必要列 → `IndustrySchemaError`

    适配后统一输出内部列名: history 用 `symbol` / `_ind` / `_from` / `_to`,
    l1_map 用 `_ind` / `_l1_code` / `_l1_name` / `_from` / `_to` / `_edition`。
    """
    history_path = Path(history_path)
    l1_map_path = Path(l1_map_path)
    for path, label in ((history_path, "history"), (l1_map_path, "l1_map")):
        if _is_ths_source(path):
            raise IndustrySourceRejected(
                f"{label} 来源 {path.name!r} 看起来是同花顺当前态, 不能用于申万 PIT; "
                "同花顺快照没有 valid_from/valid_to, 拿它当历史归属等于用未来分类污染过去信号"
            )

    raw_history = _read_table(history_path)
    raw_map = _read_table(l1_map_path)

    h_cols = _resolve_columns(
        raw_history, HISTORY_COLUMNS, table="history", required=("symbol", "industry_code", "valid_from")
    )
    m_cols = _resolve_columns(
        raw_map, L1_MAP_COLUMNS, table="l1_map", required=("industry_code", "l1_code", "valid_from")
    )

    history = _normalize_dates(raw_history, [h_cols.get("valid_from", ""), h_cols.get("valid_to", "")])
    l1_map = _normalize_dates(raw_map, [m_cols["valid_from"], m_cols.get("valid_to", "")])

    select_h: list[pl.Expr] = [
        pl.col(h_cols["symbol"]).cast(pl.Utf8).str.strip_chars().alias("symbol"),
        pl.col(h_cols["industry_code"]).cast(pl.Utf8).str.strip_chars().alias("_ind"),
        pl.col(h_cols["valid_from"]).alias("_from"),
    ]
    select_h.append(
        pl.col(h_cols["valid_to"]).alias("_to") if "valid_to" in h_cols else pl.lit(None, dtype=pl.Date).alias("_to")
    )
    history = history.select(select_h)

    select_m: list[pl.Expr] = [
        pl.col(m_cols["industry_code"]).cast(pl.Utf8).str.strip_chars().alias("_ind"),
        pl.col(m_cols["l1_code"]).cast(pl.Utf8).str.strip_chars().alias("_l1_code"),
        (
            pl.col(m_cols["l1_name"]).cast(pl.Utf8).str.strip_chars().alias("_l1_name")
            if "l1_name" in m_cols
            else pl.lit(None, dtype=pl.Utf8).alias("_l1_name")
        ),
        pl.col(m_cols["valid_from"]).alias("_from"),
        (
            pl.col(m_cols["valid_to"]).alias("_to")
            if "valid_to" in m_cols
            else pl.lit(None, dtype=pl.Date).alias("_to")
        ),
        (
            pl.col(m_cols["edition"]).cast(pl.Utf8).alias("_edition")
            if "edition" in m_cols
            else pl.lit(None, dtype=pl.Utf8).alias("_edition")
        ),
    ]
    l1_map = l1_map.select(select_m)

    return SectorTables(
        history=history,
        l1_map=l1_map,
        history_columns=h_cols,
        l1_map_columns=m_cols,
        source_paths=(str(history_path), str(l1_map_path)),
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def _in_window(frame: pl.DataFrame, asof: date) -> pl.DataFrame:
    """筛出 valid_from <= asof 且 (valid_to 为空 或 asof < valid_to) 的行。

    `valid_to` 取**开区间**: 某段归属写 [2021-01-01, 2023-01-01) 时, 2023-01-01 归
    下一段。这样相邻两段首尾相接不会出现"同一天两条都命中"。
    """
    return frame.filter(
        (pl.col("_from") <= asof)
        & (pl.col("_to").is_null() | (pl.col("_to") > asof))
    )


def industry_asof(
    tables: SectorTables,
    symbol: str,
    asof: date | datetime,
) -> IndustryPit:
    """查某只票在 `asof` 当天的申万一级行业归属。

    三级回退顺序: symbol+ind+窗 → ind+窗 → 未命中返回 None。

    注意这里**不做行业代码前缀套用**: 申万内部码跨版本会变含义, 按前两位切 L1 只在
    同一个版本内成立。`l1_map` 本身就是"内部码 → L1"的映射表, 所以必须走它。
    """
    day = asof_date(asof)
    if not isinstance(symbol, str) or not symbol.strip():
        raise SignalValidationError("symbol 不能为空")
    symbol = symbol.strip()

    hist = tables.history.filter(pl.col("symbol") == symbol)
    if hist.is_empty():
        return IndustryPit(
            symbol=symbol,
            asof=day,
            reason=f"history 里没有 {symbol} 的任何行业记录",
        )

    hit = _in_window(hist, day)
    if hit.is_empty():
        spans = hist.select(pl.col("_from").min().alias("lo"), pl.col("_to").max().alias("hi")).row(0)
        return IndustryPit(
            symbol=symbol,
            asof=day,
            reason=f"{day.isoformat()} 不落在任何有效窗内(表内覆盖 {spans[0]} ~ {spans[1]})",
        )

    # 同一 symbol 同一天命中多行: 取 valid_from 最新的一条(后生效的覆盖先生效的)。
    row = hit.sort("_from").tail(1).row(0, named=True)
    ind = row["_ind"]
    mapped = _in_window(tables.l1_map.filter(pl.col("_ind") == ind), day)
    if mapped.is_empty():
        return IndustryPit(
            symbol=symbol,
            asof=day,
            reason=f"内部行业码 {ind!r} 在 {day.isoformat()} 没有可用的 l1_map 映射",
        )
    l1 = mapped.sort("_from").tail(1).row(0, named=True)
    return IndustryPit(
        symbol=symbol,
        asof=day,
        industry_code_sw_l1=l1["_l1_code"],
        industry_name=l1["_l1_name"],
        mapping_edition=l1["_edition"],
        industry_pit_asof=row["_from"],
    )


def industry_asof_many(
    tables: SectorTables,
    symbols: Iterable[str],
    asof: date | datetime,
) -> dict[str, IndustryPit]:
    """批量查询。逐条走 `industry_asof`, 保证与单查结果一致。"""
    return {sym: industry_asof(tables, sym, asof) for sym in symbols}


__all__ = [
    "DEFAULT_HISTORY_NAME",
    "DEFAULT_L1_MAP_NAME",
    "HISTORY_COLUMNS",
    "IndustryPit",
    "IndustrySchemaError",
    "IndustrySourceRejected",
    "L1_MAP_COLUMNS",
    "SectorTables",
    "industry_asof",
    "industry_asof_many",
    "load_sector_tables",
]
