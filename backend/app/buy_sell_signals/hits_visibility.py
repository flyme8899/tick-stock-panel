"""命中可见性闸 + 范围过滤 + 响应组装。

## 闸在服务端, 不在前端

监控中心的买卖信号 tab 只该显示「已校准」的命中。这条判断必须在这里做完, 前端再做一遍
可以, 但**不能是唯一一道** —— 前端过滤只防手滑, 防不住直接调接口。

判定规则(`visible_hits`):

1. 命中 → 按 `signal_id` 查定义注册表**当前**那条(指针版优先);
2. 定义不存在 → 丢弃;
3. 定义 `status != "publishable"` → 丢弃(`candidate` / `observe` / `retired` /
   `data_abnormal` 全不可见);
4. `event_class == "limit_reopen"` → 丢弃(双保险);
5. 其余通过。

## 第 4 条为什么不冗余

状态机里 `limit_reopen` 已经有双重拦截、永远到不了 `publishable`。但这里仍然再拦一次,
理由是: 那条拦截靠的是 `state_machine.transition()` 的转移表, 而注册表是可以被外部
直接写 JSON 的(`DiskSignalRegistryStore.upsert` 也接受手工构造的记录)。一条被手改成
`publishable` 的 `limit_reopen` 定义, 在状态机之外是合法的。炸板/回封这类事件如果漏进来,
读者会把它当成"信号有效", 而它本质上只是一个"刚刚发生过的事实"。双重保险的成本是一行。

## `pending` 不洗白

命中记录里**没有** `calibrated` / `verified` 这类字段, 是刻意的: 没有字段就没有地方
把 `data_pending` 写成"已校准"。呈现层能说的最多是「暂无已校准的买卖信号」——
空就是空。

## 校准闸: AND, 不是 OR(批次 20261026gbsscalib 新增)

在既有四条闸**之后**再叠一条: 命中还必须有一条**校准通过登记**与它三键对齐
(`signal_id` + `definition_version` + `freq_lock_version`)。

        可见 ⟺ publishable ∧ ¬limit_reopen ∧ family 暴露 ∧ fund-scope ∧ calib passed

为什么是 AND 而不是"有其一即可": 原来的四条闸回答的是「这条命中**该不该**被发出来」
(定义敢不敢担保、事件类型允不允许、范围对不对)。校准闸回答的是另一个问题:
「这套口径**有没有**拿得出手的成绩」。两个问题都答"是"才该显示; 任一答不上来就不显示。
把它们做成 OR, 等于让"定义是 publishable"替"回测没过"背书 —— 而定义发布只是
一次人工审批动作, 它不产生任何关于收益/胜率的证据。

缺 `passed` 时与「没有任何命中」完全等价(`CALIB_NOT_PASSED`)。
`pending` / `failed` / 没有登记 / 登记读不出来 —— 四种情况同一个下场, 一条都不可见。
这不是"降级显示", 是**不可见**。

## 命中无权自称已校准

判断依据只有独立的 `calib.json`(经 `CalibIndex`)。命中记录里**没有**、也**禁止**加
`calibrated` 布尔字段: 一个可手改的 `true`, 从任何 diff 上都看不出来, 但会让所有闸
形同虚设。命中侧只带一个 `freq_lock_version` 标签(镜像值), 而标签本身不构成任何担保
—— 它只用来**参与对齐**, 对不上就不可见。

## 范围过滤(自选 ∪ 持仓)只作用于基本面

`daily_fundamental` 的命中是日频全市场量级, 不做范围过滤的话 tab 里会堆上千条与用户
无关的票。`minute_event` 的事件命中是稀疏的(一天几条到几十条), 过滤反而会让用户
看不到别处的异动 —— 而且事件命中本来就该在看到的那一刻是新鲜的。

**范围过滤不是权限**: 过了 publishable 闸的命中, 事件类一律可见; 基本面类只在自选/持仓内
可见。这是一条产品口径, 不是安全边界, 所以它放在可见性闸**之后**, 且明确可拆。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from .calib_store import CalibIndex
from .constants import EMPTY_HITS_MESSAGE, HITS_SCHEMA_VERSION, NAMESPACE
from .hits_models import SignalHit
from .models import SignalRecord

logger = logging.getLogger(__name__)

#: 首批对外的 family 白名单。
#:
#: `composite` 不在里面: 复合信号还没有任何一条走完验证, 给它挂一个永远是空的入口,
#: 除了暗示"这里以后会有东西"之外没有信息量。等有真命中再加。
EXPOSED_FAMILIES = ("minute_event", "daily_fundamental")


class VisibilityReject:
    """丢弃原因常量。用字符串而不是枚举: 它们只进日志和测试断言。"""

    DEFINITION_MISSING = "definition_missing"
    NOT_PUBLISHABLE = "definition_not_publishable"
    LIMIT_REOPEN = "limit_reopen_never_visible"
    FAMILY_NOT_EXPOSED = "family_not_exposed"
    UNKNOWN_FAMILY_FILTER = "unknown_family_filter"
    OUT_OF_SCOPE = "out_of_scope"
    CALIB_NOT_PASSED = "calib_not_passed"
    """没有与本命中三键对齐的 `passed` 校准登记(批次 20261026gbsscalib)。

    四条既有闸全过、但这一条拦下来时, 说明的正是本拍要说的那句话:
    「这条信号被批准发布了, 但还没有跑出过一份过门槛的回测」。

    刻意只有一个原因码, 不细分成 `calib_missing` / `calib_pending` / `calib_failed` /
    `calib_key_mismatch`。对读者来说四者的结论一样(不可见), 而细分会让诊断字段变成
    一个"这个信号离通过还差几步"的进度条 —— 那个信息不该从监控接口泄露出去。
    真要排查, 去看 `calib.json` 与 `/hits/calib` 的口径摘要。
    """

    ALL = (
        DEFINITION_MISSING,
        NOT_PUBLISHABLE,
        LIMIT_REOPEN,
        FAMILY_NOT_EXPOSED,
        UNKNOWN_FAMILY_FILTER,
        OUT_OF_SCOPE,
        CALIB_NOT_PASSED,
    )


@dataclass(frozen=True, slots=True)
class FilteredHits:
    """闸的输出。

    `rejected` 带上原因而不是只丢一个数字: 运维问"为什么我的命中不显示"时, 直接看
    计数就知道卡在哪一条规则上。原因里不含任何个股字段, 可以安全地进日志。
    """

    hits: list[SignalHit]
    rejected: dict[str, int]
    scope_applied: bool
    scope_size: int
    calib_passed_keys: int = 0
    """参与本轮判定的 `passed` 登记条数（**只回数字, 不回内容**）。

    诊断用: 「为什么一条都不显示」时, 这个数等于 0 基本就说明 `calib.json` 不存在
    或者一条 `passed` 都没有 —— 那是诚实空态, 不是缺陷。
    """

    @property
    def count(self) -> int:
        return len(self.hits)


def _reject(rejected: dict[str, int], reason: str) -> None:
    rejected[reason] = rejected.get(reason, 0) + 1


def visible_hits(
    hits: list[SignalHit],
    registry: Any,
    *,
    family: str | None = None,
    symbol: str | None = None,
    scope_symbols: frozenset[str] | None = None,
    today: date | None = None,
    calib: CalibIndex | None = None,
) -> FilteredHits:
    """把候选命中过闸。

    参数:
    - `registry`: 定义注册表(实现 `get(signal_id) -> SignalRecord | None`)。传 `None`
      等同于"定义全缺失", 即全部丢弃 —— fail-closed, 不猜。
    - `family`: 可选 family 过滤。传 `composite` 或任何不在 `EXPOSED_FAMILIES` 的值,
      结果恒空(不是 400: 空比报错更符合"这个 tab 现在没有这类数据"的语义)。
    - `symbol`: 可选个股过滤。
    - `scope_symbols`: 基本面命中的范围闸。传 `None` 表示不做范围过滤(仅测例用)。
    - `calib`: 校准登记索引。**传 `None` 等同于"一条 passed 都没有"** —— 全部丢弃。
      刻意**不**给一个"跳过校准闸"的开关: 那样等于给可见性留一个后门, 而调用
      `visible_hits` 的地方恰恰是最需要它严格的地方。测例要验"仅 publishable 是否可见",
      应当传一个空索引 `CalibIndex([])`, 得到"不可见"—— 正是真实行为。
    """
    rejected: dict[str, int] = {}
    kept: list[SignalHit] = []
    scope = scope_symbols if scope_symbols is not None else frozenset()
    index = calib if calib is not None else CalibIndex([])

    family_filter_ok = family is None or family in EXPOSED_FAMILIES

    for hit in hits:
        if not family_filter_ok:
            _reject(rejected, VisibilityReject.UNKNOWN_FAMILY_FILTER)
            continue

        # 只暴露的两类 family。`composite` 到这里就被拦掉, 与前端是否渲染无关。
        if hit.family not in EXPOSED_FAMILIES:
            _reject(rejected, VisibilityReject.FAMILY_NOT_EXPOSED)
            continue

        if family is not None and hit.family != family:
            continue
        if symbol is not None and hit.symbol != symbol:
            continue

        definition: SignalRecord | None = (
            registry.get(hit.signal_id) if registry is not None else None
        )
        if definition is None:
            _reject(rejected, VisibilityReject.DEFINITION_MISSING)
            continue
        if definition.status != "publishable":
            _reject(rejected, VisibilityReject.NOT_PUBLISHABLE)
            continue
        if definition.event_class == "limit_reopen" or hit.event_class == "limit_reopen":
            # 双保险: 定义侧被外部改成 publishable 时, 这里仍然拦住。
            _reject(rejected, VisibilityReject.LIMIT_REOPEN)
            continue

        # 范围闸只作用于基本面日表。
        if hit.is_daily_fundamental and scope_symbols is not None and hit.symbol not in scope:
            _reject(rejected, VisibilityReject.OUT_OF_SCOPE)
            continue

        # 校准闸: 独立的 calib 登记里必须有与**本命中**三键对齐的 passed。
        #
        # 三键里 `definition_version` 取**命中自带的**(不是定义当前的): 一条按 v1 算的
        # 命中, 不该因为定义升到 v2 而自动获得 v2 的校准结论背书。
        # `freq_lock_version` 取命中镜像值; 缺失即不可见 —— 连按哪套口径算的都不知道,
        # 不能把"当前戳"顶上去替它担保。
        if hit.freq_lock_version is None:
            _reject(rejected, VisibilityReject.CALIB_NOT_PASSED)
            continue
        if index.get(hit.signal_id, hit.definition_version, hit.freq_lock_version) is None:
            _reject(rejected, VisibilityReject.CALIB_NOT_PASSED)
            continue

        kept.append(hit)

    # 排序: 新的在前。asof 是字符串, 但格式可能日/时混用, 所以按解析后的日期排,
    # 解析不了(过期)的排最后 —— 它们恰好是可读性最差的那批。
    kept.sort(key=lambda h: (h.asof_day is not None, h.asof_day or date.min, h.hit_id), reverse=True)

    return FilteredHits(
        hits=kept,
        rejected=rejected,
        scope_applied=scope_symbols is not None,
        scope_size=len(scope),
        calib_passed_keys=len(index),
    )


def hit_to_view(hit: SignalHit, *, today: date | None = None) -> dict[str, Any]:
    """把命中变成 API 视图。

    在 `to_dict()` 之外补三个**派生**字段, 让前端不必自己实现日期比较:

    - `asof_date`: 归一后的上海日期(解析不了为 null)
    - `stale`: 是否过期(N4 规则)
    - `is_daily_fundamental`: 是否按日表形态呈现

    `definition_status_at_hit` 原样带出供审计, 但**不**参与可见性判断。
    """
    payload = hit.to_dict()
    payload["asof_date"] = None if hit.asof_day is None else hit.asof_day.isoformat()
    payload["stale"] = hit.is_stale(today=today)
    payload["is_daily_fundamental"] = hit.is_daily_fundamental
    return payload


def build_response(
    filtered: FilteredHits,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """组装 `GET /api/buy-sell-signals/hits` 的响应体。

    `empty_message` **恒在**, 不管 `count` 是多少。空列表时它是空态文案, 非空时前端
    也用不到 —— 但恒在的好处是契约稳定, 少一个"有时有有时没有"的字段。

    **其字面一个字节都不许改**(`EMPTY_HITS_MESSAGE`)。"暂无已校准的买卖信号" 这句话
    里的"已校准"三个字是本拍唯一一处对用户承诺"闸在生效"的地方: 改成"今天没信号"
    就等于把"还没有任何东西通过校准"说成"系统在跑、今天恰好空着"。
    """
    views = [hit_to_view(hit, today=today) for hit in filtered.hits]
    return {
        "schema_version": HITS_SCHEMA_VERSION,
        "namespace": NAMESPACE,
        "empty_message": EMPTY_HITS_MESSAGE,
        "hits": views,
        "count": len(views),
        # 以下三个是诊断字段, 只有形状没有个股内容。
        "rejected": dict(sorted(filtered.rejected.items())),
        "scope_applied": filtered.scope_applied,
        "scope_size": filtered.scope_size,
        # 校准闸的只读摘要: 告诉调用方"这道闸开着", 以及本轮有多少条 passed 参与判定。
        # 刻意只说条数, 不回任何 signal_id —— 否则这个字段会变成一个"哪些信号已校准"
        # 的查询接口, 而那个信息应当只在 calib.json 与运维口径里。
        "calib_gate": "applied",
        "calib_passed_keys": filtered.calib_passed_keys,
    }


__all__ = [
    "EXPOSED_FAMILIES",
    "FilteredHits",
    "VisibilityReject",
    "build_response",
    "hit_to_view",
    "visible_hits",
]
