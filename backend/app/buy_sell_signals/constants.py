"""买卖信号骨架的元数据常量。

这个包**故意**与 `app.services.trade_marks`、`app.news.push` 没有任何 import 关系。

原因不是洁癖: 那两个模块里的 `VWAP_BAND = 0.015`、`RANGE_BAND = 0.003`、
`T_COOLDOWN_MIN = 60` 是**提醒层**参数 —— 它们回答「这条消息值不值得现在弹出来」。
信号门禁回答的是另一个问题: 「这个形态是不是一个有效信号」。把提醒层的阈值搬进
门禁, 等于让推送静音策略悄悄改掉信号的判定标准, 而且改的时候没有任何人会觉得
自己动了信号。

数值上可能碰巧相等, 语义上没有任何关系。所以这里重新定义、独立演进, 并且
`tests/test_buy_sell_signal_registry.py` 里有一条静态检查守着「本包不得 import
这三个符号」。

## 冷却字段为什么叫 `push_dedupe_cooldown_until`

提醒层的「冷却」在本仓至少有两种不同含义:

- `T_COOLDOWN_MIN`: 做 T 里"同一只票一天之内不再出一轮", 是**轮次上限**
- `push_dedupe_cooldown_until`: 同一信号在推送通道里别重复播报, 是**去重**

这两件事只要共用一个名字, 将来做 T 分支(旁支 h)与本包混在一起时就一定会有人
搞错。所以本包只用后者, 并且名字里带 `push_` 前缀, 明确它属于提醒层而不属于信号层。

## Bonferroni m=6

`BONFERRONI_M = 6` 来自 PARAM_NAIL, 对应 `independent_family_v1` 的家族划分:
6 个独立家族各做一次检验, 显著性门槛 α_family = 0.05 / 6。

本批次**只把它写进元数据**, 不跑 p 值、不做显著性判定。写进 `version` 字符串是为了
让以后任何一条信号都能追溯"它是在哪个多重检验口径下被声明的"——事后补这个信息是
补不回来的。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------- 命名空间

NAMESPACE: Final = "buy_sell_signals"
"""存储 / 日志 / 未来表的命名空间前缀。

与 `trade_marks` 分开、与未来做 T(旁支 h)分开。任何跨包引用都必须带这个前缀,
不允许出现裸的 `signal_registry` 之类可能撞车的名字。
"""

# ---------------------------------------------------------------- 落盘(可选)

PERSIST_ENV_NAME: Final = "BUY_SELL_SIGNALS_PERSIST"
"""开启落盘的**唯一**环境变量名。

只认这一个名字(批次 20261025gp 引入)。未设或假 → `InMemorySignalRegistryStore`,
不落盘。生产是否置 1 另等部署口令, 不得随合入默开。

**落盘 ≠ 推送**: 开了也只是把注册表写进本地 JSON, 没有 SSE / 钉钉 / `news.push`。
"""

PERSIST_SUBDIR: Final = "buy_sell_signals"
"""落盘子目录名(写死), 挂在 `settings.data_dir` 之下。

不写 `partition_writer` 的生产分区, 也不碰 `/data/tsp-reports`。
"""

REGISTRY_FILENAME: Final = "registry.json"
"""注册表主文件名(写死)。与 lock 等旁路文件同目录, 不散落。"""

REGISTRY_SCHEMA_VERSION: Final = "1"
"""`registry.json` 的格式版本(字符串)。改动文件结构时 bump, 供 reload 将来做迁移。

用字符串而非 int 是有意的: 数值型常量会落进「本包常量不得与 t_rounds 门禁同值」
那条静态扫描, 而 `"1"` 与 `t_rounds.MAX_ROUNDS_PER_DAY = 1` 毫无关系 —— 版本号
是文件格式标签, 不是阈值。写成 int 会长期误报, 逼后人给版本乱跳数字。
"""

# ---------------------------------------------------------------- 推送(可选, 20261025gpush)

PUSH_ENV_NAME: Final = "BUY_SELL_SIGNALS_PUSH"
"""开启推送的**唯一**环境变量名(批次 20261025gpush 引入)。

真值集合与 `PERSIST_ENV_NAME` 完全一致(`1` / `true` / `yes` / `on`, 大小写与首尾
空白归一), 但**两个 env 互不推导**:
- `BUY_SELL_SIGNALS_PERSIST` 管记录活得比进程久
- `BUY_SELL_SIGNALS_PUSH` 管状态变更播出去

未设 → `try_push` 一行都不写。生产是否置 1 **另等部署口令**, 不得随合入默开。

