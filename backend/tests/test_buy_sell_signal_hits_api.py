"""命中只读 API 的端到端测例(走真实 FastAPI 路由)。

与 `test_buy_sell_signal_hits.py` 的分工: 那边测 schema / store / 闸的纯函数,
这边测"接上 HTTP 之后行为是否仍然成立" —— 尤其是**服务端闸**, 必须是服务端挡住的,
不能靠前端过滤装出来。

刻意**不**启整个 `app.main`: 那会拉数据源、调度器、SSE 一大堆。只挂目标 router
加上它需要的两个 state(`repo.store.data_dir` 与定义注册表), 测的仍然是真实路由函数。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.buy_sell_signals_hits import router
from app.buy_sell_signals import hits_fixtures as fx
from app.buy_sell_signals.constants import EMPTY_HITS_MESSAGE, HITS_FILENAME, PERSIST_SUBDIR
from app.buy_sell_signals.hits_store import InMemoryHitStore
from app.buy_sell_signals.store import InMemorySignalRegistryStore

HITS_URL = "/api/buy-sell-signals/hits"


def _registry(*records) -> InMemorySignalRegistryStore:
    reg = InMemorySignalRegistryStore()
    for rec in records:
        reg.upsert(rec)
    return reg


def _full_registry() -> InMemorySignalRegistryStore:
    return _registry(
        fx.publishable_definition(),
        fx.candidate_definition(),
        fx.observe_definition(),
        fx.limit_reopen_definition(),
        fx.daily_fundamental_definition(),
        fx.composite_definition(),
    )


def _client(
    tmp_path: Path,
    *,
    hits: list | None = None,
    registry: InMemorySignalRegistryStore | None = None,
    watch: list[str] | None = None,
    holdings: list[str] | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.buy_sell_signal_store = registry if registry is not None else _full_registry()
    app.state.buy_sell_signal_hit_store = InMemoryHitStore(hits or [])

    # 自选 / 持仓用 monkeypatch 级的桩, 不碰真实 data 目录
    import app.api.buy_sell_signals_hits as mod

    app.state._test_watch = watch or []
    app.state._test_holdings = holdings or []
    mod._watchlist_symbols = lambda: set(app.state._test_watch)
    mod._paper_symbols = lambda _data_dir: set(app.state._test_holdings)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_module_functions():
    """每个测例结束后还原被替换的模块级函数。"""
    import app.api.buy_sell_signals_hits as mod

    saved_watch = mod._watchlist_symbols
    saved_paper = mod._paper_symbols
    yield
    mod._watchlist_symbols = saved_watch
    mod._paper_symbols = saved_paper


# ------------------------------------------------------------------ 空态


def test_empty_store_returns_pinned_empty_message(tmp_path):
    resp = _client(tmp_path).get(HITS_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["hits"] == []
    assert body["empty_message"] == "暂无已校准的买卖信号"


def test_empty_message_does_not_drift_to_reassuring_wording(tmp_path):
    """「今天没信号」这类句子必须让这条红 —— 它把"还没校准"说成"今天没有"。"""
    body = _client(tmp_path).get(HITS_URL).json()
    msg = body["empty_message"]
    assert msg == EMPTY_HITS_MESSAGE
    for forbidden in ("今天没", "今日无", "暂时没有信号", "暂无信号"):
        assert forbidden not in msg


def test_missing_hits_file_yields_empty_state(tmp_path):
    """生产全新部署: 没有 hits.json → 空态, 不 500。"""
    resp = _client(tmp_path).get(HITS_URL)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_hits_file_on_disk_is_read_through_the_route(tmp_path):
    """真的写一份 hits.json 到 tmp_path, 走磁盘只读店读出命中。"""
    path = tmp_path / PERSIST_SUBDIR / HITS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "namespace": "buy_sell_signals",
                "hits": [fx.publishable_hit().to_dict()],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.buy_sell_signal_store = _full_registry()
    # 不注入 hit store → 走 build_hit_store(data_dir) 的磁盘路径
    body = TestClient(app).get(HITS_URL).json()
    assert body["count"] == 1
    assert body["hits"][0]["symbol"] == "000001"


# ------------------------------------------------------------ 服务端 publishable 闸


def test_only_publishable_hit_survives_the_server_side_gate(tmp_path):
    """三条夹具(pub / cand / limit_reopen)进 → 响应里只剩 pub 那条。"""
    client = _client(
        tmp_path,
        hits=[fx.publishable_hit(), fx.candidate_hit(), fx.limit_reopen_hit()],
    )
    body = client.get(HITS_URL).json()
    assert body["count"] == 1
    assert [h["hit_id"] for h in body["hits"]] == ["fx.hit.pub.000001"]
    assert body["rejected"]["definition_not_publishable"] == 2


def test_observe_hit_never_appears(tmp_path):
    client = _client(tmp_path, hits=[fx.observe_hit()])
    body = client.get(HITS_URL).json()
    assert body["count"] == 0


def test_gate_is_server_side_not_presentational(tmp_path):
    """前端没传任何过滤参数时, 服务端也必须已经滤掉。

    这条测的是"闸不在前端": 请求只有裸 URL, 没有任何 UI 状态, 结果里就没有
    candidate / limit_reopen 的命中。
    """
    client = _client(
        tmp_path,
        hits=[
            fx.publishable_hit(),
            fx.candidate_hit(),
            fx.observe_hit(),
            fx.limit_reopen_hit(),
            fx.orphan_hit(),
        ],
    )
    body = client.get(HITS_URL).json()
    symbols = {h["symbol"] for h in body["hits"]}
    assert symbols == {"000001"}


def test_registry_missing_returns_empty_not_500(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.buy_sell_signal_hit_store = InMemoryHitStore([fx.publishable_hit()])
    body = TestClient(app).get(HITS_URL).json()
    assert body["count"] == 0
    assert body["rejected"]["definition_missing"] == 1


# ------------------------------------------------------------ 定义状态事件


def test_definition_status_event_never_shows_up_as_a_hit(tmp_path, monkeypatch):
    """`/api/alerts` 里塞一条定义状态事件, 命中接口**一条都不该多**。"""
    from app.services import alert_store

    alert_store.append_many(
        tmp_path,
        [
            {
                "ts": 1_700_000_000_000,
                "rule_id": "bss.definition",
                "source": "buy_sell_signals",
                "type": "buy_sell_signal_status_changed",
                "monitor_event_type": "buy_sell_signal_status_changed",
                "signal_id": "fx.pub.intraday_volume",
                "status": "publishable",
            }
        ],
    )
    client = _client(tmp_path, hits=[])
    body = client.get(HITS_URL).json()
    assert body["count"] == 0
    dumped = json.dumps(body, ensure_ascii=False)
    assert "buy_sell_signal_status_changed" not in dumped


# ------------------------------------------------------------ family / symbol


def test_family_filter_only_accepts_exposed_families(tmp_path):
    client = _client(
        tmp_path,
        hits=[fx.publishable_hit(), fx.daily_fundamental_hit()],
        watch=["600000"],
    )
    assert client.get(HITS_URL, params={"family": "minute_event"}).json()["count"] == 1
    assert client.get(HITS_URL, params={"family": "daily_fundamental"}).json()["count"] == 1
    # composite 不在暴露集里 → 空, 且不是 400
    resp = client.get(HITS_URL, params={"family": "composite"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_symbol_filter(tmp_path):
    client = _client(tmp_path, hits=[fx.publishable_hit(symbol="000001")])
    assert client.get(HITS_URL, params={"symbol": "000001"}).json()["count"] == 1
    assert client.get(HITS_URL, params={"symbol": "600519"}).json()["count"] == 0


# ------------------------------------------------------------ 范围闸(自选 ∪ 持仓)


def test_fundamental_hit_hidden_when_not_in_watchlist_or_holdings(tmp_path):
    client = _client(tmp_path, hits=[fx.daily_fundamental_hit(symbol="600000")], watch=[], holdings=[])
    body = client.get(HITS_URL).json()
    assert body["count"] == 0
    assert body["rejected"]["out_of_scope"] == 1


def test_fundamental_hit_visible_when_in_watchlist(tmp_path):
    client = _client(tmp_path, hits=[fx.daily_fundamental_hit(symbol="600000")], watch=["600000"])
    assert client.get(HITS_URL).json()["count"] == 1


def test_fundamental_hit_visible_when_only_in_holdings(tmp_path):
    client = _client(
        tmp_path, hits=[fx.daily_fundamental_hit(symbol="600000")], watch=[], holdings=["600000"]
    )
    assert client.get(HITS_URL).json()["count"] == 1


def test_event_hit_ignores_scope(tmp_path):
    client = _client(tmp_path, hits=[fx.publishable_hit(symbol="999999")], watch=[], holdings=[])
    assert client.get(HITS_URL).json()["count"] == 1


# ------------------------------------------------------------ 过期显形


def test_stale_fundamental_hit_is_returned_but_marked(tmp_path):
    """过期**不隐藏**, 但要带 stale=true —— 藏起来等于假装没发生过。"""
    client = _client(
        tmp_path,
        hits=[fx.stale_daily_hit(symbol="600000")],
        watch=["600000"],
    )
    body = client.get(HITS_URL).json()
    assert body["count"] == 1
    assert body["hits"][0]["stale"] is True
    assert body["hits"][0]["is_daily_fundamental"] is True


def test_fresh_fundamental_hit_is_not_marked_stale(tmp_path):
    client = _client(
        tmp_path,
        hits=[fx.daily_fundamental_hit(symbol="600000")],
        watch=["600000"],
    )
    body = client.get(HITS_URL).json()
    assert body["hits"][0]["stale"] is False


def test_today_override_only_affects_staleness(tmp_path):
    """`today` 参数只影响过期判定, 不影响可见性。"""
    client = _client(
        tmp_path,
        hits=[fx.daily_fundamental_hit(symbol="600000", asof="2026-10-20")],
        watch=["600000"],
    )
    same_day = client.get(HITS_URL, params={"today": "2026-10-20"}).json()
    later = client.get(HITS_URL, params={"today": "2026-10-26"}).json()
    assert same_day["hits"][0]["stale"] is False
    assert later["hits"][0]["stale"] is True
    assert later["count"] == 1


# ------------------------------------------------------------ 响应契约


def test_response_contract_keys(tmp_path):
    body = _client(tmp_path, hits=[fx.publishable_hit()]).get(HITS_URL).json()
    for key in ("schema_version", "namespace", "empty_message", "hits", "count"):
        assert key in body
    assert body["namespace"] == "buy_sell_signals"
    hit = body["hits"][0]
    for key in ("hit_id", "signal_id", "definition_version", "symbol", "asof", "family",
                "event_class", "direction_label", "disclaimer", "stale", "asof_date"):
        assert key in hit


def test_response_never_carries_trading_fields(tmp_path):
    """整个响应里不得出现价格 / 数量 / 下单字段。"""
    body = _client(tmp_path, hits=[fx.publishable_hit()]).get(HITS_URL).json()
    dumped = json.dumps(body, ensure_ascii=False)
    for forbidden in ('"price"', '"qty"', '"order_id"', '"position"'):
        assert forbidden not in dumped


def test_disclaimer_present_on_every_hit(tmp_path):
    body = _client(
        tmp_path,
        hits=[fx.publishable_hit(), fx.daily_fundamental_hit()],
        watch=["600000"],
    ).get(HITS_URL).json()
    assert body["count"] == 2
    for hit in body["hits"]:
        assert "仅供研究" in hit["disclaimer"]


def test_scope_endpoint_reports_stats_not_symbols(tmp_path):
    client = _client(tmp_path, watch=["000001", "600000"], holdings=["600519"])
    body = client.get(f"{HITS_URL}/scope").json()
    assert body["scope_size"] == 3
    assert body["scope_family"] == "daily_fundamental"
    assert body["empty_message"] == EMPTY_HITS_MESSAGE
    dumped = json.dumps(body, ensure_ascii=False)
    # 不回明细: 这个接口不该变成第二个自选接口
    assert "000001" not in dumped
    assert "600519" not in dumped
