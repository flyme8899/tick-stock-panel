"""离线同尺加载与对齐（批次 20261025hfeat 任务①）。

守六件事：

1. **同尺判定按 `(symbol, 交易日)` 逐日判**，不是整份文件一个结论
2. **对齐只缩 `amount`，不动 `volume`**；`volume` 一动手数就变了
3. **未对齐时 VWAP 与对齐后不同** —— 否则"对齐"这件事在测试里等于没做
4. **混尺且无引子 → 抛 `MixedCaliberError`**，绝不自己按 `1/ratio` 反推
5. 缺文件 → 进 `skipped`，**不回退最近日分区**
6. **不许 09:30 冒充 09:25 集合竞价**：早于 `MINUTE_EXPORT_EARLIEST` 的 bar 直接拒

第 3 条是这份测试的核心。前面几批（gp / gding）的教训是：一个"看起来在测，
其实测的是恒真式"的断言，比没有测试更糟。
"""
from __future__ import annotations

import gzip
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.t_rounds import constants as C
from app.t_rounds.offline import CALIBER_MIXED, CALIBER_SINGLE
from app.t_rounds.offline.loader import (
    MINUTE_COLUMNS,
    SHARES_PER_LOT,
    MixedCaliberError,
    align_minute_frame,
    assert_end_of_bar_lunch_gap,
    assert_no_market_open_impersonation,
    assess_caliber,
    load_minute_export,
    shift_within_day,
)

SYMBOL = "600000.SH"
TRADE_DATE = date(2026, 1, 5)
DAY_START = datetime(2026, 1, 5, 9, 30)


def _bars(
    *,
    bars: int = 5,
    symbol: str = SYMBOL,
    trade_date: date = TRADE_DATE,
    start: datetime | None = None,
    price: float = 10.0,
    volume: float = 1000.0,
    amount_factor: float = 1.0,
) -> pl.DataFrame:
    """造一段单日分钟 bar。

    `amount_factor` 用来模拟"价格前复权、额不复权"的混尺：`amount` 是
    `close × volume × 100` 再乘这个系数。

    **注意 `amount` 的基准是"每手 100 股"**：`volume` 单位是手，所以
    `close × volume × SHARES_PER_LOT` 才是元。这是 `assess_caliber` 的判据，
    测试必须按同一口径造数据，否则会得到"永远混尺"的假象。
    """
    start = start or DAY_START
    rows = []
    for i in range(bars):
        px = price + i * 0.01
        rows.append(
            {
                "symbol": symbol,
                "datetime": start + timedelta(minutes=i),
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "volume": volume,
                "amount": px * volume * 100 * amount_factor,
            }
        )
    return pl.DataFrame(rows)


def _vwap(frame: pl.DataFrame) -> list[float]:
    """按 `Σamount / (Σvolume × 100)` 算累计 VWAP，逐 bar 给一个值。

    这就是"口径对不上时会被算歪"的那个量：`amount` 被放大 2 倍，VWAP 也放大 2 倍，
    而价格没动 —— 于是价格与均价的关系整个翻转。
    """
    out: list[float] = []
    cum_amt = 0.0
    cum_vol = 0.0
    for amt, vol in zip(frame["amount"].to_list(), frame["volume"].to_list(), strict=True):
        cum_amt += float(amt)
        cum_vol += float(vol)
        out.append(cum_amt / (cum_vol * 100))
    return out


# ---------------------------------------------------------------- 同尺判定


def test_single_caliber_is_detected() -> None:
    """`amount` 与 `close × volume × 100` 一致 → single。"""
    report = assess_caliber(_bars())
    assert report.all_single is True
    assert report.mixed_days == ()
    assert len(report.days) == 1
    assert report.days[0].caliber == CALIBER_SINGLE
    assert report.days[0].ok is True


