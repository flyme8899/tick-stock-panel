"""做 T 轮次骨架的元数据常量。

这个包与 `app.buy_sell_signals` **全面隔离**, 与 `app.services.trade_marks` /
`app.news.push` 的提醒层常量**没有**任何 import 关系。

## 为什么要隔离

g(买卖信号)与 h(做 T 轮次)回答的是两个不同问题: g 判"这个形态值不值得发布",
h 记"这一轮 T 做成了没有、净价差多少"。两者共享命名空间, 后果是有一天有人要
按信号统计胜率, 结果把做 T 轮次也算进去了 —— 而这两个口径的分母根本不是一回事。

## 冷却 / 轮次上限: 与 g 和提醒层三方分列

本仓现在有三种"限制重复"的东西, 名字必须分开:

| 名字 | 语义 | 归属 |
|---|---|---|
| `T_COOLDOWN_MIN = 60` | 同一只票一天内不再出一**轮**做 T(旧提醒层说法) | 提醒层 |
| `push_dedupe_cooldown_until` | 同一条**信号**别重复播报 | g 提醒层去重 |
| `max_rounds_per_day` / `rounds_consumed_on_trade_date` | 单日每票最多做几**轮 T** | 本包 |

上一行那个 `T_COOLDOWN_MIN` 与本包的 `max_rounds_per_day=1` 语义上像是同一件事,
但**不是一个东西**: 前者是提醒层的节流参数(分钟级), 后者是轮次计数(次/日)。
硬编码或 import 前者的数值, 会让"调整推送频率"悄悄改掉"一天能做几轮 T"。
**禁止** import / 拷贝 `VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`。

## ε = 0.30%

`EPSILON_VALUE_PCT = 0.30` 指**相对均价 0.30%**(≈30 bp), 来自 PARAM_NAIL
`design_pin_v1`。它锁定在 `version` 字符串里, 看一条记录就能知道它在哪个 ε 下成立。

本批次**只落常量与字段, 不跑分钟评估**, 所以不涉及"用什么均价"的问题。但注释
必须提前写清楚(见下 `VWAP_SAME_BASIS_NOTE`)。

## 六条软提醒(来自 MINUTE_CALIBER_CHECK + REVIEW, 不得删)

1. **同尺后再 VWAP** —— 见 `VWAP_SAME_BASIS_NOTE`
2. **禁拷提醒常量** —— 见上
3. **冷却分列** —— 见上
4. **virtual 占位** —— `virtual_position_flag` 可为 true, 但不得宣称实盘可卖老股已通
5. **研究窗 ≥2025-07-25** —— 见 `RESEARCH_WINDOW_START`
6. **禁冒充竞价** —— 见 `AUCTION_IMPERSONATION_FORBIDDEN`

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from datetime import date
from typing import Final

# ---------------------------------------------------------------- 命名空间

NAMESPACE: Final = "t_rounds"
"""存储 / 日志 / 未来表的命名空间前缀。

与 `buy_sell_signals` 分开。任何跨包引用都必须带前缀, 不允许出现裸的
`t_round_registry` 之类可能撞车的名字。
"""

T_ROUND_DESIGN_PIN: Final = "design_pin_v1"

# ---------------------------------------------------------------- ε

EPSILON_DEFINITION: Final = "pct_of_vwap_or_mean"
"""ε 的定义: 相对均价(或 VWAP)的百分比。"""

EPSILON_VALUE_PCT: Final = 0.30
"""ε = 0.30%(≈30 bp)。PARAM_NAIL `design_pin_v1`。**本包唯一的 ε 数值。**"""

EPSILON_LOCKED_IN_VERSION: Final = True
"""ε 是否 locked 进 version 串。True → `design_version()` 里带 `eps_pct:0.30`。

