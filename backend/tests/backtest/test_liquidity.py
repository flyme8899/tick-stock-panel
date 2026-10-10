"""成交量参与率与一字板判定的边界。"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.backtest.liquidity import (
    is_one_price_limit,
    is_volume_halt,
    normalize_volume_limit,
    participation_fill,
    prices_flat,
    release_one_price_boards,
    uses_prior_bar_volume,
    volume_near_zero,
)


def test_volume_limit_off_for_none_and_zero() -> None:
    assert normalize_volume_limit(None) is None
    assert normalize_volume_limit(0) is None
    assert normalize_volume_limit(0.0) is None
    assert normalize_volume_limit(0.1) == pytest.approx(0.1)
    assert normalize_volume_limit(1) == 1.0


@pytest.mark.parametrize("bad", [1.01, -0.1, math.nan, math.inf, "nope"])
def test_volume_limit_rejects_out_of_range(bad) -> None:
    with pytest.raises(ValueError):
        normalize_volume_limit(bad)


def test_participation_off_returns_requested_shares() -> None:
    assert participation_fill(2500, 1.0, None) == ("off", 2500)


def test_participation_partial_buy_rounds_down_to_lots() -> None:
    # 20 手 * 50% = 10 手 = 1000 股; 请求 2500 股只成交 1000。
    assert participation_fill(2500, 20, 0.5) == ("partial", 1000)


def test_participation_full_when_bar_can_absorb_order() -> None:
    assert participation_fill(1000, 20, 0.5) == ("full", 1000)


def test_participation_none_below_one_lot() -> None:
    assert participation_fill(5000, 1, 0.5) == ("none", 0.0)
    assert participation_fill(100, 0, 0.2) == ("none", 0.0)


def test_participation_fail_closed_on_missing_volume() -> None:
    assert participation_fill(1000, math.nan, 0.1) == ("none", 0.0)
    assert participation_fill(1000, -1, 0.1) == ("none", 0.0)


def test_odd_lot_fills_when_bar_has_one_lot() -> None:
    assert participation_fill(40, 3, 0.5) == ("full", 40)


def test_flat_and_near_zero_volume() -> None:
    assert prices_flat((11.0, 11.0, 11.0, 11.005))
    assert not prices_flat((10.0, 11.0, 10.2, 10.8))
    assert volume_near_zero(0)
    assert volume_near_zero(0.9)
    assert not volume_near_zero(1)
    assert volume_near_zero(math.nan)


def test_one_price_is_directional_and_halt_excludes_it() -> None:
    assert is_one_price_limit(locked=True, flat=True, near_zero_volume=False)
    assert is_one_price_limit(locked=True, flat=False, near_zero_volume=True)
    assert not is_one_price_limit(locked=True, flat=False, near_zero_volume=False)
    assert not is_one_price_limit(locked=False, flat=True, near_zero_volume=True)
    assert is_volume_halt(flat=True, volume=0, one_price_up=False, one_price_down=False)
    assert not is_volume_halt(flat=True, volume=0, one_price_up=True, one_price_down=False)


def test_prior_bar_volume_only_for_non_close_fills() -> None:
    assert uses_prior_bar_volume("close_t", None) is False
    assert uses_prior_bar_volume("close_t", 10.0) is True
    assert uses_prior_bar_volume("open_t+1", None) is True
    assert uses_prior_bar_volume("signal_next_minute", None) is True


def test_release_one_price_keeps_true_halt() -> None:
    tradable = np.zeros((2, 1), dtype=np.uint8)
    prices = np.array([[11.0], [10.0]], dtype=np.float32)
    volume = np.array([[0.0], [0.0]], dtype=np.float32)
    up = np.array([[1], [0]], dtype=np.uint8)
    down = np.zeros((2, 1), dtype=np.uint8)
    release_one_price_boards(tradable, prices, prices, prices, prices, volume, up, down)
    assert tradable[0, 0] == 1
    assert tradable[1, 0] == 0


def test_release_one_price_chunk_matches_single_pass() -> None:
    rng = np.random.default_rng(0)
    shape = (5, 3)
    tradable = np.zeros(shape, dtype=np.uint8)
    prices = rng.normal(10, 0.01, size=shape).astype(np.float32)
    prices[0] = 11
    prices[2, 1] = 8
    volume = rng.uniform(0, 3, size=shape).astype(np.float32)
    volume[0] = 0
    up = np.zeros(shape, dtype=np.uint8)
    down = np.zeros(shape, dtype=np.uint8)
    up[0] = 1
    down[2, 1] = 1
    chunked = tradable.copy()
    whole = tradable.copy()
    release_one_price_boards(
        chunked, prices, prices, prices, prices, volume, up, down, chunk_rows=2,
    )
    release_one_price_boards(
        whole, prices, prices, prices, prices, volume, up, down, chunk_rows=shape[0],
    )
    assert np.array_equal(chunked, whole)
    assert chunked[0, 0] == 1
    assert chunked[1, 0] == 0