站内/SSE 两路**写死**(复用 `QuoteService.push_alerts` + `alert_store`)。外部通道
另由 `DINGTALK_ENV_NAME` 二次门控(批次 20261025gding), **不**内置在本门里。
飞书**已取消**(批次 20261025gding 起从路线图去掉, 不再排期) —— 本常量没有任何
"通道选择"的开关位, 不要在这里加一个 `PUSH_CHANNELS` 之类的可配置项。
"""

MONITOR_EVENT_TYPE: Final = "buy_sell_signal_status_changed"
"""写进告警事件的 `monitor_event_type`, 供前端/订阅者按类型分流。

与既有告警的 `type` 字段并存: `type` 用同一个值, `monitor_event_type` 是更明确的
检索键。不与管理中心既有规则的事件类型撞名。
"""

PUSH_PAYLOAD_SCHEMA_VERSION: Final = "1"
"""推送载荷格式版本(字符串, 理由同 `REGISTRY_SCHEMA_VERSION`)。"""

# ---------------------------------------------------------------- 钉钉(可选)

DINGTALK_ENV_NAME: Final = "BUY_SELL_SIGNALS_DINGTALK"
"""钉钉**支路**的开关环境变量名(批次 20261025gding 引入)。真值集合同 persist / push。

**与 `PUSH_ENV_NAME` 是"与"关系, 不是替代**:

    真实外发到钉钉群 = push_env_enabled() and dingtalk_env_enabled() and webhook 非空

为什么不把钉钉直接挂在 PUSH 门下: 生产 `BUY_SELL_SIGNALS_PUSH=1` 早已打开, 而
`DINGTALK_WEBHOOK_URL` 多半也为资讯推送配好了。若钉钉只认 PUSH 门, 本批代码一合入、
一重启, 就会**立刻**往既有群里外发 —— 站内消息发错了刷新一下就没了, 群里发错了收不回来。
独立门把"外部副作用"与"站内/SSE"解耦: 想发钉钉, 必须显式再多开一道门。

默认关。生产置 1 **另等部署口令**, 合入 ≠ 开钉钉。
"""

# ---------------------------------------------------------------- 设计钉版本

SIGNAL_DESIGN_PIN: Final = "design_pin_v1"
"""信号设计口径的版本钉。

含义: 从"哪些字段进注册表"到"状态机允许怎么走"这一整套设计, 当前是第几版。
改动 `models.SignalRecord` 的必填字段、或改动 `state_machine` 的转移表, 都要 bump。

与 `indicators/pipeline.py` 的 `LIMIT_FORMULA_VERSION` 同一性质: 它是给人看的标签,
真正的判据在代码与测试里。
"""

# ---------------------------------------------------------------- 多重检验口径

BONFERRONI_SCHEME_ID: Final = "independent_family_v1"
BONFERRONI_M: Final = 6
BONFERRONI_FAMILY_ALPHA: Final = 0.05 / BONFERRONI_M
"""α_family = 0.05 / 6 ≈ 0.008333。

**仅供元数据展示与文档引用**, 本批次不做 p 值计算、不做显著性门禁。

## 分母已钉 ≠ 显著性已测(批次 20261025gwlist 补充)

`m = 6` 钉死的是"将来算 p 值时该除以几", 它把多重检验的**分母**固定下来, 让不同批次
算出来的显著性可以横向比较。但它本身**不含任何 p 值**, 也不含任何"哪个信号显著"的结论。

这条区分在讲白名单锁定时尤其要紧: 真锁的门槛 D 要求「买入日聚类 t + Bonferroni」的
**实测 p 值过线**。现在分母有了、p 值没有, 状态是 `data_pending`, 不能因为
`BONFERRONI_M` 存在就认为显著性那一关已经过了。见 `quality_rank.WHITELIST_LOCK_EVIDENCE`。
"""


def design_version() -> str:
    """拼出可写进 `SignalRecord.version` 的版本串。

    形如 ``design_pin_v1+independent_family_v1:m6``。把 Bonferroni 口径拼进来是
    刻意的: 版本串会跟着记录一起落盘(将来), 事后能直接读出"这条信号是在 m=6 的
    多重检验口径下声明的", 而不需要去翻当时的 git log。
    """
    return f"{SIGNAL_DESIGN_PIN}+{BONFERRONI_SCHEME_ID}:m{BONFERRONI_M}"


# ---------------------------------------------------------------- 枚举取值

SIGNAL_FAMILIES: Final = frozenset({"minute_event", "daily_fundamental", "composite"})
"""信号家族。决定这只信号靠什么数据、走什么验证路径。"""

EVENT_CLASSES: Final = frozenset(
    {
        "auction",            # 竞价: 09:25 集合竞价定盘那一刻的信息
        "intraday_volume",    # 盘中放量
        "limit_reopen",       # 炸板 / 回封
        "daily_quality_value",# 日线质量与估值
        "other",
    }
)

# ---------------------------------------------------------------- 竞价 / 开盘后噪声

AUCTION_RULE_ID_NONE: Final = None
"""非竞价类信号的 `auction_rule_id` 取值。

