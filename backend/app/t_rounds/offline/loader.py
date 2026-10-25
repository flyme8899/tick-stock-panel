"""离线分钟加载 + 同尺判定 + 对齐（批次 20261025hfeat）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 这个模块存在的唯一理由

分钟导出里 `close` 与 `amount` **可能不是同一个复权口径**：
价格被前复权过、成交额仍是当时的原始额（或者反过来）。此时
`amount / (volume * 100)` 既不是原始均价，也不是复权均价，而是两者的商 ——
一个没有名字的数。

而它**看起来一切正常**：数值在合理区间、逐日连续、画出来也像均价。这就是
`MINUTE_CALIBER_CHECK` 说「近端日 ratio≈1 不能推断历史日可用」的意思：最近几天
刚抓的数据 ratio 恰好是 1.0，会让人以为整段数据都对。

## 判定方法：用一天的整段数据反问自己

对每个 `(symbol, trade_date)`：

```
ratio = sum(amount) / (sum(volume) * 100 * vwap_from_close)
```

其中 `vwap_from_close` 是用 `close` 与 `volume` 算的「按收盘价加权均价」。
如果价格与额同尺，那么按定义 `sum(amount) == sum(volume * 100 * 成交均价)`，
且成交均价必然落在当天 `low..high` 区间内 —— 所以同尺时 `ratio` 应当贴近 1。

- `|ratio - 1| <= MIXED_RATIO_TOLERANCE` → 该日视为**单尺**
- 否则 → 该日**混尺**，必须给对齐引子（`align_hint`）才能进特征

判定是**逐日**的，不是全局一个开关。取全局最小值/均值做判断题，会让"只有除权日
那几天混尺"的数据被整体判成干净。

## 对齐怎么做：把额乘到价的尺度上

混尺的物理含义只有一种：价格乘过 `f`、额没乘。所以对齐 = `amount * f`，`volume`
不动（手数不随复权变）。`f` 从 ratio 反推即可：`f ≈ 1 / ratio`。

但**本模块不自己算 `f`**。`f` 是复权因子，它只能来自因子表或调用方明确给出的引子：
自己用 `ratio` 反推等价于「用这根数据证明这根数据是对的」，是循环论证。

`align_hint` 的三种合法形态（写死）：

1. `{"factor": 0.9876}` —— 调用方从因子表算好的标量，全局同一因子
2. `{"factor_by_date": {"2025-07-25": 0.9876, ...}}` —— 逐日因子
3. `{"raw_basis": True}` —— 输入已是原始口径（`.raw_basis` 分区），不需要缩放

**没有第四种**：不允许"看起来差不多就按 1 处理"。那正是本模块要防的事。

## 缺日 / 缺票

缺文件 → `LoadedMinutes.skipped` 里记一条原因，**不填邻日数据**。
缺票同理。`null ≠ 0`：某标的当天没有 bar，就让它没有，不要造一根平盘 bar 出来。
"""
from __future__ import annotations

import gzip
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

import polars as pl

from .. import constants as C

# ---------------------------------------------------------------- 常量

MINUTE_COLUMNS: Final = ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount")
"""导出 CSV 的列。缺任意一列 → 该文件跳过（不猜列名、不做位置列映射）。"""

MIXED_RATIO_TOLERANCE: Final = 1e-3
"""单尺判定的相对容差。

为什么是 1e-3 而不是更紧：`amount` 与 `close*vwap` 的日合计本来就受四舍五入、
逐笔成交价与收盘价不等的影响，差个几个 bp 是正常的。真正混尺的差是**百分比级**
（除权一次就是 5%~50%），1e-3 足以把两者分开，同时不会把正常数据误判成混尺。
"""

CALIBER_SINGLE: Final = "single"
CALIBER_MIXED: Final = "mixed"

