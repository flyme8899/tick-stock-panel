"""做 T 轮次状态机: 转移表 + KPI 硬禁 + 卖出腿老底仓硬禁。

## 四条硬约束

1. **未完成 / 已放弃 / 过期复核 / 作废 一律不得进已实现 KPI**
   `count_in_realized_kpi` 只能在 `completed` 时为 True。这是整本账的核心: 把没做成
   的轮次混进"已实现", 等于只统计赢的那部分 —— 而这类偏差从汇总数字上完全看不出来。

2. **卖出腿的份额必须是老底仓**
   按 `side` 找**卖出**的那条腿(先卖后买是 leg1, 先买后卖是 leg2), 其份额来源必须
   是 `old_lot`。取 `same_day_buy` 就等于"卖掉今天买的票", 违反 T+1 —— 纸面记录里
   它看不出来, 实盘上就是个废单。

   本批次改正: 旧实现只检查 `zheng_t` 的 leg2, 而那恰恰是**买入**腿。护栏装错了
   腿, 真正的卖出腿(buy_first 的 leg2)完全无人校验。

3. **唯一能进 KPI 的入口是 `complete_round()`**
   不允许直接 `transition(..., "completed")` —— 那个入口不知道净价差算没算完。

4. **同 `(symbol, trade_date)` 超过 `max_rounds_per_day` 的轮次拒绝**
   计数口径是"**消耗掉轮次**"的状态: 已开腿的都算(含 completed /
   unreverted_at_cutoff / data_truncated / limit_locked / expired_review),
   只有 `void_data` 不算 —— 数据异常那一轮什么都没发生, 不该占掉当天的名额。

## 与 `buy_sell_signals.state_machine` 的关系

结构上参照了那个模块与 `mining_jobs` 的"非法转移抛专用异常"模式, 但转移表、状态名、
业务含义**全部独立**, 没有任何 import 关系。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from app.market_time import cn_now

from . import constants as C
from .models import TRoundRecord, TRoundValidationError, check_sell_leg_source_lot


class TRoundStateError(RuntimeError):
    """状态机相关错误的基类。"""


class InvalidTRoundTransitionError(TRoundStateError):
    """非法的轮次状态转移。对标 `InvalidMiningStatusTransitionError`。"""

    def __init__(self, *, t_round_id: str, source: str, target: str, detail: str = "") -> None:
        self.t_round_id = t_round_id
        self.source = source
        self.target = target
        self.detail = detail
        message = f"轮次 {t_round_id!r} 不允许从 {source!r} 转移到 {target!r}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class RealizedKpiRejected(TRoundStateError):
    """试图把不该进已实现 KPI 的轮次计进去。"""


class RoundQuotaExceeded(TRoundStateError):
    """同 `(symbol, trade_date)` 的轮次已用满 `max_rounds_per_day`。"""


# ---------------------------------------------------------------------------
# 转移表
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    # 第一腿成交, 等第二腿。可以放弃, 也可以作废; **不能直接 completed**
    # (第二腿还没发生)。
    "open_leg1": frozenset(
        {"open_leg2", "unreverted_at_cutoff", "limit_locked", "void_data"}
    ),
    # 第二腿成交, 等结算。可以判为未回摆 / 数据截断 / 涨跌停锁死 / 作废。
    "open_leg2": frozenset(
        {"unreverted_at_cutoff", "data_truncated", "limit_locked", "void_data"}
    ),
    # 未回摆 → 过期复核(等人工看), 或直接作废。
    "unreverted_at_cutoff": frozenset({"expired_review", "void_data"}),
    # 数据截断 → 过期复核(补齐数据后人工看), 或作废。
    "data_truncated": frozenset({"expired_review", "void_data"}),
    # 涨跌停锁死 → 过期复核(等解锁后人工看), 或作废。**不能**转成 unreverted
    # 再计 mtm —— 那个价位根本没有成交, 按它盯市等于凭空捏一个平仓价。
    "limit_locked": frozenset({"expired_review", "void_data"}),
    # 终态之间不互相流转。
    "completed": frozenset(),
    "expired_review": frozenset(),
    "void_data": frozenset(),
}
"""合法转移表。

两个"没做成"的终态分开, 因为它们的事后处置完全不同:

- `unreverted_at_cutoff`: 交易时段完整走完, 策略没等到回摆 → **可以计 mtm**
  (按 `ts<=14:57:00` 最后一根 `price` 平掉), 数据本身没问题。
- `data_truncated`: 交易时段没走完数据就断了 → 先查数据, 不能拿残缺数据算盈亏。

