"""竞价异动的**身份 + 契约 + 只读存档适配**（批次 20261026gauction2 · 竞价窄开）。

仅供研究，不构成投资建议；执行须人工确认。

## 这个文件是什么，不是什么

**是**：把「竞价异动这条信号在注册表里长什么样」钉成可被测例扫描的常量，并把
`auction_scan` 的**既有落盘存档**只读地映射成候选结构 —— 身份、字段映射、
四条证据缺口，三件事各自钉死。

**不是**触发引擎。这里没有量比阈值判定、没有 gap 门槛计算、没有回测、没有扫描调度。
`evaluate_auction_abnormal()` **永远不会**返回"今天竞价异动了"这种结论：
`triggered` 恒 `None`（"无法判断"），`TRIGGERING_ENABLED` 恒 `False`。

## 与 #210 放量 stub 的对称关系

本模块是 `volume_breakout_stub` 的**镜像腿**，两条腿共用同一套"身份钉 + 空桩 +
证据缺口"骨架，但语义严格互斥（见 `constants.NOISE_MIX_FORBIDDEN`）：

| 项 | 放量（#210） | 竞价（本模块） |
|---|---|---|
| `event_class` | `intraday_volume` | `auction` |
| `auction_stat_bucket` | 恒 `None` | 恒 `auction_0925` |
| `post_open_noise_bucket` | 可空桩（`None`） | **不得携带** |
| 信息集 | 开盘后连续成交 | 09:25 集合竞价离散撮合 |

**两个方向都不许混桶**：竞价写进 `post_open_*` 是错，放量写进 `auction_0925` 也是错。
`SignalRecord.__post_init__` 在 tip 上已经双向拒绝，本模块只是把竞价这条路径
**显式覆盖**并测下来 —— 不是重新实现分列校验。

## 只读存档：为什么这里不重抓快照

信号侧只读 `auscan` 的**既有落盘**（`data/auction_scan/{date}.json`，或等价地
`get_scan(data_dir, target)`）。理由不是"懒得写"，而是：

1. **重抓会把信号评估变成一次新的采样**。同一天不同时刻重抓，拿到的是不同快照，
   于是"这条信号那天有没有出现"取决于你什么时候跑它 —— 这样的评估没有意义。
2. **窗口只在 09:25 附近有效**。开盘后补抓的快照已经不是集合竞价撮合那一刻的
   信息集（`SCAN_WINDOW` 的设计前提），拿它算竞价 = 用开盘后的数据冒充 09:25。
3. **存档是可复核的**。落盘文件带 `scanned_at`，能回答"这个结论基于哪一刻的快照"。

所以本模块**只读**，且**默认不改** `auction_scan` 的落盘 schema。

## 永久硬禁：09:30 冒充 09:25

`auction_ts` 由 `trade_date` + **固定 09:25:00 Asia/Shanghai** 推出，**不读任何分钟
bar**。用 09:30 或任意开盘后 bar 冒充 09:25 是永久禁止的：两者是离散撮合与连续
成交两个信息集，混用的结果**不会报错**，只会让竞价类信号的统计全部失真。

分钟库里至今没有 09:25 bar（最早 09:30），这件事由标签
`minute_0925_still_absent` 诚实记着 —— 它**不妨碍**读 scan 存档，但**永不**能用
09:30 把它清掉。

## 四条证据缺口

竞价解阻是**部分**的：prod 有 `data/auction_scan/` 多日 JSON（B1「无 auction 包」
部分解阻），但 B2 分钟 09:25 仍缺。四条缺口一条都没清：

1. `auction_price_not_persisted` —— 落盘 item 只有 `gap` / `vol_ratio` /
   `auction_volume` / `auction_amount`，**没有** `auction_price` / `prev_close`。
   所以本模块 `auction_price` 恒 `None`，并带这个标签；不反推、不用 gap 倒算。
2. `threshold_lock_data_pending` —— UI 列表用的 `min_gap` / `min_vol_ratio` 是
   **页面筛选参数**，不是 PHASE3 回测门禁。本拍**不钉任何数值**，也不把它们
   当"已锁定阈值"。
3. `backtest_evidence_data_pending` —— 没有竞价异动的回测表 / 触发表 / 显著性证据。
4. `minute_0925_still_absent` —— 分钟库仍无 09:25 bar。

**部分解阻 ≠ 整拍可推。** 有存档能映射出候选，只说明字段能对上，不说明这条信号
经过验证。任何"回测已通过 / 可交易 / 可推送"的说法在本拍都不成立。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from app.buy_sell_signals import constants as C
from app.buy_sell_signals.models import SignalRecord

# ------------------------------------------------------------------ 身份钉

AUCTION_ABNORMAL_SIGNAL_ID: Final = "auction_abnormal"
"""注册表里的 `signal_id`。全仓唯一的竞价异动信号。"""

AUCTION_ABNORMAL_EVENT_CLASS: Final = "auction"
"""事件类别 —— 竞价。须 ∈ `constants.EVENT_CLASSES`。

