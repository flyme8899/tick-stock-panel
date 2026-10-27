"""买卖信号的**频率锁定戳**(批次 20261026gbsscalib · P-calib)。

仅供研究，不构成投资建议；执行须人工确认。

## 这个模块是什么，不是什么

**是**：一个可序列化、可被哈希、能被机器比对的「评估口径身份」—— 回答
「这条信号按什么节拍、用什么阈值参数集评估」。戳里钉了三样参与判定的东西：
`signal_id`、`definition_version`、`cadence`，以及一坨 `params`（阈值参数集）。
四者一起算出一个内容指纹。

**不是**：触发引擎、阈值标定器、回测器。本模块**不读任何行情数据、不算任何量比、
不产出任何命中**，也没有任何"锁定 = 可以跑了"的含义。

## 阈值锁定 ≠ 开触发

这是本模块最容易被误读的一句话，所以写在最前面：

    有 `FreqLockStamp` ≠ `VOLUME_BREAKOUT_TRIGGERING_ENABLED = True`

`VOLUME_BREAKOUT_EVIDENCE_PENDING` 里的 `threshold_lock_data_pending` 仍然挂着
（本拍**没有**清它，也禁止清）。「阈值被写下来了一个值」和「这个值被证据支持」
是两件完全不同的事：

- 前者只需要有人填一个数；
- 后者需要一份能解释负号（见 `volume_breakout_stub.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE`）
  的回测表。

本模块交付的是**前者的容器**，不是后者。所以：戳存在、指纹稳定、序列化漂亮，
都不构成「阈值已验明」或「这条信号可以产已校准命中」。可见性最后仍由
`hits_visibility.visible_hits` 的 `passed` 闸把关，而不是由"有没有戳"把关。

## 为什么是内容指纹 + 人读标签，两个都要

`regime_caliber` 那边已经踩过这个坑，直接抄结论：`FREQ_LOCK_VERSION = "freq_lock_v1"`
这种手写版本号，忘了 bump 就完全失效 —— 而"改了两个阈值忘了改版本号"恰恰是最容易
发生的事。所以真正参与比对的是 `freq_lock_fingerprint()`：它把参与锁定的字段值本身
规范化后取哈希，改任何一个值指纹必变。

标签仍然保留，因为它覆盖指纹覆盖不到的那一类改动：**只改了代码逻辑、常量没动**。
那时候指纹一模一样，但评估行为已经变了；唯一能说"这是第几版口径"的就是这个字符串。

## 伪称同一个 `freq_lock_version` 会怎样

不会怎样 —— 单独看它不会怎样，这正是问题所在。有人把量比阈值从 2.5 改成 1.8，
版本标签仍然写 `freq_lock_v1`，单看标签完全看不出来。但指纹是从
`{signal_id, definition_version, cadence, params}` 算出来的，参数变了指纹就变。
校准登记记的是「`freq_lock_version` + 指纹」这一对，比对时**两个都要对**：
标签相同但指纹不同 → 视为另一套口径，旧登记的 `passed` 不得复用。

这条是 M6 变异矩阵要钉的东西。

## 与 `regime_caliber` 的关系：零

`app.services.regime_caliber` 描述的是**市场环境**（涨跌家数、宽度、状态分档）的
计算口径，落在 `SignalRecord.regime_caliber_version` 上。那是"市场今天是什么状态"。
本模块描述的是"某条 BSS 信号用什么参数评估"，落在 `FreqLockStamp` 上。

两者只是**都用了「内容指纹」这个技术手段**，语义毫无重叠。所以这里没有 import
`regime_caliber` 的任何符号，也不读写它的 stamp 文件。`SignalRecord.regime_caliber_version`
有值，与"信号已校准"没有任何关系 —— 这条是 M12 要钉的。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from app.market_time import CN_TZ

from .constants import (
    FREQ_LOCK_CADENCES,
    FREQ_LOCK_SCHEMA_VERSION,
    FREQ_LOCK_VERSION,
    VOLUME_UNIT_PARAM_KEY,
    VOLUME_UNIT_TRIGGER_KEYS,
    VOLUME_UNITS,
)
from .models import SignalValidationError


class FreqLockError(SignalValidationError):
    """频率锁定戳不合法。

    继承 `SignalValidationError` 让上层只有一种捕获方式; 单独类名让日志和测例能一眼
    分出「是定义错了、命中错了, 还是戳错了」。
    """


def _require(condition: object, message: str) -> None:
    if not condition:
        raise FreqLockError(message)


# ================================================ 评估单位声明(批次 20261027gbssk4 · K4)
#
# 仅供研究, 不构成投资建议; 执行须人工确认。
#
# **这个闸是什么**: `params` 里出现量相关键时, 逼调用方把「手」还是「股」写出来。
# **这个闸不是什么**: 不是「#7 量审计已过」, 不是开放触发, 不是可放量, 不是 G1 换算。
# 它只拦"没写单位", 不判断单位选得对不对 —— 那要有量审计(K8)才谈得上。


def _params_need_volume_unit(params: Mapping[str, Any]) -> bool:
    """`params` 里是否出现了量相关键(**封闭触发集** + `volume_` 前缀规则)。"""
    for key in params:
        if key in VOLUME_UNIT_TRIGGER_KEYS:
            return True
        if isinstance(key, str) and key.startswith("volume_") and key != VOLUME_UNIT_PARAM_KEY:
            return True
    return False


def require_volume_unit(params: Mapping[str, Any]) -> None:
    """含量相关键时强制 `volume_unit ∈ {手,股}`; 否则放行。

    失败一律抛 `FreqLockError`(单一可捕获类型, 不另造第二个错误类)。

    刻意**不**返回单位值: 返回它等于暗示"调用方现在可以按这个单位换算了",
    而换算(G1/B4)不在本闸范围。本函数只回答"这个 params 能不能参与评估"。

    **禁静默默认「手」**: 缺单位就是拒评。默认会让一条按股记录的量比被当成按手,
    差 100 倍, 而这个错误在输出上完全看不出来。

    空 `params` / 无量相关键 → 直接放行。"节拍已定、阈值未标定"是合法中间态,
    不能因为单位没写就连戳都不让建。
    """
    _require(isinstance(params, Mapping), "params 必须是映射")
    if not _params_need_volume_unit(params):
        return

    if VOLUME_UNIT_PARAM_KEY not in params:
        raise FreqLockError(
            f"params 含量相关键时必须声明 {VOLUME_UNIT_PARAM_KEY}∈{sorted(VOLUME_UNITS)}"
            "(缺单位拒评, 不默认「手」)"
        )

    raw = params[VOLUME_UNIT_PARAM_KEY]
    if raw is None:
        raise FreqLockError(
            f"{VOLUME_UNIT_PARAM_KEY} 不能是 None(缺单位拒评, 不默认「手」)"
        )
    if not isinstance(raw, str):
        # list / set / tuple 之类: 多值即"混口径", 不允许在这里挑一个
        raise FreqLockError(
            f"{VOLUME_UNIT_PARAM_KEY} 必须是单值字符串「手」或「股」, "
            f"收到 {type(raw).__name__}(混形态拒评)"
        )
    if raw != raw.strip() or not raw:
        raise FreqLockError(
            f"{VOLUME_UNIT_PARAM_KEY}={raw!r} 含首尾空白或为空(非法值拒评); "
            f"仅允许 {sorted(VOLUME_UNITS)}"
        )
    if "," in raw or "，" in raw:
        raise FreqLockError(
            f"{VOLUME_UNIT_PARAM_KEY}={raw!r} 看起来是多单位拼接(混形态拒评); "
            f"仅允许 {sorted(VOLUME_UNITS)}"
        )
    if raw not in VOLUME_UNITS:
        raise FreqLockError(
            f"{VOLUME_UNIT_PARAM_KEY}={raw!r} 不在封闭集 {sorted(VOLUME_UNITS)}(非法值拒评)"
        )


#: 参与指纹的字段名(**顺序即指纹输入顺序**, 但排序后序列化, 所以顺序不影响结果 ——
#: 写在这里是为了让"哪些字段进指纹"可以被测例逐项扫到, 而不是散在函数体里)。
FREQ_LOCK_FINGERPRINT_FIELDS: Final = (
    "signal_id",
    "definition_version",
    "cadence",
    "params",
)
"""指纹输入的**封闭**字段集。

