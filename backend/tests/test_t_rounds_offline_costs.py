"""做 T 成本敏感性 5c · 五档跑表 / 接档 / Regime（批次 20261025gcost5c）。

守五件事：

1. **真扣** —— 五档金额一律由 `complete_round` 登记，不绕过状态机、不"只改报告数字"
2. **禁 silent 0 回归** —— 非零档 + `amount=0` 仍拒（5b 契约没被这一拍改坏）
3. **名义单选** —— `avg(leg1, leg2) * qty`，全包只有一处实现
4. **列名钉死** —— §2.3 八列，不许同义另名并行
5. **Regime 缺数就说 pending** —— 不编造、不删列、不挡主表

测试数据沿用 `test_t_rounds_offline_backtest.py` 的 `_day` 手法：手工灌标记列，
每个场景的期望值能手算，而不是"跑一遍抄结果"。
"""
from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.t_rounds import constants as C
from app.t_rounds.offline import (
    COST_RATE_PCT_BY_TIER,
    COST_TIERS_SENSITIVITY,
    FOOTNOTE_RETAIL_ASSUMPTION,
    RETAIL_ASSUMPTION_TIERS,
    SENSITIVITY_COLUMNS,
    build_cost_sensitivity,
    build_regime_report,
    build_sensitivity_payload,
    cost_amount_for_tier,
    notional_avg_legs,
    run_mode_a_backtest,
    write_offline_report,
)
from app.t_rounds.offline import backtest as backtest_mod
from app.t_rounds.offline import costs as costs_mod
from app.t_rounds.offline import sensitivity as sens_mod
from app.t_rounds.state_machine import RoundCost, complete_round, transition
from app.t_rounds.models import TRoundRecord, TRoundValidationError

PKG_DIR = Path(__file__).resolve().parents[1] / "app" / "t_rounds" / "offline"
SYMBOL = "600000.SH"
TRADE_DATE = date(2026, 1, 5)


def _day(prices, *, vwap, up_band=None, down_band=None, up_cross=None, down_cross=None):
    """造一日 bar + 手工灌标记列（与既有 offline 测试同一手法）。"""
    start = datetime(2026, 1, 5, 9, 30)
    times = [start + timedelta(minutes=i) for i in range(len(prices))]
    values = [float(p) for p in prices]
    n = len(times)
    rows = [
        {
            "symbol": SYMBOL,
            "date": TRADE_DATE,
            "datetime": times[i],
            "price": values[i],
            "vwap": float(vwap),
        }
        for i in range(n)
    ]
    frame = pl.DataFrame(rows)

    def col(name: str, flags) -> pl.Series:
        seq = list(flags or [False] * n)
        assert len(seq) == n, f"{name} 长度须等于 bar 数"
        return pl.Series(name, seq, dtype=pl.Boolean)

    return frame.with_columns(
        [
            col("gate_enter_band_up", up_band),
            col("gate_enter_band_down", down_band),
            col("signal_intraday_avg_cross_up", up_cross),
            col("signal_intraday_avg_cross_down", down_cross),
        ]
    ).sort(["symbol", "datetime"])