这是**唯一**允许携带 `auction_stat_bucket` 的事件类别（`SignalRecord` 的通用校验
在 tip 上已强制）。改成任何其它值，竞价桶就失去了宿主。
"""

AUCTION_ABNORMAL_FAMILY: Final = "minute_event"
"""信号家族 —— 分钟级事件。须 ∈ `constants.SIGNAL_FAMILIES`。

与放量同族：两者都必须有分钟级 / 逐笔级证据才能谈标定，不能用日线代理糊过去。
"""

AUCTION_ABNORMAL_AUCTION_STAT_BUCKET: Final = C.AUCTION_STAT_BUCKET_0925
"""竞价统计桶 —— **恒 `"auction_0925"`**，恒等，不是"默认值"。

竞价信号**必须**带这个桶，且只有这一个桶。反过来，放量信号把它钉成 `None` ——
本模块引用同一个常量 `C.AUCTION_STAT_BUCKET_0925`，**不改它的取值**，
也不扩 `POST_OPEN_NOISE_BUCKETS`。
"""

AUCTION_ABNORMAL_AUCTION_RULE_ID: Final = C.AUCTION_RULE_ID_NONE
"""竞价规则 ID —— 沿用 `AUCTION_RULE_ID_NONE`（即 `None`）。

**本拍不锁规则**：阈值未标定、无回测证据，任何 `auction_rule_id` 都会暗示
"这条规则已经确定下来了"。`None` 是诚实的：规则还没定。
"""

AUCTION_ABNORMAL_POST_OPEN_NOISE_FLAG: Final = False
"""开盘后噪声标记 —— 竞价信号**永不**标记为开盘后噪声。

竞价发生在 09:25，早于连续竞价开始，本来就不属于任何开盘后时段。把它标成
`True` 等于承认"这是开盘后数据"，那是冒充。
"""

AUCTION_ABNORMAL_POST_OPEN_NOISE_BUCKET: Final = None
"""开盘后噪声桶 —— **恒 `None`**，且 `SignalRecord` 会**拒绝**非 None 的取值。

这是混桶的唯一入口：`event_class="auction"` 的记录一旦带上开盘后桶，说明有人
想把竞价与开盘后两段样本合成一份统计。两段的信息集不同（离散撮合 vs 连续成交），
合起来算的均值 / 命中率没有解释意义（`C.NOISE_MIX_FORBIDDEN`）。
"""

AUCTION_ABNORMAL_STATUS: Final = "data_pending"
"""**评估**状态。恒 `data_pending`。

与 `SignalRecord.status`（**注册表生命周期**状态，取值域 `SIGNAL_STATUSES`）
**不是同一个域**，不可互相代入：

| 域 | 取值 | 语义 |
|---|---|---|
| 本常量（评估） | `data_pending` | 证据没到，无法判断 |
| `SignalRecord.status`（生命周期） | `candidate` / `observe` / `publishable` /
  `retired` / `data_abnormal` | 这条信号在注册表里走到哪一步了 |

`data_pending` **不在** `SIGNAL_STATUSES` 里，也**不应该**加进去 —— 往生命周期
取值域里塞一个评估态，会让状态机多出一条谁都不知道该怎么迁移的状态。
"""

AUCTION_ABNORMAL_RECORD_STATUS: Final = "candidate"
"""构造 `SignalRecord` 时用的**生命周期**状态。