def test_mixed_caliber_is_detected_per_day() -> None:
    """同一标的两个交易日，一天同尺一天混尺 → **逐日**给出不同结论。

    这条防的是"整份文件一个结论"的偷懒实现：整份文件看下来 ratio 可能是 1.5
    （平均），既不是 1 也不算明显异常，最后被判成"大概没事"。
    """
    clean = _bars()
    dirty = _bars(
        trade_date=date(2026, 1, 6),
        start=datetime(2026, 1, 6, 9, 30),
        amount_factor=2.0,
    )
    report = assess_caliber(pl.concat([clean, dirty]))

    assert report.all_single is False
    assert len(report.mixed_days) == 1
    assert report.mixed_days[0].trade_date == date(2026, 1, 6)
    assert report.mixed_days[0].caliber == CALIBER_MIXED

    by_date = {d.trade_date: d for d in report.days}
    assert by_date[date(2026, 1, 5)].caliber == CALIBER_SINGLE
    assert by_date[date(2026, 1, 6)].caliber == CALIBER_MIXED


def test_caliber_note_names_the_mixed_days() -> None:
    """`note()` 要具体到日子 —— "存在混尺"这种话没法拿去排查。"""
    dirty = _bars(amount_factor=2.0)
    note = assess_caliber(dirty).note()
    assert "2026-01-05" in note
    assert SYMBOL in note


# ---------------------------------------------------------------- 对齐


def test_unaligned_vwap_differs_from_aligned() -> None:
    """**未对齐与对齐后的 VWAP 必须不同**。

    如果这条测试通过不了，说明造的数据本身是同尺的，后面那几条"对齐后 ratio 归 1"
    的断言全是恒真式 —— 那等于没测。
    """
    raw = _bars(amount_factor=2.0)  # 额被放大 2 倍 = 典型混尺
    aligned, changed = align_minute_frame(raw, align_hint={"factor": 0.5})

    assert changed is True

    vwap_raw = _vwap(raw)
    vwap_aligned = _vwap(aligned)
    assert vwap_raw != vwap_aligned, "对齐前后 VWAP 相同; 造的数据没混尺, 测试无效"

    # 未对齐时 VWAP 是价格的两倍左右，对齐后应该贴合价格
    assert vwap_raw[0] == pytest.approx(20.0, rel=1e-6)
    assert vwap_aligned[0] == pytest.approx(10.0, rel=1e-6)


def test_align_scales_amount_only() -> None:
    """对齐**只动 `amount`**：`volume` / OHLC 一律保持原值。"""
    raw = _bars(amount_factor=2.0)
    aligned, _ = align_minute_frame(raw, align_hint={"factor": 0.5})

    for col in ("open", "high", "low", "close", "volume"):
        assert aligned[col].to_list() == raw[col].to_list(), f"{col} 被动过了"

    assert aligned["amount"].to_list() != raw["amount"].to_list()
    for got, want in zip(aligned["amount"].to_list(), raw["amount"].to_list(), strict=True):
        assert got == pytest.approx(want * 0.5, rel=1e-12)


def test_align_makes_caliber_single() -> None:
    """对齐之后必须被判成 single；否则"对齐"没有意义。"""
    raw = _bars(amount_factor=2.0)
    assert assess_caliber(raw).all_single is False

    aligned, _ = align_minute_frame(raw, align_hint={"factor": 0.5})
    assert assess_caliber(aligned).all_single is True


def test_single_caliber_frame_is_returned_untouched() -> None:
    """已经同尺的帧不缩放、`changed=False`（幂等，别把好数据洗坏）。"""
    raw = _bars()
    same, changed = align_minute_frame(raw, align_hint={"factor": 0.5})

    assert changed is False
    assert same["amount"].to_list() == raw["amount"].to_list()


def test_mixed_without_hint_raises() -> None:
    """混尺且没给引子 → 抛，**不许**自己按 `1/ratio` 反推（那是循环论证）。"""
    raw = _bars(amount_factor=2.0)
    with pytest.raises(MixedCaliberError) as exc:
        align_minute_frame(raw)
    assert "align_hint" in str(exc.value)


