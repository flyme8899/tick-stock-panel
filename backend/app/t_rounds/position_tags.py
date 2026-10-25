"""持仓三态: `executable` / `observe_only` / `tomorrow_ref`。

## 三态各自的意思

| tag | 含义 | 能不能执行 `sell_first`(先卖后买) |
|---|---|---|
| `executable` | 有可卖老底仓(当日之前持有、已过 T+1) | 能 |
| `observe_only` | 只能观察 —— 没有底仓, 或数据不足无法判断 | 不能 |
| `tomorrow_ref` | 当日买入, 次日才可卖 | 不能(当天) |

注意这张表回答的是「**卖出腿**有没有老底仓可卖」, 不是「能不能做 T」。`buy_first`
(先买后卖) 的**卖出腿同样**必须有老底仓 —— 当日买入的份额当日卖不掉(T+1)。所以
`can_execute_sell_first` 对两种模式都是必要条件, 只是 `buy_first` 还多一个「先买」的
资金约束, 本批次没有接。

## 这一层为什么只做标签, 不做判定

真实可卖量取决于券商侧的持仓、冻结、T+1 可用量, 以及分红送转带来的调整。这些
本批次都没有接。所以 `tag_position()` 是一个**纯函数**: 你告诉我老底仓可用量和
当日买入量, 我算出应该打哪个标签; 我不去猜你实际有多少。

## 空仓不给 `executable`

`old_lot_qty=0` 时必须落到 `observe_only`, 不能因为"字段有值"就打 `executable`。
一个空仓的 `executable` 标签会让下游试图做一轮根本不存在的 T。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from . import constants as C
from .models import TRoundValidationError


@dataclass(frozen=True, slots=True)
class PositionTag:
    tag: str
    position_qty: int | None
    sellable_old_qty: int | None
    tag_reason: str

    def __post_init__(self) -> None:
        if self.tag not in C.POSITION_TAGS:
            raise TRoundValidationError(f"未知 position_tag: {self.tag!r}")

    @property
    def can_execute_sell_first(self) -> bool:
        """**卖出腿**是否有老底仓可卖 —— 两种模式通用。

        必须同时满足: 标签是 `executable` 且可卖老底仓 > 0。两个条件都查一遍是
        刻意的 —— 单看标签会漏掉"标签对但数量是 0"的边界。

        为什么名字里不带模式: 这个判断只回答"卖出腿能不能卖", 而 `sell_first` 与
        `buy_first` 的卖出腿受同一条 T+1 约束。旧名 `can_execute_zheng_t` 把约束
        绑在了 `zheng_t` 上, 于是 `dao_t` 被默认"不需要老底仓" —— 批次
        20261025gtrounds 修的就是这个错腿。
        """
        return self.tag == "executable" and bool(self.sellable_old_qty)

    @property
    def can_execute_zheng_t(self) -> bool:
        """**仅兼容旧名**, 等价于 `can_execute_sell_first`。

        保留它是因为本仓库之外可能还有调用点; 内部一律用新名。不要在新增代码里用它
        —— 这个名字会诱导出"`dao_t` 不需要老底仓"的错误推理。
        """
        return self.can_execute_sell_first

    def to_dict(self) -> dict[str, object]:
        return {
            "position_tag": self.tag,
            "position_qty": self.position_qty,
            "sellable_old_qty": self.sellable_old_qty,
            "tag_reason": self.tag_reason,
            "signal_universe_filtered_by_position": C.SIGNAL_UNIVERSE_FILTERED_BY_POSITION,
        }


def tag_position(
    *,
    old_lot_qty_available: int | None,
    same_day_buy_qty: int | None = None,
) -> PositionTag:
    """按老底仓可用量与当日买入量给出持仓标签。

    判定顺序:

    1. 老底仓可用 > 0 → `executable`
    2. 否则当日买入 > 0 → `tomorrow_ref`(明天可卖)
    3. 否则(含 None) → `observe_only`

    顺序不能反: 既有老底仓又有当日买入时, 应当 `executable` —— 老底仓部分今天就能卖。
    """
    if old_lot_qty_available is not None and old_lot_qty_available < 0:
        raise TRoundValidationError("old_lot_qty_available 不能为负")
    if same_day_buy_qty is not None and same_day_buy_qty < 0:
        raise TRoundValidationError("same_day_buy_qty 不能为负")

    position_qty = (old_lot_qty_available or 0) + (same_day_buy_qty or 0)

    if old_lot_qty_available:
        return PositionTag(
            tag="executable",
            position_qty=position_qty,
            sellable_old_qty=old_lot_qty_available,
            tag_reason=f"老底仓可卖 {old_lot_qty_available} 股",
        )
    if same_day_buy_qty:
        return PositionTag(
            tag="tomorrow_ref",
            position_qty=position_qty,
            sellable_old_qty=0,
            tag_reason=f"当日买入 {same_day_buy_qty} 股, T+1 前不可卖",
        )
    reason = "无持仓" if not position_qty else "持仓数量未知"
    return PositionTag(tag="observe_only", position_qty=position_qty or None, sellable_old_qty=0, tag_reason=reason)


__all__ = ["PositionTag", "tag_position"]
