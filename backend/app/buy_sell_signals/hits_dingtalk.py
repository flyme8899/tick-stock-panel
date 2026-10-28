"""BSS **命中**钉钉外发(批次 20261027gbssding · P-ding-later)。

仅供研究, 不构成投资建议; 执行须人工确认。

## 这个模块解决的是什么

`push.py` 那套是**定义状态变更**的出口: 信号定义从 `observe` 变成 `publishable` 这类
审批动作, 一周几次, 挂在资讯/狙击共用的 `DINGTALK_WEBHOOK_URL` 上没有问题。

命中是另一回事: 逐股逐日, 一次批量就可能十几条。挂同一个群, 一次命中潮就把
"哪条信号被改过状态"刷掉了 —— 而后者恰恰更需要人看一眼。所以命中走**独立 webhook**:
`BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL`。

## 三条通道, 互不相通

| 通道 | webhook 来源 | 门 | 本模块 |
|---|---|---|---|
| 定义态状态变更 | `DINGTALK_WEBHOOK_URL` | PUSH ∧ `BUY_SELL_SIGNALS_DINGTALK` ∧ URL 非空 | 零碰 |
| **命中**(本模块) | `BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL` | URL 非空 ∧ 可见性闸 ∧ 非 fixture ∧ 冷却未命中 | 唯一实现 |
| DSA 定时复盘 | 复盘专用键(`DSA_REVIEW_*`) | 日 cap(#227) | 零碰 |

三者的代码、键、失败原因字符串全部不同。**没有**"命中缺配就发到共群"这条路:
错发出去的消息收不回来, 而"没配"这个事实在配置层面看着丢人, 在行为层面却是安全的。

## 拒发不是降级, 是正确结果

缺 `BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL` 时返回
`HITS_WEBHOOK_NOT_CONFIGURED`, 且**attempts = 0**(一个字节都没有发出去)。
不写日志正文里的 url(日志里贴 url = 泄露), 也不假装"发了但被过滤"。

## 只发已校准的命中

外发前的闸**不在这里重写**, 而是直接调 `hits_visibility.visible_hits([hit], ...)`
并断言返回 1 条。理由: 监控页看到的那道闸和这里外发的那道闸如果各写一遍, 总有一天
会不一致 —— 而不一致的方向通常是"外发松了"。复用同一函数, 冲突在代码层不可能发生。

**命中无权自称已校准**: 命中记录里没有 `calibrated` 字段, 本模块也不看任何自述,
只认独立 `calib.json` 经 `CalibIndex` 给出的 `passed`。

## 挂点: 显式调用(不自动扫)

`HitStore` 是**只读**协议(`list_all`, 无 upsert), 生产也没有写 live 命中的代码路径。
所以本拍不假装"已接线自动推送": 唯一入口是显式调用 `try_push_hit`。

**禁止**新增任何定时器 / 调度框架 / `lifespan` / `daily_pipeline` 去遍历命中表外发;
**禁止**在只读 store 里塞写钩伪装自动推; **禁止**启动时扫盘补发历史命中。

没配 webhook 或没人调用 → 生产**零外发**。这是预期状态, 不是缺陷。

## 去重冷却只在成功后写

同 `(signal_id, definition_version, symbol, asof, freq_lock_version)` 五元组
30 分钟内只发一次。冷却时间戳**只在 `send_markdown` 成功返回后**写入 —— 失败不写,
否则一次网络抖动会把这条命中按 30 分钟静默。载体是进程内 dict: 重启后短窗内可能
重复一条, 这是可接受的; 跨重启持久化不在本拍范围。
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from app.market_time import CN_TZ, cn_now

from . import disclaimer as _disclaimer
from .calib_store import CalibIndex
from .constants import (
    HITS_DINGTALK_DEDUPE_COOLDOWN_MIN,
    HITS_DINGTALK_DEDUPE_KEY_FIELDS,
    HITS_DINGTALK_SECRET_ENV_NAME,
    HITS_DINGTALK_WEBHOOK_ENV_NAME,
    HITS_WEBHOOK_NOT_CONFIGURED,
)
from .hits_models import SignalHit
from .hits_visibility import visible_hits

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 结果原因

REASON_PUSHED: Final = "pushed"
"""实际发出去了(`send_markdown` 未抛)。"""

REASON_ALREADY_IN_FLIGHT: Final = "already_in_flight"
"""同键正在发送中(并发调用)。占位防同一秒内两个线程各发一条。"""

REASON_NOT_VISIBLE: Final = "not_visible"
"""未过可见性闸。具体卡在哪一条见 `HitPushOutcome.reject`(沿用 `VisibilityReject`)。"""

REASON_FIXTURE: Final = "fixture_not_pushable"
"""`source.mode == "fixture"` 的命中不进生产外发。测例须显式开 `allow_fixture`。"""

REASON_SHADOW: Final = "shadow_not_pushable"
"""`source.mode == "shadow"` 的命中不进生产外发(批次 20261028gp3a · P-bss-p3a)。

