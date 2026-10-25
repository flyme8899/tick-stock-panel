"""月度再平衡快照: 内存结构 + 纯函数工厂(批次 20261025g2 ② B3)。

## 它记的是什么

一次「如果今天要按白名单试跑阈值选股, 会选出什么」的**账面留痕**:

```
rebalance_id, pit_asof, score_version, universe_n, scored_n, whitelist_n_p40
```

用途是让"某个月的名单是怎么来的"可复核 —— 有资格的被评分票数、以及其中过了
试跑阈值的票数。没有这份留痕, 半年后没人能说清某只票当时为什么在名单上。

## 它不是什么

- **不是**调度器: 不挂 cron、不订阅事件、不看交易日历。
- **不是**持久层: 不写盘、不碰 `DiskSignalRegistryStore`、不接 `main.py` 日管道。
  Store 的默认磁盘开关仍然是关的, 这里也不打算开。
- **不是**"名单已定稿"的凭证: `whitelist_threshold_locked` 恒 False,
  `whitelist_percentile_locked` 恒 None。快照记的是**试跑**结果。

## score_version 的取法

引用已有的 `constants.SIGNAL_DESIGN_PIN` 与 `quality_proxy.QUALITY_WEIGHT_STATUS`,
不另造一个新版本号。两处版本各自代表"设计口径"与"质量/估值权重状态", 拼起来就能
回答"这份快照是在什么口径下算的"。**不**宣称任何一版"已测通"。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Iterable

from .constants import SIGNAL_DESIGN_PIN
from .models import asof_date
from .quality_proxy import QUALITY_WEIGHT_STATUS
from .quality_rank import (
    WHITELIST_PERCENTILE_LOCKED,
    WHITELIST_PERCENTILE_TRIAL,
    WHITELIST_THRESHOLD_LOCKED,
    IndustryRankResult,
)

REBALANCE_ID_PREFIX: Final = "rebal"

WHITELIST_COLUMN_TRIAL: Final = f"whitelist_n_{WHITELIST_PERCENTILE_TRIAL}"
"""试跑票数在快照里的字段名, 随试跑标签走(`whitelist_n_p40`)。

把标签拼进字段名, 是为了让"这份快照用的是哪一档"写在数据本身上。字段名叫
`whitelist_n` 的话, 换一档阈值之后新旧快照会长得一模一样。

