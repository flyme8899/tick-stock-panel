"""离线特征派生（批次 20261025hfeat 任务②）。

守五件事：

1. **可复现**：同输入 → 同输出，逐列逐值比（浮点用 1e-9 相对容差）
2. **ε 只有一个来源**：`constants.EPSILON_VALUE_PCT`，代码里不许出现写死的
   `0.003` / `0.015` / `60`
3. **路径 A 与 B 分列**，不合并成单个"门禁通过"布尔
4. **敏感性档位不覆盖主钉**：`_sens_` 列改变时，主判定列必须一动不动
5. **口径断言在特征之前**：混尺帧进不来

第 2 条用 AST 扫描实现，不用裸子串 —— gp 批次的教训：在注释里写「不用 0.003」
也会被自己写的扫描判违规。
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.t_rounds import constants as C
from app.t_rounds.offline import (
    EPSILON_PCT_MAIN,
    GATE_PATH_A,
    GATE_PATH_B,
    SENSITIVITY_EPSILON_PCT,
    assert_no_market_open_impersonation,
    build_offline_feature_frame,
    epsilon_fraction,
    sensitivity_columns,
)

FEATURES_SRC = Path(__file__).resolve().parents[1] / "app" / "t_rounds" / "offline" / "features.py"
GATES_SRC = Path(__file__).resolve().parents[1] / "app" / "t_rounds" / "offline" / "gates.py"
OFFLINE_DIR = FEATURES_SRC.parent

SYMBOL = "600000.SH"
TRADE_DATE = date(2026, 1, 5)


def _price_path(prices: list[float], *, start: datetime | None = None) -> pl.DataFrame:
    """把一串价格造成分钟 bar（同尺：`amount` 按 `close×volume×100` 算）。"""
    start = start or datetime(2026, 1, 5, 9, 30)
    rows = []
    for i, px in enumerate(prices):
        rows.append(
            {
                "symbol": SYMBOL,
                "datetime": start + timedelta(minutes=i),
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "volume": 1000.0,
                "amount": px * 1000.0 * 100,
            }
        )
    return pl.DataFrame(rows)


def _feature(prices: list[float], **kwargs):
    """把一串价格直接包成 `LoadedMinutes` 走完整派生链（不走文件 IO）。"""
    from app.t_rounds.offline.loader import LoadedMinutes, assess_caliber

    frame = _price_path(prices)
    packed = LoadedMinutes(
        frame=frame,
        caliber=assess_caliber(frame),
        inputs=("inline",),
        skipped=(),
        aligned=False,
    )
    return build_offline_feature_frame(packed, **kwargs)


# ---------------------------------------------------------------- ε 来源


def test_epsilon_main_pin_comes_from_constants() -> None:
    """主钉 ε 必须是 `constants.EPSILON_VALUE_PCT`，不是本包自己的数。"""
    assert EPSILON_PCT_MAIN == C.EPSILON_VALUE_PCT
    assert EPSILON_PCT_MAIN == 0.30
    # 0.30% → 0.003 小数
    assert epsilon_fraction() == pytest.approx(0.003, rel=1e-12)


def test_sensitivity_is_separate_from_main_pin() -> None:
    """敏感性档位与主钉不相交，且不带主钉那档。"""
    assert EPSILON_PCT_MAIN not in SENSITIVITY_EPSILON_PCT
    for pct in SENSITIVITY_EPSILON_PCT:
        assert epsilon_fraction(pct) == pytest.approx(pct / 100.0, rel=1e-12)


def _numeric_constants(path: Path) -> list[tuple[int, float]]:
    """AST 取一个文件里所有浮点/整数字面量及其行号。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, float]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if isinstance(node.value, bool):
                continue
            out.append((node.lineno, float(node.value)))
    return out


@pytest.mark.parametrize("banned", [0.003, 0.015, 0.3, 60])
def test_no_hardcoded_epsilon_in_offline_package(banned: float) -> None:
    """`offline/` 里不许出现写死的 ε 相关数。

    - `0.003`：主钉 ε 的小数形式（必须走 `epsilon_fraction()`）
    - `0.015`：历史遗留的 1.5% 带
    - `0.3`  ：主钉本身写死也算违规（应引用常量）
    - `60`   ：曾是"一小时 60 分钟"之类的魔法数

    用 AST 而不是文本搜索：注释里提到这些数字**不该**被判违规。这条规则本身
    也是 gp 批次踩过的坑（裸子串扫描误伤注释）。
    """
    offenders: list[str] = []
    for src in sorted(OFFLINE_DIR.glob("*.py")):
        for lineno, value in _numeric_constants(src):
            if value == banned:
                offenders.append(f"{src.name}:{lineno} = {value}")
    assert not offenders, f"发现写死的 ε 相关常量: {offenders}"


