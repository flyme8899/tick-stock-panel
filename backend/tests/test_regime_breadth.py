"""市场宽度(R3_breadth) 风控提醒测试。

覆盖任务书要求的四类断言:
- 阈值边界: classify_breadth 在 q1/q2 两侧的档位归属(下闭语义)
- 毛刺剔除: _aggregate_daily 的涨跌家数剔除 |日收益| > 25% 的个股
- 无未来数据: compute_breadth_series 只用"当日及之前"的比值, 窗口不足不产信号
- 序列/端口径: compute_breadth_latest 只回最近 N 天, latest 取最近有效值

阈值冻结为研究前段分位 (1.53761, 1.67424), 上线后不得用全样本重估 —— 断言常量本身,
一旦有人改动会立即失败, 起到"阈值不可悄悄漂移"的守护作用。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.services import regime_builder


# ───────────────────────── 阈值边界 ─────────────────────────


def test_thresholds_frozen():
    """阈值与研究前段分位一致, 冻结不可漂移。"""
    assert regime_builder.BREADTH_THRESHOLDS == (1.53761, 1.67424)
    assert regime_builder.BREADTH_WINDOW == 20
    assert regime_builder.BREADTH_OUTLIER == 0.25
    assert regime_builder.BREADTH_POS == {"low": 0.3, "mid": 0.6, "high": 1.0}


def test_classify_breadth_boundaries():
    """档位归属: ≤q1 → low; q1<v≤q2 → mid; v>q2 → high; None → None。"""
    q1, q2 = regime_builder.BREADTH_THRESHOLDS
    assert regime_builder.classify_breadth(q1 - 0.01) == "low"
    assert regime_builder.classify_breadth(q1) == "low"          # 下闭
    assert regime_builder.classify_breadth(q1 + 1e-9) == "mid"
    assert regime_builder.classify_breadth(q2) == "mid"          # 下闭
    assert regime_builder.classify_breadth(q2 + 1e-9) == "high"
    assert regime_builder.classify_breadth(None) is None


def test_suggested_pos_mapping():
    """三档建议仓位固定为 0.3/0.6/1.0, 且仅作提醒语义。"""
    assert regime_builder.BREADTH_POS["low"] == 0.3
    assert regime_builder.BREADTH_POS["mid"] == 0.6
    assert regime_builder.BREADTH_POS["high"] == 1.0


# ───────────────────────── 毛刺剔除 ─────────────────────────


def _enriched_day(day: date, change_pcts: list[float]) -> pl.DataFrame:
    n = len(change_pcts)
    return pl.DataFrame(
        {
            "date": [day] * n,
            "symbol": [f"S{i}" for i in range(n)],
            "close": [10.0] * n,
            "change_pct": change_pcts,
            "amount": [1e8] * n,
            "ma20": [10.0] * n,
            "signal_limit_up": [False] * n,
            "signal_limit_down": [False] * n,
            "signal_broken_limit_up": [False] * n,
            "consecutive_limit_ups": [0] * n,
        }
    )


def test_aggregate_excludes_outliers():
    """|日收益| > 25% 的个股不计入涨/跌家数(与研究 v_breadth 同口径)。"""
    df = _enriched_day(
        date(2026, 1, 5),
        [0.10, -0.08, 0.90, -0.80, 0.02],  # 第 3/4 个为毛刺, 应剔除
    )
    r = regime_builder._aggregate_daily(df).row(0, named=True)
    # 有效: +0.10 / -0.08 / +0.02 → up=2, down=1
    assert r["up_count"] == 2
    assert r["down_count"] == 1


def test_aggregate_keeps_boundary_values():
    """恰在 ±25% 边界内的计入(边界取"闭", 与研究 -0.25<r<0.25 略有差异但无关档位)。"""
    df = _enriched_day(date(2026, 1, 5), [0.25, -0.25, 0.30])
    r = regime_builder._aggregate_daily(df).row(0, named=True)
    # 前两个 |r| == 0.25 计入, +0.30 剔除 → up=1, down=1
    assert r["up_count"] == 1
    assert r["down_count"] == 1


def test_daily_ratio_floor_denominator():
    """下跌家数为 0 时分母取下限 1, 避免除零/无穷。"""
    assert regime_builder.daily_breadth_ratio(5, 0) == 5.0
    assert regime_builder.daily_breadth_ratio(0, 4) == 0.0
    assert regime_builder.daily_breadth_ratio(None, None) is None


# ───────────────────────── 无未来数据 / 窗口 ─────────────────────────


def _rows_constant(n: int, up: int = 3, down: int = 1) -> list[dict]:
    start = date(2026, 1, 1)
    return [
        {"date": start + timedelta(days=i), "up_count": up, "down_count": down}
        for i in range(n)
    ]


def test_no_future_data_window_fill():
    """窗口不足 20 日不产信号; 第 20 日才出现 v_breadth(等于当日及之前均值)。"""
    rows = _rows_constant(25, up=3, down=1)
    series = regime_builder.compute_breadth_series(rows)
    assert len(series) == 25
    assert all(s["v_breadth"] is None for s in series[:19])
    assert series[18]["v_breadth"] is None
    assert series[19]["v_breadth"] == pytest.approx(3.0)
    assert series[24]["v_breadth"] == pytest.approx(3.0)


def test_window_ignores_future_days():
    """改变"未来"某日的 up/down 不影响更早日的 v_breadth(无未来数据)。"""
    base = _rows_constant(25, up=2, down=1)
    mutated = [dict(r) for r in base]
    mutated[24] = {**mutated[24], "up_count": 999, "down_count": 1}
    s1 = regime_builder.compute_breadth_series(base)
    s2 = regime_builder.compute_breadth_series(mutated)
    # 第 20~24 日窗口不含最后一天 → 第 20 日(索引 19)结果不变
    assert s1[19]["v_breadth"] == s2[19]["v_breadth"]
    # 最后一天才受影响
    assert s1[24]["v_breadth"] != s2[24]["v_breadth"]


def test_bucket_in_series():
    """序列里每项带 bucket / suggested_pos, 且与 classify_breadth 一致。"""
    rows = _rows_constant(25, up=3, down=1)  # v_breadth=3.0 > q2 → high
    series = regime_builder.compute_breadth_series(rows)
    last = series[-1]
    assert last["bucket"] == "high"
    assert last["suggested_pos"] == 1.0


# ───────────────────────── latest 辅助 ─────────────────────────


def test_compute_breadth_latest_uses_regime_history(tmp_path, monkeypatch):
    """compute_breadth_latest 从 regime 时序读取并回最近 N 天, latest 取最近有效值。"""
    n = 30
    start = date(2026, 1, 1)
    frame = pl.DataFrame(
        {
            "date": [start + timedelta(days=i) for i in range(n)],
            "up_count": [2] * n,
            "down_count": [1] * n,
        }
    )
    # 通过 monkeypatch 让 load_regime_history 返回构造表(避免落盘)
    monkeypatch.setattr(regime_builder, "load_regime_history", lambda _dd: frame)
    out = regime_builder.compute_breadth_latest(tmp_path, days=5)
    assert out["latest"] is not None
    assert out["latest"]["v_breadth"] == pytest.approx(2.0)
    assert set(out["latest"]) == {"date", "v_breadth", "bucket", "suggested_pos"}
    assert len(out["series"]) == 5
    assert out["threshold_source"] == regime_builder.BREADTH_THRESHOLD_SOURCE
    # 档位没有切换，不在确认期。null 而不是 0。
    assert out["confirm_days"] is None


def test_compute_breadth_latest_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(regime_builder, "load_regime_history", lambda _dd: pl.DataFrame())
    out = regime_builder.compute_breadth_latest(tmp_path, days=5)
    assert out["latest"] is None
    assert out["series"] == []
    assert out["confirm_days"] is None
    # 调用方会改返回值（时序接口 pop confirm_days）。下一次仍是干净的空结果。
    out.pop("confirm_days")
    out["series"].append({"date": "x"})
    again = regime_builder.compute_breadth_latest(tmp_path, days=5)
    assert again["series"] == []
    assert again["confirm_days"] is None


# ───────────────────────── 档位确认中（只展示） ─────────────────────────
# confirm_days 为 1 或 2 时横幅加注「第 n/3 天」；满 3 个交易日或切回原档位为 None。
# None 表示不在确认期，故意不用 0。


def test_confirm_days_constant_is_three_trading_days():
    assert regime_builder.BREADTH_CONFIRM_DAYS == 3


def test_confirm_days_first_day_of_switch():
    """首日切换：新档位第 1 天，标注第 1/3 天。"""
    assert regime_builder.breadth_confirm_days(["high", "high", "high", "low"]) == 1
    # 窗口不足的 None 不计入、不打断原档位。
    assert regime_builder.breadth_confirm_days([None, None, "mid", "mid", "mid", "high"]) == 1


def test_confirm_days_second_of_three():
    """第 2 天仍不满 3 个交易日，标注第 2/3 天。"""
    assert regime_builder.breadth_confirm_days(["high", "high", "high", "low", "low"]) == 2


def test_confirm_days_cleared_after_three_consecutive_days():
    """连续满 3 个交易日当天起去掉标注；之后继续停留也不再标注。"""
    assert regime_builder.breadth_confirm_days(
        ["high", "high", "high", "low", "low", "low"]
    ) is None
    assert regime_builder.breadth_confirm_days(
        ["high", "high", "high", "low", "low", "low", "low"]
    ) is None


def test_confirm_days_cleared_when_switching_back_to_original():
    """确认期内切回原档位，即使与前一交易日不同，也去掉标注。"""
    assert regime_builder.breadth_confirm_days(["high", "high", "high", "low", "high"]) is None
    assert regime_builder.breadth_confirm_days(
        ["high", "high", "high", "low", "low", "high"]
    ) is None
    # 未确认的第三档不把原档位洗掉：回到原档位仍然去掉标注。
    assert regime_builder.breadth_confirm_days(
        ["low", "low", "low", "mid", "high", "low"]
    ) is None


def test_confirm_days_third_tier_restarts_at_day_one():
    """确认期内改切到另一新档，从第 1 天重计，而不是延续上一档的天数。"""
    assert regime_builder.breadth_confirm_days(["low", "low", "low", "mid", "high"]) == 1


def _ratio_frame(ratios: list[float]) -> pl.DataFrame:
    """日涨跌家数比 → regime 行。ratio = up/down，down 固定 1000 以免取整误差。"""
    start = date(2024, 1, 2)
    return pl.DataFrame(
        {
            "date": [start + timedelta(days=i) for i in range(len(ratios))],
            "up_count": [round(ratio * 1000) for ratio in ratios],
            "down_count": [1000] * len(ratios),
        }
    )


def _latest_from_ratios(tmp_path, monkeypatch, ratios: list[float]) -> dict:
    monkeypatch.setattr(
        regime_builder, "load_regime_history", lambda _dd: _ratio_frame(ratios)
    )
    return regime_builder.compute_breadth_latest(tmp_path, days=1)


def test_confirm_days_from_classified_history(tmp_path, monkeypatch):
    """用 classify_breadth 的真实档位序列验证确认天数，且不改 bucket / suggested_pos。

    40 日比值 2.0 使 20 日均值稳定在高档。随后每日比值 1.0：
    第 7 日均值落入中档（确认第 1 天），第 8 日仍是中档（第 2 天），
    第 9 日中档满 3 天，标注去掉。建议仓位仍按当天档位。
    """
    high = [2.0] * 40

    first = _latest_from_ratios(tmp_path, monkeypatch, high + [1.0] * 7)
    # days=1 只回最后一天，确认天数仍要看到更早的原档位。
    assert len(first["series"]) == 1
    assert first["latest"]["bucket"] == "mid"
    assert first["latest"]["suggested_pos"] == regime_builder.BREADTH_POS["mid"]
    assert first["latest"]["bucket"] == regime_builder.classify_breadth(first["latest"]["v_breadth"])
    assert set(first["latest"]) == {"date", "v_breadth", "bucket", "suggested_pos"}
    assert first["confirm_days"] == 1

    second = _latest_from_ratios(tmp_path, monkeypatch, high + [1.0] * 8)
    assert second["latest"]["bucket"] == "mid"
    assert second["latest"]["suggested_pos"] == 0.6
    assert second["confirm_days"] == 2

    third = _latest_from_ratios(tmp_path, monkeypatch, high + [1.0] * 9)
    assert third["latest"]["bucket"] == "mid"
    assert third["latest"]["suggested_pos"] == 0.6
    assert third["confirm_days"] is None


def test_confirm_days_switch_back_uses_classified_buckets(tmp_path, monkeypatch):
    """中档确认第 1 天后，20 日均值回到高档（原档位），confirm_days 为 null。"""
    ratios = [2.0] * 40 + [1.0] * 7 + [3.0]
    out = _latest_from_ratios(tmp_path, monkeypatch, ratios)
    series = regime_builder.compute_breadth_series(_ratio_frame(ratios).to_dicts())
    assert [row["bucket"] for row in series[-2:]] == ["mid", "high"]
    assert out["latest"]["bucket"] == "high"
    assert out["latest"]["suggested_pos"] == 1.0
    assert out["latest"]["suggested_pos"] == regime_builder.BREADTH_POS["high"]
    assert out["confirm_days"] is None


def test_breadth_latest_endpoint_adds_only_confirm_days(monkeypatch):
    """/breadth/latest 只新增 confirm_days；latest 内的档位字段保持原样。"""
    from app.api import regime as regime_api

    payload = {
        "latest": {
            "date": "2026-04-10",
            "v_breadth": 1.4,
            "bucket": "low",
            "suggested_pos": 0.3,
        },
        "series": [],
        "threshold_source": "研究前段(≤2026-04-01)分位 · 冻结",
        "confirm_days": 1,
    }
    monkeypatch.setattr(regime_api, "_data_dir", lambda _request: None)
    monkeypatch.setattr(
        regime_builder,
        "compute_breadth_latest",
        lambda _dd, days=60: {**payload, "latest": dict(payload["latest"])},
    )

    out = regime_api.regime_breadth_latest(None)  # type: ignore[arg-type]
    assert set(out) == {"latest", "note", "threshold_source", "confirm_days"}
    assert out["confirm_days"] == 1
    assert out["latest"]["bucket"] == "low"
    assert out["latest"]["suggested_pos"] == 0.3
    assert "confirm_days" not in out["latest"]

    series = regime_api.regime_breadth(None, days=60)  # type: ignore[arg-type]
    assert "confirm_days" not in series
    assert series["latest"]["bucket"] == "low"
    assert series["note"] == "研究备注 · 非交易信号"