这样一条记录离开它诞生时的代码之后, 仍然能读出"它是在哪个 ε 下判定的"。事后补
这个信息是补不回来的 —— 除非把当时的代码翻出来。
"""


def design_version() -> str:
    """拼出可写进 `TRoundRecord.version` 的版本串。

    形如 ``design_pin_v1+eps_pct:0.30``。
    """
    return f"{T_ROUND_DESIGN_PIN}+eps_pct:{EPSILON_VALUE_PCT:.2f}"


# ---------------------------------------------------------------- 轮次上限

MAX_ROUNDS_PER_DAY: Final = 1
"""单日每票最多 1 轮。**恒 1**, 不是可配置项。

"一天做多轮 T" 在纸面回测里几乎总是好看的(多做几次总能摊到一次成功的), 但它
要求每次都能在同一个日内区间里重复进出 —— 实盘上滑点和冲击成本会把这点优势吃光。
所以先锁 1 轮, 要放开必须有分档成本实测支撑(本轮不做)。
"""

ROUNDS_CONSUMED_FIELD: Final = "rounds_consumed_on_trade_date"
"""轮次计数用 `count_rounds()` 现算, 不落独立计数列。

之所以把这个名字常量化: 是为了让"轮次计数"有一个明确叫法, 与
`push_dedupe_cooldown_until` 彻底分开。任何地方要引用"今天这只票用掉几轮",
都用这个名字, 不叫 `cooldown`。
"""

FORBIDDEN_NAMES: Final = (
    "push_dedupe_cooldown_until",
    "cooldown_until",
    "T_COOLDOWN_MIN",
    "VWAP_BAND",
    "RANGE_BAND",
)
"""本包**字段名与符号**里不得出现的名字。测试逐项扫。"""

# ---------------------------------------------------------------- 模式 / 状态

T_MODES: Final = frozenset({"zheng_t", "dao_t"})
"""正 T / 倒 T。

- `zheng_t`(正 T): 先卖后买。第二腿买回, **必须**用当日之前就持有的老底仓
  (`leg2_source_lot="old_lot"`)—— 否则就是把当日买入的票当日卖出, 那是 T+1 违规。
- `dao_t`(倒 T): 先买后卖。第二腿卖出的是当日买入的份额, 受 T+1 约束, 只能做
  纸面记录(`virtual_position_flag=True`)或等到次日。
"""

ROUND_STATUSES: Final = frozenset(
    {
        "open_leg1",      # 第一腿已成交, 第二腿未完成
        "open_leg2",      # 第二腿已成交, 轮次未结算
        "completed",      # 两腿齐、净价差已算
        "incomplete",     # 第二腿没做成(未达门禁 / 撤单)
        "abandoned",      # 主动放弃
        "expired_review", # 超时未结算, 等人工复核
        "void_data",      # 数据异常, 整轮作废
    }
)

TERMINAL_STATUSES: Final = frozenset({"completed", "incomplete", "abandoned", "expired_review", "void_data"})

REALIZED_KPI_ELIGIBLE_STATUS: Final = frozenset({"completed"})
"""**只有** `completed` 能进已实现 KPI。

写成集合而不是 if 链, 是为了让"哪些状态能进 KPI"只有一个定义处 —— 将来加状态时
不改这里就不会误入 KPI。
"""

LEG_STATUSES: Final = frozenset({"pending", "filled", "cancelled", "rejected", "unfilled"})

LEG_SIDES: Final = frozenset({"buy", "sell"})

SOURCE_LOTS: Final = frozenset({"old_lot", "same_day_buy"})
"""第二腿的份额来源。

`same_day_buy` 在 `zheng_t` 下是**违规**的: 正 T 第二腿是买回, 卖掉当日买入的
份额再买回, 语义上就成了"用当天买的票做正 T", 而当天买的票本来就不能卖。
"""

# ---------------------------------------------------------------- T+1 / 可卖

STAMP_TAX_PRICE_REFS: Final = frozenset(
    {
        "leg1_price",        # 正 T 第二腿固定按第一腿成交价(简化假设)
        "market",            # 按市价
        "design_pending",    # 尚未定
    }
)
"""印花税 / 卖出价的参照语义。

