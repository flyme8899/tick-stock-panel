"""模式 A 纸面回测（批次 20261025hfeat 任务③）。

守五件事：

1. **每个结局一个桶**：`completed` / `unreverted_at_cutoff` / `data_truncated` /
   `limit_locked` 各自计数，不合并
2. **只有 `completed` 进已实现 KPI** —— 这条不是本包新加的，是
   `TRoundRecord.__post_init__` 与 `complete_round()` 早就钉死的，本包不绕过它
3. **卖出腿必须是 `old_lot`**：两种模式都查，标成 `same_day_buy` 那一轮到不了 `completed`
4. **路径 A 与 B 的格子互不覆盖**（各自计数，不合成一个"门禁通过"）
5. **报告含 `dates` / `inputs` / `version` / 免责声明**

测试数据是**手工灌标记**的：直接给 `gate_enter_band_*` 与 `signal_intraday_avg_cross_*`
布尔列，绕开 `features` 的派生。这样每个场景的期望值是能手算出来的，而不是
"跑一遍看结果抄下来"——后者等于用实现定义正确性。
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.t_rounds import constants as C
from app.t_rounds.offline import run_mode_a_backtest, write_offline_report
from app.t_rounds.offline.backtest import (
    COST_DESIGN_PENDING,
    LOT_SIZE,
    PATH_A,
    PATH_B,
    PATHS,
    VIRTUAL_LOT_ASSUMPTION,
    _confirmed_entry_bar,
    _confirmed_entry_bars,
    entry_side,
)

SYMBOL = "600000.SH"
C_TIME_CUTOFF = time(14, 57)
TRADE_DATE = date(2026, 1, 5)


def _day(
    prices: list[float],
    *,
    vwap: float | None = None,
    up_band: list[bool] | None = None,
    down_band: list[bool] | None = None,
    up_cross: list[bool] | None = None,
    down_cross: list[bool] | None = None,
    symbol: str = SYMBOL,
    trade_date: date = TRADE_DATE,
    start: datetime | None = None,
    close_at: datetime | None = None,
) -> pl.DataFrame:
    """造一日 bar + 手工灌入 A/B 标记列。

    `vwap` 传单值表示全日均价恒定，便于手算 ε 带位置。

    ## 为什么需要 `close_at`

    `_simulate_one` 用"当日最后一根 bar 的时刻有没有过 14:57"来区分
    `unreverted_at_cutoff`（时段走完、可盯市）和 `data_truncated`（数据提前断了）。
    如果夹具只造 6 根 09:30..09:35 的 bar 就收工，模拟器看到"最后一根在 09:35"，
    只能判定成数据截断 —— 那是**正确**的判定，不是 bug。

    所以要测"走完了一整天"，就得让数据真的铺到 14:57 之后：

    - `close_at=datetime(..., 15, 0)` → 末尾追加一根 15:00 的**收盘占位 bar**，
      价格沿用最后一根（它只用来表达"数据到这里才结束"，不参与决策与盯市，
      因为 15:00 > 14:57 两者都会跳过它）。
    - 传 `None`（默认）→ 数据就到最后一根为止，即"数据提前断了"。
    """
    start = start or datetime(2026, 1, 5, 9, 30)
    times = [start + timedelta(minutes=i) for i in range(len(prices))]
    values = [float(p) for p in prices]
    if close_at is not None:
        if close_at <= times[-1]:
            raise ValueError("close_at 必须晚于最后一根 bar")
        times.append(close_at)
        values.append(values[-1])
    n = len(times)
    vwap_seq = list(values) if vwap is None else [vwap] * n
    rows = []
    for i in range(n):
        rows.append(
            {
                "symbol": symbol,
                "date": trade_date,
                "datetime": times[i],
                "price": values[i],
                "vwap": float(vwap_seq[i]),
            }
        )
    pad = n - len(prices)
    up_band = (up_band or [False] * len(prices)) + [False] * pad
    down_band = (down_band or [False] * len(prices)) + [False] * pad
    up_cross = (up_cross or [False] * len(prices)) + [False] * pad
    down_cross = (down_cross or [False] * len(prices)) + [False] * pad
    frame = pl.DataFrame(rows)

    return frame.with_columns(
        [
            pl.Series("gate_enter_band_up", up_band, dtype=pl.Boolean),
            pl.Series("gate_enter_band_down", down_band, dtype=pl.Boolean),
            pl.Series("signal_intraday_avg_cross_up", up_cross, dtype=pl.Boolean),
            pl.Series("signal_intraday_avg_cross_down", down_cross, dtype=pl.Boolean),
        ]
    ).sort(["symbol", "datetime"])


def _group(summary, mode: str, path: str):
    for g in summary.groups:
        if g.mode == mode and g.path == path:
            return g
    raise AssertionError(f"摘要里没有 {mode}/{path} 这一格")


# ---------------------------------------------------------------- 基本结构


def test_empty_frame_yields_all_zero_groups() -> None:
    """空输入 → 四个格子都在，全 0，不抛。"""
    summary = run_mode_a_backtest(pl.DataFrame())
    assert len(summary.groups) == 4
    assert {(g.mode, g.path) for g in summary.groups} == {
        (m, p) for m in ("sell_first", "buy_first") for p in PATHS
    }
    for g in summary.groups:
        assert g.counts.rounds == 0
        assert g.counts.resolution_rate is None, "rounds=0 时完成率应是 None 而不是 0"
        assert g.realized_net_spread == 0.0
    assert summary.records == ()


def test_summary_carries_pinned_version() -> None:
    """摘要版本串必须来自常量，等于 `design_pin_v1+eps_pct:0.30`。"""
    summary = run_mode_a_backtest(pl.DataFrame())
    assert summary.version == C.design_version()
    assert summary.version == "design_pin_v1+eps_pct:0.30"
    assert summary.epsilon_pct == C.EPSILON_VALUE_PCT == 0.30


def test_group_and_path_are_orthogonal() -> None:
    """模式（正/倒）× 路径（A/B）四个格子都在，不合并。"""
    summary = run_mode_a_backtest(pl.DataFrame())
    modes = {g.mode for g in summary.groups}
    paths = {g.path for g in summary.groups}
    assert modes == {"sell_first", "buy_first"}
    assert paths == {PATH_A, PATH_B}


# ---------------------------------------------------------------- 分桶


def test_completion_lands_in_completed_bucket() -> None:
    """冲高回落：上带卖出 → 跌破均价买回 → `completed`，净价差可手算。

    价格 10.00 → 10.02 → 10.06 → 10.05 → 10.01 → 9.98，均价恒 10.00。
    路径 B：首次进上带在 10.06（索引 2）—— 那是**确认 bar**。成交发生在它的
    **下一根**（索引 3，价 10.05）—— 不能用 10.06，那是"看到收盘价就当成交"。
    之后第一根跌到均价及以下的是 9.98（索引 5）。
    毛价差**按金额算**：(10.05 − 9.98) × 100 股 = 7.00 元。
    本批次成本口径是 `design_pending`（`cost_amount=0.0`），所以净 = 毛。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 10.01, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "sell_first", PATH_B)

    assert g.counts.completed == 1
    assert g.counts.unreverted_at_cutoff == 0
    assert g.counts.data_truncated == 0
    assert g.counts.rounds == 1
    assert g.counts.resolution_rate == 1.0
    assert g.realized_gross_spread == pytest.approx(7.00, abs=1e-9)
    assert g.realized_net_spread > 0
    assert len(summary.records) == 1
    assert summary.records[0].round_status == "completed"