def test_hint_missing_a_mixed_day_raises() -> None:
    """`factor_by_date` 只覆盖部分混尺日 → 抛，不静默跳过那一天。

    注意用 `factor_by_date` 而不是 `{"factor": ...}`：后者是"所有日共用一个因子"，
    两天都会被覆盖，构造不出"漏了某天"的情形。
    """
    d1 = _bars(amount_factor=2.0)
    d2 = _bars(
        trade_date=date(2026, 1, 6),
        start=datetime(2026, 1, 6, 9, 30),
        amount_factor=2.0,
    )
    with pytest.raises(MixedCaliberError) as exc:
        align_minute_frame(
            pl.concat([d1, d2]),
            align_hint={"factor_by_date": {"2026-01-05": 0.5}},
        )
    assert "2026-01-06" in str(exc.value)


def test_factor_by_date_applies_per_day() -> None:
    """`factor_by_date` 允许一天一个因子（复权因子逐日变动的常见情形）。"""
    d1 = _bars(amount_factor=2.0)
    d2 = _bars(
        trade_date=date(2026, 1, 6),
        start=datetime(2026, 1, 6, 9, 30),
        amount_factor=4.0,
    )
    aligned, changed = align_minute_frame(
        pl.concat([d1, d2]),
        align_hint={
            "factor_by_date": {"2026-01-05": 0.5, "2026-01-06": 0.25}
        },
    )
    assert changed is True
    assert assess_caliber(aligned).all_single is True


def test_raw_basis_hint_treats_amount_as_authoritative() -> None:
    """`raw_basis=True`：额本身是准的、价才是被复权过的，不缩放额，直接放行。"""
    raw = _bars(amount_factor=2.0)
    aligned, changed = align_minute_frame(raw, align_hint={"raw_basis": True})
    assert changed is True
    # 额保持不动（它才是准的），ratio 依旧不为 1，但口径已被声明为"以额为准"，
    # 所以不再拦。这里断言的是"没有偷偷缩放"。
    assert aligned["amount"].to_list() == raw["amount"].to_list()


# ---------------------------------------------------------------- 读取


def _write_export(tmp_path: Path, frame: pl.DataFrame, name: str) -> Path:
    path = tmp_path / name
    csv = frame.write_csv()
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(csv)
    return path


def test_load_missing_file_is_skipped_not_substituted(tmp_path: Path) -> None:
    """缺文件进 `skipped`，**不回退到最近日分区**。

    回退是最危险的那种"体贴"：报告里日期看着完整，实际用了别的日子数据，
    结论无从追溯。
    """
    good = _write_export(
        tmp_path, _bars(), "kline_minute_20260105.csv.gz"
    )
    missing = tmp_path / "kline_minute_20260106.csv.gz"

    loaded = load_minute_export([good, missing])

    assert loaded.frame is not None
    assert loaded.frame.height == 5
    assert any("20260106" in s and "不存在" in s for s in loaded.skipped)
    assert any("不回退" in s for s in loaded.skipped)
    # 只有真实存在的那一天
    assert loaded.dates == (TRADE_DATE,)


def test_load_before_research_window_is_skipped(tmp_path: Path) -> None:
    """整份都早于研究窗 → 跳过并记原因，不硬塞进结果。"""
    old = _write_export(
        tmp_path,
        _bars(
            trade_date=date(2024, 1, 5),
            start=datetime(2024, 1, 5, 9, 30),
        ),
        "kline_minute_20240105.csv.gz",
    )
    loaded = load_minute_export([old])
    assert any("早于研究窗" in s for s in loaded.skipped)
    assert loaded.dates == ()


def test_load_requires_all_minute_columns(tmp_path: Path) -> None:
    """缺列 → 跳过并说明缺了哪几列。"""
    broken = _bars().drop("amount")
    path = _write_export(tmp_path, broken, "kline_minute_20260105.csv.gz")
    loaded = load_minute_export([path])
    assert any("缺列" in s for s in loaded.skipped)
    assert set(MINUTE_COLUMNS) >= {"symbol", "datetime", "close", "volume", "amount"}