`→ expired_review` 是唯一"终态之后还能再走一步"的边 —— 结论需要人看一眼。
"""

STATUSES_CONSUMING_QUOTA: Final = frozenset(
    {
        "open_leg1",
        "open_leg2",
        "completed",
        "unreverted_at_cutoff",
        "data_truncated",
        "limit_locked",
        "expired_review",
    }
)
"""消耗当日轮次名额的状态。

`void_data` **不在**里面: 数据异常那一轮什么都没发生(可能第一腿都没真成交就被判
脏了), 不该占掉当天唯一的名额。其余只要是"真的开过腿"的, 无论成败都算用掉一轮 ——
否则"一轮失败就重来"会让单日轮次上限形同虚设。

## 为什么必须纳入三个结局桶(批次 20261025gtrounds_patch ⑤)

批次 20261025gtrounds 把结局桶拆成 `unreverted_at_cutoff`(走到截点没回摆) /
`data_truncated`(数据提前断) / `limit_locked`(涨跌停锁死), 但这里当时还写着旧名
`incomplete` / `abandoned`。后果不是"漏统计"那么轻 —— 这三个桶都是**真的开过腿**的,
它们不进集合意味着当天配额会按"没用掉"回算, 于是同一天能再开一轮: 一个反复回摆
不成的票, 可以靠着失败刷出无限次机会。这是**配额空洞**, 不是统计口径问题。