g2.1 保留字段名 `whitelist_n_p40`(不为改名而改名), 但计数走 `quality_rank` 的
现行试跑阈值: `p40` = 行业前 40% = `q_rank_within_ind >= 0.60`(闭区间, 未锁定)。
"""


def score_version() -> str:
    """`design_pin_v1+quality:design_pending` —— 口径串, 不宣称已验证。"""
    return f"{SIGNAL_DESIGN_PIN}+quality:{QUALITY_WEIGHT_STATUS}"


class RebalanceSnapshotError(ValueError):
    """快照输入不自洽(计数为负 / scored_n > universe_n / 名单数对不上等)。"""


@dataclass(frozen=True, slots=True)
class RebalanceSnapshot:
    """一次月度再平衡的账面留痕。"""

    rebalance_id: str
    pit_asof: date
    score_version: str
    universe_n: int
    """候选池票数(含缺行业、缺分的票)。"""
    scored_n: int
    """其中**有 rank 资格**的票数(同日内同行业内可比较的那批)。"""
    whitelist_n_p40: int
    """其中通过试跑阈值的票数。"""
    whitelist_percentile_trial: str = WHITELIST_PERCENTILE_TRIAL
    whitelist_percentile_locked: str | None = WHITELIST_PERCENTILE_LOCKED
    whitelist_threshold_locked: bool = WHITELIST_THRESHOLD_LOCKED
    weighting_status: str = QUALITY_WEIGHT_STATUS

    def __post_init__(self) -> None:
        if not isinstance(self.rebalance_id, str) or not self.rebalance_id.strip():
            raise RebalanceSnapshotError("rebalance_id 不能为空")
        if self.universe_n < 0 or self.scored_n < 0 or self.whitelist_n_p40 < 0:
            raise RebalanceSnapshotError("计数不能为负")
        if self.scored_n > self.universe_n:
            # 有资格的票不可能比候选池还多。这条一破, 说明两个数字来自不同的批次。
            raise RebalanceSnapshotError(
                f"scored_n={self.scored_n} 不能大于 universe_n={self.universe_n}"
            )
        if self.whitelist_n_p40 > self.scored_n:
            # 白名单是从有资格的票里选的, 分母就是 scored_n。
            raise RebalanceSnapshotError(
                f"whitelist_n_p40={self.whitelist_n_p40} 不能大于 scored_n={self.scored_n}"
            )
        if self.whitelist_percentile_locked is not None:
            # 本批次不允许出现"已锁定"的快照。留这条而不是删掉: 将来真要锁定,
            # 必须连着改这里, 那是一次显式决定而不是改个赋值。
            raise RebalanceSnapshotError(
                "whitelist_percentile_locked 必须为 None(本批次白名单为试跑, 未锁定)"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rebalance_id": self.rebalance_id,
            "pit_asof": self.pit_asof.isoformat(),
            "score_version": self.score_version,
            "universe_n": self.universe_n,
            "scored_n": self.scored_n,
            WHITELIST_COLUMN_TRIAL: self.whitelist_n_p40,
            "whitelist_percentile_trial": self.whitelist_percentile_trial,
            "whitelist_percentile_locked": self.whitelist_percentile_locked,
            "whitelist_threshold_locked": self.whitelist_threshold_locked,
            "weighting_status": self.weighting_status,
        }


def make_rebalance_id(pit_asof: date) -> str:
    """按 PIT 日期生成快照 id, 形如 `rebal:2026-09-30`。

    用日期而不是流水号: 同一 asof 重复跑应当落到同一个 id 上, 便于对比两次跑出来
    的差异; 用自增号会让"同一天跑了两遍"变成两条互不相干的记录。
    """
    return f"{REBALANCE_ID_PREFIX}:{asof_date(pit_asof).isoformat()}"


def build_rebalance_snapshot(
    *,
    pit_asof: date,
    universe: Iterable[str],
    rank_result: IndustryRankResult,
    rebalance_id: str | None = None,
) -> RebalanceSnapshot:
    """从一次 `rank_within_industry` 的结果造快照。

    `universe` 是候选池(决定 `universe_n`), `rank_result` 决定 `scored_n` 与
    试跑票数。两者都按 symbol 去重 —— 同一只票在池子里出现两次只算一只。

    这里不做任何选择/加权/排序输出: 快照只记数量, 名单本身留在 `rank_result` 里。
    """
    symbols = {s.strip() for s in universe if isinstance(s, str) and s.strip()}
    if not symbols:
        raise RebalanceSnapshotError("universe 不能为空")

    day = asof_date(pit_asof)
    if rank_result.asof is not None and rank_result.asof != day:
        raise RebalanceSnapshotError(
            f"rank_result.asof={rank_result.asof} 与 pit_asof={day} 不一致"
        )

    entries = rank_result.entries
    unknown = symbols - set(entries)
    if unknown:
        # 池子里有票没参与分位计算, 两份输入就不是同一批数据。静默按"算过"处理会让
        # scored_n 虚高。
        raise RebalanceSnapshotError(f"universe 中有 {len(unknown)} 只票不在 rank_result 里: {sorted(unknown)[:5]}")

    scored = sum(1 for sym in symbols if not entries[sym].q_rank_missing)
    passed = sum(1 for sym in symbols if entries[sym].whitelist_pass)

    return RebalanceSnapshot(
        rebalance_id=rebalance_id or make_rebalance_id(day),
        pit_asof=day,
        score_version=score_version(),
        universe_n=len(symbols),
        scored_n=scored,
        whitelist_n_p40=passed,
    )


__all__ = [
    "REBALANCE_ID_PREFIX",
    "RebalanceSnapshot",
    "RebalanceSnapshotError",
    "WHITELIST_COLUMN_TRIAL",
    "build_rebalance_snapshot",
    "make_rebalance_id",
    "score_version",
]
