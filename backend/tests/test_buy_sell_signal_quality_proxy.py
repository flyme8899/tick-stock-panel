"""日线质量代理测试 + 估值/健康桩测试 (批次 20261024g 任务②)。

质量代理最要紧的三条:

1. `announce_date > asof` 的财务数据**不可知** —— 公告前指标必须为 None
2. 公告前是 `None`, **不是 0** —— 0 是一颗真实数字, None 才是"不知道"
3. 缺数据 → `q_missing_flag`, 不靠 0 或前值顶上

估值 / 健康桩: 必须返回 pending 标记, 且文案里不许出现"已测通"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from app.buy_sell_signals import quality_proxy as qp
from app.buy_sell_signals.constants import INDUSTRY_STANDARD_THS_CURRENT, INDUSTRY_STANDARD_SW_L1
from app.buy_sell_signals.health_veto_stub import (
    HEALTH_RULE_VERSION_PENDING,
    HealthVetoStub,
    health_veto_asof,
)
from app.buy_sell_signals.quality_proxy import (
    QUALITY_METRIC_COLUMNS,
    QUALITY_WEIGHT_STATUS,
    evaluate_quality,
    evaluate_quality_many,
    financials_asof,
)
from app.buy_sell_signals.valuation_stub import ValuationStub, valuation_asof

CN = timezone(timedelta(hours=8))
PKG = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"


# --------------------------------------------------------------------------- 夹具


def _financials(rows: list[dict]) -> pl.DataFrame:
    """构造 financials_metrics 形状的小表(只含本模块用到的列)。"""
    base = {col: None for col in QUALITY_METRIC_COLUMNS}
    base.update({"symbol": None, "period_end": None, "announce_date": None})
    filled = [{**base, **row} for row in rows]
    return pl.DataFrame(filled).with_columns(
        pl.col("period_end").cast(pl.Date),
        pl.col("announce_date").cast(pl.Date),
        *[pl.col(c).cast(pl.Float64) for c in QUALITY_METRIC_COLUMNS],
    )


def _kline(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": pl.Series([r[1] for r in rows], dtype=pl.Date),
            "close": [r[2] for r in rows],
        }
    )


HEALTHY = dict(roe=18.0, gross_margin=42.0, debt_to_asset_ratio=35.0, net_income_yoy=22.0, revenue_yoy=15.0, eps=1.8, bps=9.5)
ANNOUNCE = date(2024, 10, 28)


@pytest.fixture()
def fin() -> pl.DataFrame:
    """两期财务: 2024Q2 已于 08-20 公告, 2024Q3 直到 10-28 才公告。"""
    return _financials(
        [
            {"symbol": "600000.SH", "period_end": date(2024, 6, 30), "announce_date": date(2024, 8, 20),
             "roe": 12.0, "gross_margin": 30.0, "debt_to_asset_ratio": 50.0, "net_income_yoy": 8.0,
             "revenue_yoy": 5.0, "eps": 1.2, "bps": 8.0},
            {"symbol": "600000.SH", "period_end": date(2024, 9, 30), "announce_date": ANNOUNCE, **HEALTHY},
        ]
    )


def _kline_for(symbol: str, start: date, days: int = 25) -> pl.DataFrame:
    return _kline(
        [(symbol, start + timedelta(days=i), 10.0 + i * 0.1) for i in range(days)]
    )


# =========================================================== 财务 PIT


def test_financials_asof_excludes_unannounced_period(fin):
    """公告日之前, 只能用上一期; 用 period_end 判会提前偷看。"""
    known = financials_asof(fin, "600000.SH", date(2024, 9, 1))
    assert known.height == 1
    assert known["period_end"][0] == date(2024, 6, 30)
    assert known["announce_date"][0] == date(2024, 8, 20)


def test_financials_asof_switches_exactly_on_announce_date(fin):
    before = financials_asof(fin, "600000.SH", ANNOUNCE - timedelta(days=1))
    on_day = financials_asof(fin, "600000.SH", ANNOUNCE)
    assert before["period_end"][0] == date(2024, 6, 30)
    assert on_day["period_end"][0] == date(2024, 9, 30)


def test_financials_asof_null_announce_date_is_excluded():
    """不知道什么时候公告的数据, 无法证明当时已知 —— 不参与 PIT。"""
    frame = _financials(
        [
            {"symbol": "600000.SH", "period_end": date(2024, 6, 30), "announce_date": None, "roe": 30.0},
        ]
    )
    assert financials_asof(frame, "600000.SH", date(2024, 12, 31)).is_empty()


def test_financials_asof_without_announce_column_returns_empty():
    frame = pl.DataFrame({"symbol": ["600000.SH"], "period_end": [date(2024, 6, 30)], "roe": [30.0]})
    assert financials_asof(frame, "600000.SH", date(2024, 12, 31)).is_empty()


def test_financials_asof_string_dates(fin):
    as_str = fin.with_columns(
        pl.col("announce_date").cast(pl.Utf8), pl.col("period_end").cast(pl.Utf8)
    )
    known = financials_asof(as_str, "600000.SH", date(2024, 9, 1))
    assert known.height == 1 and known["period_end"][0] == date(2024, 6, 30)


def test_financials_asof_unknown_symbol_returns_empty(fin):
    assert financials_asof(fin, "000002.SZ", date(2024, 12, 31)).is_empty()


# =========================================================== 公告前必须 None, 不是 0


def test_metrics_are_null_before_any_announcement(fin):
    """asof 早于第一次公告 → 全部指标 None, 且 q_missing_flag。"""
    result = evaluate_quality(
        "600000.SH", date(2024, 1, 5), financials=fin, kline=_kline_for("600000.SH", date(2024, 1, 1))
    )
    assert all(value is None for value in result.metrics.values()), "公告前不得填 0"
    assert result.q_score is None
    assert result.score_emitted is False
    assert result.q_missing_flag is True
    assert "no_financials_known_at_asof" in result.q_missing_reasons


def test_metrics_come_from_older_period_before_new_announcement(fin):
    """新一期已过报告期但未公告: 必须还是用旧那一期, 不能"看起来更新"。"""
    result = evaluate_quality(
        "600000.SH", date(2024, 10, 1), financials=fin, kline=_kline_for("600000.SH", date(2024, 9, 1))
    )
    assert result.metrics_asof_announce == date(2024, 8, 20)
    assert result.metrics_period_end == date(2024, 6, 30)
    assert result.metrics["roe"] == 12.0


def test_metrics_switch_after_announcement(fin):
    result = evaluate_quality(
        "600000.SH", date(2024, 11, 1), financials=fin, kline=_kline_for("600000.SH", date(2024, 10, 1))
    )
    assert result.metrics_asof_announce == ANNOUNCE
    assert result.metrics["roe"] == 18.0
    assert result.metrics["gross_margin"] == 42.0


def test_null_metric_stays_null_and_is_reported(fin):
    """单列为 null 时保持 null, 并给出 null_metric:<列名>, 不用 0 补。"""
    partial = fin.with_columns(
        pl.when(pl.col("period_end") == date(2024, 9, 30)).then(None).otherwise(pl.col("gross_margin")).alias("gross_margin")
    )
    result = evaluate_quality(
        "600000.SH", date(2024, 11, 1), financials=partial, kline=_kline_for("600000.SH", date(2024, 10, 1))
    )
    assert result.metrics["gross_margin"] is None
    assert "null_metric:gross_margin" in result.q_missing_reasons


# =========================================================== 缺数据 → q_missing_flag


def test_missing_financials_entirely():
    result = evaluate_quality(
        "600000.SH", date(2024, 11, 1), financials=pl.DataFrame(), kline=_kline_for("600000.SH", date(2024, 10, 1))
    )
    assert result.q_missing_flag is True
    assert result.q_score is None
    assert "no_financials_known_at_asof" in result.q_missing_reasons


def test_missing_kline_source():
    result = evaluate_quality("600000.SH", date(2024, 11, 1), financials=None, kline=None)
    assert "no_kline_source" in result.q_missing_reasons
    assert result.price_coverage is None


def test_symbol_absent_from_kline():
    other = _kline_for("000002.SZ", date(2024, 10, 1))
    result = evaluate_quality("600000.SH", date(2024, 11, 1), financials=None, kline=other)
    assert "no_kline_rows" in result.q_missing_reasons
    assert result.price_coverage == 0.0


def test_healthy_fixture_emits_score(fin):
    """数据齐全时才产分。"""
    result = evaluate_quality(
        "600000.SH", date(2024, 11, 1), financials=fin, kline=_kline_for("600000.SH", date(2024, 10, 1))
    )
    assert result.q_score is not None
    assert result.score_emitted is True
    assert result.q_missing_flag is False
    assert 0 <= result.q_score <= len(QUALITY_METRIC_COLUMNS)
    assert result.last_close is not None


def test_low_price_coverage_flagged():
    """窗口内只有零星几天有价 → 覆盖率低。

    这里刻意让空洞落在窗口中间: 若分母用"出现过的行数", 3/3 就是 100%, 一只长期
    停牌的票会被判成健康 —— 这正是分母要按工作日跨度算的原因。

    分母固定 20 个自然日(约 14–15 个工作日), 3 行 → 覆盖率约 0.2, 落在
    `MIN_PRICE_COVERAGE=0.6` 之下, 但**不为 0**, 所以走的是"覆盖率低"这条分支。
    """
    rows = [
        ("600000.SH", date(2024, 10, 25) + timedelta(days=i), 10.0)
        for i in range(3)
    ]
    result = evaluate_quality("600000.SH", date(2024, 11, 1), financials=None, kline=_kline(rows))
    assert result.price_coverage is not None
    assert 0 < result.price_coverage < qp.MIN_PRICE_COVERAGE
    assert any(r.startswith("kline_coverage_low") for r in result.q_missing_reasons)


def test_all_prices_outside_window_is_zero_coverage():
    """窗口内一个价都没有 → no_kline_rows(不是 coverage_low)。"""
    rows = [("600000.SH", date(2024, 1, 2) + timedelta(days=i), 10.0) for i in range(3)]
    result = evaluate_quality("600000.SH", date(2024, 11, 1), financials=None, kline=_kline(rows))
    assert result.price_coverage == 0.0
    assert "no_kline_rows" in result.q_missing_reasons
    assert result.last_close is None


def test_weights_stay_design_pending(fin):
    """本批次不定权重。"""
    result = evaluate_quality("600000.SH", date(2024, 11, 1), financials=fin, kline=None)
    assert result.w_quality is None
    assert result.weighting_status == QUALITY_WEIGHT_STATUS == "design_pending"


def test_quality_does_not_invent_new_factors():
    """只用已有列, 不新造因子。"""
    allowed = {"symbol", "period_end", "announce_date", *QUALITY_METRIC_COLUMNS}
    from app.services import picker

    for col in QUALITY_METRIC_COLUMNS:
        assert col in picker._FINANCIAL_COLUMNS, f"{col} 不在现有财务列白名单里"
    assert set(QUALITY_METRIC_COLUMNS) <= allowed


def test_many_matches_single(fin):
    symbols = ["600000.SH", "000002.SZ"]
    many = evaluate_quality_many(symbols, date(2024, 11, 1), financials=fin, kline=None)
    for sym in symbols:
        assert many[sym] == evaluate_quality(sym, date(2024, 11, 1), financials=fin, kline=None)


def test_future_kline_rows_ignored():
    """asof 之后的行情不得参与 —— 否则收盘价也是未来信息。"""
    rows = [("600000.SH", date(2024, 11, 1) - timedelta(days=i), 10.0 + i) for i in range(20)]
    rows += [("600000.SH", date(2024, 11, 2), 999.0)]
    result = evaluate_quality("600000.SH", date(2024, 11, 1), financials=None, kline=_kline(rows))
    assert result.last_close != 999.0


def test_serialized_quality_shape(fin):
    payload = evaluate_quality(
        "600000.SH", date(2024, 11, 1), financials=fin, kline=_kline_for("600000.SH", date(2024, 10, 1))
    ).to_dict()
    assert set(payload) >= {
        "symbol", "asof", "q_score", "score_emitted", "q_missing_flag",
        "q_missing_reasons", "metrics", "weighting_status",
    }
    assert payload["score_emitted"] is True


def test_loaders_are_read_only(tmp_path: Path):
    """读盘适配层不得创建目录 / 写文件。

    路径不存在时 `load_kline_daily` 会抛 `FileNotFoundError`(polars 的行为), 这里
    接受"抛错"或"返回空表"两种结果 —— 重点是不产生副作用。
    """
    assert qp.load_financials(tmp_path).is_empty()
    try:
        qp.load_kline_daily(str(tmp_path / "nope"))
    except FileNotFoundError:
        pass
    assert list(tmp_path.iterdir()) == []


# =========================================================== 估值桩


def test_valuation_returns_pending():
    stub = valuation_asof("600000.SH", date(2024, 11, 1))
    assert isinstance(stub, ValuationStub)
    assert stub.data_pending is True
    assert stub.v_score is None
    assert stub.status == "data_pending"
    assert stub.missing_inputs


def test_valuation_states_it_is_untested():
    stub = valuation_asof("600000.SH", date(2024, 11, 1))
    assert stub.tested_status == "untested"
    payload = stub.to_dict()
    assert payload["v_score"] is None
    assert payload["data_pending"] is True


def test_valuation_module_docstring_says_untested():
    text = (PKG / "valuation_stub.py").read_text(encoding="utf-8")
    assert "未测通" in text
    assert "不构成投资建议" in text


def test_valuation_never_uses_snapshot_pe():
    """不得从 financials_metrics 的快照 pe 列凑估值。"""
    text = (PKG / "valuation_stub.py").read_text(encoding="utf-8")
    body = text.split('"""')[2] if text.count('"""') >= 2 else text
    assert "pe_ttm" not in body
    assert "financials_metrics" not in body


# =========================================================== 健康桩


def test_health_returns_design_pending():
    stub = health_veto_asof("600000.SH", date(2024, 11, 1))
    assert isinstance(stub, HealthVetoStub)
    assert stub.health_veto_rule_version == HEALTH_RULE_VERSION_PENDING == "design_pending"
    assert stub.health_veto_hit is None, "未查过必须 None, 不能是 False(那是'查过没命中')"
    assert stub.tested_status == "untested"


def test_health_blocks_publish_is_none_not_false():
    """None 表示未知。返回 False 等于向调用方保证"这只票健康"。"""
    stub = health_veto_asof("600000.SH", date(2024, 11, 1))
    assert stub.blocks_publish is None
    assert stub.blocks_publish is not False


def test_health_module_docstring_says_untested():
    text = (PKG / "health_veto_stub.py").read_text(encoding="utf-8")
    assert "未测通" in text
    assert "不构成投资建议" in text


@pytest.mark.parametrize("filename", ["valuation_stub.py", "health_veto_stub.py"])
def test_stub_modules_never_claim_tested(filename):
    """桩模块里不许出现"已测通"这类说法。"""
    text = (PKG / filename).read_text(encoding="utf-8")
    for claim in ("已测通", "测试通过", "已验证"):
        assert claim not in text.replace("不要", ""), f"{filename} 出现 {claim}"


def test_stubs_do_not_import_push_or_minute_capability():
    for filename in ("valuation_stub.py", "health_veto_stub.py", "quality_proxy.py", "sw_pit.py"):
        text = (PKG / filename).read_text(encoding="utf-8")
        for banned in ("news.push", "trade_marks", "minute_capability", "ProCapability"):
            assert banned not in text, f"{filename} 引用了 {banned}"


# =========================================================== 行业口径常量


def test_industry_standard_defaults_to_sw_l1():
    from app.buy_sell_signals import SignalRecord

    rec = SignalRecord(
        signal_id="s", display_name="d", family="composite", event_class="other", spec_ref="x"
    )
    assert rec.industry_standard == INDUSTRY_STANDARD_SW_L1 == "sw_l1"


def test_ths_current_constant_is_not_accepted_as_standard():
    """`ths_current` 只用于在报错里指名道姓, 不是可选口径。"""
    from app.buy_sell_signals import SignalRecord

    assert INDUSTRY_STANDARD_THS_CURRENT != INDUSTRY_STANDARD_SW_L1
    rec = SignalRecord(
        signal_id="s", display_name="d", family="composite", event_class="other", spec_ref="x",
        industry_standard=INDUSTRY_STANDARD_THS_CURRENT,
    )
    # 记录层不禁止写这个值(它只是个字段), 但 sw_pit 不接受该来源 —— 见下一条。
    assert rec.industry_standard == "ths_current"


def test_sw_pit_rejects_ths_by_source_name(tmp_path):
    from app.buy_sell_signals.sw_pit import IndustrySourceRejected, load_sector_tables

    (tmp_path / "ext_hy_ths.parquet").write_bytes(b"")
    with pytest.raises(IndustrySourceRejected):
        load_sector_tables(tmp_path / "ext_hy_ths.parquet", tmp_path / "x.parquet")


# =========================================================== 无竞价回测路径


def test_no_auction_backtest_code_path():
    """本批次不得出现"用开盘 1m 冒充竞价"的实现。"""
    for path in PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for banned in ("auction_backtest", "pre_open_1m", "open_1m_as_auction"):
            assert banned not in text, f"{path.name} 出现 {banned}"