`candidate` = 刚注册、还没验证。这是诚实的：连一条可信证据都没有，不该是
`observe`（那意味着"已进入观察"），更不该是 `publishable`（那意味着"已发布"，
而 `publishable` 只走人工闸）。
"""

AUCTION_ABNORMAL_TRIGGERING_ENABLED: Final = False
"""是否允许产出"已触发"结论。**恒 `False`**。

改成 `True` 只会让一个空桩撒谎。真开竞价需要另拍：补价字段 / 阈值标定 /
回测证据 / 分钟 09:25，四件事一件都不能少。
"""

# ------------------------------------------------------- 四条证据缺口

AUCTION_ABNORMAL_EVIDENCE_PENDING: Final = frozenset(
    {
        "auction_price_not_persisted",     # 落盘无 auction_price / prev_close
        "threshold_lock_data_pending",     # UI 阈值 ≠ 已锁回测门禁
        "backtest_evidence_data_pending",  # 无竞价异动回测 / 触发表
        "minute_0925_still_absent",        # 分钟库仍无 09:25；禁用 09:30 清掉
    }
)
"""四条证据缺口的**机器可读标签**。全部 pending，一条都没清。

标签都以 `pending` 结尾不是巧合：它们是**缺口**，不是完成项。将来清掉某一条时，
那个标签应当被删掉而不是改成 `_done` —— 已清的条目留在 pending 集合里，
会让"还有几条没清"这个判断失真。

注意 `minute_0925_still_absent` 是唯一一个**不以 pending 结尾**的，因为它的语义是
"某样东西**仍然不存在**"，而不是"某件事还没做"。它**不阻塞**读 scan 存档。
"""

AUCTION_ABNORMAL_EVIDENCE: Final[Mapping[str, tuple[str, str]]] = {
    "auction_price_not_persisted": (
        "竞价价未落盘",
        "落盘 item 只有 gap / vol_ratio / auction_volume / auction_amount;"
        " 无 auction_price / prev_close, 故 auction_price 恒 None, 不用 gap 倒推",
    ),
    "threshold_lock_data_pending": (
        "阈值锁定",
        "UI 列表的 min_gap / min_vol_ratio 是页面筛选参数, 不是 PHASE3 回测门禁;"
        " 本拍不钉任何数值, 也不写 '已锁定'",
    ),
    "backtest_evidence_data_pending": (
        "回测 / 触发表",
        "没有竞价异动的回测表 / 触发表 / 显著性证据;"
        " 缺它, 任何 gap 与量比阈值都没有标定依据",
    ),
    "minute_0925_still_absent": (
        "分钟 09:25 bar",
        "分钟库最早仍为 09:30, 无 09:25 bar; 该缺口不阻塞读 scan 存档,"
        " 但永不能用 09:30 冒充 09:25 来清掉它",
    ),
}
"""同一批缺口的**人读表**，键与 `AUCTION_ABNORMAL_EVIDENCE_PENDING` 一一对应。

两处分开写是刻意的：机器读标签，人读说明。键集合必须相等，有测例对比 ——
只改一处会让另一处悄悄过期。
"""

# ---------------------------------------------------------------- 只读适配

AUCTION_TS_TIME: Final = time(9, 25, 0)
"""竞价定盘时刻 —— **固定 09:25:00**，不是可配置参数。

写成常量而不是函数参数，是为了让"改用别的时刻"必须改源码、必须过测例，
而不是某个调用方顺手传个 `time(9, 30)`。用 09:30 或任意开盘后 bar 冒充 09:25
是永久禁止的（见模块 docstring）。
"""

_CN_TZ: Final = timezone(timedelta(hours=8), "Asia/Shanghai")
"""竞价时刻的时区。与全包其它时刻一致，用固定 +08:00。

