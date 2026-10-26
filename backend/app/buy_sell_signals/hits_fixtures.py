"""命中夹具: 合成"看起来像真的但不冒充生产"的测试数据。

## 为什么夹具要单独成文件

命中层的验收(可见性、空态、过期)全靠构造数据。如果每个测例自己 `SignalHit(...)`
拼一遍, 三件事会慢慢跑偏:

1. `definition_version` / `source.mode` 这类字段会被随手写成 `"x"`, 测出来的是
   "字段能塞进去", 而不是"语义对";
2. 对照组的构造条件(哪条是 candidate、哪条是 limit_reopen)会散落在各处, 改一处漏一处;
3. 最关键的是 —— 合成数据一旦写进生产路径就变成 fabricate。把它关在一个明确标注
   "仅供测例"的模块里, 至少让"这段代码被谁调用"这件事可以直接 `rg` 出来。

## 合成数据里**没有**什么

没有价格、没有数量、没有仓位。不是来不及加, 是命中层的载荷本来就不该有这些东西。
夹具如果带价格, 第一个抄它的人就会把价格写进真命中里。

## `mode` 恒为 `fixture`

夹具构造的 `HitSource.mode` 固定 `"fixture"`。生产路径读到的命中如果是 `fixture`,
那说明有人把测试数据写进了 `hits.json` —— API 不拦它(拦了反而让调试变难), 但
`fixture` 这个词在任何输出里都看得见, 不会被误当成 live。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from app.market_time import CN_TZ, cn_now, cn_today

from .hits_models import HitSource, SignalHit
from .models import SignalRecord

FIXTURE_MODE: Final = "fixture"

FIXTURE_PUBLISHER: Final = "fixture-reviewer"
"""夹具里"谁批的"。