用 `None` 而不是空串: 空串看起来像"有规则但名字是空的", `None` 才明确"这一类
信号根本不经过竞价规则"。
"""

AUCTION_STAT_BUCKET_0925: Final = "auction_0925"
"""09:25 集合竞价统计桶。竞价类信号的统计只能落进这个桶, 且只有这一个桶。

**本批次不做竞价回测**。这里定义常量是为了让字段有合法的封闭取值, 而不是为了让
谁现在就去算它。特别地: **不允许**用 09:30 之后第一根 1 分钟 K 线冒充 09:25 ——
两者是完全不同的信息集(集合竞价撮合 vs 连续竞价), 混用会让竞价类信号的统计
全部失真。
"""

POST_OPEN_NOISE_BUCKETS: Final = frozenset(
    {
        "open_0930_1000",     # 开盘半小时, 昨收缺口与情绪释放最集中
        "intraday_1000_1430", # 常规盘中
        "close_1430_1500",    # 尾盘, 资金调仓
    }
)
"""开盘后噪声档的**封闭**取值域。

封闭是刻意的: 往这里加一个桶, 等于宣布"这个时间段的成交性质我们已经定义清楚了",
而实际上每个桶的边界都该有证据支撑。

PHASE3 §3 里的示意名 `post_open_15m` **尚未**进入这个集合。批次 20261025gauction
**不扩集** —— 放量突破的噪声档只做空桩(`False` / `None`), 用哪个桶是另一拍的事。
把示意名当已测口径塞进来, 会让"封闭集"这个约束从内部失效。
"""

NOISE_MIX_FORBIDDEN: Final = True
"""竞价桶与开盘后噪声桶**不得混算**。

这是硬约束而不是配置项: 两类样本的统计性质不同(竞价是离散撮合、开盘后是连续
成交), 合并算出来的均值/命中率没有解释意义。所以写成了模块级常量而不是记录字段
—— 记录上留一个可配置开关, 迟早会有人把它打开。
"""

# ---------------------------------------------------------------- 冷却(提醒层, 非信号层)

PUSH_DEDUPE_COOLDOWN_FIELD: Final = "push_dedupe_cooldown_until"
"""提醒层去重字段名。语义: 在此之前不要重复推送同一信号。

**不是**做 T 的"单日每票 1 轮"上限。两者互不引用、互不推导。
"""

PUSH_DEDUPE_COOLDOWN_MIN: Final = 30
"""默认的推送去重窗口(分钟)。

与 `trade_marks.T_COOLDOWN_MIN = 60` **没有关系**: 那个是"同一只票一天内不再出
新一轮做 T", 语义是轮次上限; 这个是"同一条信号别重复播报", 语义是去重。
数值不同是结果, 不是目的 —— 就算将来碰巧改成一样, 也不得合并成一个常量。
"""

# ---------------------------------------------------------------- 行业口径

INDUSTRY_STANDARD_SW_L1: Final = "sw_l1"
"""默认行业口径: 申万一级。

**PIT 来源只能用 `sw_pit` 里那套 history × l1_map**。同花顺当前态
(`ext_hy_ths` / `industry_ths`) 是快照, 没有 valid_from/valid_to, 拿它当历史
归属就是在用未来的行业分类污染过去的信号 —— 而这类污染从结果上完全看不出来。
"""

INDUSTRY_STANDARD_THS_CURRENT: Final = "ths_current"
"""同花顺当前态。**不被本包接受**, 仅用于在错误信息里指名道姓地说清拒绝的是什么。"""

# ---------------------------------------------------------------- 合规

DISCLAIMER_KEY: Final = "research_only_v1"

__all__ = [
    "AUCTION_RULE_ID_NONE",
    "AUCTION_STAT_BUCKET_0925",
    "BONFERRONI_FAMILY_ALPHA",
    "BONFERRONI_M",
    "BONFERRONI_SCHEME_ID",
    "DINGTALK_ENV_NAME",
    "DISCLAIMER_KEY",
    "EVENT_CLASSES",
    "INDUSTRY_STANDARD_SW_L1",
    "INDUSTRY_STANDARD_THS_CURRENT",
    "MONITOR_EVENT_TYPE",
    "NAMESPACE",
    "NOISE_MIX_FORBIDDEN",
    "PERSIST_ENV_NAME",
    "PERSIST_SUBDIR",
    "POST_OPEN_NOISE_BUCKETS",
    "PUSH_DEDUPE_COOLDOWN_FIELD",
    "PUSH_DEDUPE_COOLDOWN_MIN",
    "PUSH_ENV_NAME",
    "PUSH_PAYLOAD_SCHEMA_VERSION",
    "REGISTRY_FILENAME",
    "REGISTRY_SCHEMA_VERSION",
    "SIGNAL_DESIGN_PIN",
    "SIGNAL_FAMILIES",
    "design_version",
]