def _mixed_frame():
    """造一条**同一天、两个格子各一轮、结局不同**的帧。

    | 格子 | 结局 | mtm |
    |---|---|---|
    | `sell_first` / B（上带） | `completed` | — |
    | `buy_first` / B（下带） | `unreverted_at_cutoff` | **非零** |

    为什么要这种混合帧：只用一条 completed 轮时，
    - `rounds == completed` → 分母写错（用 completed 当分母）测不出来；
    - `mtm_total == 0` → 漏掉 mtm 测不出来。
    这两个都是真实的测试盲区，变异测试抓到了（M13 / M14）。

    **配额**：`MAX_ROUNDS_PER_DAY = 1` 按 (模式, 路径, symbol, date) 计数，四个格子
    是独立世界 —— 所以让两个格子各一轮互不影响（见 `run_mode_a_backtest` 的注释）。

    **覆盖全天**：必须铺一根过 14:57 的 bar，否则没回摆的那轮会被判成 `data_truncated`
    （数据提前断了）而不是 `unreverted_at_cutoff`（时段走完、可盯市）—— 那就没有 mtm 了。

    价格（均价恒定 10.0，ε=0.3%）：

    | idx | price | 说明 |
    |---|---|---|
    | 0 | 10.10 | 上带首次进入 → sell_first/B 的确认 bar |
    | 1 | 10.10 | 成交（fill=10.10）；同时是下带首次进入?否 |
    | 2 | 9.85 | 跌破（1-ε)×10 = 9.97 → 下带首次进入 → buy_first/B 确认 bar |
    | 3 | 9.85 | 下带轮的成交价（fill=9.85） |
    | 4..7 | 9.85 | 一路在均价下方，**没有**回到 ≥10.0 → buy_first 那轮不回摆 |
    | 收盘占位 | 15:00 | 让"时段走完"成立 → unreverted_at_cutoff + 可盯市 |

    buy_first 的 B 路径：成交在均价下方（买），要 `offset >= 0`（价格回到均价上方）
    才算平掉。价格一路 9.85 < 10.0，永不满足 → 未回摆。

    mtm 价 = 14:57 前最后一根 = 9.85；buy_first 是"先买后卖"，平仓方向是卖：
    `(mtm - fill) * qty = (9.85 - 9.85) * 100 = 0` —— **不行**，差价为 0 又测不出 mtm。
    所以让末尾价格与成交价不同：
    """
    prices = [10.10, 10.10, 9.85, 9.80, 9.82, 9.80, 9.78]
    frame = _day(
        prices,
        vwap=10.0,
        up_band=[True, False, False, False, False, False, False],
        down_band=[False, False, True, False, False, False, False],
    )
    # 追加一根 15:00 的收盘占位（价格沿最后一根，只表达"数据到这里才结束"）
    tail = pl.DataFrame(
        {
            "symbol": [SYMBOL],
            "date": [TRADE_DATE],
            "datetime": [datetime(2026, 1, 5, 15, 0)],
            "price": [float(prices[-1])],
            "vwap": [10.0],
            "gate_enter_band_up": [False],
            "gate_enter_band_down": [False],
            "signal_intraday_avg_cross_up": [False],
            "signal_intraday_avg_cross_down": [False],
        }
    )
    return pl.concat([frame, tail]).sort(["symbol", "datetime"])


def _completed_frame():
    """造一个**一定产生 completed** 的帧：B 路径首次进上带，随后价格跌回均价下方。

    价格序列（均价恒定 10.0，ε=0.3% → 上带 10.03）：

    | idx | price | 说明 |
    |---|---|---|
    | 0 | 10.10 | 首次进上带 → 确认 bar |
    | 1 | 10.10 | 成交（确认 bar 的下一根）→ fill_px=10.10 |
    | 2 |  9.90 | 跌到均价下方 → crossed → completed，exit_px=9.90 |

    正 T 的 B 路径（`sell_first`）卖出腿是 leg1，成交价 10.10 卖、9.90 买回：
    毛价差 = (10.10 - 9.90) × 100 = 20.0（正数，符合"卖高买低才赚"）。
    """
    return _day(
        [10.10, 10.10, 9.90],
        vwap=10.0,
        up_band=[True, False, False],
    )


# ============================================== ① 名义口径（唯一实现）


def test_notional_is_avg_of_two_legs_times_qty():
    """名义 = 两腿成交价算术平均 × qty。手算：(10.0 + 10.4)/2 × 100 = 1020。"""
    assert notional_avg_legs(10.0, 10.4, 100) == pytest.approx(1020.0)
    # 与净价差同量纲：两腿价格各变动 0.2，净价差变动 0.2×100=20，名义变动 0
    #（两腿反向等幅变动时均价不变）—— 这条恒等式正是"同一个价×量量纲"的体现。
    assert notional_avg_legs(10.2, 10.2, 100) == pytest.approx(
        notional_avg_legs(10.0, 10.4, 100)
    )


def test_notional_is_symmetric_in_legs():
    """哪条腿是卖出腿不影响名义 —— 否则成本会随模式带上偏置。"""
    assert notional_avg_legs(9.9, 10.1, 100) == pytest.approx(
        notional_avg_legs(10.1, 9.9, 100)
    )


