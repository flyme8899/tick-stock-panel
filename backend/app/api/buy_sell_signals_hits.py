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

## 状态自述(P-bss-obs)

`GET /hits/scope` 额外露出命中钉钉通道的只读状态(`hits_dingtalk`)与两个
`*_TRIGGERING_ENABLED` 布尔。这是**零行为扩**: 只读既有 `hits_dingtalk_status()`
与既有 `Final` 常量, 不新增 `try_push_hit` 调用方、不翻开关、不回传 url / secret。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request

from app.buy_sell_signals.auction_abnormal_stub import AUCTION_ABNORMAL_TRIGGERING_ENABLED
from app.buy_sell_signals.calib_store import DiskCalibStore, build_calib_store, build_index
from app.buy_sell_signals.constants import EMPTY_HITS_MESSAGE
from app.buy_sell_signals.hits_dingtalk import hits_dingtalk_status
from app.buy_sell_signals.hits_store import DiskHitStore, build_hit_store
from app.buy_sell_signals.hits_visibility import build_response, visible_hits
from app.buy_sell_signals.volume_breakout_stub import VOLUME_BREAKOUT_TRIGGERING_ENABLED
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

    ## P-bss-obs：状态自述(零行为扩)

    在既有空态统计之外, 并入两样**只读**信息, 回答「为什么没有命中钉钉消息」:

    - `hits_dingtalk`(嵌套对象): 直接取 `hits_dingtalk_status()` 的五个键,
      不在这里重写 configured 判定。**不回** url / secret —— 运维只需要知道
      「配了没」, 不需要在这个只读口上把 webhook 完整打印出来。
    - `volume_breakout_triggering_enabled` / `auction_abnormal_triggering_enabled`:
      原样读两个 `Final = False` 常量。**本接口不改它们**, 翻 True 是另口令。

    `notes` 刻意省略(N2): 那三句是给人读的文档, 已经写进 `docs/buy-sell-signals-skeleton.md`,
    塞进 JSON 只会让响应变长。**也不加** `empty_reason_chain` 数组(N4):
    空态顺序靠上面这些字段天然可观测, 再造一个数组等于把同一份判断写两遍。

    ## P-bss-p3a：`NON_LIVE_MODE` 报警计数

    新增扁平键 `non_live_mode_rejected`(int)。它跑的是与 `GET /hits` **同构**的那一次
    `visible_hits`, 只取被 `NON_LIVE_MODE` 拒掉的条数。

    - **常态 0**: 空 store 是 0, 全是 live 也是 0。
    - **非 0 ＝ 报警**: 生产 store 里混进了影子/夹具记录。这不是"用户看不到信号"的
      正常原因, 而是"有东西不该在这儿"。它**不**参与 #238 那条空态主因链, 只是旁注。
    - **只回数字**: 不回 symbol / hit_id / 明细数组。回明细等于把影子数据从后门列出来,
      而本拍做的正是把它挡在可见层之外。

    本函数**只读**: 不调 `try_push_hit` / `maybe_try_push_hit`, 不写 HitStore,
    不碰 `calib.upsert`。露出状态 ≠ 已接线自动推送。
    """
    data_dir = _data_dir(request)
    hit_store = DiskHitStore(data_dir)
    calib_store = DiskCalibStore(data_dir)
    # 既有函数, 不新造语义: configured / shares / cooldown / reason / env_name 的含义
    # 以 `hits_dingtalk` 模块为准, 本文件只做搬运。
    status = hits_dingtalk_status()

    # --- P-bss-p3a: live store 的 NON_LIVE_MODE 报警计数(只读; 不列明细)
    #
    # 复用与 `GET /hits` 完全同构的那一次判定(同一个 registry / scope / calib 索引),
    # 区别只是这里**只取计数**。同构很重要: 如果这里另写一套过滤, 两边迟早会不一致,
    # 而这个字段的全部价值就在于"它和监控页看到的是同一件事"。
    #
    # 语义: 常态 0(空 store 自然是 0, 全 live 也是 0)。非 0 ＝ 生产 store 里混进了
    # 影子/夹具记录 —— 那是报警, 不是正常空态, 也不是"用户看不到信号"的原因。
    #
    # 刻意**不**返回任何 symbol / hit_id / 明细数组: 这个接口只回统计, 回明细就等于
    # 把影子数据从后门列出来了(那正是本拍要挡的东西)。
    filtered = visible_hits(
        hit_store.list_all(),
        _registry(request),
        scope_symbols=scope_symbols(request),
        calib=_calib_index(request),
    )
    non_live_mode_rejected = int(filtered.rejected.get("NON_LIVE_MODE", 0))

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
        # --- P-bss-obs: 只读露出(N1 嵌套 / N2 省略 notes / N4 不加 chain)---
        "hits_dingtalk": {
            "configured": status.configured,
            "shares_with_common_group": status.shares_with_common_group,
            "env_name": status.env_name,
            "cooldown_min": status.cooldown_min,
            "reason_when_missing": status.reason_when_missing,
        },
        "volume_breakout_triggering_enabled": VOLUME_BREAKOUT_TRIGGERING_ENABLED,
        "auction_abnormal_triggering_enabled": AUCTION_ABNORMAL_TRIGGERING_ENABLED,
        # --- P-bss-p3a: 报警计数(N1 拍板扁平键; 不列影子明细)---
        #
        # 旁注性质: 空态主因仍按「hits_file_exists → 两 TRIGGERING →
        # calib_passed_keys → hits_dingtalk.configured」排查, 这个数字不参与那条链。
        "non_live_mode_rejected": non_live_mode_rejected,
    }


__all__ = ["router", "scope_symbols"]
