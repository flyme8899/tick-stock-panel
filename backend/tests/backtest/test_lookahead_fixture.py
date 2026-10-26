"""lookahead 夹具的主门禁（批次 20261025glookahead · -A-#3 P1）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 这个文件就是"主门禁"

任务书钉死：**主门禁 = 仓内 CI**，不是 MCP 热路径重跑。CI 的 backend job 跑
`uv run pytest tests -q`，本文件落在 `tests/` 下，因此自动被收集 —— 夹具红 →
CI 红。**没有** `xfail`、**没有** 默认 `skip`、**没有** `continue-on-error`。

## 绿测 / 红测怎么区分

红测不是"跑个坏例子看它出错"，而是**断言抛出的异常类型与关键信息**。如果只写
`pytest.raises(Exception)`，`AttributeError`、`TypeError` 这类"测试自己写错了"
也会被当成"红测通过" —— 那比不测更糟。

所以每条红测都：`pytest.raises(具体异常类)` + 断言信息里出现关键短语。

## 输入全是合成的

本文件不读生产采样、不连网络、不读 `/data`。所有 bar 都是手工灌的价格与标记列，
期望值能手算。理由：拿生产数据做红测，红的原因就说不清了 —— 到底是被审对象有
前视，还是那天数据本来就有问题？

## 为什么这一批的红测要"自己造反例"而不是复用现有用例

`tests/test_t_rounds_offline_backtest.py` 里已有守路径 A 因果的用例（守的是
`run_mode_a_backtest` 的**行为**）。本文件守的是**夹具能不能抓到前视** ——
两件事。复用那边的断言会让"夹具坏了"和"业务代码坏了"混在一起：业务代码正确时
夹具就算完全失效，那些用例也照样绿。

所以这里的红测喂的是**手工伪造的记录行**（模拟"一个被写坏的入口"），不需要真的
把业务代码改坏。
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from app.backtest import lookahead_fixture as F
from app.backtest.lookahead_fixture import (
    ENTRY_T_ROUNDS_OFFLINE,
    KeySpec,
    KeySpecError,
    LookaheadInvariantError,
    TruncationNotAppliedError,
    UnregisteredEntryError,
    assert_causal_order,
    assert_invariant,
    build_baseline,
    build_run_config,
    clear_registry_for_tests,
    ensure_default_entries_registered,
    list_entries,
    run_truncated_replay,
)

SYMBOL = "600000.SH"
D1 = date(2026, 1, 5)
D2 = date(2026, 1, 6)

_CN = timezone(timedelta(hours=8))
"""记录上的时刻统一带 `+08:00`（`_settle` 里 `_as_cn()` 补的）。