def test_notional_has_exactly_one_implementation():
    """全包只有一处**真的算**「两腿均价 × qty」。

    第二份实现是这一层最危险的失败模式：两个数都"有道理"，所以谁也不会去查，
    最后报告里的成本数字取决于哪段代码先跑。

    这里用 AST 而不是字符串搜索：文档字符串里的公式、`leg1, leg2, qty = ...` 解包、
    报告里写给读者看的公式串，都不是实现 —— 字符串搜索会把它们全判成违规，
    然后为了过测就去删注释，反而把该写清的口径删掉了。

    判据：某个函数体内出现了「两数相加后除以 2」的除法节点。
    """
    def divides_avg_sum(func: ast.AST) -> bool:
        for node in ast.walk(func):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            left = node.left
            # 形态: (a + b) / 2
            if isinstance(left, ast.BinOp) and isinstance(left.op, ast.Add):
                if isinstance(node.right, ast.Constant) and node.right.value == 2:
                    return True
        return False

    offenders: list[str] = []
    for path in PKG_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and divides_avg_sum(node):
                if path.name == "costs.py" and node.name == "notional_avg_legs":
                    continue
                offenders.append(f"{path.name}::{node.name}")
    assert not offenders, f"名义口径出现了第二处实现: {offenders}"


@pytest.mark.parametrize("tier", COST_TIERS_SENSITIVITY)
def test_cost_amount_follows_rate_times_notional(tier):
    """`cost_amount = rate_pct/100 × notional`，五档逐一核对。"""
    leg1, leg2, qty = 10.0, 10.4, 100
    expected = (COST_RATE_PCT_BY_TIER[tier] / 100.0) * notional_avg_legs(leg1, leg2, qty)
    assert cost_amount_for_tier(tier, leg1_price=leg1, leg2_price=leg2, qty=qty) == pytest.approx(
        expected
    )


def test_zero_tier_returns_exact_zero():
    """`0bp` 必须返回**精确** 0.0 —— 闸门判据是 `== 0`，浮点残渣会让它变成"0bp 却扣了钱"。"""
    amount = cost_amount_for_tier("0bp", leg1_price=123.456, leg2_price=789.012, qty=100)
    assert amount == 0.0
    assert str(amount) == "0.0"
    # 与"直接走乘法"结果一致 —— 这条分支是**等价**的（删掉它行为不变）。
    # 这么写是为了把话说清：不假装它在防什么浮点残渣，也不假装测得出它被删。
    assert amount == (0.0 / 100.0) * notional_avg_legs(123.456, 789.012, 100)


def test_pending_tier_is_not_in_sensitivity_grid():
    """`design_pending` 不是一档成本，不许出现在跑表行里。"""
    assert "design_pending" not in COST_RATE_PCT_BY_TIER
    assert "design_pending" not in COST_TIERS_SENSITIVITY
    with pytest.raises(KeyError, match="design_pending"):
        cost_amount_for_tier("design_pending", leg1_price=10.0, leg2_price=10.0, qty=100)


def test_grid_matches_whitelist_exactly():
    """档位网格 = 5b 白名单去掉 pending，逐档对齐，不多不少。"""
    assert set(COST_RATE_PCT_BY_TIER) == C.COST_BP_TIERS - {"design_pending"}
    costs_mod.assert_grid_matches_whitelist()


def test_grid_selfcheck_catches_drift():
    """导入期自检必须真的能发现漂移 —— 否则它只是个装饰。"""
    saved = dict(costs_mod.COST_RATE_PCT_BY_TIER)
    try:
        # 少一档
        costs_mod.COST_RATE_PCT_BY_TIER.pop("20bp")
        with pytest.raises(AssertionError, match="网格缺"):
            costs_mod.assert_grid_matches_whitelist()
        # 多一个白名单里没有的
        costs_mod.COST_RATE_PCT_BY_TIER["20bp"] = 0.20
        costs_mod.COST_RATE_PCT_BY_TIER["25bp"] = 0.25
        with pytest.raises(AssertionError, match="白名单没有"):
            costs_mod.assert_grid_matches_whitelist()
    finally:
        costs_mod.COST_RATE_PCT_BY_TIER.clear()
        costs_mod.COST_RATE_PCT_BY_TIER.update(saved)
    costs_mod.assert_grid_matches_whitelist()


def test_rate_values_are_pinned():
    """费率数值逐键钉死。改任何数字都该让这批断言变红 —— 它是研究结论的直接输入。"""
    assert COST_RATE_PCT_BY_TIER == {
        "0bp": 0.0,
        "5bp": 0.05,
        "10bp": 0.10,
        "15bp": 0.15,
        "20bp": 0.20,
    }
    assert COST_TIERS_SENSITIVITY == ("0bp", "5bp", "10bp", "15bp", "20bp")


# ============================================== ② _settle 接档（真扣）


