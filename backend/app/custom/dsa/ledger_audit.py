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

P-arg-snap 增量（四字段 + snapshot 冻结）
----------------------------------------
在 A1–A7 **之上并列增量**，不改任何既有分母/入场/徽章语义。

借 TradingAgents 的**机制**——多空两段论据、风控否决、决策时点 PIT 冻结——
落到 TSP 的 ledger 审计契约上。**零代码搬运**：本模块不 import
``tradingagents``，不引任何多代理编排依赖，只有只读解析与时间比较。

- ``bull_case`` / ``bear_case``：建议生成时的多空论据摘要，只读快照。
- ``risk_veto``：``{"veto": bool, "reason": str | null}`` 或 ``null``。
  **``null`` ≠ ``veto=false``** —— 前者是"没有风控结论"，后者是"明确未否决"。
  ``veto=true`` 缺 reason 视为非法结论，降 ``null`` 而不是输出半个结论。
- ``snapshot_asof``：论据所依据的行情快照时点，须 **≤** ``advice_generated_at``。
  晚于 → ``snapshot_late=True``，**只**排除论据质量统计，**不**并入 A2 胜率分母。
- ``advice_generated_at``：建议生成时刻，默认取 ``analysis_history.created_at``，
  并列暴露便于对拍。

时间口径（N4）：全部 ISO-8601 带 ``+08:00``，比较走**秒级**同区。
裸 datetime 无时区按 Asia/Shanghai 解释；只有日期则按当日 00:00:00。

**禁事后回填**：不拿结算后行情、当日 bar、``evaluated_at`` 或重跑回测的结果
回填 ``snapshot_asof``。历史缺字段就是 ``null``，然后不计入论据质量。
宁缺毋滥好过编一个看起来合理的时点。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ===== P-arg-snap：声明式四字段 + snapshot 冻结（只读解析）=====
#
# 借 TradingAgents 的**机制**（多空两段论据 + 风控否决 + 决策时点 PIT 冻结），
# 但**零代码搬运**：这里只有只读解析与时间比较，没有任何多代理编排、
# 没有 ``import tradingagents``、没有 pip 依赖。
#
# 仅供研究，不构成投资建议；执行须人工确认。

#: 时间口径写死为 Asia/Shanghai（UTC+8）。中国无夏令时，用固定偏移即可，
#: 不引 ``zoneinfo`` 依赖，也避免宿主缺 tzdata 时静默退化成 UTC。
ASIA_SHANGHAI: timezone = timezone(timedelta(hours=8))
TZ_NAME: str = "Asia/Shanghai"

#: 多空论据的候选键（按优先级，先命中先用）。上游各处命名不统一，
#: 这里只认这批已知名字；**不**做模糊匹配（``contains("bull")`` 那种会把
#: ``bullish_beta`` 之类的数值键误当文案）。
BULL_CASE_KEYS: tuple[str, ...] = (
    "bull_case",
    "strongest_bullish_signal",
    "bullish_case",
    "bull_argument",
)
BEAR_CASE_KEYS: tuple[str, ...] = (
    "bear_case",
    "strongest_bearish_signal",
    "bearish_case",
    "bear_argument",
)
#: ``snapshot_asof`` 候选键。**禁**把 ``evaluated_at`` / 结算日 / ``analysis_date``
#: 收进来 —— 那是「事后」的时刻，拿它当快照时点等于自己给自己发通行证。
SNAPSHOT_ASOF_KEYS: tuple[str, ...] = (
    "snapshot_asof",
    "data_asof",
    "market_data_asof",
    "as_of",
)
#: ``risk_veto`` 的 reason 候选键（``trigger`` 形态才用）。
VETO_REASON_KEYS: tuple[str, ...] = (
    "reason",
    "guardrail_reason",
    "downgrade_reason",
)