不用 `zoneinfo` 是因为本模块只需要一个**固定偏移**：中国不实行夏令时，
`+08:00` 与实际本地时间恒等，且不依赖系统 tzdata（容器里常常没有）。
"""


def auction_ts_for(trade_date: date_cls) -> datetime:
    """由交易日推出竞价定盘时刻 `trade_date 09:25:00+08:00`。

    **只用 `trade_date`，不读分钟 bar。** 这是"不冒充"的机械保证：函数签名里
    根本没有分钟数据可以传进来。
    """
    return datetime.combine(trade_date, AUCTION_TS_TIME, tzinfo=_CN_TZ)


def load_scan_payload(
    data_dir: Path | str | None,
    target: date_cls | None = None,
) -> dict[str, Any]:
    """只读拿一份 `auction_scan` 存档。缺文件时返回 `state="no_data"`。

    走 `app.services.auction_scan.get_scan` —— **复用它已有的缺文件语义**，
    而不是自己 `open()` 一个 JSON。理由：`get_scan` 已经把 `state` /
    `no_data` / `dates` 这套契约定好了（tip 已实现），信号侧另写一份读取逻辑，
    迟早会和它分叉。

    这个函数**不**触发扫描、**不**进 `SCAN_WINDOW`、**不**重抓快照。
    `get_scan` 本身是纯读取函数（读落盘文件），这一点在 tip 上可以核对。

    `data_dir` 为 `None` 时按 `no_data` 处理 —— 不猜默认目录。猜错了会读到别的
    环境的存档，那种错比读不到更难发现。
    """
    if data_dir is None:
        return {
            "state": "no_data",
            "trade_date": None,
            "scanned_at": None,
            "min_gap": None,
            "min_vol_ratio": None,
            "items": [],
            "message": "未提供 auction_scan 数据目录",
        }

    from app.services.auction_scan import get_scan

    return get_scan(Path(data_dir), target)


def _to_float(value: Any) -> float | None:
    """能转成有限浮点就转，否则 `None`。**不填 0**。

    0 是一个具体的数值结论（"量比是 0"），而缺字段的正确表达是 `None`
    （"不知道"）。用 0 顶上会让下游的均值 / 排序把"缺失"当成"极小值"。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


