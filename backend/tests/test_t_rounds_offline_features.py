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


# ---------------------------------------------------------------- price := close
#
# 批次 20261025gtrounds_patch ②。
#
# 回测的成交价取 bar 的 `price`。这个字段不是本包算的 —— 它是
# `app/strategy/intraday_features.py` L101 里 `{"price": pl.col("close")}` 赋过来的。
# 所以"成交价 = bar[i+1]['price']"等价于"以下一根 bar 的收盘价成交"。
# 这条测试把生成处的这层赋值钉住: 哪天那边把 price 改成 mid 或典型价, 这里立刻红。


STRATEGY_FEATURES_SRC = (
    Path(__file__).resolve().parents[1] / "app" / "strategy" / "intraday_features.py"
)


def test_price_is_assigned_from_close_at_the_generation_site() -> None:
    """生成处 `price` 必须就是 `close`(源码级核对, 不用跑策略)。"""
    src = STRATEGY_FEATURES_SRC.read_text(encoding="utf-8")
    assert 'pl.col("close")' in src, "生成处不见了 close 赋值, price 口径可能已改"
    # 允许有空白差异, 但必须是 "price" 映射到 close
    compact = "".join(src.split())
    assert '"price":pl.col("close")' in compact, (
        "生成处 price 不再等于 close —— 回测的'下一根收盘价成交'口径随之失效"
    )


def test_built_frame_price_equals_close() -> None:
    """端到端: 建出来的特征帧里, 每一行 `price == close`。"""
    from app.strategy.intraday_features import build_feature_frame

    d = date(2026, 1, 5)
    rows = []
    for i in range(5):
        c = 10.0 + i * 0.03
        rows.append(
            {
                "symbol": SYMBOL,
                "date": d,
                "datetime": datetime(d.year, d.month, d.day, 9, 30) + timedelta(minutes=i),
                "open": c - 0.02,
                "high": c + 0.05,
                "low": c - 0.05,
                "close": c,
                "volume": 1000.0,
                "amount": c * 1000.0 * 100,
            }
        )
    raw = pl.DataFrame(rows)
    out = build_feature_frame(raw)
    assert "price" in out.columns
    # `build_feature_frame` 不保留 `close` 列, 所以按 `datetime` 对齐回原始帧比较,
    # 而不是指望输出里还有 close 可比。
    joined = out.select("datetime", "price").join(
        raw.select("datetime", "close"), on="datetime", how="inner"
    )
    assert joined.height == out.height, "有行没对上原始 close"
    diffs = (joined["price"] - joined["close"]).abs().max()
    assert diffs == pytest.approx(0.0), (
        f"price 与 close 不一致(最大偏差 {diffs}); 生成处可能已改口径"
    )


# ---------------------------------------------------------------- D-assert 接线
#
# 批次 20261025gtrounds_patch ①。
#
# 断言定义在 loader, 但只有被 `build_offline_feature_frame` **真的调用**才算数。
# 光测 loader 那个函数是测不到"接线断了"的 —— 把调用行删掉, 那些用例照样全绿。


def _lunch_packed(*, afternoon_first_hhmm: tuple[int, int]) -> object:
    """造一个"上午末段 + 下午开段"的 `LoadedMinutes`, 下午首根时刻可调。"""
    from app.t_rounds.offline.loader import LoadedMinutes, assess_caliber

    d = date(2026, 1, 5)
    stamps = [
        datetime(d.year, d.month, d.day, 11, 28),
        datetime(d.year, d.month, d.day, 11, 29),
        datetime(d.year, d.month, d.day, 11, 30),
        # 下午首根 —— 正确应是 13:01; 传 (13,0) 就是"开始标注"那种错
        datetime(d.year, d.month, d.day, *afternoon_first_hhmm),
        datetime(d.year, d.month, d.day, afternoon_first_hhmm[0], afternoon_first_hhmm[1] + 1),
    ]
    rows = [
        {
            "symbol": SYMBOL,
            "datetime": ts,
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "volume": 1000.0, "amount": 10.0 * 1000.0 * 100,
        }
        for ts in stamps
    ]
    frame = pl.DataFrame(rows)
    return LoadedMinutes(
        frame=frame,
        caliber=assess_caliber(frame),
        inputs=("inline",),
        skipped=(),
        aligned=False,
    )


def test_build_offline_feature_frame_runs_the_lunch_assert() -> None:
    """接线测试: 坏午休结构必须在 `build_offline_feature_frame` 里被拦下。

    把 `features.py` 里那行 `assert_end_of_bar_lunch_gap(loaded.frame)` 删掉,
    这条就会失败 —— 这才是"接线没断"的证据。
    """
    good = _lunch_packed(afternoon_first_hhmm=(13, 1))
    out = build_offline_feature_frame(good)  # 不抛
    assert out.bars == 5

    bad = _lunch_packed(afternoon_first_hhmm=(13, 0))
    with pytest.raises(ValueError, match="午休结构异常"):
        build_offline_feature_frame(bad)


def test_docs_and_backtest_state_price_equals_close() -> None:
    """三处钉注必须在: backtest docstring / features 调用点 / docs。"""
    backtest_src = (OFFLINE_DIR / "backtest.py").read_text(encoding="utf-8")
    features_src = FEATURES_SRC.read_text(encoding="utf-8")
    # OFFLINE_DIR = <repo>/backend/app/t_rounds/offline
    #   parents[0]=t_rounds  [1]=app  [2]=backend  [3]=<repo>
    repo_root = OFFLINE_DIR.parents[3]
    docs_path = repo_root / "docs" / "t-rounds-offline-hfeat.md"
    assert docs_path.exists(), f"docs 不存在: {docs_path}"
    docs = docs_path.read_text(encoding="utf-8")

    assert "price := 该 bar 的 close" in backtest_src, "backtest.py docstring 未钉 price:=close"
    assert "price := 该 bar 的 close" in features_src, "features.py 调用点未钉 price:=close"
    assert "price := 该 bar 的 close" in docs, "docs 未钉 price:=close"
    # 引用生成处; 且不得再出现"生成处还没核过"那类旧表述
    # (拼起来写, 免得这行断言本身把那个词带进源码扫描)
    stale_phrase = "未" + "核实"
    for name, text in (("backtest.py", backtest_src), ("features.py", features_src)):
        assert "L101" in text, f"{name} 未引用生成处 L101"
        assert stale_phrase not in text, f"{name} 仍含旧的不确定表述"


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