def test_no_exit_before_cutoff_lands_in_unreverted() -> None:
    """单边上涨、再也没有回到均价，且**数据走完了** → `unreverted_at_cutoff`。

    这一批（②）把名字修对了：旧代码把"时段走完"记成 `abandoned`、把"数据断了"
    记成 `incomplete`，与它自己的 docstring 正好对调。所以这条用例同时也是
    "盯市算在哪个桶上"的守卫 —— 桶名错了，mtm 就会算在数据截断上。
    """
    frame = _day(
        [10.00, 10.02, 10.05, 10.08, 10.10, 10.12],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
        close_at=datetime(2026, 1, 5, 15, 0),  # 数据走到收盘 —— 区别就在这里
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "sell_first", PATH_B)

    assert g.counts.completed == 0
    assert g.counts.unreverted_at_cutoff == 1
    assert g.counts.data_truncated == 0
    assert g.counts.rounds == 1
    assert g.counts.resolution_rate == 0.0
    # 关键：未回摆不贡献任何**已实现**价差
    assert g.realized_net_spread == 0.0
    assert g.realized_gross_spread == 0.0

    record = summary.records[0]
    assert record.round_status == "unreverted_at_cutoff"
    assert record.count_in_realized_kpi is False
    assert record.net_spread is None


def test_only_completed_counts_in_realized_kpi() -> None:
    """**KPI 硬规则**：只有 `completed` 的 `count_in_realized_kpi` 为 True。

    这条是本批次的核心不变量。如果哪天有人让 `incomplete` 也进 KPI，报告的
    "已实现"就变成了"试图实现"，而那个数会被当成成绩。
    """
    # 一天完成、一天没完成
    d1 = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        trade_date=date(2026, 1, 5),
        start=datetime(2026, 1, 5, 9, 30),
        symbol="AAA",
    )
    d2 = _day(
        [10.00, 10.02, 10.06, 10.10, 10.12],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        trade_date=date(2026, 1, 6),
        start=datetime(2026, 1, 6, 9, 30),
        symbol="BBB",
        close_at=datetime(2026, 1, 6, 15, 0),
    )
    summary = run_mode_a_backtest(pl.concat([d1, d2]))

    by_status = {}
    for rec in summary.records:
        by_status.setdefault(rec.round_status, []).append(rec)

    assert "completed" in by_status
    assert "unreverted_at_cutoff" in by_status
    for rec in by_status["completed"]:
        assert rec.count_in_realized_kpi is True
    for status in ("unreverted_at_cutoff", "data_truncated", "limit_locked"):
        for rec in by_status.get(status, []):
            assert rec.count_in_realized_kpi is False, f"{status} 进了已实现 KPI"

    # 摘要口径与记录一致：已实现只由 completed 贡献
    g = _group(summary, "sell_first", PATH_B)
    assert g.kpi_realized_only_completed is True
    assert g.counts.completed == len(by_status["completed"])


def test_net_spread_matches_gross_minus_cost() -> None:
    """净价差 = 毛价差 − 成本（成本按 `RoundCost` 的设计口径）。"""
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)
    rec = summary.records[0]
    assert rec.round_status == "completed"
    assert rec.gross_spread is not None and rec.net_spread is not None
    assert rec.net_spread == pytest.approx(
        rec.gross_spread - (rec.cost_amount or 0.0), abs=1e-9
    )
    g = _group(summary, "sell_first", PATH_B)
    assert g.realized_net_spread == pytest.approx(rec.net_spread, abs=1e-9)


def test_realized_aggregate_excludes_incomplete_rounds() -> None:
    """**已实现汇总只累加 `completed`**：未完成的轮次一个子儿都不许混进去。

    这条是上面 `test_only_completed_counts_in_realized_kpi` 的**行为级**补充。
    那边断言的是 `count_in_realized_kpi` 字段，而那个字段由
    `TRoundRecord.__post_init__` 强制 —— 也就是说那条断言测的是生产代码早已
    保障的东西，**测不出本包自己的汇总逻辑出错**。

    （这是变更前先失败验证里发现的真洞：把 `_settle` 的判据从
    `round_status == "completed"` 改成 `!= "abandoned"`，上面那条测试仍然全绿，
    因为 `incomplete` 的 `net_spread` 本来就是 None。所以必须在这里锁住
    汇总值的**具体数字**。）

    构造：同一标的两个交易日
      - 01-05 冲高回落 → completed，净额 = (10.05−9.98)×100 = 7.00
      - 01-06 单边上涨 → unreverted_at_cutoff，没有任何**已实现**价差
        （但有盯市盈亏，见 `test_unreverted_round_gets_marked_to_market`）
    若汇总把 unreverted 也算进「已实现」，数字就会偏离 7.00。
    """
    done = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        trade_date=date(2026, 1, 5),
        start=datetime(2026, 1, 5, 9, 30),
    )
    stuck = _day(
        [10.00, 10.02, 10.06, 10.10, 10.12],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        trade_date=date(2026, 1, 6),
        start=datetime(2026, 1, 6, 9, 30),
        close_at=datetime(2026, 1, 6, 15, 0),
    )
    summary = run_mode_a_backtest(pl.concat([done, stuck]))
    g = _group(summary, "sell_first", PATH_B)

    assert g.counts.completed == 1
    assert g.counts.unreverted_at_cutoff == 1

    # 已实现汇总必须**恰好**等于那一条 completed 的净额
    completed = [r for r in summary.records if r.round_status == "completed"]
    assert len(completed) == 1
    assert g.realized_net_spread == pytest.approx(completed[0].net_spread, abs=1e-9)
    assert g.realized_gross_spread == pytest.approx(7.00, abs=1e-9)

    # 均值也只按 completed 条数平均（不是按 rounds 数）
    assert g.realized_avg_net_spread == pytest.approx(completed[0].net_spread, abs=1e-9)


