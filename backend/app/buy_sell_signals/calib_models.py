"""买卖信号的**回测校准登记**(批次 20261026gbsscalib · P-calib)。

仅供研究，不构成投资建议；执行须人工确认。

## 「已校准」是一个可以被机器判定的结论，不再是一句话

在本次改动之前，'已校准'在代码里只以两种形式存在：一句空态文案
（`EMPTY_HITS_MESSAGE`），和一堆 `data_pending` 证据缺口标签。系统自己没有
任何地方能回答「这条信号现在算不算已校准」。于是所有含糊的表述都找到了容身处 ——
"阈值已经定下来了"、"回测跑过了"、"应该没问题"，这些话不需要任何记录就能说。

本模块把它变成一个**必须留痕**的东西：想宣称 `passed`，就得写一条登记，而登记里
的每个字段都必须能被追问到底。

## 为什么 `/ passed` 的门槛是硬拒而不是降级

回顾 `constants.CALIB_SAMPLE_N_MIN` 的说明：`sample_n < 30` 时静默转成 `pending`，
会让人以为"我写进去了，只是没过"。实际上他**没写进去**。下次来查登记看到一条
`pending`，只会以为自己的回测没过，而不是自己的调用被拒了。

所以三条硬门槛（样本量、成本、必填引用）全部**抛异常**。写入失败就是写入失败。

## 字段为什么这么多

因为每少一个字段，就多一处可以含糊的地方：

| 少了它 | 会发生什么 |
|---|---|
| `eval_window` + 单位 | "跑了一段时间" 可以是 3 天也可以是 3 年 |
| `sample_n` | 一条 2 个样本的回测和一个 300 个样本的回测长得一样 |
| `cost_bp_roundtrip` | 不算成本的策略回测在 A 股上几乎必然偏正 |
| `baseline_ref` | 没有对照，"跑赢了"和"跑赢了什么"是两回事 |
| `interval` | 点估计可以漂亮，区间可以跨零 |
| `report_id` / `source` | 结论无法回溯到任何一份产物 |
| `passed_at` | 不知道这条结论是什么时候成立的 |

这不是为了字段多而多。每个字段都对应一个真实出现过的、**在事后无法补救**的
含糊表述。

## 本模块不做什么

- 不读行情、不跑回测、不算任何指标。这些数字全部由外部产物提供，这里只做**校验与登记**。
- **不生产** `passed`。仓库里没有任何代码路径会自动写一条 `passed` 出来 ——
  连夹具都只能写进 `tmp_path`（见 `hits_fixtures`）。生产环境下 `calib.json`
  不存在或没有 `passed` 条目，是**正确且诚实**的状态。
- 不碰 `BUY_SELL_SIGNALS_PERSIST`。那个 env 管定义注册表落不落盘；校准看不看得见
  只取决于文件在不在（理由见 `constants.CALIB_FILENAME`）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from app.market_time import CN_TZ

from . import disclaimer as _disclaimer
from .constants import (
    CALIB_COST_BP_ROUNDTRIP,
    CALIB_EVAL_WINDOW_UNIT,
    CALIB_SAMPLE_N_MIN,
    CALIB_SCHEMA_VERSION,
    CALIB_STATUS_PASSED,
    CALIB_STATUSES,
    DISCLAIMER_KEY,
    NAMESPACE,
)
from .freq_lock import FreqLockStamp
from .models import SignalValidationError


class CalibValidationError(SignalValidationError):
    """校准登记不合法, 或者不满足写 `passed` 的门槛。

    两种情况用同一个类: 对调用方来说都是"这次写入没成功, 原因见消息"。消息里会把
    具体是哪一条门槛拦下来的说清楚 —— 一条笼统的"校验失败"等于没说话。
    """


def _require(condition: object, message: str) -> None:
    if not condition:
        raise CalibValidationError(message)


#: 允许出现在 `interval` 里的键。封闭。
_INTERVAL_KEYS: Final = frozenset({"lo", "hi"})


@dataclass(frozen=True, slots=True)
class CalibRecord:
    """一条回测校准登记。

    `frozen=True`: 登记一旦写入就不是可改的。要修正就写一条新的（同一个键、
    更新的 `passed_at`），读侧取最新的那条。原地改会让"以前看到过什么结论"这件事
    无法追溯 —— 而校准结论恰恰是最需要追溯的东西。

    ## 三键必须与戳、命中一致

    `(signal_id, definition_version, freq_lock_version)` 这个三元组是**对齐键**。
    可见性闸用它把「这条命中」「这条定义」「这条登记」串起来：三者任一不一致，
    这条登记就不给这条命中背书。

    只比 `signal_id` 是不够的：同名信号改版后旧登记的 `passed` 会顺延到新版。
    只比 `signal_id` + `definition_version` 也不够：定义没动、但阈值参数改了
    （指纹变、`freq_lock_version` 本应变），旧登记同样会顺延过去。M6 钉的就是这个。
    """

    # ---- 三键对齐 ------------------------------------------------------
    signal_id: str
    definition_version: str
    freq_lock_version: str

    # ---- 结论 ----------------------------------------------------------
    status: str
    """封闭集 `CALIB_STATUSES` = `{passed, pending, failed}`。

    **只有 `passed` 算已校准。** 其余两个在可见性闸上与"没有登记"完全等价。
    """

    # ---- 回测口径（`passed` 时全部必填） ---------------------------------
    eval_window: int
    """评估窗口长度, **正整数**。单位恒为 `CALIB_EVAL_WINDOW_UNIT`（交易日）。

    窗口与单位分开写是刻意的：只写 "20" 会被读成 20 个自然日, 而 20 个自然日
    在 A 股上盖不满 3 个完整交易周。单位必须显式落进文件。
    """

    sample_n: int
    """独立样本数。`passed` 要求 `>= CALIB_SAMPLE_N_MIN`（30）。"""

    cost_bp_roundtrip: float
    """来回成本, 单位 bp。`passed` 时必须**恰好** `CALIB_COST_BP_ROUNDTRIP`（18.2）。"""

    baseline_ref: str
    """同期对照基线引用（非空）。"跑赢了"必须能回答"跑赢了什么"。"""

    interval: Mapping[str, float] = field(default_factory=dict)
    """结论的**显式区间** `{lo, hi}`。

    点估计相对基线的结论必须带区间: 区间跨零的"不劣于基线"在统计上不成立。
    这里只做形状校验, 不做统计判定 —— 判定属于回测产物, 不属于登记。
    """

    # ---- 产物指针 ------------------------------------------------------
    report_id: str = ""
    source: str = ""

    # ---- 时间 ----------------------------------------------------------
    passed_at: datetime | None = None
    """结论成立时刻。**仅 `passed` 时必填**, 必须带时区（取 Asia/Shanghai 呈现）。"""

    # ---- 元数据 --------------------------------------------------------
    eval_window_unit: str = CALIB_EVAL_WINDOW_UNIT
    schema_version: str = CALIB_SCHEMA_VERSION
    namespace: str = NAMESPACE

    def __post_init__(self) -> None:
        # ---- 三键: 任何状态都非空 ----
        for name in ("signal_id", "definition_version", "freq_lock_version"):
            value = getattr(self, name)
            _require(
                isinstance(value, str) and value.strip(),
                f"{name} 不能为空 —— 三键缺一, 这条登记就不给任何命中背书",
            )

        # ---- 状态封闭集 ----
        _require(
            self.status in CALIB_STATUSES,
            f"status 必须是 {sorted(CALIB_STATUSES)} 之一, 收到 {self.status!r}"
            "（不接受 ok / true / verified 这类近义词）",
        )

        # ---- 窗口: 正整数 + 单位钉死 ----
        _require(
            isinstance(self.eval_window, int) and not isinstance(self.eval_window, bool),
            "eval_window 必须是整数",
        )
        _require(self.eval_window > 0, f"eval_window 必须为正, 收到 {self.eval_window}")
        _require(
            self.eval_window_unit == CALIB_EVAL_WINDOW_UNIT,
            f"eval_window 的单位恒为 {CALIB_EVAL_WINDOW_UNIT!r}, 收到 {self.eval_window_unit!r}",
        )

        # ---- 样本量: 非负整数（门槛只在 passed 时卡） ----
        _require(
            isinstance(self.sample_n, int) and not isinstance(self.sample_n, bool),
            "sample_n 必须是整数",
        )
        _require(self.sample_n >= 0, f"sample_n 不能为负, 收到 {self.sample_n}")

        # ---- 成本: 数值形状（门槛只在 passed 时卡） ----
        _require(
            isinstance(self.cost_bp_roundtrip, (int, float))
            and not isinstance(self.cost_bp_roundtrip, bool),
            "cost_bp_roundtrip 必须是数值",
        )

        # ---- 区间形状 ----
        _require(isinstance(self.interval, Mapping), "interval 必须是 {lo, hi} 映射")
        if self.interval:
            _require(
                set(self.interval) == _INTERVAL_KEYS,
                f"interval 的键必须恰好是 {sorted(_INTERVAL_KEYS)}, 收到 {sorted(self.interval)}",
            )
            lo, hi = self.interval["lo"], self.interval["hi"]
            for label, value in (("lo", lo), ("hi", hi)):
                _require(
                    isinstance(value, (int, float)) and not isinstance(value, bool),
                    f"interval.{label} 必须是数值",
                )
            _require(float(lo) <= float(hi), f"interval 下界 {lo} 大于上界 {hi}")

        _require(self.namespace == NAMESPACE, f"namespace 必须是 {NAMESPACE!r}")
        _require(self.schema_version == CALIB_SCHEMA_VERSION, "schema_version 不匹配")

        # ---- `passed` 专属门槛（fail-closed, 硬拒） ----
        if self.status == CALIB_STATUS_PASSED:
            self._require_passed_gates()

    def _require_passed_gates(self) -> None:
        """写 `passed` 的门槛。**任何一条不满足都抛错, 不降级。**

        顺序按"最容易被含糊带过"的排前面: 样本量 → 成本 → 引用 → 区间 → 时点。
        全部检查完才返回, 但一旦命中就抛 —— 不攒着一起报, 因为调用方通常只需要知道
        第一个卡点。
        """
        _require(
            self.sample_n >= CALIB_SAMPLE_N_MIN,
            f"写 passed 要求 sample_n >= {CALIB_SAMPLE_N_MIN}, 收到 {self.sample_n}",
        )
        _require(
            float(self.cost_bp_roundtrip) == float(CALIB_COST_BP_ROUNDTRIP),
            f"写 passed 要求 cost_bp_roundtrip == {CALIB_COST_BP_ROUNDTRIP}, "
            f"收到 {self.cost_bp_roundtrip}（成本是准入口径的一部分, 不可自报）",
        )
        _require(
            isinstance(self.baseline_ref, str) and self.baseline_ref.strip(),
            "写 passed 要求 baseline_ref 非空（没有对照的'跑赢'不成立）",
        )
        _require(
            bool(self.interval),
            "写 passed 要求显式 interval {lo, hi}（点估计单独不足以支撑结论）",
        )
        _require(
            isinstance(self.report_id, str) and self.report_id.strip(),
            "写 passed 要求 report_id（结论必须能回溯到回测产物）",
        )
        _require(
            isinstance(self.source, str) and self.source.strip(),
            "写 passed 要求 source（同上: 产物从哪来）",
        )
        _require(
            self.eval_window > 0,
            "写 passed 要求 eval_window 为正整数",
        )
        _require(
            self.passed_at is not None,
            "写 passed 要求 passed_at（这条结论何时成立）",
        )
        _require(
            isinstance(self.passed_at, datetime) and self.passed_at.tzinfo is not None,
            "passed_at 必须带时区（用 Asia/Shanghai）",
        )

    # ================================================================ 派生

    @property
    def align_key(self) -> tuple[str, str, str]:
        """三键对齐键, 与 `FreqLockStamp.align_key` 同一口径。"""
        return (self.signal_id, self.definition_version, self.freq_lock_version)

    @property
    def is_passed(self) -> bool:
        """是否算「已校准」。**`passed` 之外一律 `False`**, 没有中间地带。"""
        return self.status == CALIB_STATUS_PASSED

    @property
    def passed_at_shanghai(self) -> str | None:
        """`passed_at` 归一到 Asia/Shanghai 的 ISO 串; 无则 `None`。"""
        if self.passed_at is None:
            return None
        if self.passed_at.tzinfo is None:
            return None
        return self.passed_at.astimezone(CN_TZ).isoformat()

    # ================================================================ 序列化

    def to_dict(self) -> dict[str, Any]:
        """序列化。**显式列举**, 不反射字段。"""
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "signal_id": self.signal_id,
            "definition_version": self.definition_version,
            "freq_lock_version": self.freq_lock_version,
            "status": self.status,
            "eval_window": self.eval_window,
            "eval_window_unit": self.eval_window_unit,
            "sample_n": self.sample_n,
            "cost_bp_roundtrip": self.cost_bp_roundtrip,
            "baseline_ref": self.baseline_ref,
            "interval": dict(self.interval),
            "report_id": self.report_id,
            "source": self.source,
            "passed_at": self.passed_at_shanghai,
            "disclaimer_key": DISCLAIMER_KEY,
            "disclaimer": _disclaimer.DISCLAIMER_TEXT,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CalibRecord:
        """从 `to_dict` 的产物重建。**反序列化不是绕过校验的后门** —— 门槛照样走一遍。"""
        return calib_record_from_dict(payload)


def _coerce_interval(raw: Any) -> dict[str, float]:
    """接受 `{lo, hi}` 或 `[lo, hi]` 两种写法, 归一成映射。

    数组写法只是为了让人手写 JSON 时不必记键名; 归一之后 `interval` 只有一个内部表示,
    免得下游要处理两种形状。
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            raise CalibValidationError(f"interval 写成数组时必须恰好两个元素, 收到 {len(raw)} 个")
        return {"lo": raw[0], "hi": raw[1]}
    raise CalibValidationError(f"interval 必须是 {{lo, hi}} 或 [lo, hi], 收到 {type(raw).__name__}")


