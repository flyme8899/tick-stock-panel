"""balanced_alpha 行业分散：候选行业来自行业映射表，同一桶不能超过上限。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "daily_stock_analysis"
sys.path.insert(0, str(VENDOR))

from src.services.screening.industry import enrich_industry_concepts  # noqa: E402
from src.services.screening.pipeline import _df_to_picks  # noqa: E402
from src.services.screening.risk import (  # noqa: E402
    apply_portfolio_overlay,
    limit_picks_per_industry,
)
from src.services.screening.strategy import load_strategy  # noqa: E402

_STRATEGY = (
    VENDOR / "src" / "services" / "screening" / "strategies" / "balanced_alpha.yaml"
)


def _profile() -> dict:
    strategy = load_strategy(_STRATEGY)
    profile = strategy.screening.portfolio_profile
    assert strategy.name == "balanced_alpha"
    assert profile["max_same_bucket"] == 1
    return profile


def test_mapping_table_fills_blank_and_mismatched_industry(tmp_path: Path) -> None:
    """快照行业为空或是 Other 时，用同花顺映射表，映射没有的代码保留快照行业。"""
    path = tmp_path / "industry.csv"
    path.write_text(
        "股票代码,所属同花顺行业\n"
        "600000.SH,银行-银行-股份制银行\n"
        "000001.SZ,银行-银行-股份制银行\n",
        encoding="utf-8",
    )
    snapshot = pd.DataFrame([
        {"code": "600000", "name": "浦发银行", "industry": "", "screen_score": 90},
        {"code": "000001", "name": "平安银行", "industry": "Other", "screen_score": 89},
        {"code": "600690", "name": "海尔智家", "industry": "家电", "screen_score": 86},
    ])

    enriched, notes = enrich_industry_concepts(snapshot, map_files=[path])
    picks = _df_to_picks(enriched)
    by_code = {pick.code: pick.industry for pick in picks}

    assert by_code == {
        "600000": "银行-银行-股份制银行",
        "000001": "银行-银行-股份制银行",
        "600690": "家电",
    }
    assert any("industry map loaded" in note for note in notes)


def test_balanced_alpha_industry_cap_holds(tmp_path: Path) -> None:
    """两只银行加一只家电时，均衡多因子的每桶上限只留下一只金融。"""
    path = tmp_path / "industry.csv"
    path.write_text(
        "股票代码,所属同花顺行业\n"
        "600000.SH,银行-银行-股份制银行\n"
        "000001.SZ,银行-银行-股份制银行\n",
        encoding="utf-8",
    )
    snapshot = pd.DataFrame([
        {"code": "600000", "name": "浦发银行", "industry": "", "screen_score": 90},
        {"code": "000001", "name": "平安银行", "industry": "Other", "screen_score": 89},
        {"code": "600690", "name": "海尔智家", "industry": "家电", "screen_score": 86},
    ])
    enriched, _notes = enrich_industry_concepts(snapshot, map_files=[path])
    picks = _df_to_picks(enriched)
    profile = _profile()

    ordered, notes = apply_portfolio_overlay(picks, profile=profile)
    capped = limit_picks_per_industry(ordered, profile=profile)

    assert [pick.code for pick in capped] == ["600000", "600690"]
    assert notes
    assert "金融" in notes[0]
    assert all("银行" in pick.industry or pick.industry == "家电" for pick in capped)
    assert sum("银行" in pick.industry for pick in capped) == 1


def test_candidate_industry_falls_back_to_mapping_column_then_snapshot() -> None:
    """行上已有映射列时直接用它；占位行业不能挡住映射，映射缺失才留快照行业。"""
    frame = pd.DataFrame([
        {
            "code": "600519",
            "name": "贵州茅台",
            "industry": "Other",
            "所属同花顺行业": "食品饮料-白酒-白酒Ⅲ",
            "screen_score": 80,
        },
        {
            "code": "000858",
            "name": "五粮液",
            "industry": "",
            "所属同花顺行业": "食品饮料-白酒-白酒Ⅲ",
            "screen_score": 70,
        },
        {"code": "600690", "name": "海尔智家", "industry": "家电", "screen_score": 60},
        {"code": "300001", "name": "占位", "industry": "其他", "screen_score": 50},
    ])

    picks = _df_to_picks(frame)

    assert [pick.industry for pick in picks] == [
        "食品饮料-白酒-白酒Ⅲ",
        "食品饮料-白酒-白酒Ⅲ",
        "家电",
        "",
    ]