# ---------------------------------------------------------------- 盯市与期望值（③）


def test_unreverted_round_gets_marked_to_market() -> None:
    """未回摆的轮次要按 `ts<=14:57` 最后一根**盯市**，不能当没发生。

    为什么这条是本批次的核心：只统计 `completed` 等于只统计赢的那部分。轮次一旦
    不回摆就落进 `unreverted_at_cutoff`，而那个桶以前不算任何盈亏 —— 于是"已实现"
    永远为正。这不是策略好，是幸存者偏差。

    构造（卖出腿 = leg1，sell_first）：
      09:30 10.00 → 09:31 10.02 → 09:32 10.06（确认）→ 09:33 10.05（成交卖出）
      之后一路上涨不回头 → 买不回来 → 未回摆
      末尾 15:00 一根收盘占位（>14:57，既不决策也不盯市）
    盯市盈亏 = (卖出 10.05 − 盯市 10.20) × 100 = **−15.00**（亏损）
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 10.08, 10.12, 10.15, 10.18, 10.20],
        vwap=10.0,
        up_band=[False, False, True, False, False, False, False, False, False],
        close_at=datetime(2026, 1, 5, 15, 0),
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "sell_first", PATH_B)

    assert g.counts.completed == 0
    assert g.counts.unreverted_at_cutoff == 1
    assert g.counts.rounds == 1

    # 已实现仍然是 0 —— 没有做成的交易
    assert g.realized_net_spread == 0.0
    # 但盯市把亏损摆出来了
    assert g.mtm_rounds == 1
    assert g.mtm_net_spread == pytest.approx((10.05 - 10.20) * LOT_SIZE, abs=1e-9)
    assert g.mtm_net_spread < 0

    rec = summary.records[0]
    assert rec.round_status == "unreverted_at_cutoff"
    assert rec.mtm_pnl == pytest.approx(-15.0, abs=1e-9)
    assert rec.mtm_price == pytest.approx(10.20)
    assert rec.mtm_ts is not None and rec.mtm_ts.time() <= C_TIME_CUTOFF
    # 盯市不进已实现 KPI
    assert rec.count_in_realized_kpi is False
    assert rec.net_spread is None


def test_mtm_ignores_bars_after_cutoff() -> None:
    """盯市价必须取 `ts <= 14:57` 的最后一根，**不能**用收盘那根。

    否则会出现"策略在 14:57 认输，账却按 14:59 的价格算"。夹具末尾那根 15:00 的
    占位 bar 价格故意压到 9.00 —— 若盯市错用了它，未回摆轮会凭空变成大赚。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 10.08, 10.12],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
        close_at=datetime(2026, 1, 5, 15, 0),
    )
    # 把最后一根（15:00 占位）价格改掉，验证它不参与盯市
    frame = frame.with_columns(
        pl.when(pl.col("datetime") == datetime(2026, 1, 5, 15, 0))
        .then(pl.lit(9.00))
        .otherwise(pl.col("price"))
        .alias("price")
    )
    summary = run_mode_a_backtest(frame)
    rec = summary.records[0]
    assert rec.round_status == "unreverted_at_cutoff"
    assert rec.mtm_ts is not None
    assert rec.mtm_ts.time() <= C_TIME_CUTOFF, "盯市时刻不得晚于 14:57"
    assert rec.mtm_price == pytest.approx(10.12), "盯市价必须取 14:57 内的最后一根"
    assert rec.mtm_pnl == pytest.approx((10.05 - 10.12) * LOT_SIZE, abs=1e-9)