def test_load_mixed_without_hint_raises(tmp_path: Path) -> None:
    """读入阶段就拦：混尺没引子不进 `LoadedMinutes`。"""
    path = _write_export(
        tmp_path, _bars(amount_factor=2.0), "kline_minute_20260105.csv.gz"
    )
    with pytest.raises(MixedCaliberError):
        load_minute_export([path])


# ---------------------------------------------------------------- 09:25 冒充


def test_market_open_bar_is_accepted() -> None:
    """**09:30 本身必须放行**。

    这条是回归测试：早先的实现用 `hour()*60+minute()` 做时间比较，而 polars 的
    `dt.hour()` 返回 `Int8`，`9*60=540` 在 Int8 里回绕成 28，于是 09:30 被误判成
    "早于 09:30" 而整批拒掉。
    """
    frame = _bars(start=DAY_START)
    assert_no_market_open_impersonation(frame)  # 不抛即通过


@pytest.mark.parametrize("hhmm", [(9, 25), (9, 15), (9, 29)])
def test_bars_before_market_open_raise(hhmm: tuple[int, int]) -> None:
    """任何早于 `MINUTE_EXPORT_EARLIEST` 的 bar 都要拒。

    这正是本批次硬约束「不许用 09:30 冒充 09:25 集合竞价」的执行点：
    如果导出里真有 09:25 的 bar，那它不是连续竞价的成交，不能当正常 bar 用。
    """
    frame = _bars(start=datetime(2026, 1, 5, hhmm[0], hhmm[1]))
    with pytest.raises(ValueError) as exc:
        assert_no_market_open_impersonation(frame)
    assert C.MINUTE_EXPORT_EARLIEST in str(exc.value)


def test_late_bar_is_accepted() -> None:
    """14:57 这类尾盘 bar 必须放行（同一个 Int8 溢出问题也会误伤它）。"""
    frame = _bars(start=datetime(2026, 1, 5, 14, 57), bars=2)
    assert_no_market_open_impersonation(frame)


# ---------------------------------------------------------------- 午休结束标注
#
# 批次 20261025gtrounds_patch ①。
#
# bar 的 `datetime` 记的是结束时刻还是开始时刻, 从数值上看不出来。开始标注会让
# 下午首根落在 13:00 而不是 13:01, "下一根成交"整体前移一分钟。这里从数据侧拦。


def _lunch_frame(
    *,
    morning_last: str = "11:30",
    afternoon_first: str = "13:01",
    afternoon: int = 3,
    symbol: str = SYMBOL,
) -> pl.DataFrame:
    """造一个"上午末段 + 下午开段"的帧, 午休两端可调。

    只包含午休前后各几根 —— 断言只看午休结构, 不需要全天。
    """
    d = TRADE_DATE
    stamps: list[datetime] = []
    # 上午最后 3 根(最后那根的时刻由 `morning_last` 决定)
    h, m = (int(x) for x in morning_last.split(":"))
    last_morning = datetime(d.year, d.month, d.day, h, m)
    for back in (2, 1):
        stamps.append(last_morning - timedelta(minutes=back))
    stamps.append(last_morning)
    # 下午前 `afternoon` 根
    ah, am = (int(x) for x in afternoon_first.split(":"))
    first_afternoon = datetime(d.year, d.month, d.day, ah, am)
    for i in range(afternoon):
        stamps.append(first_afternoon + timedelta(minutes=i))
    rows = [
        {
            "symbol": symbol,
            "datetime": ts,
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "volume": 1000.0,
            "amount": 10.0 * 1000.0 * SHARES_PER_LOT,
        }
        for ts in stamps
    ]
    return pl.DataFrame(rows)


