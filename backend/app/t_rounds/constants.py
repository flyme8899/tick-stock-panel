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

T_MODES: Final = frozenset({"sell_first", "buy_first"})
"""两种做 T 方向。**按「先卖」还是「先买」命名**，不按"正/倒"。

- `sell_first`(先卖后买, 旧名 `zheng_t`): 第一腿卖出, 第二腿买回。卖出的是当日
  之前就持有的老底仓; 买回的份额来源无所谓。
- `buy_first`(先买后卖, 旧名 `dao_t`): 第一腿买入, 第二腿卖出。**卖出腿**必须是
  当日之前就持有的老底仓(`old_lot`)—— 当日买入的份额当日不能卖, 那是 T+1 违规。

改名理由(批次 20261025gtrounds): 旧名的"正/倒"没有说清哪一腿受 T+1 约束, 于是
校验被写在了 `zheng_t` 的第二腿(那是**买入**腿)上 —— 真正的卖出腿反而无人检查。
按"先卖/先买"命名后, 约束指向"哪条腿是 sell"就一目了然。
"""

T_MODE_ALIASES: Final[dict[str, str]] = {
    "zheng_t": "sell_first",
    "dao_t": "buy_first",
}
"""旧名 → 新名。**仅供迁移期调用方与历史记录读取**。

新代码一律用 `sell_first` / `buy_first`。本包内部不读这张表, 它只给"读进一条
历史记录"的场景用; 写入侧永远写新名。
"""

SELL_FIRST: Final = "sell_first"
BUY_FIRST: Final = "buy_first"

ROUND_STATUSES: Final = frozenset(
    {
        "open_leg1",              # 第一腿已成交, 第二腿未完成
        "open_leg2",              # 第二腿已成交, 轮次未结算
        "completed",              # 两腿齐、净价差已算
        "unreverted_at_cutoff",   # 到 14:57 仍没回摆(策略没等到) —— 可计 mtm
        "data_truncated",         # 14:57 之前数据就断了(数据问题) —— 先分因
        "limit_locked",           # 涨跌停锁死, 无法按收盘价平 —— 留分母, 不计 mtm
        "expired_review",         # 超时未结算, 等人工复核
        "void_data",              # 数据异常, 整轮作废
    }
)
"""轮次状态。**终态名与语义必须一一对应**, 别按注释倒着理解。

| 状态 | 语义 | 能不能计 mtm |
|---|---|---|
| `unreverted_at_cutoff` | 交易时段走完了, 策略没等到回摆 | ✅ 按 `ts<=14:57:00` 最后一根 `price` |
| `data_truncated` | 交易时段**没走完**数据就断了 | ❌ 先分因; 有可靠收盘价再说 |
| `limit_locked` | 走到截点仍涨跌停锁死, 平不掉 | ❌ **留在分母**; 不剔除、不算数据问题 |
| `void_data` | 数据异常整轮作废 | ❌ |

`limit_locked` 为什么单列而不是塞进 `data_truncated`: 数据是全的, 只是那个价位
没有对手盘。把流动性问题记成数据问题, 后果是"卡在跌停板上出不来的亏损"被下次
数据补齐时悄悄抹掉 —— 那正好是最该被看见的一类。它也**留在期望值分母里**。

旧名 `incomplete` / `abandoned` 在 tip 上与 docstring **对调**过(见批次
20261025gtrounds 的修法记录), 这正是"没等到回摆就被算成废弃、且不给 mtm"的
根因 —— 只统计 `completed` 会系统性高估策略, 因为等不到回摆的那些轮次被当成
"没发生"而不是"发生了但没赚到"。
"""

TERMINAL_STATUSES: Final = frozenset(
    {
        "completed",
        "unreverted_at_cutoff",
        "data_truncated",
        "limit_locked",
        "expired_review",
        "void_data",
    }
)

LEGACY_STATUS_ALIASES: Final[dict[str, str]] = {
    "incomplete": "data_truncated",
    "abandoned": "unreverted_at_cutoff",
}
"""旧终态名 → 新名。**仅供读取历史记录**, 含义按旧名当时的语义映射:

- 旧 `incomplete` 的代码分支是"数据在 14:57 前就结束了" → `data_truncated`
- 旧 `abandoned` 的代码分支是"走完了但没等到回摆" → `unreverted_at_cutoff`

映射依据是**代码分支**而不是旧 docstring —— 两者在 tip 上是对调的, 以代码为准。
"""

