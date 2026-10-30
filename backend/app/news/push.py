"""钉钉推送框架。热点候选、异动监控、做T提醒各自开关、时段和冷却。

默认全关。登录失效提醒仍走 host_collector 的短文本，不从这里发送。
异动用实时报价对盘前价位，不读盘后信号列。报价缓存新鲜时不再打 TickFlow。

## 批次 20261030gtedge: 做T 的边沿源换成分钟 bar(图≠钉)

**图有钉无 = 边沿源不同构**, 不是文案缺字、不是阈值不一致。

| | 取数 | 判定 |
|---|---|---|
| 图(`trade_marks.replay_t_marks`) | 当日分钟 bar 序列 | `session_signal_sets` → 逐根差分 |
| 旧钉(本模块) | `_live_rows` 报价快照 @180s | `t_conditions(单行)` |

一分钟级的窗在两次快照之间就开合完毕, 图上留下 mark, 钉上什么都没发。
本拍把 `_load_t` 改成走与图**同一个** `minute_edge_events` 引擎, 只推新边沿。

**冷却 / 日封顶 / 文案 / 三 env 一律未动**(值未放宽)。缩 `INTRADAY_CHECK_S` **不是**
本拍手段, 也不足以单独修好 —— 换了边沿源才修得好。

**文案齐 ≠ 漏触发已修**: gtpush / #243 系三件套本拍一字未改, 本拍也**不**宣称它已修
漏触发。**合入 ≠ 部署**; **tip ≠ prod**(prod 仍 `7e5c983…`), 本拍默不部署, 也不部署
#254 / #255。**建议T ≠ BSS**: 钉钉「建议T买入/卖出/观察」是研究用的做T提醒, 不是
BSS 买卖信号, 更不是下单指令。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp(非回测, 旁注)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.config import settings
from app.news.config import (
    SOURCE_LABELS,
    push_master_enabled,
    push_type_enabled,
    push_type_saved,
    webhook_configured,
)
from app.news.dingtalk import send_markdown
from app.news.hot_events import HOT_RULES_VERSION, display_view_key
from app.market_time import CN_TZ
from app.services.amount_guard import sanitize_bar_amount, vwap_outside_day_range

logger = logging.getLogger(__name__)

PRIVATE_SOURCES = frozenset({"dws", "zsxq", "ima"})
LINK_SOURCES = frozenset({"cls", "wscn", "glh"})
ABNORMAL_SIGNALS = frozenset({
    "limit_up", "broken", "recovery", "limit_down", "new_high", "new_low",
})
SIGNAL_LABELS = {
    "limit_up": "涨停",
    "broken": "炸板",
    "recovery": "跌停翘板",
    "limit_down": "跌停",
    "new_high": "60日新高",
    "new_low": "60日新低",
}
T_LABELS = {
    "above_vwap": "高于分时均价",
    "below_vwap": "低于分时均价",
    "near_high": "接近日内高点",
    "near_low": "接近日内低点",
    "near_prev_close": "回到昨收附近",
}
RANGE_RULES = frozenset({"near_high", "near_low"})
TYPE_LABELS = {"hot": "热点候选", "abnormal": "异动监控", "t_trade": "做T提醒"}

# ---------------------------------------------------------------- 做T文案(批次 20261028gtpush)

#: 建议**买入**的理由。只影响文案里的一个词, 不影响边沿判定/冷却/日封顶。
_T_BUY_REASONS = frozenset({"below_vwap", "near_low"})
#: 建议**卖出**的理由。同上。
_T_SELL_REASONS = frozenset({"above_vwap", "near_high"})
#: 两个都不是的理由(`near_prev_close` = 回到昨收附近)只能是**观察**。
#: 刻意不给它一个方向: 回到昨收既不是「便宜了」也不是「贵了」, 它是「重新对齐」。
_T_WATCH_REASONS = frozenset({"near_prev_close"})

TRIGGER_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

_CHANGE_MARKS = {"new": "新进", "jump": "升温"}
_KIND_LABELS = {"event": "事件", "sector": "板块", "stock": "个股", "etf": "ETF"}

HOT_CHECK_S = 300
# 一次新进超过这个数，多半是基线空了或榜单整批换血，只更新基线不推。
HOT_NEW_BURST = 8
INTRADAY_CHECK_S = 180
SLOT_WINDOW = timedelta(minutes=20)
RATE_LIMIT = 20
RATE_WINDOW_S = 60
ABNORMAL_CAP = 80
T_CAP = 40
VWAP_BAND = 0.015
RANGE_BAND = 0.003
RANGE_QUIET_UNTIL = dt_time(10, 0)
PREV_BAND = 0.003
PREV_AWAY = 0.01
RANGE_MIN = 0.01
LIMIT_TOL = 0.005
QUOTE_FRESH_S = 180

_STATE: PushState | None = None
_STATE_LOCK = threading.Lock()

#: 批次 20261030gtedge: 做T 分钟边沿「本进程内已处理过的 bar 时间戳」。
#:   key   = (symbol, "YYYY-MM-DD")
#:   value = 该标的该日已处理过的 bar `HH:MM` 集合
#: 只用于去重(同一根 bar 不重复报), **不参与任何判定**, 也不落盘: 进程重启后
#: 从当日第一根重放, 边沿仍由 `minute_edge_events` 按 bar 根数冷却算出, 与图一致。
#: 上限 512 条, 超过就丢最早插入的——一天几十只标的、240 根/只, 正常量级远低于此。
_T_MINUTE_SEEN: dict[tuple[str, str], set[str]] = {}

#: 批次 20261030gtedge: 观察钩子(仅供测), 收到本次做T 宇宙。生产恒为 `None`。
_T_MINUTE_PROBE = None


class TMinuteEdges(tuple):
    """`_load_t()` 的返回值: 前三项与旧三元组**逐字节等价**, 第四项是分钟戳 meta。

    ## 为什么用 tuple 子类

    既有调用方与测例大量写着 `current, pct, names = _load_t()` /
    `assert _load_t() == ({}, {}, {})`。**返回值加一项会让它们整批红掉**, 而它们
    本来在测与边沿源无关的东西(改 `_load_t` 签名是上一批次专门记过的一条禁忌)。

    所以这里做成一个 tuple 子类: `len()` 仍是 3, 相等比较只比前三项, 解包也只解三项;
    第四项 `meta` 挂在属性上, 由 `_split_loader` 认出来交给 `_push_edges`。
    没有 meta 的路径(报价快照、夹具注入)行为与改动前逐字节一致。
    """

    # 注意: tuple 子类不能有非空 `__slots__`(CPython 限制), 所以属性存在实例
    # `__dict__` 里。代价是每个实例多一个 dict, 而一天只有几十次构造。

    def __new__(cls, current, pct, names, meta=None):
        obj = super().__new__(cls, (current, pct, names))
        obj.meta = meta or {}
        return obj

    def __getitem__(self, index):
        if index == 3:
            return self.meta
        return super().__getitem__(index)

    def __len__(self) -> int:
        return 3

    def __eq__(self, other) -> bool:
        # 与普通三元组比较时只比前三项(测例大量写着 `== ({}, {}, {})`); 与另一个
        # `TMinuteEdges` 比较时连 meta 一起比 —— 两个 loader 结果不相等, 不该因为
        # 「前三项恰好一样」被当成同一个。
        if isinstance(other, TMinuteEdges):
            return tuple(self) == tuple(other) and self.meta == other.meta
        return tuple(self) == other


def _clock(name: str, default: dt_time) -> dt_time:
    raw = str(getattr(settings, name, "") or "").strip()
    if not raw:
        return default
    try:
        hour, minute = raw.split(":", 1)
        return dt_time(int(hour), int(minute))
    except ValueError:
        return default


def premarket_at() -> dt_time:
    return _clock("news_push_premarket", dt_time(8, 45))


def postclose_at() -> dt_time:
    return _clock("news_push_postclose", dt_time(15, 40))


def top_n() -> int:
    return max(1, min(int(settings.news_push_top_n or 5), 20))


def min_stories() -> int:
    return max(1, min(int(settings.news_push_min_stories or 2), 20))


def score_jump() -> float:
    try:
        value = float(settings.news_push_score_jump)
    except (TypeError, ValueError):
        value = 0.5
    return value if value > 0 else 0.5


def hot_cooldown_s() -> int:
    return max(60, int(settings.news_push_hot_cooldown_min or 30) * 60)


def symbol_cooldown_s() -> int:
    return max(60, int(settings.news_push_symbol_cooldown_min or 30) * 60)


def t_cooldown_s() -> int:
    return max(60, int(settings.news_push_t_cooldown_min or 60) * 60)


def t_range_cooldown_s() -> int:
    return max(60, int(settings.news_push_t_range_cooldown_min or 60) * 60)


def t_daily_cap() -> int | None:
    """全自选当天做T消息条数。0 表示不限制。"""
    try:
        value = int(settings.news_push_t_daily_cap)
    except (TypeError, ValueError):
        value = 20
    if value <= 0:
        return None
    return min(value, 500)


def _cooldown_seconds(kind: str, reason: str) -> int:
    if kind == "abnormal":
        return symbol_cooldown_s()
    if reason in RANGE_RULES:
        return t_range_cooldown_s()
    return t_cooldown_s()


def pick_refs(messages: list[dict], *, limit: int = 3) -> list[dict]:
    """每条候选最多 3 个出处。钉钉、知识星球、ima 只留来源名，不带标题、链接和正文。"""
    refs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for msg in messages:
        source = str(msg.get("source") or "")
        if source in PRIVATE_SOURCES:
            label = str(msg.get("source_label") or SOURCE_LABELS.get(source, source))
            if ("name", label) in seen:
                continue
            seen.add(("name", label))
            refs.append({"kind": "name", "label": label})
        elif source in LINK_SOURCES:
            url = str(msg.get("url") or "")
            label = str(msg.get("source_label") or SOURCE_LABELS.get(source, source))
            title = str(msg.get("title") or label)[:40]
            if url.startswith(("http://", "https://")):
                if ("url", url) in seen:
                    continue
                seen.add(("url", url))
                refs.append({"kind": "link", "label": title, "url": url})
            elif ("name", label) not in seen:
                seen.add(("name", label))
                refs.append({"kind": "name", "label": label})
        if len(refs) >= limit:
            break
    return refs


def _ref_text(ref: dict) -> str:
    if ref.get("kind") == "link" and ref.get("url"):
        return f"[{ref.get('label') or '链接'}]({ref['url']})"
    return str(ref.get("label") or "")


def _confirm_label(confirm: dict) -> str:
    strength = str(confirm.get("strength") or "")
    phase = str(confirm.get("phase") or "")
    name = "竞价验证" if phase == "auction" else "盘面验证"
    horizon = str(confirm.get("horizon") or "")
    if horizon not in {"主线", "一日游"}:
        horizon = str(confirm.get("persistence") or "")
        if horizon in {"", "无"}:
            horizon = ""
    if confirm.get("lagged") and strength in {"", "无"}:
        return "消息滞后确认"
    if strength in {"", "无"}:
        return ""
    text = f"{name} {strength}"
    if horizon:
        text += f"·{horizon}"
    if confirm.get("lagged"):
        text = f"消息滞后确认·{text}"
    return text


def event_is_verified(confirm: dict | None) -> bool:
    """该事件是否**盘面已验证**（有真实观测并打出分）。

    改为**白名单**：只有 `strength` 是 强/中/弱 才算已验证。
    早前是黑名单（`strength not in {"", "无"}`）—— 那是 fail-open 写法：任何新增的
    strength 取值都会默认落进「已验证」，被 `_verified_event_names()` 当成有效信号
    映射进钉钉推送。同时优先看 `state`（更准）：`scored` 才算已验证，
    `unverifiable`/`unconfirmed`/`pending` 一律不算。
    """
    if not isinstance(confirm, dict):
        return False
    state = str(confirm.get("state") or "")
    if state:
        return state == "scored"
    return str(confirm.get("strength") or "") in {"强", "中", "弱"}


def _verified_event_names() -> dict[str, str]:
    """已验证事件映射到的个股。同一标的多条时保留先出现的。"""
    from app.news.service import hot_event_listing

    found: dict[str, str] = {}
    try:
        listing = hot_event_listing(limit=20)
    except Exception:  # noqa: BLE001
        logger.debug("读取已验证事件失败", exc_info=True)
        return found
    for event in listing.get("events") or []:
        if not isinstance(event, dict) or not event_is_verified(event.get("confirmation")):
            continue
        name = str(event.get("name") or "").strip()
        if not name:
            continue
        phase = str((event.get("confirmation") or {}).get("phase") or "")
        prefix = "竞价验证" if phase == "auction" else "盘面验证"
        label = f"{prefix} {name}"
        for stock in event.get("stocks") or []:
            if not isinstance(stock, dict):
                continue
            key = str(stock.get("key") or "").strip()
            if key and key not in found:
                found[key] = label
    return found


def _change_line(index: int, row: dict) -> str:
    mark = _CHANGE_MARKS.get(str(row.get("reason") or ""), "变化")
    kind = _KIND_LABELS.get(str(row.get("kind") or ""), "")
    prefix = f"{mark} · {kind}" if kind else mark
    name = str(row.get("name") or row.get("key") or "")
    confirm = row.get("confirmation") if isinstance(row.get("confirmation"), dict) else {}
    checked = _confirm_label(confirm)
    label = f" · {checked}" if checked else ""
    return (
        f"{index}. {prefix} · {name}{label} · "
        f"提及 {row.get('story_count', 0)} · "
        f"来源 {row.get('source_count', 0)}"
    )


def format_hot_markdown(
    sectors: list[dict],
    stocks: list[dict],
    *,
    heading: str,
    events: list[dict] | None = None,
    etfs: list[dict] | None = None,
    changed: list[dict] | None = None,
) -> tuple[str, str] | None:
    """热点候选。具体事件在前，板块、个股、ETF 升温榜在后。没有内容时不发。

    「变化」把新进、升温列在当前榜单前面。
    """
    blocks = []
    if changed:
        lines = ["**本次变化**"]
        for index, row in enumerate(changed, start=1):
            lines.append(_change_line(index, row))
        blocks.append("\n".join(lines))
    if events:
        lines = ["**具体事件**"]
        for index, row in enumerate(events, start=1):
            concepts = "、".join(str(item) for item in (row.get("concepts") or []) if str(item).strip())
            concept_text = f" · {concepts}" if concepts else ""
            seen = row.get("first_seen") or ""
            seen_text = f" · 首见 {seen}" if seen else ""
            tag = str(row.get("category") or "").strip()
            direction = str(row.get("direction") or "").strip()
            level = str(row.get("importance") or "").strip()
            confirm = row.get("confirmation") if isinstance(row.get("confirmation"), dict) else {}
            checked = _confirm_label(confirm)
            label = "".join(f" · {part}" for part in (level, checked, tag, direction) if part)
            name = str(row.get("name") or row.get("key") or "")
            lines.append(
                f"{index}. {name}{label}{concept_text} · "
                f"提及 {row.get('story_count', 0)} · "
                f"来源 {row.get('source_count', 0)}{seen_text}"
            )
            subtitle = str(row.get("subtitle") or "").strip()
            if subtitle:
                lines.append(f"   {subtitle}")
            headline = str(row.get("headline") or "")
            if headline and headline != name and name not in headline and headline not in name:
                lines.append(f"   - {headline}")
            for ref in row.get("refs") or []:
                text = _ref_text(ref)
                if text:
                    lines.append(f"   - {text}")
        blocks.append("\n".join(lines))
    for title, rows in (("热门板块", sectors), ("热门个股", stocks), ("热门ETF", etfs or [])):
        if not rows:
            continue
        lines = [f"**{title}**"]
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index}. {row.get('name') or row.get('key')} · "
                f"提及 {row.get('story_count', 0)} · "
                f"来源 {row.get('source_count', 0)} · "
                f"相对基线 {row.get('growth', 0)} 倍"
            )
            for ref in row.get("refs") or []:
                text = _ref_text(ref)
                if text:
                    lines.append(f"   - {text}")
        blocks.append("\n".join(lines))
    if not blocks:
        return None
    title = heading[:64]
    text = f"### {heading}\n\n" + "\n\n".join(blocks) + "\n\n仅供内部研究，不含资讯正文。"
    return title, text


def _fmt_trigger_ts(now: datetime | None) -> str:
    """触发时刻 → `YYYY-MM-DD HH:MM:SS`, **固定上海时区**。

    `now` 为 `None` 时取当前时刻。aware 的一律 `astimezone(CN_TZ)`; naive 的按
    `CN_TZ` 解释(本函数的输入恒来自 `_push_edges` 的 `now`, 那边是 aware 的)。
    不猜 UTC: 猜错 8 小时会让「14:52 触发」写成「06:52」, 而读的人不会怀疑时区。
    """
    moment = now if now is not None else datetime.now(CN_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ).strftime(TRIGGER_TS_FORMAT)


def _t_side_label(reason: str) -> str:
    """理由 → 「建议T买入 / 建议T卖出 / 建议T观察」。

    **这是文案映射, 不是交易指令**: 它只决定括号里写哪个词, 不决定推不推、
    推几次、什么时候推。边沿成立与冷却全在 `_push_edges`, 与这里无关。
    """
    if reason in _T_BUY_REASONS:
        return "建议T买入"
    if reason in _T_SELL_REASONS:
        return "建议T卖出"
    return "建议T观察"


def _fmt_price(value) -> str | None:
    """报价两位小数; 缺失/非数/非正一律 `None`(不填 0, 也不写「0.00」)。"""
    number = _num(value)
    if number is None or number <= 0:
        return None
    return f"{number:.2f}"


def _t_reason_parts(row: dict) -> str:
    """理由字段: 「种类中文 · 现价 X」(有数时)。

    种类用 `T_LABELS` 的中文, **拿不到中文就退回原始 reason** —— 退回到原始码而不是
    「未知」: 原始码至少能被搜到、能追到规则名, 「未知」等于把线索丢了。

    报价缺失的项**直接不出现**, 不留 `--` 占位: 占位会被读成「有这个数但很小」。

    ## 本批次(20261028gtprice): 价格段**只留现价**

    以前这里是「现价 X · 均价 Y · 高 H · 低 L」四段。均价与日内高低在「高于分时均价」
    这类原因标签里**已经说过了**, 再打一遍数字只是噪音 —— 一条提醒里三四个小数,
    真正要看的现价反而不显眼。所以本拍把价格键收窄到只剩 `("price", "现价")`。

    **注意: 收窄的是「打印」, 不是「计算」。** `vwap` 仍然被 `t_conditions` 与
    `_quote_lookup` 正常算、正常用于边沿判定; `high` / `low` 也仍然参与区间规则。
    这里只是不再把它们拼进文案。所以「文案里没有均价」**不等于**「系统不看均价」。

    `T_LABELS` 里的原因中文(「高于分时均价」「接近日内高点」「回到昨收附近」…)
    **一个字都不动** —— 那是**为什么**被叫醒, 不是价格数值。

    价格段**没有昨收键**([已核] tip 上就没有): 昨收只以 `near_prev_close` 原因标签
    的形式出现, 从不作为价格数值打印。
    """
    reason = str(row.get("reason") or "")
    label = T_LABELS.get(reason, reason)
    parts = [label] if label else []
    for key, name in (
        ("price", "现价"),
        # 20261028gtprice: 不再打印 ("vwap", "均价") / ("high", "高") / ("low", "低")
        # —— 只收窄文案, vwap/high/low 的计算与边沿判定不受影响。
    ):
        text = _fmt_price(row.get(key))
        if text is not None:
            parts.append(f"{name} {text}")
    return " · ".join(parts)


def format_symbol_markdown(
    kind: str,
    rows: list[dict],
    *,
    now: datetime | None = None,
) -> tuple[str, str] | None:
    """异动监控 / 做T提醒的钉钉文案。

    ## 本批次(20261028gtprice)改了什么

    `t_trade` 的**理由字段价格段收窄到只留现价**, `abnormal` **一个字没动**。
    改前是「现价 X · 均价 Y · 高 H · 低 L」四段, 改后只剩「现价 X」(有数时)。

    为什么: 均价与日内高低在原因标签(「高于分时均价」「接近日内高点」)里已经表达过,
    再打一遍具体数字只是噪音。**收窄的只是打印**, vwap / high / low 的计算与边沿判定
    完全不变 —— 见 `_t_reason_parts` 的说明。

    ## 上一批次(20261028gtpush)给的文案三件套 —— 本拍**零回退**

    `t_trade` 的**文案三件套**, `abnormal` **一个字没动**:

    1. **触发时刻**: 标题下第一行 `触发时刻 YYYY-MM-DD HH:MM:SS`(上海时区)。
       没有时刻的提醒, 事后无法判断它是「刚发生」还是「补发的」, 也没法和行情
       对时间轴。
    2. **建议方向**: 每条前面标「建议T买入 / 建议T卖出 / 建议T观察」。
    3. **理由字段**: 种类中文 + **现价**(有数时)。让人不用回看行情就知道为什么被叫醒。

    `T_LABELS` 的原因中文全部保留 —— 本拍只动价格数值那一段。

    ## 本批次**没**改什么

    冷却、频率、日封顶、边沿成立、过滤规则、`T_CAP`、`VWAP_BAND`、`RANGE_BAND`、
    `PREV_BAND`、`INTRADAY_CHECK_S` —— **全部零改**。本函数只吃 `rows` 里的字段,
    不读行情、不写状态、不决定发不发。
    """
    label = TYPE_LABELS[kind]
    if not rows:
        return None
    if kind == "t_trade":
        lines = [f"### 【{label}】", f"触发时刻 {_fmt_trigger_ts(now)}"]
        for row in rows[:15]:
            reason = str(row.get("reason") or "")
            side = _t_side_label(reason)
            body = _t_reason_parts(row)
            detail = row.get("detail") or ""
            suffix = f" · {detail}" if detail and detail not in body else ""
            lines.append(
                f"- {side} · {row.get('name') or row['symbol']} {row['symbol']}"
                f"{(' · ' + body) if body else ''}{suffix}"
            )
        lines.append("\n仅供内部研究，建议须人工确认。")
        return f"【{label}】", "\n".join(lines)
    lines = [f"### 【{label}】"]
    for row in rows[:15]:
        extra = row.get("detail") or ""
        suffix = f" · {extra}" if extra else ""
        lines.append(f"- {row.get('name') or row['symbol']} {row['symbol']}{suffix}")
    lines.append("\n仅供内部研究。")
    return f"【{label}】", "\n".join(lines)


def format_test_markdown() -> tuple[str, str]:
    text = (
        "### 【测试】热点候选推送\n\n"
        "这是一条手动测试，没有附带资讯正文。\n\n"
        "示例：华为发布新模型 · 昇腾 · 提及 3 · 来源 2 · 首见 14:52\n\n"
        "正式推送会标成【热点候选】、【异动监控】或【做T提醒】。"
        "登录失效仍是单独的短文本。"
    )
    return "【测试】热点候选推送", text


def hot_material(previous: list[dict] | None, current: list[dict], *, top_n: int, min_stories: int, jump: float) -> list[dict]:
    """新进入前 N 且提及数够，或仍在前 N 但分数跳升。没有上次快照时只建立基线。"""
    if previous is None:
        return []
    prev = {(item["kind"], item["key"]): item for item in previous}
    changed = []
    for item in current:
        if int(item.get("rank") or 0) > top_n:
            continue
        ident = (item["kind"], item["key"])
        old = prev.get(ident)
        if old is None:
            if int(item.get("story_count") or 0) >= min_stories:
                changed.append({**item, "reason": "new"})
            continue
        old_score = float(old.get("score") or 0)
        new_score = float(item.get("score") or 0)
        if old_score > 0 and (new_score - old_score) / old_score >= jump:
            changed.append({**item, "reason": "jump"})
    return changed


def new_edges(previous: dict[str, set[str]] | None, current: dict[str, set[str]]) -> list[tuple[str, str]]:
    """第一次见到当天状态时不推，避免启动时把已经成立的信号全发出去。"""
    if previous is None:
        return []
    found = []
    for symbol, signals in current.items():
        before = previous.get(symbol, set())
        for signal in sorted(signals - before):
            found.append((symbol, signal))
    return found


def limit_price(previous: float, limit_pct: float, *, up: bool) -> float:
    """涨跌停价。与 price_limits.polars_limit_price 同一套分整数算法。"""
    sign = 1 if up else -1
    numerator = round((1 + sign * limit_pct) * 100)
    cents = int((previous * 100 + 0.5) // 1)
    return ((cents * numerator + 50) // 100) / 100.0


def abnormal_signals(row: dict) -> set[str]:
    """涨跌停用原始价对照昨收。60 日新高新低用前复权价对照盘前 59 日收盘极值。"""
    price = _num(row.get("raw_close")) or _num(row.get("close")) or _num(row.get("last_price")) or _num(row.get("price"))
    high = _num(row.get("raw_high")) or _num(row.get("high"))
    low = _num(row.get("raw_low")) or _num(row.get("low"))
    opened = _num(row.get("open"))
    prev = _num(row.get("prev_close"))
    prior_high = _num(row.get("prior_high"))
    prior_low = _num(row.get("prior_low"))
    level_price = _num(row.get("qfq_close"))
    if level_price is None:
        level_price = price
    found: set[str] = set()
    if level_price and prior_high and level_price >= prior_high:
        found.add("new_high")
    if level_price and prior_low and prior_low > 0 and level_price <= prior_low:
        found.add("new_low")
    symbol = str(row.get("symbol") or "")
    if not (price and prev and prev > 0 and symbol):
        return found
    when = row.get("trade_date")
    if isinstance(when, datetime):
        when = when.date()
    if not isinstance(when, date):
        from app.market_time import cn_today
        when = cn_today()
    from app.price_limits import is_risk_warning_name, price_limit_pct
    pct = price_limit_pct(symbol, when, is_risk_warning=is_risk_warning_name(str(row.get("name") or "")))
    up = limit_price(prev, pct, up=True)
    down = limit_price(prev, pct, up=False)
    sealed_up = price >= up - LIMIT_TOL
    sealed_down = price <= down + LIMIT_TOL
    if sealed_up:
        found.add("limit_up")
    if sealed_down:
        found.add("limit_down")
    if high and high >= up - LIMIT_TOL and not sealed_up:
        found.add("broken")
    if low and opened and low <= down + LIMIT_TOL and not sealed_down and price > opened:
        found.add("recovery")
    return found


def t_universe(
    watchlist: list[str],
    positions: list[str],
    *,
    include_positions: bool,
    cap: int,
) -> list[str]:
    """做T默认只看自选。include_positions 时再把有持仓的代码补进去。"""
    symbols: list[str] = []
    for symbol in watchlist:
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if include_positions:
        for symbol in positions:
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols[:cap]


def count_rule_edges(series: list[set[str]], cooldown_min: int) -> dict[str, int]:
    """按分钟序列数「从无到有」的次数。第一分钟只做基线，冷却期内不重复。"""
    previous: set[str] | None = None
    last_fire: dict[str, int] = {}
    counts: dict[str, int] = {}
    for index, current in enumerate(series):
        if previous is None:
            previous = set(current)
            continue
        fresh = []
        for reason in sorted(current - previous):
            last = last_fire.get(reason)
            if last is not None and index - last < cooldown_min:
                continue
            fresh.append(reason)
        previous = set(current)
        for reason in fresh:
            counts[reason] = counts.get(reason, 0) + 1
            last_fire[reason] = index
    return counts


def session_signal_sets(
    bars: list[dict],
    *,
    mode: str,
    symbol: str,
    prev_close: float | None,
    prior_high: float | None = None,
    prior_low: float | None = None,
    vwap_band: float = VWAP_BAND,
    range_band: float = RANGE_BAND,
    trade_date: date | None = None,
    name: str = "",
) -> list[set[str]]:
    """把一分钟一根的增量 K 收成当天的信号序列。成交量按根累加。"""
    running_high = None
    running_low = None
    opened = None
    volume = 0.0
    amount = 0.0
    previous_pct = None
    sets: list[set[str]] = []
    for bar in bars:
        close = _num(bar.get("close"))
        high = _num(bar.get("high"))
        low = _num(bar.get("low"))
        bar_open = _num(bar.get("open"))
        if opened is None:
            opened = bar_open or close
        if high is not None:
            running_high = high if running_high is None else max(running_high, high)
        if low is not None:
            running_low = low if running_low is None else min(running_low, low)
        bar_volume = _num(bar.get("volume")) or 0.0
        # 用本根高低价/收盘清洗 amount，再累加。一根 10 倍脏额会把后续 VWAP 全部抬出区间。
        bar_amount = sanitize_bar_amount(
            _num(bar.get("amount")), bar_volume, low=low, high=high, close=close,
        )
        volume += bar_volume
        amount += 0.0 if bar_amount is None else bar_amount
        row = {
            "symbol": symbol,
            "name": name,
            "close": close,
            "open": opened,
            "high": running_high,
            "low": running_low,
            "volume": volume,
            "amount": amount,
            "prev_close": prev_close,
            "prior_high": prior_high,
            "prior_low": prior_low,
            "trade_date": trade_date,
        }
        if mode == "abnormal":
            sets.append(abnormal_signals(row))
            continue
        signals, pct = t_conditions(row, vwap_band=vwap_band, range_band=range_band)
        if "near_prev_close" in signals and (previous_pct is None or abs(previous_pct) < PREV_AWAY):
            signals.discard("near_prev_close")
        previous_pct = pct
        sets.append(signals)
    return sets


def t_conditions(
    row: dict,
    *,
    vwap_band: float = VWAP_BAND,
    range_band: float = RANGE_BAND,
) -> tuple[set[str], float | None]:
    """用当日累计均价和日内高低。窗口不够或价格缺失的条件不成立。"""
    price = _num(row.get("close"))
    high = _num(row.get("high"))
    low = _num(row.get("low"))
    volume = _num(row.get("volume"))
    amount = _num(row.get("amount"))
    prev = _num(row.get("prev_close"))
    found: set[str] = set()
    pct = None
    if price and volume and amount and volume > 0:
        vwap = amount / (volume * 100.0)
        # 累计均价落在当日高低价之外时，成交额与价格对不上。跳过 VWAP 条件，
        # 不改偏离阈值，也不改近高/近低。实时报价只有日累计，洗不了单根 bar。
        if vwap > 0 and not vwap_outside_day_range(vwap, low, high):
            dev = price / vwap - 1
            if dev >= vwap_band:
                found.add("above_vwap")
            elif dev <= -vwap_band:
                found.add("below_vwap")
    if price and high and low and low > 0 and (high - low) / low >= RANGE_MIN:
        if high > 0 and price / high - 1 >= -range_band:
            found.add("near_high")
        if price / low - 1 <= range_band:
            found.add("near_low")
    if price and prev and prev > 0:
        pct = price / prev - 1
        if abs(pct) <= PREV_BAND:
            found.add("near_prev_close")
    return found, pct


def filter_prev_close(entered: list[tuple[str, str]], prev_pct: dict[str, float], current_pct: dict[str, float]) -> list[tuple[str, str]]:
    """回到昨收只在先前已经偏离至少 1% 时提醒。"""
    kept = []
    for symbol, reason in entered:
        if reason != "near_prev_close":
            kept.append((symbol, reason))
            continue
        before = prev_pct.get(symbol)
        if before is None or abs(before) < PREV_AWAY:
            continue
        if abs(current_pct.get(symbol) or 0) > PREV_BAND:
            continue
        kept.append((symbol, reason))
    return kept


def in_slot(now: datetime, start: dt_time, window: timedelta = SLOT_WINDOW) -> bool:
    begin = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return begin <= now < begin + window


def in_spans(now: datetime, spans: tuple[tuple[dt_time, dt_time], ...]) -> bool:
    clock = now.timetz().replace(tzinfo=None)
    return any(start <= clock <= end for start, end in spans)


ABNORMAL_SPANS = (
    (dt_time(9, 25), dt_time(11, 30)),
    (dt_time(13, 0), dt_time(15, 5)),
)
T_SPANS = (
    (dt_time(9, 35), dt_time(11, 25)),
    (dt_time(13, 5), dt_time(14, 55)),
)


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return float(value)


class PushState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data = self._read()

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"sent_at": [], "slots": {}, "cooldowns": {}, "edges": {}, "t_pct": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        payload.setdefault("sent_at", [])
        payload.setdefault("slots", {})
        payload.setdefault("cooldowns", {})
        payload.setdefault("edges", {})
        payload.setdefault("t_pct", {})
        return payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def rate_ok(self, now_ts: float) -> bool:
        fresh = [item for item in self.data["sent_at"] if now_ts - float(item) < RATE_WINDOW_S]
        self.data["sent_at"] = fresh
        return len(fresh) < RATE_LIMIT

    def mark_sent(self, now_ts: float) -> None:
        self.data["sent_at"].append(now_ts)
        self.data["sent_at"] = [
            item for item in self.data["sent_at"] if now_ts - float(item) < RATE_WINDOW_S
        ]

    def cooling(self, key: str, now_ts: float) -> bool:
        until = float(self.data["cooldowns"].get(key) or 0)
        return now_ts < until

    def cool_until(self, key: str, now_ts: float, seconds: int) -> None:
        self.data["cooldowns"][key] = now_ts + seconds


def get_state() -> PushState:
    global _STATE
    with _STATE_LOCK:
        path = settings.data_dir / "news" / "push_state.json"
        if _STATE is None or _STATE.path != path:
            _STATE = PushState(path)
        return _STATE


def reset_state_for_tests(path: Path) -> PushState:
    global _STATE
    with _STATE_LOCK:
        _STATE = PushState(path)
        return _STATE


def _snapshot_rows(items: list[dict]) -> list[dict]:
    rank = {"sector": 0, "stock": 0}
    rows = []
    for item in items:
        kind = item["kind"]
        rank[kind] = rank.get(kind, 0) + 1
        sources = item.get("sources") or []
        source_count = item.get("source_count")
        if source_count is None:
            source_count = len(sources)
        row = {
            "kind": kind,
            "key": item["key"],
            "name": item.get("name") or item["key"],
            "score": item.get("score") or 0,
            "story_count": item.get("story_count") or 0,
            "source_count": int(source_count or 0),
            "growth": item.get("growth") or 0,
            "rank": rank[kind],
        }
        if item.get("first_seen"):
            row["first_seen"] = item["first_seen"]
        if item.get("headline"):
            row["headline"] = item["headline"]
        if item.get("category"):
            row["category"] = item["category"]
        if item.get("direction"):
            row["direction"] = item["direction"]
        if item.get("importance"):
            row["importance"] = item["importance"]
        if isinstance(item.get("confirmation"), dict):
            row["confirmation"] = item["confirmation"]
        concepts = [str(concept) for concept in (item.get("concepts") or []) if str(concept).strip()]
        if concepts:
            row["concepts"] = concepts
        rows.append(row)
    return rows


def _edge_map(state: PushState, kind: str, day: str) -> dict[str, set[str]] | None:
    bucket = state.data["edges"].get(kind) or {}
    if bucket.get("day") != day:
        return None
    raw = bucket.get("symbols") or {}
    return {symbol: set(signals) for symbol, signals in raw.items()}


def _store_edges(state: PushState, kind: str, day: str, current: dict[str, set[str]]) -> None:
    state.data["edges"][kind] = {
        "day": day,
        "symbols": {symbol: sorted(signals) for symbol, signals in current.items()},
    }


def _due(state: PushState, key: str, now_ts: float, interval: int) -> bool:
    last = float(state.data.get(key) or 0)
    return now_ts - last >= interval


def _eligible_hot_rows(rows: list[dict] | None, *, verified_only: bool) -> list[dict] | None:
    """触发和正文用同一份事件。只推已验证时，未验证事件不参与比较。"""
    if rows is None or not verified_only:
        return rows
    return [
        row for row in rows
        if row.get("kind") != "event" or event_is_verified(
            row.get("confirmation") if isinstance(row.get("confirmation"), dict) else None
        )
    ]


def _hot_digest(day: str, text: str) -> str:
    """同一天的正文哈希。下一天的盘前、收盘不因昨天的正文被跳过。"""
    return hashlib.sha256(f"{day}\n{text}".encode()).hexdigest()


def _hot_names(*groups: list[dict]) -> list[str]:
    names: list[str] = []
    for rows in groups:
        for row in rows:
            name = str(row.get("name") or row.get("key") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def _send(kind: str, title: str, text: str, opener, now_ts: float, state: PushState) -> bool:
    if not state.rate_ok(now_ts):
        logger.info("钉钉推送达到每分钟 %s 条上限，本轮跳过 %s", RATE_LIMIT, TYPE_LABELS.get(kind, kind))
        return False
    send_markdown(
        settings.dingtalk_webhook_url.strip(),
        (settings.dingtalk_secret or "").strip(),
        title,
        text,
        opener=opener,
    )
    state.mark_sent(now_ts)
    return True


def send_test(*, confirm: bool, opener=None, state: PushState | None = None, now: datetime | None = None) -> dict:
    if not confirm:
        raise ValueError("需要明确确认后才发送测试消息")
    if not webhook_configured():
        raise ValueError("未配置钉钉机器人")
    state = state or get_state()
    moment = now or _now()
    title, text = format_test_markdown()
    with state._lock:
        if not _send("test", title, text, opener, moment.timestamp(), state):
            raise RuntimeError("钉钉机器人每分钟最多 20 条，请稍后再试")
        state.save()
    return {"ok": True}


def tick(
    now: datetime | None = None,
    *,
    state: PushState | None = None,
    opener=None,
    trading: bool | None = None,
    hot_loader=None,
    abnormal_loader=None,
    t_loader=None,
) -> list[str]:
    """到点或触发时各发一条。总开关或该类关闭时什么都不做。"""
    if not push_master_enabled():
        return []
    moment = now or _now()
    state = state or get_state()
    sent: list[str] = []
    with state._lock:
        if push_type_enabled("hot") and _push_hot(moment, state, opener, trading, hot_loader):
            sent.append("hot")
        if push_type_enabled("abnormal") and _push_edges(
            moment, state, opener, trading, "abnormal", abnormal_loader,
        ):
            sent.append("abnormal")
        if push_type_enabled("t_trade") and _push_edges(
            moment, state, opener, trading, "t_trade", t_loader,
        ):
            sent.append("t_trade")
        state.save()
    return sent


def _open_slot(now: datetime, state: PushState, day: str) -> str | None:
    if in_slot(now, premarket_at()) and f"{day}:pre" not in state.data["slots"]:
        return "pre"
    if in_slot(now, postclose_at()) and f"{day}:post" not in state.data["slots"]:
        return "post"
    return None


def _coerce_hot_load(loaded) -> tuple[list[dict], str | None]:
    """测试直接给行列表；正式加载给带 status 的字典。"""
    if isinstance(loaded, dict):
        rows = loaded.get("rows") or []
        status = loaded.get("status")
        return list(rows), (str(status) if status else None)
    if not loaded:
        return [], None
    return list(loaded), None


def _hot_load_usable(rows: list[dict], status: str | None) -> bool:
    """空结果、正在计算、或缺事件的半成品都不能覆盖上一轮基线。

    重启后事件快照还在算时，板块/个股/ETF 仍可能从资讯库读出来。把这份半截
    或空列表存进去之后，`[]` 不是 None，下一轮完整榜会整批变成「新进」。
    """
    if status in {"computing", "partial"}:
        return False
    return bool(rows)


def _store_hot_baseline(
    state: PushState,
    *,
    stamp: float,
    snapshot: list[dict],
    day: str,
    view: str,
    digest: str | None = None,
    slot: str | None = None,
) -> None:
    state.data["last_hot_check"] = stamp
    state.data["hot_snapshot"] = snapshot
    state.data["hot_rules_version"] = HOT_RULES_VERSION
    state.data["hot_day"] = day
    state.data["hot_view"] = view
    if digest is not None:
        state.data["hot_digest"] = digest
    if slot:
        state.data["slots"][f"{day}:{slot}"] = stamp


def _push_hot(now: datetime, state: PushState, opener, trading: bool | None, loader) -> bool:
    open_day = _is_trading(now) if trading is None else trading
    if not open_day:
        return False
    stamp = now.timestamp()
    day = now.date().isoformat()
    slot = _open_slot(now, state, day)
    change_due = _due(state, "last_hot_check", stamp, HOT_CHECK_S)
    if slot is None and not change_due:
        return False
    if slot is None and state.cooling("hot", stamp):
        state.data["last_hot_check"] = stamp
        return False
    rows, status = _coerce_hot_load((loader or _load_hot)())
    if not _hot_load_usable(rows, status):
        logger.info(
            "热点快照未就绪（%s，%s 条），保留上一轮基线",
            status or "空",
            len(rows),
        )
        state.data["last_hot_check"] = stamp
        return False
    snapshot = _snapshot_rows(rows)
    previous = state.data.get("hot_snapshot")
    if previous is not None and state.data.get("hot_day") != day:
        previous = None
    # 计分/准入口径升级后，上一轮快照的分数是新口径算不出来的。版本不匹配时
    # 只建基线不比较 —— 否则部署后第一轮会把口径差（importance 20→130 就是
    # 6.5 倍跳变）全报成「升温」（PR #97 评审）。
    if previous is not None and state.data.get("hot_rules_version") != HOT_RULES_VERSION:
        previous = None
    # 15:00 榜单从「当天」切到「下一场」，9:25 再丢掉上一场盘中。名次会整批换掉，
    # 不能把这次切换报成一堆新热点。展示日或带上的开市日变了就只建基线。
    view = display_view_key(now)
    if previous is not None and state.data.get("hot_view") != view:
        previous = None
    verified_only = push_type_saved("hot_verified")
    eligible = _eligible_hot_rows(snapshot, verified_only=verified_only)
    eligible_prev = _eligible_hot_rows(previous, verified_only=verified_only)
    heading = None
    changed: list[dict] = []
    if slot == "pre":
        heading = "【热点候选】盘前"
    elif slot == "post":
        heading = "【热点候选】收盘"
    elif change_due and not state.cooling("hot", stamp):
        changed = hot_material(
            eligible_prev, eligible or [], top_n=top_n(), min_stories=min_stories(), jump=score_jump(),
        )
        if changed:
            heading = "【热点候选】变化"
    if heading is None:
        _store_hot_baseline(state, stamp=stamp, snapshot=snapshot, day=day, view=view)
        return False
    new_count = sum(1 for item in changed if item.get("reason") == "new")
    if new_count > HOT_NEW_BURST:
        logger.info(
            "热点候选一次新进 %s 条，超过 %s，只更新基线",
            new_count,
            HOT_NEW_BURST,
        )
        _store_hot_baseline(state, stamp=stamp, snapshot=snapshot, day=day, view=view)
        return False
    limit = top_n()
    events = [row for row in snapshot if row["kind"] == "event"][:limit]
    if verified_only:
        events = [
            row for row in events
            if event_is_verified(row.get("confirmation") if isinstance(row.get("confirmation"), dict) else None)
        ]
    sectors = [row for row in snapshot if row["kind"] == "sector"][:limit]
    stocks = [row for row in snapshot if row["kind"] == "stock"][:limit]
    etfs = [row for row in snapshot if row["kind"] == "etf"][:limit]
    if loader is None:
        _attach_refs(sectors, stocks, events, etfs)
    packed = format_hot_markdown(
        sectors, stocks, heading=heading, events=events, etfs=etfs, changed=changed or None,
    )
    if packed is None:
        state.data["last_hot_check"] = stamp
        return False
    digest = _hot_digest(day, packed[1])
    if state.data.get("hot_digest") == digest:
        logger.info("钉钉热点候选内容与上次相同，跳过 %s", heading)
        _store_hot_baseline(
            state, stamp=stamp, snapshot=snapshot, day=day, view=view, slot=slot,
        )
        return False
    if not _send("hot", packed[0], packed[1], opener, stamp, state):
        return False
    logger.info(
        "钉钉热点候选已推送 %s %s %s",
        now.isoformat(timespec="seconds"),
        heading,
        "、".join(_hot_names(changed, events, sectors, stocks, etfs)),
    )
    _store_hot_baseline(
        state, stamp=stamp, snapshot=snapshot, day=day, view=view, digest=digest, slot=slot,
    )
    state.cool_until("hot", stamp, hot_cooldown_s())
    return True


def _push_edges(now, state, opener, trading, kind: str, loader) -> bool:
    open_day = _is_trading(now) if trading is None else trading
    spans = ABNORMAL_SPANS if kind == "abnormal" else T_SPANS
    if not open_day or not in_spans(now, spans):
        return False
    stamp = now.timestamp()
    check_key = f"last_{kind}_check"
    if not _due(state, check_key, stamp, INTRADAY_CHECK_S):
        return False
    state.data[check_key] = stamp
    day = now.date().isoformat()
    loaded = (loader or (_load_abnormal if kind == "abnormal" else _load_t))()
    current, pct, names, meta = _split_loader(loaded)
    previous = _edge_map(state, kind, day)
    entered = new_edges(previous, current)
    if kind == "t_trade" and meta is not None:
        # 分钟路径(批次 20261030gtedge): 边沿本身已由 `minute_edge_events` 按「相对最近
        # 一根旧 bar 的差分 + bar 根数冷却」算出, 本轮只把「还没处理过的分钟」交出去。
        # 这里**不再**套 `new_edges` 的「第一次见到只建基线」—— 那会让重启后的第一轮静默
        # 丢掉一整轮; 只建基线由 `minute_edge_events` 的「第一根不触发」承担。
        entered = _t_minute_entries(previous, current, meta)
    if kind == "t_trade":
        old_pct = state.data.get("t_pct") if state.data.get("t_pct_day") == day else {}
        entered = filter_prev_close(entered, old_pct or {}, pct)
        entered = _filter_range_rules(entered, now, state, day)
    fresh = [
        (symbol, reason)
        for symbol, reason in entered
        if not state.cooling(f"{kind}:{symbol}:{reason}", stamp)
    ]
    if not fresh or (kind == "t_trade" and _t_daily_full(state, day)):
        _store_edges(state, kind, day, current)
        if kind == "t_trade":
            state.data["t_pct"] = pct
            state.data["t_pct_day"] = day
            _remember_minute_progress(day, meta)
        return False
    detail_of = _detail_lookup(kind, fresh)
    quote_of = _quote_lookup(kind, fresh)
    linked = _verified_event_names() if kind == "abnormal" and push_type_saved("abnormal_verified") else {}
    rows = []
    for symbol, reason in fresh[:15]:
        detail = detail_of.get((symbol, reason), "")
        event_name = linked.get(symbol)
        if event_name:
            detail = f"{detail} · {event_name}" if detail else event_name
        row = {
            "symbol": symbol,
            "name": names.get(symbol) or symbol,
            "detail": detail,
            "reason": reason,
        }
        # 报价只给做T文案用; 异动文案不读这几项, 行为与改动前逐字节一致。
        if kind == "t_trade":
            row.update(quote_of.get((symbol, reason)) or {})
            # 分钟路径: 边沿所在那根 bar 的价格先垫底, 报价取不到时文案仍有个真数
            # (不是占位、不是 0.00); 拿得到报价就用报价, 与旧行为一致。
            edge = (meta or {}).get((symbol, reason)) or {}
            if row.get("price") is None and edge.get("price") is not None:
                row["price"] = edge["price"]
        rows.append(row)
    packed = format_symbol_markdown(kind, rows, now=now if kind == "t_trade" else None)
    if packed is None or not _send(kind, packed[0], packed[1], opener, stamp, state):
        return False
    _store_edges(state, kind, day, current)
    if kind == "t_trade":
        state.data["t_pct"] = pct
        state.data["t_pct_day"] = day
    for symbol, reason in fresh:
        state.cool_until(f"{kind}:{symbol}:{reason}", stamp, _cooldown_seconds(kind, reason))
    if kind == "t_trade":
        _mark_t_sent(state, day, fresh)
        _remember_minute_progress(day, meta)
    return True


def _t_minute_entries(previous, current, meta) -> list[tuple[str, str]]:
    """分钟路径本轮要交出去的 `(symbol, reason)`。

    ## 与 `new_edges` 的差别, 以及为什么这个差别是必要的

    `new_edges` 在 `previous is None`(当天第一次见到)时返回 `[]` —— 对**报价快照**是
    对的: 开机第一轮看到「现在是 below_vwap」, 并不知道它是什么时候开始的, 推出去
    等于补发一条不知道多旧的提醒。但分钟路径**知道**边沿发生在哪一根 bar, 而且
    `minute_edge_events` 已经把「第一根不触发」处理掉了。此时再用 `previous is None`
    一刀切, 会让进程在 10:03 重启后把 10:01 那根 already 收齐的边沿静默丢掉 ——
    那正是「图有钉无」的一种形态。

    所以这里按**分钟戳**判新旧: 只交「meta 里那一根 bar 的时间戳还没处理过」的边沿。
    冷却 / 日封顶 / 区间规则 / `filter_prev_close` 一律仍在调用方照旧执行, 值未放宽。

    `current[symbol]` 只含本轮新边沿(不是当日累计集合), 因此 `previous` 不参与筛选;
    保留该参数只为签名可读与将来并列过渡时对齐。
    """
    del previous  # 见 docstring: 分钟路径按戳判新旧, 不按「当天是否第一次见到」
    found: list[tuple[str, str]] = []
    for symbol, reasons in current.items():
        for reason in sorted(reasons):
            edge = meta.get((symbol, reason)) or {}
            if not str(edge.get("time") or ""):
                continue
            found.append((symbol, reason))
    return found


def _remember_minute_progress(day: str, meta) -> None:
    """把本轮交出去的分钟戳记进进程内去重表。**不落盘、不参与判定。**"""
    if not meta:
        return
    for (symbol, _reason), edge in meta.items():
        stamp = str(edge.get("time") or "")
        if stamp:
            _remember_stamps(symbol, date.fromisoformat(day), {stamp})


def _range_quiet(now: datetime) -> bool:
    """开盘后 30 分钟内，日内高低还没拉开，贴近高低几乎总会成立。"""
    clock = now.timetz().replace(tzinfo=None)
    return clock < RANGE_QUIET_UNTIL


def _filter_range_rules(entered, now: datetime, state: PushState, day: str):
    quiet = _range_quiet(now)
    once = _flag_on("news_push_t_range_once_per_day")
    sent = _range_sent(state, day) if once else set()
    kept = []
    for symbol, reason in entered:
        if reason in RANGE_RULES and (quiet or symbol in sent):
            continue
        kept.append((symbol, reason))
    return kept


def _range_sent(state: PushState, day: str) -> set[str]:
    if state.data.get("t_range_day") != day:
        return set()
    return set(state.data.get("t_range_sent") or [])


def _t_daily_full(state: PushState, day: str) -> bool:
    cap = t_daily_cap()
    if cap is None:
        return False
    if state.data.get("t_daily_day") != day:
        return False
    return int(state.data.get("t_daily_count") or 0) >= cap


def _mark_t_sent(state: PushState, day: str, fresh) -> None:
    if state.data.get("t_daily_day") != day:
        state.data["t_daily_day"] = day
        state.data["t_daily_count"] = 0
    state.data["t_daily_count"] = int(state.data.get("t_daily_count") or 0) + 1
    ranged = [symbol for symbol, reason in fresh if reason in RANGE_RULES]
    if not ranged:
        return
    if state.data.get("t_range_day") != day:
        state.data["t_range_day"] = day
        state.data["t_range_sent"] = []
    sent = list(state.data.get("t_range_sent") or [])
    for symbol in ranged:
        if symbol not in sent:
            sent.append(symbol)
    state.data["t_range_sent"] = sent


def _split_loader(loaded) -> tuple[dict, dict, dict, dict | None]:
    """拆 loader 返回值。**2/3/4 项都接受**, 4 项时最后一项是文案用的报价行。

    保留 2/3 项兼容是刻意的: 若干测例与夹具用 `lambda: (current, pct)` 直接注入,
    强制改成 4 项会让它们全红, 而它们本来就在测与文案无关的东西。

    第 4 个返回值(批次 20261030gtedge)是分钟边沿的 meta: `{(symbol, reason): {...}}`,
    挂着该边沿的分钟戳与价格,**没有就返回 `None`**。它不改变前三个返回值, 也不改变
    任何既有 loader 协议 —— `_load_t` 返回的 `TMinuteEdges` 把 meta 挂在 `.meta` 上,
    这里认出来就一并交回; 夹具给的普通 dict / 普通元组没有这个属性, 于是 meta 为
    `None`, 行为与改动前逐字节一致。
    """
    meta = getattr(loaded, "meta", None)
    if meta is None and loaded:
        meta = getattr(loaded[0], "_t_edge_meta", None)
    if len(loaded) == 3:
        current, pct, names = loaded
        return current, pct or {}, names or {}, meta
    current, pct = loaded[0], loaded[1]
    names = loaded[2] if len(loaded) >= 3 else {}
    return current, pct or {}, names or {}, meta


def _detail_lookup(kind: str, fresh) -> dict:
    labels = SIGNAL_LABELS if kind == "abnormal" else T_LABELS
    return {(symbol, reason): labels.get(reason, reason) for symbol, reason in fresh}


def _quote_lookup(kind: str, fresh) -> dict:
    """做T文案用的报价快照: `{(symbol, reason): {price, vwap, high, low}}`。

    ## 只为文案供数, 不参与任何判定

    边沿成立、`filter_prev_close`、`_filter_range_rules`、冷却、日封顶
    **全部在拿到这里的数据之前就已经跑完**。这里只是把「已经决定要推的那几条」
    的数字捞出来贴上, 让收到提醒的人不必再翻行情。

    因此本函数**不改变**任何一条会不会推、推几次的结论; 它只影响文案长什么样。

    数据来源就是 `_load_t()` 已经抓过的那批行(经 `_split_loader` 拆出来的
    `current`)。**不额外发请求**: 为了文案再打一轮行情, 既慢又可能在两次快照之间
    拿到不一致的价格。均价按 `amount / (volume * 100)` 现算, 与 `t_conditions`
    同一口径(volume 手 → 股)。
    """
    if kind != "t_trade" or not fresh:
        return {}
    symbols = sorted({symbol for symbol, _reason in fresh})
    try:
        raw = _live_quote_snapshot(symbols)
    except Exception:  # noqa: BLE001
        # 文案是锦上添花: 取数失败最多让文案少几个数字, **不能**让一条本该发出的
        # 提醒发不出去。这里吞掉, 由 `_live_quote_snapshot` 之外的调用方保证不重试。
        logger.warning("做T文案取报价失败, 本次不带报价", exc_info=True)
        raw = {}
    out: dict[tuple[str, str], dict] = {}
    for symbol, reason in fresh:
        row = raw.get(symbol) or {}
        price = _num(row.get("close"))
        high = _num(row.get("high"))
        low = _num(row.get("low"))
        volume = _num(row.get("volume"))
        amount = _num(row.get("amount"))
        vwap = None
        if volume and amount and volume > 0:
            candidate = amount / (volume * 100.0)
            if candidate > 0 and not vwap_outside_day_range(candidate, low, high):
                vwap = candidate
        out[(symbol, reason)] = {
            "price": price,
            "vwap": vwap,
            "high": high,
            "low": low,
        }
    return out


def _load_hot() -> dict:
    """带上事件快照 status。正在计算时板块候选仍可能有数，调用方不能拿去当基线。"""
    from app.news.service import hot_candidates, hot_event_listing
    listing = hot_event_listing(limit=top_n())
    raw_status = listing.get("status")
    status = str(raw_status) if raw_status else None
    rows = []
    for event in listing["events"]:
        rows.append({
            "kind": "event",
            "key": event["key"],
            "name": event["name"],
            "score": event.get("score", event["heat"]),
            "story_count": event["mentions"],
            "source_count": event["source_count"],
            "growth": 0,
            "first_seen": event["first_seen"],
            "concepts": event["concepts"],
            "headline": event["headline"],
            "subtitle": event.get("subtitle") or "",
            "category": event.get("category") or "",
            "direction": event.get("direction") or "",
            "importance": event.get("importance") or "",
            "confirmation": event.get("confirmation") or {},
        })
    for kind in ("sector", "stock", "etf"):
        for item in hot_candidates(kind=kind, limit=top_n()):
            rows.append({
                "kind": item.kind,
                "key": item.key,
                "name": item.name,
                "score": item.score,
                "story_count": item.story_count,
                "sources": list(item.sources),
                "growth": item.growth,
            })
    settled = status in {"ready", "stale"}
    has_events = any(row["kind"] == "event" for row in rows)
    # 事件列表还没落地时，后面的板块/个股/ETF 只是半截榜。
    if status == "computing" or (not settled and not has_events):
        marked = "computing" if status == "computing" else "partial"
        return {"rows": rows, "status": marked}
    return {"rows": rows, "status": status or "ready"}


def _attach_refs(
    sectors: list[dict],
    stocks: list[dict],
    events: list[dict] | None = None,
    etfs: list[dict] | None = None,
) -> None:
    from app.news.service import event_detail, hot_messages
    for row in [*(events or []), *sectors, *stocks, *(etfs or [])]:
        try:
            if row.get("kind") == "event":
                messages = event_detail(str(row.get("key") or ""), limit=8)["items"]
            else:
                messages = hot_messages(row["kind"], row["key"], limit=8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取候选出处失败 %s: %s", row.get("key"), exc)
            messages = []
        row["refs"] = pick_refs(messages)


def _load_abnormal() -> tuple[dict[str, set[str]], dict, dict]:
    from app.news.service import hot_candidates
    from app.services.watchlist import list_symbols

    symbols = [str(row.get("symbol") or "") for row in list_symbols()]
    symbols = [item for item in symbols if item]
    if _flag_on("news_push_abnormal_include_hot"):
        for item in hot_candidates(kind="stock", limit=top_n()):
            if item.key not in symbols:
                symbols.append(item.key)
    symbols = symbols[:ABNORMAL_CAP]
    if not symbols:
        return {}, {}, {}
    current: dict[str, set[str]] = {}
    names = {}
    for row in _live_rows(symbols):
        symbol = str(row.get("symbol") or "")
        signals = abnormal_signals(row) & ABNORMAL_SIGNALS
        if symbol and signals:
            current[symbol] = signals
            names[symbol] = str(row.get("name") or symbol)
    return current, {}, names


def _load_t() -> tuple[dict[str, set[str]], dict, dict]:
    """做T候选: `(边沿集合, 涨跌幅, 名称)` —— **边上挂分钟戳**。

    ## 批次 20261030gtedge: 边沿源换成分钟 bar(与图同构)

    **图有钉无的根因是边沿源不同构**, 不是阈值不一致:

    - 图 = `replay_t_marks`(分钟序列 → `session_signal_sets` → 逐根差分);
    - 旧钉 = `_live_rows` 报价快照 @ `INTRADAY_CHECK_S=180`。

    报价快照每 180s 取一次日累计高低/均价, 一分钟级的窗(见 688008 10:06 那根
    `below_vwap`)在两次快照之间就已经开合完毕 —— 于是「图上有钉, 钉上没推」。

    本函数改为: 用与图**同一个** `minute_edge_events` 引擎, 在当日分钟 bar 上算出
    「相对已处理分钟的新边沿」, 只把新边沿交出去。判定、阈值、差分全是共用的那一份
    (`session_signal_sets` 同一口径), 没有第二套边沿规则。

    ### 驱动时机

    每轮按 `_due(INTRADAY_CHECK_S)` 的轮询上限进入, 但**进入之后处理自上次以来所有
    新收齐的分钟**, 而不是只看「当前一分钟」—— 于是 180s 的轮询间隔不会漏掉中间任何
    一根已收齐的 bar。缩 `INTRADAY_CHECK_S` **不是**本拍手段, 也不足以单独修好它。

    ### 数据源(N6 · 写死)

    与分时图同构的本地分钟仓: 优先 `repo.get_minute_batch`(多标的一次 scan), 不可用
    时逐只 `repo.get_minute`; bars 形态与图一致(`sort("datetime").to_dicts()`)。

    ### fail-closed(写死)

    repo 不可得 / `get_minute*` 抛错 / 空帧 / bars 不足 2 根(做不了「第一根不触发」
    之后的差分) / 缺昨收 → **该标的本轮不进入结果**, 记 warning。
    **禁止**退回「用单点报价冒充分钟序列」或「乱造 bar」当作同构交付: 那种回退会让
    本轮看起来有输出, 而输出与图仍然不同构 —— 比不输出更难查。

    ### 返回值的第一个字典**带 `(symbol, reason)` 内层值**

    外层键仍是 `symbol`, 值仍是 `set[str]`(既有调用方与测例的形状零改); 内层每个
    reason 都在 `_t_edge_meta[(symbol, reason)]` 里挂着它的分钟戳与价格。`_split_loader`
    认出这个属性后把 meta 一并交回 `_push_edges`, 由它决定「这一根之前是不是已经推过」。

    仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。
    """
    from app.services.trade_marks import T_COOLDOWN_MIN, minute_edge_events
    from app.services.watchlist import list_symbols

    watchlist = [str(row.get("symbol") or "") for row in list_symbols()]
    positions = _position_symbols() if _flag_on("news_push_t_include_positions") else []
    symbols = t_universe(watchlist, positions, include_positions=bool(positions), cap=T_CAP)
    if not symbols:
        return {}, {}, {}
    current: dict[str, set[str]] = {}
    pct: dict[str, float] = {}
    names = {}
    meta: dict[tuple[str, str], dict] = {}
    for symbol, bars, prev_close, name, up_to in _minute_bars_for_symbols(symbols):
        events = minute_edge_events(
            bars,
            symbol=symbol,
            prev_close=prev_close,
            cooldown_min=T_COOLDOWN_MIN,
            processed_upto=up_to,
        )
        reasons: set[str] = set()
        for event in events:
            reasons.add(event["reason"])
            meta[(symbol, event["reason"])] = event
        last_close = _num(bars[-1].get("close"))
        if last_close is not None and prev_close:
            pct[symbol] = last_close / prev_close - 1
        current[symbol] = reasons
        names[symbol] = name
    return TMinuteEdges(current, pct, names, meta)


def _repo_or_none():
    try:
        from app.news.service import _repo
        return _repo()
    except Exception:  # noqa: BLE001
        return None


def _minute_bars_for_symbols(symbols: list[str]) -> list[tuple]:
    """按 N6 数据源取当日分钟 bars。**失败即跳过该标的**, 不抛、不造假 bar。

    返回 `[(symbol, bars, prev_close, name, processed_upto), ...]`(可为空列表);
    `processed_upto` 是本进程内已经处理过的 bar 时间戳(按 (symbol, day) 记在本模块字典里),
    每轮重放当日全部 bars 靠它避免重复报同一根。

    昨收口径与分时图同一套: `minute_prev_close` + `minute_reads_are_adjusted`, 数据源
    是 `_prior_levels`(live_agg 前 59 日)。**缺昨收的标的本轮不判** —— 拿脏参照算出来
    的偏离不比不判更安全。
    """
    from app.market_time import cn_today

    # 观察钩子(仅供测): 拿到宇宙后**先**回调一次, 便于核 `t_universe` 口径未被改动。
    # 生产上恒为 None, 不产生任何开销与行为差异。
    if _T_MINUTE_PROBE is not None:
        try:
            _T_MINUTE_PROBE(list(symbols))
        except Exception:  # noqa: BLE001
            logger.debug("做T 分钟边沿: 观察钩子失败", exc_info=True)

    repo = _repo_or_none()
    if repo is None:
        logger.warning("做T 分钟边沿: 仓库未就绪, %d 只标的本轮不判", len(symbols))
        return []
    day = cn_today()
    frames = _minute_frames(repo, symbols, day)
    if not frames:
        return []
    levels = _prior_levels(symbols)
    reads_adjusted = _minute_reads_adjusted(repo)
    found: list[tuple] = []
    for symbol in symbols:
        bars = frames.get(symbol) or []
        if not bars:
            continue
        if len(bars) < 2:
            logger.info("做T 分钟边沿: %s 当日 bars %d 根, 做不了差分, 本轮不判", symbol, len(bars))
            continue
        level = levels.get(symbol) or {}
        prev_close = _aligned_prev_close(level, reads_adjusted)
        if prev_close is None:
            logger.info("做T 分钟边沿: %s 缺昨收, 本轮不判(不拿脏参照算偏离)", symbol)
            continue
        found.append((
            symbol,
            bars,
            prev_close,
            str(level.get("name") or symbol),
            _processed_stamps(symbol, day),
        ))
    return found


def _minute_frames(repo, symbols: list[str], day) -> dict[str, list[dict]]:
    """当日分钟 bars, 按标的分组: 优先批量, 逐只回退。整批失败不拖累其它标的。

    返回的已是 `list[dict]`(与图同一排序口径), 不再把帧往外传 —— 帧是 polars 的
    实现细节, 而调用方只需要 bars。
    """
    frames: dict[str, list[dict]] = {}
    batch = getattr(repo, "get_minute_batch", None)
    if callable(batch):
        try:
            frame = batch(list(symbols), day, "stock")
        except Exception:  # noqa: BLE001
            logger.warning("做T 分钟边沿: 批量分钟读取失败, 退回逐只", exc_info=True)
            frame = None
        bars_by_symbol = _bars_by_symbol(frame)
        if bars_by_symbol:
            return bars_by_symbol
    single = getattr(repo, "get_minute", None)
    if not callable(single):
        return frames
    for symbol in symbols:
        try:
            frame = single(symbol, day, "stock")
        except Exception:  # noqa: BLE001
            logger.warning("做T 分钟边沿: %s 分钟读取失败, 本轮不判", symbol, exc_info=True)
            continue
        bars = _bars_of(frame)
        if bars:
            frames[symbol] = bars
    return frames


def _bars_by_symbol(frame) -> dict[str, list[dict]]:
    """批量帧 → `{symbol: bars}`。帧空 / 缺 symbol 列 / 切分失败都返回 `{}`。"""
    if frame is None:
        return {}
    try:
        if frame.is_empty() or "symbol" not in frame.columns:
            return {}
        parts = frame.partition_by("symbol", as_dict=True)
    except Exception:  # noqa: BLE001
        logger.warning("做T 分钟边沿: 批量帧按标的切分失败, 退回逐只", exc_info=True)
        return {}
    out: dict[str, list[dict]] = {}
    for key, part in parts.items():
        symbol = key[0] if isinstance(key, tuple) else key
        bars = _bars_of(part)
        if bars:
            out[str(symbol)] = bars
    return out


def _bars_of(frame) -> list[dict]:
    """帧 → bars(与 `trade_marks._t_trade_block` 同一排序口径)。"""
    if frame is None:
        return []
    try:
        if frame.is_empty():
            return []
        return frame.sort("datetime").to_dicts() if "datetime" in frame.columns else frame.to_dicts()
    except Exception:  # noqa: BLE001
        return []


def _minute_reads_adjusted(repo) -> bool:
    from app.services.price_basis import minute_reads_are_adjusted, repo_data_dir

    try:
        return bool(minute_reads_are_adjusted(repo_data_dir(repo)))
    except Exception:  # noqa: BLE001
        return False


def _aligned_prev_close(level: dict, reads_adjusted: bool) -> float | None:
    """昨收对齐到分钟读取口径。与分时图同一函数, 不另算一套。"""
    from app.services.price_basis import minute_prev_close

    prev = _num(level.get("prev_close"))
    if prev is None or prev <= 0:
        return None
    try:
        return minute_prev_close(prev, reads_adjusted=reads_adjusted)
    except Exception:  # noqa: BLE001
        return None


def _processed_stamps(symbol: str, day) -> set[str]:
    """本进程内该标的当日已处理过的 bar 时间戳。**只用于去重, 不参与判定。**"""
    key = (symbol, day.isoformat())
    return set(_T_MINUTE_SEEN.get(key) or ())


def _remember_stamps(symbol: str, day, stamps: set[str]) -> None:
    if not stamps:
        return
    key = (symbol, day.isoformat())
    _T_MINUTE_SEEN[key] = set(_T_MINUTE_SEEN.get(key) or ()) | set(stamps)
    if len(_T_MINUTE_SEEN) > 512:
        for stale in list(_T_MINUTE_SEEN)[: len(_T_MINUTE_SEEN) - 512]:
            _T_MINUTE_SEEN.pop(stale, None)


def _live_quote_snapshot(symbols: list[str]) -> dict[str, dict]:
    """按需给文案取报价行。**只在 `_push_edges` 已经决定要推之后调用。**

    失败一律返回 `{}`: 文案缺报价不该让一条本来该发的提醒发不出去。
    """
    if not symbols:
        return {}
    try:
        rows = _live_rows(symbols)
    except Exception:  # noqa: BLE001
        logger.warning("做T文案取报价失败, 本次只发不含报价的文案", exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for row in rows or []:
        symbol = str(row.get("symbol") or "")
        if symbol:
            out[symbol] = row
    return out


def _position_symbols() -> list[str]:
    from app.strategy import paper

    found: list[str] = []
    try:
        accounts = paper.list_accounts(settings.data_dir) or []
    except Exception:  # noqa: BLE001
        accounts = []
    if not accounts:
        accounts = [{"id": paper.DEFAULT_ACCOUNT_ID}]
    for account in accounts:
        account_id = account.get("id") or paper.DEFAULT_ACCOUNT_ID
        try:
            positions = paper.load_positions(settings.data_dir, account_id)
        except Exception:  # noqa: BLE001
            continue
        for symbol, pos in positions.items():
            if int(pos.get("qty") or 0) > 0 and symbol not in found:
                found.append(symbol)
    return found


def _live_rows(symbols: list[str]) -> list[dict]:
    """实时报价。缓存还新鲜就用缓存，否则按批量行情接口补拉，不读盘后信号列。"""
    cached = _cached_live_rows(symbols)
    rows = cached if cached is not None else _fetch_quote_batch(symbols)
    _attach_prior(rows, symbols)
    return rows


def _cached_live_rows(symbols: list[str]) -> list[dict] | None:
    try:
        from app.main import app
        from app.market_time import cn_today
        service = getattr(getattr(app, "state", None), "quote_service", None)
    except Exception:  # noqa: BLE001
        return None
    if service is None:
        return None
    try:
        status = service.status()
    except Exception:  # noqa: BLE001
        return None
    if not status.get("enabled"):
        return None
    age = status.get("quote_age_ms")
    if not isinstance(age, (int, float)) or age < 0 or age > QUOTE_FRESH_S * 1000:
        return None
    try:
        frame, cache_date = service.get_enriched_today()
    except Exception:  # noqa: BLE001
        return None
    if cache_date != cn_today() or frame is None or frame.is_empty() or "symbol" not in frame.columns:
        return None
    import polars as pl
    frame = frame.filter(pl.col("symbol").is_in(symbols))
    cols = [
        name for name in (
            "symbol", "name", "close", "open", "high", "low", "volume", "amount",
            "prev_close", "raw_close", "raw_high", "raw_low",
        )
        if name in frame.columns
    ]
    return [_cache_quote_view(row) for row in frame.select(cols).to_dicts()]


def _fetch_quote_batch(symbols: list[str]) -> list[dict]:
    try:
        from app.main import app
        from app.tickflow.capabilities import Cap
        from app.tickflow.client import get_client
        from app.tickflow.rate_limits import resolve_limit
    except Exception as exc:  # noqa: BLE001
        logger.info("实时行情客户端不可用: %s", exc)
        return []
    client = get_client()
    if client is None:
        return []
    batch = 80
    capset = getattr(getattr(app, "state", None), "capabilities", None)
    if capset is not None and capset.has(Cap.QUOTE_BATCH):
        batch = resolve_limit(capset, Cap.QUOTE_BATCH, default_batch=80).batch or 80
    elif capset is not None and not capset.has(Cap.QUOTE_BY_SYMBOL):
        logger.info("当前档位没有批量实时行情，异动和做T本轮跳过")
        return []
    batch = max(1, min(int(batch), 500))
    rows: list[dict] = []
    for start in range(0, len(symbols), batch):
        chunk = symbols[start:start + batch]
        try:
            payload = client.quotes.get(symbols=chunk) or []
        except Exception as exc:  # noqa: BLE001
            logger.info("实时行情批量失败 %s 只: %s", len(chunk), exc)
            break
        for item in payload:
            if isinstance(item, dict):
                rows.append(_normalize_quote(item))
    return rows


def _cache_quote_view(row: dict) -> dict:
    """缓存里 close 是前复权，raw_close 是原始价。做T和涨跌停用原始价。"""
    qfq_close = _num(row.get("close"))
    raw_close = _num(row.get("raw_close"))
    adjusted = raw_close is not None and qfq_close not in (None, 0) and raw_close > 0
    factor = (qfq_close / raw_close) if adjusted else None

    def raw_of(raw_key: str, qfq_key: str) -> float | None:
        raw = _num(row.get(raw_key))
        if raw is not None:
            return raw
        qfq = _num(row.get(qfq_key))
        if qfq is None:
            return None
        if factor:
            return qfq / factor
        return qfq

    prev = _num(row.get("prev_close"))
    if prev is not None and factor:
        prev = prev / factor
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "qfq_close": qfq_close,
        "close": raw_close if raw_close is not None else qfq_close,
        "open": raw_of("raw_open", "open"),
        "high": raw_of("raw_high", "high"),
        "low": raw_of("raw_low", "low"),
        "volume": row.get("volume"),
        "amount": row.get("amount"),
        "prev_close": prev,
    }


def _normalize_quote(item: dict) -> dict:
    ext = item.get("ext") if isinstance(item.get("ext"), dict) else {}
    return {
        "symbol": item.get("symbol"),
        "name": item.get("name") or ext.get("name"),
        "close": item.get("last_price", item.get("close")),
        "open": item.get("open"),
        "high": item.get("high"),
        "low": item.get("low"),
        "volume": item.get("volume"),
        "amount": item.get("amount"),
        "prev_close": item.get("prev_close") or ext.get("prev_close"),
    }


def _attach_prior(rows: list[dict], symbols: list[str]) -> None:
    levels = _prior_levels(symbols)
    for row in rows:
        level = levels.get(str(row.get("symbol") or "")) or {}
        if level.get("prior_high") is not None:
            row["prior_high"] = level["prior_high"]
        if level.get("prior_low") is not None:
            row["prior_low"] = level["prior_low"]
        factor = _num(level.get("adj_factor"))
        raw = _num(row.get("close"))
        if _num(row.get("qfq_close")) is None and raw and factor:
            row["qfq_close"] = raw * factor
        if not _num(row.get("prev_close")) and _num(level.get("prev_close")):
            prev = _num(level["prev_close"])
            row["prev_close"] = prev / factor if factor else prev


def _prior_levels(symbols: list[str]) -> dict[str, dict]:
    """盘前 60 日收盘极值。来自 live_agg 的前 59 日，不含今天。"""
    try:
        from app.news.service import _repo
        repo = _repo()
        if repo is None:
            logger.warning("盘前价位不可用: 仓库未就绪, %d 只标的的 60 日新高/新低本轮不判", len(symbols))
            return {}
        frame = repo.get_live_agg()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "盘前价位读取失败, %d 只标的的 60 日新高/新低本轮不判: %s", len(symbols), exc,
        )
        return {}
    if frame is None or frame.is_empty() or "symbol" not in frame.columns:
        logger.warning(
            "盘前价位为空(表空或缺 symbol 列), %d 只标的的 60 日新高/新低本轮不判", len(symbols),
        )
        return {}
    if "_high_59d" not in frame.columns or "_low_59d" not in frame.columns:
        logger.warning(
            "盘前价位缺 _high_59d/_low_59d 列, %d 只标的的 60 日新高/新低本轮不判", len(symbols),
        )
        return {}
    import polars as pl
    frame = frame.filter(pl.col("symbol").is_in(symbols))
    cols = ["symbol", "_high_59d", "_low_59d"]
    if "close" in frame.columns:
        cols.append("close")
    if "_adj_factor" in frame.columns:
        cols.append("_adj_factor")
    found = {}
    for row in frame.select(cols).to_dicts():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        found[symbol] = {
            "prior_high": row.get("_high_59d"),
            "prior_low": row.get("_low_59d"),
            "prev_close": row.get("close"),
            "adj_factor": row.get("_adj_factor"),
        }
    return found


def _flag_on(field: str) -> bool:
    from app.news.config import _flag
    env = field.upper()
    return _flag(env) is True


def _is_trading(now: datetime) -> bool:
    from app.services.trading_day import is_trading_day
    verdict = is_trading_day(now)
    if verdict is None:
        return now.weekday() < 5
    return bool(verdict)


def _now() -> datetime:
    from app.market_time import cn_now
    return cn_now()

