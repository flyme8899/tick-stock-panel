"""全市场竞价扫描：阈值、幂等、非交易日与缺文件 no_data。不访问真实网络。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auction_scan import router
from app.market_time import CN_TZ
from app.plugins.fuyao.client import FuyaoError
from app.services import auction_scan as scan

# 2026-10-13 是周二。近 5 日均量 24000 手 → 每分钟 100 手 = 10000 股。
_DAY = datetime(2026, 10, 13, 9, 25, 30, tzinfo=CN_TZ)
_PRIOR = ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10")


class _Client:
    def __init__(self, rows: list[dict], server_ts: int = 1_760_318_730_000, error: Exception | None = None):
        self.rows = rows
        self.server_ts = server_ts
        self.error = error
        self.calls = 0

    def snapshot_all(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows), self.server_ts

    def close(self):
        return None


def _row(symbol: str, prev: float, open_: float, volume: float, **over) -> dict:
    row = {
        "thscode": symbol,
        "ticker": symbol.split(".")[0],
        "last_price": open_,
        "open_price": open_,
        "prev_price": prev,
        "volume": volume,
        "turnover": open_ * volume,
    }
    row.update(over)
    return row


def _write_volume(data_dir: Path, day: str, volume: float = 24_000.0) -> None:
    part = data_dir / "kline_daily" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    symbols = ["600519.SH", "000001.SZ", "000002.SZ", "000003.SZ", "300001.SZ"]
    pl.DataFrame({"symbol": symbols, "volume": [volume] * len(symbols)}).write_parquet(part / "part.parquet")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    for day in _PRIOR:
        _write_volume(tmp_path, day)
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("AUCTION_SCAN_ENABLED", raising=False)
    monkeypatch.delenv("AUCTION_SCAN_MIN_GAP", raising=False)
    monkeypatch.delenv("AUCTION_SCAN_MIN_VOL_RATIO", raising=False)


def _sample_rows() -> list[dict]:
    return [
        _row("600519.SH", 10, 10.5, 100_000),     # gap 5%, ratio 10 → 命中
        _row("000001.SZ", 10, 10.4, 200_000),     # gap 4%, ratio 20 → 只进宽候选
        _row("000002.SZ", 10, 11.0, 90_000),      # gap 10%, ratio 9 → 只进宽候选
        _row("000003.SZ", 10, 10.2, 500_000),     # gap 2% → 两边都不要
        _row("300001.SZ", 20, 22.0, 150_000),     # gap 10%, ratio 15 → 命中
        {"last_price": 1.0},                      # 无代码丢弃
    ]


def _names() -> dict[str, str]:
    return {
        "600519.SH": "贵州茅台",
        "000001.SZ": "平安银行",
        "000002.SZ": "万科A",
        "300001.SZ": "特锐德",
    }


def test_threshold_filter_uses_five_day_average_over_240(data_dir: Path):
    client = _Client(_sample_rows())
    result = scan.run_scan(
        data_dir, now=_DAY, trading=True, client=client, name_map=_names(),
    )
    assert result["status"] == "ok"
    assert result["volume_basis"] == scan.VOLUME_BASIS_AVG5
    assert result["scanned_count"] == 6
    assert client.calls == 1

    payload = json.loads((data_dir / "auction_scan" / "2026-10-13.json").read_text(encoding="utf-8"))
    assert [row["symbol"] for row in payload["items"]] == ["300001.SZ", "600519.SH"]
    maotai = payload["items"][1]
    assert maotai["name"] == "贵州茅台"
    assert maotai["gap"] == pytest.approx(0.05)
    assert maotai["vol_ratio"] == pytest.approx(10.0)
    assert maotai["auction_volume"] == pytest.approx(1000.0)  # 100000 股 = 1000 手
    assert maotai["auction_amount"] == pytest.approx(10.5 * 100_000)
    assert payload["server_ts"] == client.server_ts
    assert payload["scanned_at"] == "2026-10-13T09:25:30+08:00"
    assert [row["symbol"] for row in payload["candidates"]] == [
        "300001.SZ", "000002.SZ", "600519.SH", "000001.SZ",
    ]
    assert "000003.SZ" not in {row["symbol"] for row in payload["candidates"]}


def test_env_thresholds_shrink_hits_but_keep_wide_list(data_dir: Path, monkeypatch):
    monkeypatch.setenv("AUCTION_SCAN_MIN_GAP", "0.08")
    monkeypatch.setenv("AUCTION_SCAN_MIN_VOL_RATIO", "12")
    client = _Client(_sample_rows())
    scan.run_scan(data_dir, now=_DAY, trading=True, client=client, name_map=_names())
    payload = json.loads((data_dir / "auction_scan" / "2026-10-13.json").read_text(encoding="utf-8"))
    assert [row["symbol"] for row in payload["items"]] == ["300001.SZ"]
    assert payload["min_gap"] == pytest.approx(0.08)
    assert payload["min_vol_ratio"] == pytest.approx(12)
    assert {row["symbol"] for row in payload["candidates"]} == {
        "300001.SZ", "000002.SZ", "600519.SH", "000001.SZ",
    }


def test_snapshot_volume_ratio_field_wins_over_average(data_dir: Path):
    rows = [_row("600519.SH", 10, 11, 100_000, volume_ratio=3.5)]
    client = _Client(rows)
    result = scan.run_scan(data_dir, now=_DAY, trading=True, client=client, name_map=_names())
    assert result["volume_basis"] == scan.VOLUME_BASIS_RATIO
    payload = json.loads((data_dir / "auction_scan" / "2026-10-13.json").read_text(encoding="utf-8"))
    assert payload["items"] == []
    assert payload["candidates"][0]["vol_ratio"] == pytest.approx(3.5)


def test_snapshot_auction_volume_field_is_the_numerator(data_dir: Path):
    rows = [_row("600519.SH", 10, 10.6, 1_000, auction_volume=200_000)]
    client = _Client(rows)
    result = scan.run_scan(data_dir, now=_DAY, trading=True, client=client, name_map=_names())
    assert result["volume_basis"] == scan.VOLUME_BASIS_AUCTION_VOL
    payload = json.loads((data_dir / "auction_scan" / "2026-10-13.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["symbol"] == "600519.SH"
    assert payload["items"][0]["vol_ratio"] == pytest.approx(20.0)
    assert payload["items"][0]["auction_volume"] == pytest.approx(2_000.0)


def test_idempotent_rerun_skips_when_file_exists(data_dir: Path):
    client = _Client(_sample_rows())
    first = scan.run_scan(data_dir, now=_DAY, trading=True, client=client, name_map=_names())
    assert first["status"] == "ok"
    path = data_dir / "auction_scan" / "2026-10-13.json"
    body = path.read_text(encoding="utf-8")

    later = _DAY.replace(minute=26, second=30)
    second = scan.run_scan(data_dir, now=later, trading=True, client=client, name_map=_names())
    assert second == {"status": "skipped", "reason": "exists", "trade_date": "2026-10-13"}
    assert client.calls == 1
    assert path.read_text(encoding="utf-8") == body


def test_failed_fetch_does_not_block_retry(data_dir: Path):
    broken = _Client([], error=FuyaoError("超时"))
    failed = scan.run_scan(data_dir, now=_DAY, trading=True, client=broken, name_map=_names())
    assert failed["status"] == "failed"
    assert not (data_dir / "auction_scan" / "2026-10-13.json").exists()

    client = _Client(_sample_rows())
    retried = scan.run_scan(
        data_dir,
        now=_DAY.replace(minute=26, second=30),
        trading=True,
        client=client,
        name_map=_names(),
    )
    assert retried["status"] == "ok"
    third = scan.run_scan(
        data_dir,
        now=_DAY.replace(minute=27, second=30),
        trading=True,
        client=client,
        name_map=_names(),
    )
    assert third["reason"] == "exists"
    assert client.calls == 1


def test_skips_weekend_without_calling_snapshot(data_dir: Path):
    saturday = datetime(2026, 10, 17, 9, 25, 30, tzinfo=CN_TZ)
    client = _Client(_sample_rows())
    result = scan.run_scan(data_dir, now=saturday, client=client, name_map=_names())
    assert result["reason"] == "not_trading_day"
    assert client.calls == 0
    assert not (data_dir / "auction_scan").exists()


def test_skips_holiday_and_after_hours(data_dir: Path, monkeypatch):
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda _now: False)
    client = _Client(_sample_rows())
    holiday = scan.run_scan(data_dir, now=_DAY, client=client, name_map=_names())
    assert holiday["reason"] == "not_trading_day"
    assert client.calls == 0

    afternoon = scan.run_scan(
        data_dir,
        now=datetime(2026, 10, 13, 15, 5, tzinfo=CN_TZ),
        trading=True,
        client=client,
        name_map=_names(),
    )
    assert afternoon["reason"] == "outside_window"
    assert client.calls == 0
    assert not (data_dir / "auction_scan").exists()


def test_disabled_switch_skips(data_dir: Path, monkeypatch):
    monkeypatch.setenv("AUCTION_SCAN_ENABLED", "false")
    client = _Client(_sample_rows())
    result = scan.run_scan(data_dir, now=_DAY, trading=True, client=client)
    assert result["reason"] == "disabled"
    assert client.calls == 0


@pytest.mark.parametrize(
    ("clock", "open_"),
    [
        (datetime(2026, 10, 13, 9, 25, 29, tzinfo=CN_TZ), False),
        (datetime(2026, 10, 13, 9, 25, 30, tzinfo=CN_TZ), True),
        (datetime(2026, 10, 13, 9, 27, 59, tzinfo=CN_TZ), True),
        (datetime(2026, 10, 13, 9, 28, 0, tzinfo=CN_TZ), False),
    ],
)
def test_scan_window(clock: datetime, open_: bool):
    assert scan.in_scan_window(clock) is open_


def test_schedule_is_primary_plus_two_retries():
    specs = scan.schedule_specs()
    assert [(spec["hour"], spec["minute"], spec["second"]) for spec in specs] == [
        (9, 25, 30), (9, 26, 30), (9, 27, 30),
    ]


def test_api_missing_day_is_no_data_and_existing_day_returns_items(data_dir: Path):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    client = TestClient(app)

    missing = client.get("/api/auction-scan", params={"date": "2026-10-13"})
    assert missing.status_code == 200
    body = missing.json()
    assert body["state"] == "no_data"
    assert body["trade_date"] == "2026-10-13"
    assert body["items"] == []
    assert body["dates"] == []
    assert "candidates" not in body

    scan.run_scan(data_dir, now=_DAY, trading=True, client=_Client(_sample_rows()), name_map=_names())
    found = client.get("/api/auction-scan", params={"date": "2026-10-13"})
    assert found.status_code == 200
    payload = found.json()
    assert payload["state"] == "ok"
    assert [row["symbol"] for row in payload["items"]] == ["300001.SZ", "600519.SH"]
    assert payload["dates"] == ["2026-10-13"]
    assert "candidates" not in payload

    other = client.get("/api/auction-scan", params={"date": "2026-10-12"})
    assert other.status_code == 200
    assert other.json()["state"] == "no_data"
    assert other.json()["dates"] == ["2026-10-13"]

    bad = client.get("/api/auction-scan", params={"date": "13/10/2026"})
    assert bad.status_code == 400