SHARES_PER_LOT: Final = 100
"""1 手 = 100 股。与 `amount_guard` 同口径（那里是常量，这里不 import 它的数值 ——
本包要能在它改口径时立刻炸出来，而不是悄悄跟着变）。"""


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True, slots=True)
class CaliberDay:
    """单个 `(symbol, trade_date)` 的同尺判定结果。"""

    symbol: str
    trade_date: date
    ratio: float
    """`sum(amount) / (sum(volume) * 100 * 收盘价加权均价)`。同尺时 ≈ 1。"""
    bars: int
    caliber: str
    """`CALIBER_SINGLE` / `CALIBER_MIXED`。"""

    @property
    def ok(self) -> bool:
        return self.caliber == CALIBER_SINGLE


@dataclass(frozen=True, slots=True)
class CaliberReport:
    """整批数据的同尺判定汇总。

    `days` 逐日保留，**不做全局短路** —— 见模块 docstring 与软提醒 2。
    """

    days: tuple[CaliberDay, ...]

    @property
    def mixed_days(self) -> tuple[CaliberDay, ...]:
        return tuple(d for d in self.days if d.caliber == CALIBER_MIXED)

    @property
    def all_single(self) -> bool:
        return not self.mixed_days

    def note(self) -> str:
        """写进报告 `caliber_note` 的一行摘要。"""
        if not self.days:
            return "无有效交易日: 未做同尺判定"
        if self.all_single:
            worst = max(self.days, key=lambda d: abs(d.ratio - 1.0))
            return (
                f"{len(self.days)} 个 (symbol, 交易日) 全部判为单尺; "
                f"最大偏离 {abs(worst.ratio - 1.0):.2e} "
                f"({worst.symbol} {worst.trade_date.isoformat()}, ratio={worst.ratio:.6f}); "
                "容差 1e-3"
            )
        mixed = self.mixed_days
        # 混尺分支必须能直接拿去排查, 所以逐日列出 symbol/日期/ratio, 而不是只说
        # "有 N 个混尺"。ratio 差多少决定了该怀疑复权、还是该怀疑成交量单位。
        detail = "; ".join(
            f"{d.symbol} {d.trade_date.isoformat()} ratio={d.ratio:.6f} ({d.bars} bars)"
            for d in mixed[:10]
        )
        more = f"; 另有 {len(mixed) - 10} 个未列出" if len(mixed) > 10 else ""
        return (
            f"{len(self.days)} 个 (symbol, 交易日) 中 {len(mixed)} 个判为混尺: "
            f"{detail}{more}; "
            "混尺日必须先给定对齐引子才可进特征(不允许按 ratio≈1 短路)"
        )


@dataclass(frozen=True, slots=True)
class LoadedMinutes:
    """加载结果。`frame` 永远已对齐（或已确认单尺），可直接进特征。"""

    frame: pl.DataFrame
    caliber: CaliberReport
    inputs: tuple[str, ...]
    """实际读到的输入文件路径字符串（写进报告 `inputs`）。"""
    skipped: tuple[str, ...] = ()
    """跳过的输入及原因。缺日/缺票在这里留痕，**不填邻日**。"""
    aligned: bool = False
    """是否执行过缩放。`True` 表示 `frame` 与磁盘上的原值不同。"""

    @property
    def dates(self) -> tuple[date, ...]:
        """实际参与计算的交易日清单（排序去重）。"""
        if self.frame.is_empty():
            return ()
        return tuple(sorted(set(self.frame["datetime"].dt.date().to_list())))


# ---------------------------------------------------------------- 判定


