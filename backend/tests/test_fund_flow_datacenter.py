"""东财数据中心资金流回补，以及同花顺不得覆盖东财。不出网。"""

from __future__ import annotations

from datetime import date

import pytest

from app.fund_flow import jobs, store
from app.fund_flow.backfill import backfill_dates, main, resolve_dates
from app.fund_flow.circuit import CircuitBreaker
from app.fund_flow.datacenter import SOURCE, fetch_market_day, map_datacenter_row
from app.fund_flow.sources import SourceClients


def _response(payload: dict):
    return type("Response", (), {"json": lambda self: payload})()


def _page(rows: list[dict], *, pages: int = 1) -> dict:
    return {"success": True, "result": {"pages": pages, "data": rows}}


def _raw(
    code: str,
    suffix: str,
    name: str,
    net: float,
    big: float,
    super_net: float,
    *,
    day: str = "2026-10-13",
) -> dict:
    return {
        "SECURITY_CODE": code,
        "SECUCODE": f"{code}.{suffix}",
        "SECURITY_NAME_ABBR": name,
        "TRADE_DATE": f"{day} 00:00:00",
        "NET_INFLOW": net,
        "BIGDEAL_NET": big,
        "SUPERDEAL_NET": super_net,
    }


def test_map_datacenter_row_converts_wan_and_keeps_suffix() -> None:
    assert SOURCE in store.EASTMONEY_STOCK_SOURCES
    row = map_datacenter_row(
        _raw("600519", "SH", "贵州茅台", 5040.1248, 3200.5, 1839.6248),
        trade_date="2026-10-13",
    )
    assert row is not None
    assert row["symbol"] == "600519.SH"
    assert row["code"] == "600519"
    assert row["name"] == "贵州茅台"
    assert row["trade_date"] == "2026-10-13"
    assert row["main_net"] == pytest.approx(50401248)
    assert row["large_net"] == pytest.approx(32005000)
    assert row["super_net"] == pytest.approx(18396248)
    assert row["source"] == "eastmoney_datacenter"
    assert "main_net_5d" not in row
    assert (
        map_datacenter_row(
            {**_raw("000001", "SZ", "平安银行", 1, 1, 1), "TRADE_DATE": "2026-10-12 00:00:00"},
            trade_date="2026-10-13",
        )
        is None
    )
    assert (
        map_datacenter_row(
            {"SECURITY_CODE": "000001", "SECUCODE": "000001.SZ", "TRADE_DATE": "2026-10-13"},
            trade_date="2026-10-13",
        )
        is None
    )


def test_fetch_market_day_paginates_whole_market() -> None:
    seen: list[dict] = []

    def fake(url, *, params=None, headers=None, timeout=None):
        params = params or {}
        seen.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        page = int(params["pageNumber"])
        assert params["reportName"] == "RPT_DMSK_TS_FUNDFLOW"
        assert params["pageSize"] == "2"
        assert params["filter"] == "(TRADE_DATE='2026-10-13')"
        assert params["sortColumns"] == "SECURITY_CODE"
        assert "SECUCODE" in params["columns"]
        assert "NET_INFLOW" in params["columns"]
        assert "datacenter-web.eastmoney.com" in url
        if page == 1:
            return _response(
                _page(
                    [
                        _raw("600519", "SH", "贵州茅台", 5040.1248, 1, 2),
                        _raw("000001", "SZ", "平安银行", -6882.2298, -1, -2),
                    ],
                    pages=2,
                )
            )
        return _response(
            _page(
                [
                    _raw("920378", "BJ", "北交所样本", 39.1608, 10.5, 28.6608),
                ],
                pages=2,
            )
        )

    rows = fetch_market_day("2026-10-13", get=fake, page_size=2)
    assert [item["params"]["pageNumber"] for item in seen] == ["1", "2"]
    assert seen[0]["timeout"] == (10.0, 30.0)
    by_code = {row["code"]: row for row in rows}
    assert by_code["600519"]["main_net"] == pytest.approx(50401248)
    assert by_code["600519"]["symbol"] == "600519.SH"
    assert by_code["000001"]["main_net"] == pytest.approx(-68822298)
    assert by_code["000001"]["symbol"] == "000001.SZ"
    assert by_code["920378"]["main_net"] == pytest.approx(391608)
    assert by_code["920378"]["symbol"] == "920378.BJ"
    assert by_code["920378"]["source"] == SOURCE


def test_empty_report_and_truncation_and_bad_date() -> None:
    def empty(url, *, params=None, headers=None, timeout=None):
        return _response({"success": False, "code": 9201, "message": "返回数据为空"})

    assert fetch_market_day("2026-10-01", get=empty) == []

    def truncated(url, *, params=None, headers=None, timeout=None):
        return _response(_page([_raw("600519", "SH", "贵州茅台", 1, 1, 1)], pages=3))

    with pytest.raises(RuntimeError, match="超过"):
        fetch_market_day("2026-10-13", get=truncated, page_size=1, page_limit=1)

    def boom(*_args, **_kwargs):
        raise AssertionError("不应出网")

    with pytest.raises(ValueError):
        fetch_market_day("20261013", get=boom)