#: ``trigger`` 取值 → 语义（旁证上游 ``RiskControlExplanation``）。
TRIGGER_RISK_VETO: str = "risk_veto"
TRIGGER_RISK_DOWNGRADE: str = "risk_downgrade"
TRIGGER_NONE: str = "none"

#: 解析时间时要试的格式。先带偏移，再裸 datetime（按上海解释），最后纯日期。
_TS_FORMATS_WITH_TZ: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M%z",
)
_TS_FORMATS_NAIVE: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
)
_DATE_ONLY: tuple[str, ...] = ("%Y-%m-%d",)
_ISO_Z_SUFFIX = re.compile(r"[Zz]$")

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


# ===== P-arg-snap · 时间规范化（N4：秒级 + Asia/Shanghai）=====
def normalize_ts(value: object) -> datetime | None:
    """把上游各种时间表示规范成**上海时区、截断到秒**的 aware datetime。

    纪律（N4 写死，实现只此一处，别的函数不许自己 ``strptime``）：

    - 带偏移（``+08:00`` / ``Z`` / ``+0000``）→ 先解析再 ``astimezone`` 到上海；
    - 裸 datetime 无时区 → **按 Asia/Shanghai 解释**（中国行情数据本来就是本地时间，
      猜 UTC 会整体偏 8 小时，让本该 late 的快照看起来合法）；
    - 只有日期（``2026-10-27``）→ 当天上海 ``00:00:00``（N4 第 3 条：接受但会
      在文档里注明口径；比较仍走秒级）；
    - 秒以下一律截断（``microsecond=0``），不做四舍五入 —— 四舍五入会让
      ``asof = advice + 0.4s`` 变成同一秒而漏判 late。

    解析不了（``None`` / 空串 / 垃圾串 / bool / 非时间类型）一律 ``None``，
    **绝不返回"当前时间"顶替**。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)):
        # 不接 Unix 时间戳：上游给的是字符串，数字多半是误传（如 id）。
        return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if _ISO_Z_SUFFIX.search(text):
            text = text[:-1] + "+0000"
        parsed = _parse_datetime_text(text)
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ASIA_SHANGHAI)
    # 截断到秒：先转上海，再抹掉亚秒。
    return parsed.astimezone(ASIA_SHANGHAI).replace(microsecond=0)


def _parse_datetime_text(text: str) -> datetime | None:
    # ``+08:00`` 这种带冒号的偏移，``%z`` 在部分实现下不认，先归一成 ``+0800``。
    candidate = _normalize_offset(text)
    for fmt in _TS_FORMATS_WITH_TZ:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    for fmt in _TS_FORMATS_NAIVE:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    for fmt in _DATE_ONLY:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def _normalize_offset(text: str) -> str:
    """把 ``2026-10-27T10:00:00+08:00`` 的偏移改写成 ``+0800``。

    只动**末尾**的偏移，不碰日期里的连字符。没有偏移就原样返回。
    """
    match = re.search(r"([+-])(\d{2}):(\d{2})$", text)
    if not match:
        return text
    return f"{text[: match.start()]}{match.group(1)}{match.group(2)}{match.group(3)}"


def format_ts(value: datetime | None) -> str | None:
    """把规范化后的 datetime 序列化成带 ``+08:00`` 的 ISO-8601 串。"""
    if value is None:
        return None
    return value.astimezone(ASIA_SHANGHAI).replace(microsecond=0).isoformat()


# 旧名兼容：`format_ts` 与任务书里的 `normalize_ts` 是一对，
# `iso_or_none` 只是给调用点一个更直白的别名。
def iso_or_none(value: object) -> str | None:
    """任意输入 → ISO-8601(+08:00) 串或 ``None``。"""
    return format_ts(normalize_ts(value))


def is_snapshot_late(
    *,
    snapshot_asof: str | None,
    advice_generated_at: str | None,
) -> bool:
    """``snapshot_asof`` 是否**晚于**建议生成时刻（秒级、同区比较）。

    - 任一为 ``None`` → ``False``。缺失的排除靠「不计入分母」规则，
      **不靠** late 旗 —— 把缺失也标成 late 会让两种完全不同的病因
      （数据迟到 vs 根本没快照）在报告里合并，查不出根因。
    - 相等 → 不 late（N4 测例：同一秒不算迟到）。
    """
    asof = normalize_ts(snapshot_asof)
    advice = normalize_ts(advice_generated_at)
    if asof is None or advice is None:
        return False
    return asof > advice


def argument_quality_eligible(
    *,
    snapshot_asof: str | None,
    snapshot_late: bool,
) -> bool:
    """是否进入「论据质量」统计分母（N6 默认门槛）。

    写死：有结算行（调用方已保证）∧ ``snapshot_late`` 不为 ``True``
    ∧ ``snapshot_asof`` 非空。**本拍不加**「多空至少一段非空」——
    那条挂档，另口令再开。

    注意这条**不**参与 A2 的胜率分母：``snapshot_late`` 的行照样可以按
    A1–A7 计入/排除胜率分母，两套账分开算（FIX §D「与 A2 分账」）。
    """
    if snapshot_late:
        return False
    return normalize_ts(snapshot_asof) is not None


# ===== P-arg-snap · 只读解析器（N1 / N2 / N5）=====
def _raw_sources(raw: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """按优先级返回查找面：item 顶层 → ``raw_result`` → ``context_snapshot``。

    只下探这三层，且只下探**一层**（不再递归）：再深就不是"上游已有的
    旁路字段"，而是猜结构了。非 Mapping 的层直接跳过。
    """
    layers: list[Mapping[str, Any]] = [raw]
    for key in ("raw_result", "context_snapshot"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            layers.append(nested)
    return tuple(layers)


def _first_text(raw: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """按 ``keys`` 优先级取第一个**非空字符串**。空串 / 纯空白视为无。

    非字符串（数字、list、dict）一律跳过：论据是文案，数字不是论据。
    """
    for layer in _raw_sources(raw):
        for key in keys:
            value = layer.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
    return None


def parse_bull_case(raw: Mapping[str, Any]) -> str | None:
    """多头论据摘要。无候选键 → ``None``（UI 诚实说「无多头论据」）。"""
    return _first_text(raw, BULL_CASE_KEYS)


def parse_bear_case(raw: Mapping[str, Any]) -> str | None:
    """空头论据摘要。无候选键 → ``None``。"""
    return _first_text(raw, BEAR_CASE_KEYS)


def parse_advice_generated_at(raw: Mapping[str, Any]) -> str | None:
    """建议生成时刻（ISO-8601 +08:00）。

    只认 ``advice_generated_at`` / ``created_at``（后者来自
    ``AnalysisHistory.created_at``，是 FIX 写死的默认来源）。
    **不接受** ``evaluated_at`` / ``analysis_date`` / ``settle_date`` ——
    那是结算侧时间，拿它当建议时刻会把"事后"伪装成"当时"。
    """
    return iso_or_none(_first_text(raw, ("advice_generated_at", "created_at")))


def parse_snapshot_asof(raw: Mapping[str, Any]) -> str | None:
    """行情/数据快照时点（ISO-8601 +08:00）。

    只认 ``snapshot_asof`` / ``data_asof`` / ``market_data_asof`` / ``as_of``，
    且值必须能解析成时间。**禁止**用 ``evaluated_at`` / 结算日 / "今天" 顶替：
    那等于用结算后的行情回填快照，是 FIX X6 明令禁止的事。

    解析不了的值返回 ``None``（不抛错）—— 上游偶尔会塞空串或 ``"unknown"``，
    那是"没给"，不是"错了"。
    """
    for layer in _raw_sources(raw):
        for key in SNAPSHOT_ASOF_KEYS:
            if key not in layer:
                continue
            value = layer.get(key)
            if isinstance(value, str) and not value.strip():
                continue
            parsed = normalize_ts(value)
            if parsed is not None:
                return format_ts(parsed)
    return None


def parse_risk_veto(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """风控否决结论。``null`` 与 ``{veto: false}`` 是**两回事**（N5）。

    | 上游 | 输出 |
    |---|---|
    | 无风控材料 | ``None`` |
    | 明确未否决 | ``{"veto": False, "reason": None}`` |
    | 明确否决且有 reason | ``{"veto": True, "reason": "<非空>"}`` |
    | 否决但缺 reason | ``None``（非法结论不输出） |

    第二条路径看 ``trigger``（旁证上游 ``RiskControlExplanation``）：
    ``risk_veto`` → 否决，reason 缺失则整段降 ``None``；
    ``none`` → 明确未否决；``risk_downgrade`` → 整段 ``None``。

    ``risk_downgrade`` 为什么整段 ``None`` 而不是 ``{veto: False, reason: ...}``：
    降级是"仓位打折"，不是"风控放行"。输出 ``veto: False`` 会被读成
    "风控审过了、没问题"，那是比没有结论更强的断言 —— 而证据只有"降级"。
    保守起见整段留空，理由不进 ledger 否决结论。
    """
    for layer in _raw_sources(raw):
        explicit = layer.get("risk_veto")
        if isinstance(explicit, Mapping):
            parsed = _veto_from_mapping(explicit)
            if parsed is not None:
                return parsed
            # 显式对象非法（如 veto=true 缺 reason）→ 继续看 trigger；
            # 都不成立就返回 None（不猜）。
        trigger = str(layer.get("trigger") or "").strip().lower()
        if not trigger:
            continue
        if trigger == TRIGGER_RISK_VETO:
            reason = _first_text(layer, VETO_REASON_KEYS)
            if not reason:
                # 否决了却说不清为什么 → 不是结论，是缺证据。
                return None
            return {"veto": True, "reason": reason}
        if trigger == TRIGGER_NONE:
            return {"veto": False, "reason": None}
        if trigger == TRIGGER_RISK_DOWNGRADE:
            return None
    return None


def _veto_from_mapping(explicit: Mapping[str, Any]) -> dict[str, Any] | None:
    """校验显式 ``risk_veto`` 对象。非法（含 ``veto=true`` 缺 reason）返回 ``None``。"""
    veto = explicit.get("veto")
    if not isinstance(veto, bool):
        # ``"true"`` / ``1`` / 缺失 —— 都不是布尔结论。
        return None
    reason_raw = explicit.get("reason")
    reason = reason_raw.strip() if isinstance(reason_raw, str) else None
    if veto:
        if not reason:
            return None  # 禁止 {veto: true, reason: null/""}
        return {"veto": True, "reason": reason}
    return {"veto": False, "reason": reason or None}


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
    # ── P-arg-snap 增量（全部只读解析，缺则 None / False）──
    bull_case: str | None = None
    bear_case: str | None = None
    risk_veto: dict[str, Any] | None = None
    snapshot_asof: str | None = None
    advice_generated_at: str | None = None
    snapshot_late: bool = False

    @property
    def in_denominator(self) -> bool:
        return not exclude_from_denominator(outcome=self.outcome, unfillable=self.unfillable)

    @property
    def in_argument_quality_denominator(self) -> bool:
        """是否计入**论据质量**分母（N6）。与 ``in_denominator``（胜率）分账。

        两套账必须分开：``snapshot_late`` 的行照样可以算胜率（A1–A7 不关心
        论据是什么时候抓的），但它不能进论据质量统计（论据用了未来数据）。
        把两者合并会让"迟到"污染胜率，或者让"不可成交"污染论据质量。
        """
        return argument_quality_eligible(
            snapshot_asof=self.snapshot_asof, snapshot_late=self.snapshot_late
        )

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
        return (
            "close（上游原值·漏隔夜跳空）"
            if self.entry_next_open is None
            else "close 与 next_open 并列"
        )

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
            # ── P-arg-snap（键名稳定，勿改名：前端类型与测例按名对齐）──
            "bull_case": self.bull_case,
            "bear_case": self.bear_case,
            "risk_veto": self.risk_veto,
            "snapshot_asof": self.snapshot_asof,
            "advice_generated_at": self.advice_generated_at,
            "snapshot_late": self.snapshot_late,
            "in_argument_quality_denominator": self.in_argument_quality_denominator,
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
        # ── P-arg-snap：论据质量分母（N6 默认门槛），与胜率分母分账 ──
        # 分子是"有结算行 ∧ 未迟到 ∧ 有快照"。本拍**不**加「多空至少一段非空」。
        quality_rows = [r for r in self.rows if r.in_argument_quality_denominator]
        quality_denominator = len(quality_rows)
        # 质量百分比按同一门槛抑制（复用 MIN_SAMPLE_FOR_PERCENT，不另立一个数）。
        quality_suppressed = quality_denominator < MIN_SAMPLE_FOR_PERCENT
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
            "win_rate_pct": None
            if suppressed
            else round(wins / denominator * 100.0, 2)
            if denominator
            else None,
            "sample_below_threshold": suppressed,
            "min_sample_for_percent": MIN_SAMPLE_FOR_PERCENT,
            "unavailable_reason": self.unavailable_reason,
            # ── P-arg-snap 增量键（稳定名，勿改）──
            "argument_quality_denominator": quality_denominator,
            "argument_quality_excluded_late": sum(1 for r in self.rows if r.snapshot_late),
            "argument_quality_excluded_missing_snapshot": sum(
                1 for r in self.rows if r.snapshot_asof is None
            ),
            "argument_quality_suppressed": quality_suppressed,
            # 论据质量**率**本拍不给：分母门槛、多空非空门槛都还挂档。
            # 留一个恒 null 的占位键，比"以后再说"更容易被前端诚实渲染成「—」。
            "argument_quality_pct": None,
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
        # P-arg-snap：四字段全部**只读解析**上游 items，没有一个字是算出来的。
        # 缺键 → None；解析不出时间 → None。不许用 evaluated_at / 今天 顶替。
        advice_generated_at = parse_advice_generated_at(raw)
        snapshot_asof = parse_snapshot_asof(raw)
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
                bull_case=parse_bull_case(raw),
                bear_case=parse_bear_case(raw),
                risk_veto=parse_risk_veto(raw),
                snapshot_asof=snapshot_asof,
                advice_generated_at=advice_generated_at,
                snapshot_late=is_snapshot_late(
                    snapshot_asof=snapshot_asof,
                    advice_generated_at=advice_generated_at,
                ),
            )
        )
    rows.sort(key=lambda row: (row.code, row.analysis_history_id))
    return LedgerAudit(rows=tuple(rows))


def audit_unavailable(reason: str) -> LedgerAudit:
    """取不到上游账本时的 fail-closed 结果：空行 + 未审计标 + 原因。"""
    return LedgerAudit(
        rows=(),
        audit_status=AUDIT_STATUS_UNAUDITED,
        unavailable_reason=str(reason or "unavailable"),
    )


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
        status_code, payload, _media, _extra = forward(
            "GET", "backtest/results", params=list(params.items())
        )
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
            # P-arg-snap 在 A5 下挂一条并列说明（FIX §F A5：可扩展 detail，
            # **不**重做 M02，也**不**新增第 8 条把既有 7 条契约撑破）。
            "detail": (
                "证据重放登记 run_truncated_replay / assert_truncated；截断失效 fail-closed；"
                "snapshot_asof 不得晚于 advice_generated_at（秒级·Asia/Shanghai），"
                "迟到或缺失只排除论据质量，不并入 A2 胜率分母"
            ),
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