def _day_key_frame(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(pl.col("datetime").dt.date().alias("trade_date"))


def assess_caliber(frame: pl.DataFrame) -> CaliberReport:
    """逐 `(symbol, 交易日)` 判定单尺 / 混尺。

    输入需含 `symbol/datetime/close/volume/amount`。缺列 → 返回空报告（调用方
    应当先拒绝，而不是拿空报告当"全部单尺"）。
    """
    need = {"symbol", "datetime", "close", "volume", "amount"}
    if frame is None or frame.is_empty() or not need.issubset(frame.columns):
        return CaliberReport(days=())

    df = _day_key_frame(frame)
    agg = df.group_by(["symbol", "trade_date"]).agg(
        pl.col("amount").cast(pl.Float64).sum().alias("_amt"),
        pl.col("volume").cast(pl.Float64).sum().alias("_vol"),
        (pl.col("close").cast(pl.Float64) * pl.col("volume").cast(pl.Float64)).sum().alias("_pv"),
        pl.len().alias("_bars"),
    )

    days: list[CaliberDay] = []
    for row in agg.iter_rows(named=True):
        amt = float(row["_amt"] or 0.0)
        vol = float(row["_vol"] or 0.0)
        pv = float(row["_pv"] or 0.0)
        # 收盘价加权均价 = Σ(close*volume) / Σ(volume)；同尺时 amount 应等于
        # Σ(volume * 100 * 成交均价)，而成交均价落在 low..high 内，故 ratio ≈ 1。
        denom = vol * SHARES_PER_LOT * (pv / vol) if vol > 0 else 0.0
        if denom <= 0 or not math.isfinite(denom) or amt <= 0 or not math.isfinite(amt):
            # 数据不足以判定 → 判混尺（保守）。判单尺会把"不知道"伪装成"没问题"。
            ratio = float("nan")
            caliber = CALIBER_MIXED
        else:
            ratio = amt / denom
            caliber = (
                CALIBER_SINGLE
                if math.isfinite(ratio) and abs(ratio - 1.0) <= MIXED_RATIO_TOLERANCE
                else CALIBER_MIXED
            )
        days.append(
            CaliberDay(
                symbol=str(row["symbol"]),
                trade_date=row["trade_date"],
                ratio=ratio,
                bars=int(row["_bars"]),
                caliber=caliber,
            )
        )
    return CaliberReport(days=tuple(sorted(days, key=lambda d: (d.trade_date, d.symbol))))


# ---------------------------------------------------------------- 对齐


class MixedCaliberError(ValueError):
    """输入混尺且没有给出对齐引子。

    这不是"数据脏"，而是**调用方尚未声明口径**。猜一个因子然后继续，得到的
    ε 带位置是错的，而错误看不出来 —— 所以宁可停下来。
    """


def _factor_for(
    hint: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date,
) -> float | None:
    """从对齐引子里取该 `(symbol, 交易日)` 适用的因子。取不到 → None。"""
    if not hint:
        return None
    if hint.get("raw_basis") is True:
        return 1.0
    by_date = hint.get("factor_by_date")
    if isinstance(by_date, Mapping):
        raw = by_date.get(trade_date.isoformat())
        if raw is None:
            return None
        value = float(raw)
        return value if math.isfinite(value) and value > 0 else None
    if "factor" in hint:
        value = float(hint["factor"])
        return value if math.isfinite(value) and value > 0 else None
    return None


def align_minute_frame(
    frame: pl.DataFrame,
    *,
    align_hint: Mapping[str, Any] | None = None,
) -> tuple[pl.DataFrame, bool]:
    """把 `amount` 缩放对齐到 `close` 的口径。返回 `(对齐后的帧, 是否真的动过)`。

    **只缩放 `amount`，不动 `volume`**：手数不随复权变。
    **不动 OHLC**：任务书的前提是"价格已是特征要用的那个口径"，需要缩的是额。
    （若价格需要缩放，那是另一种混尺，属于 `price_basis.scale_minute_prices` 的
    职责，本模块不重复实现、也不假装能处理。）

    单尺输入 → 原样返回 `(frame, False)`。混尺且引子不全 → 抛
    `MixedCaliberError`，**绝不**按 `1/ratio` 自己反推因子（循环论证）。
    """
    report = assess_caliber(frame)
    if report.all_single:
        return frame, False

    if align_hint is None:
        raise MixedCaliberError(
            f"输入混尺({len(report.mixed_days)} 个 (symbol,交易日) 未对齐), "
            "且未给 align_hint; 见 loader 模块 docstring 的三种合法引子形态"
        )

    df = _day_key_frame(frame)
    parts: list[pl.DataFrame] = []
    for part in df.partition_by(["symbol", "trade_date"], maintain_order=True):
        symbol = str(part["symbol"][0])
        trade_date = part["trade_date"][0]
        factor = _factor_for(align_hint, symbol=symbol, trade_date=trade_date)
        if factor is None:
            raise MixedCaliberError(
                f"混尺日 {symbol} {trade_date.isoformat()} 取不到对齐因子; "
                "align_hint 必须覆盖全部混尺日(不得留空由本模块自行推测)"
            )
        if factor == 1.0:
            parts.append(part)
            continue
        parts.append(
            part.with_columns((pl.col("amount").cast(pl.Float64) * factor).alias("amount"))
        )
    return pl.concat(parts, how="vertical").drop("trade_date"), True


# ---------------------------------------------------------------- 读取


def _read_csv_gz(path: Path) -> pl.DataFrame:
    """读导出 CSV.GZ。列名按 `MINUTE_COLUMNS` 校验，缺列由调用方跳过。"""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return pl.read_csv(handle, try_parse_dates=True)


def _normalize(frame: pl.DataFrame) -> pl.DataFrame:
    """统一 `symbol` 为 Utf8、`datetime` 为 naive(北京墙钟) —— 与分钟存储契约一致。"""
    out = frame.with_columns(pl.col("symbol").cast(pl.Utf8))
    if out.schema["datetime"].time_zone is not None:
        out = out.with_columns(
            pl.col("datetime").dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
        )
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in out.columns:
            out = out.with_columns(pl.col(col).cast(pl.Float64))
    return out


def load_minute_export(
    paths: Iterable[Path | str],
    *,
    align_hint: Mapping[str, Any] | None = None,
    research_start: date | None = None,
    symbols: Iterable[str] | None = None,
) -> LoadedMinutes:
    """读一批导出文件 → 研究窗过滤 → 同尺判定 → 对齐。

    参数:
      paths: `kline_minute_YYYYMMDD.csv.gz` 的路径们。缺文件**跳过**并记原因。
      align_hint: 混尺时的对齐引子，见模块 docstring。
      research_start: 研究窗起点，默认 `constants.RESEARCH_WINDOW_START`(2025-07-25)。
      symbols: 只要这些标的；None = 全要。

    跳过的情形（都进 `skipped`，**绝不回退到最近日分区**）：
      - 文件不存在
      - 缺 `MINUTE_COLUMNS` 中的列
      - 读出来为空
      - 该文件所有 bar 都早于研究窗
    """
    start = research_start or C.RESEARCH_WINDOW_START
    wanted = {str(s) for s in symbols} if symbols is not None else None

    frames: list[pl.DataFrame] = []
    inputs: list[str] = []
    skipped: list[str] = []

    for raw in paths:
        path = Path(raw)
        if not path.exists():
            skipped.append(f"{path.name}: 文件不存在(不回退最近分区)")
            continue
        try:
            frame = _read_csv_gz(path)
        except Exception as exc:  # noqa: BLE001 - 坏文件不该让整批挂掉
            skipped.append(f"{path.name}: 读取失败 {type(exc).__name__}")
            continue
        missing = [c for c in MINUTE_COLUMNS if c not in frame.columns]
        if missing:
            skipped.append(f"{path.name}: 缺列 {missing}")
            continue
        if frame.is_empty():
            skipped.append(f"{path.name}: 空文件")
            continue

        frame = _normalize(frame)
        frame = frame.with_columns(pl.col("datetime").dt.date().alias("_d"))
        frame = frame.filter(pl.col("_d") >= start).drop("_d")
        if wanted is not None:
            frame = frame.filter(pl.col("symbol").is_in(sorted(wanted)))
        if frame.is_empty():
            skipped.append(f"{path.name}: 全部早于研究窗(<{start.isoformat()}), 无数据")
            continue

        frames.append(frame)
        inputs.append(str(path))

    if not frames:
        return LoadedMinutes(
            frame=pl.DataFrame(),
            caliber=CaliberReport(days=()),
            inputs=(),
            skipped=tuple(skipped),
        )

    merged = pl.concat(frames, how="vertical").sort(["symbol", "datetime"])
    # 同一 symbol+datetime 出现两次（两次导出被一起传进来）→ 保留第一条，并留痕。
    before = merged.height
    merged = merged.unique(subset=["symbol", "datetime"], keep="first", maintain_order=True)
    if merged.height != before:
        skipped.append(f"合并后去重: 丢弃 {before - merged.height} 行重复 (symbol, datetime)")

    caliber = assess_caliber(merged)
    aligned_frame, did_align = align_minute_frame(merged, align_hint=align_hint)

    return LoadedMinutes(
        frame=aligned_frame,
        caliber=caliber,
        inputs=tuple(inputs),
        skipped=tuple(skipped),
        aligned=did_align,
    )


def assert_no_market_open_impersonation(frame: pl.DataFrame) -> None:
    """断言帧里没有被改标成 09:25 的竞价 bar。

    `AUCTION_IMPERSONATION_FORBIDDEN` 是硬约束，但"没有这段代码"没法被测试证明，
    所以这里从**数据侧**再拦一次：出现早于 `MINUTE_EXPORT_EARLIEST` 的 bar，
    说明有人把 09:30 那根改标了，或者混进了别的来源。两种都不该进特征。
    """
    if frame is None or frame.is_empty() or "datetime" not in frame.columns:
        return
    # 用 "HH:MM" 字符串比较, 不走 hour()*60+minute(): dt.hour()/dt.minute() 返回 Int8,
    # 9*60=540 在 Int8 里回绕成 28, 会把 09:30 误判成"早于 09:30"(尾盘 14:57 更会溢出成负数)。
    # "HH:MM" 是定长零填充字符串, 字典序 == 时间序。
    too_early = frame.filter(
        pl.col("datetime").dt.strftime("%H:%M") < C.MINUTE_EXPORT_EARLIEST
    )
    if too_early.height:
        sample = too_early.select("symbol", "datetime").head(3).to_dicts()
        raise ValueError(
            f"发现 {too_early.height} 根早于 {C.MINUTE_EXPORT_EARLIEST} 的 bar "
            f"({sample}); 禁止用开盘首根 1 分钟 K 冒充 09:25 竞价"
        )


def shift_within_day(
    frame: pl.DataFrame, lag: int, *, column: str = "price"
) -> pl.Series | None:
    """取同一 `(symbol, 交易日)` 内向后 `lag` 根的 `column` 值（上一根）。

    **不跨日、不跨标的**：`shift` 在 `over(["symbol","date"])` 里做，所以当日首根的
    上一根是 `null`，不会串到前一天的最后一根。

    为什么不直接 `pl.col(...).shift(1).over(...)`：调用方（A 路径的比较）需要区分
    「当日首根」和「真的有上一根」，一个裸表达式容易在 `with_columns` 里被重排后
    悄悄串味。这里只做一件事，返回值可直接 `.alias()`。

    `lag <= 0` 或帧缺列时返回 `None`，让调用方显式降级而不是拿到错位的值。
    """
    if lag <= 0 or frame is None or frame.is_empty():
        return None
    if column not in frame.columns or not {"symbol", "date"}.issubset(frame.columns):
        return None
    # 必须 `over(["symbol", "date"])`: 裸 `shift` 会跨日、跨标的把上一行搬过来,
    # 那样次日元第一根就继承了前一日末根的值, 开盘跳空会被读成"价格在动"。
    return (
        frame.select(
            pl.col(column).shift(lag).over(["symbol", "date"]).alias(f"_prev_{column}_{lag}")
        )[f"_prev_{column}_{lag}"]
    )


__all__ = [
    "CALIBER_MIXED",
    "CALIBER_SINGLE",
    "MINUTE_COLUMNS",
    "MIXED_RATIO_TOLERANCE",
    "SHARES_PER_LOT",
    "CaliberDay",
    "CaliberReport",
    "LoadedMinutes",
    "MixedCaliberError",
    "align_minute_frame",
    "assert_no_market_open_impersonation",
    "assess_caliber",
    "load_minute_export",
    "shift_within_day",
]