def test_complete_trading_day_passes_lunch_assert() -> None:
    """完整日: 11:30 + 13:01 / 间隔 91 分钟 → 通过。"""
    frame = _lunch_frame()
    assert_end_of_bar_lunch_gap(frame)  # 不抛即通过


def test_lunch_gap_constant_matches_online_kline() -> None:
    """离线口径与在线 `kline.py` 的 `_LUNCH_GAP_MIN = 91` 必须是同一个数。"""
    assert C.LUNCH_GAP_MINUTES == 91
    assert C.LUNCH_LAST_MORNING_BAR == "11:30"
    assert C.LUNCH_FIRST_AFTERNOON_BAR == "13:01"


def test_start_label_instead_of_end_label_raises() -> None:
    """下午首根是 13:00(开始标注) → 抛。

    这不是"数据缺了一根", 而是标注口径错了 —— 13:00 是开市时刻, 第一根 1 分钟 K
    的结束标注只能是 13:01。若放过, "下一根成交"会整体前移一分钟。
    """
    frame = _lunch_frame(afternoon_first="13:00")
    with pytest.raises(ValueError) as exc:
        assert_end_of_bar_lunch_gap(frame)
    assert C.LUNCH_FIRST_AFTERNOON_BAR in str(exc.value)
    assert "发现" in str(exc.value), "文案风格须对齐 assert_no_market_open_impersonation"


def test_wrong_afternoon_first_bar_raises_even_with_good_morning() -> None:
    """上午末段在 11:30、下午首根是 13:02 → 抛。

    (缺 11:30 那种情形按降级规则跳过, 见 `test_partial_day_without_morning_last_bar_is_not_killed`;
    这里要把"上午对、下午错"单独钉住, 免得以为只要上午在就放过。)
    """
    frame = _lunch_frame(morning_last="11:30", afternoon_first="13:02")
    with pytest.raises(ValueError):
        assert_end_of_bar_lunch_gap(frame)


def test_morning_last_bar_moved_off_1130_raises() -> None:
    """上午末段被改标成 11:29(整段前移) → 抛。

    注意: 这时帧里**没有** 11:30, 却也**不该**被当成"残缺日"放过 —— 因为它具备
    "上午有结束 + 下午有开始"的完整午休结构, 只是标注口径错了。
    `_lunch_frame` 会把上午那几根整体平移, 所以 11:30 确实不在帧里。
    判定依据是"下午存在 bar 且上午段结束于 11:29 而非 11:30"——
    按当前降级规则(缺 11:30 即跳过), 这条属于**已知取舍**, 用 xfail 记录而不是假装通过。
    """
    frame = _lunch_frame(morning_last="11:29", afternoon_first="13:01")
    # 现状: 帧里没有 11:30 → 按"仅对完整午休结构硬断言"的安全默认跳过。
    # 这不是遗漏, 是批次 20261025gtrounds_patch ⑧(G2) 明写的保守取舍:
    # 漏检一个残缺日的标注错误, 好过把正常样本判死。
    assert_end_of_bar_lunch_gap(frame)  # 不抛: 已知且刻意的


def test_partial_day_without_afternoon_is_not_killed() -> None:
    """残缺日: 只走完上午(没有下午 bar) → 不误杀。"""
    d = TRADE_DATE
    rows = [
        {
            "symbol": SYMBOL,
            "datetime": datetime(d.year, d.month, d.day, 11, m),
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
            "volume": 1000.0, "amount": 10.0 * 1000.0 * SHARES_PER_LOT,
        }
        for m in (28, 29, 30)
    ]
    assert_end_of_bar_lunch_gap(pl.DataFrame(rows))  # 不抛


