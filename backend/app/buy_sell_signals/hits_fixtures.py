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

## 校准对照(批次 20261026gbsscalib 新增)

`passed` / `pending` / `failed` 三组对照登记都在这里造, 且**只在这里**造:

- 本模块产出的 `CalibRecord` 一律带 `source="fixture://calib"`,
  `report_id="fixture://report"` —— 从任何输出上都看得出它是合成的;
- 生产读路径(`DiskCalibStore.list_all`)不调用本模块的任何函数, 夹具只能写进
  `tmp_path`(测例里显式调用 `DiskCalibStore(tmp_path).upsert(...)`);
- `hits_store` / `calib_store` 都没有"从夹具生成"的入口。有一个测例专门扫这件事。

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

from .calib_models import CalibRecord
from .constants import CALIB_COST_BP_ROUNDTRIP, CALIB_SAMPLE_N_MIN, FREQ_LOCK_VERSION
from .freq_lock import FreqLockStamp
from .hits_models import HitSource, SignalHit
from .models import SignalRecord

FIXTURE_MODE: Final = "fixture"

FIXTURE_CALIB_SOURCE: Final = "fixture://calib"
"""夹具登记里的 `source`。带 `fixture://` 前缀: 合成产物在任何输出上都看得见。"""

FIXTURE_REPORT_ID: Final = "fixture://report"
"""夹具登记里的 `report_id`。理由同上。"""

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
    freq_lock_version: str | None = None,
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
        freq_lock_version=freq_lock_version,
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
    freq_lock_version: str | None = FREQ_LOCK_VERSION,
) -> SignalHit:
    """publishable 命中, **默认带口径标签**。

    批次 20261026gbsscalib 起默认值从 `None` 改成 `FREQ_LOCK_VERSION`:
    「publishable 即可见」这个前提已经不成立了, 完整的前提是
    「publishable ∧ 口径标签 ∧ 对应 passed 登记」。把标签做成默认值, 让既有测例
    继续谈它们本来要谈的那件事(定义状态 / 事件类型 / 范围), 而不是集体卡在新闸上。

    要造「publishable 但没标签」的对照, 显式传 `freq_lock_version=None`
    (或直接用 `unlabelled_hit()`), 那正是 `calib_not_passed` 的第一个分支。
    """
    return _hit(
        hit_id=hit_id,
        signal_id=signal_id,
        symbol=symbol,
        asof=asof or _today_iso(),
        definition_status_at_hit="publishable",
        freq_lock_version=freq_lock_version,
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
    freq_lock_version: str | None = FREQ_LOCK_VERSION,
) -> SignalHit:
    """基本面命中, 默认带口径标签（理由同 `publishable_hit`）。"""
    return _hit(
        hit_id=hit_id,
        signal_id="fx.pub.daily_quality_value",
        symbol=symbol,
        asof=asof or _today_iso(),
        family="daily_fundamental",
        event_class="daily_quality_value",
        direction_label="research_buy",
        definition_status_at_hit="publishable",
        freq_lock_version=FREQ_LOCK_VERSION,
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
        freq_lock_version=FREQ_LOCK_VERSION,
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


# ---------------------------------------------------------------------------
# 校准登记对照（批次 20261026gbsscalib）
#
# 三组对照，缺一组就验不出闸是否真的 AND：
#   passed  → 配合 publishable 命中，可见
#   pending → 配合 publishable 命中，不可见（"还没结论"不等于"过了"）
#   failed  → 配合 publishable 命中，不可见（"有结论且没过"更不该显示）
# ---------------------------------------------------------------------------

#: 夹具阈值参数集。**这些数字不是标定结果**，只是为了让"改参数→指纹变"可以被测到。
#: 它们不出现在任何生产常量里，也不构成 `threshold_lock_data_pending` 被清掉的证据。
#:
#: `volume_unit`（P-bss-k4 / K4）：含 `volume_ratio_min` 就必须声明单位，否则
#: `FreqLockStamp` 直接抛错。夹具写「手」——**这是夹具口径，不是生产口径**，
#: 更不是"单位已审计"的证据。
FIXTURE_FREQ_PARAMS: Final = {
    "volume_ratio_min": 2.5,
    "breakout_pct_min": 3.0,
    "window_minutes": 30,
    "volume_unit": "手",
}

#: 另一套参数：只改了一个值，用来验"改参指纹必变"。
FIXTURE_FREQ_PARAMS_TWEAKED: Final = {
    "volume_ratio_min": 1.8,
    "breakout_pct_min": 3.0,
    "window_minutes": 30,
    "volume_unit": "手",
}

#: 同阈值、只换单位「手」→「股」。用来验**单位进指纹**：换单位＝另一套口径。
#: 注意这不是"换算"——数值一个都没动，正因为没动，两种口径才是同一个数、
#: 两个意思，才必须算出不同指纹。
FIXTURE_FREQ_PARAMS_GU: Final = {
    "volume_ratio_min": 2.5,
    "breakout_pct_min": 3.0,
    "window_minutes": 30,
    "volume_unit": "股",
}


def freq_lock_stamp(
    *,
    signal_id: str = "fx.pub.intraday_volume",
    definition_version: str = "fixture_v1",
    cadence: str = "minute_event",
    params: dict | None = None,
    freq_lock_version: str = FREQ_LOCK_VERSION,
    asof: str | None = None,
) -> FreqLockStamp:
    """造一条频率锁定戳。**仅测例**：本函数不落盘、不被生产路径调用。"""
    return FreqLockStamp(
        signal_id=signal_id,
        definition_version=definition_version,
        cadence=cadence,
        asof=asof or _today_iso(),
        params=dict(FIXTURE_FREQ_PARAMS if params is None else params),
        freq_lock_version=freq_lock_version,
        locked_at=cn_now(),
    )


def passed_calib(
    *,
    signal_id: str = "fx.pub.intraday_volume",
    definition_version: str = "fixture_v1",
    freq_lock_version: str = FREQ_LOCK_VERSION,
    sample_n: int = CALIB_SAMPLE_N_MIN,
    cost_bp_roundtrip: float = CALIB_COST_BP_ROUNDTRIP,
    eval_window: int = 60,
) -> CalibRecord:
    """造一条**合法**的 `passed` 登记。

    默认值刻意贴着门槛（`sample_n = 30`、`cost = 18.2`）：让"刚好够"成为默认，
    门槛失守时最容易被发现。要造越界的，传更小的值 —— 那会当场抛错，
    这正是测例要验的行为。
    """
    return CalibRecord(
        signal_id=signal_id,
        definition_version=definition_version,
        freq_lock_version=freq_lock_version,
        status="passed",
        eval_window=eval_window,
        sample_n=sample_n,
        cost_bp_roundtrip=cost_bp_roundtrip,
        baseline_ref="fixture://baseline/equal_weight",
        interval={"lo": 0.4, "hi": 2.1},
        report_id=FIXTURE_REPORT_ID,
        source=FIXTURE_CALIB_SOURCE,
        passed_at=cn_now(),
    )


def pending_calib(
    *,
    signal_id: str = "fx.pub.intraday_volume",
    definition_version: str = "fixture_v1",
    freq_lock_version: str = FREQ_LOCK_VERSION,
    eval_window: int = 60,
) -> CalibRecord:
    """`pending` 登记：跑过但还没结论。

    刻意**不带** `interval` / `report_id` —— "还没结论"本来就不该有这些。写上一堆
    凑数的字段会让它看起来比 `passed` 只差一个 `status` 字符串，那是假象。
    """
    return CalibRecord(
        signal_id=signal_id,
        definition_version=definition_version,
        freq_lock_version=freq_lock_version,
        status="pending",
        eval_window=eval_window,
        sample_n=0,
        cost_bp_roundtrip=CALIB_COST_BP_ROUNDTRIP,
        baseline_ref="",
    )


def failed_calib(
    *,
    signal_id: str = "fx.pub.intraday_volume",
    definition_version: str = "fixture_v1",
    freq_lock_version: str = FREQ_LOCK_VERSION,
    eval_window: int = 60,
) -> CalibRecord:
    """`failed` 登记：有结论且没过门槛（区间跨零，不劣于基线不成立）。"""
    return CalibRecord(
        signal_id=signal_id,
        definition_version=definition_version,
        freq_lock_version=freq_lock_version,
        status="failed",
        eval_window=eval_window,
        sample_n=45,
        cost_bp_roundtrip=CALIB_COST_BP_ROUNDTRIP,
        baseline_ref="fixture://baseline/equal_weight",
        interval={"lo": -1.3, "hi": 0.9},
        report_id=FIXTURE_REPORT_ID,
        source=FIXTURE_CALIB_SOURCE,
    )


def calibrated_hit(
    *,
    hit_id: str = "fx.hit.pub.000001",
    signal_id: str = "fx.pub.intraday_volume",
    symbol: str = "000001",
    definition_version: str = "fixture_v1",
    freq_lock_version: str = FREQ_LOCK_VERSION,
    asof: str | None = None,
) -> SignalHit:
    """一条**带口径标签**的 publishable 命中 —— 配合 `passed_calib()` 才可见。

    `freq_lock_version` 缺了它，无论登记怎么写都不可见（fail-closed）。所以凡是
    期望"可见"的夹具都必须走这个函数，而不是 `publishable_hit()`。
    """
    return _hit(
        hit_id=hit_id,
        signal_id=signal_id,
        symbol=symbol,
        asof=asof or _today_iso(),
        definition_version=definition_version,
        definition_status_at_hit="publishable",
        freq_lock_version=freq_lock_version,
    )


def unlabelled_hit(
    *,
    hit_id: str = "fx.hit.unlabelled.000009",
    symbol: str = "000009",
) -> SignalHit:
    """publishable 但**没有**口径标签的命中 —— 无论如何都不可见（M1 的软版本）。"""
    return _hit(
        hit_id=hit_id,
        signal_id="fx.pub.intraday_volume",
        symbol=symbol,
        asof=_today_iso(),
        definition_status_at_hit="publishable",
        freq_lock_version=None,
    )


def candidate_calibrated_hit(
    *,
    hit_id: str = "fx.hit.cand.000002",
    symbol: str = "000002",
) -> SignalHit:
    """candidate 定义 + 带口径标签的命中（M3：即便有 passed 登记也不可见）。"""
    return _hit(
        hit_id=hit_id,
        signal_id="fx.cand.intraday_volume",
        symbol=symbol,
        asof=_today_iso(),
        definition_status_at_hit="candidate",
        freq_lock_version=FREQ_LOCK_VERSION,
    )


def limit_reopen_calibrated_hit(
    *,
    hit_id: str = "fx.hit.limit.000004",
    symbol: str = "000004",
) -> SignalHit:
    """limit_reopen + 口径标签（M4：定义被手改 publishable 时仍不可见）。"""
    return _hit(
        hit_id=hit_id,
        signal_id="fx.limit_reopen",
        symbol=symbol,
        asof=_today_iso(),
        event_class="limit_reopen",
        definition_status_at_hit="observe",
        freq_lock_version=FREQ_LOCK_VERSION,
    )


def daily_calibrated_hit(
    *,
    hit_id: str = "fx.hit.daily.600000",
    symbol: str = "600000",
    asof: str | None = None,
) -> SignalHit:
    """基本面 + 口径标签（范围闸仍生效：自选 ∪ 持仓之外不可见）。"""
    return _hit(
        hit_id=hit_id,
        signal_id="fx.pub.daily_quality_value",
        symbol=symbol,
        asof=asof or _today_iso(),
        family="daily_fundamental",
        event_class="daily_quality_value",
        direction_label="research_buy",
        definition_status_at_hit="publishable",
        freq_lock_version=FREQ_LOCK_VERSION,
    )


def daily_calib_passed(
    *, signal_id: str = "fx.pub.daily_quality_value"
) -> CalibRecord:
    """基本面信号的 `passed` 登记（节拍是 `daily_fundamental`）。"""
    return passed_calib(signal_id=signal_id)
