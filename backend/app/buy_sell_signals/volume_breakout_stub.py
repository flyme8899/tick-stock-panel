"""放量突破的**身份 + 契约 + 空桩**（批次 20261025gauction · P1-3 · 竞价不做）。

仅供研究，不构成投资建议；执行须人工确认。

## 这个文件是什么，不是什么

**是**：把「放量突破这条信号，在注册表里长什么样」钉成可被测例扫描的常量 ——
`signal_id` / `event_class` / `family` / `auction_stat_bucket` / `auction_rule_id`
这五个字段的取值，以及「它为什么还不能跑」的四条证据缺口。

**不是**：放量触发引擎。这里没有任何量比计算、没有突破幅度判定、没有窗口分钟数、
没有回测。`evaluate_volume_breakout()` 恒返回 `data_pending`，**永远不会**返回
「今天放量了」。

## P-bss-k4 / K4：单位声明（本拍只加契约，不改行为）

量比是按「手」还是按「股」算，差 100 倍（#237 跨层单位对照）。所以本模块的契约多一条：
**没有合法 `volume_unit`（`手` / `股`）就不许出现非空 `volume_ratio`**。
空桩两边都是 `None`，天然满足；这条约束真正约束的是将来那个真实评估器。

`VOLUME_BREAKOUT_TRIGGERING_ENABLED` **仍 `False`**，本拍**没有**因此开放任何触发。
补 K4 **不等于**「#7 量审计已过」，也不等于可放量。

## 为什么这一拍只能出空桩

三条硬缺口，缺任一条都跑不出一个诚实的结论：

1. **本机没有分钟库**。远端 SSH 历史上有分钟数据，但本机既没挂载也没落盘；
   有远端源 ≠ 本机可跑（软提醒 2）。
2. **没有触发证据表**。放量突破的回测 / 触发表不存在，所以「量比 > X 且价
   突破 Y」这类阈值没有任何依据可以标定。
3. **HTTP 分钟列目录 401，凭证未交**。这一条不是"忘了配"，是"还没拿到"；
   本文件不写、也不该写任何密码 / 密钥 / token。

外加一条**已经被写下来的反证**：`app/custom/dsa/quant_evidence.yaml` 里的
`volume_breakout` 条目记录了四个近似口径的回测结论 ——
放量创60日新高 **-23.86%**、趋势突破 **-32.44%**、平台整理突破 **-13.80%**、
布林突破 **-19.81%**，四个口径**全为负**，`verdict: strongly_negative`。

所以这一拍更准确的说法不是「数据还没到，等到了就能跑」，而是
**「已有证据指向负期望，开闸前必须先解释这个负号」**。阈值可以标定，
负号解释不了就不能开。这一点写在这里，是为了让将来某个"补上数据就开跑"的
冲动先撞上它。

## 分列契约（竞价桶与放量桶不得混）

放量是 `intraday_volume`，属于**开盘后**的信息；`auction_0925` 是 09:25
集合竞价撮合那一刻的信息。两者的信息集不同（离散撮合 vs 连续成交），
统计性质也不同，**合在一起算出来的均值 / 命中率没有解释意义**
（`constants.NOISE_MIX_FORBIDDEN = True`）。

因此本文件把 `VOLUME_BREAKOUT_AUCTION_STAT_BUCKET` 钉成 `None`，并且
`build_volume_breakout_record()` 产出的记录 `auction_stat_bucket` 恒为 `None`。
`SignalRecord.__post_init__` 在 tip 上已经有通用拒绝规则（非 `auction` 类
不得带竞价桶），本模块只是**显式覆盖放量这条路径**并把它测下来。

**缺竞价包不得用放量回填竞价。** 「09:25 那个桶空着，先用开盘后第一根
1 分钟 K 线顶上」这个做法是永久禁止的：两者是完全不同的信息集，冒充会让
竞价类信号的统计全部失真，而且**不会报错**。

## 噪声档：可空桩，但不扩封闭集

`post_open_noise_bucket` 的合法取值是一个**封闭集**
（`constants.POST_OPEN_NOISE_BUCKETS`，tip 上是
`open_0930_1000` / `intraday_1000_1430` / `close_1430_1500` 三个）。

PHASE3 §3 里的示意名 `post_open_15m` **还没有**进入这个封闭集。本拍
**不扩集** —— 扩集等于悄悄修改一个封闭取值域，而封闭域的意义正是"不让人随手
往里加"。噪声档在本拍只作空桩（`False` / `None`）。

## 四条证据缺口

见 `VOLUME_BREAKOUT_EVIDENCE_PENDING` 与 `VOLUME_BREAKOUT_EVIDENCE`。
四条全部 `data_pending`，一条都没清。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.buy_sell_signals import constants as C
from app.buy_sell_signals.models import SignalRecord

# ------------------------------------------------------------------ 身份钉

VOLUME_BREAKOUT_SIGNAL_ID: Final = "volume_breakout"
"""注册表里的 `signal_id`。全仓唯一的放量突破信号。"""

VOLUME_BREAKOUT_EVENT_CLASS: Final = "intraday_volume"
"""事件类别 —— 盘中放量，**不是** `auction`。