字符串与 `fixture_not_pushable` 对称, 但**故意不对称为开关**:

- `fixture` 有 `allow_fixture`, 因为夹具命中是**测例**自己造的, 放行是测试手段;
- `shadow` **没有** `allow_shadow`, 也不许将来加。影子命中是"用真实数据、按未锁定
  的口径算出来的", 它长得和生产命中一模一样 —— 一旦有一个开关能把它放进生产钉钉群,
  那条群消息就没有任何特征能让人看出它是影子。测例要验影子路径, 应当直接断言拒发。

与可见性闸的关系: 即使这道门被绕过, `visible_hits` 的 `NON_LIVE_MODE` 仍会挡住它 ——
两道闸判的是同一件事的不同面(推不推 / 看不看), 冗余是刻意的。
"""

REASON_DEDUPE: Final = "hits_dedupe_cooldown"
"""同一五元组在 30 分钟冷却窗内已发过。"""

REASON_SEND_FAILED: Final = "hits_send_failed"
"""`send_markdown` 抛了。**不**回滚命中、**不**阻塞监控 API、**不**写冷却。"""

REASON_KEYS: Final = frozenset(
    {
        REASON_PUSHED,
        REASON_ALREADY_IN_FLIGHT,
        REASON_NOT_VISIBLE,
        REASON_FIXTURE,
        REASON_SHADOW,
        REASON_DEDUPE,
        REASON_SEND_FAILED,
        HITS_WEBHOOK_NOT_CONFIGURED,
    }
)
"""全部合法 reason(**封闭集**)。加新的要先想清楚"运维看到这个字符串该做什么"。"""


# ---------------------------------------------------------------- 结果对象


@dataclass(frozen=True, slots=True)
class HitPushOutcome:
    """一次 `try_push_hit` 的结果。

    `attempts` 与 `reason` 是分开的两个维度: `reason` 说"为什么没发/为什么发了",
    `attempts` 说"到底发了几次"(0 或 1)。缺配时两者是
    `(hits_webhook_not_configured, 0)` —— 明确地"一条都没发", 而不是一个模糊的 False。
    """

    reason: str
    attempts: int = 0
    reject: str | None = None
    """过闸失败时的 `VisibilityReject` 值(仅 `not_visible` 时非空), 便于运维定位。"""

    dedupe_key: tuple[str, ...] = ()
    """本次判定用的去重键(无个股以外内容, 可安全进日志)。"""

    @property
    def ok(self) -> bool:
        return self.reason == REASON_PUSHED

    @property
    def pushed(self) -> bool:
        return self.reason == REASON_PUSHED


# ---------------------------------------------------------------- 配置读取


def hits_webhook_url() -> str:
    """命中钉钉的 webhook。**只**读命中专用键。

    顺序: `settings.buy_sell_signals_hits_dingtalk_webhook_url` 优先, 空则回落到
    直接读同名 env。两条路径读的是**同一个名字**, 不构成"第二条来源" ——
    生产走 settings(pydantic 从 env 装载), 未装载 settings 的脚本/测例走 env。
    刻意**不**读 `settings.dingtalk_webhook_url`: 那是共群, 不属于命中通道。

    注意这里**没有**任何"如果命中键没配就用旧键"的分支。加那个分支等于把
    "没配"变成"配好了", 而错发出去的群消息收不回来。
    """
    raw = ""
    try:
        from app.config import settings

        raw = getattr(settings, "buy_sell_signals_hits_dingtalk_webhook_url", "") or ""
    except Exception:  # noqa: BLE001 - 读配置失败不该让外发路径 500
        raw = ""
    if not str(raw).strip():
        raw = os.environ.get(HITS_DINGTALK_WEBHOOK_ENV_NAME, "") or ""
    return str(raw).strip()


def hits_webhook_secret() -> str:
    """命中钉钉的加签密钥(可选)。空 = 该机器人未开启加签。"""
    raw = ""
    try:
        from app.config import settings

        raw = getattr(settings, "buy_sell_signals_hits_dingtalk_secret", "") or ""
    except Exception:  # noqa: BLE001
        raw = ""
    if not str(raw).strip():
        raw = os.environ.get(HITS_DINGTALK_SECRET_ENV_NAME, "") or ""
    return str(raw).strip()


def hits_webhook_ready() -> bool:
    """命中通道是否可发(只看 URL 非空)。

    **没有额外 enable 旗标**(N3 写死): URL 非空即门, 对标 #227 复盘通道。
    多一个旗标就多一个"配了但忘了开"的位置。
    """
    return bool(hits_webhook_url())


def _common_group_webhook_url() -> str:
    """资讯 / 定义态 / 狙击共用的那个 webhook（只读，**仅用于同群比对告警**）。

    这个值**不参与任何发送**。单独抽成函数有两个好处：一是让"谁读了共群 URL"
    在文件里只有一处、可被静态断言（见测例 V3），二是测例能直接替换它，
    不必去改不可变的 `settings`。
    """
    raw = ""
    try:
        from app.config import settings

        raw = getattr(settings, "dingtalk_webhook_url", "") or ""
    except Exception:  # noqa: BLE001
        raw = ""
    if not str(raw).strip():
        raw = os.environ.get("DINGTALK_WEBHOOK_URL", "") or ""
    return str(raw).strip()


def shares_webhook_with_common_group() -> bool:
    """命中 webhook 是否与资讯/定义态/狙击共用的 `DINGTALK_WEBHOOK_URL` 相同。

    **相同不拒发**: 所有者显式填同一个群是他的选择, 我们只提醒。
    同群意味着注意力竞争没解除 —— 命中潮会淹掉定义状态变更。这个判断只用于日志
    告警, 不参与任何门控。
    """
    hits = hits_webhook_url()
    if not hits:
        return False
    common = _common_group_webhook_url()
    return bool(common) and hits == common


# ---------------------------------------------------------------- 文案


def build_hit_dingtalk_text(hit: SignalHit) -> tuple[str, str]:
    """组命中消息 `(title, text)`。

    标题前缀「买卖信号命中」与定义态的「买卖信号 · {display_name}」**刻意不同**:
    两个通道可能被填成同一个群(`shares_webhook_with_common_group` 已对此告警),
    群里必须一眼能分出这是"某条信号被命中"还是"某条信号被改状态"。

    正文**只放可审计的标识**: symbol / direction_label / signal_id / definition_version
    / asof / family / event_class / hit_id。**不放价格、数量、仓位、订单、任何可执行
    指令** —— 本包没有这些数据, 凭空补一个"参考价"就是 fabricate。
    """
    title = f"买卖信号命中 · {hit.symbol}"[:64]
    lines = [
        f"**命中** {hit.symbol} · {hit.direction_label}",
        f"signal_id={hit.signal_id} · definition_version={hit.definition_version} · asof={hit.asof}",
        f"family={hit.family} · event_class={hit.event_class} · hit_id={hit.hit_id}",
        "",
        f"_{_disclaimer.DISCLAIMER_TEXT}_",
    ]
    return title, "\n".join(lines)


# ---------------------------------------------------------------- 去重冷却


def dedupe_key(hit: SignalHit) -> tuple[str, ...]:
    """按 `HITS_DINGTALK_DEDUPE_KEY_FIELDS` 的顺序取键。缺失字段补空串, 不抛。"""
    mapping: Mapping[str, Any] = {
        "signal_id": hit.signal_id,
        "definition_version": hit.definition_version,
        "symbol": hit.symbol,
        "asof": hit.asof,
        "freq_lock_version": hit.freq_lock_version,
    }
    return tuple(str(mapping.get(name) or "") for name in HITS_DINGTALK_DEDUPE_KEY_FIELDS)


class HitsDedupe:
    """进程内冷却表。**只在成功外发后**才写。

    为什么不在发送前就占位: 发送失败(网络抖动)时若已写冷却, 这条命中会在 30 分钟内
    完全静默 —— 而失败恰恰是最需要重试的情形。所以先发, 发成了再记。

    并发: 用一把锁保护整个"查 + 标记在途"的动作, 再加 `_inflight` 阻止同一键在
    发送过程中被重复进入。用完即删, 不占内存。
    """

    def __init__(self, cooldown_min: int = HITS_DINGTALK_DEDUPE_COOLDOWN_MIN) -> None:
        self._cooldown = timedelta(minutes=int(cooldown_min))
        self._until: dict[tuple[str, ...], datetime] = {}
        self._inflight: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()

    def cooled_down(self, key: tuple[str, ...], *, now: datetime) -> bool:
        """该键是否还在冷却窗内(只读, 会顺手清过期项)。"""
        stamp = _as_cn(now)
        with self._lock:
            deadline = self._until.get(key)
            if deadline is None:
                return False
            if stamp >= deadline:
                self._until.pop(key, None)
                return False
            return True

    def claim(self, key: tuple[str, ...]) -> bool:
        """标记"正在发"。返回 False 表示同键已有请求在途。"""
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def release(self, key: tuple[str, ...]) -> None:
        with self._lock:
            self._inflight.discard(key)

    def mark_sent(self, key: tuple[str, ...], *, now: datetime) -> None:
        """**仅在成功外发后**调用。"""
        stamp = _as_cn(now)
        with self._lock:
            self._until[key] = stamp + self._cooldown

    def clear(self) -> None:
        """测例用: 清空全部状态, 避免跨测例串味。"""
        with self._lock:
            self._until.clear()
            self._inflight.clear()


def _as_cn(stamp: datetime) -> datetime:
    """统一到 Asia/Shanghai。裸时间按上海处理(本包所有时间都走 `cn_now`)。"""
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=CN_TZ)
    return stamp.astimezone(CN_TZ)


#: 模块级冷却表。进程内共享 —— 外发是进程级行为, 不该给每个调用方各发一条。
_DEDUPE: Final = HitsDedupe()


def _dedupe() -> HitsDedupe:
    return _DEDUPE


# ---------------------------------------------------------------- 主卡口


def try_push_hit(
    hit: SignalHit,
    registry: Any,
    *,
    calib: CalibIndex | None,
    scope_symbols: frozenset[str] | None = None,
    today: Any = None,
    now: datetime | None = None,
    opener: Callable[..., Any] | None = None,
    allow_fixture: bool = False,
    webhook: str | None = None,
    secret: str | None = None,
    dedupe: HitsDedupe | None = None,
) -> HitPushOutcome:
    """尝试把**一条命中**外发到命中钉钉通道。

    硬门顺序(**顺序本身是契约**, 测例逐条钉):

    1. fixture: `hit.source.mode == "fixture"` → `fixture_not_pushable`
       (测例须显式 `allow_fixture=True`; 生产调用方传默认值即可)
    1b. shadow: `hit.source.mode == "shadow"` → `shadow_not_pushable`(批次 20261028gp3a)
       **没有** `allow_shadow`, 将来也不许加: 影子命中与生产命中长得完全一样, 一个
       开关就能让它混进生产钉钉群, 而群里没人看得出。夹具是测例造的, 影子是真实数据
       算的 —— 前者可以放行, 后者不行。
    2. 可见性: 复用 `visible_hits([hit], ...)`, 返回 0 条 → `not_visible` + `reject`
       (非 live 若漏过 1/1b, 这里还会被 `NON_LIVE_MODE` 兜住 —— 冗余是刻意的)
    3. 缺配: 命中 webhook 空 → `hits_webhook_not_configured`, **attempts = 0**
    4. 冷却: 同五元组 30 分钟内已发 → `hits_dedupe_cooldown`
    5. 发送: `send_markdown(hits_url, hits_secret, title, text)`
       - 成功 → `pushed`, 写冷却
       - 抛异常 → `hits_send_failed`, **不写冷却**, 不让异常冒出去

    参数:
    - `registry` / `calib` / `scope_symbols` / `today`: 原样传给 `visible_hits`,
      保证这里的判定与监控页**同构**。
    - `webhook` / `secret`: 仅测例与受控脚本注入; 传 `None` 则读配置。
    - `opener`: 仅测例注入(传给 `send_markdown` → `post_payload`)。

    返回 `HitPushOutcome`, **永不抛**。外发是旁路: 它失败不该让调用它的主流程坏掉。
    """
    # ---- 门 1: fixture 不进生产外发
    mode = getattr(getattr(hit, "source", None), "mode", "")
    if mode == "fixture" and not allow_fixture:
        logger.warning(
            "买卖信号命中外发跳过: 合成命中(source.mode=fixture)不进生产钉钉通道 "
            "(hit_id=%s)",
            getattr(hit, "hit_id", ""),
        )
        return HitPushOutcome(reason=REASON_FIXTURE)

    # ---- 门 1b: shadow 不进生产外发(无 allow_shadow, 且不打算加)
    #
    # 与门 1 不对称是刻意的: fixture 有 `allow_fixture` 给测例用, shadow 一个开关都不给。
    # 理由见 `REASON_SHADOW` 的 docstring —— 影子命中与生产命中形状完全一致, 放进群里
    # 没有任何可辨识特征。
    if mode == "shadow":
        logger.warning(
            "买卖信号命中外发跳过: 影子命中(source.mode=shadow)不进生产钉钉通道 "
            "(hit_id=%s)",
            getattr(hit, "hit_id", ""),
        )
        return HitPushOutcome(reason=REASON_SHADOW)

    # ---- 门 2: 可见性(复用监控页那道闸, 不另写软闸)
    filtered = visible_hits(
        [hit],
        registry,
        scope_symbols=scope_symbols,
        today=today,
        calib=calib,
    )
    if filtered.count != 1:
        reject = next(iter(filtered.rejected), None)
        logger.info(
            "买卖信号命中外发跳过: 未通过可见性闸 (reject=%s, hit_id=%s)",
            reject,
            getattr(hit, "hit_id", ""),
        )
        return HitPushOutcome(reason=REASON_NOT_VISIBLE, reject=reject)

    # ---- 门 3: 缺配即拒发(**不回落**共群)
    url = (webhook if webhook is not None else hits_webhook_url()).strip()
    if not url:
        logger.info(
            "买卖信号命中钉钉未配置 %s, 已跳过外发(不会发到资讯/定义态共群)。",
            HITS_DINGTALK_WEBHOOK_ENV_NAME,
        )
        return HitPushOutcome(reason=HITS_WEBHOOK_NOT_CONFIGURED)

    key = dedupe_key(hit)
    table = dedupe if dedupe is not None else _dedupe()
    stamp = _as_cn(now or cn_now())

    # ---- 门 4: 冷却
    if table.cooled_down(key, now=stamp):
        logger.info(
            "买卖信号命中外发跳过: %d 分钟内已发过同一命中 (key=%s)",
            HITS_DINGTALK_DEDUPE_COOLDOWN_MIN,
            "/".join(key),
        )
        return HitPushOutcome(reason=REASON_DEDUPE, dedupe_key=key)
    if not table.claim(key):
        return HitPushOutcome(reason=REASON_ALREADY_IN_FLIGHT, dedupe_key=key)

    try:
        if shares_webhook_with_common_group():
            logger.warning(
                "买卖信号命中钉钉与资讯/定义态/狙击共用同一 webhook, "
                "注意力竞争未解除(命中潮会淹掉定义状态变更)。仍按配置发送。"
            )
        title, text = build_hit_dingtalk_text(hit)
        # 延迟 import: `app.news.dingtalk` 会拖起 urllib, 而本模块在无外发需求的
        # 场景(纯读监控页)也会被导入。
        from app.news.dingtalk import send_markdown

        send_markdown(
            url,
            (secret if secret is not None else hits_webhook_secret()),
            title,
            text,
            opener=opener,
        )
    except Exception as exc:  # noqa: BLE001 - 外发失败不冒泡, 不写冷却
        logger.warning(
            "买卖信号命中外发失败(best-effort, 不影响命中存储与监控接口): %s",
            type(exc).__name__,
        )
        return HitPushOutcome(reason=REASON_SEND_FAILED, attempts=1, dedupe_key=key)
    finally:
        table.release(key)

    table.mark_sent(key, now=stamp)
    logger.info("买卖信号命中已外发到命中钉钉通道 (hit_id=%s)", hit.hit_id)
    return HitPushOutcome(reason=REASON_PUSHED, attempts=1, dedupe_key=key)


def maybe_try_push_hit(hit: SignalHit, *args: Any, **kwargs: Any) -> HitPushOutcome:
    """`try_push_hit` 的吞异常包装。

    `try_push_hit` 本身已经不抛(它逐门 return), 这层只是给将来"在写入成功路径上
    顺手调一行"的调用点加一道保险: 将来那块代码改动引入新异常, 也不该让写入回滚。
    **本拍不实现评估器写路径**, 所以这个函数目前只在测例里被覆盖到。
    """
    try:
        return try_push_hit(hit, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("买卖信号命中外发外层兜底: %s", type(exc).__name__)
        return HitPushOutcome(reason=REASON_SEND_FAILED)


@dataclass(frozen=True, slots=True)
class HitsDingtalkStatus:
    """命中钉钉通道的只读自述。运维/诊断用, **不含 secret、不含完整 url**。"""

    configured: bool
    shares_with_common_group: bool
    cooldown_min: int = HITS_DINGTALK_DEDUPE_COOLDOWN_MIN
    reason_when_missing: str = HITS_WEBHOOK_NOT_CONFIGURED
    env_name: str = HITS_DINGTALK_WEBHOOK_ENV_NAME
    notes: tuple[str, ...] = field(
        default=(
            "命中通道与定义态/资讯/狙击共群、以及定时复盘的钉钉通道都不相通",
            "缺配拒发, 不回落 DINGTALK_WEBHOOK_URL",
            "无额外 enable 旗标: URL 非空即门",
        )
    )


def hits_dingtalk_status() -> HitsDingtalkStatus:
    """当前命中通道状态。只回布尔与常量, **不回 url / secret**。"""
    return HitsDingtalkStatus(
        configured=hits_webhook_ready(),
        shares_with_common_group=shares_webhook_with_common_group(),
    )


__all__ = [
    "HITS_DINGTALK_DEDUPE_COOLDOWN_MIN",
    "HITS_WEBHOOK_NOT_CONFIGURED",
    "REASON_ALREADY_IN_FLIGHT",
    "REASON_DEDUPE",
    "REASON_FIXTURE",
    "REASON_KEYS",
    "REASON_NOT_VISIBLE",
    "REASON_PUSHED",
    "REASON_SEND_FAILED",
    "REASON_SHADOW",
    "HitPushOutcome",
    "HitsDedupe",
    "HitsDingtalkStatus",
    "build_hit_dingtalk_text",
    "dedupe_key",
    "hits_dingtalk_status",
    "hits_webhook_ready",
    "hits_webhook_secret",
    "hits_webhook_url",
    "maybe_try_push_hit",
    "shares_webhook_with_common_group",
    "try_push_hit",
]