def _opened_record() -> TRoundRecord:
    """造一条处于 `open_leg1` 的记录，供直接测 `_settle`。

    **注意传的必须是 `open_leg1`**：`_settle` 自己会做
    `transition(opened, "open_leg2", ...)`。传一条已经在 `open_leg2` 的记录进去，
    就会撞上「不允许从 open_leg2 转移到 open_leg2」—— 那是夹具的错，不是被测代码的错。
    """
    return TRoundRecord(
        t_round_id="tr.5c.demo",
        symbol=SYMBOL,
        trade_date=TRADE_DATE,
        mode=C.SELL_FIRST,
        leg1_side="sell",
        leg2_side="buy",
        virtual_position_flag=True,
        position_tag="executable",
        position_qty=100,
        sellable_old_qty=100,
        tag_reason="离线虚拟底仓(纸面)",
    )


def _open_leg2_record() -> TRoundRecord:
    """处于 `open_leg2` 的记录（leg1 已成交）—— 供直接测 `complete_round` 的回归用例。"""
    opened = _opened_record().with_fields(
        leg1_status="filled", leg1_price=10.10, leg1_qty=100, leg1_source_lot="old_lot"
    )
    return transition(opened, "open_leg2", leg2_status="filled", leg2_source_lot="old_lot")


@pytest.mark.parametrize("tier", COST_TIERS_SENSITIVITY)
def test_settle_registers_each_tier_through_complete_round(tier):
    """T1 —— 五档都经 `complete_round` 合法登记；`net = gross - amount`。"""
    out = backtest_mod._settle(
        _opened_record(),
        outcome="completed",
        fill_px=10.10,
        exit_px=9.90,
        fill_ts=datetime(2026, 1, 5, 9, 31),
        exit_ts=datetime(2026, 1, 5, 9, 32),
        cost_bp_tier=tier,
    )
    assert out.round_status == "completed"
    assert out.cost_bp_tier == tier
    expected_amount = cost_amount_for_tier(tier, leg1_price=10.10, leg2_price=9.90, qty=100)
    assert out.cost_amount == pytest.approx(expected_amount)
    if tier == "0bp":
        assert out.cost_amount == 0.0
    else:
        assert out.cost_amount > 0.0, "非零档必须扣出正数（禁 silent 0）"
    assert out.net_spread == pytest.approx(out.gross_spread - expected_amount)


def test_settle_default_path_stays_design_pending():
    """T3 —— 缺省（不传档位）仍是 `design_pending` + 0，旧对照路径不受影响。"""
    out = backtest_mod._settle(
        _opened_record(),
        outcome="completed",
        fill_px=10.10,
        exit_px=9.90,
        fill_ts=datetime(2026, 1, 5, 9, 31),
        exit_ts=datetime(2026, 1, 5, 9, 32),
    )
    assert out.cost_bp_tier == "design_pending"
    assert out.cost_amount == 0.0
    assert out.net_spread == pytest.approx(out.gross_spread)


def test_settle_rejects_unknown_tier():
    """未知档位在 `_settle` 就该炸（`cost_amount_for_tier` 抛 KeyError），不会静默当 0。"""
    with pytest.raises(KeyError):
        backtest_mod._settle(
            _opened_record(),
            outcome="completed",
            fill_px=10.10,
            exit_px=9.90,
            fill_ts=datetime(2026, 1, 5, 9, 31),
            exit_ts=datetime(2026, 1, 5, 9, 32),
            cost_bp_tier="tier_a_measured",
        )


def test_settle_goes_through_complete_round_not_just_arithmetic():
    """真扣的判据不是"数字对不对"，是"有没有过闸门"。

    做法：把 `complete_round` 换成会记账的桩，确认 `_settle` 的 completed 分支
    确实调用了它、且传进去的 `RoundCost` 就是该档。如果哪天有人图省事改成
    `record.with_fields(net_spread=gross - amount)`，这条会红。
    """
    calls: list[RoundCost] = []
    original = backtest_mod.complete_round

    def spy(record, **kwargs):
        calls.append(kwargs["cost"])
        return original(record, **kwargs)

    backtest_mod.complete_round = spy
    try:
        backtest_mod._settle(
            _opened_record(),
            outcome="completed",
            fill_px=10.10,
            exit_px=9.90,
            fill_ts=datetime(2026, 1, 5, 9, 31),
            exit_ts=datetime(2026, 1, 5, 9, 32),
            cost_bp_tier="10bp",
        )
    finally:
        backtest_mod.complete_round = original
    assert len(calls) == 1, "completed 必须且只能经 complete_round 登记一次"
    assert calls[0].cost_bp_tier == "10bp"
    assert calls[0].cost_amount > 0