def test_backfill_overwrites_ths_and_keeps_days_that_fail(tmp_path) -> None:
    store.write_rows(
        tmp_path,
        "stock",
        "2026-10-13",
        [
            {
                "symbol": "600036.SH",
                "code": "600036",
                "name": "招商银行",
                "trade_date": "2026-10-13",
                "main_net": 270_000_000,
                "source": "akshare_ths",
            },
            {
                "symbol": "000858.SZ",
                "code": "000858",
                "name": "五粮液",
                "trade_date": "2026-10-13",
                "main_net": 12.0,
                "large_net": 3.0,
                "super_net": 4.0,
                "source": "efinance",
            },
        ],
    )
    store.write_rows(
        tmp_path,
        "stock",
        "2026-10-14",
        [
            {
                "symbol": "600519.SH",
                "code": "600519",
                "trade_date": "2026-10-14",
                "main_net": 1.0,
                "source": "efinance",
            }
        ],
    )

    def fake(url, *, params=None, headers=None, timeout=None):
        params = params or {}
        if "2026-10-14" in params["filter"]:
            raise RuntimeError("datacenter down")
        return _response(
            _page(
                [
                    _raw("600036", "SH", "招商银行", -56462.6, -100.0, -50.0),
                    _raw("600519", "SH", "贵州茅台", 5040.1248, 10.0, 20.0),
                ]
            )
        )

    result = backfill_dates(
        tmp_path,
        ["2026-10-13", "2026-10-14"],
        get=fake,
        sleeper=lambda _seconds: None,
        concurrency=2,
        spacing_s=0,
        page_size=50,
    )
    assert result.errors and result.errors[0].startswith("2026-10-14")
    assert "2026-10-13" in result.wrote
    assert result.skipped == []
    day = store.read_partition(tmp_path, "stock", "2026-10-13")
    by_code = {row["code"]: row for row in day.to_dicts()}
    assert by_code["600036"]["source"] == SOURCE
    assert by_code["600036"]["symbol"] == "600036.SH"
    assert by_code["600036"]["main_net"] == pytest.approx(-564626000)
    assert by_code["600519"]["source"] == SOURCE
    assert by_code["600519"]["main_net"] == pytest.approx(50401248)
    # 这一天接口没返回的东财行留在分区里。
    assert by_code["000858"]["source"] == "efinance"
    assert by_code["000858"]["main_net"] == pytest.approx(12.0)
    failed = store.read_partition(tmp_path, "stock", "2026-10-14")
    assert failed.height == 1
    assert failed["main_net"][0] == pytest.approx(1.0)
    assert failed["source"][0] == "efinance"
    path = store.partition_file(tmp_path, "stock", "2026-10-13")
    assert path == tmp_path / "fund_flow" / "stock" / "date=2026-10-13" / "part.parquet"
    assert path.exists()


@pytest.mark.parametrize("source", sorted(store.EASTMONEY_STOCK_SOURCES))
def test_ths_does_not_overwrite_eastmoney_sources(tmp_path, source: str) -> None:
    store.write_rows(
        tmp_path,
        "stock",
        "2026-10-13",
        [
            {
                "symbol": "600519.SH",
                "code": "600519",
                "trade_date": "2026-10-13",
                "main_net": 50401248.0,
                "large_net": 1.0,
                "super_net": 2.0,
                "source": source,
            }
        ],
    )
    store.write_rows(
        tmp_path,
        "stock",
        "2026-10-13",
        [
            {
                "symbol": "600519",
                "code": "600519",
                "trade_date": "2026-10-13",
                "main_net": -167_000_000.0,
                "large_net": None,
                "super_net": None,
                "main_net_5d": 9.0,
                "source": "akshare_ths",
            }
        ],
    )
    frame = store.read_partition(tmp_path, "stock", "2026-10-13")
    assert frame.height == 1
    row = frame.to_dicts()[0]
    assert row["source"] == source
    assert row["main_net"] == pytest.approx(50401248.0)
    assert row["large_net"] == pytest.approx(1.0)
    assert row["symbol"] == "600519.SH"


