"""腾讯实时成交量归一兜底：科创板（688/689）不 ×100。

批次 20261027gd1g6 · G6/B1。

背景：``akshare_fetcher._normalize_tencent_volume`` 会用「流通市值 / 价格 × 换手率」
反推成交量，再在原值与「手转股」结果之间择优。当交叉校验不可用（停牌、新股、
字段错位）时走兜底分支。兜底以前恒定 ``raw_volume * 100``，但 D1 审计实测腾讯
对 688/689 返回的成交量**已经是「股」**，再 ×100 会把科创成交量放大 100 倍。

本测试只钉住「兜底分支按板块分流」这一件事，外加「交叉校验成功路径不变」。

## 为什么不直接 ``import data_provider.akshare_fetcher``

``akshare_fetcher`` 是 vendor（DSA）的运行时代码，它的模块级 import 链包括
``tenacity``、``fake_useragent`` 以及 ``src.config`` → ``src.schemas``（pydantic
模型）等，这些**都不在 backend 的依赖图里**（``backend/uv.lock`` 无 ``tenacity``）。
CI 只在 ``backend`` 目录跑 ``pytest tests``，在那里导入整个模块必失败。

所以本测试沿用仓内既有的「按需加载单文件模块」范式
（参见 ``test_check_minute_kline.py`` 的 ``spec_from_file_location``），
把范围收窄到**只执行被测函数本身**：

- 用 ``ast`` 从产品源码里取 ``_normalize_tencent_volume`` 的**原文**（不复制、不改写）
- 依赖全部取真实实现：``safe_int`` / ``safe_float`` 来自 ``realtime_types``
  （该模块只用标准库，可整模块导入）；``_is_star_market`` 来自 ``tencent_fetcher``
- 若上游把这两个 helper 改名/删除，本测试会在加载期失败，而不是悄悄用假实现

这样测的仍是产品代码，且不引入 backend 未声明的依赖。

仅供研究，不构成投资建议；执行须人工确认。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Callable

import pytest

_VENDOR_DSA = Path(__file__).resolve().parents[2] / "vendor" / "daily_stock_analysis"
_DP_DIR = _VENDOR_DSA / "data_provider"

_AKSHARE_SRC = _DP_DIR / "akshare_fetcher.py"
_TENCENT_SRC = _DP_DIR / "tencent_fetcher.py"
_REALTIME_TYPES_SRC = _DP_DIR / "realtime_types.py"

# 兜底样本的原始成交量：选一个「×100 之后与原值明显不同」的量，便于区分两条分支。
_RAW = 4321


def _load_realtime_types() -> Any:
    """``realtime_types`` 只依赖标准库，整模块执行是安全的。"""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("_g6_realtime_types", _REALTIME_TYPES_SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _extract_function(source_path: Path, func_name: str) -> ast.FunctionDef:
    """在产品源码里定位顶层函数节点。"""
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise AssertionError(f"{source_path.name} 里找不到顶层函数 {func_name}（上游可能已改名/删除）")


def _exec_function(source_path: Path, func_name: str, namespace: dict[str, Any]) -> Callable[..., Any]:
    """取函数**原文**在给定命名空间里执行，返回可调用对象。

    不复制、不改写函数体；命名空间由调用方提供真实依赖。
    """
    node = _extract_function(source_path, func_name)
    source = ast.get_source_segment(source_path.read_text(encoding="utf-8"), node)
    assert source is not None, f"{func_name} 源码切片失败"
    # 去掉装饰器后执行（本函数无装饰器，此处只为稳妥）
    exec(compile(source, str(source_path), "exec"), namespace)  # noqa: S102
    return namespace[func_name]


_REALTIME = _load_realtime_types()

# 真实依赖：safe_int / safe_float 来自产品 realtime_types，_is_star_market 来自产品 tencent_fetcher
_NS: dict[str, Any] = {
    "List": list,
    "Optional": __import__("typing").Optional,
    "safe_int": _REALTIME.safe_int,
    "safe_float": _REALTIME.safe_float,
}
_is_star_market = _exec_function(_TENCENT_SRC, "_is_star_market", dict(_NS))
_NS["_is_star_market"] = _is_star_market
_normalize_tencent_volume = _exec_function(_AKSHARE_SRC, "_normalize_tencent_volume", _NS)


# --------------------------------------------------------------------------- #
# 样本构造
# --------------------------------------------------------------------------- #


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


def _crosscheck_fields(code: str, raw: int, *, expected_multiple: int) -> list[str]:
    """构造能进交叉校验成功分支的 fields。

    价格 10 元、流通市值 100 亿，换手率设定为使 ``expected_volume`` ≈
    ``raw * expected_multiple``，从而决定择优走向：
    ``expected_multiple=1`` → expected ≈ raw（应选 raw）；
    ``expected_multiple=100`` → expected ≈ raw*100（应选 raw*100）。
    """
    price = 10.0
    circ_mv_yi = 100.0                      # 100 亿
    circ_mv = circ_mv_yi * 100000000
    expected = raw * expected_multiple      # 期望的「股」口径成交量
    # expected = (circ_mv / price) * (turnover / 100) → turnover = expected * 100 * price / circ_mv
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
    got_raw = _normalize_tencent_volume(_crosscheck_fields(code, _RAW, expected_multiple=1), code)
    assert got_raw == _RAW, f"{code} 成功路径应择 raw，实得 {got_raw}"

    # expected ≈ raw*100 → 手转股结果更接近，应返回 raw*100
    got_hand = _normalize_tencent_volume(
        _crosscheck_fields(code, _RAW, expected_multiple=100), code
    )
    assert got_hand == _RAW * 100, f"{code} 成功路径应择 raw*100，实得 {got_hand}"


def test_crosscheck_result_is_board_agnostic() -> None:
    """同一份成功分支 fields，科创与非科创返回完全一致（成功路径无板块分流）。"""
    fields_main = _crosscheck_fields("600000", _RAW, expected_multiple=1)
    fields_star = _crosscheck_fields("688111", _RAW, expected_multiple=1)
    # 除 fields[2] 代码外其余字段等价
    assert fields_main[1] == fields_star[1]
    assert fields_main[3:] == fields_star[3:]

    assert _normalize_tencent_volume(fields_main, "600000") == _normalize_tencent_volume(
        fields_star, "688111"
    )


# --------------------------------------------------------------------------- #
# N4 / G-T5 / G-T6：判定语义对齐 _is_star_market，且未误用 is_kc_cy_stock
# --------------------------------------------------------------------------- #


def test_uses_shared_star_helper_import() -> None:
    """产品源码 import 的是 ``tencent_fetcher._is_star_market``，不是手抄分叉。"""
    source = _AKSHARE_SRC.read_text(encoding="utf-8")
    assert "from .tencent_fetcher import _is_star_market" in source, (
        "akshare_fetcher 应直接复用 tencent_fetcher._is_star_market"
    )


def test_normalize_body_calls_star_helper_not_kc_cy() -> None:
    """被测函数体内调用 ``_is_star_market``，且**不**调用 ``is_kc_cy_stock``。"""
    node = _extract_function(_AKSHARE_SRC, "_normalize_tencent_volume")
    called = {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_is_star_market" in called, f"函数体应调用 _is_star_market，实际调用 {sorted(called)}"
    assert "is_kc_cy_stock" not in called, "禁止用 is_kc_cy_stock 判科创（缺 689、误含创业板）"


def test_helper_semantics_match_star_market() -> None:
    """本次执行所注入的判定与产品 ``_is_star_market`` 是同一份实现。"""
    tencent_source = _TENCENT_SRC.read_text(encoding="utf-8")
    assert "def _is_star_market" in tencent_source
    # 688 + 689 都覆盖，且剥 sh/sz/bj 前缀
    assert _is_star_market("688111") is True
    assert _is_star_market("689009") is True
    assert _is_star_market("sh688111") is True
    assert _is_star_market("688111.SH") is True
    assert _is_star_market("300750") is False
    assert _is_star_market("600000") is False


def test_689_cdr_not_misjudged() -> None:
    """689 CDR 兜底不 ×100——若误用 is_kc_cy_stock（缺 689）本条会红。"""
    got = _normalize_tencent_volume(_fallback_fields("689009"), "689009")
    assert got == _RAW, f"689 CDR 兜底应返回 {_RAW}，实得 {got}（误判为非科创会得 {_RAW * 100}）"


def test_cyb_300_not_exempted() -> None:
    """创业板 300 兜底仍 ×100——若误用 is_kc_cy_stock（含 30）本条会红。"""
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