@dataclass(frozen=True, slots=True)
class AuctionCandidate:
    """一条从 `auction_scan` 存档映射出来的**候选**。

    每个字段都在说"知道多少"，而不是"没事"。特别是 `triggered` 恒 `None` ——
    与 `VolumeBreakoutStub` / `health_veto_hit` 同一个理由：`False` 是一个结论
    （"查过，没异动"），在没有阈值、没有回测、没有 09:25 分钟证据的情况下返回
    `False`，等于向调用方保证一件我们并不知道的事。
    """

    symbol: str
    trade_date: date_cls
    auction_ts: datetime
    """`trade_date 09:25:00+08:00`，由 `auction_ts_for()` 推出，**不读分钟 bar**。"""

    display_name: str | None = None
    ticker: str | None = None

    auction_volume: float | None = None
    """竞价成交量（手）。存档里有就填，没有就 `None` —— 不填 0。"""

    auction_price: float | None = None
    """竞价价格。**恒 `None`** —— 存档没有这个字段。

    随 `auction_price_not_persisted` 标签一起返回。**不**用 `gap` 反推价格：
    `gap` 是相对昨收的比例，反推需要 `prev_close`，而 `prev_close` 同样没落盘。
    反推出来的"价格"是两个未落盘量的比值，看起来像数、实际不可复核。
    """

    metadata: Mapping[str, Any] = field(default_factory=dict)
    """存档里的旁证：`scanned_at` / `gap` / `vol_ratio` / `auction_amount`。

    `vol_ratio` 在这里**只是**存档里的一个值，不是已锁定门禁。
    """

    status: str = AUCTION_ABNORMAL_STATUS
    """恒 `"data_pending"`。"""

    triggered: bool | None = None
    """是否竞价异动。**恒 `None`**，表示"无法判断"。

    不要写成 `if not triggered:` —— `None` 是假值，那个写法会把"不知道"
    当成"没异动"。唯一正确的判据是 `triggered is False`，而本模块永不返回 `False`。
    """

    triggered_reason: str = "data_pending"

    auction_stat_bucket: str | None = AUCTION_ABNORMAL_AUCTION_STAT_BUCKET
    """恒 `"auction_0925"`。竞价必须进这个桶。"""

    auction_rule_id: str | None = AUCTION_ABNORMAL_AUCTION_RULE_ID
    """恒 `None`。规则未锁定。"""

    post_open_noise_flag: bool = AUCTION_ABNORMAL_POST_OPEN_NOISE_FLAG
    """恒 `False`。竞价不属于开盘后时段。"""

    post_open_noise_bucket: str | None = AUCTION_ABNORMAL_POST_OPEN_NOISE_BUCKET
    """恒 `None`。带非 None 值会被 `SignalRecord` 拒掉。"""

    evidence_pending: tuple[str, ...] = field(
        default_factory=lambda: tuple(sorted(AUCTION_ABNORMAL_EVIDENCE_PENDING))
    )
    """尚未清掉的证据缺口，随候选一起返回，免得调用方以为拿到的是可信结论。"""

    @property
    def data_pending(self) -> bool:
        """是否仍处"证据未到、无法判断"。当前恒 `True`。"""
        return self.status == AUCTION_ABNORMAL_STATUS

    @property
    def passed(self) -> bool:
        """是否**确认**竞价异动成立。只有 `triggered is True` 才算。当前恒 `False`。

        刻意的 `is True` 而不是 `bool(...)`：`None` 与 `False` 在这一层的
        含义完全不同（"不知道" vs "没异动"），不能靠真值性把它们糊成一个。
        """
        return self.triggered is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": AUCTION_ABNORMAL_SIGNAL_ID,
            "event_class": AUCTION_ABNORMAL_EVENT_CLASS,
            "family": AUCTION_ABNORMAL_FAMILY,
            "symbol": self.symbol,
            "display_name": self.display_name,
            "ticker": self.ticker,
            "trade_date": self.trade_date.isoformat(),
            "auction_ts": self.auction_ts.isoformat(),
            "auction_volume": self.auction_volume,
            "auction_price": self.auction_price,
            "status": self.status,
            "data_pending": self.data_pending,
            "triggered": self.triggered,
            "triggered_reason": self.triggered_reason,
            "auction_stat_bucket": self.auction_stat_bucket,
            "auction_rule_id": self.auction_rule_id,
            "post_open_noise_flag": self.post_open_noise_flag,
            "post_open_noise_bucket": self.post_open_noise_bucket,
            "noise_mix_forbidden": C.NOISE_MIX_FORBIDDEN,
            "evidence_pending": list(self.evidence_pending),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AuctionEvaluation:
    """一次**整日**竞价评估的结果。可能零候选，也可能有若干候选。

    两种情况下 `status` 都是 `data_pending`：缺存档是"没有数据"，有存档是
    "有数据但无结论"。这两个原因不同（前者 `reason="no_data"`，后者
    `reason="evidence_pending"`），但**结论相同**：无法判断。
    """

    trade_date: date_cls | None
    auction_ts: datetime | None
    status: str = AUCTION_ABNORMAL_STATUS
    reason: str = "no_data"

    scanned_at: str | None = None
    """存档里的 `scanned_at` —— 这个结论基于哪一刻的快照。缺存档时为 `None`。"""

    candidates: tuple[AuctionCandidate, ...] = ()

    evidence_pending: tuple[str, ...] = field(
        default_factory=lambda: tuple(sorted(AUCTION_ABNORMAL_EVIDENCE_PENDING))
    )

    triggered: bool | None = None
    """是否触发。**恒 `None`**，无论有没有候选。"""

    @property
    def data_pending(self) -> bool:
        return self.status == AUCTION_ABNORMAL_STATUS

    @property
    def has_scan(self) -> bool:
        """是否读到了存档。读到 ≠ 有结论。"""
        return self.reason != "no_data"

    @property
    def passed(self) -> bool:
        return self.triggered is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": AUCTION_ABNORMAL_SIGNAL_ID,
            "event_class": AUCTION_ABNORMAL_EVENT_CLASS,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "auction_ts": self.auction_ts.isoformat() if self.auction_ts else None,
            "status": self.status,
            "reason": self.reason,
            "data_pending": self.data_pending,
            "has_scan": self.has_scan,
            "scanned_at": self.scanned_at,
            "triggered": self.triggered,
            "triggering_enabled": AUCTION_ABNORMAL_TRIGGERING_ENABLED,
            "auction_stat_bucket": AUCTION_ABNORMAL_AUCTION_STAT_BUCKET,
            "auction_rule_id": AUCTION_ABNORMAL_AUCTION_RULE_ID,
            "post_open_noise_flag": AUCTION_ABNORMAL_POST_OPEN_NOISE_FLAG,
            "post_open_noise_bucket": AUCTION_ABNORMAL_POST_OPEN_NOISE_BUCKET,
            "noise_mix_forbidden": C.NOISE_MIX_FORBIDDEN,
            "evidence_pending": list(self.evidence_pending),
            "candidates": [c.to_dict() for c in self.candidates],
        }