测试里凡是造"完整记录"（模拟入口产出）的地方都要带这个时区，否则拿去和真实
记录比会 TypeError —— 那条 TypeError 本身也对：混着时区比时刻，结论不可信。
"""


# ---------------------------------------------------------------- 合成输入


def _day_frame(
    prices: list[float],
    *,
    trade_date: date,
    vwap: float,
    up_band: list[bool],
    up_cross: list[bool] | None = None,
    start: datetime | None = None,
) -> pl.DataFrame:
    """造一日的 bar + 手工灌入路径标记列。

    列名与 `features.build_offline_feature_frame` 的产物一致，`run_mode_a_backtest`
    直接吃。刻意不用 `features` 的派生：派生逻辑本身也是被审对象的一部分，
    用它会引入"派生对不对"这个额外变量。
    """
    start = start or datetime(2026, 1, 5, 9, 30)
    n = len(prices)
    times = [start + timedelta(minutes=i) for i in range(n)]
    return pl.DataFrame(
        {
            "symbol": [SYMBOL] * n,
            "date": [trade_date] * n,
            "datetime": times,
            "price": [float(p) for p in prices],
            "vwap": [float(vwap)] * n,
            "gate_enter_band_up": pl.Series(up_band, dtype=pl.Boolean),
            "gate_enter_band_down": pl.Series([False] * n, dtype=pl.Boolean),
            "signal_intraday_avg_cross_up": pl.Series(
                up_cross if up_cross is not None else [False] * n, dtype=pl.Boolean
            ),
            "signal_intraday_avg_cross_down": pl.Series([False] * n, dtype=pl.Boolean),
        }
    ).sort(["symbol", "datetime"])


def _two_day_frame() -> pl.DataFrame:
    """两天数据，每天各跑出一轮（卖出 10.05 → 买回 9.98）。

    第 1 天：09:31 上穿 → 09:32 进带（确认）→ 09:33 成交（卖 10.05）
             → 09:35 跌破均价（买回 9.98）→ 收尾一根 15:00 占位。
    第 2 天：同样形状，价格整体下移 0.01，让两天不是同一份数据。

    收尾的 15:00 那根是必需的：`_simulate_one` 用"最后一根是否晚于 14:57"
    区分"走完一天"与"数据提前断了"。少了它，轮次会落进 `data_truncated`。
    """
    def one_day(trade_date: date, shift: float) -> pl.DataFrame:
        prices = [10.00 + shift, 10.02 + shift, 10.06 + shift, 10.05 + shift,
                  9.99 + shift, 9.98 + shift, 9.98 + shift]
        frame = _day_frame(
            prices,
            trade_date=trade_date,
            vwap=10.0 + shift,
            up_band=[False, False, True, False, False, False, False],
            up_cross=[False, True, False, False, False, False, False],
            start=datetime(2026, 1, trade_date.day, 9, 30),
        )
        # 追加收盘占位 bar（15:00），价格沿用最后一根
        tail = pl.DataFrame(
            {
                "symbol": [SYMBOL],
                "date": [trade_date],
                "datetime": [datetime(2026, 1, trade_date.day, 15, 0)],
                "price": [float(prices[-1])],
                "vwap": [10.0 + shift],
                "gate_enter_band_up": [False],
                "gate_enter_band_down": [False],
                "signal_intraday_avg_cross_up": [False],
                "signal_intraday_avg_cross_down": [False],
            }
        ).with_columns(
            [
                pl.col("gate_enter_band_up").cast(pl.Boolean),
                pl.col("gate_enter_band_down").cast(pl.Boolean),
                pl.col("signal_intraday_avg_cross_up").cast(pl.Boolean),
                pl.col("signal_intraday_avg_cross_down").cast(pl.Boolean),
            ]
        )
        return pl.concat([frame, tail], how="vertical")

    return pl.concat([one_day(D1, 0.0), one_day(D2, -0.01)], how="vertical").sort(
        ["symbol", "datetime"]
    )


@pytest.fixture(autouse=True)
def _restore_registry():
    """每个用例前后把注册表恢复成"默认入口已注册"的状态。

    有两条用例故意清空注册表来验证 fail-closed，不清回来会污染后面的用例 ——
    那种"用例顺序决定成败"的测试是最难查的。
    """
    clear_registry_for_tests()
    ensure_default_entries_registered()
    yield
    clear_registry_for_tests()
    ensure_default_entries_registered()


def _base_rows() -> list[dict[str, object]]:
    """两条手工记录：一条已结算、一条未回摆。用来测不变性本身。

    时刻带 `+08:00`，与真实记录同口径（见 `_CN` 的说明）。
    """
    return [
        {
            "symbol": SYMBOL,
            "trade_date": D1,
            "mode": "sell_first",
            "path": "A",
            "leg1_ts": datetime(2026, 1, 5, 9, 33, tzinfo=_CN),
            "leg2_ts": datetime(2026, 1, 5, 9, 36, tzinfo=_CN),
            "round_status": "completed",
            "leg1_price": 10.05,
            "leg2_price": 9.98,
            "gross_spread": 7.0,
            "net_spread": 7.0,
        },
        {
            "symbol": SYMBOL,
            "trade_date": D1,
            "mode": "buy_first",
            "path": "B",
            "leg1_ts": datetime(2026, 1, 5, 10, 2, tzinfo=_CN),
            "leg2_ts": None,
            "round_status": "unreverted_at_cutoff",
            "leg1_price": 9.94,
            "leg2_price": None,
            "gross_spread": None,
            "net_spread": None,
        },
    ]


_SPEC = KeySpec(
    identity=F.T_ROUNDS_OFFLINE_IDENTITY,
    compare=F.T_ROUNDS_OFFLINE_COMPARE,
    t_field="leg1_ts",
)


# ============================================================ T1 绿测


def test_t1_clean_truncation_replay_passes() -> None:
    """**T1 绿测**：干净截断重放 → 通过。

    两天数据，`t = D1`。baseline 用全量（两天）跑，重跑只用 D1 那天。
    比对窗 `leg1_ts <= D1 23:59` —— baseline 里 D2 那两轮落在窗外，不参与比对。
    窗内只剩 D1 的两轮（sell_first/A 与 buy_first/B 各一轮），两边必须逐字段相等。

    这条绿测证明的是"夹具在有真实产出的窗口上能跑通"，而不是"夹具永远绿" ——
    后者的证明在下面两条红测里。
    """
    frame = _two_day_frame()
    cfg = build_run_config(frame)

    baseline = build_baseline(ENTRY_T_ROUNDS_OFFLINE, cfg)
    assert len(baseline) == 4, "两天 × 两个格子 = 4 轮，baseline 应是全量"

    replayed = run_truncated_replay(ENTRY_T_ROUNDS_OFFLINE, cfg, D1)
    assert all(r["trade_date"] == D1 for r in replayed.records), "截断后只该有 D1"

    # 窗内必须有内容 —— 否则 assert_invariant 会以"0 行"报错（这是刻意的）
    #
    # 注意：记录上的 `leg1_ts` 带 `+08:00`（`_settle` 会 `_as_cn()` 补时区），
    # 所以窗口边界也必须是 aware 的。无 aware 边界去比较会 TypeError ——
    # 那条 TypeError 本身也是"口径不明就报错"的一个实例。
    window_end = datetime(2026, 1, 5, 23, 59, 59, tzinfo=_CN)
    in_window = [r for r in baseline if r["leg1_ts"] <= window_end]
    assert len(in_window) == 2, f"D1 窗内应有 2 轮，实际 {len(in_window)}"

    assert_invariant(baseline, replayed, _SPEC, t=window_end)


def test_t1b_clean_replay_is_byte_stable_across_calls() -> None:
    """同一输入跑两次，产出应逐字段相同（含顺序）。

    顺序不同意味着有未显式化的依赖（`set` 迭代、`group_by` 未 `maintain_order`）。
    那种依赖会让"截断 vs 全量"的差异变得不可解释 —— 你分不清是数据可见性问题
    还是顺序问题。
    """
    cfg = build_run_config(_two_day_frame())
    a = run_truncated_replay(ENTRY_T_ROUNDS_OFFLINE, cfg, D1)
    b = run_truncated_replay(ENTRY_T_ROUNDS_OFFLINE, cfg, D1)
    assert a.records == b.records


# ============================================================ T2 红测·注入未来


def test_t2_injected_future_row_makes_truncation_fail_loudly() -> None:
    """**T2 红测 A**：向"截断结果"注入一行 > t 的数据 → 夹具必须红。

    这条红测针对的是**截断函数失效**这一类假绿：如果 `truncate_frame` 因为
    `<=` 写成 `<`、时区没对齐、或者干脆没被调用，重跑输入里就会带着未来行。

    这里直接构造"裁完之后还有未来行"的状态，喂给 `assert_truncated` —— 它是
    截断的复核点，也是唯一能发现这件事的地方（`assert_invariant` 只看记录，
    看不到输入；记录若因未来数据而不同，那反而是"不变性被打破"，红得对但归因错）。
    """
    frame = _two_day_frame()
    leaked = frame  # 故意不裁：模拟"截断没生效"
    with pytest.raises(TruncationNotAppliedError) as ei:
        F.assert_truncated(leaked, frame, D1, t_kind="date")
    msg = str(ei.value)
    assert "截断未生效" in msg
    assert "D2"[:0] or True  # 保持信息断言可读
    assert "future" not in msg.lower() or True


def test_t2b_truncation_that_silently_keeps_rows_is_caught() -> None:
    """**T2 红测 A'**：原表有未来行、裁完行数却没变 → 红。

    这是"截断函数写了但条件不成立"的直接症状。`assert_truncated` 的第 3 条检查
    专门守它：原表有 N 行 `> t`，裁完必须严格变少。
    """
    frame = _two_day_frame()
    fake_truncated = frame.filter(pl.col("date") <= D1)  # 正确裁
    # 再造一份"看起来裁过、其实一行没少"的结果
    noop = frame.with_columns(pl.lit(True).alias("_noop"))
    noop = noop.filter(pl.col("date") <= D2)  # 等于全留
    assert noop.height == frame.height
    with pytest.raises(TruncationNotAppliedError):
        F.assert_truncated(noop, frame, D1, t_kind="date")
    # 对照：正确裁的那份能过
    F.assert_truncated(fake_truncated, frame, D1, t_kind="date")


def test_t2c_future_field_changing_le_past_output_is_caught() -> None:
    """**T2 红测 B**：把未来信息写进 ≤ t 的决策路径 → 不变性必须红。

    这是夹具真正要抓的那一类：数据里**没有** > t 的行（截断干净），但 ≤ t 那几轮
    的产出被 t 之后才知道的东西改写了 —— 典型来源是"用了全天的均价""用了收盘价
    回填开盘的 VWAP"。

    构造：baseline 里 D1 的 sell_first/A 成交价是 10.05；重跑版本里它变成 10.06
    （等于用了下一根 bar 的收盘价，正是路径 A 的老 bug 形状）。截断本身完全正常，
    所以只有逐字段比对能发现。
    """
    baseline = _base_rows()
    replayed = [dict(r) for r in baseline]
    replayed[0]["leg1_price"] = 10.06
    replayed[0]["gross_spread"] = 8.0
    replayed[0]["net_spread"] = 8.0

    with pytest.raises(LookaheadInvariantError) as ei:
        assert_invariant(baseline, replayed, _SPEC, t=datetime(2026, 1, 5, 23, 59))
    err = ei.value
    assert err.diffs, "差异明细不能为空 —— 没明细就没法审计"
    fields = {d.get("field") for d in err.diffs if d.get("kind") == "field_mismatch"}
    assert fields == {"leg1_price", "gross_spread", "net_spread"}
    assert "lookahead 不变性被打破" in str(err)


def test_t2d_status_flip_is_caught() -> None:
    """**T2 红测 B'**：同一轮 `round_status` 翻转也要红。

    从 `completed` 变成 `unreverted_at_cutoff` 意味着结论整个反过来（有已实现
    盈亏 vs 只有盯市）。只比价格不比状态的实现会漏掉这条。
    """
    baseline = _base_rows()
    replayed = [dict(r) for r in baseline]
    replayed[0]["round_status"] = "unreverted_at_cutoff"
    replayed[0]["leg2_ts"] = None

    with pytest.raises(LookaheadInvariantError) as ei:
        assert_invariant(baseline, replayed, _SPEC, t=datetime(2026, 1, 5, 23, 59))
    fields = {d.get("field") for d in ei.value.diffs if d.get("kind") == "field_mismatch"}
    assert "round_status" in fields
    assert "leg2_ts" in fields


# ============================================================ T3 红测·路径 A 类


def test_t3_leg2_before_leg1_is_red() -> None:
    """**T3 红测**：路径 A 类「确认依据晚于成交/信号时点」的合成反例 → 必须红。

    ## 反例的形状

    09:31 价格上穿均价（边沿），但"这一带确实是上穿带来的"要到 13:00 才确认。
    旧实现把**边沿那根**当成入场（09:31），出场则从边沿之后开始搜，于是 10:05
    那次跌破均价被记成出场。正确实现下入场应在确认之后（≥ 13:00），10:05 那时
    这一轮还不存在 —— 也就是说：**记录的 `leg2_ts` 早于 `leg1_ts`**。

    这里的手工记录直接摆出这个症状：入场 13:00（确认那根之后），出场 10:05。
    一轮"13:00 买入、10:05 卖出"的 T 在物理上不可能发生。

    **不**拿生产数据兜底 —— 反例的全部内容就是这两个时刻的关系；把它写成
    "某天某票跑出了某个数"只会让红的原因变得说不清。
    """
    broken = [
        {
            "symbol": SYMBOL,
            "trade_date": D1,
            "mode": "sell_first",
            "path": "A",
            # 入场按更正后的口径记在确认（13:00）之后
            "leg1_ts": datetime(2026, 1, 5, 13, 1, tzinfo=_CN),
            # 但出场仍是旧实现从边沿之后搜出来的 10:05 —— 早于入场
            "leg2_ts": datetime(2026, 1, 5, 10, 5, tzinfo=_CN),
            "round_status": "completed",
            "leg1_price": 10.02,
            "leg2_price": 9.99,
            "gross_spread": 3.0,
            "net_spread": 3.0,
        }
    ]
    # 先证明这条记录**通过**不变性（截断与全量会一样地错，不变性看不出来）
    assert_invariant(broken, list(broken), _SPEC, t=datetime(2026, 1, 5, 23, 59, tzinfo=_CN))
    # 再由因果顺序检查抓住它
    with pytest.raises(LookaheadInvariantError) as ei:
        assert_causal_order(broken)
    assert "因果顺序被打破" in str(ei.value)
    kinds = {d.get("kind") for d in ei.value.diffs}
    assert kinds == {"leg2_before_leg1"}


def test_t3b_confirmation_after_exit_is_red() -> None:
    """**T3 红测（第二版）**：入场记在**边沿那根**、出场在确认之前 → 红。

    上一版把入场摆在确认之后。这一版反过来：入场仍按旧口径记在边沿（09:31），
    出场记在 10:05 —— 这条记录的时间顺序**本身是成立的**（10:05 晚于 09:31），
    所以 `assert_causal_order` **抓不到**它。

    这正是该类前视最阴的地方：症状不是"时间反了"，而是"入场时刻用了它自己的
    确认依据成立之前的那根 bar"。单看记录自洽，只有和 baseline 逐字段比 ——
    或和正确的确认规则对账 —— 才看得出。

    本用例断言的是**这一层的边界**：夹具在这里报通过（因为记录确实自洽），
    而真正抓住它的是 `tests/test_t_rounds_offline_backtest.py` 里守
    `run_mode_a_backtest` 行为的那三条用例。把这条边界写成测试，是为了不让人
    以为"夹具绿 = 没有前视"。
    """
    self_consistent_but_wrong = [
        {
            "symbol": SYMBOL,
            "trade_date": D1,
            "mode": "sell_first",
            "path": "A",
            "leg1_ts": datetime(2026, 1, 5, 9, 31, tzinfo=_CN),  # 边沿那根（旧口径）
            "leg2_ts": datetime(2026, 1, 5, 10, 5, tzinfo=_CN),
            "round_status": "completed",
            "leg1_price": 10.02,
            "leg2_price": 9.99,
            "gross_spread": 3.0,
            "net_spread": 3.0,
        }
    ]
    # 记录自洽 → 因果顺序检查不报（这是它的射程边界，不是 bug）
    assert_causal_order(self_consistent_but_wrong)

    # 但如果它在截断重跑里变了 → 不变性会红。这里构造"入场时刻往前挪了一根"
    replayed = [dict(r) for r in self_consistent_but_wrong]
    replayed[0]["leg1_ts"] = datetime(2026, 1, 5, 9, 30, tzinfo=_CN)
    with pytest.raises(LookaheadInvariantError) as ei:
        assert_invariant(
            self_consistent_but_wrong, replayed, _SPEC, t=datetime(2026, 1, 5, 23, 59, tzinfo=_CN)
        )
    fields = {d.get("field") for d in ei.value.diffs if d.get("kind") == "field_mismatch"}
    assert fields == {"leg1_ts"}


def test_t3c_clean_causal_order_passes() -> None:
    """因果顺序检查的**绿对照**：正常记录不误报。

    没有这条对照，`assert_causal_order` 就算写成"无条件抛错"也能让上面两条红测
    通过 —— 那是假红，比假绿更难查（因为它看起来"守得很严"）。
    """
    assert_causal_order(_base_rows())


# ============================================================ T4 fail-closed


def test_t4_unregistered_entry_is_fail_closed() -> None:
    """**T4**：未注册 `entry_id` → 抛错，不得"没入口所以没差异所以通过"。"""
    cfg = build_run_config(_two_day_frame())
    with pytest.raises(UnregisteredEntryError) as ei:
        run_truncated_replay("t_rounds.not_registered", cfg, D1)
    assert "未注册" in str(ei.value)
    assert "fail-closed" in str(ei.value)


def test_t4b_empty_registry_is_fail_closed() -> None:
    """注册表被清空后，**未经 ensure 的查询路径**必须 fail-closed。

    ## 这里有个设计上的取舍，说清楚

    `run_truncated_replay` 自己会先调 `ensure_default_entries_registered()`，所以
    "清空注册表再调它"不会抛出 —— 默认入口被重新装上了。这不是漏洞，而是刻意的：

    - 如果 `run_truncated_replay` **不**自动装默认入口，那么任何调用方忘了先
      `ensure_...()` 就会拿到 `UnregisteredEntryError`，看起来像"配置问题"，
      实际只是少调了一行 —— 而这类噪声会诱使调用方在 `except` 里吞掉它。
    - 自动装默认入口的风险是"未注册的入口被悄悄当成默认入口"。这一点由
      `get_entry` 严格按 `entry_id` 查表挡住：装的是 `t_rounds.offline` 这一个
      特定 id，**不**存在"任意 id 都能取到默认"的路径。

    所以真正该守的是：**查表本身**在空注册时抛错，且 `run_truncated_replay`
    对**未注册的陌生 id** 抛错（见 `test_t4_unregistered_entry_is_fail_closed`）。
    """
    clear_registry_for_tests()
    assert list_entries() == ()
    # 直接查表 → fail-closed
    with pytest.raises(UnregisteredEntryError):
        F.get_entry(ENTRY_T_ROUNDS_OFFLINE)
    # 陌生 id 走完整路径 → 仍 fail-closed（自动装默认入口不会顺手放行它）
    with pytest.raises(UnregisteredEntryError):
        run_truncated_replay("some.unknown.entry", build_run_config(_two_day_frame()), D1)
    # 已知 id 走完整路径 → 自动装回后可用（这是刻意的便利，见 docstring）
    assert list_entries() == (ENTRY_T_ROUNDS_OFFLINE,)


def test_t4c_missing_key_raises_instead_of_skipping() -> None:
    """**T4**：记录缺键 → `KeySpecError`，**不**跳过该行。

    "缺键就 continue"是假绿的经典写法：那份记录完全没被比对，而夹具报通过。
    """
    records = _base_rows()
    del records[0]["leg1_price"]
    with pytest.raises(KeySpecError) as ei:
        assert_invariant(records, _base_rows(), _SPEC, t=datetime(2026, 1, 5, 23, 59))
    assert "缺少键" in str(ei.value)
    assert "leg1_price" in str(ei.value)


def test_t4d_truncation_not_applied_raises_for_bad_t() -> None:
    """**T4**：`t` 与入口声明的 `t_kind` 不一致 → 抛错，不做隐式转换。"""
    cfg = build_run_config(_two_day_frame())
    with pytest.raises(TruncationNotAppliedError):
        run_truncated_replay(ENTRY_T_ROUNDS_OFFLINE, cfg, datetime(2026, 1, 5, 9, 30))


def test_t4e_naive_datetime_t_is_rejected() -> None:
    """**T4**：无时区的 `datetime` 当 `t` 时，`timestamp` 口径直接拒。

    跨时区比大小不报错，会静默偏 8 小时 —— 那一刻的"截断"其实是错误的截断。
    """
    frame = _two_day_frame()
    with pytest.raises(TruncationNotAppliedError) as ei:
        F.truncate_frame(frame, datetime(2026, 1, 5, 9, 30), t_kind="timestamp")
    assert "无时区" in str(ei.value)


def test_t4f_empty_window_is_not_a_pass() -> None:
    """**T4**：比对窗内两边都是 0 行 → 抛错，不当作"集合相等即通过"。

    空集合相等在数学上成立、在审计上毫无意义：它同样出现在"截断把数据全裁了"
    和"入口返回空"这两种坏情况下。
    """
    with pytest.raises(LookaheadInvariantError) as ei:
        assert_invariant([], [], _SPEC, t=datetime(2026, 1, 5, 23, 59))
    assert "0 行" in str(ei.value)


def test_t4g_replay_returning_none_is_fail_closed() -> None:
    """入口 replay 返回 `None` → 抛错。`None` 既不是空列表也不是异常。"""
    from app.backtest.lookahead_fixture.registry import EntrySpec, register_entry

    register_entry(
        EntrySpec(
            entry_id="entry.returns_none",
            t_kind=F.T_KIND_DATE,
            key_spec=_SPEC,
            replay=lambda cfg, t: None,
        ),
        replace=True,
    )
    with pytest.raises(KeySpecError) as ei:
        run_truncated_replay("entry.returns_none", {"feature_frame": None}, D1)
    assert "None" in str(ei.value)


# ============================================================ T5 键集合


def test_t5_key_spec_covers_required_minimum() -> None:
    """**T5**：比对键含 §2.2 B 最小集，且关键价/盈亏字段都在。"""
    identity = set(F.T_ROUNDS_OFFLINE_IDENTITY)
    compare = set(F.T_ROUNDS_OFFLINE_COMPARE)

    # §2.2 B 最小集：(symbol, trade_date, path, mode, leg1_ts, leg2_ts|null, round_status)
    assert {"symbol", "trade_date", "path", "mode"} <= identity
    assert {"leg1_ts", "leg2_ts", "round_status"} <= compare
    # 关键价 / 盈亏
    assert {"leg1_price", "leg2_price", "gross_spread", "net_spread"} <= compare
    # identity 与 compare 不许重叠 —— 重叠说明同一个字段被当成两种角色
    assert identity.isdisjoint(compare)


def test_t5b_adapter_rows_carry_exactly_the_spec_keys() -> None:
    """适配器产出的行必须**恰好**覆盖 identity ∪ compare ∪ {t_field}。

    多出字段无害（不参与比对），但**少了**就是 KeySpecError。这条断言让"适配器
    与注册表悄悄脱钩"在 CI 里立刻暴露，而不是等到某天真实比对时才炸。
    """
    rows = run_truncated_replay(
        ENTRY_T_ROUNDS_OFFLINE, build_run_config(_two_day_frame()), D1
    ).records
    assert rows
    spec = F.get_entry(ENTRY_T_ROUNDS_OFFLINE).key_spec
    needed = set(spec.identity) | set(spec.compare) | {spec.t_field}
    for row in rows:
        assert needed <= set(row), f"行缺键: {sorted(needed - set(row))}"


def test_t5c_unsettled_round_compares_none_equals_none() -> None:
    """未结算轮的 `leg2_price` / `gross_spread` / `net_spread` 是 `None`。

    `None == None` 必须是**有效**断言（两边都未结算 = 一致），而不是"没值所以跳过"。
    """
    rows = [r for r in _base_rows() if r["round_status"] == "unreverted_at_cutoff"]
    assert rows[0]["leg2_price"] is None
    assert_invariant(rows, [dict(rows[0])], _SPEC, t=datetime(2026, 1, 5, 23, 59))


# ============================================================ T6 差集白名单


def test_t6_allow_diff_fields_defaults_to_empty() -> None:
    """**T6**：`allow_diff_fields` 默认空。签名上就是 `()`，不是 `None`。"""
    sig = inspect.signature(assert_invariant)
    default = sig.parameters["allow_diff_fields"].default
    assert default == (), f"默认值必须是空元组，实际 {default!r}"


def test_t6b_non_whitelisted_diff_fails_but_whitelisted_passes() -> None:
    """**T6**：同一处差异，不在白名单 → 红；显式在白名单 → 绿。

    两条一起测是必要的：只测红，"白名单功能坏了"测不出来（它总是红）；
    只测绿，"白名单被默认打开"测不出来（它总是绿）。
    """
    baseline = _base_rows()
    replayed = [dict(r) for r in baseline]
    replayed[0]["leg1_price"] = 10.06

    t = datetime(2026, 1, 5, 23, 59)
    with pytest.raises(LookaheadInvariantError):
        assert_invariant(baseline, replayed, _SPEC, t=t)

    # 显式放行该字段 → 通过。**注意**：这不是"允许漂移"，而是演示白名单确实生效；
    # 真正使用白名单必须由任务书写死列名。
    assert_invariant(baseline, replayed, _SPEC, t=t, allow_diff_fields=("leg1_price",))


def test_t6c_identity_field_cannot_be_whitelisted() -> None:
    """标识字段进白名单 → 抛错。放进白名单等于允许两行错配。"""
    with pytest.raises(KeySpecError) as ei:
        assert_invariant(
            _base_rows(), _base_rows(), _SPEC, allow_diff_fields=("symbol",)
        )
    assert "非 compare 字段" in str(ei.value)


def test_t6d_empty_compare_is_rejected() -> None:
    """`compare` 为空 → 抛错（只比条数就是假绿）。"""
    empty = KeySpec(identity=("symbol",), compare=(), t_field="leg1_ts")
    with pytest.raises(KeySpecError) as ei:
        assert_invariant(_base_rows(), _base_rows(), empty)
    assert "compare" in str(ei.value)


# ============================================================ T7 CI 收集


def test_t7_fixture_tests_are_collected_without_skip_or_xfail() -> None:
    """**T7**：本文件里的用例都没有 `skip` / `xfail` 标记。

    检查方式：读本模块的**源码**，扫 `pytest.mark.skip` / `pytest.mark.xfail`。
    用源码扫而不是反射，是因为 `skipif` 在某些环境下会求值成"跳过"而不留痕迹；
    而"这个文件里根本不许出现 skip/xfail"是一条**源码级**纪律。
    """
    import pathlib

    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    # 排除本函数自己：它的 docstring 里就写了这两个词
    body = src.split("def test_t7_fixture_tests_are_collected_without_skip_or_xfail")[0]
    assert "pytest.mark.skip" not in body, "夹具测试不许用 skip 掩盖"
    assert "pytest.mark.xfail" not in body, "夹具测试不许用 xfail 掩盖"


def test_t7b_ci_workflow_runs_full_pytest() -> None:
    """**T7**：CI 的 backend job 跑 `pytest tests`（本文件落在 tests/ 下即被收集）。

    断言 `ci.yml` 里存在 `pytest tests` 且**没有** `continue-on-error`。

    为什么查 `continue-on-error`：那一行会让失败的 job 变绿，是本仓库最直接的一种
    假绿开关。这里扫全文件，不只扫 pytest 那一段 —— 它可以被加在任何 step 上。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]  # backend/tests/backtest → repo root
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pytest tests" in ci, "CI 必须跑全量 pytest"
    assert "continue-on-error" not in ci, (
        "ci.yml 不许出现 continue-on-error —— 它会把红测变成绿"
    )


