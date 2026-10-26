"""证据账本审计口径（P-ledger）。

本模块是**只读审计层**，不写库、不建表、不改上游结算语义。

真源（tip 已存在，本拍只启用审计）
--------------------------------
- ``analysis_history``：建议快照（``operation_advice`` / ``ideal_buy`` /
  ``stop_loss`` / ``take_profit`` / ``created_at`` / ``query_id``）。
- ``backtest_results``：上游结算（``start_price`` / ``end_close`` /
  ``outcome`` / ``hit_*`` / ``simulated_*``）。

两者都在 ``vendor/daily_stock_analysis/src/storage.py``。上游已提供只读端点
``GET /backtest/results``，本模块经 TSP 既有 proxy 消费，**不另起 append-only
旁路表**，也**不默认硬拉 sidecar**（关掉 DSA 时审计整体降级为不可用）。

三道审计缺口（本模块落地 A1–A3 / A5–A7）
----------------------------------------
- **A1 入场偏乐观**：上游 ``backtest_service.py`` 用 ``start_bar.close`` 入场
  （收盘入场），漏掉隔夜跳空。本模块并列 ``entry_close``（上游原值）与
  ``entry_next_open``（次日开盘，TSP 侧才有的列），两者不合并、不互相顶替。
- **A2 一字涨停 / 不可成交**：上游 service / engine **没有**涨停剔除逻辑。
  一字涨停（开=高=低=收且相对昨收涨停）当天既买不到也卖不出，把它算进胜率
  分母等于给「买不到的赢」记分。本模块标 ``unfillable="limit_up_one_word"``
  并把它**排除出分母**，但仍在计数里可见。
- **A7 未审计标**：任何展示上游回测 / 纸面战绩的面必须带
  ``TSP_AUDIT_BADGE``；``tsp_audit_status`` 默认 ``unaudited``，
  只有显式审计记录才能翻转（本拍不提供翻转路径，见 ``audit_status_for``）。

一句话：**这一层只负责把账本研究清楚，不负责宣布账本可信。**

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ===== 未审计标（A7）=====
TSP_AUDIT_BADGE: str = "未经 TSP 审计"
AUDIT_STATUS_UNAUDITED: str = "unaudited"
AUDIT_STATUSES: frozenset[str] = frozenset({AUDIT_STATUS_UNAUDITED})

# ===== 来源打标（A6）=====
SOURCE_ANALYSIS_BRIDGE: str = "analysis_bridge"
SOURCE_MANUAL_REGISTER: str = "manual_register"
LEDGER_SOURCES: frozenset[str] = frozenset({SOURCE_ANALYSIS_BRIDGE, SOURCE_MANUAL_REGISTER})
SOURCE_NOTES: Mapping[str, str] = {
    SOURCE_ANALYSIS_BRIDGE: "分析落库时由 wrap_history_save 自动登记（无需人工动作）",
    SOURCE_MANUAL_REGISTER: "由 sniper_alerts.register 手动登记；整段替换并清 fired，线史不可回看",
}

# ===== 不可成交分类（A2）=====
UNFILLABLE_LIMIT_UP_ONE_WORD: str = "limit_up_one_word"
UNFILLABLE_LIMIT_DOWN_ONE_WORD: str = "limit_down_one_word"
UNFILLABLE_REASONS: frozenset[str] = frozenset(
    {UNFILLABLE_LIMIT_UP_ONE_WORD, UNFILLABLE_LIMIT_DOWN_ONE_WORD}
)

# ===== 引擎口径（A3，tip 锚定值；本拍不改上游默认）=====
ENGINE_VERSION_TIP: str = "v1"
DEFAULT_EVAL_WINDOW_DAYS: int = 10
NEUTRAL_BAND_PCT: float = 2.0
LIMIT_PCT_MAIN_BOARD: float = 10.0
LIMIT_TOLERANCE_PCT: float = 0.15

# ===== 样本门槛（A4）=====
MIN_SAMPLE_FOR_PERCENT: int = 30


def audit_status_for(_record: Mapping[str, Any] | None = None) -> str:
    """当前审计状态。本拍恒 ``unaudited``。

    刻意**不接受** env / 参数翻转：审计状态要写在审计记录里，不能靠一个
    环境变量假装已过审。签名单收一个 record 是为了让调用点看起来统一，
    但它不参与判定。
    """
    return AUDIT_STATUS_UNAUDITED


def is_unfillable_limit_up(
    *,
    open_price: float | None,
    high_price: float | None,
    low_price: float | None,
    close_price: float | None,
    prev_close: float | None,
    limit_pct: float = LIMIT_PCT_MAIN_BOARD,
    tolerance: float = LIMIT_TOLERANCE_PCT,
) -> bool:
    """一字涨停：全天开=高=低=收，且相对昨收达到涨停。

    判不出来的情况（缺价、昨收非正）一律返回 ``False`` —— 宁可把样本留在
    分母里，也不要凭猜测把样本剔掉。剔除必须能被证据支持。
    """
    values = (open_price, high_price, low_price, close_price)
    if prev_close is None or prev_close <= 0:
        return False
    if any(value is None for value in values):
        return False
    o, h, lo, c = (float(value) for value in values)  # type: ignore[arg-type]
    if not (o == h == lo == c):
        return False
    change_pct = (c / float(prev_close) - 1.0) * 100.0
    return change_pct >= limit_pct - tolerance


def unfillable_reason(
    *,
    open_price: float | None = None,
    high_price: float | None = None,
    low_price: float | None = None,
    close_price: float | None = None,
    prev_close: float | None = None,
    limit_pct: float = LIMIT_PCT_MAIN_BOARD,
    tolerance: float = LIMIT_TOLERANCE_PCT,
) -> str | None:
    """不可成交原因；可成交返回 ``None``。"""
    if is_unfillable_limit_up(
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        close_price=close_price,
        prev_close=prev_close,
        limit_pct=limit_pct,
        tolerance=tolerance,
    ):
        return UNFILLABLE_LIMIT_UP_ONE_WORD
    values = (open_price, high_price, low_price, close_price)
    if prev_close is None or prev_close <= 0 or any(v is None for v in values):
        return None
    o, h, lo, c = (float(v) for v in values)  # type: ignore[arg-type]
    if not (o == h == lo == c):
        return None
    change_pct = (c / float(prev_close) - 1.0) * 100.0
    if change_pct <= -limit_pct + tolerance:
        return UNFILLABLE_LIMIT_DOWN_ONE_WORD
    return None


def exclude_from_denominator(*, outcome: str | None, unfillable: str | None) -> bool:
    """该样本是否应排除出胜率分母。

    不可成交 → 排除（但仍计数）；``outcome`` 为 ``neutral`` / 空 → 排除；
    其余（``win`` / ``loss``）计入。
    """
    if unfillable:
        return True
    return str(outcome or "").strip().lower() not in {"win", "loss"}


@dataclass(frozen=True, slots=True)
class AuditedRow:
    """单条审计后的结算行。

    并列两个入场价是本模块的核心：上游只给 ``entry_close``，
    ``entry_next_open`` 由 TSP 侧补，两者都留着，谁也没覆盖谁。
    """

    analysis_history_id: int
    code: str
    outcome: str
    entry_close: float | None
    entry_next_open: float | None
    unfillable: str | None = None
    source: str = SOURCE_ANALYSIS_BRIDGE
    simulated_return_pct: float | None = None

    @property
    def in_denominator(self) -> bool:
        return not exclude_from_denominator(outcome=self.outcome, unfillable=self.unfillable)

    @property
    def entry_gap_pct(self) -> float | None:
        """次日开盘相对收盘入场的偏离（%）。正=开盘更高，上游偏保守；负=更乐观。"""
        if self.entry_close is None or self.entry_next_open is None:
            return None
        if self.entry_close <= 0:
            return None
        return (self.entry_next_open / self.entry_close - 1.0) * 100.0

    @property
    def entry_basis(self) -> str:
        """入场口径说明。上游是 ``close``（偏乐观），TSP 并列 ``next_open``。"""
        return "close（上游原值·漏隔夜跳空）" if self.entry_next_open is None else "close 与 next_open 并列"

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_history_id": self.analysis_history_id,
            "code": self.code,
            "outcome": self.outcome,
            "entry_close": self.entry_close,
            "entry_next_open": self.entry_next_open,
            "entry_gap_pct": _round(self.entry_gap_pct),
            "entry_basis": self.entry_basis,
            "unfillable": self.unfillable,
            "in_denominator": self.in_denominator,
            "source": self.source,
            "source_note": SOURCE_NOTES.get(self.source, ""),
            "simulated_return_pct": self.simulated_return_pct,
        }


@dataclass(frozen=True, slots=True)
class LedgerAudit:
    """一次审计汇总。"""

    rows: tuple[AuditedRow, ...] = ()
    audit_status: str = AUDIT_STATUS_UNAUDITED
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.rows) and not self.unavailable_reason

    @property
    def badge(self) -> str:
        return TSP_AUDIT_BADGE if self.audit_status == AUDIT_STATUS_UNAUDITED else ""

    def _counts(self) -> tuple[int, int, int, int]:
        denominator = [r for r in self.rows if r.in_denominator]
        wins = sum(1 for r in denominator if str(r.outcome).lower() == "win")
        losses = sum(1 for r in denominator if str(r.outcome).lower() == "loss")
        excluded = len(self.rows) - len(denominator)
        return wins, losses, excluded, len(denominator)

    def summary(self) -> dict[str, Any]:
        wins, losses, excluded, denominator = self._counts()
        suppressed = denominator < MIN_SAMPLE_FOR_PERCENT
        return {
            "audit_status": self.audit_status,
            "badge": self.badge,
            "rows_total": len(self.rows),
            "denominator": denominator,
            "wins": wins,
            "losses": losses,
            "excluded_from_denominator": excluded,
            "unfillable_count": sum(1 for r in self.rows if r.unfillable),
            "entry_basis": sorted({r.entry_basis for r in self.rows}),
            "win_rate_pct": None if suppressed else round(wins / denominator * 100.0, 2) if denominator else None,
            "sample_below_threshold": suppressed,
            "min_sample_for_percent": MIN_SAMPLE_FOR_PERCENT,
            "unavailable_reason": self.unavailable_reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "summary": self.summary(),
            "rows": [row.to_dict() for row in self.rows],
            "sources": dict(SOURCE_NOTES),
        }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _source_of(raw: Mapping[str, Any]) -> str:
    raw_source = str(raw.get("source") or "").strip()
    if raw_source in LEDGER_SOURCES:
        return raw_source
    # 上游 ``/backtest/results`` 不带来源字段。历史上只有自动登记会落
    # ``analysis_history`` 并触发结算，所以缺省按自动登记处理；手动的
    # 线史不走 backtest_results，不会混进来。
    return SOURCE_ANALYSIS_BRIDGE


def audit_rows(
    items: Iterable[Mapping[str, Any]],
    *,
    next_open_by_history_id: Mapping[int, float] | None = None,
    prev_close_by_history_id: Mapping[int, float] | None = None,
) -> LedgerAudit:
    """把 ``/backtest/results`` 的 items 审计成行。

    ``next_open_by_history_id`` / ``prev_close_by_history_id`` 由调用方按需注入；
    缺的时候对应列就是 ``None``，绝不猜。
    """
    next_open_map = next_open_by_history_id or {}
    prev_close_map = prev_close_by_history_id or {}
    rows: list[AuditedRow] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        history_id = raw.get("analysis_history_id")
        if isinstance(history_id, bool) or not isinstance(history_id, int):
            continue
        reason = unfillable_reason(prev_close=prev_close_map.get(history_id))
        rows.append(
            AuditedRow(
                analysis_history_id=history_id,
                code=str(raw.get("code") or ""),
                outcome=str(raw.get("outcome") or ""),
                entry_close=_float_or_none(raw.get("start_price")),
                entry_next_open=_float_or_none(next_open_map.get(history_id)),
                unfillable=reason,
                source=_source_of(raw),
                simulated_return_pct=_float_or_none(raw.get("simulated_return_pct")),
            )
        )
    rows.sort(key=lambda row: (row.code, row.analysis_history_id))
    return LedgerAudit(rows=tuple(rows))


def audit_unavailable(reason: str) -> LedgerAudit:
    """取不到上游账本时的 fail-closed 结果：空行 + 未审计标 + 原因。"""
    return LedgerAudit(rows=(), audit_status=AUDIT_STATUS_UNAUDITED, unavailable_reason=str(reason or "unavailable"))


def fetch_ledger_items(
    *,
    limit: int = 200,
    page: int = 1,
    code: str | None = None,
) -> tuple[list[Mapping[str, Any]], str]:
    """经 TSP 既有 proxy 读上游 ``GET /backtest/results``。

    返回 ``(items, error)``：成功时 ``error`` 为空串；失败时 ``items`` 为空、
    ``error`` 是给人看的一句话。**不重试、不重抓、不触发回测**——调的是
    ``results`` 而不是 ``run``，天然没有写库副作用。
    """
    from app.custom.dsa.proxy import UpstreamError, forward, health

    reachable, detail = health()
    if not reachable:
        return [], f"sidecar 不可达：{detail}"
    params: dict[str, Any] = {"page": max(1, int(page)), "limit": max(1, min(int(limit), 200))}
    if code:
        params["code"] = code
    try:
        status_code, payload, _media, _extra = forward("GET", "backtest/results", params=list(params.items()))
    except UpstreamError as exc:
        return [], f"上游读取失败：{exc}"
    if status_code != 200:
        return [], f"上游返回 {status_code}"
    import json

    try:
        body = json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 坏 JSON 不能冒充「没有记录」
        return [], "上游返回非 JSON"
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return [], "上游响应缺 items"
    return [item for item in items if isinstance(item, Mapping)], ""


def ledger_audit_payload(
    *,
    limit: int = 200,
    page: int = 1,
    code: str | None = None,
    items: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """路由层入口。``items`` 只在测例里注入，生产走 proxy。"""
    if items is None:
        fetched, error = fetch_ledger_items(limit=limit, page=page, code=code)
        if error:
            return audit_unavailable(error).to_dict()
        items = fetched
    audit = audit_rows(items)
    payload = audit.to_dict()
    payload["audit_items"] = [
        {
            "id": "A1",
            "title": "入场价并列",
            "state": "partial",
            "detail": "上游 close 入场（偏乐观·漏隔夜跳空）；next_open 列由调用方按需注入，缺则留空，不倒推",
        },
        {
            "id": "A2",
            "title": "一字涨停 / 不可成交",
            "state": "partial",
            "detail": "已标 unfillable 并排除分母；判定依赖 prev_close，缺则留空且不剔除",
        },
        {
            "id": "A3",
            "title": "窗口 / 中性带 / ambiguous",
            "state": "pinned",
            "detail": f"eng={ENGINE_VERSION_TIP} 窗={DEFAULT_EVAL_WINDOW_DAYS} 中性带={NEUTRAL_BAND_PCT}%；改默认须显式",
        },
        {
            "id": "A4",
            "title": "同期机械基线",
            "state": "pending",
            "detail": "机械基线来自 quant_evidence；样本 < "
            f"{MIN_SAMPLE_FOR_PERCENT} 不给百分比；两者**不合并分母**",
        },
        {
            "id": "A5",
            "title": "lookahead 截断",
            "state": "registered",
            "detail": "证据重放登记 run_truncated_replay / assert_truncated；截断失效 fail-closed",
        },
        {
            "id": "A6",
            "title": "来源打标",
            "state": "done",
            "detail": "analysis_bridge 自动 vs manual_register 手动；覆盖致线史不可回看，仅展示层说明",
        },
        {
            "id": "A7",
            "title": "未审计标",
            "state": "shown",
            "detail": f"审计状态恒 {AUDIT_STATUS_UNAUDITED}，展示面须带「{TSP_AUDIT_BADGE}」",
        },
    ]
    return payload
