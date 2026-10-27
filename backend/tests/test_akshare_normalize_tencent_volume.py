"""腾讯实时成交量归一兜底：科创板（688/689）不 ×100。

批次 20261027gd1g6 · G6/B1。

背景：``akshare_fetcher._normalize_tencent_volume`` 会用「流通市值 / 价格 × 换手率」
反推成交量，再在原值与「手转股」结果之间择优。当交叉校验不可用（停牌、新股、
字段错位）时走兜底分支。兜底以前恒定 ``raw_volume * 100``，但 D1 审计实测腾讯
对 688/689 返回的成交量**已经是「股」**，再 ×100 会把科创成交量放大 100 倍。

本测试只钉住「兜底分支按板块分流」这一件事，外加「交叉校验成功路径不变」。
真实调用产品函数，不复制私有实现。

落点说明：CI（``.github/workflows/ci.yml``）只在 ``backend`` 目录跑 pytest，
``vendor/daily_stock_analysis/tests`` 不会被 CI 触发；故本文件落在
``backend/tests/``，与 ``test_dsa_*`` 系列同惯例经 ``sys.path`` 注入导入 vendor。

仅供研究，不构成投资建议；执行须人工确认。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VENDOR_DSA = Path(__file__).resolve().parents[2] / "vendor" / "daily_stock_analysis"

if str(_VENDOR_DSA) not in sys.path:  # pragma: no cover - 幂等注入
    sys.path.insert(0, str(_VENDOR_DSA))

from data_provider.akshare_fetcher import _normalize_tencent_volume  # noqa: E402
from data_provider.tencent_fetcher import _is_star_market  # noqa: E402

# 兜底样本的原始成交量：选一个「×100 之后与原值明显不同」的量，便于区分两条分支。
_RAW = 4321


def _fallback_fields(code: str = "600000", raw: int = _RAW) -> list[str]:
    """构造进不了交叉校验成功分支的 fields。

    交叉校验需要 price>0 ∧ turnover_rate>0 ∧ circ_mv>0 同时成立；这里把
    price / 换手率 / 流通市值全部留空，函数必然落到兜底分支。
    """
    fields = [""] * 50
    fields[1] = "样本"
    fields[2] = code
    fields[3] = ""      # price —— 留空即不进成功分支
    fields[6] = str(raw)
    fields[38] = ""     # 换手率
    fields[44] = ""     # 流通市值（亿）
    return fields


def _crosscheck_fields(code: str, raw: int, *, shares_per_hand_gap: int) -> list[str]:
    """构造能进交叉校验成功分支的 fields。

    价格 10 元、流通市值 100 亿、换手率设定为使 ``expected_volume`` ≈ ``raw``
    或 ``raw * 100``，由 ``shares_per_hand_gap`` 决定择优走向：
    ``gap=1`` → expected ≈ raw（应选 raw）；``gap=100`` → expected ≈ raw*100
    （应选 raw*100）。
    """
    price = 10.0
    circ_mv_yi = 100.0                      # 100 亿
    circ_mv = circ_mv_yi * 100000000
    expected = raw * shares_per_hand_gap    # 期望的「股」口径成交量
    # expected = (circ_mv / price) * (turnover / 100)  →  turnover = expected * 100 * price / circ_mv
    turnover = expected * 100 * price / circ_mv

    fields = [""] * 50
    fields[1] = "样本"
    fields[2] = code
    fields[3] = repr(price)
    fields[6] = str(raw)
    fields[38] = repr(turnover)
    fields[44] = repr(circ_mv_yi)
    return fields


# --------------------------------------------------------------------------- #
# N1 / G-T2：兜底路径 688/689（含前缀与后缀形态）不 ×100
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code",
    [
        "688111",     # 裸码
        "sh688111",   # 带交易所前缀
        "SH688111",   # 前缀大写
        "688111.SH",  # 带交易所后缀
        "688111.sh",  # 后缀小写
        "689009",     # 689 CDR 裸码
        "sh689009",   # 689 CDR 带前缀
        "689009.SH",  # 689 CDR 带后缀
    ],
)
def test_fallback_star_no_x100(code: str) -> None:
    """科创板腾讯量已是「股」，兜底不得再 ×100。"""
    # 前置：确认样本代码确实被判为科创板，否则本参数组形同虚设。
    assert _is_star_market(code) is True, f"{code} 应按 _is_star_market 判为科创板"

    got = _normalize_tencent_volume(_fallback_fields(code), code)
    assert got == _RAW, f"{code} 兜底应返回原值 {_RAW}（股），实得 {got}"


def test_fallback_star_from_fields2_when_symbol_empty() -> None:
    """symbol 留空时回落 fields[2]，仍能识别科创板。"""
    fields = _fallback_fields("688981")
    got = _normalize_tencent_volume(fields, "")
    assert got == _RAW

    got_default = _normalize_tencent_volume(fields)  # 不传 symbol，走缺省
    assert got_default == _RAW


def test_fallback_star_symbol_wins_over_fields2() -> None:
    """symbol 非空时优先于 fields[2]（方案 A 的「非空优先」语义）。"""
    fields = _fallback_fields("600000")  # 字段 2 写主板
    got = _normalize_tencent_volume(fields, "688111")  # 显式传科创
    assert got == _RAW, "显式 symbol 应覆盖 fields[2] 的板块判定"


# --------------------------------------------------------------------------- #
# N2 / G-T3：兜底路径非科创仍 ×100（创业板不豁免）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code",
    [
        "600000",     # 沪主板
        "601318",     # 沪主板
        "000001",     # 深主板
        "sz000001",   # 深主板带前缀
        "300750",     # 创业板 —— 本拍**不**豁免
        "sz300750",   # 创业板带前缀
        "920001",     # 北交所
    ],
)
def test_fallback_non_star_still_x100(code: str) -> None:
    """非科创维持既有「手转股」语义。"""
    assert _is_star_market(code) is False, f"{code} 不应被判为科创板"

    got = _normalize_tencent_volume(_fallback_fields(code), code)
    assert got == _RAW * 100, f"{code} 兜底应返回 {_RAW * 100}（手→股），实得 {got}"


# --------------------------------------------------------------------------- #
# N3 / G-T4：交叉校验成功路径语义不变
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", ["600000", "688111", "689009", "300750"])
def test_crosscheck_success_path_unchanged(code: str) -> None:
    """成功分支仍按「谁的偏差小选谁」择优，与板块无关。

    该分支在 tip 基线上即不区分板块；本拍只改兜底，成功路径不得因本拍
    产生任何科创特例。
    """
    # expected ≈ raw → raw 更接近，应返回 raw
    got_raw = _normalize_tencent_volume(_crosscheck_fields(code, _RAW, shares_per_hand_gap=1), code)
    assert got_raw == _RAW, f"{code} 成功路径应择 raw，实得 {got_raw}"

    # expected ≈ raw*100 → 手转股结果更接近，应返回 raw*100
    got_hand = _normalize_tencent_volume(
        _crosscheck_fields(code, _RAW, shares_per_hand_gap=100), code
    )
    assert got_hand == _RAW * 100, f"{code} 成功路径应择 raw*100，实得 {got_hand}"


def test_crosscheck_result_is_board_agnostic() -> None:
    """同一份成功分支 fields，科创与非科创返回完全一致（成功路径无板块分流）。"""
    fields_main = _crosscheck_fields("600000", _RAW, shares_per_hand_gap=1)
    fields_star = _crosscheck_fields("688111", _RAW, shares_per_hand_gap=1)
    # 除 fields[2] 代码外其余字段等价
    assert fields_main[:2] == fields_star[:2]
    assert fields_main[3:] == fields_star[3:]

    assert _normalize_tencent_volume(fields_main, "600000") == _normalize_tencent_volume(
        fields_star, "688111"
    )


# --------------------------------------------------------------------------- #
# N4 / G-T5 / G-T6：判定语义对齐 _is_star_market，且未误用 is_kc_cy_stock
# --------------------------------------------------------------------------- #


def test_helper_is_reused_not_forked() -> None:
    """产品模块直接复用 tencent 侧的 ``_is_star_market``，而非自带一份分叉。"""
    from data_provider import akshare_fetcher

    assert akshare_fetcher._is_star_market is _is_star_market, (
        "akshare_fetcher 应直接复用 tencent_fetcher._is_star_market，不得另写一份"
    )


def test_689_cdr_not_misjudged_by_is_kc_cy_stock() -> None:
    """689 CDR 兜底不 ×100——若误用 is_kc_cy_stock（缺 689）本条会红。

    这是 N4 的可测锚：``is_kc_cy_stock`` 只覆盖 688 + 30，689 会被它判为
    「非科创」从而 ×100，与本拍目标相反。
    """
    from data_provider.base import is_kc_cy_stock

    # 记录禁误用锚的现状：is_kc_cy_stock 对 689 不认为科创板
    assert is_kc_cy_stock("689009") is False, (
        "is_kc_cy_stock 现状应不含 689；若上游已扩到 689，本锚需重评"
    )

    got = _normalize_tencent_volume(_fallback_fields("689009"), "689009")
    assert got == _RAW, f"689 CDR 兜底应返回 {_RAW}，实得 {got}（误用 is_kc_cy_stock 会得 {_RAW * 100}）"


def test_cyb_300_not_exempted_by_star_branch() -> None:
    """创业板 300 兜底仍 ×100——若误用 is_kc_cy_stock（含 30）本条会红。"""
    from data_provider.base import is_kc_cy_stock

    assert is_kc_cy_stock("300750") is True, "is_kc_cy_stock 现状含创业板 300，正是它不能用于本判定的原因"

    got = _normalize_tencent_volume(_fallback_fields("300750"), "300750")
    assert got == _RAW * 100, f"创业板兜底应仍 ×100（{_RAW * 100}），实得 {got}"


# --------------------------------------------------------------------------- #
# 边界：无法判定 / 空值
# --------------------------------------------------------------------------- #


def test_short_or_empty_fields_return_none() -> None:
    """字段不足或成交量为空时返回 None，行为与 tip 一致。"""
    assert _normalize_tencent_volume([], "") is None
    assert _normalize_tencent_volume(["", "", "688111", "", "", "", ""], "688111") is None
    assert _normalize_tencent_volume(["", "", "688111", "", "", "", "abc"], "688111") is None


def test_blank_everything_falls_back_to_x100() -> None:
    """代码提示完全缺失时按非科创处理，保持旧的 ×100 兜底。"""
    fields = _fallback_fields("")
    got = _normalize_tencent_volume(fields, "")
    assert got == _RAW * 100