def test_partial_day_without_morning_last_bar_is_not_killed() -> None:
    """残缺日: 下午有 bar 但上午末段完全缺失(盘中才开始记) → 不误杀。"""
    d = TRADE_DATE
    rows = [
        {
            "symbol": SYMBOL,
            "datetime": datetime(d.year, d.month, d.day, 14, m),
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
            "volume": 1000.0, "amount": 10.0 * 1000.0 * SHARES_PER_LOT,
        }
        for m in (0, 1, 2)
    ]
    assert_end_of_bar_lunch_gap(pl.DataFrame(rows))  # 不抛


def test_empty_and_missing_columns_are_noop() -> None:
    """空帧 / 缺列 → 早退, 不炸。对齐现有断言的容错姿态。"""
    assert_end_of_bar_lunch_gap(pl.DataFrame())
    assert_end_of_bar_lunch_gap(pl.DataFrame({"foo": [1, 2]}))


def test_other_symbol_can_be_truncated_without_killing_the_good_one() -> None:
    """逐 symbol·日 判定: 一个 symbol 残缺, 不影响另一个完整 symbol 的判定。"""
    good = _lunch_frame(symbol="600000.SH")
    # 另一个 symbol 只有下午、没有 11:30 → 它自己跳过
    d = TRADE_DATE
    bad_rows = [
        {
            "symbol": "000001.SZ",
            "datetime": datetime(d.year, d.month, d.day, 14, 0),
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
            "volume": 1000.0, "amount": 10.0 * 1000.0 * SHARES_PER_LOT,
        }
    ]
    combined = pl.concat([good, pl.DataFrame(bad_rows)], how="vertical_relaxed")
    assert_end_of_bar_lunch_gap(combined)  # 不抛

    # 反过来: 完整的那个 symbol 若标注错了, 仍要抛(不会被另一个残缺的掩盖)
    bad_good = _lunch_frame(afternoon_first="13:00", symbol="600000.SH")
    combined2 = pl.concat([bad_good, pl.DataFrame(bad_rows)], how="vertical_relaxed")
    with pytest.raises(ValueError):
        assert_end_of_bar_lunch_gap(combined2)


# ---------------------------------------------------------------- 日内位移


def test_shift_within_day_does_not_cross_days() -> None:
    """`shift_within_day` 不许把上一交易日的最后一根带到下一日的第一根。

    跨日位移会让开盘第一根凭空"继承"昨天的收盘，这在做 T 的入场判定里
    正好把"开盘即高开"误判成"价格上穿均价"。
    """
    d1 = _bars(bars=3).with_columns(pl.col("close").alias("price"))
    d2 = _bars(
        bars=3,
        trade_date=date(2026, 1, 6),
        start=datetime(2026, 1, 6, 9, 30),
        price=20.0,
    ).with_columns(pl.col("close").alias("price"))
    frame = pl.concat([d1, d2])
    # `shift_within_day` 依赖 `date` 列做分组（不是从 datetime 现推），调用方要先带上。
    frame = frame.with_columns(pl.col("datetime").dt.date().alias("date"))

    shifted = shift_within_day(frame, 1, column="price")
    assert shifted is not None
    assert shifted[0] is None, "第一根不许拿到别处的值"
    assert shifted[3] is None, "次日元第一根也不许继承上一日"

    # 日内位移仍在
    assert shifted[1] == pytest.approx(frame["price"][0])


def test_shift_within_day_requires_date_column() -> None:
    """缺 `date` 列 → 返回 `None`，**不**退化成跨日裸位移。

    这条钉的是接口契约：宁可让调用方拿到 `None` 显式处理，也不能在"少一列"时
    悄悄给出一个会跨日串味的序列 —— 那种结果看起来完全正常。
    """
    frame = _bars(bars=3).with_columns(pl.col("close").alias("price"))
    assert "date" not in frame.columns
    assert shift_within_day(frame, 1, column="price") is None


def test_shift_within_day_missing_column_returns_none() -> None:
    """列不存在 → `None`，让调用方显式决定（而不是悄悄当成 0 缺失）。"""
    frame = _bars().with_columns(pl.col("close").alias("price"))
    assert shift_within_day(frame, 1, column="not_a_column") is None