def test_data_truncated_round_is_not_marked() -> None:
    """数据在 14:57 之前就断了 → `data_truncated`，**不计盯市**。

    拿不到截点价格时硬算，等于把"数据缺失"当成"行情如此"。它进分母（那一轮确实
    开出去了）但不进分子 —— 期望值因此偏保守，这是有意的。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 10.08],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        # 不给 close_at: 数据到 09:34 就没了
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "sell_first", PATH_B)

    assert g.counts.unreverted_at_cutoff == 0
    assert g.counts.data_truncated == 1
    assert g.counts.rounds == 1, "数据截断的轮次**仍算开出去了**, 要进分母"
    assert g.counts.data_problem == 1

    rec = summary.records[0]
    assert rec.round_status == "data_truncated"
    assert rec.mtm_pnl is None, "数据断了的轮次不许盯市"
    assert g.mtm_rounds == 0


def test_expectancy_includes_unreverted_losses() -> None:
    """期望值 = (Σ completed 净价差 + Σ 盯市) / 实际开出轮次。

    这是本批次唯一能拿来谈"赚不赚"的数。`realized_avg_net_spread` 只数赢的部分，
    用它算等于给自己发奖。

    构造：一天做成（净 +7.00），一天未回摆（盯市 −15.00）。
      realized_avg = +7.00                ← 看起来是赚的
      expectancy   = (7 − 15) / 2 = −4.00  ← 实际是亏的
    """
    done = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        trade_date=date(2026, 1, 5),
        start=datetime(2026, 1, 5, 9, 30),
        close_at=datetime(2026, 1, 5, 15, 0),
    )
    stuck = _day(
        [10.00, 10.02, 10.06, 10.05, 10.08, 10.12, 10.15, 10.18, 10.20],
        vwap=10.0,
        up_band=[False, False, True, False, False, False, False, False, False],
        trade_date=date(2026, 1, 6),
        start=datetime(2026, 1, 6, 9, 30),
        close_at=datetime(2026, 1, 6, 15, 0),
    )
    summary = run_mode_a_backtest(pl.concat([done, stuck]))
    g = _group(summary, "sell_first", PATH_B)

    assert g.counts.completed == 1
    assert g.counts.unreverted_at_cutoff == 1
    assert g.counts.rounds == 2

    assert g.realized_avg_net_spread == pytest.approx(7.00, abs=1e-9)
    assert g.mtm_net_spread == pytest.approx(-15.00, abs=1e-9)
    assert g.expectancy == pytest.approx((7.00 - 15.00) / 2, abs=1e-9)
    assert g.expectancy < 0, "期望值必须把未回摆的亏损算进去"
    assert g.expectancy < g.realized_avg_net_spread, "期望值应比只数赢的那栏差"


def test_expectancy_is_none_when_no_rounds() -> None:
    """`rounds=0` 时期望值是 None，不是 0 —— 没样本时写 0 会被读成"打平"。"""
    summary = run_mode_a_backtest(pl.DataFrame())
    for g in summary.groups:
        assert g.expectancy is None
        assert g.counts.rounds == 0


def test_expectancy_denominator_excludes_skipped_quota() -> None:
    """`skipped_quota` 不进期望值分母 —— 它不是一个轮次。

    若分母混进被配额挡掉的候选，期望值会被稀释成一个漂亮的接近 0 的数，
    那正是"多做几次总能摊到一次成功"的错觉。

    这里**写死数字**，不拿 `counts.rounds` 去反推期望值 —— 那样等于拿待验证的
    量当基线，`rounds` 定义一改基准就跟着改，测试永远绿。
    """
    # 三组「上穿 → 进带(确认) → 下一根 10.30 成交 → 再下一根 9.90 跌破出场」。
    # 每组都能独立成一整轮；配额一日一轮，所以只有第 1 组成交，另两组记 skipped_quota。
    prices = [10.00]
    bands, cross = [False], [False]
    for _ in range(3):
        prices += [10.02, 10.06, 10.30, 9.90]
        bands += [False, True, False, False]
        cross += [True, False, False, False]
    frame = _day(
        prices,
        vwap=10.0,
        up_band=bands,
        up_cross=cross,
        close_at=datetime(2026, 1, 5, 15, 0),
    )
    summary = run_mode_a_backtest(frame)

    g = _group(summary, "sell_first", PATH_A)
    c = g.counts
    assert c.skipped_quota == 2, "夹具：另两组候选必须撞上配额"
    assert c.completed == 1
    assert c.rounds == 1, "rounds 只数真实开出的轮次"
    # (10.30 - 9.90) * 100 = 40.00 —— 写死，不给实现留漂移空间
    assert g.realized_net_spread == pytest.approx(40.0)
    assert g.expectancy == pytest.approx(40.0), (
        "期望值分母是 rounds(=1)；混进 skipped_quota(=2) 会变成 40/3=13.33"
    )


# ---------------------------------------------------------------- limit_locked（③）
# 本批次**没有**涨跌停数据, 所以这些用例守的是"结构留好了、并且没被偷偷剔除"。


def test_limit_locked_bucket_exists_and_stays_in_denominator() -> None:
    """`limit_locked` 必须在分桶里、**留在分母**、且不算数据问题。

    它现在恒为 0（没有涨跌停数据）。这条用例不是测"能算出几笔"，而是锁住三件事：

    1. 桶存在（没有因为"反正都是 0"被删掉）；
    2. 如果它非 0，`rounds` 必须把它算进去；
    3. `data_problem` 不能把它算进去 —— 那不是数据问题。
    """
    from app.t_rounds.offline.backtest import BucketCounts

    c = BucketCounts(completed=1, unreverted_at_cutoff=2, data_truncated=3, limit_locked=4)
    assert c.rounds == 1 + 2 + 3 + 4, "limit_locked 必须留在分母里"
    assert c.data_problem == 3, "data_problem 只能等于 data_truncated"

    assert BucketCounts().limit_locked == 0


def test_limit_lock_source_is_explicitly_pending() -> None:
    """涨跌停数据来源必须显式标 `data_pending`，不许按百分比阈值瞎猜。

    按"跌了 10% 就是跌停"反推会把普通的大跌错记成锁死，而那种错误**看起来**
    很合理，不会有任何告警。
    """
    from app.t_rounds.constants import LIMIT_LOCK_SOURCE_PENDING

    assert LIMIT_LOCK_SOURCE_PENDING == "data_pending"
    summary = run_mode_a_backtest(pl.DataFrame())
    assert summary.as_dict()["limit_lock_source"] == "data_pending"


def test_report_never_uses_the_word_for_removal() -> None:
    """报告里**不许**出现「剔除」。

    任务书写明 `limit_locked` 留在分母、不算数据问题。一旦文案里出现「剔除」，
    读报告的人会以为这些轮次被排除了，然后拿一个偏乐观的数去下结论。
    """
    summary = run_mode_a_backtest(pl.DataFrame())
    blob = json.dumps(summary.as_dict(), ensure_ascii=False)
    for banned in ("剔除", "已排除", "不纳入统计"):
        assert banned not in blob, f"报告文案里出现了 {banned!r}"


# ---------------------------------------------------------------- 路径 A 的开关与口径


def test_summary_reports_require_prior_cross_switch() -> None:
    """路径 A 的定义必须进摘要 —— A 是 B 的更严变体，不是平行信号。"""
    summary = run_mode_a_backtest(pl.DataFrame())
    payload = summary.as_dict()
    assert payload["require_prior_cross"] is True
    assert "require_prior_cross" in payload["path_a_definition"]
    assert "敏感度" in payload["path_a_definition"]


def test_disabling_prior_cross_makes_a_behave_like_b() -> None:
    """关掉 `require_prior_cross` 后，路径 A 与路径 B 应当看到同一个确认点。

    这条是"开关有没有真的接上"的路由检查：如果参数被吞掉，A 仍按穿越确认走，
    两个格子的成轮数就会不同。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    off = run_mode_a_backtest(frame, require_prior_cross=False)
    a_off = _group(off, "sell_first", PATH_A)
    b_off = _group(off, "sell_first", PATH_B)
    assert a_off.counts.rounds == b_off.counts.rounds