def calib_record_from_dict(payload: Mapping[str, Any]) -> CalibRecord:
    """从 JSON 载荷重建 `CalibRecord`。

    未知键丢弃（将来回滚不至于整表读不出来）；`disclaimer` 这类文案快照丢弃 ——
    允许回灌等于允许有人改一行 JSON 伪造合规措辞。

    时间字段: `passed_at` 接受带时区的 ISO 串。**裸时间串不接受** —— 与
    `hits_models.asof_date_or_none` 同一条纪律。
    """
    if not isinstance(payload, Mapping):
        raise CalibValidationError(f"from_dict 需要映射, 收到 {type(payload).__name__}")

    passed_raw = payload.get("passed_at")
    passed_at: datetime | None = None
    if passed_raw is not None:
        if not isinstance(passed_raw, str):
            raise CalibValidationError("passed_at 必须是 ISO8601 字符串或 null")
        try:
            passed_at = datetime.fromisoformat(passed_raw)
        except ValueError as exc:
            raise CalibValidationError(f"passed_at 不是合法 ISO8601: {passed_raw!r}") from exc

    kwargs: dict[str, Any] = {
        "signal_id": payload.get("signal_id"),
        "definition_version": payload.get("definition_version"),
        "freq_lock_version": payload.get("freq_lock_version"),
        "status": payload.get("status"),
        "eval_window": payload.get("eval_window"),
        "sample_n": payload.get("sample_n"),
        "cost_bp_roundtrip": payload.get("cost_bp_roundtrip"),
        "baseline_ref": payload.get("baseline_ref", ""),
        "interval": _coerce_interval(payload.get("interval")),
        "report_id": payload.get("report_id", ""),
        "source": payload.get("source", ""),
        "passed_at": passed_at,
    }
    for key in ("eval_window_unit", "schema_version", "namespace"):
        if key in payload:
            kwargs[key] = payload[key]
    return CalibRecord(**kwargs)


def stamp_for_record(record: CalibRecord, *, cadence: str, asof: str, params: Mapping[str, Any]) -> FreqLockStamp:
    """按登记的键造一条对应的频率锁定戳（测例与运维核对用）。

    **不是**生产写路径: 这里不往任何地方落盘, 只是把"这条登记说的是哪套口径"
    拼成一个可比较的对象。生产路径上不存在"由登记反推戳"的代码。
    """
    return FreqLockStamp(
        signal_id=record.signal_id,
        definition_version=record.definition_version,
        cadence=cadence,
        asof=asof,
        params=params,
        freq_lock_version=record.freq_lock_version,
    )


def evaluation_date(record: CalibRecord) -> date | None:
    """`passed_at` 归一到上海日期（只作展示/日志用, 不参与判定）。"""
    if record.passed_at is None or record.passed_at.tzinfo is None:
        return None
    return record.passed_at.astimezone(CN_TZ).date()


__all__ = [
    "CalibRecord",
    "CalibValidationError",
    "calib_record_from_dict",
    "evaluation_date",
    "stamp_for_record",
]