印花税只在卖出端收, 所以正 T(先卖后买)与倒 T(先买后卖)的税费结构不同。这里的
枚举只说明"卖出价按什么取", 不做任何计算 —— 分档成本实测本轮不做。
"""

# ---------------------------------------------------------------- 持仓标签

POSITION_TAGS: Final = frozenset(
    {
        "executable",    # 有可卖老底仓, 可执行正 T
        "observe_only",  # 只能观察, 不能执行
        "tomorrow_ref",  # 当日买入, 次日才可卖
    }
)
"""持仓三态。**信号宇宙不按持仓过滤** —— 见 `SIGNAL_UNIVERSE_FILTERED_BY_POSITION`。"""

SIGNAL_UNIVERSE_FILTERED_BY_POSITION: Final = False
"""信号宇宙**不**按持仓过滤, 恒 False。

按持仓过滤会让回测的样本集随每个人的账户变化 —— 同一个策略在不同人手里跑出
不同结论, 而谁都说不清差异来自策略还是来自持仓。持仓只影响 `position_tag`
(能不能执行), 不影响"这只票今天该不该进候选"。
"""

# ---------------------------------------------------------------- 推送 / 监控占位

FORMAL_PUSH_ALLOWED_DEFAULT: Final = False
PAPER_TRACK_ONLY_DEFAULT: Final = True
T_MONITOR_EVENT_TYPE: Final = "t_round_status_changed"

# ---------------------------------------------------------------- 软提醒(常量形式)

RESEARCH_WINDOW_START: Final = date(2025, 7, 25)
"""离线研究窗建议起点。

全量分钟数据大致自此日起可用。**缺日就跳过, 禁止回退到最近分区** —— 回退会把
另一个交易日的数据当成今天, 而这种错误在结果上表现为"某几天信号特别准"。
`null ≠ 0`: 缺数据填 null, 不填 0。
"""

VWAP_SAME_BASIS_NOTE: Final = (
    "同尺后再 VWAP: 离线导出若用 OHLC + amount 派生 VWAP, 必须先把价格与成交额"
    "对齐到同一复权口径再计算; 禁止未对齐就宣称 ε 门禁已测。优先使用 "
    ".raw_basis / price_basis 路径。"
)
"""软提醒 1。本包不实现该计算, 但任何将来的实现都必须先满足这条。"""

AUCTION_IMPERSONATION_FORBIDDEN: Final = True
"""软提醒 6: 分钟导出最早多为 09:30。**禁止**用开盘第一根 1 分钟 K 冒充 09:25 竞价。

集合竞价撮合与连续竞价是完全不同的信息集。混用的后果是竞价类统计全部失真,
而且从结果上看不出来 —— 你要去逐条核对数据来源才会发现。
"""

MINUTE_EXPORT_EARLIEST: Final = "09:30"
"""分钟导出最早时间。任何声称有 09:25 数据的路径都必须先证明来源, 见上。"""

__all__ = [
    "AUCTION_IMPERSONATION_FORBIDDEN",
    "EPSILON_DEFINITION",
    "EPSILON_LOCKED_IN_VERSION",
    "EPSILON_VALUE_PCT",
    "FORBIDDEN_NAMES",
    "FORMAL_PUSH_ALLOWED_DEFAULT",
    "LEG_SIDES",
    "LEG_STATUSES",
    "MAX_ROUNDS_PER_DAY",
    "MINUTE_EXPORT_EARLIEST",
    "NAMESPACE",
    "PAPER_TRACK_ONLY_DEFAULT",
    "POSITION_TAGS",
    "REALIZED_KPI_ELIGIBLE_STATUS",
    "RESEARCH_WINDOW_START",
    "ROUNDS_CONSUMED_FIELD",
    "ROUND_STATUSES",
    "SIGNAL_UNIVERSE_FILTERED_BY_POSITION",
    "SOURCE_LOTS",
    "STAMP_TAX_PRICE_REFS",
    "TERMINAL_STATUSES",
    "T_MODES",
    "T_MONITOR_EVENT_TYPE",
    "T_ROUND_DESIGN_PIN",
    "VWAP_SAME_BASIS_NOTE",
    "design_version",
]