def test_offline_package_does_not_redefine_epsilon() -> None:
    """ε 的唯一来源是 `t_rounds.constants`；本包只许**引用**。"""
    for src in sorted(OFFLINE_DIR.glob("*.py")):
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "EPSILON_VALUE_PCT":
                        pytest.fail(f"{src.name}:{node.lineno} 重新定义了 EPSILON_VALUE_PCT")


# ---------------------------------------------------------------- 可复现


def test_feature_derivation_is_reproducible() -> None:
    """同输入跑两次 → 逐列逐值相同（浮点按 1e-9 相对容差）。"""
    prices = [10.00, 10.02, 10.06, 10.05, 10.01, 9.98, 9.96, 10.00]
    a = _feature(prices)
    b = _feature(prices)

    assert a.frame.columns == b.frame.columns
    assert a.frame.height == b.frame.height == len(prices)
    assert a.dates == b.dates
    assert a.bars == b.bars
    assert a.path_a_hits == b.path_a_hits
    assert a.path_b_hits == b.path_b_hits

    for col in a.frame.columns:
        va, vb = a.frame[col].to_list(), b.frame[col].to_list()
        if va and isinstance(va[0], float):
            for x, y in zip(va, vb, strict=True):
                if x is None and y is None:
                    continue
                assert x == pytest.approx(y, rel=1e-9, abs=1e-12), f"{col} 不可复现"
        else:
            assert va == vb, f"{col} 不可复现"


def test_feature_frame_has_required_columns() -> None:
    """报告要引用这些列，缺一个都会让下游对不上名。"""
    result = _feature([10.0, 10.02, 10.06])
    for col in ("symbol", "date", "datetime", "price", "vwap"):
        assert col in result.frame.columns, f"缺 {col}"
    for col in ("gate_enter_band_up", "gate_enter_band_down"):
        assert col in result.frame.columns, f"缺路径 B 列 {col}"


# ---------------------------------------------------------------- A / B 分离


def test_path_a_and_b_are_separate_columns() -> None:
    """A / B 必须是不同列，**绝不**合成一个"门禁通过"布尔。

    任务书原文：「把 A 或 B 合成一个「门禁通过」的布尔值，就等于放弃了这个区分」。
    """
    result = _feature([10.0, 10.02, 10.06])
    cols = set(result.frame.columns)

    assert GATE_PATH_A == "A" and GATE_PATH_B == "B"
    # 路径 A 的列名带 avg_cross
    a_cols = {c for c in cols if "avg_cross" in c}
    assert a_cols, "路径 A 没有产出任何列"
    # 路径 B 的列名带 gate_enter_band
    b_cols = {c for c in cols if "gate_enter_band" in c}
    assert b_cols, "路径 B 没有产出任何列"
    # 两者不相交
    assert not (a_cols & b_cols)

    # 不存在一个笼统的合并列
    for suspicious in ("gate_pass", "gate_pass_any", "signal_any", "entry_any"):
        assert suspicious not in cols, f"出现了合并列 {suspicious}"


def test_path_b_marks_first_entry_only() -> None:
    """路径 B 只标**首次**进带：第二次进同一带不再标。

    做 T 的第二腿是一次性动作，同一带内反复穿越不构成新机会。
    """
    # vwap 会随价格漂移，这里用单调上行让 +0.3% 带被反复触及
    prices = [10.0, 10.02, 10.04, 10.06, 10.08, 10.10]
    result = _feature(prices)
    up = result.frame["gate_enter_band_up"].to_list()
    assert sum(bool(x) for x in up) <= 1, f"gate_enter_band_up 标了多次: {up}"


# ---------------------------------------------------------------- 敏感性


