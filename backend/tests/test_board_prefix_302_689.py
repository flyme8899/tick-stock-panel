"""板块号段口径回归: 创业板 300/301/302, 科创板 688/689。

对齐上游 shy3130/tick-stock-panel #485/#486/#488 与 f5ca4e1。
"""
from __future__ import annotations

import polars as pl

from app.api.overview import _board as overview_board
from app.backtest.matrix import _symbol_in_boards
from app.plugins.fuyao.provider import _price_limit as fuyao_price_limit
from app.services.market_overview_builder import _board as builder_board
from app.strategy.engine import StrategyEngine


def test_matrix_board_filter_includes_302_and_689():
    assert _symbol_in_boards("302132.SZ", ["创业板"])
    assert not _symbol_in_boards("302132.SZ", ["深主板"])
    assert _symbol_in_boards("689009.SH", ["科创板"])
    assert not _symbol_in_boards("689009.SH", ["沪主板"])


def _df() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH", "300750.SZ", "302132.SZ", "688001.SH", "689009.SH"],
        "close": [10.0] * 5,
    })


def test_engine_board_filter_includes_302_and_689():
    df = _df()
    star = df.filter(StrategyEngine._basic_filter_expr(df, {"boards": ["科创板"]}))
    assert star["symbol"].to_list() == ["688001.SH", "689009.SH"]
    gem = df.filter(StrategyEngine._basic_filter_expr(df, {"boards": ["创业板"]}))
    assert gem["symbol"].to_list() == ["300750.SZ", "302132.SZ"]


def test_overview_board_labels():
    for fn in (overview_board, builder_board):
        assert fn("302132.SZ") == "创业板"
        assert fn("689009.SH") == "科创板"


def test_fuyao_price_limit():
    assert fuyao_price_limit("302132.SZ") == 0.20
    assert fuyao_price_limit("689009.SH") == 0.20