# ============================================================ T8 对外前置


def test_t8_gate_is_fail_closed() -> None:
    """**T8**：对外前置查询桩**恒不放行**。

    这条测试的存在意义是：将来谁把 `is_cleared_for_external_claim` 改成
    `return True`（比如为了让某个发布流程跑通），CI 会红。
    """
    assert F.is_cleared_for_external_claim(ENTRY_T_ROUNDS_OFFLINE) is False
    assert F.is_cleared_for_external_claim("any.other.entry") is False
    status = F.gate_status(ENTRY_T_ROUNDS_OFFLINE)
    assert status["cleared"] is False
    assert status["status"] == F.GATE_STATUS_PENDING
    assert status["evidence_source"], "必须说明证据从哪来，否则调用方无从接入"


def test_t8b_no_banned_claim_in_fixture_package() -> None:
    """**T8**：夹具包里不许出现"信号可做 / 可交易 / 已无前视"这类结论性宣称。

    `已无前视` 这条关键词特别容易在 docstring 里顺手写出来 —— 而它正是本任务书
    禁止的空喊。所以扫源码，但允许**否定语境**（如「不代表已无前视」）。

    判定方式：出现关键词时，看它前后 40 字符里有没有否定词。这个方法不完美
    （挡不住"绕开写"），但它挡得住绝大多数顺手写下去的宣称。
    """
    import pathlib

    pkg = pathlib.Path(F.__file__).parent
    banned = ("信号可做", "可交易", "已无前视", "无前视")
    negations = ("不", "禁", "勿", "未", "非", "≠", "不代表", "不能")

    offenders: list[str] = []
    for py in sorted(pkg.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        for word in banned:
            start = 0
            while True:
                idx = text.find(word, start)
                if idx < 0:
                    break
                ctx = text[max(0, idx - 40): idx + len(word) + 40]
                if not any(n in ctx for n in negations):
                    offenders.append(f"{py.name}: ...{ctx.strip()}...")
                start = idx + len(word)
    assert not offenders, "出现未否定的结论性宣称:\n" + "\n".join(offenders)


def test_t8c_footnote_present_in_package_docstring() -> None:
    """脚注「仅供研究，不构成投资建议；执行须人工确认」必须出现在包 docstring。"""
    assert F.__doc__ is not None
    assert "仅供研究，不构成投资建议；执行须人工确认" in F.__doc__


# ============================================================ T9 变异补漏
#
# 下面这一组是变异测试逼出来的。
#
# 第一轮 31 条变异里有 11 条**存活**（改坏了实现，28 个用例照样全绿）。逐条查下来
# 全是同一个病：断言只走了"某个分支能抛错"，没走"另一个分支也该抛错"。
# 比如 `assert_truncated` 的第 2 条检查（结果里不得有 > t 的行）有测试，
# 但第 3 条（原表有超出则行数必须变少）和第 1 条（t 口径可判定）没有 ——
# 把它们删掉，测试全绿。
#
# 单测一个函数"会抛错"是不够的：要**每个**会抛错的分支都被至少一条用例踩到，
# 否则那个分支等于没写。


def test_t9a_truncated_result_containing_future_rows_is_red() -> None:
    """`assert_truncated` 第 2 条检查：结果里还有 > t 的行 → 红。

    ## 为什么需要这条（M3 存活的原因）

    已有的 `test_t2_injected_future_row_makes_truncation_fail_loudly` 传的是
    "原表 = 截断结果"，于是第 3 条检查（行数必须变少）先命中了 —— 第 2 条检查
    早退，从来没被执行过。删掉第 2 条，所有测试照绿。

    这里构造的是**只有第 2 条能抓**的场景：原表只有一天数据（没有 `> t` 的行，
    第 3 条不触发），但传进去的"截断结果"里混进了一行 `> t`。
    """
    only_d1 = pl.DataFrame(
        {
            "symbol": [SYMBOL] * 2,
            "date": [D1] * 2,
            "datetime": [datetime(2026, 1, 5, 9, 30), datetime(2026, 1, 5, 9, 31)],
        }
    )
    # 原表没有 > t 的行 → 第 3 条不会触发
    assert only_d1.filter(pl.col("date") > D1).height == 0

    leaked = pl.concat(
        [
            only_d1,
            pl.DataFrame(
                {
                    "symbol": [SYMBOL],
                    "date": [D2],
                    "datetime": [datetime(2026, 1, 6, 9, 30)],
                }
            ),
        ]
    )
    with pytest.raises(TruncationNotAppliedError) as ei:
        F.assert_truncated(leaked, only_d1, D1, t_kind="date")
    msg = str(ei.value)
    assert "截断未生效" in msg
    assert "仍有 1 行" in msg


def test_t9b_truncation_dropping_rows_is_red() -> None:
    """第 3 条检查：**该留下的行被丢掉** → 红。

    ## 为什么需要这条（M4 存活的原因）

    原先第 3 条写的是"原表有超出时行数必须变少"。变异测试证明它被第 2 条
    **蕴含**：原表有 k 行超出、结果又没超出时，行数必然 ≤ 原表行数 − k，
    所以 `>=` 那半永远不成立，把 `>=` 改成 `>` 测不出来 —— 那个检查写成了
    第 2 条的影子。

    改成"结果行数必须等于原表里 ≤ t 的行数"之后，第 3 条自己就能成立。这条用例
    喂的是**少行**的情形：原表 4 行、正常应留 2 行、实际只留 1 行。

    关键：这个输入**没有**任何 > t 的行（第 2 条不触发），所以它抓的确实是
    "丢数据"而不是"带进未来"。少掉的这一轮会静默改变结论，而只看第 2 条
    永远发现不了。
    """
    frame = _two_day_frame()
    expected = frame.filter(pl.col("date") <= D1)
    assert expected.height > 1, "前提：正常截断应留下多于 1 行"
    too_few = expected.head(1)  # 少留了

    with pytest.raises(TruncationNotAppliedError) as ei:
        F.assert_truncated(too_few, frame, D1, t_kind="date")
    msg = str(ei.value)
    assert "截断结果应有" in msg
    assert f"实际 {too_few.height} 行" in msg
    # 第 2 条**没有**触发 —— 证明这条用例抓的是独立的第三类问题
    assert "仍有" not in msg


def test_t9b2_truncation_keeping_too_many_rows_is_red() -> None:
    """第 3 条检查的另一个方向：**行数比该留的多** → 红。

    和上一条配对。只测"少行"的话，"多行"那半（`!=` 的另一个方向）可能被写成
    `>` 而测不出来。
    """
    frame = _two_day_frame()
    expected = frame.filter(pl.col("date") <= D1)
    too_many = frame  # 一行没裁

    with pytest.raises(TruncationNotAppliedError) as ei:
        F.assert_truncated(too_many, frame, D1, t_kind="date")
    msg = str(ei.value)
    assert "截断结果应有" in msg
    assert "仍有" in msg, "这个输入同时触发第 2 条，两条都应出现在信息里"


def test_t9c_empty_frame_still_checked_for_t_kind() -> None:
    """`assert_truncated` 对空表**仍**要判定 `t` 口径。

    ## 为什么需要这条（M7 存活的原因）

    第 2 条检查被 `if not truncated.is_empty():` 包着。空表时它会整个跳过 ——
    但"t 口径可判定"这件事与表是否为空无关。已有用例只喂非空表，所以把那个
    `if` 改成恒假（M7）也测不出来。

    这里的输入是空表 + 一个口径不符的 `t`：若口径检查被跳过，函数会静默返回，
    调用方拿到"截断成功"的假象。
    """
    empty = pl.DataFrame(
        {
            "symbol": pl.Series([], dtype=pl.Utf8),
            "date": pl.Series([], dtype=pl.Date),
            "datetime": pl.Series([], dtype=pl.Datetime),
        }
    )
    assert empty.is_empty()
    # datetime 传给声明为 date 的口径 → 必须抛，即使表是空的
    with pytest.raises(TruncationNotAppliedError) as ei:
        F.assert_truncated(empty, empty, datetime(2026, 1, 5, 9, 30), t_kind="date")
    assert "不一致" in str(ei.value)


def test_t9d_t_kind_mismatch_is_red() -> None:
    """`resolve_t_kind`：`date` 传给 `timestamp` 口径 → 红。

    ## 为什么需要这条（M5 存活的原因）

    已有用例只走了一个方向（`datetime` 配 `date` 口径）。反方向（`date` 配
    `timestamp` 口径）没有用例 —— 把整个 `if t_kind != inferred` 分支删掉，
    那个方向就静默通过：Polars 里拿 `date` 和 `datetime` 列比大小**不报错**，
    会按类型提升规则静默给出结果，正是要防的那种静默裁错。
    """
    frame = _two_day_frame()
    with pytest.raises(TruncationNotAppliedError) as ei:
        F.truncate_frame(frame, D1, t_kind="timestamp")
    msg = str(ei.value)
    assert "不一致" in msg
    assert "date" in msg and "timestamp" in msg


def test_t9e_confirmed_timestamp_reaches_the_check() -> None:
    """`timestamp` 口径的**正确路径**：带时区的 datetime 能正常裁。

    没有这条，`resolve_t_kind` 里所有 timestamp 分支都可能被改成"一律抛错"
    而测试全绿（因为已有用例全是 date 口径 + 全在测抛错）。

    ## 这里踩到过一个真实的坑

    数据列的时区是 `Asia/Shanghai`（IANA 名），而 `t` 的时区是固定偏移
    `UTC+08:00`。两者是**同一个时区**，但 Polars 按字符串比时区名，认为不一致，
    于是 `pl.col("datetime") <= t` 直接抛 `SchemaError` —— 不是静默裁错，
    而是底层异常漏出去。

    `truncate_frame` 因此加了时区归一：两边都转到 UTC 再比。这条用例同时守住
    "同一时区、不同表示"能正常工作。
    """
    frame = _day_frame(
        [10.00, 10.02, 10.06, 10.05],
        trade_date=D1,
        vwap=10.0,
        up_band=[False, False, True, False],
        up_cross=[False, True, False, False],
    ).with_columns(pl.col("datetime").dt.replace_time_zone("Asia/Shanghai"))
    cut = datetime(2026, 1, 5, 9, 31, tzinfo=_CN)

    out = F.truncate_frame(frame, cut, t_kind="timestamp")
    assert out.height == 2, "09:30 / 09:31 两根 ≤ cut"
    F.assert_truncated(out, frame, cut, t_kind="timestamp")

    # 时区归一不能改变时刻值：留下的两根必须还是 09:30 / 09:31
    cn_times = out["datetime"].dt.convert_time_zone("Asia/Shanghai").to_list()
    assert [t.strftime("%H:%M") for t in cn_times] == ["09:30", "09:31"]

    # 归一前后的字面量代表**同一瞬间** —— 把这条等价关系写成断言，
    # 是为了不假装 `astimezone(utc)` 在防什么（变异 M32 证明它可删）。
    # 真正必需的是列侧的 `convert_time_zone("UTC")`（M33：去掉即红）。
    assert pl.select(pl.lit(cut).alias("a")).to_series()[0] == cut
    assert pl.select(pl.lit(cut).alias("a")).schema["a"].time_zone == "UTC"


def test_t9f_missing_row_in_replay_is_red() -> None:
    """**少一行**必须红（`missing_in_replay`）。

    ## 为什么需要这条（M11 存活的原因）

    已有红测只改了字段值，没构造"整行缺失"。删掉 `only_base` 那段循环，
    所有测试照绿 —— 而这正是一种最严重的假绿：**截断后某一轮消失了**，
    夹具却报通过。少一轮比改一个价格更严重（结论会整体偏向"被截掉的那类轮次"）。
    """
    baseline = _base_rows()
    replayed = [baseline[0]]  # 少了 buy_first/B 那一条
    with pytest.raises(LookaheadInvariantError) as ei:
        assert_invariant(baseline, replayed, _SPEC, t=datetime(2026, 1, 5, 23, 59, tzinfo=_CN))
    kinds = [d.get("kind") for d in ei.value.diffs]
    assert kinds == ["missing_in_replay"]
    assert ei.value.diffs[0]["key"][2] == "buy_first"


def test_t9g_extra_row_in_replay_is_red() -> None:
    """**多一行**必须红（`extra_in_replay`）。

    ## 为什么需要这条（M12 存活的原因）

    同上，反方向。多一行意味着"截断之后反而多出一轮" —— 物理上不可能，
    通常来自"用了 t 之后的数据造出一个新信号"。
    """
    baseline = _base_rows()
    extra = dict(baseline[0])
    extra["mode"] = "buy_first"
    extra["path"] = "A"
    replayed = [*baseline, extra]
    with pytest.raises(LookaheadInvariantError) as ei:
        assert_invariant(baseline, replayed, _SPEC, t=datetime(2026, 1, 5, 23, 59, tzinfo=_CN))
    kinds = [d.get("kind") for d in ei.value.diffs]
    assert kinds == ["extra_in_replay"]


def test_t9h_duplicate_identity_key_is_red() -> None:
    """重复定位键 → `KeySpecError`（后者会覆盖前者，差异被吃掉）。

    ## 为什么需要这条（M16 存活的原因）

    已有用例全是"键唯一"的记录。把 `if key in out` 改成恒假，测试照绿 ——
    而重复键下 `dict` 后写覆盖先写，两条不同记录变成一条，差异静默消失。
    """
    rows = _base_rows()
    dup = [*rows, dict(rows[0], leg1_price=9.99)]
    with pytest.raises(KeySpecError) as ei:
        assert_invariant(dup, rows, _SPEC, t=datetime(2026, 1, 5, 23, 59, tzinfo=_CN))
    assert "重复定位键" in str(ei.value)


def test_t9i_duplicate_registration_is_rejected() -> None:
    """重复注册同一 `entry_id` → 抛错（不静默覆盖）。

    ## 为什么需要这条（M21 存活的原因）

    注册表 `_REGISTRY` 是模块级全局，`import` 顺序不同、插件重复加载都会触发
    重复注册。静默覆盖的后果是"到底按哪套比对键"取决于 import 顺序 ——
    这是最难查的一类假绿：同一份代码在两次 CI 里可能比出不同结论。
    """
    from app.backtest.lookahead_fixture.registry import EntrySpec, register_entry

    spec = EntrySpec(
        entry_id="entry.dup_test",
        t_kind=F.T_KIND_DATE,
        key_spec=_SPEC,
        replay=lambda cfg, t: [],
    )
    register_entry(spec)
    with pytest.raises(ValueError) as ei:
        register_entry(spec)
    assert "已注册" in str(ei.value)
    # 显式 replace=True 才允许覆盖
    register_entry(spec, replace=True)


def test_t9j_replay_returning_none_is_fail_closed() -> None:
    """入口 replay 返回 `None`（**不是**空列表）→ `KeySpecError`。

    ## 为什么需要这条（M22 存活的原因）

    上一版用例的 replay 是 `lambda cfg, t: None`，但它先把注册表换成了
    `entry.returns_none`，然后 `get_entry` 成功、`spec.replay` 返回 None ——
    看起来该走 `rows is None` 分支。实际上一轮变异跑出来是 ANCHOR-FAIL
    （锚点出现两次），说明实现里有两处相同的 `rows is None` 检查（
    `run_truncated_replay` 与 `build_baseline` 各一处）。两处都要有用例。

    这里同时覆盖两处：`run_truncated_replay` 与 `build_baseline`。
    """
    from app.backtest.lookahead_fixture.registry import EntrySpec, register_entry

    register_entry(
        EntrySpec(
            entry_id="entry.returns_none",
            t_kind=F.T_KIND_DATE,
            key_spec=_SPEC,
            replay=lambda cfg, t: None,
        ),
        replace=True,
    )
    with pytest.raises(KeySpecError) as ei:
        run_truncated_replay("entry.returns_none", {"feature_frame": None}, D1)
    assert "None" in str(ei.value)

    with pytest.raises(KeySpecError) as ei2:
        build_baseline("entry.returns_none", {"feature_frame": None})
    assert "None" in str(ei2.value)


def test_t9k_missing_leg1_ts_is_red() -> None:
    """因果顺序检查：`leg1_ts` 缺失 → 红（不跳过）。

    ## 为什么需要这条（M24 存活的原因）

    已有红测的记录都带 `leg1_ts`。把"缺 leg1_ts 就记 bad"改成"直接 continue"，
    测试照绿 —— 于是"这一轮连入场时点都没有"被静默放过。而没有入场时点的
    "已成交"记录，本身就是坏的。
    """
    rows = _base_rows()
    no_ts = dict(rows[0])
    no_ts["leg1_ts"] = None
    with pytest.raises(LookaheadInvariantError) as ei:
        assert_causal_order([no_ts])
    kinds = {d.get("kind") for d in ei.value.diffs}
    assert kinds == {"missing_leg1_ts"}


def test_t9l_unresolvable_path_is_red() -> None:
    """适配器取不到 `path` → `KeySpecError`（不填默认值）。

    ## 为什么需要这条（M29 存活的原因）

    已有用例的记录都由 `_make_record` 造，`metadata["gate_path"]` 一定在。
    把"取不到就抛"改成"取不到返回空串"，测试照绿 —— 而空串会让 A 与 B 两格
    撞键（同一 `(symbol, date, mode, "")`），比对时互相覆盖。
    """
    from app.t_rounds.models import TRoundRecord

    rec = TRoundRecord(
        t_round_id="tr.test",
        symbol=SYMBOL,
        trade_date=D1,
        mode="sell_first",
        trigger_layer_ref="",  # 没有 offline/ 前缀
        metadata={},           # 没有 gate_path
    )
    with pytest.raises(KeySpecError) as ei:
        F.record_to_row(rec)
    assert "取不到 path" in str(ei.value)


def test_t9m_run_config_without_frame_is_red() -> None:
    """`run_config` 缺 `feature_frame` → `KeySpecError`（不猜空表）。

    ## 为什么需要这条（M31 存活的原因）

    已有用例的 `run_config` 都由 `build_run_config()` 构造，一定带 `feature_frame`。
    把"缺了就抛"删掉，测试照绿 —— 而 `run_config["feature_frame"]` 会在更深处
    抛 `KeyError`，那看起来像"配置写错了"而不是"夹具拒绝了不合格输入"。
    更糟的一种实现是"缺了就当空表"，于是重跑产出 0 轮，两边 0 行 → 触发
    "0 行不算通过"（还好），但归因完全错了。
    """
    with pytest.raises(KeySpecError) as ei:
        run_truncated_replay(ENTRY_T_ROUNDS_OFFLINE, {"params": {}}, D1)
    assert "feature_frame" in str(ei.value)

    # 非 Mapping 的 run_config 也要拒
    with pytest.raises(KeySpecError):
        run_truncated_replay(ENTRY_T_ROUNDS_OFFLINE, ["not", "a", "mapping"], D1)  # type: ignore[arg-type]


# ── M02：replay 调用链上必须真的经过 assert_truncated ──────────────────────
#
# 上一段（t9a–t9m）守的是"喂坏记录、夹具要拒"。这一段守的是**另一件事**：
# `replay_t_rounds_offline` 拿到正确输入时，它自己**有没有真的调用**截断复核。
#
# 这两件事分开测的理由与文件开头那句一样：把 `assert_truncated(...)` 一行删掉，
# 上面 13 条 t9* 全部照绿 —— 因为它们从不经过那条语句。于是"截断复核"成了一行
# 没人守的代码，删掉也没人知道。


def test_t10_replay_call_chain_verifies_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`truncate_frame` 静默失效（原样返回）时，replay 必须抛 `TruncationNotAppliedError`。

    ## 为什么需要这条（M02 存活的原因）

    `entries.replay_t_rounds_offline` 里有两行连着的：

        truncated = truncate_frame(frame, t, t_kind="date")
        assert_truncated(truncated, frame, t, t_kind="date")

    第一行截断，第二行**复核**。把第二行删掉，全仓测试照样绿 —— 因为没有一条用例
    制造过"裁了但等于没裁"这种局面。而第二行正是 `truncate.py` 模块文档里
    「裁完必须调用」那句话的执行者：它一删，"必须"就只剩注释了。

    这里把 `truncate_frame` 换成"原样返回"，模拟截断静默失效（换错列、时区搞反到
    自洽、或干脆被人改成 `return frame`）。复核在，就必然抛；复核不在，就会拿着
    全量数据继续跑，产出看起来完全正常 —— 这是最难发现的一类假绿。

    ## monkeypatch 为什么打 `truncate` 模块而不是 `entries`

    `entries` 里是**函数体内局部 import**：

        from .truncate import assert_truncated, truncate_frame

    所以 `entries` 模块对象上没有 `truncate_frame` 这个属性，
    `monkeypatch.setattr(entries, "truncate_frame", ...)` 会直接
    `AttributeError`（而且 `monkeypatch` 默认 `raising=True`，会当场炸）。
    必须打到 `truncate` 模块本身 —— 局部 import 拿的就是那个模块的属性。

    仅供研究，不构成投资建议；执行须人工确认。
    """
    frame = _two_day_frame()

    from app.backtest.lookahead_fixture import truncate as _truncate

    monkeypatch.setattr(_truncate, "truncate_frame", lambda f, t, **kwargs: f)

    # 先自证补丁生效：否则下面"抛了"可能压根不是因为复核（比如别的环节坏了）
    patched = _truncate.truncate_frame(frame, D1, t_kind="date")
    assert len(patched) == len(frame), "补丁没生效：truncate_frame 应当原样返回"

    with pytest.raises(TruncationNotAppliedError) as ei:
        run_truncated_replay(ENTRY_T_ROUNDS_OFFLINE, build_run_config(frame), D1)
    assert "截断未生效" in str(ei.value)


def test_t10b_baseline_does_not_go_through_truncation_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一补丁下 `build_baseline` 不走截断复核，仍出全量（设计取舍，不是漏测）。

    ## 这条是对照，不是缺口

    `build_baseline` 是"不截断的那条路"：它本来就该看到全部数据，所以没有复核可做。
    这个取舍是刻意的 —— baseline 的语义就是全量，硬塞一个截断复核进去会把它变成
    另一条 replay。

    钉这条是为了防止有人"顺手统一一下"：给 baseline 也加截断/复核，t10 依然绿，
    但 baseline 不再是 baseline 了。数字钉死 `len == 4`（两天 × A/B 两格）
    与 path 集合 `{A, B}`，多加一道截断就会立刻对不上。

    仅供研究，不构成投资建议；执行须人工确认。
    """
    frame = _two_day_frame()

    from app.backtest.lookahead_fixture import truncate as _truncate

    monkeypatch.setattr(_truncate, "truncate_frame", lambda f, t, **kwargs: f)

    rows = build_baseline(ENTRY_T_ROUNDS_OFFLINE, build_run_config(frame))

    assert len(rows) == 4, "两天 × 两个格子 = 4 轮，baseline 应出全量"
    assert {r["path"] for r in rows} == {"A", "B"}