def test_a_never_has_more_rounds_than_b() -> None:
    """结构性不变量：**A 的轮次数 <= B 的轮次数**。

    A = B + require_prior_cross + 一根延迟，所以 A 的候选集必然是 B 的子集。
    若哪天 A 比 B 多，说明因果又坏了（A 抢到了 B 拿不到的信号）。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 9.98, 10.02, 10.06, 9.99],
        vwap=10.0,
        up_band=[False, False, True, False, False, True, False],
        up_cross=[False, True, False, False, True, False, False],
        close_at=datetime(2026, 1, 5, 15, 0),
    )
    summary = run_mode_a_backtest(frame)
    for mode in ("sell_first", "buy_first"):
        a = _group(summary, mode, PATH_A)
        b = _group(summary, mode, PATH_B)
        assert a.counts.rounds <= b.counts.rounds, (
            f"{mode}: A({a.counts.rounds}) 比 B({b.counts.rounds}) 还多"
        )


def test_no_fill_bar_is_counted_but_not_a_round() -> None:
    """确认 bar 在最后一根 → 没有下一根可成交 → 只记 `no_fill_bar`，不建记录。

    不能记成 `data_truncated`（数据没断），也不能凭空开一轮（没有持仓）。
    """
    frame = _day(
        [10.00, 10.02, 10.06],
        vwap=10.0,
        up_band=[False, False, True],  # 确认在最后一根
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "sell_first", PATH_B)
    assert g.counts.no_fill_bar == 1
    assert g.counts.rounds == 0, "没有下一根可成交就不是一轮 T"
    assert g.counts.data_truncated == 0, "数据没断, 不许记成截断"


# ---------------------------------------------------------------- T+1 卖出腿


def test_sell_first_sell_leg_source_lot_is_old_lot() -> None:
    """正 T 完成的轮次，leg2 来源必须是 `old_lot`（卖的是当日之前的老底仓）。

    这个字段同时在 `TRoundRecord.__post_init__` 里被硬校验：`zheng_t` 只要填了
    非 `old_lot` 的值，记录根本构造不出来。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)
    rec = summary.records[0]
    assert rec.round_status == "completed"
    # sell_first 的**卖出腿是 leg1**
    assert rec.leg1_source_lot == "old_lot"
    assert rec.leg1_status == "filled"
    assert rec.leg1_price is not None


def test_buy_first_with_same_day_buy_cannot_reach_completed() -> None:
    """把 leg2 标成 `same_day_buy` → `complete_round()` 抛，那一轮到不了 completed。

    这条不测本包自己的判断，而是**确认本包没有绕过状态机**：它调的就是
    `state_machine.complete_round()`，所以那条"正 T 第二腿必须是老底仓"的校验
    在这里同样生效。正 T 是先卖后买，卖出的必须是当日之前就持有的股份。
    """
    from app.t_rounds import (
        InvalidTRoundTransitionError,
        RoundCost,
        complete_round,
        transition,
    )
    from app.t_rounds.offline.backtest import _make_record

    record = _make_record(SYMBOL, TRADE_DATE, "buy_first", PATH_B, "2026-01-05 09:32:00")
    record = transition(record, "open_leg2", leg2_source_lot="old_lot")

    # buy_first 的卖出腿是 leg2。旧实现只查 zheng_t 的第二腿（那是买入腿），
    # 这条路径一直是绿灯 —— 本用例锁住修复。
    with pytest.raises(InvalidTRoundTransitionError):
        complete_round(
            record,
            leg1_price=9.98,
            leg2_price=10.06,
            qty=LOT_SIZE,
            leg2_source_lot="same_day_buy",  # 卖出腿不允许当日买入
            cost=RoundCost(cost_amount=0.0, cost_bp_tier=COST_DESIGN_PENDING),
        )


# ---------------------------------------------------------------- 路径 A 的确认语义
#
# 这一节守的是批次 20261025gtrounds ① —— 旧实现的前视（look-ahead）。
#
# 旧实现的 `_confirmed_entry_bars` 最后一句是:
#     [entry_ts for entry_ts in candidates if any(c > entry_ts for c in confirmed_list)]
# 它返回的是**边沿那根**，而"边沿之后确实进到 ε 带"这件事要到更晚才成立。于是
# 入场时刻早于它自己的确认依据；同时 `_simulate_one` 从边沿那根之后开始找出场，
# 出场也可以早于确认 —— 极端情形下能记出一笔"13:00 才确认、10:05 就出场"的轮次。
#
# 修法:
#   1. 返回**确认 bar**（边沿之后第一根进带），不是边沿那根;
#   2. 成交在确认 bar 的**下一根**，价取那一根的 `price`（⑤a，两条路径同口径）;
#   3. 出场只在成交之后找。


def test_path_a_entry_is_the_confirmation_bar_not_the_cross_bar() -> None:
    """路径 A 的入场依据是**确认 bar**（进带那根），不是边沿那根。

    这是旧用例 `test_path_a_entry_is_the_cross_bar_not_the_band_bar` 的**反向**
    改写：旧断言守的正是被修掉的前视行为。

    价格 10.00 → 10.02（上穿）→ 10.06（进带）→ 10.05 → 9.98。
    上穿在索引 1，但"上穿确实带来了 ε 带"这件事到索引 2 才成立 ——
    所以确认 bar 是索引 2，成交在索引 3。

    如果把这条断言改回"等于边沿那根"，`run_mode_a_backtest` 就会重新拿到一根
    **早于确认**的入场价，前视原样回来。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    bar = _confirmed_entry_bar(
        frame, flag="signal_intraday_avg_cross_up", confirm=("gate_enter_band_up",)
    )
    assert bar == frame["datetime"][2], "确认 bar 必须是进带那根，不是边沿那根"
    assert bar != frame["datetime"][1], "不能退回边沿那根 —— 那就是前视"


def test_path_a_fills_on_the_next_bar_after_confirmation() -> None:
    """成交价 = **确认 bar 的下一根**的 `price`，不是确认 bar 自己那根。

    确认 bar 在索引 2（价 10.06），下一根索引 3 价 10.05 → 卖出成交价 10.05。
    用 10.06 就是"看到收盘价的同时就在那个价成交"，同样是前视。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98, 9.96],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
        up_cross=[False, True, False, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "sell_first", PATH_A)
    assert g.counts.completed == 1

    rec = next(r for r in summary.records if r.trigger_layer_ref.endswith("A"))
    assert rec.leg1_price == pytest.approx(10.05), "成交价必须取确认 bar 的下一根"
    # 成交时刻也必须是下一根，不能写成确认时刻（审计的人会以为下单早了一根）
    assert rec.leg1_ts is not None
    assert rec.leg1_ts.hour == 9 and rec.leg1_ts.minute == 33