`void_data` 仍然不在集合里, 与本批次无关, 保持不变。
"""


def assert_transition_allowed(record: TRoundRecord, target: str) -> None:
    """只检查不执行。"""
    if target not in C.ROUND_STATUSES:
        raise TRoundValidationError(f"未知目标状态: {target!r}")

    if target == "completed":
        raise InvalidTRoundTransitionError(
            t_round_id=record.t_round_id,
            source=record.round_status,
            target=target,
            detail="completed 只能由 complete_round() 进入(那里才检查净价差与卖出腿老底仓(sell_first/buy_first))",
        )

    allowed = ALLOWED_TRANSITIONS.get(record.round_status, frozenset())
    if target not in allowed:
        raise InvalidTRoundTransitionError(
            t_round_id=record.t_round_id,
            source=record.round_status,
            target=target,
            detail=f"合法目标: {sorted(allowed) or '无(终态)'}",
        )


def transition(
    record: TRoundRecord,
    target: str,
    *,
    note: str | None = None,
    leg1_status: str | None = None,
    leg2_status: str | None = None,
    leg1_source_lot: str | None = None,
    leg2_source_lot: str | None = None,
) -> TRoundRecord:
    """推进到目标状态, 返回**新**记录。

    `note` 用于 `unreverted_at_cutoff` / `data_truncated` / `void_data`: 这三类都必须
    留下原因, 否则下次看到这条记录的人不知道当时发生了什么。

    `leg1_source_lot` / `leg2_source_lot` 都可以传 —— 因为**卖出腿是哪一条取决于
    `mode`**, 调用方不该自己先判断。两个都传也只是各自写值; T+1 校验统一走
    `_check_source_lots()`, 按 `side` 找腿, 不管卖出腿叫 leg1 还是 leg2。
    """
    assert_transition_allowed(record, target)

    if target in ("unreverted_at_cutoff", "data_truncated", "limit_locked", "void_data") and not (
        note and note.strip()
    ):
        raise TRoundValidationError(f"转入 {target} 必须提供 note 说明原因")

    changes: dict[str, object] = {"round_status": target, "last_status_change_at": cn_now()}
    if target == "unreverted_at_cutoff":
        changes["unreverted_reason"] = note.strip()
    if target == "data_truncated":
        changes["truncated_reason"] = note.strip()
        changes["incomplete_flag"] = True
    if target == "limit_locked":
        changes["limit_locked_reason"] = note.strip()
    if note and target in ("void_data",):
        meta = dict(record.metadata)
        meta["last_void_reason"] = note.strip()
        changes["metadata"] = meta

    if leg1_status is not None:
        if leg1_status not in C.LEG_STATUSES:
            raise TRoundValidationError(f"未知 leg1_status: {leg1_status!r}")
        changes["leg1_status"] = leg1_status
    if leg2_status is not None:
        if leg2_status not in C.LEG_STATUSES:
            raise TRoundValidationError(f"未知 leg2_status: {leg2_status!r}")
        changes["leg2_status"] = leg2_status
    if leg1_source_lot is not None or leg2_source_lot is not None:
        _check_source_lots(record, leg1_source_lot=leg1_source_lot, leg2_source_lot=leg2_source_lot)
    if leg1_source_lot is not None:
        changes["leg1_source_lot"] = leg1_source_lot
    if leg2_source_lot is not None:
        changes["leg2_source_lot"] = leg2_source_lot

    # 任何非 completed 的目标都必须把 KPI 标记清掉, 不能靠"反正构造时校验过"。
    # 一条记录可能是从 completed 之外的分支拼出来的, 清一遍最省事也最安全。
    if target != "completed":
        changes["count_in_realized_kpi"] = False
        changes["net_spread_realized"] = False

    return record.with_fields(**changes)


def _check_source_lots(
    record: TRoundRecord,
    *,
    leg1_source_lot: str | None = None,
    leg2_source_lot: str | None = None,
    require_filled: bool = False,
    target: str = "open_leg2",
) -> None:
    """**卖出腿**份额校验 —— 两条腿的来源一起判。

    位置在这里是为了在"开腿"当下就拦住, 不必等到结算。判定走
    `models.check_sell_leg_source_lot()` —— 与 `__post_init__` / `complete_round()`
    同一份定义(三处调用, 一处实现)。

    为什么两个参数都收: `sell_first` 的卖出腿是 leg1, `buy_first` 的卖出腿是 leg2。
    只收 `leg2_source_lot` 的话, `sell_first` 这条路径上 leg1 的来源永远是构造时的
    `None`, 校验会被"来源为 None 就放行"直接跳过 —— 等于没查。批次
    20261025gtrounds 的回归用例正是踩在这里。

    传进来的 `None` 在**开腿路径**上会退回记录上的已有值, 所以"只改 leg2"的旧调用
    方式行为不变。但结算路径(`require_filled=True`)**不做回退**: 那里显式传 None
    与不传是同一个意思 —— "这一轮没被确认过卖的是老底仓", 必须拒绝。
    若仍回退到记录旧值, 就会出现"调用方明确说了 None、系统却拿旧值放行"的静默
    覆盖, 而那恰恰是最需要报错的情形。

    `require_filled=True` 只在**结算路径**(`complete_round`)传: 那时卖出腿的
    来源不能还是 `None` —— 开腿前不知道是合理的, 结算时不知道就说明这一轮从没被
    确认过卖的是老底仓。判定本身仍在 `models.check_sell_leg_source_lot` 里,
    这里只透传, 不另写一份。

    `target` 是为了让报错指向**真实的目标状态**: 从 `open_leg1` 调到 `open_leg2`
    与从 `open_leg2` 结算到 `completed`, 是两条不同的边, 报错写错边会让人查错地方。
    """
    for name, value in (("leg1_source_lot", leg1_source_lot), ("leg2_source_lot", leg2_source_lot)):
        if value is not None and value not in C.SOURCE_LOTS:
            raise TRoundValidationError(f"未知 {name}: {value!r}")

    def _resolve(passed: str | None, stored: str | None) -> str | None:
        """结算路径不回退: 显式 None 就是 None; 开腿路径才退回记录旧值。"""
        if require_filled:
            return passed
        return passed if passed is not None else stored

    try:
        check_sell_leg_source_lot(
            mode=record.mode,
            leg1_side=record.leg1_side,
            leg1_source_lot=_resolve(leg1_source_lot, record.leg1_source_lot),
            leg2_side=record.leg2_side,
            leg2_source_lot=_resolve(leg2_source_lot, record.leg2_source_lot),
            t_round_id=record.t_round_id,
            require_filled=require_filled,
        )
    except TRoundValidationError as exc:
        raise InvalidTRoundTransitionError(
            t_round_id=record.t_round_id,
            source=record.round_status,
            target=target,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# 成本档位: 非零档位必须配非零金额
# ---------------------------------------------------------------------------

ZERO_COST_EXCLUDED_TIERS: Final = C.COST_BP_TIERS - {"design_pending", "0bp"}
"""白名单里**排除** `design_pending` 与 `0bp` 之后的那些档位 —— 必须配 `cost_amount > 0`。

从白名单**减去**而不是另写一份字面量: 将来在 `constants.COST_BP_TIERS` 里加一个
`25bp`, 这条规则自动覆盖它, 不需要有人记得回来改第二处。另写一份的话, 新档位会
默默变成"金额随便填", 而它看起来和白名单里别的档位一模一样。

两个被排除的档位理由不同:
- `design_pending` 是占位, 允许 0(缺省 `RoundCost()` 就是它 + 0.0);
- `0bp` 是显式零成本, **必须** 0, 由上面单独一条检查管。