@pytest.mark.parametrize("tier", ["5bp", "10bp", "15bp", "20bp"])
def test_silent_zero_still_rejected_after_5c(tier):
    """T2 —— **禁 silent 0 回归**：非零档 + `amount=0` 仍拒（5b 契约没被这一拍改坏）。"""
    with pytest.raises(TRoundValidationError, match="必须 > 0"):
        complete_round(
            _open_leg2_record(),
            leg1_price=10.10, leg2_price=9.90, qty=100,
            leg1_source_lot="old_lot", leg2_source_lot="old_lot",
            cost=RoundCost(cost_amount=0.0, cost_bp_tier=tier),
        )


def test_whitelist_membership_unchanged_after_5c():
    """`COST_BP_TIERS` 成员集合本拍**未改**（5b 已钉死）。"""
    assert C.COST_BP_TIERS == frozenset(
        {"design_pending", "0bp", "5bp", "10bp", "15bp", "20bp"}
    )


# ============================================== ③ 五档跑表


def test_sensitivity_has_five_rows_in_order():
    """五档齐、按 0→20 升序，不多不少。"""
    rows = build_cost_sensitivity(run_mode_a_backtest(_completed_frame()))
    assert [r.cost_bp_tier for r in rows] == list(COST_TIERS_SENSITIVITY)


def test_sensitivity_columns_are_the_pinned_eight():
    """T4 —— §2.3 八列，顺序与名字都钉死。"""
    assert SENSITIVITY_COLUMNS == (
        "cost_bp_tier",
        "cost_rate_pct",
        "cost_amount_sum",
        "expectancy_gross",
        "expectancy_net",
        "n_rounds_opened",
        "completed_rate",
        "footnote_retail_assumption",
    )
    row = build_cost_sensitivity(run_mode_a_backtest(_completed_frame()))[0]
    assert tuple(row.as_row()) == SENSITIVITY_COLUMNS, "导出顺序必须与列名常量一致"


def test_footnote_text_is_byte_pinned():
    """T4 —— 散户假设脚注文案固定，一字不改。"""
    assert FOOTNOTE_RETAIL_ASSUMPTION == "散户 0.1–0.15% 为假设口径，非唯一真实成本"
    for row in build_cost_sensitivity(run_mode_a_backtest(_completed_frame())):
        assert row.as_row()["footnote_retail_assumption"] == FOOTNOTE_RETAIL_ASSUMPTION


def test_retail_assumption_tiers_are_10_and_15bp():
    assert RETAIL_ASSUMPTION_TIERS == ("10bp", "15bp")


def test_zero_tier_net_equals_gross():
    """`0bp` 扣的是 0 —— 所以它的净期望值**必然**等于毛期望值。

    这是跑表正确性的第一道自检：如果这条不成立，说明毛或净有一个算错了。
    """
    summary = run_mode_a_backtest(_completed_frame())
    rows = {r.cost_bp_tier: r for r in build_cost_sensitivity(summary)}
    zero = rows["0bp"]
    assert zero.cost_amount_sum == 0.0
    assert zero.expectancy_net == pytest.approx(zero.expectancy_gross)


def test_net_expectancy_decreases_monotonically_with_tier():
    """档位越高，净期望值越低（成本是纯减法），且毛值每行相同。"""
    rows = build_cost_sensitivity(run_mode_a_backtest(_completed_frame()))
    gross = {r.expectancy_gross for r in rows}
    assert len(gross) == 1, "毛期望值不该随成本假设变"
    nets = [r.expectancy_net for r in rows]
    assert nets == sorted(nets, reverse=True), f"净期望值应随档位单调下降, 实际 {nets}"


def test_net_gross_gap_equals_amount_over_rounds():
    """`毛 − 净 = 该档合计扣减 / 分母`。三处数字必须自洽。"""
    rows = build_cost_sensitivity(run_mode_a_backtest(_completed_frame()))
    for r in rows:
        expected_gap = r.cost_amount_sum / r.n_rounds_opened
        assert (r.expectancy_gross - r.expectancy_net) == pytest.approx(expected_gap)


def test_cost_amount_sum_matches_hand_computed():
    """手算：一轮 completed，(10.10 卖 / 9.90 买) × 100 股，名义 = 1000.0。

    `10bp` → 0.10% × 1000.0 = 1.0；`20bp` → 2.0。
    """
    rows = {r.cost_bp_tier: r for r in build_cost_sensitivity(run_mode_a_backtest(_completed_frame()))}
    assert rows["0bp"].cost_amount_sum == 0.0
    assert rows["5bp"].cost_amount_sum == pytest.approx(0.5)
    assert rows["10bp"].cost_amount_sum == pytest.approx(1.0)
    assert rows["15bp"].cost_amount_sum == pytest.approx(1.5)
    assert rows["20bp"].cost_amount_sum == pytest.approx(2.0)