def test_ths_still_fills_codes_without_eastmoney_and_replaces_older_ths(tmp_path) -> None:
    store.write_rows(
        tmp_path,
        "stock",
        "2026-10-13",
        [
            {
                "code": "600519",
                "trade_date": "2026-10-13",
                "main_net": 1.0,
                "source": "mx",
            },
            {
                "code": "000001",
                "trade_date": "2026-10-13",
                "main_net": 2.0,
                "source": "akshare_ths",
            },
        ],
    )
    store.write_rows(
        tmp_path,
        "stock",
        "2026-10-13",
        [
            {
                "code": "600519",
                "trade_date": "2026-10-13",
                "main_net": 8.0,
                "source": "akshare_ths",
            },
            {
                "code": "000001",
                "trade_date": "2026-10-13",
                "main_net": 9.0,
                "source": "akshare_ths",
            },
            {
                "code": "300750",
                "trade_date": "2026-10-13",
                "main_net": -3.0,
                "source": "akshare_ths",
            },
        ],
    )
    by_code = {
        row["code"]: row for row in store.read_partition(tmp_path, "stock", "2026-10-13").to_dicts()
    }
    assert by_code["600519"]["source"] == "akshare_ths"
    assert by_code["600519"]["main_net"] == pytest.approx(8.0)
    assert by_code["000001"]["main_net"] == pytest.approx(9.0)
    assert by_code["300750"]["source"] == "akshare_ths"
    assert by_code["300750"]["symbol"] == "300750.SZ"


def test_sync_stock_rank_fallback_keeps_existing_eastmoney(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "ranking_limit", lambda _universe: 10_000)
    store.write_rows(
        tmp_path,
        "stock",
        "2026-10-13",
        [
            {
                "symbol": "600519.SH",
                "code": "600519",
                "name": "贵州茅台",
                "trade_date": "2026-10-13",
                "main_net": 50401248.0,
                "large_net": 1.0,
                "super_net": 2.0,
                "source": "eastmoney_datacenter",
            }
        ],
    )

    def bill(symbol: str):
        raise RuntimeError("push2his 403")

    def ak(name, **kwargs):
        if name == "stock_fund_flow_individual" and kwargs.get("symbol") == "即时":
            return [
                {"股票代码": "600519", "股票简称": "贵州茅台", "净额": -167_000_000},
                {"股票代码": "000001", "股票简称": "平安银行", "净额": 500_000_000},
            ]
        return []

    jobs.sync_stock(
        tmp_path,
        ["600519.SH", "000001.SZ"],
        SourceClients(history_bill=bill, ak_call=ak),
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="",
        trade_date="2026-10-13",
    )
    by_code = {
        row["code"]: row for row in store.read_partition(tmp_path, "stock", "2026-10-13").to_dicts()
    }
    assert by_code["600519"]["source"] == "eastmoney_datacenter"
    assert by_code["600519"]["main_net"] == pytest.approx(50401248.0)
    assert by_code["600519"]["large_net"] == pytest.approx(1.0)
    assert by_code["000001"]["source"] == "akshare_ths"
    assert by_code["000001"]["main_net"] == pytest.approx(500_000_000)
    assert by_code["000001"]["symbol"] == "000001.SZ"


def test_resolve_dates_prefers_local_calendar_and_weekdays(tmp_path) -> None:
    for day in ("2026-10-08", "2026-10-09", "2026-10-12"):
        (tmp_path / "kline_daily" / f"date={day}").mkdir(parents=True)
    assert resolve_dates(
        tmp_path,
        days=2,
        today=date(2026, 10, 15),
    ) == ["2026-10-09", "2026-10-12"]
    assert resolve_dates(
        tmp_path,
        start="2026-10-09",
        end="2026-10-12",
        today=date(2026, 10, 15),
    ) == ["2026-10-09", "2026-10-12"]
    empty = tmp_path / "no-calendar"
    assert resolve_dates(empty, days=2, today=date(2026, 10, 11)) == ["2026-10-08", "2026-10-09"]
    assert resolve_dates(
        empty,
        start="2026-10-10",
        end="2026-10-12",
        today=date(2026, 10, 15),
    ) == ["2026-10-12"]
    with pytest.raises(ValueError):
        resolve_dates(tmp_path, days=2, start="2026-10-01", today=date(2026, 10, 15))


def test_cli_backfill_command(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "app.fund_flow.backfill.resolve_dates",
        lambda _data_dir, **_kwargs: ["2026-10-13"],
    )

    def fake(url, *, params=None, headers=None, timeout=None):
        return _response(_page([_raw("600519", "SH", "贵州茅台", 5040.1248, 1, 2)]))

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    code = main(
        [
            "--days",
            "120",
            "--data-dir",
            str(tmp_path),
            "--spacing",
            "0",
            "--concurrency",
            "3",
        ]
    )
    assert code == 0
    text = capsys.readouterr().out
    assert "2026-10-13 wrote=" in text
    assert "errors=0" in text
    frame = store.read_partition(tmp_path, "stock", "2026-10-13")
    assert frame["symbol"][0] == "600519.SH"
    assert frame["main_net"][0] == pytest.approx(50401248)
    assert frame["source"][0] == SOURCE
    monkeypatch.undo()
    assert main(["--data-dir", str(tmp_path)]) == 2