REALIZED_KPI_ELIGIBLE_STATUS: Final = frozenset({"completed"})
"""**只有** `completed` 能进已实现 KPI。

写成集合而不是 if 链, 是为了让"哪些状态能进 KPI"只有一个定义处 —— 将来加状态时
不改这里就不会误入 KPI。
"""

LIMIT_LOCK_SOURCE_PENDING: Final = "data_pending"
"""涨跌停判定的数据来源 —— **本批次没有**, 显式标 pending。

判断"这一轮是不是停在涨跌停上"需要有每日涨跌停价, 或者至少"收盘价是否等于涨停/
跌停价"的标记。当前特征表只有 `price` / `vwap`, **没有**这两样, 所以:

- 本包**不猜**涨跌停(按百分比阈值反推属于编数据, 会把"刚好跌 10%"的普通下跌
  错记成锁死);
- 报告里 `limit_lock_source` 显式写 `data_pending`;
- `limit_locked` 这个桶先建好、计数为 0, 等涨跌停数据接上就能直接填。

不许因为"反正都是 0"就把这个桶删掉 —— 删掉之后接数据的人会以为从没考虑过。
"""

LEG_STATUSES: Final = frozenset({"pending", "filled", "cancelled", "rejected", "unfilled"})

LEG_SIDES: Final = frozenset({"buy", "sell"})

SOURCE_LOTS: Final = frozenset({"old_lot", "same_day_buy"})
"""份额来源。

**约束挂在卖出腿上, 不挂在"第二腿"上。** 哪种模式都通用: 两条腿里凡 `side=="sell"`
的那条, 份额来源必须是 `old_lot` —— 当日买入的份额当日不能卖(T+1)。

- `sell_first`: 卖出腿是 leg1 → leg1 来源约束(纸面底仓恒 `old_lot`)
- `buy_first`:  卖出腿是 leg2 → leg2 来源约束

旧实现只检查 `zheng_t` 的 `leg2`(那是**买入**腿), 真正的卖出腿无人校验 —— 等于
把护栏装在了不承重的那条腿上。见批次 20261025gtrounds 修法记录。
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

**注意别和 `COST_BP_TIERS` 混为一谈。** 两边都含 `"design_pending"` 这个字符串,
但它们回答的是两个不同问题: 这里说的是"卖出价按哪个价算"(价参照), 那边说的是
"这一轮按几个 bp 扣成本"(成本档位)。因为浮在同一个字面量上, 很容易有人以为
改一处另一处自动跟着走 —— 实际不会, 也不该会: 卖出价参照与成本档位没有任何
推导关系。要新增成本档位, 改的是 `COST_BP_TIERS`, 不是这里。
"""

# ---------------------------------------------------------------- 做 T 成本档位

COST_BP_TIERS: Final = frozenset(
    {
        "design_pending",  # 未实测 / 占位: 不宣称已按费率扣过成本
        "0bp",             # 显式零成本(0%)
        "5bp",             # 5 bp = 0.05%
        "10bp",            # 10 bp = 0.10%
        "15bp",            # 15 bp = 0.15%
        "20bp",            # 20 bp = 0.20%
    }
)
"""`cost_bp_tier` 的**唯一**合法取值集合(做 T 成本闸门 5b)。

`complete_round` 的闸门判据就是 `cost_bp_tier in COST_BP_TIERS` —— 白名单之外
一律 `TRoundValidationError`, 包括拼错的和"看着很合理"的新档位。**单一定义**:
别处不要再抄一份字面量, 抄出来的第二份迟早与这份漂移, 而漂移的表现是"某个档位
在一个入口能过、在另一个入口报错", 这种不一致比直接不支持更难查。

## 为什么 `design_pending` 也在白名单里

它是**占位**, 不是"已测成本", 但缺省值必须能过闸门: 分档成本实测属于另一拍
(5c 敏感性跑表), 在它落地之前, 绝大多数轮次本来就只能挂这个值。把它留在白名单
里不等于宣称它已测 —— 语义仍然只有一句: "这一轮没有按费率扣成本"。

## 档位与成本**金额**的关系(闸门同时校验, 见 `state_machine.complete_round`)

| 档位 | `cost_amount` 规则 |
|---|---|
| `design_pending` | `>= 0`, 含 0; **不**要求与费率公式一致(占位) |
| `0bp` | **必须 == 0** —— 显式零成本, 写着 0bp 却扣了钱是自相矛盾 |
| `5bp`/`10bp`/`15bp`/`20bp` | **必须 > 0** —— 禁止非零档位配 `amount=0` 冒充"已测" |

最后一行是这一拍最要紧的地方: 如果 `10bp` 允许 `amount=0`, 那么一份"按 10bp
扣成本"的报告与"根本没扣成本"的报告在数字上完全一致, 而前者会被当成已经考虑过
成本的结论。宁可拒绝, 也不让这种静默通过。

## 换算落在调用方

"费率 × 名义金额 → `cost_amount`"的换算**不在**本包里: `complete_round` 只扣
绝对额, 不碰费率。这是刻意的 —— 名义金额取哪个价(成交价 / 均价 / 收盘价)、
手续费是否有最低 5 元、印花税卖出单边收取, 这些口径一旦写进闸门, 就变成"闸门
顺手替你选了口径"。换算与口径留在离线跑表那一侧, 闸门只管"档位合法 + 金额不矛盾"。

## 本拍边界

- 本拍(5b)只放开**契约**: 白名单 + 金额规则。offline 的 `_settle` 仍走
  `design_pending` + `0.0`, 不强制改。
- 成本敏感性跑表(0 / 0.05% / 0.10% / 0.15% / 0.20%)属于 **5c**, 另一拍。
- 散户双边成本 0.1%–0.15% 只是**假设口径**, 不是唯一真实成本; 报告与对外表述
  必须标明这是假设。

仅供研究, 不构成投资建议; 执行须人工确认。
"""