def test_sensitivity_columns_do_not_override_main_pin() -> None:
    """打开敏感性后，主判定列必须**一模一样**。

    这是"敏感性只做旁证"的执行点：如果 `with_sensitivity=True` 能改动主列，
    那份报告的主结论就会被敏感性档位污染。
    """
    prices = [10.00, 10.02, 10.04, 10.06, 10.08, 10.05, 10.01, 9.98]

    plain = _feature(prices)
    with_sens = _feature(prices, with_sensitivity=True)

    for col in ("gate_enter_band_up", "gate_enter_band_down"):
        assert plain.frame[col].to_list() == with_sens.frame[col].to_list(), (
            f"开敏感性后主列 {col} 变了"
        )
    for col in ("signal_intraday_avg_cross_up", "signal_intraday_avg_cross_down"):
        if col in plain.frame.columns:
            assert plain.frame[col].to_list() == with_sens.frame[col].to_list()

    # 敏感性列确实新增了，且全部带前缀
    new_cols = set(with_sens.frame.columns) - set(plain.frame.columns)
    assert new_cols, "with_sensitivity=True 没有新增任何列"
    for col in new_cols:
        assert col.startswith("_sens_"), f"敏感性列 {col} 没带 _sens_ 前缀"
    assert set(sensitivity_columns()) == new_cols


def test_sensitivity_prefix_is_not_a_valid_main_column() -> None:
    """`_sens_` 前缀是"非主判定"的标记，不能被当成主列名用。"""
    for col in sensitivity_columns():
        assert col.startswith("_sens_")
        assert col not in ("gate_enter_band_up", "gate_enter_band_down")


# ---------------------------------------------------------------- 口径守门


def test_mixed_caliber_is_rejected_before_features() -> None:
    """混尺帧进不来：`build_offline_feature_frame` 在派生之前就抛。

    这条防的是「先算后洗」：`build_feature_frame` 内部的 `sanitize_minute_amount`
    会把混尺额改写成 `close×volume×100`，ratio 就归 1 了，问题消失得无影无踪。
    """
    from app.t_rounds.offline.loader import LoadedMinutes, assess_caliber

    frame = _price_path([10.0, 10.02, 10.06]).with_columns(
        (pl.col("amount") * 2.0).alias("amount")
    )
    packed = LoadedMinutes(
        frame=frame,
        caliber=assess_caliber(frame),
        inputs=("inline",),
        skipped=(),
        aligned=False,
    )
    with pytest.raises(ValueError) as exc:
        build_offline_feature_frame(packed)
    assert "混尺" in str(exc.value) or "对齐" in str(exc.value)


def test_auction_bar_is_rejected_before_features() -> None:
    """09:25 的 bar 不许进特征派生。"""
    from app.t_rounds.offline.loader import LoadedMinutes, assess_caliber

    frame = _price_path([10.0, 10.02], start=datetime(2026, 1, 5, 9, 25))
    packed = LoadedMinutes(
        frame=frame,
        caliber=assess_caliber(frame),
        inputs=("inline",),
        skipped=(),
        aligned=False,
    )
    with pytest.raises(ValueError):
        build_offline_feature_frame(packed)

    # 直接调守门函数也抛（双保险）
    with pytest.raises(ValueError):
        assert_no_market_open_impersonation(frame)


# ---------------------------------------------------------------- 版本


def test_version_string_is_pinned() -> None:
    """报告版本串必须可从常量推出，不在这里另拼一份。"""
    from app.t_rounds.offline import assert_version_matches_pin

    version = assert_version_matches_pin()
    assert version == C.design_version()
    assert version == "design_pin_v1+eps_pct:0.30"


def test_feature_module_does_not_fork_vwap_formula() -> None:
    """特征模块**不许**自己再写一遍 VWAP 公式。

    那个公式是四条消费路径的单一口径源。离线回测若自己实现一份"看起来一样"的，
    上游一改就悄悄分叉，而分叉的表现是"回测结论与实盘不一致"。
    """
    tree = ast.parse(FEATURES_SRC.read_text(encoding="utf-8"))
    source = FEATURES_SRC.read_text(encoding="utf-8")
    # 应当调用统一实现
    assert "build_feature_frame" in source
    # 不应出现自己算 VWAP 的迹象
    for fn in ("cum_sum", "cumsum"):
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == fn:
                pytest.fail(
                    f"features.py:{node.lineno} 出现 {fn}; "
                    "离线特征不该自己累计成交量算 VWAP, 应交给 build_feature_frame"
                )
