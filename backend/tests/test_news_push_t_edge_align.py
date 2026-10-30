"""批次 20261030gtedge · P-t-edge-align · 做T「图≠钉」分钟边沿同构。

## 这一组测在测什么

**图有钉无 = 边沿源不同构**, 不是文案缺字、不是阈值不一致。

- 图 = `replay_t_marks`(分钟序列 → `session_signal_sets` → 逐根差分)
- 旧钉 = `_live_rows` 报价快照 @ `INTRADAY_CHECK_S=180`

一分钟级的窗(688008 2026-10-30 10:06 那根 `below_vwap`, 均价 158.56)在两次快照之间
就已经开合完毕 —— 图上留下一个 mark, 钉上什么都没发。

本拍把 live 的边沿源换成与图**同一个** `minute_edge_events` 引擎, 于是「同分钟同 reason」
在结构上成立。下面的测例用**合成 bars**(不依赖真盘 parquet)把这件事钉住。

## 不再重复的老话

合入 ≠ 部署; tip ≠ prod; 文案齐 ≠ 漏触发已修; 建议T ≠ BSS。
`*_TRIGGERING_ENABLED` 与 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` 本拍零碰(仍 `False`);

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp(本拍非回测, 仅旁注)。
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta

import pytest

from app.market_time import CN_TZ
from app.news import push as P
from app.services import trade_marks as M

SYMBOL = "688008.SH"
PREV_CLOSE = 158.56


# ---------------------------------------------------------------- 合成分钟 bars


def _bars(points, *, start=(9, 31), volume=1000.0, symbol=SYMBOL):
    """把 `[(close, high_extra, low_extra), ...]` 编成当日分钟 bars。

    均价与 `t_conditions` 同一口径: `amount / (volume * 100)`(volume 手 → 股)。
    每根的 `amount` 用该根均价 `(o+h+l+c)/4` 记, 于是累计均价落在当根区间里 ——
    这样才可能真的低于/高于均价, 而不是每根都被 `vwap_outside_day_range` 判成脏数据。

    想让某根出现 `below_vwap`, 就让它的 close 明显低于此前累计均价即可。
    """
    hour, minute = start
    moment = datetime(2026, 10, 30, hour, minute)
    bars = []
    for index, point in enumerate(points):
        close, high_extra, low_extra = point
        high = round(close * (1 + high_extra), 4)
        low = round(close * (1 - low_extra), 4)
        opened = round(close * 0.999, 4)
        price = (opened + high + low + close) / 4
        bars.append({
            "symbol": symbol,
            "datetime": moment + timedelta(minutes=index),
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": round(price * volume * 100.0, 4),
        })
    return bars


_FLAT = (PREV_CLOSE, 0.0005, 0.0005)
_DIP = (round(PREV_CLOSE * 0.945, 2), 0.0005, 0.0005)   # 一根深砸 -5.5%
_UP = (round(PREV_CLOSE * 1.03, 2), 0.0005, 0.0005)     # 反弹越过累计均价


def _short_window_bars():
    """类 688008 10:06: 前面平稳, 一根深砸破均价(09:41), 下一根反弹越过均价。

    深砸那根**只有一根** —— 报价快照 180s 一轮, 这一根在两次快照之间就结束了;
    而分钟路径逐根看, 采得到。
    """
    return _bars([_FLAT] * 10 + [_DIP, _UP])


def _fake_repo(bars_by_symbol):
    """假仓库: 只有 `get_minute_batch` / `get_minute`, 帧形态与真仓一致。"""

    class _Frame:
        columns = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

        def __init__(self, rows):
            self._rows = rows

        def is_empty(self):
            return not self._rows

        def sort(self, key):
            return _Frame(sorted(self._rows, key=lambda row: row[key]))

        def to_dicts(self):
            return [dict(row) for row in self._rows]

        def partition_by(self, key, as_dict=False):
            groups = {}
            for row in self._rows:
                groups.setdefault(row[key], []).append(row)
            return {symbol: _Frame(rows) for symbol, rows in groups.items()}

    class _Repo:
        def get_minute_batch(self, symbols, trade_date, asset_type="stock"):
            rows = []
            for symbol in symbols:
                rows.extend(bars_by_symbol.get(symbol) or [])
            return _Frame(rows)

        def get_minute(self, symbol, trade_date, asset_type="stock"):
            return _Frame(bars_by_symbol.get(symbol) or [])

    return _Repo()


@pytest.fixture
def minute_world(monkeypatch, tmp_path):
    """把 live 做T 路径的取数全部换成本地合成数据; 冷却/日封顶保持默认值。"""
    P._T_MINUTE_SEEN.clear()
    P._T_MINUTE_PROBE = None

    def install(bars_by_symbol, *, prev=({"symbol": SYMBOL, "prev_close": PREV_CLOSE},)):
        monkeypatch.setattr("app.news.service._repo", lambda: _fake_repo(bars_by_symbol))
        levels = {item["symbol"]: item for item in prev}
        monkeypatch.setattr(P, "_prior_levels", lambda symbols: {
            symbol: levels[symbol] for symbol in symbols if symbol in levels
        })
        monkeypatch.setattr(P, "_minute_reads_adjusted", lambda repo: False)
        monkeypatch.setattr("app.services.watchlist.list_symbols", lambda: [{"symbol": SYMBOL}])
        monkeypatch.setattr(P, "_flag_on", lambda field: False)

    return install


def _enable_t(monkeypatch, *, cap=20, vwap_min=1, range_min=120):
    monkeypatch.setattr(P, "push_master_enabled", lambda: True)
    monkeypatch.setattr(P, "push_type_enabled", lambda kind: kind == "t_trade")
    monkeypatch.setattr(
        "app.config.settings.dingtalk_webhook_url",
        "https://oapi.dingtalk.com/robot/send?access_token=test",
    )
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "")
    monkeypatch.setattr("app.config.settings.news_push_t_cooldown_min", vwap_min)
    monkeypatch.setattr("app.config.settings.news_push_t_range_cooldown_min", range_min)
    monkeypatch.setattr("app.config.settings.news_push_t_range_once_per_day", "")
    monkeypatch.setattr("app.config.settings.news_push_t_daily_cap", cap)
    # 分钟路径由 `minute_edge_events` 按 bar 根数冷却, 不再走报价路线的 `_filter_range_rules`
    # 的「10:00 前不报 near_high/near_low」分支 —— 那条分支在引擎里已按 `RANGE_QUIET_UNTIL`
    # 同口径实现。这里保持 `_flag_on` 原样(读 settings), 不额外放宽任何值。


# ---------------------------------------------------------------- ① 同构


def test_live_minute_path_matches_replay_t_marks_same_minute_same_reason(minute_world):
    """G-T3: 合成分钟序列上 live 分钟边沿路径与 replay_t_marks **同分钟同 reason**。

    序列里 09:41 那一根深砸(类 688008 10:06 的短窗)。报价快照 180s 一轮采不到它;
    分钟路径必须采到, 且与图的 mark 逐条对齐。
    """
    bars = _short_window_bars()
    minute_world({SYMBOL: bars})

    marks = M.replay_t_marks(bars, symbol=SYMBOL, prev_close=PREV_CLOSE)
    assert marks, "图路径本身应当在这批合成 bars 上找到边沿(否则本测无意义)"
    assert any(mark["rule"] == "低于分时均价" for mark in marks), marks

    live = P._load_t()
    current, _pct, _names, meta = P._split_loader(live)
    edges = sorted(
        (edge["time"], edge["reason"])
        for (_symbol, _reason), edge in meta.items()
    )

    # live 侧把 rule(中文)换回 reason 再比, 两边比的是同一件事。
    label_of = {label: reason for reason, label in P.T_LABELS.items()}
    from_replay = sorted((mark["time"], label_of.get(mark["rule"], mark["rule"])) for mark in marks)
    assert edges == from_replay
    assert current[SYMBOL] == {reason for _time, reason in from_replay}


def test_short_window_edge_is_caught_by_minute_path_not_by_quote_path(minute_world):
    """同一批 bars: 分钟路径采到那根深砸; 报价快照(旧路径)采不到。

    这条测把「图有钉无」的机制直接摆出来 —— 不是阈值不同, 是**采样点**不同。
    """
    bars = _short_window_bars()
    minute_world({SYMBOL: bars})

    live = P._load_t()
    _current, _pct, _names, meta = P._split_loader(live)
    minutes = sorted({edge["time"] for edge in meta.values()})
    assert "09:41" in minutes

    # 旧路径的逻辑: 用「日累计」的单行快照喂 t_conditions。快照取的是最后一次
    # 读数, 而深砸那一根已经被后面的收回覆盖 —— 于是 below_vwap 不成立。
    settled_close = bars[-1]["close"]
    snapshot = {
        "symbol": SYMBOL,
        "close": settled_close,
        "open": bars[0]["open"],
        "high": max(bar["high"] for bar in bars),
        "low": min(bar["low"] for bar in bars),
        "volume": sum(bar["volume"] for bar in bars),
        "amount": sum(bar["amount"] for bar in bars),
        "prev_close": PREV_CLOSE,
    }
    signals, _change = P.t_conditions(snapshot)
    assert "below_vwap" not in signals


# ---------------------------------------------------------------- ② 冷却 / 日封顶


def test_cooldown_and_daily_cap_not_relaxed_by_edge_align(monkeypatch, tmp_path, minute_world):
    """G-T5: `INTRADAY_CHECK_S` / `t_daily_cap` / 冷却值**未放宽**, 行为也不多推。"""
    assert P.INTRADAY_CHECK_S == 180
    assert P.T_CAP == 40
    assert P.VWAP_BAND == 0.015
    assert P.RANGE_BAND == 0.003
    assert P.PREV_BAND == 0.003
    assert P.PREV_AWAY == 0.01
    assert P.RANGE_MIN == 0.01
    assert P.RANGE_QUIET_UNTIL.hour == 10

    _enable_t(monkeypatch, cap=1)
    bars = _short_window_bars()
    minute_world({SYMBOL: bars})
    state = P.PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    moment = datetime(2026, 10, 30, 9, 45, tzinfo=CN_TZ)
    P.tick(moment, state=state, opener=opener, trading=True)
    assert len(sent) == 1
    # 日封顶 = 1: 下一轮同样有边沿也不该再发。
    later = moment + timedelta(minutes=5)
    P.tick(later, state=state, opener=opener, trading=True)
    assert len(sent) == 1
    assert state.data["t_daily_count"] == 1


def test_same_reason_in_cooldown_does_not_push_twice(minute_world, monkeypatch, tmp_path):
    """G-T4: 只推新边沿 —— 同一 reason 在冷却窗内不重复推。"""
    _enable_t(monkeypatch, cap=20, vwap_min=30)
    bars = _short_window_bars()
    minute_world({SYMBOL: bars})
    state = P.PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    moment = datetime(2026, 10, 30, 9, 45, tzinfo=CN_TZ)
    P.tick(moment, state=state, opener=opener, trading=True)
    assert len(sent) == 1
    # 同一批 bars 再跑一轮: 那一根已被处理过, 不该重复推。
    P.tick(moment + timedelta(minutes=3), state=state, opener=opener, trading=True)
    assert len(sent) == 1


# ---------------------------------------------------------------- ③ 非仅缩轮询 / 文案 / 三 env


def test_not_only_shrink_intraday_check_and_copy_env_regression():
    """G-T6/G-T7/G-T8: 分钟同构主路径在; 文案三件套在; 不靠缩 `INTRADAY_CHECK_S`。"""
    source = inspect.getsource(P)
    assert "minute_edge_events" in source
    assert "session_signal_sets(" in source
    assert "_minute_bars_for_symbols" in source
    assert P.INTRADAY_CHECK_S == 180

    assert "建议T买入" in source
    assert "触发时刻" in source
    assert hasattr(P, "_t_side_label")
    assert hasattr(P, "_fmt_trigger_ts")

    import os
    for name in ("BUY_SELL_SIGNALS_PERSIST", "PUSH", "DINGTALK"):
        assert name not in source or os.environ.get(name) is None


def test_first_bar_does_not_fire_aligned_with_replay():
    """对齐 `replay_t_marks` 的「第一根不触发」语义, 且 live 走的是同一个引擎。"""
    bars = _bars([_DIP, _UP, _FLAT])  # 第一根就深砸
    marks = M.replay_t_marks(bars, symbol=SYMBOL, prev_close=PREV_CLOSE)
    assert marks == []

    events = M.minute_edge_events(bars, symbol=SYMBOL, prev_close=PREV_CLOSE)
    assert events == []


def test_live_and_chart_share_one_engine_not_two_rules():
    """G-T2/G-T3 的结构性保证: 图与 live 只有一份边沿规则。"""
    replay_src = inspect.getsource(M.replay_t_marks)
    assert "minute_edge_events" in replay_src
    load_src = inspect.getsource(P._load_t)
    assert "minute_edge_events" in load_src
    # 引擎里必须复用 push 的 session_signal_sets, 不许另建一套阈值。
    engine_src = inspect.getsource(M.minute_edge_events)
    assert "session_signal_sets" in engine_src
    assert "VWAP_BAND" in engine_src


def test_fail_closed_when_minute_data_missing(monkeypatch, minute_world):
    """N6: 空帧 / 只有一根 / 缺昨收 → 该标的本轮不判, 不造假 bar, 不抛。"""
    minute_world({SYMBOL: []})
    assert P._load_t() == ({}, {}, {})

    minute_world({SYMBOL: _bars([_FLAT])})
    assert P._load_t() == ({}, {}, {})

    minute_world({SYMBOL: _short_window_bars()}, prev=())
    assert P._load_t() == ({}, {}, {})


def test_minute_universe_uses_same_t_cap(monkeypatch, minute_world):
    """取数口径未放宽: 仍是 `t_universe` + `T_CAP`, 不因换边沿源而扩宇宙。"""
    seen = {}
    minute_world({SYMBOL: _short_window_bars()})
    monkeypatch.setattr(P, "_T_MINUTE_PROBE", lambda symbols: seen.update(symbols=list(symbols)))
    P._load_t()
    assert seen["symbols"] == [SYMBOL]


def test_triggering_and_complete_flags_still_false():
    """G-T8: 零碰 —— 两处 TRIGGERING 与 A2 COMPLETE 仍 `False`。"""
    from app.buy_sell_signals import auction_abnormal_stub, volume_breakout_stub
    from app.buy_sell_signals.first_seen_audit import EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE

    assert auction_abnormal_stub.AUCTION_ABNORMAL_TRIGGERING_ENABLED is False
    assert volume_breakout_stub.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False


def test_edge_price_used_as_copy_fallback(minute_world, monkeypatch, tmp_path):
    """文案仍只打现价; 报价取不到时用边沿那根 bar 的价格垫底(不留空、不写 0.00)。"""
    _enable_t(monkeypatch)
    minute_world({SYMBOL: _short_window_bars()})
    monkeypatch.setattr(P, "_live_quote_snapshot", lambda symbols: {})
    state = P.PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    P.tick(datetime(2026, 10, 30, 9, 45, tzinfo=CN_TZ), state=state, opener=opener, trading=True)
    assert sent, "边沿存在却没推, 说明分钟路径没接上"
    text = sent[0]["markdown"]["text"]
    assert "现价" in text
    assert "0.00" not in text