不用真名、不用人名: 这个字符串会出现在 `manual_publisher` 字段里, 而该字段在
`publishable` 记录上是必填的。用一个人名, 等于在仓库里编造一条审批记录。
"""


def _published_record(
    *,
    signal_id: str,
    family: str = "minute_event",
    event_class: str = "intraday_volume",
    version: str = "fixture_v1",
) -> SignalRecord:
    """造一条**合法**的 publishable 定义。

    注意 `SignalRecord.__post_init__` 要求 `publishable` 必须带
    `manual_published_at` 与 `manual_publisher`, 所以这里必须显式给 —— 夹具也不能
    绕过校验走 `object.__setattr__`, 否则夹具本身就成了"如何伪造 publishable"的示范。
    """
    now = cn_now()
    return SignalRecord(
        signal_id=signal_id,
        display_name=f"夹具信号 {signal_id}",
        family=family,
        event_class=event_class,
        spec_ref="fixture://spec",
        version=version,
        status="publishable",
        push_gate="manual_pending",
        manual_publisher=FIXTURE_PUBLISHER,
        manual_published_at=now,
    )


def _candidate_record(
    *,
    signal_id: str,
    family: str = "minute_event",
    event_class: str = "intraday_volume",
    version: str = "fixture_v1",
) -> SignalRecord:
    """造一条 candidate 定义(未校准 —— 它的命中永远不该出现在监控里)。"""
    return SignalRecord(
        signal_id=signal_id,
        display_name=f"夹具候选 {signal_id}",
        family=family,
        event_class=event_class,
        spec_ref="fixture://spec",
        version=version,
    )


def _observe_record(*, signal_id: str, version: str = "fixture_v1") -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id,
        display_name=f"夹具观察 {signal_id}",
        family="minute_event",
        event_class="intraday_volume",
        spec_ref="fixture://spec",
        version=version,
        status="observe",
    )


def _limit_reopen_record(*, signal_id: str, version: str = "fixture_v1") -> SignalRecord:
    """炸板/回封定义。构造出来就是 `observe`, 状态机不许它进 publishable。"""
    return SignalRecord(
        signal_id=signal_id,
        display_name=f"夹具炸板 {signal_id}",
        family="minute_event",
        event_class="limit_reopen",
        spec_ref="fixture://spec",
        version=version,
    )


def _hit(
    *,
    hit_id: str,
    signal_id: str,
    symbol: str,
    asof: str,
    family: str = "minute_event",
    event_class: str = "intraday_volume",
    direction_label: str = "research_watch",
    definition_version: str = "fixture_v1",
    definition_status_at_hit: str | None = None,
) -> SignalHit:
    return SignalHit(
        hit_id=hit_id,
        signal_id=signal_id,
        definition_version=definition_version,
        symbol=symbol,
        asof=asof,
        source=HitSource(evaluator="fixture_evaluator", mode=FIXTURE_MODE),
        family=family,
        event_class=event_class,
        direction_label=direction_label,
        definition_status_at_hit=definition_status_at_hit,
    )


def _today_iso() -> str:
    return cn_today().isoformat()


def _yesterday_iso() -> str:
    return (cn_today() - timedelta(days=1)).isoformat()


def _t_minus(days: int) -> str:
    return (datetime.now(CN_TZ).date() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# 对照组
# ---------------------------------------------------------------------------


def publishable_definition(signal_id: str = "fx.pub.intraday_volume") -> SignalRecord:
    return _published_record(signal_id=signal_id)


def candidate_definition(signal_id: str = "fx.cand.intraday_volume") -> SignalRecord:
    return _candidate_record(signal_id=signal_id)


def observe_definition(signal_id: str = "fx.obs.intraday_volume") -> SignalRecord:
    return _observe_record(signal_id=signal_id)


def limit_reopen_definition(signal_id: str = "fx.limit_reopen") -> SignalRecord:
    return _limit_reopen_record(signal_id=signal_id)


def daily_fundamental_definition(signal_id: str = "fx.pub.daily_quality_value") -> SignalRecord:
    return _published_record(
        signal_id=signal_id,
        family="daily_fundamental",
        event_class="daily_quality_value",
    )


def composite_definition(signal_id: str = "fx.pub.composite") -> SignalRecord:
    return _published_record(
        signal_id=signal_id,
        family="composite",
        event_class="other",
    )


def publishable_hit(
    *,
    hit_id: str = "fx.hit.pub.000001",
    signal_id: str = "fx.pub.intraday_volume",
    symbol: str = "000001",
    asof: str | None = None,
) -> SignalHit:
    return _hit(
        hit_id=hit_id,
        signal_id=signal_id,
        symbol=symbol,
        asof=asof or _today_iso(),
        definition_status_at_hit="publishable",
    )


def candidate_hit(*, hit_id: str = "fx.hit.cand.000002", symbol: str = "000002") -> SignalHit:
    return _hit(
        hit_id=hit_id,
        signal_id="fx.cand.intraday_volume",
        symbol=symbol,
        asof=_today_iso(),
        definition_status_at_hit="candidate",
    )


def observe_hit(*, hit_id: str = "fx.hit.obs.000003", symbol: str = "000003") -> SignalHit:
    return _hit(
        hit_id=hit_id,
        signal_id="fx.obs.intraday_volume",
        symbol=symbol,
        asof=_today_iso(),
        definition_status_at_hit="observe",
    )


def limit_reopen_hit(*, hit_id: str = "fx.hit.limit.000004", symbol: str = "000004") -> SignalHit:
    return _hit(
        hit_id=hit_id,
        signal_id="fx.limit_reopen",
        symbol=symbol,
        asof=_today_iso(),
        event_class="limit_reopen",
        definition_status_at_hit="observe",
    )


def daily_fundamental_hit(
    *,
    hit_id: str = "fx.hit.daily.600000",
    symbol: str = "600000",
    asof: str | None = None,
) -> SignalHit:
    return _hit(
        hit_id=hit_id,
        signal_id="fx.pub.daily_quality_value",
        symbol=symbol,
        asof=asof or _today_iso(),
        family="daily_fundamental",
        event_class="daily_quality_value",
        direction_label="research_buy",
        definition_status_at_hit="publishable",
    )


def composite_hit(*, hit_id: str = "fx.hit.composite.600001", symbol: str = "600001") -> SignalHit:
    return _hit(
        hit_id=hit_id,
        signal_id="fx.pub.composite",
        symbol=symbol,
        asof=_today_iso(),
        family="composite",
        event_class="other",
        definition_status_at_hit="publishable",
    )


def stale_daily_hit(*, hit_id: str = "fx.hit.daily.stale", symbol: str = "600002") -> SignalHit:
    """昨天的基本面命中 —— 过期的对照组。"""
    return _hit(
        hit_id=hit_id,
        signal_id="fx.pub.daily_quality_value",
        symbol=symbol,
        asof=_yesterday_iso(),
        family="daily_fundamental",
        event_class="daily_quality_value",
        direction_label="research_buy",
        definition_status_at_hit="publishable",
    )


def orphan_hit(*, hit_id: str = "fx.hit.orphan.000005", symbol: str = "000005") -> SignalHit:
    """指向一条**不存在**的定义的命中(定义被删掉之后的历史遗留)。"""
    return _hit(
        hit_id=hit_id,
        signal_id="fx.missing.definition",
        symbol=symbol,
        asof=_today_iso(),
    )


def dated(days_ago: int) -> str:
    """生成 `days_ago` 天前的日期串, 供过期测例用。"""
    return _t_minus(days_ago)
