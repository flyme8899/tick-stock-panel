"""轨①首见时间戳审计契约(批次 20261030ga2 · P-bss-a2-first-seen · -A-#11 A2)。

## 这个模块做什么

它**只钉语义**。给出一个写死的定义、一张来源表(接口位)、一条研究归档路径、一个时序
辅助函数, 以及一扇 A2 完成门 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE`(默认 `False`)。

它**不**计算首见、**不**读数据库、**不**写任何命中文件。真正的取数在信号层(E1/E2/E3),
那是 A2 之后的事 —— **数据层 ≺ 信号层**。

## 首见 = 我方首次可得, 不是发布时间

```
first_seen_at  ==  "我方第一次能拿到这条信息的时刻"
```

Asia/Shanghai, 可解析 ISO 串(秒级, 带 `+08:00`)。**优先源**是 `news_items.ingested_at`
(`backend/app/news/store.py`: 采集落盘时 `datetime.now(CN_TZ).isoformat(timespec="seconds")`
写入, 只在该 `(source, source_id)` 首次插入时落一次, 之后 `INSERT OR IGNORE` 不覆盖)。

**明确不是**下面三样 —— 它们各自都很像"首见", 但每一个都会把未来信息搬进过去:

| 冒充者 | 它其实是什么 | 为什么不能用 |
|---|---|---|
| `published_at` | 媒体/公告自己声明的**发布**时刻 | 发布 ≠ 可得。源站延迟推送、我们晚采几小时, 用 published 就等于提前知道 |
| HotEvents 展示串 `first_seen` | `hot_events.py` 里由簇内**最早 published** 格式化出来的展示字段 | 它名字叫 first_seen, 口径却是 published。拿它当 PIT 是把前视风险原样搬过来 |
| `SignalHit.asof` | 评估 / 数据时点(`hits_models.py`) | 它是"这条命中按哪个时点算出来的", 不是"我们什么时候第一次看到" |

三者的区别在**回测里会直接变成收益**: 用 published 做 PIT, 等于允许策略在消息还没被
采到时就下单。这不是精度问题, 是方向性偏差。

## 字段来源表(**接口位**, 本模块不实现取数)

本拍只留位置与口径, **不**写 E1/E2/E3 的信号逻辑与评价器(那是信号层)。

| 轨 | 对象 | 首见取什么 | 备注 |
|---|---|---|---|
| E1 | 隔夜热点 / 公告 | 采集入库戳(`news_items.ingested_at`) / 公告首次入库戳 | 主路径, 优先源已 PRESENT |
| E2 | 美股映射 | 映射源**首次可得**戳(另一张表) | 本拍只留接口位, 不建表 |
| E3 | 盘中突发 | 分钟 / 异动源首次可得戳 | **禁止**用 09:30 冒充 09:25 集合竞价定盘 |

E3 那条要单独说一句: 集合竞价 09:25 定盘, 但分钟的**首根 bar 时间戳是 09:30**。
若用 09:30 当"首见", 等于默认 09:25~09:30 这五分钟的信息我们在 09:30 才知道 ——
这与真实可得性不符, 且方向上偏保守得没有理由。取哪个戳由 A2 之后定, 本拍只钉"不许混"。

## 研究归档: 写这里, 不写 hits, 不写 shadow

```
<data_dir>/buy_sell_signals/research/first_seen/...
```

- **禁止**写 live `hits.json`(`hits_store.py` 那条写口)
- **禁止**写 shadow 分桶(`hits_shadow_store.py`, 三键路径)

理由: 研究样本是"我们在验证首见口径", 它不是产线命中、也不是影子命中。混进任一边都会
让"样本数够不够"这个判断被非本口径的条数污染 —— 而污染之后没人会发现, 因为文件是对的,
条数是错的。

## 时序: asof 不得早于首见

未来事件轨命中的 `asof` **不得早于**对应的 `first_seen_at`。缺首见 → **fail-closed**:
跳过, 或标 `first_seen_missing`。

**不得**用 `published_at` 顶上。顶上看起来"补全了数据", 实际是把前视风险重新引进来,
而且引入得无声无息 —— 缺首见至少还是个能被统计到的洞。

## A2 ≺ A3

A3 的 horizon 起点要锚在本拍定义的首见上。所以 A3 必须等 A2: 起点口径没定死之前算
horizon, 算出来的每个数都要重做。

## 本模块与影子闸的关系

`hits_shadow_store._require_shadow_hit` 会读本模块的 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE`:
`False` 时, `family == "minute_event"` 一律拒写影子分桶, 错误消息带
`event_track_shadow_blocked_until_a2`。这一扇门是**单一来源** —— 本模块就是那唯一的源,
别处不要另立一个同义常量。

翻 `True` ＝ A2 完成, **另口令 / 另验收**, 不在本拍。

## 这个模块不做什么

- **不**改 `BONFERRONI_M` / `BONFERRONI_SCHEME_ID`
- **不**翻两处 `*_TRIGGERING_ENABLED`
- **不**开 A1 / A3
- **不**实现 E1/E2/E3 信号定义或评价器
- **不**改 `hot_events.py`、不改 `hits_models.py` 的 `asof` 字段
- **不**读 env、**不**新增开关

## 合规

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Final

from ..market_time import CN_TZ

# ---------------------------------------------------------------- 闸门与指认

#: A2 完成门。**本拍不翻 True**。
#:
#: 单一来源: 影子写口的硬闸从本模块导入这一个符号, 不在别处另立同名常量。两处各写一份
#: 的后果不是"冗余", 而是"哪天只翻了一处", 于是闸和读数对不上 —— 那种状态下没人能说清
#: 事件轨到底能不能写。
EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE: Final = False

#: 影子闸的指认子串。错误消息里必须带它, 好让日志与测例能一眼认出"这是 A2 闸挡的",
#: 而不是某条别的原因导致的写失败。
EVENT_TRACK_SHADOW_BLOCK_TOKEN: Final = "event_track_shadow_blocked_until_a2"

#: 研究归档的相对路径形态(相对 `data_dir`)。
#:
#: 用 tuple 而不是字符串: 每一层是路径的一段, 拼接时不依赖分隔符, 也不会因为谁手滑
#: 写了个 `/` 或 `\` 就跑到别的盘符下面去。
RESEARCH_FIRST_SEEN_SUBDIR: Final = ("buy_sell_signals", "research", "first_seen")

# ---------------------------------------------------------------- 语义钉(可被断言)

#: 首见的定义。字面量写死, 因为它要被测例逐字断言 —— 一旦哪天有人把它改成
#: `"published_at"` 之类的近义词, 这里会红。
FIRST_SEEN_MEANING: Final = "my_first_available"

#: 首见**不是**什么。三个冒充者各自对应上表一行。
FIRST_SEEN_NOT: Final = frozenset(
    {
        "published_at",            # 媒体/公告自称的发布时刻
        "hot_events_first_seen",   # hot_events.py 由簇内最早 published 格式化
        "signal_hit_asof",         # SignalHit.asof, 评估时点
    }
)

#: 优先源。`news_items.ingested_at` 在 tip 上已 PRESENT(`news/store.py` L31)。
PREFERRED_SOURCE: Final = "news_items.ingested_at"

# ---------------------------------------------------------------- 时序辅助

#: 缺首见的原因码。
REASON_FIRST_SEEN_MISSING: Final = "first_seen_missing"

#: asof 早于首见的原因码。
REASON_ASOF_BEFORE_FIRST_SEEN: Final = "asof_before_first_seen"

#: 时间串不可解析的原因码。
REASON_UNPARSABLE: Final = "timestamp_unparsable"

#: 只认 ISO 日期 或 ISO 时间串。刻意不接受 `2026/10/20`、`10-20` 这类人写格式: 它们没有
#: 时区, 而"没有时区"在多数据中心采集里就是一个不存在的确定性。
_ISO_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_instant(value: str) -> datetime | None:
    """把 ISO 串解析成**带时区**的 datetime; 无时区的按 Asia/Shanghai 处理。

    为什么无时区也不报错: `asof` 在仓内既有写法里出现过裸日期(`2026-10-24`)。把它当
    Asia/Shanghai 的零点, 是这里最小心的解读 —— 若真按 UTC 解读, 会把首见往**早**推
    8 小时, 那正好是前视的方向。宁可保守。

    返回 `None` 表示不可解析(调用方据此 fail-closed)。
    """
    text = (value or "").strip()
    if not text:
        return None
    if _ISO_DATE_RE.match(text):
        try:
            return datetime.combine(
                date.fromisoformat(text),
                datetime.min.time(),
                tzinfo=CN_TZ,
            )
        except ValueError:
            return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 裸时间串 → 按 Asia/Shanghai 解读(见上: 宁可晚, 不提前)。
        return parsed.replace(tzinfo=CN_TZ)
    return parsed


def assert_asof_not_before_first_seen(
    asof: str,
    first_seen_at: str | None,
) -> str | None:
    """时序辅助: 检查 `asof` 是否早于首见。

    返回值是**原因码或 None**, 不是布尔 —— 抛异常会让调用方要么全部 try, 要么全部不
    try; 返回原因码则让调用方自己决定"跳过 / 打标 / 报错", 而缺首见与"早于首见"这两件
    事在统计上要分开计数(一个说明采集有洞, 一个说明时序算错了)。

    返回:

    - `None` —— `asof >= first_seen_at`, 通过
    - `"first_seen_missing"` —— 首见为空 / 全空白。**fail-closed**, 调用方须跳过或打标,
      不得用 `published_at` 顶上
    - `"asof_before_first_seen"` —— `asof` 早于首见, 属于前视
    - `"timestamp_unparsable"` —— 任一侧不可解析。同样 fail-closed: 解不开的时间串不能
      当成"大概没问题"放过去

    允许 `asof` 是日级(`2026-10-24`): 按当日 `00:00+08:00` 解读后比较。这意味着日级
    `asof` 对上同一天内更晚的首见会判 `asof_before_first_seen` —— 这是**故意偏保守**的:
    日级精度本就不足以证明"不早于", 宁可多拒一条, 也不放过一条前视。
    """
    if first_seen_at is None or not str(first_seen_at).strip():
        return REASON_FIRST_SEEN_MISSING

    seen_at = _parse_instant(str(first_seen_at))
    asof_at = _parse_instant(str(asof))
    if seen_at is None or asof_at is None:
        return REASON_UNPARSABLE

    if asof_at.astimezone(timezone.utc) < seen_at.astimezone(timezone.utc):
        return REASON_ASOF_BEFORE_FIRST_SEEN
    return None


def research_first_seen_dir(data_dir: str | None = None) -> str:
    """研究归档目录(字符串形态), 供调用方展示 / 落盘时拼路径。

    只拼路径, **不**建目录、**不**写文件 —— 本模块任何函数都不落盘。研究与产线的分界
    靠"谁写"来守, 不靠"谁有权限写"。
    """
    import os

    root = data_dir if data_dir is not None else os.path.join("data")
    return os.path.join(root, *RESEARCH_FIRST_SEEN_SUBDIR)


__all__ = [
    "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE",
    "EVENT_TRACK_SHADOW_BLOCK_TOKEN",
    "FIRST_SEEN_MEANING",
    "FIRST_SEEN_NOT",
    "PREFERRED_SOURCE",
    "REASON_ASOF_BEFORE_FIRST_SEEN",
    "REASON_FIRST_SEEN_MISSING",
    "REASON_UNPARSABLE",
    "RESEARCH_FIRST_SEEN_SUBDIR",
    "assert_asof_not_before_first_seen",
    "research_first_seen_dir",
]