这个取值必须 ∈ `constants.EVENT_CLASSES`。它是"开盘后"的语义标记：
拿它去填 `auction_stat_bucket` 会被 `SignalRecord` 直接拒掉。
"""

VOLUME_BREAKOUT_FAMILY: Final = "minute_event"
"""信号家族 —— 分钟级事件。须 ∈ `constants.SIGNAL_FAMILIES`。

家族决定验证路径：`minute_event` 意味着"必须有分钟级证据才能谈标定"，
而不能用日线代理糊过去。
"""

VOLUME_BREAKOUT_AUCTION_STAT_BUCKET: Final = None
"""竞价统计桶 —— **恒 `None`**，恒等，不是"默认值"。

放量信号永远不带竞价桶。把它写成 `C.AUCTION_STAT_BUCKET_0925` 是"把放量
写进竞价桶"，属于混桶，会让两段样本的统计互相污染，而且不会报错。
"""

VOLUME_BREAKOUT_AUCTION_RULE_ID: Final = C.AUCTION_RULE_ID_NONE
"""竞价规则 ID —— 沿用 `AUCTION_RULE_ID_NONE`（即 `None`）。

语义：这一类信号**根本不经过**竞价规则，不是"过了但没命中"。
"""

VOLUME_BREAKOUT_STATUS: Final = "data_pending"
"""**评估**状态。**恒 `data_pending`**。

注意这是「这次评估有没有结论」的语义，与 `SignalRecord.status`（**注册表生命周期**
状态，取值域 `SIGNAL_STATUSES`）**不是同一个域**，两者不可互相代入：

| 域 | 取值 | 语义 |
|---|---|---|
| 本常量（评估） | `data_pending` | 数据没到，无法判断 |
| `SignalRecord.status`（生命周期） | `candidate` / `observe` / `publishable` / `retired` / `data_abnormal` | 这条信号在注册表里走到哪一步了 |

`data_pending` **不在** `SIGNAL_STATUSES` 里，也**不应该**加进去 —— 往生命周期
取值域里塞一个评估态，会让状态机多出一条谁都不知道该怎么迁移的状态。构造记录时
用 `VOLUME_BREAKOUT_RECORD_STATUS`（= `candidate`）：还没验证，正是 candidate 的含义。
"""

VOLUME_BREAKOUT_RECORD_STATUS: Final = "candidate"
"""构造 `SignalRecord` 时用的**生命周期**状态。见 `VOLUME_BREAKOUT_STATUS` 的说明。

`candidate` = 刚注册、还没验证。这是诚实的：连一条可信证据都没有，
不该是 `observe`（那意味着"已进入观察"），更不该是 `publishable`。
"""

VOLUME_BREAKOUT_TRIGGERING_ENABLED: Final = False
"""是否允许产出"已触发"结论。**恒 `False`**。