def _coerce_trade_date(payload: Mapping[str, Any], fallback: date_cls | None) -> date_cls | None:
    """存档里的 `trade_date` 优先；解析不出来就退回调用的 `target`。"""
    raw = payload.get("trade_date")
    if isinstance(raw, str):
        try:
            return date_cls.fromisoformat(raw)
        except ValueError:
            pass
    return fallback


def _candidate_from_item(
    item: Mapping[str, Any],
    *,
    trade_date: date_cls,
    scanned_at: str | None,
) -> AuctionCandidate | None:
    """把存档里的一个 `item` 映射成候选。缺 `symbol` 的条目直接跳过。

    字段映射（与模块 docstring / docs 的表一致）：

    | 存档 | 候选 |
    |---|---|
    | `item.symbol` | `symbol`（必需，否则跳过） |
    | `item.ticker` / `item.name` | `ticker` / `display_name` |
    | `item.gap` | `metadata["gap"]` |
    | `item.vol_ratio` | `metadata["vol_ratio"]`（**不**当已锁门禁） |
    | `item.auction_volume` | `auction_volume` |
    | `item.auction_amount` | `metadata["auction_amount"]` |
    | （无）`auction_price` | `auction_price=None` |
    """
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None

    ticker = item.get("ticker")
    name = item.get("name")
    metadata: dict[str, Any] = {
        "gap": _to_float(item.get("gap")),
        "vol_ratio": _to_float(item.get("vol_ratio")),
        "auction_amount": _to_float(item.get("auction_amount")),
    }
    if scanned_at is not None:
        metadata["scanned_at"] = scanned_at

    return AuctionCandidate(
        symbol=symbol.strip(),
        trade_date=trade_date,
        auction_ts=auction_ts_for(trade_date),
        display_name=str(name).strip() if isinstance(name, str) and name.strip() else None,
        ticker=str(ticker).strip() if isinstance(ticker, str) and ticker.strip() else None,
        auction_volume=_to_float(item.get("auction_volume")),
        auction_price=None,
        metadata=metadata,
    )


def evaluate_auction_abnormal(
    data_dir: Path | str | None,
    target: date_cls | None = None,
) -> AuctionEvaluation:
    """读**已有**存档并给出一次竞价评估。**永远不会**说"异动了"。

    三条路径，结论都是 `data_pending`：

    | 情形 | `reason` | `candidates` |
    |---|---|---|
    | 没有 `data_dir` / 存档缺失（`state=no_data`） | `no_data` | 空 |
    | 读到存档但没有可映射条目 | `no_candidates` | 空 |
    | 读到存档且有条目 | `evidence_pending` | 若干候选，全部带四条 pending 标签 |

    第三条路径**最容易误读**：明明映射出了候选、`gap` 和 `vol_ratio` 都有值，
    看起来像"能算了"。但那些值是存档里现成的筛选结果，不是这条信号的触发结论 ——
    阈值没锁定、没有回测、没有 09:25 分钟证据，三条缺口一条没清。所以
    `triggered` 仍然是 `None`。

    这个函数**不**跑扫描、**不**重抓快照、**不**碰 `SCAN_WINDOW`。
    """
    payload = load_scan_payload(data_dir, target)

    if payload.get("state") != "ok":
        return AuctionEvaluation(
            trade_date=target,
            auction_ts=auction_ts_for(target) if target else None,
            reason="no_data",
        )

    trade_date = _coerce_trade_date(payload, target)
    if trade_date is None:
        return AuctionEvaluation(trade_date=None, auction_ts=None, reason="no_data")

    scanned_at = payload.get("scanned_at")
    scanned_at = str(scanned_at) if scanned_at is not None else None

    raw_items = payload.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    candidates = tuple(
        cand
        for cand in (
            _candidate_from_item(item, trade_date=trade_date, scanned_at=scanned_at)
            for item in items
            if isinstance(item, Mapping)
        )
        if cand is not None
    )

    return AuctionEvaluation(
        trade_date=trade_date,
        auction_ts=auction_ts_for(trade_date),
        reason="evidence_pending" if candidates else "no_candidates",
        scanned_at=scanned_at,
        candidates=candidates,
    )