def test_n_rounds_excludes_skipped_and_includes_unreverted():
    """分母口径与主报告一致：含 `limit_locked`/未回摆，不含 `skipped_quota`。

    **必须用有非 completed 轮的帧**才会失败。只用一条 completed 轮时
    `rounds == completed` 恒等，把分母写成 completed 也照样过 —— 那样的断言是假的。
    """
    summary = run_mode_a_backtest(_mixed_frame())
    main_rounds = sum(g.counts.rounds for g in summary.groups)
    main_completed = sum(g.counts.completed for g in summary.groups)
    # 这条帧里 completed 与总轮次必须**不相等**, 否则本用例又退化成恒等式
    assert main_completed < main_rounds, (
        f"夹具失效: completed({main_completed}) 应小于 rounds({main_rounds})"
    )
    for r in build_cost_sensitivity(summary):
        assert r.n_rounds_opened == main_rounds
        assert r.n_rounds_opened != main_completed or main_completed == main_rounds


def test_mtm_part_is_carried_into_net_expectancy():
    """未回摆轮的盯市盈亏必须**留在**净期望值的分子里（只是不扣成本）。

    只用一条 completed 轮的帧测不出这条：那种帧 `mtm_total == 0`，漏掉 mtm 与带上
    mtm 结果相同。所以这里必须有**真的 mtm 非零**的轮次。
    """
    summary = run_mode_a_backtest(_mixed_frame())
    mtm_total = sum(float(r.mtm_pnl) for r in summary.records if r.mtm_pnl is not None)
    assert mtm_total != 0.0, "夹具失效: 这条帧应至少有一轮可盯市且 mtm 非零"

    n_rounds = sum(g.counts.rounds for g in summary.groups)
    completed_nets = [
        float(r.net_spread) for r in summary.records
        if r.round_status == "completed" and r.net_spread is not None
    ]
    expected = (sum(completed_nets) + mtm_total) / n_rounds
    zero = {r.cost_bp_tier: r for r in build_cost_sensitivity(summary)}["0bp"]
    assert zero.expectancy_net == pytest.approx(expected)
    # 若实现漏掉 mtm, 就会等于下面这个数 —— 显式断言两者不同, 免得再退化成恒等
    without_mtm = sum(completed_nets) / n_rounds
    assert zero.expectancy_net != pytest.approx(without_mtm)


def test_completed_rate_matches_main_summary():
    """`completed_rate` 与主报告逐格加总一致（同一批轮次，不该有两套完成率）。"""
    summary = run_mode_a_backtest(_completed_frame())
    total_rounds = sum(g.counts.rounds for g in summary.groups)
    total_completed = sum(g.counts.completed for g in summary.groups)
    expected = total_completed / total_rounds if total_rounds else None
    for r in build_cost_sensitivity(summary):
        assert r.completed_rate == pytest.approx(expected)


def test_empty_summary_still_yields_five_rows_with_none():
    """**表不能空**：零样本也要出五行，数值 None / 0。

    空表会让读者以为"这一步没跑"，而真相是"跑了，样本是零"。
    """
    rows = build_cost_sensitivity(run_mode_a_backtest(pl.DataFrame()))
    assert len(rows) == 5
    for r in rows:
        assert r.n_rounds_opened == 0
        assert r.expectancy_gross is None
        assert r.expectancy_net is None
        assert r.completed_rate is None
        assert r.cost_amount_sum == 0.0


def test_replay_rejects_incomplete_record():
    """重放遇到缺腿价的 completed 记录必须报错，不能当 0 —— 当 0 会让成本凭空消失。"""
    rec = _open_leg2_record()
    broken = rec.with_fields(round_status="completed", gross_spread=20.0,
                             net_spread=20.0, net_spread_realized=True,
                             count_in_realized_kpi=True, leg1_price=None)
    with pytest.raises(ValueError, match="缺腿价"):
        sens_mod._replay_completed_net(broken, "10bp")