没有任何理由在本拍把它改成 `True` —— 改了也只是让一个空桩撒谎。
真开放量需要另拍（阈值标定 + 触发表 + 负号解释），不是一个开关的事。
"""

# ------------------------------------------------------- 四条证据缺口

VOLUME_BREAKOUT_EVIDENCE_PENDING: Final = frozenset(
    {
        "local_minute_lib_data_pending",        # 本机无分钟库
        "trigger_evidence_table_data_pending",  # 无触发表 / 回测表
        "http_minute_listing_auth_pending",     # HTTP 列目录 401，凭证未交
        "threshold_lock_data_pending",          # 阈值未锁定
    }
)
"""四条证据缺口的**机器可读标签**。全部 `data_pending`，一条都没清。

标签都以 `pending` 结尾不是巧合：它们是**缺口**，不是完成项。将来清掉某一条时，
那个标签应当被删掉而不是被改成 `_done` —— 一个已清的条目留在 pending 集合里，
会让"还有几条没清"这个判断失真。
"""

VOLUME_BREAKOUT_EVIDENCE: Final[Mapping[str, tuple[str, str]]] = {
    "local_minute_lib_data_pending": (
        "本机分钟库",
        "本机既没挂载也没落盘分钟数据; 远端 SSH 历史有源 != 本机可跑, 不假装触达",
    ),
    "trigger_evidence_table_data_pending": (
        "触发证据表",
        "没有放量突破的回测表 / 触发表; 缺它, 任何量比与突破幅度阈值都没有标定依据",
    ),
    "http_minute_listing_auth_pending": (
        "HTTP 列目录凭证",
        "取分钟数据的 HTTP 入口 401, 凭证未交; 本仓不写、也不该写任何密码或 token",
    ),
    "threshold_lock_data_pending": (
        "阈值锁定",
        "量比 / 突破幅度 / 窗口分钟数均未锁定; 本拍不钉任何数值, 也不写 '已锁定'",
    ),
}
"""同一批缺口的**人读表**，键与 `VOLUME_BREAKOUT_EVIDENCE_PENDING` 一一对应。

两处分开写是刻意的：机器读标签，人读说明。键集合必须相等，有测例对比 ——
只改一处会让另一处悄悄过期。
"""

VOLUME_BREAKOUT_NEGATIVE_EVIDENCE: Final[Mapping[str, Any]] = {
    "source": "app/custom/dsa/quant_evidence.yaml",
    "key": "volume_breakout",
    "verdict": "strongly_negative",
    "returns": (-23.86, -32.44, -13.80, -19.81),
    "matched": ("放量创60日新高", "趋势突破", "平台整理突破", "布林突破"),
}
"""仓内**已有**的放量突破负证据（四个口径全为负）。

