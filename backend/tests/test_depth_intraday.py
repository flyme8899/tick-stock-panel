from datetime import datetime

import polars as pl

from app.services import depth_intraday as di


class FakeDepth:
    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, 0

    def _call_depth_batch(self, symbols):
        self.calls += 1
        if self.exc:
            raise self.exc
        return {s: self.data[s] for s in symbols if s in self.data}


SAMPLE = {
    "600000.SH": {"timestamp": 1792121371000, "bid_prices": [10.0, 9.99, 9.98, 9.97, 9.96],
                  "bid_volumes": [100, 200, 300, 400, 500],
                  "ask_prices": [10.01, 10.02, 10.03, 10.04, 10.05],
                  "ask_volumes": [11, 22, 33, 44, 55]},
    "000001.SZ": {"timestamp": 1792121372000, "bid_prices": [5.0], "bid_volumes": [9],
                  "ask_prices": [], "ask_volumes": []},
}


def _mk(tmp_path, depth, now=datetime(2026, 10, 16, 10, 0, 0), symbols=("600000.SH", "000001.SZ")):
    return di.DepthIntradayCollector(
        tmp_path, depth, symbols_fn=lambda: list(symbols),
        last_price_fn=lambda s: {"600000.SH": 10.0}, trading_day_fn=lambda n: True,
        now_fn=lambda: now)


def test_window():
    assert di.in_collect_window(datetime(2026, 10, 16, 9, 30))
    assert di.in_collect_window(datetime(2026, 10, 16, 14, 57))
    assert not di.in_collect_window(datetime(2026, 10, 16, 9, 25))
    assert not di.in_collect_window(datetime(2026, 10, 16, 12, 0))
    assert not di.in_collect_window(datetime(2026, 10, 16, 14, 58))
    assert not di.in_collect_window(datetime(2026, 10, 17, 10, 0))  # 周六


def test_row_flatten_missing_levels():
    row = di.depth_to_row("000001.SZ", SAMPLE["000001.SZ"], 1.0, None)
    assert row["bid1_px"] == 5.0 and row["bid1_vol"] == 9
    assert row["bid2_px"] is None and row["ask1_px"] is None and row["ask5_vol"] is None


def test_poll_and_flush_new_path_only(tmp_path):
    c = _mk(tmp_path, FakeDepth(SAMPLE))
    c.tick()
    assert c.status["rows_today"] == 2
    out = c.flush()
    assert out.parent == tmp_path / "depth5_intraday" / "date=2026-10-16"
    assert not (tmp_path / "depth5").exists()
    df = pl.read_parquet(out)
    assert df.height == 2 and set(di.SCHEMA) == set(df.columns)
    r = df.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert r["ask5_px"] == 10.05 and r["last_price"] == 10.0 and r["source_ts_ms"] == 1792121371000


def test_failure_is_soft_and_backs_off(tmp_path):
    c = _mk(tmp_path, FakeDepth(exc=RuntimeError("429 too many")))
    w1 = c.tick()
    w2 = c.tick()
    assert c.status["consecutive_failures"] == 2 and "429" in c.status["last_error"]
    assert w2 > w1 > c.interval
    assert c.tick() <= di.MAX_BACKOFF_S
    assert c.flush() is None


def test_outside_window_no_fetch(tmp_path):
    fd = FakeDepth(SAMPLE)
    c = _mk(tmp_path, fd, now=datetime(2026, 10, 16, 12, 0))
    c.tick()
    assert fd.calls == 0


def test_holiday_no_fetch(tmp_path):
    fd = FakeDepth(SAMPLE)
    c = _mk(tmp_path, fd)
    c.trading_day_fn = lambda n: False
    c.tick()
    assert fd.calls == 0


def test_env_flag_and_min_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_DEPTH_INTRADAY_ENABLED", "0")
    monkeypatch.setenv("TSP_DEPTH_INTRADAY_INTERVAL_S", "0.5")
    c = _mk(tmp_path, FakeDepth(SAMPLE))
    assert c.enabled is False and c.interval == di.MIN_INTERVAL_S
    c.start()
    assert c._thread is None


def test_unchanged_source_ts_deduped(tmp_path):
    c = _mk(tmp_path, FakeDepth(SAMPLE))
    c.tick()
    c.tick()
    assert c.status["rows_today"] == 2 and c.status["dup_skipped_today"] == 2