def test_replay_does_not_touch_mtm():
    """未回摆轮的盯市盈亏**不扣**成本 —— 它不是一次真实平仓。

    做法：造一个只含未回摆轮的摘要，五档的净期望值应**全部相等**且等于毛值。
    """
    frame = _day([10.10, 10.10, 10.20], vwap=10.0, up_band=[True, False, False])
    # 价格一路没跌回均价下方 → unreverted_at_cutoff（数据到 09:32 就断 → data_truncated）
    # 为了让"走完一天"成立，必须让最后一根过 14:57：
    frame = frame.with_columns(
        pl.when(pl.col("datetime") == pl.col("datetime").max())
        .then(pl.lit(datetime(2026, 1, 5, 15, 0)))
        .otherwise(pl.col("datetime"))
        .alias("datetime")
    ).sort(["symbol", "datetime"])
    summary = run_mode_a_backtest(frame)
    rows = build_cost_sensitivity(summary)
    for r in rows:
        assert r.cost_amount_sum == 0.0, "没有 completed 就不该扣任何成本"
    nets = {r.expectancy_net for r in rows}
    assert len(nets) == 1, f"mtm 不该被扣成本, 各档净期望值应相同, 实际 {nets}"


# ============================================== ④ Regime 附报


def test_regime_report_has_both_keys():
    """T6 —— 两个分组键都在，且都有分桶与单元格。"""
    regime = build_regime_report()
    payload = regime.as_dict()
    assert set(payload) >= {"regime_day", "near_limit", "note"}
    assert payload["regime_day"]["buckets"] == ["up", "flat", "down"]
    assert payload["near_limit"]["buckets"] == ["near", "normal"]


def test_regime_is_data_pending_not_fabricated():
    """T6 —— 缺数据来源就明说 `data_pending`，计数 0，期望值 None。**不编造**。"""
    payload = build_regime_report().as_dict()
    assert payload["regime_day"]["regime_source"] == "data_pending"
    assert payload["near_limit"]["regime_source"] == "data_pending"
    for key in ("regime_day", "near_limit"):
        for cell in payload[key]["cells"]:
            assert cell["expectancy_net"] is None
            assert cell["n_rounds"] == 0


def test_regime_cells_cover_all_buckets_times_five_tiers():
    """结构铺满：分桶 × 五档，将来接上数据不用改报告结构。"""
    payload = build_regime_report().as_dict()
    for key, buckets in (("regime_day", 3), ("near_limit", 2)):
        cells = payload[key]["cells"]
        assert len(cells) == buckets * len(COST_TIERS_SENSITIVITY)
        assert {c["cost_bp_tier"] for c in cells} == set(COST_TIERS_SENSITIVITY)


def test_regime_does_not_block_main_table():
    """Regime pend 了，主表五行照出 —— 不挡。"""
    summary = run_mode_a_backtest(_completed_frame())
    payload = build_sensitivity_payload(summary)
    assert len(payload["rows"]) == 5
    assert payload["regime"]["regime_day"]["regime_source"] == "data_pending"


def test_regime_near_limit_inherits_limit_lock_pending():
    """`near_limit` 的 pending 与 `LIMIT_LOCK_SOURCE_PENDING` 是同一条理由，不是另立一套。"""
    assert C.LIMIT_LOCK_SOURCE_PENDING == "data_pending"
    assert sens_mod.REGIME_SOURCE_NEAR_LIMIT == "data_pending"


# ============================================== ⑤ 报告接线


def test_report_meta_carries_cost_sensitivity(tmp_path):
    """报告 JSON 里 `cost_sensitivity` 段齐全：列名、五行、Regime、名义口径、脚注。"""
    summary = run_mode_a_backtest(_completed_frame())
    report = write_offline_report(
        out_dir=tmp_path / "out", feature_frame=_completed_frame(), summary=summary
    )
    payload = json_load(report.summary_path)
    block = payload["cost_sensitivity"]
    assert block["columns"] == list(SENSITIVITY_COLUMNS)
    assert [r["cost_bp_tier"] for r in block["rows"]] == list(COST_TIERS_SENSITIVITY)
    assert block["footnote_retail_assumption"] == FOOTNOTE_RETAIL_ASSUMPTION
    assert "leg1_price + leg2_price" in block["notional_definition"]
    assert block["retail_assumption_tiers"] == ["10bp", "15bp"]
    assert "regime" in block


