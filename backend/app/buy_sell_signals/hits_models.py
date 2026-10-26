"""买卖信号**命中层**记录。

## 为什么必须和 `SignalRecord` 分开

`SignalRecord` 回答的是「这个信号是什么、现在能不能发布」, 它描述的是**定义**,
全仓只有几十条, 且从来没有 `symbol` —— 定义不知道也不该知道哪只票命中了它。

命中是另一件事: 「某某票, 在某某时点, 按某某版本的定义, 被判定为命中」。它描述的是
**事件**, 天然按股票 × 时间增长。把这两件事塞进同一个记录类型, 会出现三个立刻能看到的
坏处:

1. 定义改版时, 历史命中会被一起改写 —— 而历史命中是不能被改写的。
2. `definition_version` 无处安放, 于是「这条命中是按哪版定义算的」永久丢失。
3. 状态机被污染: 定义有 `candidate → observe → publishable` 这条路, 命中没有。

所以本模块独立成层, 只从定义**拷贝** `family` / `event_class`, 不做任何反向写入。

## `definition_version` 为什么要钉死

定义改版后, 旧命中仍然要按**当时那一版**的定义来解释。如果读侧按「当前版」重新解释,
一条按 v1 口径算出来的命中会被 v2 的措辞描述, 而且从输出上完全看不出来 —— 这是最
难在事后发现的一类失真。所以每条命中自带版本快照, 读侧不得跨版本重读。

## 载荷里**不许**出现的东西

价格、数量、仓位、订单号、可执行指令、模拟盘下单字段。命中层是研究记录,
不是交易指令。`direction_label` 是封闭集里的研究标签(`research_buy` 等),
不是「买入」这个动作。这条约束在 `__post_init__` 里逐项拒绝, 而不是靠约定。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from app.market_time import CN_TZ, cn_today

from . import disclaimer as _disclaimer
from .constants import (
    DIRECTION_LABELS,
    DISCLAIMER_KEY,
    EVENT_CLASSES,
    HIT_SOURCE_MODES,
    HITS_SCHEMA_VERSION,
    NAMESPACE,
    SIGNAL_FAMILIES,
)
from .models import SignalValidationError


class HitValidationError(SignalValidationError):
    """命中记录本身不合法。

    继承 `SignalValidationError` 是为了让"构造错了"这件事对上层只有一种捕获方式;
    单独一个类名是为了让测试和日志能一眼区分「是定义错了还是命中错了」。
    """


#: 命中载荷里**禁止出现**的键。出现即拒。
#:
#: 用键名黑名单而不是"只允许白名单字段"是刻意的: 白名单会让将来加一个正当字段也必须
#: 改这里, 而黑名单只拦真正危险的那几个词。`to_dict` 是显式列举的, 本来就不会泄出
#: 额外键 —— 这层拦的是 `from_dict` 把外部 JSON 里的脏键吃进来。
FORBIDDEN_PAYLOAD_KEYS: Final = frozenset(
    {
        "price",
        "last_price",
        "target_price",
        "stop_loss",
        "qty",
        "quantity",
        "shares",
        "position",
        "position_size",
        "weight",
        "order_id",
        "order",
        "side",
        "buy",
        "sell",
        "action",
        "instruction",
        "paper_trade",
        "paper_order",
    }
)


def _require(condition: object, message: str) -> None:
    if not condition:
        raise HitValidationError(message)


@dataclass(frozen=True, slots=True)
class HitSource:
    """命中来源。

    `mode` 只允许 `fixture` / `shadow` / `live`。本拍的生产/启动路径**没有**任何写
    `live` 的代码 —— 评估器还没锁定阈值, 也没开触发。写 `live` 需要等 P-calib。
    """

    evaluator: str
    mode: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.evaluator, str) and self.evaluator.strip(),
            "source.evaluator 不能为空",
        )
        _require(self.mode in HIT_SOURCE_MODES, f"未知 source.mode: {self.mode!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"evaluator": self.evaluator, "mode": self.mode}


@dataclass(frozen=True, slots=True)
class SignalHit:
    """一条命中记录。

    `frozen=True` 与 `SignalRecord` 一致: 命中一旦写入就不该被原地改。要修正就写一条新的
    (以新的 `hit_id`), 而不是覆盖旧的 —— 覆盖会让"以前看到过什么"这件事无法追溯。
    """

    # ---- 身份 ----------------------------------------------------------
    hit_id: str
    signal_id: str
    definition_version: str
    symbol: str

    # ---- 时点 ----------------------------------------------------------
    asof: str
    """评估 / 数据时点。ISO 日期(`2026-10-24`)或带时区的时间串。

    **刻意用字符串而不是 `date`**: 命中可能来自基本面(到日)也可能来自盘中事件(到分钟),
    而且反序列化时不能因为格式差异就丢数据。归一与过期判定见 `asof_date_or_none`。
    """

    # ---- 来源 ----------------------------------------------------------
    source: HitSource

    # ---- 分类(从定义拷贝, 展示层不许改) --------------------------------
    family: str
    event_class: str

    # ---- 研究标签 ------------------------------------------------------
    direction_label: str

    # ---- 审计 ----------------------------------------------------------
    definition_status_at_hit: str | None = None
    """写入时定义的 status 快照, 只供审计。

    **不是**可见性依据。可见性一律看定义**当前**的 status(见 `hits_api` 的闸),
    否则一条按 candidate 写入的命中, 在定义晋升为 publishable 之后会自动"变成"可见,
    而它当时根本不该被当作已校准结果。
    """

    freq_lock_version: str | None = None
    """这条命中是**按哪一套频率锁定口径**算出来的(镜像值, 批次 20261026gbsscalib)。

    ## 为什么是镜像而不是"查当前戳"

    校准登记记的是「某套口径在历史上跑出过什么」。一条命中如果是按 `freq_lock_v1`
    的参数算的, 而今天口径已经走到 `freq_lock_v2`, 拿"当前戳"去给这条老命中找登记,
    会找到一份**属于别的口径**的 `passed` —— 从输出上完全看不出来。

    所以命中自带一份口径标签, 对齐时用它, 不用"当前"。缺这个字段(`None`)的命中
    一律不可见(fail-closed): 连按哪套口径算的都不知道, 凭什么说它已校准。

    ## 它只是标签, 不是指纹

    指纹在 `freq_lock.FreqLockStamp` 上, 不落在命中里 —— 命中带指纹会让每条命中
    膨胀一大截, 而"参数有没有被改过"这件事只需要在登记侧比对一次。

    ## 这里**没有** `calibrated`

    `calibrated: bool` 是刻意不加的。一个可手改的布尔字段, 等于给"洗白"留了一个
    一字之差的入口: 把 `false` 改成 `true`, 从任何 diff 上都看不出来。可见性由
    `hits_visibility.visible_hits` 查**独立的**校准登记决定, 命中记录本身无权自称。
    """

    schema_version: str = HITS_SCHEMA_VERSION
    namespace: str = NAMESPACE
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("hit_id", "signal_id", "definition_version", "symbol", "asof"):
            value = getattr(self, name)
            _require(isinstance(value, str) and value.strip(), f"{name} 不能为空")

        _require(isinstance(self.source, HitSource), "source 必须是 HitSource")
        _require(self.family in SIGNAL_FAMILIES, f"未知 family: {self.family!r}")
        _require(self.event_class in EVENT_CLASSES, f"未知 event_class: {self.event_class!r}")
        _require(
            self.direction_label in DIRECTION_LABELS,
            f"direction_label 必须是 {sorted(DIRECTION_LABELS)} 之一, 收到 {self.direction_label!r}",
        )
        _require(self.namespace == NAMESPACE, f"namespace 必须是 {NAMESPACE!r}")
        _require(self.schema_version == HITS_SCHEMA_VERSION, "schema_version 不匹配")

        if self.recorded_at is not None:
            _require(isinstance(self.recorded_at, datetime), "recorded_at 必须是 datetime")
            _require(self.recorded_at.tzinfo is not None, "recorded_at 必须带时区")

        if self.freq_lock_version is not None:
            _require(
                isinstance(self.freq_lock_version, str) and self.freq_lock_version.strip(),
                "freq_lock_version 传了就必须是非空字符串(不传请用 None)",
            )

    # ================================================================ 派生

    @property
    def is_daily_fundamental(self) -> bool:
        """是否属于「基本面日表」形态。

        `daily_fundamental` 家族或 `daily_quality_value` 事件类都算: 后者的数据是
        日频的, 呈现方式必须和日表一致(不 toast / 带 asof / 过期显形), 否则一天几百条
        会把告警流淹掉。
        """
        return self.family == "daily_fundamental" or self.event_class == "daily_quality_value"

    @property
    def asof_day(self) -> date | None:
        """`asof` 归一到上海时区下的日期; 解析不了返回 `None`。"""
        return asof_date_or_none(self.asof)

    def is_stale(self, *, today: date | None = None) -> bool:
        """数据是否过期(N4 简易规则)。

        规则: `asof` 日期(上海) < 今日(上海) → 过期。

        **非法 asof 一律视为过期** —— 这是 fail-closed: 一条连时间都读不出来的记录,
        按「当前」呈现就是在替它担保。宁可多打一个「数据过期」的标, 也不要静默把它
        说成最新。

        不做精细交易日历(跳周末/节假日找「最近交易日」): 那需要一份准确的节假日表,
        是另一拍的事。所以节后第一天, 节前最后一个交易日的命中会显示为过期 —— 这是
        已知且可接受的保守偏差。
        """
        day = self.asof_day
        if day is None:
            return True
        ref = today if today is not None else cn_today()
        return day < ref

    # ================================================================ 序列化

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的 dict。**显式列举**, 不反射字段。"""
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "hit_id": self.hit_id,
            "signal_id": self.signal_id,
            "definition_version": self.definition_version,
            "symbol": self.symbol,
            "asof": self.asof,
            "source": self.source.to_dict(),
            "family": self.family,
            "event_class": self.event_class,
            "direction_label": self.direction_label,
            "definition_status_at_hit": self.definition_status_at_hit,
            "freq_lock_version": self.freq_lock_version,
            "recorded_at": None if self.recorded_at is None else self.recorded_at.isoformat(),
            "disclaimer_key": DISCLAIMER_KEY,
            "disclaimer": _disclaimer.DISCLAIMER_TEXT,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SignalHit:
        """从 `to_dict` 的产物重建。**反序列化不是绕过校验的后门**。"""
        return signal_hit_from_dict(payload)


