"""除权日复权价与不复权分钟价不得混用。

夹具: 前复权 close=10, 不复权 raw_close=20 (因子 0.5)。分钟成交在 20,
真实 VWAP = amount / (volume×100) = 20。与复权收盘相除会得到 -50% 的假收益。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.matrix import build_market_data_matrix, matrix_feature
from app.services.price_basis import (
    lookup_minute_price_factors,
    minute_prev_close,
    session_prev_on_raw_scale,
)
from app.strategy.scoring import materialize_scoring_columns

EX_DAY = date(2024, 6, 4)
PREV_DAY = date(2024, 6, 3)


def _ex_rights_daily() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "date": [PREV_DAY, EX_DAY],
        # 昨日因子 0.5; 当日因子跳到 1, 相当于最新日除权, 昨收应回到前复权 8。
        "close": [8.0, 10.0],
        "raw_close": [16.0, 10.0],
        "open": [8.0, 10.0],
        "high": [8.0, 10.0],
        "low": [8.0, 10.0],
        "volume": [100.0, 100.0],
        "amount": [16.0 * 100 * 100, 10.0 * 100 * 100],
    })


def test_session_prev_uses_adjusted_close_when_ex_rights_factor_is_one():
    """最新日因子为 1 的除权日, 原始尺度昨收等于前复权昨收, 不是昨日原始收盘。"""
    prev = session_prev_on_raw_scale(8.0, 16.0, 10.0, 10.0)
    assert prev == pytest.approx(8.0)


def test_session_prev_maps_historical_adjusted_close_onto_raw():
    """历史日因子 0.5 且当日未再跳变时, 昨收落在原始价 (前复权昨收 / 0.5)。"""
    prev = session_prev_on_raw_scale(8.0, 16.0, 5.0, 10.0)
    assert prev == pytest.approx(16.0)
    mapped = minute_prev_close(8.0, day_close=5.0, day_raw=10.0, reads_adjusted=False)
    assert mapped == pytest.approx(16.0)
    assert minute_prev_close(8.0, day_close=5.0, day_raw=10.0, reads_adjusted=True) == pytest.approx(8.0)


def test_vwap_bias_uses_raw_close_on_ex_rights_day():
    panel = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [EX_DAY],
        "open": [10.0],
        "high": [10.0],
        "low": [10.0],
        "close": [10.0],
        "raw_close": [20.0],
        "volume": [100.0],
        "amount": [20.0 * 100.0 * 100.0],
    })
    frame = materialize_scoring_columns(panel, {"vwap_bias"})
    assert frame["vwap_bias"][0] == pytest.approx(0.0)

    market = build_market_data_matrix(panel, field_columns={"amount", "raw_close"})
    bias = matrix_feature(market, "vwap_bias")
    assert float(bias[0, 0]) == pytest.approx(0.0)

    # 没有 raw_close 时仍用 close, 与旧面板口径一致 (这里会得到假的 -50%)。
    bare = panel.drop("raw_close")
    fallback = materialize_scoring_columns(bare, {"vwap_bias"})
    assert fallback["vwap_bias"][0] == pytest.approx(10.0 / 20.0 - 1.0)


class _Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _MinuteRepo:
    def __init__(self, minute: pl.DataFrame, daily: pl.DataFrame, data_dir):
        self._minute = minute
        self._daily = daily
        self.store = _Store(data_dir)

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        return self._minute

    def get_daily_batch(self, symbols, start, end, columns=None):
        frame = self._daily
        if columns:
            existing = [column for column in columns if column in frame.columns]
            frame = frame.select(existing)
        return frame


def _raw_minute() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ"],
        "datetime": [datetime(2024, 6, 4, 10, 0)],
        "open": [20.0],
        "high": [20.0],
        "low": [20.0],
        "close": [20.0],
        "volume": [100.0],
        "amount": [20.0 * 100.0 * 100.0],
    })


def test_minute_fill_scales_raw_bars_onto_adjusted_prices(tmp_path):
    daily = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [EX_DAY],
        "close": [10.0],
        "raw_close": [20.0],
    })
    repo = _MinuteRepo(_raw_minute(), daily, tmp_path)
    loaded = BacktestEngine._load_minute_for_fills(
        repo, ["000001.SZ"], {"2024-06-04"}, "stock",
    )
    arr = loaded[("000001.SZ", "2024-06-04")]
    assert arr[0, 3] == pytest.approx(10.0)
    assert arr[0, 4] == pytest.approx(100.0)
    price = BacktestEngine._resolve_minute_fill(arr, None, "buy")
    assert price == pytest.approx(10.0)


def test_minute_fill_does_not_scale_when_reads_are_already_adjusted(tmp_path):
    from app.services.minute_adjust import mark_minute_basis_raw

    mark_minute_basis_raw(tmp_path)
    daily = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [EX_DAY],
        "close": [10.0],
        "raw_close": [20.0],
    })
    repo = _MinuteRepo(_raw_minute(), daily, tmp_path)
    assert lookup_minute_price_factors(repo, ["000001.SZ"], [EX_DAY], "stock") == {}
    loaded = BacktestEngine._load_minute_for_fills(
        repo, ["000001.SZ"], {"2024-06-04"}, "stock",
    )
    arr = loaded[("000001.SZ", "2024-06-04")]
    assert arr[0, 3] == pytest.approx(20.0)
    price = BacktestEngine._resolve_minute_fill(arr, None, "buy")
    assert price == pytest.approx(20.0)


def test_explicit_price_factors_scale_without_daily_lookup():
    loaded = BacktestEngine._load_minute_for_fills(
        _MinuteRepo(_raw_minute(), pl.DataFrame(), None),
        ["000001.SZ"],
        {"2024-06-04"},
        "stock",
        price_factors={("000001.SZ", "2024-06-04"): 0.5},
    )
    arr = loaded[("000001.SZ", "2024-06-04")]
    assert arr[0, 0] == pytest.approx(10.0)
    assert arr[0, 5] == pytest.approx(20.0 * 100.0 * 100.0 * 0.5)


def test_market_confirm_scales_raw_minutes_to_adjusted_daily(tmp_path, monkeypatch):
    from app.config import settings
    from app.market_time import CN_TZ
    from app.news.market_confirm import load_market_tape

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    minute = tmp_path / "kline_minute" / "date=2026-10-09"
    minute.mkdir(parents=True)
    (tmp_path / "kline_daily_enriched" / "date=2026-10-09").mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["601318.SH"],
        "datetime": [datetime(2026, 10, 9, 10, 0)],
        "open": [20.0],
        "high": [20.0],
        "low": [20.0],
        "close": [20.0],
        "volume": [100.0],
    }).write_parquet(minute / "part.parquet")
    pl.DataFrame({
        "symbol": ["601318.SH"],
        "date": [date(2026, 10, 9)],
        "open": [10.0],
        "high": [10.0],
        "low": [10.0],
        "close": [10.0],
        "raw_close": [20.0],
        "volume": [100.0],
        "prev_close": [10.0],
        "name": ["中国平安"],
    }).write_parquet(tmp_path / "kline_daily_enriched" / "date=2026-10-09" / "part.parquet")
    event = {
        "name": "回购",
        "direction": "利好",
        "concepts": [],
        "mentioned_stocks": [{"key": "601318.SH", "name": "中国平安", "mentions": 1}],
        "_first": datetime(2026, 10, 9, 9, 5, tzinfo=CN_TZ),
    }
    tape = load_market_tape(datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ), [event])
    bars = tape["stocks"]["601318.SH"]["bars"]
    minute_bars = [bar for bar in bars if bar.get("kind") == "minute"]
    assert minute_bars
    assert minute_bars[-1]["close"] == pytest.approx(10.0)
    assert "_px_factor" not in tape["stocks"]["601318.SH"]


def test_previous_close_helper_on_ex_rights_panel():
    from app.api.kline import _get_previous_closes

    class _Repo:
        store = None

        def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
            frame = _ex_rights_daily()
            if columns:
                frame = frame.select([column for column in columns if column in frame.columns])
            return frame

    closes = _get_previous_closes(_Repo(), "000001.SZ", [EX_DAY], "stock")
    assert closes[EX_DAY] == pytest.approx(8.0)


def test_trade_mark_prev_close_follows_minute_basis():
    from app.services.trade_marks import _previous_raw_close

    panel = _ex_rights_daily()
    raw_basis = _previous_raw_close(panel, "000001.SZ", "2024-06-04", reads_adjusted=False)
    adjusted_basis = _previous_raw_close(panel, "000001.SZ", "2024-06-04", reads_adjusted=True)
    assert raw_basis == pytest.approx(8.0)
    assert adjusted_basis == pytest.approx(8.0)

    historical = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "date": [PREV_DAY, EX_DAY],
        "close": [8.0, 5.0],
        "raw_close": [16.0, 10.0],
    })
    assert _previous_raw_close(
        historical, "000001.SZ", "2024-06-04", reads_adjusted=False,
    ) == pytest.approx(16.0)
    assert _previous_raw_close(
        historical, "000001.SZ", "2024-06-04", reads_adjusted=True,
    ) == pytest.approx(8.0)


def test_minute_replay_does_not_double_adjust(tmp_path):
    from app.backtest.minute_replay import MinuteSignalReplayer
    from app.services.minute_adjust import mark_minute_basis_raw

    class _Strategy:
        minute_daily_bars = 1

        def __init__(self) -> None:
            self.meta = {"id": "probe"}

    class _Result:
        def __init__(self, close):
            self.rows = [{
                "symbol": "000001.SZ",
                "close": close,
                "last_datetime": datetime(2024, 6, 4, 2, 0),
            }]
            self.scores = {}

    class _StrategyEngine:
        def __init__(self, close):
            self._close = close

        def run(self, *args, **kwargs):
            return _Result(self._close)

    class _Repo:
        def __init__(self, data_dir):
            self.store = _Store(data_dir)

        def list_minute_dates(self, start, end, asset_type="stock"):
            return [EX_DAY]

        def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
            return pl.DataFrame({
                "symbol": ["000001.SZ"],
                "datetime": [datetime(2024, 6, 4, 2, 0)],
                "close": [10.0],
            })

    panel = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "date": [PREV_DAY, EX_DAY],
        "close": [8.0, 10.0],
        "raw_close": [16.0, 20.0],
        "name": ["银行", "银行"],
    })

    class _Backtest:
        def __init__(self, data_dir):
            self.repo = _Repo(data_dir)

    raw_hit = MinuteSignalReplayer(_Backtest(tmp_path), _StrategyEngine(16.0)).replay(
        _Strategy(),
        panel=panel,
        start=PREV_DAY,
        end=EX_DAY,
        params={},
        overrides={},
        symbols=["000001.SZ"],
    )
    assert raw_hit.hits[0].entry_price == pytest.approx(8.0)

    adjusted_dir = tmp_path / "adjusted"
    mark_minute_basis_raw(adjusted_dir)
    adjusted_hit = MinuteSignalReplayer(
        _Backtest(adjusted_dir), _StrategyEngine(8.0),
    ).replay(
        _Strategy(),
        panel=panel,
        start=PREV_DAY,
        end=EX_DAY,
        params={},
        overrides={},
        symbols=["000001.SZ"],
    )
    assert adjusted_hit.hits[0].entry_price == pytest.approx(8.0)
    assert adjusted_hit.buy_limit_up == 0