这不是本拍造的数，是从 `app/custom/dsa/quant_evidence.yaml` 抄来的既有结论，
放在这里是为了让"补上数据就开跑"的冲动先撞上它。本模块**不**据此下任何
投资结论，只主张一件事：开闸之前必须先解释这个负号。
"""

# ---------------------------------------------------------------- 空桩

VOLUME_BREAKOUT_NOISE_NOTE: Final = (
    "post_open_15m 是 PHASE3 示意名, 尚未进入 POST_OPEN_NOISE_BUCKETS 封闭集; "
    "本拍不扩集, 噪声档仅空桩"
)
"""噪声档的说明。写进函数返回值，免得读者以为 `post_open_15m` 已经可以用。"""


@dataclass(frozen=True, slots=True)
class VolumeBreakoutStub:
    """一次放量评估的**空**结果。

    每个字段都在说"不知道"，而不是"没事"。特别是 `triggered` 恒 `None` ——
    与 `health_veto_hit` 同一个理由：`False` 是一个结论（"查过，没放量"），
    在没有阈值、没有分钟数据、没有触发表的情况下返回 `False`，等于向调用方
    保证一件我们并不知道的事。
    """

    symbol: str
    asof: Any = None
    status: str = VOLUME_BREAKOUT_STATUS
    """恒 `"data_pending"`。"""

    triggered: bool | None = None
    """是否放量突破。**恒 `None`**，表示"无法判断"。

    不要写成 `if not triggered:` —— `None` 是假值，那个写法会把"不知道"
    当成"没放量"。唯一正确的判据是 `triggered is False`，而空桩永不返回 `False`。
    """

    triggered_reason: str = "data_pending"
    """为什么没有结论。"""

    volume_ratio: float | None = None
    """量比。**恒 `None`** —— 阈值未锁定，填任何数都是假数。

    ## P-bss-k4 / K4：无单位声明不得填非空量比

    量比是"按手算"还是"按股算"差 100 倍（见 #237 跨层单位对照）。所以 `volume_ratio`
    与 `volume_unit` 是**同生同死**的：没有合法 `volume_unit`（`手` / `股`）就**不许**
    在这里出现非空数值。空桩两边都是 `None`，这个不变式天然成立。

    这条约束管的是将来那个真实评估器，不是本拍：本拍仍恒 `None`，也**不**开触发。
    """

    volume_unit: str | None = None
    """评估单位声明。空桩恒 `None`。

    有真实评估器时须 `∈ {手, 股}`（`constants.VOLUME_UNITS`）；与 `volume_ratio`
    必须同时出现或同时为空。这里**不**做校验也不默认「手」—— 空桩没有量比，
    补一个默认单位反而是在替未来那个评估器提前撒谎。
    """

    breakout_pct: float | None = None
    """突破幅度。**恒 `None`** —— 同上。"""

    auction_stat_bucket: str | None = VOLUME_BREAKOUT_AUCTION_STAT_BUCKET
    """恒 `None`。放量不进竞价桶。"""

    auction_rule_id: str | None = VOLUME_BREAKOUT_AUCTION_RULE_ID
    """恒 `None`。放量不经过竞价规则。"""

    post_open_noise_flag: bool = False
    """噪声档标记。空桩恒 `False`（未判定，不是"判定为无噪声"）。"""

    post_open_noise_bucket: str | None = None
    """噪声档桶。空桩恒 `None`（见 `VOLUME_BREAKOUT_NOISE_NOTE`）。"""

    evidence_pending: tuple[str, ...] = tuple(sorted(VOLUME_BREAKOUT_EVIDENCE_PENDING))
    """尚未清掉的证据缺口，随结果一起返回，免得调用方以为拿到的是可信结论。"""

    noise_note: str = VOLUME_BREAKOUT_NOISE_NOTE

    @property
    def data_pending(self) -> bool:
        """是否仍处"数据未到、无法判断"。当前恒 `True`。"""
        return self.status == VOLUME_BREAKOUT_STATUS

    @property
    def passed(self) -> bool:
        """是否**确认**放量突破成立。只有 `triggered is True` 才算。当前恒 `False`。

        刻意的 `is True` 而不是 `bool(...)`：`None` 与 `False` 在这一层的
        含义完全不同（"不知道" vs "没放量"），不能靠真值性把它们糊成一个。
        """
        return self.triggered is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": VOLUME_BREAKOUT_SIGNAL_ID,
            "event_class": VOLUME_BREAKOUT_EVENT_CLASS,
            "family": VOLUME_BREAKOUT_FAMILY,
            "symbol": self.symbol,
            "asof": self.asof.isoformat() if hasattr(self.asof, "isoformat") else str(self.asof),
            "status": self.status,
            "data_pending": self.data_pending,
            "triggered": self.triggered,
            "triggered_reason": self.triggered_reason,
            "volume_ratio": self.volume_ratio,
            "volume_unit": self.volume_unit,
            "breakout_pct": self.breakout_pct,
            "auction_stat_bucket": self.auction_stat_bucket,
            "auction_rule_id": self.auction_rule_id,
            "post_open_noise_flag": self.post_open_noise_flag,
            "post_open_noise_bucket": self.post_open_noise_bucket,
            "noise_mix_forbidden": C.NOISE_MIX_FORBIDDEN,
            "evidence_pending": list(self.evidence_pending),
        }


def evaluate_volume_breakout(symbol: str, asof: Any = None) -> VolumeBreakoutStub:
    """评估一只票在 `asof` 是否放量突破。**当前恒返回"无法判断"。**

    这个函数不做任何计算，也不读任何数据源。它存在的意义是把签名与返回形状
    先钉下来，让调用方按"可能没有结论"来写代码，而不是等引擎上线了才发现
    到处都在假设"一定有布尔值"。

    特别地：**不要**在别处用 09:30 之后第一根 1 分钟 K 线的成交量去顶替
    09:25 的竞价量。两者是完全不同的信息集，冒充的结果不会报错，只会让统计失真。
    """
    return VolumeBreakoutStub(symbol=symbol, asof=asof)


def build_volume_breakout_record(
    *,
    display_name: str = "放量突破",
    spec_ref: str = "PHASE3 §3",
) -> SignalRecord:
    """按上面的身份钉构造一条注册表记录。

    身份字段全部取自模块常量，调用方无法覆盖 —— 否则"钉死"只是句口号。
    生命周期 `status` 固定 `VOLUME_BREAKOUT_RECORD_STATUS`（= `candidate`），
    评估态 `data_pending` 走 `status_pending` 进 `metadata`，两个域不混。
    `auction_stat_bucket` 固定 `None`。

    构造出来的记录是合法的（能过 `SignalRecord.__post_init__`），但它**不代表
    这条信号可用**：合法只说明字段没写错，不说明阈值标定过。
    """
    return SignalRecord(
        signal_id=VOLUME_BREAKOUT_SIGNAL_ID,
        display_name=display_name,
        family=VOLUME_BREAKOUT_FAMILY,
        event_class=VOLUME_BREAKOUT_EVENT_CLASS,
        spec_ref=spec_ref,
        status=VOLUME_BREAKOUT_RECORD_STATUS,
        auction_rule_id=VOLUME_BREAKOUT_AUCTION_RULE_ID,
        auction_stat_bucket=VOLUME_BREAKOUT_AUCTION_STAT_BUCKET,
        post_open_noise_flag=False,
        post_open_noise_bucket=None,
        metadata={
            "volume_breakout_status": VOLUME_BREAKOUT_STATUS,
            "volume_breakout_evidence_pending": sorted(VOLUME_BREAKOUT_EVIDENCE_PENDING),
            "volume_breakout_triggering_enabled": VOLUME_BREAKOUT_TRIGGERING_ENABLED,
            "noise_note": VOLUME_BREAKOUT_NOISE_NOTE,
        },
    )


__all__ = [
    "VOLUME_BREAKOUT_AUCTION_RULE_ID",
    "VOLUME_BREAKOUT_AUCTION_STAT_BUCKET",
    "VOLUME_BREAKOUT_EVENT_CLASS",
    "VOLUME_BREAKOUT_EVIDENCE",
    "VOLUME_BREAKOUT_EVIDENCE_PENDING",
    "VOLUME_BREAKOUT_FAMILY",
    "VOLUME_BREAKOUT_NEGATIVE_EVIDENCE",
    "VOLUME_BREAKOUT_NOISE_NOTE",
    "VOLUME_BREAKOUT_RECORD_STATUS",
    "VOLUME_BREAKOUT_SIGNAL_ID",
    "VOLUME_BREAKOUT_STATUS",
    "VOLUME_BREAKOUT_TRIGGERING_ENABLED",
    "VolumeBreakoutStub",
    "build_volume_breakout_record",
    "evaluate_volume_breakout",
]