新增一个参与锁定的字段, 必须加进这个元组, 否则它变了指纹也不会变。测例逐项断言
"改这一个字段 → 指纹必变", 漏加会红。
"""


def canonical_json(value: Any) -> str:
    """规范化 JSON: 键排序、无多余空白、`ensure_ascii=False`。

    `sort_keys=True` 是关键 —— 不做的话 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 会算出两个
    不同的指纹, 而它们在语义上是同一份参数集。这类噪音会让人误以为"参数被改了",
    从而不再相信指纹。

    浮点用 `repr` 语义(JSON 默认), 不做四舍五入: 把 2.55 归一成 2.5 就等于允许
    一个悄悄改变了阈值的写入通过指纹校验。
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def freq_lock_fingerprint(
    *,
    signal_id: str,
    definition_version: str,
    cadence: str,
    params: Mapping[str, Any],
) -> str:
    """算频率锁定的内容指纹(sha256 全 64 位, 不是截断)。

    ## 为什么用全 64 位而不是 `regime_caliber` 那样的前 16 位

    那个场景是「人肉比对 stamp 与当前值」, 短一点更好读。本场景的指纹要落进
    `calib.json` 并参与**机器闸的判定**: 一条登记与一条命中对齐时, 指纹是决定
    「这条 `passed` 到底是不是给这套口径背的书」的唯一依据。这种判定不该建立在
    截断哈希上 —— 截断不改变"诚实使用"的正确性, 但它把碰撞从"不可能"降到
    "理论上可能", 而代价只是多 48 个字符。这里选不省那 48 个字符。

    ## 输入就是那四个字段, 不多不少

    `params` 内部结构由调用方决定(量比 / 突破幅度 / 窗口分钟……), 但**必须**是
    JSON 可序列化的有序结构。空 `params` 是允许的 —— 它表示"这一版还没有任何阈值参数",
    这在"节拍已定、阈值未标定"的中间态里是合法且诚实的。
    """
    _require(isinstance(signal_id, str) and signal_id.strip(), "signal_id 不能为空")
    _require(
        isinstance(definition_version, str) and definition_version.strip(),
        "definition_version 不能为空",
    )
    _require(cadence in FREQ_LOCK_CADENCES, f"未知 cadence: {cadence!r}")
    _require(isinstance(params, Mapping), "params 必须是映射")

    blob = canonical_json(
        {
            "signal_id": signal_id,
            "definition_version": definition_version,
            "cadence": cadence,
            "params": dict(params),
        }
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FreqLockStamp:
    """一套评估口径的锁定戳。

    ## 钉 `definition_version` 的理由

    定义改版后，旧的校准结论**不能顺延**到新版。一条 v1 口径下跑出 30 个样本、
    过了门槛的登记，不构成对 v2 定义任何形式的背书 —— v2 改了措辞还是改了判定条件，
    读的人无从知道，而"沿用旧结论"从输出上完全看不出来。所以戳里钉死
    `definition_version`，比对时三键（`signal_id` + `definition_version` +
    `freq_lock_version`）齐对才算对齐，指纹再对一次才算同一套口径。

    ## `asof` 与 `locked_at` 的区别

    - `asof`：**这套口径针对哪个评估时点**（字符串，保留原始粒度）。
    - `locked_at`：**这条戳是什么时候被写下来的**（带时区 datetime）。

    两者不是一回事，也不该合并：一套 10 月 20 日针对 `2026-10-20` 锁定的口径，
    可以在 10 月 26 日被重新登记一次（`locked_at` 变、`asof` 不变）。合并之后，
    "什么时候锁的"这件事会被"锁的是哪天的数据"淹掉。

    `asof` 的粒度要符合该 family 的节拍：`daily_fundamental` 用日，`minute_event`
    可以到分钟。本模块**不**替调用方猜 —— 猜节拍会让一条日频口径被错当成分钟级。
    """

    signal_id: str
    definition_version: str
    cadence: str
    """评估节拍。封闭集 `FREQ_LOCK_CADENCES`: `minute_event` / `daily_fundamental`。"""

    asof: str
    """这套口径针对的评估时点。ISO 日期或带时区时间串。"""

    params: Mapping[str, Any] = field(default_factory=dict)
    """阈值参数集(量比 / 突破幅度 / 窗口分钟……)。参与指纹。

    允许空: "节拍已定、阈值未标定"是合法中间态。空 `params` 不代表"阈值已锁定",
    只代表"这一版没有参数"。

    ## 量相关键必须声明单位(P-bss-k4 / K4)

    只要出现 `volume_ratio` / `volume_ratio_min` / `volume_ratio_max`, 或任何以
    `volume_` 开头且非 `volume_unit` 的键, 就必须带 `volume_unit ∈ {手, 股}`,
    否则构造**直接抛** `FreqLockError`(见 `require_volume_unit`)。
    不做静默默认: 主仓「手」与 DSA「股」差 100 倍(#237 跨层对照), 猜错看不出来。

    `volume_unit` 落在 `params` 内 → **自动进指纹**: 同一套阈值换个单位, 是另一套
    口径, 指纹必须不同。
    """

    freq_lock_version: str = FREQ_LOCK_VERSION
    """人读标签。参与**三键对齐**, 不参与指纹本身。

    它不参与指纹是有意的: 标签是手写的，把它算进指纹等于让手写字符串污染内容哈希。
    但它在对齐时仍然必须一致 —— 标签与指纹是两个独立的维度, 一起用。
    """

    locked_at: datetime | None = None
    """写戳时刻。`None` 表示"这是一条从外部读进来的戳, 落盘时刻已丢失"。"""

    schema_version: str = FREQ_LOCK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("signal_id", "definition_version", "asof", "freq_lock_version"):
            value = getattr(self, name)
            _require(isinstance(value, str) and value.strip(), f"{name} 不能为空")

        _require(
            self.cadence in FREQ_LOCK_CADENCES,
            f"cadence 必须是 {sorted(FREQ_LOCK_CADENCES)} 之一, 收到 {self.cadence!r}",
        )
        _require(isinstance(self.params, Mapping), "params 必须是映射")
        # P-bss-k4 / K4: 量相关键存在时必须声明单位。放在这里而不是只放助手,
        # 是为了让"直接构造 / from_dict / 夹具"三条入口过同一道闸 ——
        # 助手存在但调用方漏调, 是这类闸最典型的静默洞。
        require_volume_unit(self.params)
        _require(self.schema_version == FREQ_LOCK_SCHEMA_VERSION, "schema_version 不匹配")

        if self.locked_at is not None:
            _require(isinstance(self.locked_at, datetime), "locked_at 必须是 datetime")
            # 裸时间不接受: 猜时区会让同一个戳在两地解析出不同的绝对时刻。
            _require(self.locked_at.tzinfo is not None, "locked_at 必须带时区")

    # ================================================================ 派生

    @property
    def fingerprint(self) -> str:
        """本戳的内容指纹。**每次现算**, 不缓存 —— 缓存会让"改了 params 但戳没重建"
        这种情况算出旧值, 而它恰恰是要被抓的那一种。"""
        return freq_lock_fingerprint(
            signal_id=self.signal_id,
            definition_version=self.definition_version,
            cadence=self.cadence,
            params=self.params,
        )

    @property
    def align_key(self) -> tuple[str, str, str]:
        """三键对齐键: `(signal_id, definition_version, freq_lock_version)`。

        命中、戳、校准登记三者对齐时用的就是这一个元组。缺任一项非空即可见性闸拒绝
        —— 不是"随便挑一个对得上就算"。
        """
        return (self.signal_id, self.definition_version, self.freq_lock_version)

    @property
    def asof_in_shanghai(self) -> bool:
        """`asof` 若是带时区的时间串, 是否能解析到上海时区。

        纯日期恒 `True`(无时区可争)。裸时间串(无时区)`False` —— 与
        `hits_models.asof_date_or_none` 同一条纪律: 猜时区会让同一个文件在不同机器上
        解析出不同的绝对时刻, 且 diff 里看不出来。
        """
        text = self.asof.strip()
        if len(text) == 10:
            try:
                datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return False
            return True
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            return False
        return parsed.astimezone(CN_TZ).tzinfo is not None

    def matches(self, other: FreqLockStamp) -> bool:
        """两条戳是否**同一套口径**。

        三键一致 **且** 指纹一致才算。只对三键是不够的(M6: 改参数但伪称同一版本),
        只对指纹也不够(不同 `signal_id` 恰好参数相同)。
        """
        if not isinstance(other, FreqLockStamp):
            return False
        return self.align_key == other.align_key and self.fingerprint == other.fingerprint

    # ================================================================ 序列化

    def to_dict(self) -> dict[str, Any]:
        """序列化。**显式列举**, 不反射字段。

        `fingerprint` 会被写进产物, 但**读的时候重算**, 不信文件里的值 ——
        一份被手改过 `fingerprint` 的文件, 不该因为改对了字段就通过校验。
        """
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "definition_version": self.definition_version,
            "cadence": self.cadence,
            "asof": self.asof,
            "params": dict(self.params),
            "freq_lock_version": self.freq_lock_version,
            "fingerprint": self.fingerprint,
            "locked_at": None if self.locked_at is None else self.locked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FreqLockStamp:
        """从 `to_dict` 的产物重建。

        **文件里的 `fingerprint` 不采信**: 重算一遍, 与文件里的值不一致就报错。
        这是"指纹有没有被改"这件事唯一的抓手。
        """
        if not isinstance(payload, Mapping):
            raise FreqLockError(f"from_dict 需要映射, 收到 {type(payload).__name__}")

        params_raw = payload.get("params") or {}
        if not isinstance(params_raw, Mapping):
            raise FreqLockError("params 必须是映射")

        locked_raw = payload.get("locked_at")
        locked_at: datetime | None = None
        if locked_raw is not None:
            if not isinstance(locked_raw, str):
                raise FreqLockError("locked_at 必须是 ISO8601 字符串或 null")
            try:
                locked_at = datetime.fromisoformat(locked_raw)
            except ValueError as exc:
                raise FreqLockError(f"locked_at 不是合法 ISO8601: {locked_raw!r}") from exc

        kwargs: dict[str, Any] = {
            "signal_id": payload.get("signal_id"),
            "definition_version": payload.get("definition_version"),
            "cadence": payload.get("cadence"),
            "asof": payload.get("asof"),
            "params": dict(params_raw),
            "freq_lock_version": payload.get("freq_lock_version") or FREQ_LOCK_VERSION,
            "locked_at": locked_at,
        }
        if "schema_version" in payload:
            kwargs["schema_version"] = payload["schema_version"]

        stamp = cls(**kwargs)

        declared = payload.get("fingerprint")
        if declared is not None:
            if not isinstance(declared, str):
                raise FreqLockError("fingerprint 必须是字符串或 null")
            if declared != stamp.fingerprint:
                raise FreqLockError(
                    "戳的内容指纹与文件里声明的不一致: 参与锁定的字段被改过"
                    f"(声明 {declared[:12]}…, 实算 {stamp.fingerprint[:12]}…)"
                )
        return stamp


__all__ = [
    "FREQ_LOCK_FINGERPRINT_FIELDS",
    "FreqLockError",
    "FreqLockStamp",
    "canonical_json",
    "freq_lock_fingerprint",
    "require_volume_unit",
]