def test_path_b_fills_on_the_next_bar_too() -> None:
    """⑤a：路径 B 与 A **同一口径** —— 进带那根是确认，成交在下一根。

    两条路径的成交口径必须一致，否则 A/B 的差额里混进了"在哪儿成交"的差别，
    而不是信号定义的差别。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98, 9.96],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    rec = _group(summary, "sell_first", PATH_B)
    assert rec.counts.completed == 1
    b_rec = next(r for r in summary.records if r.trigger_layer_ref.endswith("B"))
    assert b_rec.leg1_price == pytest.approx(10.05), "B 也必须用进带的下一根成交"


def test_path_a_never_completes_before_its_confirmation() -> None:
    """**ALIGN §1.2 反例**：确认在 13:00，而 10:05 有个"出场"信号。

    构造：早盘的边沿与一份假"回摆"（10:05 跌破均价），确认进带拖到 13:00 之后。
    正确的行为是：确认 bar 之后**没有**再跌破均价 → 这一轮**不存在 completed**。

    旧实现会从边沿那根之后开始找出场，于是在 10:05 就记一笔 completed ——
    出场时刻（10:05）早于它自己的确认依据（13:00），一轮实际不可能发生的 T
    就这么成交了，而且还带着正价差进了已实现 KPI。

    注意这条用例抓住的是「确认 bar 取错」（把边沿那根当确认），**不**抓
    「出场搜索起点取错」—— 确认在最后一根时，从哪儿开始搜都凑不出 completed。
    那个方向的守卫在 `test_completed_round_cannot_use_a_bar_before_it_filled`。
    """
    # 09:30 起每分钟一根，索引 i 的时刻 = 09:30 + i 分钟。
    # 09:31（索引 1）上穿；确认进带被拖到 09:37（索引 7）—— 相隔 6 分钟，
    # 性质与"09:31 上穿、13:00 才确认"完全一样，只是更紧凑。
    prices = [
        10.00,   # 0 09:30
        10.02,   # 1 09:31 边沿（上穿）
        10.01,   # 2
        10.00,   # 3
        9.99,    # 4 09:34 —— 假"回摆"：旧实现会在这里记 completed 并收工
        10.00,   # 5
        10.02,   # 6
        10.05,   # 7 09:37 确认进带
    ]
    frame = _day(
        prices,
        vwap=10.0,
        up_band=[False, False, False, False, False, False, False, True],
        up_cross=[False, True, False, False, False, False, False, False],
    )
    # 索引 4 确实跌到均价下方。若出场搜索从边沿（索引 1）之后开始，
    # 就会在这一根成交 —— 而它早于确认（索引 7）。这正是要禁掉的前视。
    fake_exit_ts = frame["datetime"][4]
    confirm_ts = frame["datetime"][7]
    assert prices[4] < 10.0
    assert fake_exit_ts < confirm_ts, "夹具本身要把'假出场'放在确认之前"

    summary = run_mode_a_backtest(frame)
    a = _group(summary, "sell_first", PATH_A)

    assert a.counts.completed == 0, (
        "确认在最后一根之前、出场只允许在确认之后 —— 这一轮不该 completed"
    )
    completed_a = [
        r for r in summary.records
        if r.round_status == "completed" and r.trigger_layer_ref.endswith("A")
    ]
    assert completed_a == [], "不得出现出场早于确认的轮次"

    # 全量记录里也不许有任何一轮的两腿时刻倒挂（出场 ≤ 入场）
    for rec in summary.records:
        if rec.round_status == "completed":
            assert rec.leg1_ts is not None and rec.leg2_ts is not None
            assert rec.leg2_ts > rec.leg1_ts, (
                f"{rec.t_round_id}: 出场 {rec.leg2_ts} 不晚于入场 {rec.leg1_ts}"
            )


def test_completed_round_cannot_use_a_bar_before_it_filled() -> None:
    """**出场搜索起点**的守卫：确认 09:32，成交 09:33，但均价在 09:32 就被跌破。

    若出场搜索从确认 bar 开始（而不是从**成交 bar 之后**开始），09:32 那根会被当成
    出场 —— 于是出现"还没成交就已经平仓"的轮次，成交价还取自 09:33。
    正确行为：09:32 不算，唯一合法的回摆在 09:35，成交 09:33 → completed 且
    `leg2_ts`(09:35) > `leg1_ts`(09:33)。
    """
    frame = _day(
        [10.00, 10.02, 10.05, 9.99, 10.04, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
        up_cross=[False, True, False, False, False, False],
    )
    # 索引: 0 09:30 · 1 09:31 上穿 · 2 09:32 确认进带 · 3 09:33 成交
    #       4 09:34 仍在均价上方 · 5 09:35 跌破 → 唯一合法出场
    confirm_ts = frame["datetime"][2]
    early_break_ts = frame["datetime"][3]  # 09:33 价格 9.99 < 10.00
    assert early_break_ts > confirm_ts, "夹具：确认之后才有一根跌破均价的 bar"

    summary = run_mode_a_backtest(frame)
    a = _group(summary, "sell_first", PATH_A)
    assert a.counts.completed == 1, "合法回摆存在于成交之后，这一轮应当 completed"

    rec = next(r for r in summary.records if r.trigger_layer_ref.endswith("A"))
    assert rec.round_status == "completed"
    assert rec.leg1_ts is not None and rec.leg2_ts is not None
    # 成交在确认的下一根（09:33）—— 而不是把 09:32 当成交
    assert rec.leg1_ts.hour == 9 and rec.leg1_ts.minute == 33, (
        f"成交时刻应为 09:33（确认 bar 的下一根），实际 {rec.leg1_ts}"
    )
    # 出场只能在成交之后：09:33 那根自身不能当出场
    assert rec.leg2_ts > rec.leg1_ts, (
        f"出场 {rec.leg2_ts} 不晚于成交 {rec.leg1_ts} —— 出场搜索起点早于成交"
    )
    assert rec.leg2_ts.hour == 9 and rec.leg2_ts.minute == 35, (
        f"出场应落在 09:35 那根真回摆，实际 {rec.leg2_ts}"
    )


def test_band_on_the_same_bar_as_the_cross_is_not_a_confirmation() -> None:
    """同一根 bar 既穿越均价又落进 ε 带 —— **不算**确认，当天不成交。

    严格 `c > edge_ts`：这一根同时承载"穿越"和"进带"两条信息，把它当确认等于
    用这根收盘后才知道的事，给一个以这根为起点的成交做依据。宁可空仓一天。
    """
    frame = _day(
        [10.00, 10.02, 10.05, 10.03, 10.01, 9.99],
        vwap=10.0,
        up_band=[False, True, False, False, False, False],   # 与上穿同一根（索引 1）
        up_cross=[False, True, False, False, False, False],
    )
    edge_ts = frame["datetime"][1]

    confirmed = _confirmed_entry_bars(
        frame,
        flag="signal_intraday_avg_cross_up",
        confirm=("gate_enter_band_up",),
    )
    assert confirmed == [], (
        f"同一根 bar 的穿越+进带不构成确认，实际返回 {confirmed}（边沿 {edge_ts}）"
    )

    # 端到端：路径 A 当天没有轮次（有信号，但没有合规成交点）
    summary = run_mode_a_backtest(frame)
    a = _group(summary, "sell_first", PATH_A)
    assert a.counts.rounds == 0, "没有合规确认 → 不该有轮次"


def test_path_a_requires_band_after_cross() -> None:
    """边沿之后**没有**进带 → 不成立（贴着均价抖动不算成交点）。"""
    frame = _day(
        [10.00, 10.02, 10.00, 10.01, 9.99],
        vwap=10.0,
        up_band=[False, False, False, False, False],
        up_cross=[False, True, False, False, False],
    )
    assert (
        _confirmed_entry_bar(
            frame, flag="signal_intraday_avg_cross_up", confirm=("gate_enter_band_up",)
        )
        is None
    )


def test_path_a_band_before_cross_does_not_confirm() -> None:
    """进带发生在边沿**之前** → 不算确认（因果方向必须是边沿在前）。

    这一条守的是更早一版（hfeat）修掉的 bug：那次实现要求"边沿与进带同一根
    同时成立"，两个条件在定义上互斥，路径 A 恒为空集。本批次改成"边沿在前、
    最近一根进带在后"，但**方向不能反** —— 之前的进带是上一段行情的尾巴。
    """
    frame = _day(
        [10.00, 10.06, 10.02, 10.01, 9.98],
        vwap=10.0,
        up_band=[False, True, False, False, False],  # 进带在索引 1
        up_cross=[False, False, True, False, False],  # 边沿在索引 2（更晚）
    )
    assert (
        _confirmed_entry_bar(
            frame, flag="signal_intraday_avg_cross_up", confirm=("gate_enter_band_up",)
        )
        is None
    )


def test_path_a_produces_rounds() -> None:
    """路径 A **必须能真的产出轮次**，不能是空架子。"""
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "sell_first", PATH_A)
    assert g.counts.rounds >= 1, "路径 A 没有产出任何轮次"
    assert g.counts.completed == 1


# ---------------------------------------------------------------- 配额


def test_groups_do_not_compete_for_the_same_slot() -> None:
    """四个格子**各是独立世界**：正 T 的 A 和 B 都要能各自成轮，不互相抢名额。

    这条钉的是本批次修正过的一个口径错误。早先实现让四格共享一个
    `(symbol, 交易日)` 名额，扫到顺序是「正 T 优先、A 优先」，于是 A 会在每个
    两类条件都满足的日子抢走名额、B 永远拿不到 —— A 与 B 的对比自带系统性优势，
    而任务书要求 A∥B 分列且**不混主门禁**，带偏置的分列没有可比性。

    夹具里 10.02 上穿（A 的边沿）与 10.06 首次进上带（B 的入场）在同一天都成立。
    旧实现下只有一个格子的 `rounds == 1`；现在 A 与 B 各有一个。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)

    a = _group(summary, "sell_first", PATH_A)
    b = _group(summary, "sell_first", PATH_B)

    assert a.counts.completed == 1, "正 T + A 这个反事实世界没成轮"
    assert b.counts.completed == 1, "正 T + B 这个反事实世界没成轮"

    # 两个世界互不记账为"被对方挤掉"
    assert a.counts.skipped_quota == 0
    assert b.counts.skipped_quota == 0