def test_cost_gate_scope_states_5c_is_wired(tmp_path):
    """T5 —— `cost_gate_scope` 必须说清：5b 契约 / 5c 已接档 / 两套口径不可混 /
    五档 / 名义口径 / G1 pending / 仍禁「信号可做」。"""
    summary = run_mode_a_backtest(_completed_frame())
    report = write_offline_report(
        out_dir=tmp_path / "out2", feature_frame=_completed_frame(), summary=summary
    )
    scope = json_load(report.summary_path)["cost_gate_scope"]
    # 逐条检查**成句的概念**，不是零散 token。M26/M27 变异第一轮绿，就是因为
    # "5c" 在 scope 里出现两次（"5c 已接档" 与 "G1 仍为 design_pending" 那半句里
    # 也可能带别的编号），删掉真正的那句仍然能匹配到别处。所以这里断言整句。
    required_sentences = {
        "5c 已接档": "跑表批次" ,
        "同一批轮次在 0bp/5bp/10bp/15bp/20bp 五档下": "五档齐",
        "每档经 complete_round 真扣": "真扣",
        "((leg1_price + leg2_price) / 2.0) * qty": "名义口径",
        "未扣任何成本的对照口径": "主口径边界",
        "成本只对 completed 轮扣减": "扣减范围",
        "mtm_pnl 不扣成本": "mtm 不扣",
        # G1 这句必须**整句**匹配。第一版只断言 "仍为 design_pending" 就绿了，
        # 因为 scope 里 `design_pending` 出现三次（白名单前缀 / 主摘要口径 / G1），
        # 把 G1 那半句改成"已实装"仍能在别处匹配到 —— 那是假测试。
        "精确拆分费率表(佣金/印花税/滑点, G1)仍为 design_pending": "G1 仍 pending",
        "散户 0.1–0.15% 为假设口径，非唯一真实成本": "散户假设",
        "仍禁止据此宣称信号可做": "禁令本身",
    }
    for sentence, why in required_sentences.items():
        assert sentence in scope, f"cost_gate_scope 缺少 {why!r}（{sentence!r}）"


def test_no_banned_conclusion_in_report(tmp_path):
    """报告里不许出现"信号可做 / ε 最优 / 可交易"这类结论性措辞。

    注意 `cost_gate_scope` 里会出现"仍禁止据此宣称信号可做"—— 那是**禁令本身**，
    是必须写的。所以这里查的是这些词不带否定语境的独立出现：逐条检查它们前面
    是否紧邻"禁/不许/不得/勿"。
    """
    summary = run_mode_a_backtest(_completed_frame())
    report = write_offline_report(
        out_dir=tmp_path / "out3", feature_frame=_completed_frame(), summary=summary
    )
    blob = report.summary_path.read_text(encoding="utf-8")
    for phrase in ("信号可做", "ε 最优", "可交易"):
        idx = blob.find(phrase)
        while idx != -1:
            # 窗口取 40 字: 本报告的禁令句是
            #   "仍禁止据此宣称信号可做 / ε 最优 / 可交易"
            # 从 "禁" 到 "ε 最优" 隔着 "止据此宣称信号可做 / " 十几个字符, 窗口
            # 开太小就会把**禁令本身**误判成结论。这里查的是同一句里有没有否定词,
            # 不是"紧邻三个字"。
            window = blob[max(0, idx - 40): idx]
            assert any(
                neg in window for neg in ("禁", "不许", "不得", "勿", "不")
            ), f"{phrase!r} 出现在非否定语境: ...{window}{phrase}"
            idx = blob.find(phrase, idx + len(phrase))


def json_load(path: Path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


# ============================================== ⑥ 静态隔离


def test_sensitivity_module_does_not_redefine_whitelist():
    """5c 不许自己另写白名单成员 —— 白名单唯一来源是 `constants.COST_BP_TIERS`。"""
    tree = ast.parse((PKG_DIR / "costs.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "COST_BP_TIERS":
                    raise AssertionError("costs.py 不许重新定义 COST_BP_TIERS")


def test_complete_round_amount_semantics_untouched():
    """`complete_round` 的金额语义本拍未改：源码里仍有那三条规则。"""
    src = inspect.getsource(complete_round)
    assert "COST_BP_TIERS" in src
    assert "必须是非负数" in src
    assert "必须为 0" in src
    assert "必须 > 0" in src


def test_settle_signature_gained_only_cost_tier():
    """`_settle` 只多了一个 `cost_bp_tier` 参数，其余调用约定不变。"""
    params = list(inspect.signature(backtest_mod._settle).parameters)
    assert "cost_bp_tier" in params
    assert params[-1] == "cost_bp_tier", "新参数应在末尾（带缺省，向后兼容）"
