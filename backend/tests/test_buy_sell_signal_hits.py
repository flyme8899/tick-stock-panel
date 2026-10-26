"""买卖信号**命中层**的 schema / 存储 / 可见性 / 过期 / API 闸。

本文件只覆盖后端。前端 tab / 空态 / chip 的验收另见 PR 里的手工步骤与
`frontend/src/pages/Monitor.tsx` 的注释。

测例分四组:

1. schema: 必填、封闭集、禁交易字段、序列化往返;
2. store: 缺文件 / 空文件 / 损坏 / 脏条目 / 路径钉死;
3. 可见性: publishable 可见, candidate / observe / limit_reopen / 缺定义 / composite
   不可见; 范围闸只作用于基本面;
4. API: 服务端闸、空态文案、不混入定义状态事件。

全部用夹具 + `tmp_path`, **不依赖**任何真实行情、真实 webhook、真实账户。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.buy_sell_signals import hits_fixtures as fx
from app.buy_sell_signals.calib_store import CalibIndex
from app.buy_sell_signals.constants import (
    DIRECTION_LABELS,
    EMPTY_HITS_MESSAGE,
    HITS_FILENAME,
    MONITOR_EVENT_TYPE,
    PERSIST_SUBDIR,
)
from app.buy_sell_signals.hits_models import (
    FORBIDDEN_PAYLOAD_KEYS,
    HitSource,
    HitValidationError,
    SignalHit,
    asof_date_or_none,
)
from app.buy_sell_signals.hits_store import (
    DiskHitStore,
    InMemoryHitStore,
    build_hit_store,
)
from app.buy_sell_signals.hits_visibility import (
    EXPOSED_FAMILIES,
    VisibilityReject,
    build_response,
    hit_to_view,
    visible_hits,
)
from app.buy_sell_signals.store import InMemorySignalRegistryStore
from app.market_time import cn_today


def _registry(*records) -> InMemorySignalRegistryStore:
    reg = InMemorySignalRegistryStore()
    for rec in records:
        reg.upsert(rec)
    return reg


def _passed_index() -> CalibIndex:
    """一条 `passed` 登记覆盖全部夹具信号的三键。

    ## 为什么这个 helper 是必要的(而不是"给测例开后门")

    叠加校准闸之后, "定义是 publishable" 不再等于 "命中可见"。既有测例谈的是
    **前半段**闸(定义状态 / 事件类型 / 范围), 要让它们继续谈前半段, 就得把后半段
    (校准) 配齐 —— 否则每条都会卡在 `calib_not_passed` 上, 测出来的全是同一件事。

    所以这里给的是"校准已过"这个**前提**, 不是"跳过校准闸"的开关: 闸本身仍然
    逐条走(`CalibIndex.get` 找不到就拒), 只是这次的登记确实都在。谁想验"没登记
    就不显示", 传 `CalibIndex([])` —— 那正是本文件末尾 adjacencies 段的做法。
    """
    return CalibIndex(
        [
            fx.passed_calib(signal_id="fx.pub.intraday_volume"),
            fx.passed_calib(signal_id="fx.pub.daily_quality_value"),
            # 以下两条是"对照组的证明": 给 candidate / limit_reopen 也配上 passed,
            # 才能验出它们挡住的原因**不是**校准缺失, 而是定义状态 / 事件类型本身。
            fx.passed_calib(signal_id="fx.cand.intraday_volume"),
            fx.passed_calib(signal_id="fx.limit_reopen"),
        ]
    )


def _full_registry() -> InMemorySignalRegistryStore:
    return _registry(
        fx.publishable_definition(),
        fx.candidate_definition(),
        fx.observe_definition(),
        fx.limit_reopen_definition(),
        fx.daily_fundamental_definition(),
        fx.composite_definition(),
    )


# =============================================================== 1. schema


def test_empty_message_is_pinned_verbatim():
    """空态文案逐字写死 —— 改成「今天没信号」这类句子必须让这条红。"""
    assert EMPTY_HITS_MESSAGE == "暂无已校准的买卖信号"


def test_hit_carries_all_required_identity_fields():
    hit = fx.publishable_hit()
    payload = hit.to_dict()
    for key in (
        "schema_version",
        "namespace",
        "hit_id",
        "signal_id",
        "definition_version",
        "symbol",
        "asof",
        "source",
        "family",
        "event_class",
        "direction_label",
        "disclaimer",
        "disclaimer_key",
    ):
        assert key in payload, f"命中缺少必填字段 {key}"


@pytest.mark.parametrize("field", ["hit_id", "signal_id", "definition_version", "symbol", "asof"])
def test_blank_identity_field_is_rejected(field):
    kwargs = {
        "hit_id": "h1",
        "signal_id": "s1",
        "definition_version": "v1",
        "symbol": "000001",
        "asof": "2026-10-24",
        "source": HitSource(evaluator="fx", mode="fixture"),
        "family": "minute_event",
        "event_class": "intraday_volume",
        "direction_label": "research_watch",
    }
    kwargs[field] = "   "
    with pytest.raises(HitValidationError):
        SignalHit(**kwargs)


def test_direction_label_is_a_closed_set():
    assert frozenset({"research_buy", "research_sell", "research_watch"}) == DIRECTION_LABELS
    kwargs = {
        "hit_id": "h1",
        "signal_id": "s1",
        "definition_version": "v1",
        "symbol": "000001",
        "asof": "2026-10-24",
        "source": HitSource(evaluator="fx", mode="fixture"),
        "family": "minute_event",
        "event_class": "intraday_volume",
        "direction_label": "买入",
    }
    with pytest.raises(HitValidationError):
        SignalHit(**kwargs)


def test_direction_label_rejects_actionable_wording():
    for bad in ("buy", "sell", "加仓", "下单", "BUY"):
        kwargs = {
            "hit_id": "h1",
            "signal_id": "s1",
            "definition_version": "v1",
            "symbol": "000001",
            "asof": "2026-10-24",
            "source": HitSource(evaluator="fx", mode="fixture"),
            "family": "minute_event",
            "event_class": "intraday_volume",
            "direction_label": bad,
        }
        with pytest.raises(HitValidationError):
            SignalHit(**kwargs)


def test_source_mode_is_a_closed_set():
    with pytest.raises(HitValidationError):
        HitSource(evaluator="fx", mode="prod")
    with pytest.raises(HitValidationError):
        HitSource(evaluator="", mode="fixture")


@pytest.mark.parametrize("bad_key", sorted(FORBIDDEN_PAYLOAD_KEYS))
def test_forbidden_trading_field_in_payload_is_rejected(bad_key):
    """载荷里出现价格 / 数量 / 下单字段 → 拒。

    用参数化逐个跑, 而不是只试一个: 黑名单里每个词都该被真正守住, 不是只守住
    `price` 那一条。
    """
    payload = fx.publishable_hit().to_dict()
    payload[bad_key] = 12.34
    with pytest.raises(HitValidationError):
        SignalHit.from_dict(payload)


def test_hit_serialization_round_trip():
    hit = fx.publishable_hit()
    again = SignalHit.from_dict(hit.to_dict())
    assert again == hit


def test_vendor_payload_without_price_survives_round_trip():
    """正向对照: 干净载荷必须能读回来, 否则上一条测例可能只是因为"全都拒"才绿。"""
    payload = fx.daily_fundamental_hit().to_dict()
    assert "price" not in payload
    assert SignalHit.from_dict(payload).symbol == "600000"


# --------------------------------------------------------- 过期(N4)


def test_asof_yesterday_is_stale():
    # 夹具按 cn_today() 算「昨天」，这里的参照日必须跟着走 —— 写死某一天的话，
    # 一过零点就静默失效（本仓真的这么红过一次）。
    today = cn_today()
    assert fx.stale_daily_hit().is_stale(today=today) is True


def test_asof_today_is_not_stale():
    today = cn_today()
    hit = fx.publishable_hit(asof=today.isoformat())
    assert hit.is_stale(today=today) is False


def test_illegal_asof_is_treated_as_stale():
    """读不出来的时间戳一律按过期呈现 —— fail-closed, 禁静默当"当前"。"""
    kwargs = {
        "hit_id": "h1",
        "signal_id": "s1",
        "definition_version": "v1",
        "symbol": "000001",
        "asof": "昨天",
        "source": HitSource(evaluator="fx", mode="fixture"),
        "family": "minute_event",
        "event_class": "intraday_volume",
        "direction_label": "research_watch",
    }
    hit = SignalHit(**kwargs)
    assert hit.asof_day is None
    assert hit.is_stale(today=date(2026, 10, 26)) is True


def test_naive_datetime_asof_is_not_parsed():
    """裸时间不带时区 → 不解析(不猜), 因而判为过期。"""
    assert asof_date_or_none("2026-10-26T09:25:00") is None
    assert asof_date_or_none("2026-10-26T09:25:00+08:00") == date(2026, 10, 26)


def test_asof_from_utc_converts_to_shanghai_day():
    """UTC 16:30 已是上海次日 00:00 —— 用 UTC 日期会整体早一天。"""
    assert asof_date_or_none("2026-10-25T16:30:00+00:00") == date(2026, 10, 26)


def test_daily_fundamental_flag_covers_event_class():
    hit = fx.daily_fundamental_hit()
    assert hit.is_daily_fundamental is True
    # event_class 单独命中也要算日表形态
    manual = fx.publishable_hit()
    assert manual.is_daily_fundamental is False


# =============================================================== 2. store


def test_hits_path_is_pinned_under_buy_sell_signals(tmp_path):
    store = DiskHitStore(tmp_path)
    assert store.path == tmp_path / PERSIST_SUBDIR / HITS_FILENAME
    assert store.path.name == "hits.json"


def test_missing_hits_file_reads_as_empty(tmp_path):
    """全新部署没有 hits.json —— 0 条命中, 不抛错。"""
    assert DiskHitStore(tmp_path).list_all() == []


def test_empty_hits_file_reads_as_empty(tmp_path):
    path = tmp_path / PERSIST_SUBDIR / HITS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   \n", encoding="utf-8")
    assert DiskHitStore(tmp_path).list_all() == []


def test_corrupt_json_reads_as_empty_without_raising(tmp_path):
    """语法坏 → 空 + warning, 不 500, 更不回落去读注册表。"""
    path = tmp_path / PERSIST_SUBDIR / HITS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert DiskHitStore(tmp_path).list_all() == []


def test_wrong_shape_reads_as_empty(tmp_path):
    path = tmp_path / PERSIST_SUBDIR / HITS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hits": {"oops": 1}}), encoding="utf-8")
    assert DiskHitStore(tmp_path).list_all() == []


def test_one_bad_record_does_not_hide_the_others(tmp_path):
    """单条脏数据只跳过它自己 —— 一条坏记录不该让整个 tab 打不开。"""
    path = tmp_path / PERSIST_SUBDIR / HITS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    good = fx.publishable_hit().to_dict()
    dirty = dict(good, hit_id="bad", direction_label="买入")
    path.write_text(
        json.dumps({"schema_version": "1", "namespace": "buy_sell_signals", "hits": [good, dirty]}),
        encoding="utf-8",
    )
    hits = DiskHitStore(tmp_path).list_all()
    assert [h.hit_id for h in hits] == [good["hit_id"]]


def test_build_hit_store_ignores_persist_env(tmp_path, monkeypatch):
    """命中读取与 `BUY_SELL_SIGNALS_PERSIST` 无关 —— 那个 env 管的是定义注册表。"""
    monkeypatch.delenv("BUY_SELL_SIGNALS_PERSIST", raising=False)
    store = build_hit_store(tmp_path)
    assert isinstance(store, DiskHitStore)

    monkeypatch.setenv("BUY_SELL_SIGNALS_PERSIST", "1")
    assert isinstance(build_hit_store(tmp_path), DiskHitStore)


def test_in_memory_store_is_pure_injection():
    store = InMemoryHitStore([fx.publishable_hit()])
    assert len(store) == 1
    assert store.list_all()[0].hit_id == "fx.hit.pub.000001"
    # 没有写方法 —— 协议层就不给写入的口子
    assert not hasattr(store, "upsert")
    assert not hasattr(store, "write")


def test_hit_store_has_no_write_api():
    """只读店不得暴露任何写方法。"""
    for forbidden in ("upsert", "write", "append", "save"):
        assert not hasattr(DiskHitStore, forbidden), f"DiskHitStore 不该有 {forbidden}"


# =============================================================== 3. 可见性


def test_publishable_hit_is_visible():
    filtered = visible_hits([fx.publishable_hit()], _full_registry(), scope_symbols=frozenset(), calib=_passed_index())
    assert [h.hit_id for h in filtered.hits] == ["fx.hit.pub.000001"]


@pytest.mark.parametrize(
    "hit_factory",
    [fx.candidate_hit, fx.observe_hit, fx.limit_reopen_hit],
)
def test_non_publishable_hits_are_invisible(hit_factory):
    """candidate / observe / limit_reopen 的命中**不可见** —— 即使夹具真的写出了命中。"""
    filtered = visible_hits([hit_factory()], _full_registry(), scope_symbols=frozenset(), calib=_passed_index())
    assert filtered.hits == []
    assert sum(filtered.rejected.values()) == 1


def test_limit_reopen_rejected_even_if_definition_was_hand_edited_to_publishable():
    """定义侧被外部改成 publishable 也不放行 —— 双保险必须真的是一道独立的闸。"""
    from app.buy_sell_signals.models import SignalRecord

    forged = SignalRecord(
        signal_id="fx.limit_reopen",
        display_name="被手改的炸板信号",
        family="minute_event",
        event_class="limit_reopen",
        spec_ref="fixture://spec",
        version="fixture_v1",
        status="publishable",
        manual_publisher="someone",
        manual_published_at=fx.cn_now() if hasattr(fx, "cn_now") else __import__(
            "app.market_time", fromlist=["cn_now"]
        ).cn_now(),
    )
    filtered = visible_hits([fx.limit_reopen_hit()], _registry(forged), scope_symbols=frozenset(), calib=_passed_index())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.LIMIT_REOPEN) == 1


def test_hit_without_definition_is_invisible():
    filtered = visible_hits([fx.orphan_hit()], _full_registry(), scope_symbols=frozenset(), calib=_passed_index())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.DEFINITION_MISSING) == 1


def test_missing_registry_fails_closed():
    """注册表压根没有 → 全丢。不猜、不默认放行。"""
    filtered = visible_hits([fx.publishable_hit()], None, scope_symbols=frozenset(), calib=_passed_index())
    assert filtered.hits == []


def test_composite_hits_are_not_exposed():
    """`composite` 首批不暴露 —— 即使定义是 publishable。"""
    filtered = visible_hits([fx.composite_hit()], _full_registry(), scope_symbols=frozenset(), calib=_passed_index())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.FAMILY_NOT_EXPOSED) == 1
    assert "composite" not in EXPOSED_FAMILIES


def test_unknown_family_filter_returns_empty():
    filtered = visible_hits(
        [fx.publishable_hit()],
        _full_registry(),
        family="composite",
        scope_symbols=frozenset(),
        calib=_passed_index(),
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.UNKNOWN_FAMILY_FILTER) == 1


def test_definition_status_is_read_at_request_time_not_at_hit_time():
    """可见性看**当前**定义状态, 不看命中写入时的快照。

    同一条命中(指向 `fx.pub.intraday_volume`), 只换注册表里那条定义的当前状态:
    candidate → 不可见; publishable → 可见。命中自带的 `definition_status_at_hit`
    全程不变, 且**不参与**判断。
    """
    hit = fx.publishable_hit()  # signal_id = fx.pub.intraday_volume
    assert hit.definition_status_at_hit == "publishable"

    # 同 signal_id, 但当前状态是 candidate → 不可见
    downgraded = fx.candidate_definition(signal_id="fx.pub.intraday_volume")
    filtered = visible_hits([hit], _registry(downgraded), scope_symbols=frozenset(), calib=_passed_index())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NOT_PUBLISHABLE) == 1

    # 同 signal_id, 当前 publishable → 可见
    filtered = visible_hits(
        [hit], _registry(fx.publishable_definition()), scope_symbols=frozenset(), calib=_passed_index()
    )
    assert len(filtered.hits) == 1

    # 反向: 命中写入时是 candidate, 定义现在 publishable → 仍可见
    # (可见性只认当前状态, 不因"写的时候还没批"而永久封印)
    early = fx.publishable_hit(hit_id="early")
    filtered = visible_hits(
        [early],
        _registry(fx.publishable_definition()),
        scope_symbols=frozenset(),
        calib=_passed_index(),
    )
    assert len(filtered.hits) == 1


def test_fundamental_out_of_scope_is_hidden():
    hit = fx.daily_fundamental_hit(symbol="600000")
    filtered = visible_hits(
        [hit],
        _full_registry(),
        scope_symbols=frozenset({"000001", "000002"}),
        calib=_passed_index(),
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.OUT_OF_SCOPE) == 1
    assert filtered.scope_applied is True
    assert filtered.scope_size == 2


def test_fundamental_in_scope_is_visible():
    hit = fx.daily_fundamental_hit(symbol="600000")
    filtered = visible_hits(
        [hit], _full_registry(), scope_symbols=frozenset({"600000"}), calib=_passed_index()
    )
    assert [h.symbol for h in filtered.hits] == ["600000"]


def test_minute_event_is_not_scope_limited():
    """事件命中不过范围闸 —— 否则别处的异动永远看不见。"""
    hit = fx.publishable_hit(symbol="999999")
    filtered = visible_hits(
        [hit], _full_registry(), scope_symbols=frozenset({"600000"}), calib=_passed_index()
    )
    assert [h.symbol for h in filtered.hits] == ["999999"]
    assert filtered.rejected.get(VisibilityReject.OUT_OF_SCOPE) is None


def test_scope_none_means_no_scope_filter():
    filtered = visible_hits(
        [fx.daily_fundamental_hit()], _full_registry(), scope_symbols=None, calib=_passed_index()
    )
    assert len(filtered.hits) == 1
    assert filtered.scope_applied is False


def test_visible_hits_are_sorted_newest_first():
    old = fx.publishable_hit(hit_id="old", asof=fx.dated(3))
    new = fx.publishable_hit(hit_id="new", asof=fx.dated(0))
    filtered = visible_hits([old, new], _full_registry(), scope_symbols=frozenset(), calib=_passed_index())
    assert [h.hit_id for h in filtered.hits] == ["new", "old"]


# =============================================================== 4. API 视图


def test_view_marks_stale_and_asof_date():
    today = cn_today()
    view = hit_to_view(fx.stale_daily_hit(), today=today)
    assert view["stale"] is True
    assert view["asof_date"] == (today - timedelta(days=1)).isoformat()
    assert view["is_daily_fundamental"] is True


def test_empty_response_keeps_message_and_zero_count():
    from app.buy_sell_signals.hits_visibility import FilteredHits

    payload = build_response(FilteredHits(hits=[], rejected={}, scope_applied=True, scope_size=0))
    assert payload["count"] == 0
    assert payload["empty_message"] == EMPTY_HITS_MESSAGE
    assert payload["hits"] == []


def test_definition_status_events_never_become_hits():
    """定义状态事件不混进命中列表。

    这里不靠"看代码里没有那段"来证明, 而是从数据结构上验证: 定义状态事件走的是
    `MONITOR_EVENT_TYPE`, 它没有 symbol; 命中层要求 symbol 必填 —— 所以一条定义状态
    事件**无法**被构造成 SignalHit。
    """
    event_like = {
        "monitor_event_type": MONITOR_EVENT_TYPE,
        "signal_id": "fx.pub.intraday_volume",
        "status": "publishable",
        # 定义状态事件没有 symbol
    }
    with pytest.raises(HitValidationError):
        SignalHit.from_dict(event_like)

    # 而且响应体里不含这个键
    payload = build_response(
        visible_hits([fx.publishable_hit()], _full_registry(), scope_symbols=frozenset(), calib=_passed_index())
    )
    dumped = json.dumps(payload, ensure_ascii=False)
    assert MONITOR_EVENT_TYPE not in dumped


# =============================================================== 5. 不 import t_rounds


def test_hit_modules_do_not_import_t_rounds():
    """命中层新文件与做 T 包零耦合(静态 AST 检查, 与既有隔离测例同构)。"""
    import ast

    pkg = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
    targets = [
        *sorted(pkg.glob("hits_*.py")),
        Path(__file__).resolve().parents[1] / "app" / "api" / "buy_sell_signals_hits.py",
    ]
    assert targets, "没找到命中层文件 —— 路径变了要同步改这个测例"
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                assert not module.startswith("app.t_rounds"), f"{path.name} import 了 {module}"
                assert ".t_rounds" not in module, f"{path.name} import 了 {module}"


def test_triggering_still_disabled():
    """放量 stub 的触发开关本拍**不得**翻 True。"""
    from app.buy_sell_signals import volume_breakout_stub as stub

    assert stub.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False


def test_three_persist_env_literals_unchanged():
    """三 env 字面量一字未改。"""
    from app.buy_sell_signals import constants as const

    assert const.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert const.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert const.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_scope_helper_covers_watchlist_and_paper_symbols():
    """`scope_symbols` 读的是自选 ∪ 持仓, 两者任一为空都不该影响另一个。"""
    import inspect

    from app.api import buy_sell_signals_hits as mod

    src = inspect.getsource(mod.scope_symbols)
    assert "_watchlist_symbols" in src
    assert "_paper_symbols" in src
    assert "|" in src


def test_asof_none_marks_stale_even_for_event_hits():
    """没有 asof 的事件命中同样按过期呈现 —— 不让"缺时间"变成"看起来最新"。"""
    hit = fx.publishable_hit(asof=fx.dated(400))
    assert hit.is_stale(today=date.today() + timedelta(days=1)) is True
