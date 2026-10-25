"""模式 A 纸面回测（批次 20261025hfeat 任务③）。

守五件事：

1. **三个桶分列**：`completed` / `incomplete` / `abandoned` 各自计数，不合并
2. **只有 `completed` 进已实现 KPI** —— 这条不是本包新加的，是
   `TRoundRecord.__post_init__` 与 `complete_round()` 早就钉死的，本包不绕过它
3. **正 T 的 leg2 必须是 `old_lot`**：标成 `same_day_buy` 那一轮就到不了 `completed`
4. **路径 A 与 B 的格子互不覆盖**（各自计数，不合成一个"门禁通过"）
5. **报告含 `dates` / `inputs` / `version` / 免责声明**

测试数据是**手工灌标记**的：直接给 `gate_enter_band_*` 与 `signal_intraday_avg_cross_*`
布尔列，绕开 `features` 的派生。这样每个场景的期望值是能手算出来的，而不是
"跑一遍看结果抄下来"——后者等于用实现定义正确性。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
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
    entry_side,
)

SYMBOL = "600000.SH"
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
) -> pl.DataFrame:
    """造一日 bar + 手工灌入 A/B 标记列。

    `vwap` 传单值表示全日均价恒定，便于手算 ε 带位置。
    """
    start = start or datetime(2026, 1, 5, 9, 30)
    n = len(prices)
    vwap_seq = [vwap] * n if vwap is not None else list(prices)
    rows = []
    for i, px in enumerate(prices):
        rows.append(
            {
                "symbol": symbol,
                "date": trade_date,
                "datetime": start + timedelta(minutes=i),
                "price": float(px),
                "vwap": float(vwap_seq[i]),
            }
        )
    frame = pl.DataFrame(rows)
    falses = [False] * n

    def col(values: list[bool] | None) -> list[bool]:
        return values if values is not None else falses

    return frame.with_columns(
        [
            pl.Series("gate_enter_band_up", col(up_band), dtype=pl.Boolean),
            pl.Series("gate_enter_band_down", col(down_band), dtype=pl.Boolean),
            pl.Series(
                "signal_intraday_avg_cross_up", col(up_cross), dtype=pl.Boolean
            ),
            pl.Series(
                "signal_intraday_avg_cross_down", col(down_cross), dtype=pl.Boolean
            ),
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
        (m, p) for m in ("zheng_t", "dao_t") for p in PATHS
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
    assert modes == {"zheng_t", "dao_t"}
    assert paths == {PATH_A, PATH_B}


# ---------------------------------------------------------------- 分桶


def test_completion_lands_in_completed_bucket() -> None:
    """冲高回落：上带卖出 → 跌破均价买回 → `completed`，净价差可手算。

    价格 10.00 → 10.02 → 10.06 → 10.05 → 10.01 → 9.98，均价恒 10.00。
    路径 B：首次进上带在 10.06（第 3 根，索引 2），之后第一根跌到均价及以下
    是 9.98（索引 5）。
    毛价差**按金额算**（不是每股价差）：(10.06 − 9.98) × 100 股 = 8.00 元。
    本批次成本口径是 `design_pending`（`cost_amount=0.0`），所以净 = 毛。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 10.01, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "zheng_t", PATH_B)

    assert g.counts.completed == 1
    assert g.counts.incomplete == 0
    assert g.counts.abandoned == 0
    assert g.counts.rounds == 1
    assert g.counts.resolution_rate == 1.0
    assert g.realized_gross_spread == pytest.approx(8.00, abs=1e-9)
    assert g.realized_net_spread > 0
    assert len(summary.records) == 1
    assert summary.records[0].round_status == "completed"


def test_no_exit_before_cutoff_lands_in_incomplete() -> None:
    """单边上涨，价格再也没有回到均价 → `incomplete`，**不进已实现 KPI**。"""
    frame = _day(
        [10.00, 10.02, 10.05, 10.08, 10.10, 10.12],
        vwap=10.0,
        up_band=[False, False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "zheng_t", PATH_B)

    assert g.counts.completed == 0
    assert g.counts.incomplete == 1
    assert g.counts.abandoned == 0
    assert g.counts.rounds == 1
    assert g.counts.resolution_rate == 0.0
    # 关键：未完成不贡献任何已实现价差
    assert g.realized_net_spread == 0.0
    assert g.realized_gross_spread == 0.0

    record = summary.records[0]
    assert record.round_status == "incomplete"
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
    )
    summary = run_mode_a_backtest(pl.concat([d1, d2]))

    by_status = {}
    for rec in summary.records:
        by_status.setdefault(rec.round_status, []).append(rec)

    assert "completed" in by_status
    assert "incomplete" in by_status
    for rec in by_status["completed"]:
        assert rec.count_in_realized_kpi is True
    for status in ("incomplete", "abandoned"):
        for rec in by_status.get(status, []):
            assert rec.count_in_realized_kpi is False, f"{status} 进了已实现 KPI"

    # 摘要口径与记录一致：已实现只由 completed 贡献
    g = _group(summary, "zheng_t", PATH_B)
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
    g = _group(summary, "zheng_t", PATH_B)
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
      - 01-05 冲高回落 → completed，净额 = (10.06−9.98)×100 = 8.00
      - 01-06 单边上涨 → incomplete，没有任何已实现价差
    若汇总把 incomplete 也算进来，数字就会偏离 8.00。
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
    )
    summary = run_mode_a_backtest(pl.concat([done, stuck]))
    g = _group(summary, "zheng_t", PATH_B)

    assert g.counts.completed == 1
    assert g.counts.incomplete == 1

    # 已实现汇总必须**恰好**等于那一条 completed 的净额
    completed = [r for r in summary.records if r.round_status == "completed"]
    assert len(completed) == 1
    assert g.realized_net_spread == pytest.approx(completed[0].net_spread, abs=1e-9)
    assert g.realized_gross_spread == pytest.approx(8.00, abs=1e-9)

    # 均值也只按 completed 条数平均（不是按 rounds 数）
    assert g.realized_avg_net_spread == pytest.approx(completed[0].net_spread, abs=1e-9)