def asof_date_or_none(raw: str) -> date | None:
    """把 `asof` 字符串归一到上海时区下的日期。

    接受两种写法:
    - 纯日期 `YYYY-MM-DD`
    - 带时区的 ISO 时间(转上海后取日期)

    裸时间(无时区)**不接受** —— 猜时区会让同一个文件在不同机器上解析出不同的绝对时刻,
    而且 diff 里看不出来。返回 `None` 表示"解析不了", 由调用方决定怎么呈现
    (`is_stale` 把它当过期)。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(CN_TZ).date()


_DERIVED_KEYS: Final = frozenset({"disclaimer"})


def signal_hit_from_dict(payload: dict[str, Any]) -> SignalHit:
    """从 `to_dict` 的产物重建 `SignalHit`。

    未知键丢弃(将来回滚不至于整表读不出来), 派生键丢弃(`disclaimer` 是文案快照,
    允许回灌就等于允许有人改一行 JSON 伪造合规措辞)。

    禁用键**报错而不是丢弃**: 一份带 `price` 的命中文件说明写入侧出了问题, 静默丢掉
    会让问题一直藏着。报错是让它显形。
    """
    if not isinstance(payload, dict):
        raise HitValidationError(f"from_dict 需要 dict, 收到 {type(payload).__name__}")

    bad = sorted(k for k in payload if k in FORBIDDEN_PAYLOAD_KEYS)
    if bad:
        raise HitValidationError(f"命中载荷不得包含交易字段: {bad}")

    source_raw = payload.get("source")
    if not isinstance(source_raw, dict):
        raise HitValidationError("source 必须是 {evaluator, mode} 对象")
    try:
        source = HitSource(
            evaluator=str(source_raw.get("evaluator", "")),
            mode=str(source_raw.get("mode", "")),
        )
    except HitValidationError as exc:
        raise HitValidationError(f"source 不合法: {exc}") from exc

    recorded_raw = payload.get("recorded_at")
    recorded_at: datetime | None = None
    if recorded_raw is not None:
        if not isinstance(recorded_raw, str):
            raise HitValidationError("recorded_at 必须是 ISO8601 字符串或 null")
        try:
            recorded_at = datetime.fromisoformat(recorded_raw)
        except ValueError as exc:
            raise HitValidationError(f"recorded_at 不是合法 ISO8601: {recorded_raw!r}") from exc

    kwargs: dict[str, Any] = {
        "hit_id": payload.get("hit_id"),
        "signal_id": payload.get("signal_id"),
        "definition_version": payload.get("definition_version"),
        "symbol": payload.get("symbol"),
        "asof": payload.get("asof"),
        "source": source,
        "family": payload.get("family"),
        "event_class": payload.get("event_class"),
        "direction_label": payload.get("direction_label"),
        "definition_status_at_hit": payload.get("definition_status_at_hit"),
        "freq_lock_version": payload.get("freq_lock_version"),
        "recorded_at": recorded_at,
    }
    for key in ("schema_version", "namespace"):
        if key in payload and key not in _DERIVED_KEYS:
            kwargs[key] = payload[key]
    return SignalHit(**kwargs)


__all__ = [
    "FORBIDDEN_PAYLOAD_KEYS",
    "HitSource",
    "HitValidationError",
    "SignalHit",
    "asof_date_or_none",
    "signal_hit_from_dict",
]
