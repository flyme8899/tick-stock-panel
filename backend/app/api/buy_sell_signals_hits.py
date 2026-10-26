"""买卖信号命中只读 API。

`GET /api/buy-sell-signals/hits` —— 监控中心「买卖信号」tab 的唯一数据源。

## 为什么不复用 `/api/alerts`

`/api/alerts` 里已经有一条 `monitor_event_type=buy_sell_signal_status_changed` 的记录流,
那是**定义状态变更**(「这条信号被批准发布了」), 不是「某只票命中了」。两者混在一个列表里,
读者会把"信号上线"读成"今天有票被选中" —— 这正是本拍要修的那类误读。

所以命中走独立路由, 各自的数据源、各自的字段、各自的空态文案。订阅端(SSE)那条
仍然只有定义状态, 本拍只给它补了 `family` / `event_class` 两个分类字段。

## 校准闸在本文件的哪里

不在接口层，在 `hits_visibility.visible_hits` 里（第 7 条规则）。本文件只负责把
校准登记索引递给它，以及在响应里带一个只读摘要 `calib_gate: "applied"`。

**不在前端过滤**：前端即使全放了也不过是显示空，后端不放的东西不会因为它"渲染逻辑
宽松"而漏出去。反过来，如果只在 `Monitor.tsx` 里按 `calibrated` 过滤，那么绕过前端
直接调本接口就能拿到全部命中 —— 那种实现看起来一模一样，安全性完全不同。

`empty_message` 恒为「暂无已校准的买卖信号」，本文件不改它的字面，
`calib.json` 不存在也照用这一句：空就是空。

## 鉴权

沿用站内只读接口的现状(与 `/api/alerts` / `/api/watchlist` 同级), **不新增** Basic、
**不开**匿名写。本路由只有 GET, 没有任何写入口 —— 命中层的写入属于另一个拍(评估器),
现在连代码路径都没有。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request

from app.buy_sell_signals.calib_store import DiskCalibStore, build_calib_store, build_index
from app.buy_sell_signals.constants import EMPTY_HITS_MESSAGE
from app.buy_sell_signals.hits_store import DiskHitStore, build_hit_store
from app.buy_sell_signals.hits_visibility import build_response, visible_hits
from app.market_time import cn_today
from app.services import watchlist

router = APIRouter(prefix="/api/buy-sell-signals", tags=["buy-sell-signals"])

#: 基本面命中的范围闸口径(PR 里钉死的一种):
#: **基本面过闸, 事件不过闸** —— 两者都仍须过 publishable 闸。
SCOPE_FAMILY = "daily_fundamental"


def _store_hits(request: Request) -> list:
    """取命中店的候选命中。

    优先用 `app.state.buy_sell_signal_hit_store`(启动时建好, 也方便测例替换成内存店),
    没有则按 `data_dir` 现建一个只读店。**任何情况下都只读**。
    """
    store = getattr(request.app.state, "buy_sell_signal_hit_store", None)
    if store is None:
        store = build_hit_store(_data_dir(request))
    return store.list_all()


def _data_dir(request: Request) -> Path:
    try:
        return request.app.state.repo.store.data_dir
    except AttributeError:
        return Path("data")


def _registry(request: Request):
    """定义注册表。缺失时返回 `None` —— 闸会把所有命中判为"定义缺失"。"""
    return getattr(request.app.state, "buy_sell_signal_store", None)


def _calib_index(request: Request):
    """校准登记索引。

    优先用 `app.state.buy_sell_signal_calib_store`(启动时建好, 也方便测例替换成内存店),
    没有则按 `data_dir` 现建一个磁盘店。**任何情况下都只读。**

    缺失/读不出来时返回空索引 —— 含义是"一条 passed 都没有", 即所有命中不可见。
    这是 fail-closed 的正确方向: 拿不到校准证据时, 宁可什么都不显示。
    """
    store = getattr(request.app.state, "buy_sell_signal_calib_store", None)
    if store is None:
        store = build_calib_store(_data_dir(request))
    return build_index(store)


def _paper_symbols(data_dir: Path) -> set[str]:
    """纸面账户当前持仓的 symbol 集合。

    直接读 `paper.overview()` 而不是走 HTTP: 同一个进程里没有理由绕一圈网络。
    任何异常都退化为空集 —— 持仓读不出来时, 基本面命中显示得少一点, 比让整个 tab
    报 500 好。
    """
    try:
        from app.strategy import paper

        ov = paper.overview(data_dir)
        return {
            str(item.get("symbol", "")).strip()
            for item in (ov.get("holdings") or [])
            if str(item.get("symbol", "")).strip()
        }
    except Exception:  # noqa: BLE001
        return set()


def _watchlist_symbols() -> set[str]:
    try:
        rows = watchlist.list_symbols()
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for row in rows:
        sym = row.get("symbol") if isinstance(row, dict) else None
        if isinstance(sym, str) and sym.strip():
            out.add(sym.strip())
    return out


def scope_symbols(request: Request) -> frozenset[str]:
    """自选 ∪ 持仓。

    两者都读不出来时返回空集 —— 含义是"基本面命中一条都不显示"。这是保守方向:
    空集会让人以为"没有信号"而不是"看到了一堆不该看的"。
    """
    data_dir = _data_dir(request)
    return frozenset(_watchlist_symbols() | _paper_symbols(data_dir))


@router.get("/hits")
def list_hits(
    request: Request,
    family: str | None = Query(
        default=None,
        description="family 过滤; 只接受 minute_event / daily_fundamental, 其余一律返回空",
    ),
    symbol: str | None = Query(default=None, description="个股过滤"),
    today: date | None = Query(
        default=None,
        description="仅测试用: 覆盖过期判定的'今天'。生产调用方不应传。",
        include_in_schema=False,
    ),
) -> dict[str, Any]:
    """只读列出**已校准**的命中。

    过闸规则见 `hits_visibility.visible_hits`。返回体里的 `empty_message` 恒为
    「暂无已校准的买卖信号」—— 前端直接渲染它, 不要自己拼空态文案。
    """
    hits = _store_hits(request)
    scope = scope_symbols(request)
    filtered = visible_hits(
        hits,
        _registry(request),
        family=family,
        symbol=symbol,
        scope_symbols=scope,
        today=today,
        calib=_calib_index(request),
    )
    return build_response(filtered, today=today or cn_today())


@router.get("/hits/scope")
def hits_scope(request: Request) -> dict[str, Any]:
    """返回范围闸的口径与命中数(**只回统计, 不回明细**)。

    给运维用的: 「为什么我这里基本面是空的」—— 大概率是自选和持仓都为空。
    刻意不回 symbol 列表, 免得这个接口变成第二个自选接口。
    """
    data_dir = _data_dir(request)
    hit_store = DiskHitStore(data_dir)
    calib_store = DiskCalibStore(data_dir)
    return {
        "scope_family": SCOPE_FAMILY,
        "scope_size": len(scope_symbols(request)),
        "empty_message": EMPTY_HITS_MESSAGE,
        "hits_file": str(hit_store.path),
        "hits_file_exists": hit_store.exists(),
        # 校准闸的口径摘要。**只回条数与路径, 不回任何 signal_id** —— 否则这个接口
        # 会变成一个"哪些信号已校准"的查询口。
        "calib_gate": "applied",
        "calib_passed_keys": len(build_index(calib_store)),
        "calib_file": str(calib_store.path),
        "calib_file_exists": calib_store.exists(),
    }


__all__ = ["router", "scope_symbols"]