仅供研究, 不构成投资建议; 执行须人工确认。
"""


# ---------------------------------------------------------------------------
# 唯一完成入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoundCost:
    """一轮的成本明细。

    `cost_bp_tier` 取 `constants.COST_BP_TIERS` 里的值, 缺省 `design_pending`:

    | 档位 | 含义 | `cost_amount` |
    |---|---|---|
    | `design_pending` | 占位, **不**宣称已按费率扣过成本 | `>= 0`(含 0) |
    | `0bp` | 显式零成本 | **必须 == 0** |
    | `5bp`/`10bp`/`15bp`/`20bp` | 按该档扣 | **必须 > 0** |

    缺省 `RoundCost()` 是 `design_pending` + `0.0` —— 这是"没测成本"的诚实默认值,
    也是绝大多数尚未做敏感性跑表的轮次该用的值。白名单之外一律拒绝, 所以随手
    填一个 `"15bp"` 或 `"tier_a_measured"` 都不会静默通过。

    为什么要卡"非零档位必须 > 0": 若 `10bp` 允许 `cost_amount=0`, 一份"按 10bp
    扣成本"的报告与"根本没扣成本"的报告在数字上完全一致, 而前者会被当成已经考虑
    过成本的结论。宁可报错。

    注意这里**不**做"费率 × 名义金额"的换算 —— 闸门只验档位合法 + 金额不矛盾,
    扣的是绝对额。换算口径(用哪个价当名义)留在调用方, 免得闸门顺手替人选了口径。
    """

    cost_amount: float = 0.0
    cost_bp_tier: str = "design_pending"


def complete_round(
    record: TRoundRecord,
    *,
    leg1_price: float,
    leg2_price: float,
    qty: int,
    leg2_source_lot: str,
    leg1_source_lot: str | None = None,
    cost: RoundCost | None = None,
    leg1_ts: datetime | None = None,
    leg2_ts: datetime | None = None,
) -> TRoundRecord:
    """唯一能把轮次推进到 `completed` 并计入已实现 KPI 的入口。

    四道检查:

    1. 当前状态必须是 `open_leg2`(两腿都已成交才谈得上完成)
    2. **卖出腿**的份额来源必须是 `old_lot`(按 `side` 找腿, 两种模式通用)
    3. 两腿价格 / 数量必须有限且为正
    4. 成本档位须 ∈ `C.COST_BP_TIERS` 白名单, 且 `cost_amount` 按档位规则:
       恒非负; `design_pending` 允许 0; `0bp` 必须 == 0; `5bp`/`10bp`/`15bp`/
       `20bp` 必须 > 0(禁 silent 0 冒充已测)

    `leg1_source_lot` 是可选参数: 只对 `sell_first`(卖出腿是 leg1)有意义。
    不传就沿用记录上的值 —— 构造期已经填过并校验过的情况不必重复传。

    净价差 = `(卖出腿价 - 买入腿价) * qty`, 一条式子覆盖两种模式:

    - 先卖后买(sell_first): 卖出腿是 leg1 → `(leg1 - leg2) * qty`
    - 先买后卖(buy_first):  卖出腿是 leg2 → `(leg2 - leg1) * qty`

    写错方向的后果是全部符号反过来 —— 一个永远亏的策略看起来很诚实, 只是没人会
    注意到它其实是反的。
    """
    if record.round_status != "open_leg2":
        raise InvalidTRoundTransitionError(
            t_round_id=record.t_round_id,
            source=record.round_status,
            target="completed",
            detail="只有 open_leg2(两腿均已成交)才能结算完成",
        )
    # 结算路径: 卖出腿来源**不能是 None**(填了还必须是 old_lot)。
    # 开腿前/构造期的 None 仍放行 —— 那时本就知道不了。判据只有这一处定义。
    _check_source_lots(
        record,
        leg1_source_lot=leg1_source_lot,
        leg2_source_lot=leg2_source_lot,
        require_filled=True,
        target="completed",
    )

    for name, value in (("leg1_price", leg1_price), ("leg2_price", leg2_price)):
        if not isinstance(value, (int, float)) or value <= 0:
            raise TRoundValidationError(f"{name} 必须是正数")
    if not isinstance(qty, int) or qty <= 0:
        raise TRoundValidationError("qty 必须是正整数")

    cost = cost or RoundCost()
    # 闸门 = 白名单成员资格。合法取值单一定义在 constants.COST_BP_TIERS, 这里不
    # 另抄一份字面量 —— 抄出来的第二份迟早与 constants 漂移, 而漂移的表现是
    # "某个档位在一个入口能过、在另一个入口报错"。拼错的新档位(比如 'bp_10')
    # 也会落在这里被拒, 不会当成"未知但先放行"。
    if cost.cost_bp_tier not in C.COST_BP_TIERS:
        raise TRoundValidationError(
            f"cost_bp_tier 必须属于白名单 {sorted(C.COST_BP_TIERS)}, "
            f"收到 {cost.cost_bp_tier!r}"
        )
    if not isinstance(cost.cost_amount, (int, float)) or cost.cost_amount < 0:
        raise TRoundValidationError("cost_amount 必须是非负数")
    # 金额必须与档位自洽, 否则档位就只是个标签。两条规则方向相反:
    #   0bp   → 写着零成本却扣了钱, 自相矛盾 → 拒
    #   非零档 → 写着按 N bp 扣却一分没扣, 等于"已测"的假象 → 拒
    # 后一条是本拍最要紧的: 允许它, 一份 10bp 的报告与"根本没扣"在数字上一模一样,
    # 而前者会被当成已经考虑过成本的结论。见 constants.COST_BP_TIERS 注释。
    if cost.cost_bp_tier == "0bp" and cost.cost_amount != 0:
        raise TRoundValidationError(
            f"cost_bp_tier='0bp' 时 cost_amount 必须为 0, 收到 {cost.cost_amount!r}"
        )
    if cost.cost_bp_tier in ZERO_COST_EXCLUDED_TIERS and not (cost.cost_amount > 0):
        raise TRoundValidationError(
            f"cost_bp_tier={cost.cost_bp_tier!r} 时 cost_amount 必须 > 0, "
            f"收到 {cost.cost_amount!r}(禁止 silent 0 冒充已测)"
        )

    # 价差方向按「哪条腿是卖出腿」决定, 而不是按模式名字符串:
    #   卖出腿在 leg1(先卖后买) → 卖得高才赚 → leg1 - leg2
    #   卖出腿在 leg2(先买后卖) → 卖得高才赚 → leg2 - leg1
    # 写成"卖出价 - 买入价"这一个式子, 就不存在两种模式各写一遍写反的可能。
    sell_px, buy_px = (
        (float(leg1_price), float(leg2_price))
        if record.leg1_side == "sell"
        else (float(leg2_price), float(leg1_price))
    )
    gross = (sell_px - buy_px) * qty
    net = gross - float(cost.cost_amount)

    now = cn_now()
    return record.with_fields(
        round_status="completed",
        leg1_status="filled",
        leg2_status="filled",
        leg1_price=float(leg1_price),
        leg2_price=float(leg2_price),
        leg1_qty=qty,
        leg2_qty=qty,
        leg1_ts=leg1_ts or record.leg1_ts or now,
        leg2_ts=leg2_ts or record.leg2_ts or now,
        # 两条腿的来源都落库: 只写 leg2 的话, `sell_first` 的卖出腿(leg1)
        # 校验通过了却查不到它当初填的是什么 —— 审计时无从复核。
        leg1_source_lot=leg1_source_lot if leg1_source_lot is not None else record.leg1_source_lot,
        leg2_source_lot=leg2_source_lot,
        gross_spread=gross,
        cost_amount=float(cost.cost_amount),
        cost_bp_tier=cost.cost_bp_tier,
        net_spread=net,
        net_spread_realized=True,
        count_in_realized_kpi=True,
        incomplete_flag=False,
        last_status_change_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# 轮次上限
# ---------------------------------------------------------------------------


def check_round_quota(existing: list[TRoundRecord], symbol: str, trade_date: date) -> None:
    """检查同 `(symbol, trade_date)` 是否还有轮次名额。超了就抛 `RoundQuotaExceeded`。

    `existing` 由调用方(Store)给出, 这样可以避免这里反向依赖 Store。
    """
    used = sum(
        1
        for rec in existing
        if rec.symbol == symbol
        and rec.trade_date == trade_date
        and rec.round_status in STATUSES_CONSUMING_QUOTA
    )
    if used >= C.MAX_ROUNDS_PER_DAY:
        raise RoundQuotaExceeded(
            f"{symbol} 在 {trade_date.isoformat()} 已用掉 {used} 轮"
            f"(max_rounds_per_day={C.MAX_ROUNDS_PER_DAY}); 同票同日不允许多轮"
        )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidTRoundTransitionError",
    "RealizedKpiRejected",
    "RoundCost",
    "RoundQuotaExceeded",
    "STATUSES_CONSUMING_QUOTA",
    "TRoundStateError",
    "ZERO_COST_EXCLUDED_TIERS",
    "assert_transition_allowed",
    "check_round_quota",
    "complete_round",
    "transition",
]