# ---------------------------------------------------------------- 正 T 的 old_lot


def test_zheng_t_leg2_source_lot_is_old_lot() -> None:
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
    assert rec.leg2_source_lot == "old_lot"
    assert rec.leg2_status == "filled"
    assert rec.leg2_price is not None


def test_zheng_t_with_same_day_buy_cannot_reach_completed() -> None:
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

    record = _make_record(SYMBOL, TRADE_DATE, "zheng_t", PATH_B, "2026-01-05 09:32:00")
    record = transition(record, "open_leg2", leg2_source_lot="old_lot")

    with pytest.raises(InvalidTRoundTransitionError):
        complete_round(
            record,
            leg1_price=10.06,
            leg2_price=9.98,
            qty=LOT_SIZE,
            leg2_source_lot="same_day_buy",  # 正 T 不允许
            cost=RoundCost(cost_amount=0.0, cost_bp_tier=COST_DESIGN_PENDING),
        )


# ---------------------------------------------------------------- 路径 A 的确认语义


def test_path_a_entry_is_the_cross_bar_not_the_band_bar() -> None:
    """路径 A 的入场是**边沿那根**，进带只作确认（不把入场点挪到进带那根）。

    价格 10.00 → 10.02（上穿）→ 10.06（进带）。
    期望入场 = 10.02 那根（索引 1），不是 10.06 那根。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    entry = _confirmed_entry_bar(
        frame, flag="signal_intraday_avg_cross_up", confirm=("gate_enter_band_up",)
    )
    assert entry == frame["datetime"][1], "入场应落在边沿那根，而不是进带那根"


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

    这是本批次修掉的一个真 bug：早先实现要求"边沿与进带同一根同时成立"，
    而这两个条件在定义上互斥（刚穿越那根只高出零点几 bp），导致路径 A 恒为空集。
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
    g = _group(summary, "zheng_t", PATH_A)
    assert g.counts.rounds >= 1, "路径 A 没有产出任何轮次"
    assert g.counts.completed == 1


# ---------------------------------------------------------------- 配额


def test_quota_is_per_symbol_per_day_not_per_group() -> None:
    """一日一票只允许一轮：四个格子竞争同一个名额，其余记 `skipped_quota`。

    四个格子各自独立计数会得到"一天做四轮 T"的样本，与本系统一日一轮的硬规则
    不符 —— 而且那种样本几乎总是更好看（多做几次总能摊到一次成功的）。
    """
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)

    assert len(summary.records) == 1, "同票同日建立了不止一轮"
    total_rounds = sum(g.counts.rounds for g in summary.groups)
    total_skipped = sum(g.counts.skipped_quota for g in summary.groups)
    assert total_rounds == 1
    assert total_skipped >= 1, "有别的格子也看到了信号，应记 skipped_quota"


def test_max_rounds_per_day_is_respected() -> None:
    """配额上限就是常量 `MAX_ROUNDS_PER_DAY`，不是本包自己定的数。"""
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    assert len(summary.records) <= C.MAX_ROUNDS_PER_DAY


def test_skipped_quota_is_not_a_round() -> None:
    """`skipped_quota` 不进 `rounds`（它不是一轮 T，只是被拒的信号）。"""
    frame = _day(
        [10.00, 10.02, 10.06, 10.05, 9.98],
        vwap=10.0,
        up_band=[False, False, True, False, False],
        up_cross=[False, True, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    for g in summary.groups:
        c = g.counts
        assert c.rounds == c.completed + c.incomplete + c.abandoned
        # skipped 单独一列，不能混进 rounds
        assert c.rounds <= 1


# ---------------------------------------------------------------- 倒 T


def test_dao_t_buys_below_and_sells_above() -> None:
    """先跌后涨：下带买入 → 回升到均价上方卖出 → 倒 T 赚。"""
    frame = _day(
        [10.00, 9.96, 9.92, 9.95, 10.02, 10.05],
        vwap=10.0,
        down_band=[False, True, False, False, False, False],
    )
    summary = run_mode_a_backtest(frame)
    g = _group(summary, "dao_t", PATH_B)
    assert g.counts.completed == 1
    assert g.realized_gross_spread == pytest.approx((10.02 - 9.96) * LOT_SIZE, abs=1e-9)
    assert g.realized_net_spread > 0


def test_entry_side_mapping() -> None:
    """入场侧：正 T 在均价上方卖，倒 T 在均价下方买。"""
    assert entry_side("zheng_t", PATH_A) == "up"
    assert entry_side("zheng_t", PATH_B) == "up"
    assert entry_side("dao_t", PATH_A) == "down"
    assert entry_side("dao_t", PATH_B) == "down"


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
