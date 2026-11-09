"""XTick 历史竞价补充：fixture 覆盖真实 ZIP/data.json，不访问上游。"""

from __future__ import annotations

import ast
import io
import json
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.xtick_supplement import router
from app.config import settings
from app.market_time import CN_TZ
from app.services import auction_scan
from app.services import xtick_supplement as xs

BACKEND = Path(__file__).resolve().parents[1]
CLOCK = datetime(2026, 11, 9, 17, 20, tzinfo=CN_TZ)
SAMPLE = {
    "type": 1,
    "code": "000001",
    "seq": 1,
    "time": 1770307200000,
    "price": 11.08,
    "close": 11.09,
    "jjzf": -0.09,
    "jjl": 8018,
    "jje": 8883944,
    "nol": 1469,
    "noe": 1627652,
    "trend": -1,
}
TOKEN = "fixture-token"


def _zip(text: str, extra: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.json", text)
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return buf.getvalue()


def _ndjson_zip(*rows: dict) -> bytes:
    return _zip("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in (
        "XTICK_SUPPLEMENT_ENABLED",
        "XTICK_BIDHISTORY_ALLOW_ALL",
        "XTICK_API_TOKEN",
        "XTICK_API_BASE",
        "XTICK_HTTP_TIMEOUT_S",
        "XTICK_MIN_INTERVAL_S",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XTICK_MIN_INTERVAL_S", "0")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    import app.secrets_store as secrets_store

    monkeypatch.setattr(secrets_store, "_cache", None)
    monkeypatch.setattr(secrets_store, "_cache_sig", None)
    xs.reset_rate_limit_for_tests()
    yield tmp_path
    xs.reset_rate_limit_for_tests()


def _query(tmp_path: Path, transport, **kwargs):
    calls = {"n": 0, "urls": []}

    def wrapped(url, timeout):
        calls["n"] += 1
        calls["urls"].append(url)
        return transport(url, timeout)

    result = xs.query_bidhistory(
        tmp_path,
        code=kwargs.get("code", "000001"),
        trade_date=kwargs.get("trade_date", "2026-11-06"),
        seq=kwargs.get("seq", "1"),
        now=kwargs.get("now", CLOCK),
        transport=wrapped,
        mono=kwargs.get("mono"),
    )
    return result, calls


def test_disabled_does_not_call_upstream(tmp_path):
    result, calls = _query(tmp_path, lambda url, timeout: (_ for _ in ()).throw(AssertionError("called")))
    assert result["state"] == "disabled"
    assert result["live"] is False
    assert result["affects_scoring"] is False
    assert result["affects_risk"] is False
    assert result["affects_triggers"] is False
    assert calls["n"] == 0


def test_missing_credential_does_not_call_upstream(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    result, calls = _query(tmp_path, lambda url, timeout: (200, b"[]"))
    assert result["state"] == "missing_credential"
    assert calls["n"] == 0
    assert TOKEN not in json.dumps(result)
    assert "XTICK_API_TOKEN" in result["message"]


def test_zip_ndjson_uses_documented_contract_and_redacts(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    result, calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    assert calls["n"] == 1
    url = calls["urls"][0]
    assert url.startswith("http://api.xtick.top/doc/hot/bidhistory?")
    assert "type=1" in url
    assert "code=000001" in url
    assert "seq=1" in url
    assert "startDate=2026-11-06" in url
    assert "endDate=2026-11-06" in url
    assert TOKEN in url
    blob = json.dumps(result)
    assert TOKEN not in blob
    assert result["state"] == "ok"
    assert result["live"] is False
    assert result["freshness"] == "historical"
    assert result["cache"] == "miss"
    assert result["primary_source_unchanged"] is True
    row = result["rows"][0]
    assert row["prev_close"] == 11.09
    assert "close" not in row
    assert row["jjzf_percent"] == -0.09
    assert row["jjl"] == 8018
    assert row["jje"] == 8883944
    assert row["nol"] == 1469
    assert row["noe"] == 1627652
    assert row["trend_label"] == "主动性卖"
    assert row["symbol"] == "000001.SZ"
    assert row["amount_identity"]["jj"] == "matches_price_times_qty_times_100"
    assert row["amount_identity"]["unmatched"] == "matches_price_times_qty_times_100"
    assert row["time_bj"] == "2026-02-06 00:00:00"
    assert row["time_is_midnight"] is True
    assert row["time_matches_trade_date"] is False
    assert result["warnings"] == ["vendor_time_date_mismatch"]
    assert result["seq_doc"]["adopted_label"] is None
    assert result["seq_doc"]["conflict"] is True
    cached = next((tmp_path / "xtick_supplement").rglob("*.json"))
    assert TOKEN not in cached.read_text(encoding="utf-8")


def test_documented_json_array_body_and_cache_hit(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    body = json.dumps([SAMPLE]).encode()
    result, calls = _query(tmp_path, lambda url, timeout: (200, body))
    assert result["state"] == "ok"
    assert result["row_count"] == 1
    again, calls2 = _query(tmp_path, lambda url, timeout: (_ for _ in ()).throw(AssertionError("refetch")))
    assert again["cache"] == "hit"
    assert again["freshness"] == "historical"
    assert again["live"] is False
    assert calls["n"] == 1
    assert calls2["n"] == 0


def test_empty_array_is_missing_and_not_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)

    def transport(url, timeout):
        return 200, b"[]"

    result, calls = _query(tmp_path, transport)
    assert result["state"] == "missing"
    assert result["rows"] == []
    assert "限流" in result["message"]
    cache_root = tmp_path / "xtick_supplement"
    assert not cache_root.exists() or not list(cache_root.rglob("*.json"))
    again, calls2 = _query(tmp_path, transport)
    assert calls["n"] == 1
    assert calls2["n"] == 1
    assert again["state"] == "missing"


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (500, b"", "http_status"),
        (502, b"token=fixture-token", "http_status"),
        (200, b"", "empty_body"),
        (200, b"   ", "empty_body"),
        (200, b"PK\x03\x04broken", "zip"),
        (200, b"not-json", "json"),
        (429, b"[]", "rate_limited"),
    ],
)
def test_upstream_failures_are_isolated(monkeypatch, tmp_path, status, body, kind):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    result, _calls = _query(tmp_path, lambda url, timeout: (status, body))
    assert result["live"] is False
    assert result["rows"] == []
    assert result["error"]["kind"] == kind
    assert TOKEN not in json.dumps(result)
    if kind == "rate_limited":
        assert result["state"] == "rate_limited"
    else:
        assert result["state"] == "error"
    if status != 200:
        assert result["error"]["http_status"] == status


def test_timeout_and_exception_do_not_leak_token(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)

    def boom(url, timeout):
        raise RuntimeError(f"boom {url}")

    caplog.set_level("WARNING")
    result, _calls = _query(tmp_path, boom)
    assert result["state"] == "error"
    assert result["error"]["kind"] == "internal"
    body = json.dumps(result)
    assert TOKEN not in body
    assert "api.xtick.top" not in body
    assert "token=" not in body
    assert TOKEN not in caplog.text
    assert "token=***" in caplog.text


def test_zip_without_data_json_and_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    missing = io.BytesIO()
    with zipfile.ZipFile(missing, "w") as archive:
        archive.writestr("readme.txt", "{}")
    result, _calls = _query(tmp_path, lambda url, timeout: (200, missing.getvalue()))
    assert result["error"]["kind"] == "zip"
    slipped = io.BytesIO()
    with zipfile.ZipFile(slipped, "w") as archive:
        archive.writestr("../data.json", json.dumps([SAMPLE]))
    slipped_result, _calls = _query(tmp_path, lambda url, timeout: (200, slipped.getvalue()))
    assert slipped_result["error"]["kind"] == "zip"
    assert slipped_result["rows"] == []


def test_row_and_byte_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    monkeypatch.setattr(xs, "MAX_ROWS", 1)
    two = _ndjson_zip(SAMPLE, {**SAMPLE, "code": "000002"})
    limited, _calls = _query(tmp_path, lambda url, timeout: (200, two))
    assert limited["error"]["kind"] == "too_large"
    assert limited["rows"] == []
    monkeypatch.setattr(xs, "MAX_UNCOMPRESSED_BYTES", 32)
    oversized, _calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    assert oversized["error"]["kind"] == "too_large"


def test_date_semantics_block_live_auction_weekend_and_future(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)

    def transport(url, timeout):
        return 200, _ndjson_zip(SAMPLE)

    early, early_calls = _query(
        tmp_path,
        transport,
        trade_date="2026-11-09",
        now=datetime(2026, 11, 9, 9, 25, tzinfo=CN_TZ),
    )
    assert early["state"] == "not_ready"
    assert early["live"] is False
    assert early_calls["n"] == 0
    ready, ready_calls = _query(
        tmp_path,
        transport,
        trade_date="2026-11-09",
        now=datetime(2026, 11, 9, 9, 26, tzinfo=CN_TZ),
    )
    assert ready["state"] == "ok"
    assert ready["freshness"] == "same_day_final"
    assert ready["live"] is False
    assert "不是盘中实时行情" in ready["message"]
    assert ready_calls["n"] == 1
    weekend, weekend_calls = _query(
        tmp_path,
        transport,
        trade_date="2026-11-08",
        now=CLOCK,
    )
    assert weekend["state"] == "invalid_request"
    assert weekend_calls["n"] == 0
    future, future_calls = _query(
        tmp_path,
        transport,
        trade_date="2026-11-10",
        now=CLOCK,
    )
    assert future["state"] == "invalid_request"
    assert future_calls["n"] == 0


def test_seq_omitted_all_guard_and_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    monkeypatch.setenv("XTICK_MIN_INTERVAL_S", "10")
    xs.reset_rate_limit_for_tests()

    def transport(url, timeout):
        return 200, _ndjson_zip(SAMPLE)

    denied, denied_calls = _query(tmp_path, transport, code="all", seq=None, mono=0)
    assert denied["state"] == "invalid_request"
    assert denied_calls["n"] == 0
    first, first_calls = _query(tmp_path, transport, code="000001", seq=None, mono=0)
    assert first["state"] == "ok"
    assert "seq=" not in first_calls["urls"][0]
    second, second_calls = _query(tmp_path, transport, code="000002", seq=None, mono=0.2)
    assert second["state"] == "rate_limited"
    assert second_calls["n"] == 0
    monkeypatch.setenv("XTICK_BIDHISTORY_ALLOW_ALL", "1")
    monkeypatch.setenv("XTICK_MIN_INTERVAL_S", "0")
    allowed, allowed_calls = _query(tmp_path, transport, code="ALL", seq=None, mono=100)
    assert allowed["state"] == "ok"
    assert "code=all" in allowed_calls["urls"][0]


def test_symbol_is_not_guessed_for_unmapped_prefix():
    assert xs._display_symbol("600519") == "600519.SH"
    assert xs._display_symbol("300001") == "300001.SZ"
    assert xs._display_symbol("920305") == "920305.BJ"
    assert xs._display_symbol("999999") is None
    assert xs._display_symbol("400001") is None


def test_secrets_file_overrides_env_and_stays_out_of_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", "env-token-aaa")
    (tmp_path / "user_data").mkdir()
    (tmp_path / "user_data" / "secrets.json").write_text(
        json.dumps({"xtick_api_token": "file-token-bbb"}),
        encoding="utf-8",
    )
    result, calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    assert "file-token-bbb" in calls["urls"][0]
    assert "env-token-aaa" not in calls["urls"][0]
    blob = json.dumps(result)
    assert "file-token-bbb" not in blob
    assert "env-token-aaa" not in blob


def test_primary_chains_do_not_reference_the_supplement():
    roots = [
        BACKEND / "app/services/quote_service.py",
        BACKEND / "app/services/auction_scan.py",
        BACKEND / "app/services/auction_benchmark.py",
        BACKEND / "app/buy_sell_signals",
        BACKEND / "app/strategy",
        BACKEND / "app/t_rounds",
    ]
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            assert "xtick_supplement" not in text
            assert "api.xtick.top" not in text
    own = (BACKEND / "app/services/xtick_supplement.py").read_text(encoding="utf-8")
    assert "quote_service" not in own
    assert "auction_scan" not in own
    assert "buy_sell_signals" not in own


def test_api_returns_supplement_without_touching_auction_scan(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOCK

    monkeypatch.setattr(xs, "datetime", Frozen)
    monkeypatch.setattr(xs, "default_transport", lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)
    status = client.get("/api/xtick-supplement/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is True
    assert status.json()["credential_configured"] is True
    assert status.json()["allow_all"] is False
    assert TOKEN not in status.text
    assert "xtick_api_token" not in status.json()
    found = client.get(
        "/api/xtick-supplement/bidhistory",
        params={"code": "000001", "trade_date": "2026-11-06", "seq": "1"},
    )
    assert found.status_code == 200
    body = found.json()
    assert body["state"] == "ok"
    assert body["rows"][0]["nol"] == 1469
    assert TOKEN not in found.text
    scan = auction_scan.get_scan(tmp_path, None)
    assert scan["state"] == "no_data"


def test_status_reports_missing_credential(tmp_path):
    del tmp_path
    payload = xs.status_payload()
    assert payload["enabled"] is False
    assert payload["credential_configured"] is False
    assert payload["live"] is False
    assert TOKEN not in json.dumps(payload)


def test_secrets_json_enables_without_env_and_explicit_off_wins(monkeypatch, tmp_path):
    (tmp_path / "user_data").mkdir()
    (tmp_path / "user_data" / "secrets.json").write_text(
        json.dumps({
            "xtick_api_token": "file-token-bbb",
            "xtick_supplement_enabled": "1",
            "tickflow_api_key": "leave-this-key",
        }),
        encoding="utf-8",
    )
    result, calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    assert result["state"] == "ok"
    assert calls["n"] == 1
    assert "file-token-bbb" in calls["urls"][0]
    blob = json.dumps(result)
    assert "file-token-bbb" not in blob
    assert "leave-this-key" not in blob
    status = xs.status_payload()
    assert status["enabled"] is True
    assert status["credential_configured"] is True
    assert "file-token-bbb" not in json.dumps(status)
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "0")
    stopped, stopped_calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    assert stopped["state"] == "disabled"
    assert stopped_calls["n"] == 0


def test_invalid_inputs_are_not_echoed_and_bad_numbers_are_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    rejected, calls = _query(
        tmp_path,
        lambda url, timeout: (200, _ndjson_zip(SAMPLE)),
        code=TOKEN,
        trade_date="2026-11-06T00:00:00",
        seq=TOKEN,
    )
    assert calls["n"] == 0
    assert rejected["state"] == "invalid_request"
    assert rejected["code"] == ""
    assert rejected["trade_date"] == ""
    assert rejected["seq_requested"] is None
    assert TOKEN not in json.dumps(rejected)
    bad = dict(SAMPLE)
    bad.update(price=-1, close=True, jjzf=10_000, jjl=-3, code="../000001", seq=9)
    cleaned, _calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(bad)))
    row = cleaned["rows"][0]
    assert cleaned["state"] == "ok"
    assert row["price"] is None
    assert row["prev_close"] is None
    assert row["jjzf_percent"] is None
    assert row["jjl"] is None
    assert row["code"] is None
    assert row["seq"] is None
    assert "close" not in row


def test_error_and_truncated_cache_do_not_pollute_the_next_read(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    cache = (
        tmp_path / "xtick_supplement" / "bidhistory" / "date=2026-11-06" / "000001__seq-1.json"
    )
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({
        "dataset": "bidhistory",
        "state": "error",
        "error": {"message": TOKEN},
        "live": False,
        "code": "000001",
        "trade_date": "2026-11-06",
        "seq_requested": "1",
        "rows": [{"code": "000001", "price": -5, "close": 11}],
    }), encoding="utf-8")
    result, calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    assert calls["n"] == 1
    assert result["state"] == "ok"
    assert result["cache"] == "miss"
    assert TOKEN not in json.dumps(result)
    assert result["rows"][0]["price"] == 11.08
    cache.write_text("{", encoding="utf-8")
    again, again_calls = _query(tmp_path, lambda url, timeout: (200, _ndjson_zip(SAMPLE)))
    assert again_calls["n"] == 1
    assert again["state"] == "ok"
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["state"] == "ok"
    assert TOKEN not in cache.read_text(encoding="utf-8")


def test_parallel_cache_writes_leave_one_complete_json(monkeypatch, tmp_path):
    monkeypatch.setenv("XTICK_SUPPLEMENT_ENABLED", "1")
    monkeypatch.setenv("XTICK_API_TOKEN", TOKEN)
    monkeypatch.setenv("XTICK_MIN_INTERVAL_S", "0")
    xs.reset_rate_limit_for_tests()
    errors: list[BaseException] = []

    def worker():
        try:
            xs.query_bidhistory(
                tmp_path,
                code="000001",
                trade_date="2026-11-06",
                seq="1",
                now=CLOCK,
                transport=lambda url, timeout: (200, _ndjson_zip(SAMPLE)),
                mono=1000,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    cache = next((tmp_path / "xtick_supplement").rglob("*.json"))
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["state"] == "ok"
    assert saved["rows"][0]["nol"] == 1469
    assert TOKEN not in cache.read_text(encoding="utf-8")


def test_routes_stay_on_the_existing_auth_middleware():
    source = (BACKEND / "app/main.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    exact = prefix = None
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "_AUTH_WHITELIST_EXACT":
                exact = ast.literal_eval(node.value)
            if target.id == "_AUTH_WHITELIST_PREFIX":
                prefix = ast.literal_eval(node.value)
    assert exact == ("/health", "/api/events")
    assert prefix == ("/api/auth/",)
    for path in ("/api/xtick-supplement/status", "/api/xtick-supplement/bidhistory"):
        assert path not in exact
        assert not any(path.startswith(item) for item in prefix)
    assert 'if not path.startswith("/api/")' in source
    assert "未登录或会话已过期" in source
    from app.api.xtick_supplement import router as xtick_router

    assert {route.path for route in xtick_router.routes} == {
        "/api/xtick-supplement/status",
        "/api/xtick-supplement/bidhistory",
    }