def build_auction_abnormal_record(
    *,
    display_name: str = "竞价异动",
    spec_ref: str = "PHASE3 §2",
) -> SignalRecord:
    """按上面的身份钉构造一条注册表记录。

    身份字段全部取自模块常量，调用方无法覆盖 —— 否则"钉死"只是句口号。
    生命周期 `status` 固定 `AUCTION_ABNORMAL_RECORD_STATUS`（= `candidate`），
    评估态 `data_pending` 走 metadata，两个域不混。
    `auction_stat_bucket` 固定 `auction_0925`，`post_open_noise_bucket` 固定 `None`。

    构造出来的记录是合法的（能过 `SignalRecord.__post_init__`），但它**不代表
    这条信号可用**：合法只说明字段没写错，不说明阈值标定过、也不说明回测过。
    """
    return SignalRecord(
        signal_id=AUCTION_ABNORMAL_SIGNAL_ID,
        display_name=display_name,
        family=AUCTION_ABNORMAL_FAMILY,
        event_class=AUCTION_ABNORMAL_EVENT_CLASS,
        spec_ref=spec_ref,
        status=AUCTION_ABNORMAL_RECORD_STATUS,
        auction_rule_id=AUCTION_ABNORMAL_AUCTION_RULE_ID,
        auction_stat_bucket=AUCTION_ABNORMAL_AUCTION_STAT_BUCKET,
        post_open_noise_flag=AUCTION_ABNORMAL_POST_OPEN_NOISE_FLAG,
        post_open_noise_bucket=AUCTION_ABNORMAL_POST_OPEN_NOISE_BUCKET,
        metadata={
            "auction_abnormal_status": AUCTION_ABNORMAL_STATUS,
            "auction_abnormal_evidence_pending": sorted(AUCTION_ABNORMAL_EVIDENCE_PENDING),
            "auction_abnormal_triggering_enabled": AUCTION_ABNORMAL_TRIGGERING_ENABLED,
            "auction_ts_rule": "trade_date + 09:25:00+08:00, 不读分钟 bar",
        },
    )


__all__ = [
    "AUCTION_ABNORMAL_AUCTION_RULE_ID",
    "AUCTION_ABNORMAL_AUCTION_STAT_BUCKET",
    "AUCTION_ABNORMAL_EVENT_CLASS",
    "AUCTION_ABNORMAL_EVIDENCE",
    "AUCTION_ABNORMAL_EVIDENCE_PENDING",
    "AUCTION_ABNORMAL_FAMILY",
    "AUCTION_ABNORMAL_POST_OPEN_NOISE_BUCKET",
    "AUCTION_ABNORMAL_POST_OPEN_NOISE_FLAG",
    "AUCTION_ABNORMAL_RECORD_STATUS",
    "AUCTION_ABNORMAL_SIGNAL_ID",
    "AUCTION_ABNORMAL_STATUS",
    "AUCTION_ABNORMAL_TRIGGERING_ENABLED",
    "AUCTION_TS_TIME",
    "AuctionCandidate",
    "AuctionEvaluation",
    "auction_ts_for",
    "build_auction_abnormal_record",
    "evaluate_auction_abnormal",
    "load_scan_payload",
]