ALLOWED_COST_BP_TIERS: Final = COST_BP_TIERS
"""`COST_BP_TIERS` 的只读别名。

两个名字指向**同一个** frozenset 对象(不是拷贝), 所以不存在"改了一个另一个没跟上"
的可能。存在两个名字只是因为读代码的人有时会先想"允许哪些" —— 别名省去一次跳转,
但**任何新增档位只能改 `COST_BP_TIERS`**。
"""

# ---------------------------------------------------------------- 持仓标签

POSITION_TAGS: Final = frozenset(
    {
        "executable",    # 有可卖老底仓, 可执行 sell_first(先卖后买)
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

LUNCH_LAST_MORNING_BAR: Final = "11:30"
"""上午末根(连续竞价结束那根)。午休从这一根之后开始。"""

LUNCH_FIRST_AFTERNOON_BAR: Final = "13:01"
"""下午首根。

**不是 13:00** —— 13:00 是开市时刻, 第一根 **1 分钟** K 的结束标注是 13:01
(bar 记的是结束时刻)。写成 13:00 会让每一份正常数据都报错。
"""

LUNCH_GAP_MINUTES: Final = 91
"""11:30 → 13:01 的分钟差。

与在线 `backend/app/api/kline.py` 的 `_LUNCH_GAP_MIN = 91` **同一口径**, 刻意取同一个数,
免得离线判定和在线补洞对"午休多长"各持一词。11:30 + 91 分钟 = 13:01。
"""

__all__ = [
    "ALLOWED_COST_BP_TIERS",
    "AUCTION_IMPERSONATION_FORBIDDEN",
    "BUY_FIRST",
    "COST_BP_TIERS",
    "EPSILON_DEFINITION",
    "EPSILON_LOCKED_IN_VERSION",
    "EPSILON_VALUE_PCT",
    "FORBIDDEN_NAMES",
    "FORMAL_PUSH_ALLOWED_DEFAULT",
    "LEGACY_STATUS_ALIASES",
    "LEG_SIDES",
    "LEG_STATUSES",
    "LIMIT_LOCK_SOURCE_PENDING",
    "LUNCH_FIRST_AFTERNOON_BAR",
    "LUNCH_GAP_MINUTES",
    "LUNCH_LAST_MORNING_BAR",
    "MAX_ROUNDS_PER_DAY",
    "MINUTE_EXPORT_EARLIEST",
    "NAMESPACE",
    "PAPER_TRACK_ONLY_DEFAULT",
    "POSITION_TAGS",
    "REALIZED_KPI_ELIGIBLE_STATUS",
    "RESEARCH_WINDOW_START",
    "ROUNDS_CONSUMED_FIELD",
    "ROUND_STATUSES",
    "SELL_FIRST",
    "SIGNAL_UNIVERSE_FILTERED_BY_POSITION",
    "SOURCE_LOTS",
    "STAMP_TAX_PRICE_REFS",
    "TERMINAL_STATUSES",
    "T_MODES",
    "T_MODE_ALIASES",
    "T_MONITOR_EVENT_TYPE",
    "T_ROUND_DESIGN_PIN",
    "VWAP_SAME_BASIS_NOTE",
    "design_version",
]