def test_skipped_quota_counts_only_within_the_same_world() -> None:
    """`skipped_quota` 的**唯一**含义：同世界、同路径、同日多候选撞配额。

    构造同一天里同一格子出现两次合格入场（上穿 → 进带 → 跌回 → 再上穿 → 再进带）。
    一日一轮只允许第一轮，第二个候选记 `skipped_quota`。
    """
    # 价格：10.00 → 10.02(上穿1) → 10.06(进带1) → 9.98(回摆) → 10.02(上穿2) → 10.06(进带2)
    frame = _day(
        [10.00, 10.02, 10.06, 9.98, 10.02, 10.06, 9.99],
        vwap=10.0,
        up_band=[False, False, True, False, False, True, False],
        up_cross=[False, True, False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)
    a = _group(summary, "sell_first", PATH_A)

    # 同一世界内只开一轮，第二个候选被配额拒
    assert a.counts.rounds == C.MAX_ROUNDS_PER_DAY
    assert a.counts.skipped_quota >= 1, "同格第二个候选应记 skipped_quota"
    # 落盘记录也只一条
    same_world = [r for r in summary.records if r.mode == "sell_first" and r.trigger_layer_ref.endswith("A")]
    assert len(same_world) == C.MAX_ROUNDS_PER_DAY


def test_max_rounds_per_day_is_respected_per_world() -> None:
    """配额上限就是常量 `MAX_ROUNDS_PER_DAY`，**每个格子各自**受它约束。"""
    frame = _day(
        [10.00, 10.02, 10.06, 9.98, 10.02, 10.06, 9.99],
        vwap=10.0,
        up_band=[False, False, True, False, False, True, False],
        up_cross=[False, True, False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)
    for g in summary.groups:
        # 每个格子内，一轮 T 的记录数不超过上限；`skipped_quota` 不算记录
        assert g.counts.rounds <= C.MAX_ROUNDS_PER_DAY


def test_skipped_quota_is_not_a_round() -> None:
    """`skipped_quota` 不进 `rounds`（它不是一轮 T，只是被拒的候选）。"""
    frame = _day(
        [10.00, 10.02, 10.06, 9.98, 10.02, 10.06, 9.99],
        vwap=10.0,
        up_band=[False, False, True, False, False, True, False],
        up_cross=[False, True, False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)
    for g in summary.groups:
        c = g.counts
        assert c.rounds == (
            c.completed + c.unreverted_at_cutoff + c.data_truncated + c.limit_locked
        ), "rounds 必须等于四个结局桶之和"
        assert c.rounds <= C.MAX_ROUNDS_PER_DAY


# ---------------------------------------------------------------- 先买后卖


def test_buy_first_buys_below_and_sells_above() -> None:
    """先跌后涨：下带买入 → 回升到均价上方卖出 → 先买后卖赚。

    下带在索引 1（价 9.96），但**成交在下一根**索引 2（价 9.92）。
    之后第一根回到均价上方的是 10.02（索引 4）。
    毛价差 = (10.02 − 9.92) × 100 = 10.00 —— 买入价是 9.92，不是 9.96。
    """
    frame = _day(
        [10.00, 9.96, 9.92, 9.95, 10.02, 10.05],
        vwap=10.0,
        down_band=[False, True, False, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "buy_first", PATH_B)
    assert g.counts.completed == 1
    assert g.realized_gross_spread == pytest.approx((10.02 - 9.92) * LOT_SIZE, abs=1e-9)
    assert g.realized_net_spread > 0


def test_entry_side_mapping() -> None:
    """入场侧：先卖后买在均价上方卖，先买后卖在均价下方买。"""
    assert entry_side("sell_first", PATH_A) == "up"
    assert entry_side("sell_first", PATH_B) == "up"
    assert entry_side("buy_first", PATH_A) == "down"
    assert entry_side("buy_first", PATH_B) == "down"


# ---------------------------------------------------------------- 成本口径


def test_cost_is_marked_design_pending() -> None:
    """成本口径尚未实测 → 必须显式标 `design_pending`，不能假装是实测值。"""
    assert COST_DESIGN_PENDING == "design_pending"
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)
    rec = summary.records[0]
    assert rec.cost_bp_tier == COST_DESIGN_PENDING


def test_lot_size_is_one_board_lot() -> None:
    """纸面每轮 1 手（100 股）—— 够表达价差，又不制造"大仓位"的错觉。"""
    assert LOT_SIZE == 100


# ---------------------------------------------------------------- 报告


def test_report_contains_required_metadata(tmp_path: Path) -> None:
    """报告 JSON 必须含 `dates` / `inputs` / `version` / 免责声明 / 虚拟底仓说明。"""
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)

    report = write_offline_report(
        out_dir=tmp_path,
        feature_frame=frame,
        summary=summary,
        inputs=("kline_minute_20260105.csv.gz",),
    )
    assert report.summary_path.exists()
    meta = json.loads(report.summary_path.read_text(encoding="utf-8"))

    assert meta["version"] == "design_pin_v1+eps_pct:0.30"
    assert meta["epsilon_value_pct"] == 0.30
    assert "2026-01-05" in json.dumps(meta["dates"])
    assert "kline_minute_20260105.csv.gz" in meta["inputs"]
    assert meta["virtual_lot_assumption"] == VIRTUAL_LOT_ASSUMPTION
    assert meta["disclaimer"]
    # 这一项是**声明文案**（写清楚"什么没被验证"），不是布尔开关
    assert isinstance(meta["not_claimed_verified"], str)
    assert "未测通" in meta["not_claimed_verified"]

    # ---- ⑥ 口径说明必须进 JSON, 不能只躺在 docstring 里 ----
    assert "require_prior_cross" in meta["path_a_definition"]
    assert "敏感度" in meta["path_a_definition"], "A 必须写明是敏感度开关, 不是平行信号"
    assert "expectancy" in meta["expectancy_definition"]
    assert meta["cost_gate_scope"] and "不在本批次范围" in meta["cost_gate_scope"]
    assert meta["limit_lock_source"] == "data_pending"
    assert meta["limit_locked_policy"]
    assert meta["footnote"]
    # 不许出现「剔除」这类会让人以为轮次被排除的措辞
    assert "剔除" not in json.dumps(meta, ensure_ascii=False)
    # 不许出现"已测通"这类结论性措辞
    blob = json.dumps(meta, ensure_ascii=False)
    for banned in ("已测通", "生产可用", "实盘验证通过"):
        assert banned not in blob


def test_report_writes_features_parquet(tmp_path: Path) -> None:
    """特征表落 Parquet，且能被读回、行数一致。"""
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
    )
    summary = run_mode_a_backtest(frame)
    report = write_offline_report(out_dir=tmp_path, feature_frame=frame, summary=summary)

    assert report.features_path.exists()
    back = pl.read_parquet(report.features_path)
    assert back.height == frame.height


def test_report_requires_explicit_out_dir(tmp_path: Path) -> None:
    """`out_dir` 必填，不默认写生产分区（也不调 `partition_writer`）。"""
    frame = _day([10.0, 10.02], vwap=10.0)
    summary = run_mode_a_backtest(frame)
    with pytest.raises(TypeError):
        write_offline_report(feature_frame=frame, summary=summary)  # type: ignore[call-arg]


def test_report_records_skipped_inputs(tmp_path: Path) -> None:
    """被跳过的输入要进报告 —— 否则"这些日子为什么没有"无从追查。"""
    frame = _day([10.0, 10.02], vwap=10.0)
    summary = run_mode_a_backtest(frame)
    report = write_offline_report(
        out_dir=tmp_path,
        feature_frame=frame,
        summary=summary,
        skipped=("kline_minute_20260106.csv.gz: 文件不存在(不回退最近分区)",),
    )
    meta = json.loads(report.summary_path.read_text(encoding="utf-8"))
    assert any("20260106" in s for s in meta["skipped"])
