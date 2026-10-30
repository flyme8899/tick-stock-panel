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
        "daily_macd",         # 日线 MACD 柱穿越(批次 20261030ga1 · D2a 命名位)
        "daily_kdj",          # 日线 KDJ 金叉(批次 20261030ga1 · D2b 命名位)
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

# ---------------------------------------------------------------- 命中层(批次 20261026gbsshits)

HITS_FILENAME: Final = "hits.json"
"""命中层只读店的文件名(写死)。

与 `REGISTRY_FILENAME` 同目录(`<data_dir>/buy_sell_signals/`), 但**是两份文件**:
注册表存的是「信号定义」, 这份存的是「某只票在某天命中过某条定义」。合成一份会让
「定义改版」和「历史命中」互相覆盖, 而历史命中是不能被改写的。

**只读**: 生产/启动路径只读, 本拍没有任何代码路径往这里写 live 命中。缺文件 = 0 条命中,
UI 走空态, 这是合法且诚实的生产状态。
"""

HITS_SCHEMA_VERSION: Final = "1"
"""`hits.json` 的文件级格式版本(字符串, 理由同 `REGISTRY_SCHEMA_VERSION`)。"""

EMPTY_HITS_MESSAGE: Final = "暂无已校准的买卖信号"
"""监控中心买卖信号 tab 的空态文案(**逐字写死**)。

不许改成「今天没信号」这类句子: 那句话在读者眼里等价于「系统在跑、只是今天没有」,
而真实情况往往是「评估器还没锁定阈值, 一条都还没产出」。两者对使用者的含义完全不同,
把后者说成前者就是在暗示系统比实际更就绪。详见 `models.SignalHit` 的 docstring。
"""

DIRECTION_LABELS: Final = frozenset({"research_buy", "research_sell", "research_watch"})

# --------------------------------------------------- 影子生产写入(P-bss-p3b · 批次 20261028gp3b)

SHADOW_SUBDIR: Final = "shadow"
"""影子分桶根段名(写死), 挂在 `PERSIST_SUBDIR` 之下。

完整路径(由三键确定性指认):

```
<data_dir>/buy_sell_signals/shadow/<signal_id>/<definition_version>/<freq_lock_version>/hits.json
```

**独立于**同在 `PERSIST_SUBDIR` 下的 live `HITS_FILENAME`。为什么必须独立一个目录,
而不是在 live `hits.json` 里加个 `mode` 字段:

1. 读口不被污染 —— `DiskHitStore.list_all()` 返回的就是全部, 不需要下游再做一次
   "把 shadow 剔掉"的过滤; 过滤写错一次, 影子就进了可见流。
2. 样本量不互相污染 —— live 条数不会被影子堆高, "样本够不够"能问得清。
3. 回滚不互相牵连 —— 清影子样本只动影子文件。

桶内文件名仍叫 `hits.json`(与 live 同名): 格式相同, 只有位置不同。同名让"用同一个
反序列化器读两种文件"成为默认; 位置不同让"读错文件"必须显式拼错路径才会发生。

**路径已通 ≠ calib 已过 ≠ 可放量 ≠ TRIGGERING。** 这里的常量只描述"样本写在哪",
不描述"样本够不够"或"过没过门"。
"""

SHADOW_BUCKET_KEY_FIELDS: Final = ("signal_id", "definition_version", "freq_lock_version")
"""分桶三键, 顺序即路径层级顺序。

顺序写死而不是按字母排: 改顺序就改了路径, 已写出的样本会突然"找不到"—— 而文件还在
磁盘上, 只是没人按新路径去读它。这种失效不会报错, 只会让样本数悄悄变成 0。
"""

# ------------------------------------------------------- 命中钉钉外发(P-ding-later)
#
# 三分边界(**本包唯一的钉钉通道分类**, 文档 / 代码 / PR 三处口径必须一致):
#
#   1. 定义态(`push.py`):  `DINGTALK_WEBHOOK_URL` + PUSH ∧ BUY_SELL_SIGNALS_DINGTALK 门
#   2. 命中(本组常量):    `BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL` 独立键, URL 非空即门
#   3. 复盘(`DSA_REVIEW_*` 系列键): + 日 cap —— 另一条链路, 零碰
#
# 为什么命中必须独立一个键(而不是复用 `DINGTALK_WEBHOOK_URL`): 那个键已经被资讯热点、
# BSS 定义态、DSA 狙击三处共用。命中量和定义变更量不是一个量级 —— 命中是逐股逐日的,
# 定义变更是一周几次。挂在共群上, 一次命中批量就足以把"哪条信号被改过状态"淹掉, 而
# 淹掉的恰恰是更需要人看一眼的那类。所以宁可在运维上多一个"配了没"的位置,
# 也不能让两种语义抢同一个出口。
#
# 缺这个键时**拒发**, 不回落共群 —— 回落在配置上看着"更省事", 实际是把"没配"这个
# 事实伪装成"配好了", 而错发到共群是**发出去就收不回**的。

HITS_DINGTALK_WEBHOOK_ENV_NAME: Final = "BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL"
"""命中钉钉通道**唯一**的 webhook 环境变量名。

本包内**只有** `hits_dingtalk` 读它。定义态仍读 `DINGTALK_ENV_NAME` 那一套,
两者不是"新旧版本", 是两条并存的通道。
"""

HITS_DINGTALK_SECRET_ENV_NAME: Final = "BUY_SELL_SIGNALS_HITS_DINGTALK_SECRET"
"""命中钉钉的可选加签密钥。留空 = 该机器人未开启加签(与既有 `DINGTALK_SECRET` 同义)。"""

HITS_WEBHOOK_NOT_CONFIGURED: Final = "hits_webhook_not_configured"
"""缺命中 webhook 时的拒发原因(**机器可读**, 写进日志与结果对象)。

用专有字符串而不是 `not_configured`: 复盘那边已经有一个
`review_webhook_not_configured`, 两者同时出现在一份日志里时必须一眼分清是哪条通道
没配 —— 混用同一个字符串会让人去查错的键。
"""

HITS_DINGTALK_DEDUPE_COOLDOWN_MIN: Final = 30
"""同一命中 30 分钟内不重复外发(分钟)。

与定义态的 `PUSH_DEDUPE_COOLDOWN_MIN` **同数值但相互独立**: 两者键不同(定义态按
`signal_id` 冷却, 命中按含 `symbol` / `asof` 的五元组), 载体也不同(定义态记在
`SignalRecord` 字段上, 命中只存进程内)。共用一个常量会让"调了命中冷却顺手改了
定义态节奏"这种事故变得可能。
"""

HITS_DINGTALK_DEDUPE_KEY_FIELDS: Final = (
    "signal_id",
    "definition_version",
    "symbol",
    "asof",
    "freq_lock_version",
)
"""去重键的字段顺序(**写死**)。

五个字段都在的道理: 少了 `symbol` 会把"同一条信号打在不同票上"当成一条;
少了 `asof` 会把"昨天命中今天又命中"当成重复; 少了 `definition_version` 或
`freq_lock_version` 会让改版/换口径后的新结论被旧冷却挡住 —— 而口径变了本来就
**该**再发一次, 那是新信息。

刻意**不**复用定义态的 `PUSH_DEDUPE_COOLDOWN_FIELD`: 那个记录里没有 `symbol`,
拿它当命中冷却载体等于把一只票的冷却施加到全市场。
"""
"""`direction_label` 的**封闭**取值集。

刻意不叫 `direction`、不叫 `side`、不用 `buy` / `sell`: 这三个词在本仓别处指的是
可执行动作。命中层的标签只是「这条记录在研究中偏向哪一边」, 不是下单方向, 也不含
任何数量。名字里带 `research_` 前缀, 是为了让任何一次代码搜索都搜不到裸的 `"buy"`。
"""

HIT_SOURCE_MODES: Final = frozenset({"fixture", "shadow", "live"})
"""命中来源模式。本拍只产出 `fixture`(测例); `live` 是**保留值**, 没有写它的代码路径。"""

# ---------------------------------------------------------------- 频率锁定戳(批次 20261026gbsscalib)
#
# 这一段与下面的「校准登记」是**两层不同的东西**, 不要合并:
#
# - 频率锁定戳(`freq_lock.py`)回答「这条信号按什么节拍、用什么阈值参数集评估」,
#   以及「这份口径有没有被悄悄改过」。它是**评估口径的身份**。
# - 校准登记(`calib_*.py`)回答「这套口径在历史上跑出来过什么样的结果, 过没过门槛」。
#   它是**结果的可信度**。
#
# 口径被改过(指纹变)而结果登记还挂着旧的那个, 就是本拍要拦的那类失真: 登记说的是
# 旧口径的成绩, 读者以为它给新口径背书。

FREQ_LOCK_VERSION: Final = "freq_lock_v1"
"""频率锁定戳的**人读标签**(写死)。

它是给人看的, 不参与任何判定 —— 真正参与比对的是 `freq_lock.FreqLockStamp.fingerprint`
(内容指纹)。留一个标签是因为「只改了代码逻辑、常量没动」这种情况下指纹不会变,
那时候唯一能说出来"这是第几版口径"的就只有这个字符串。

对标 `regime_caliber` 的做法, 但**独立模块、独立命名空间**: 那边描述的是**市场环境**
的计算口径(`SignalRecord.regime_caliber_version`), 与 BSS 信号的评估频率/阈值毫无关系。
"""

FREQ_LOCK_CADENCES: Final = frozenset({"minute_event", "daily_fundamental"})
"""**评估节拍**的封闭取值域。

刻意与 `SIGNAL_FAMILIES` 分开写一份、而不是直接复用:
`SIGNAL_FAMILIES` 里还有 `composite`, 而 `composite` **没有**一个明确的评估节拍
(它将来由若干子信号合成, 节拍取决于被合的那些)。让 `FREQ_LOCK_CADENCES` 少一个值,
是为了让「合成信号的节拍还没定义」这件事在类型上就显形, 而不是等某个人给
`composite` 随便挑一个 cadence 写进去。

本拍没有任何 `composite` 的频率锁定戳, 也没有写它的代码路径。
"""

FREQ_LOCK_SCHEMA_VERSION: Final = "1"
"""戳的序列化格式版本(字符串, 理由同 `REGISTRY_SCHEMA_VERSION`)。"""

# ------------------------------------------------- 评估单位声明(批次 20261027gbssk4 · P-bss-k4 · 补 K4)
#
# 仅供研究, 不构成投资建议; 执行须人工确认。
#
# 这一组常量只解决一件事: 让「量比是按手还是按股算的」在评估口径里**显式**写出来。
# 它**不是**「#7 量审计已过」, **不是**开放触发, **不是**放量能力, 也**不是** G1/B4
# 那种跨层换算 —— 它只是一道"不写单位就拒评"的闸。
#
# 为什么必须有: 主仓口径是「手」, DSA 侧是「股」(#237 跨层对照表记的就是这条双轨)。
# 同一条量比阈值, 单位不同可以差 100 倍。而 params 里的 `volume_ratio_min: 2.5` 从字面上
# 看不出单位 —— 不逼人写出来, 这个歧义就会一路静默到结论里。

VOLUME_UNIT_PARAM_KEY: Final = "volume_unit"
"""`FreqLockStamp.params` 内评估单位字段名(写死)。

正式值**只**认中文「手」「股」。英文 alias(lot / share / hand)一律非法 ——
同一件事有两种写法, 就一定会有人在两种写法之间比大小。
"""

VOLUME_UNITS: Final = frozenset({"手", "股"})
"""评估口径单位**封闭集**。

"封闭"是刻意的: 任何新单位(千股、万手、张……)都必须先改这里, 而不是被 `volume_unit`
悄悄接受。改这个集合是一次口径变更, 该走评审, 不该靠调用方随手填个字符串。
"""

VOLUME_UNIT_TRIGGER_KEYS: Final = frozenset(
    {
        "volume_ratio",
        "volume_ratio_min",
        "volume_ratio_max",
    }
)
"""显式列出的量相关键: 出现任一个即强制声明 `volume_unit`。

除这些键之外, **任何以 `volume_` 开头且不等于 `volume_unit` 的键**同样触发
(见 `freq_lock.require_volume_unit`)。前缀规则是为了让将来新增的 `volume_ma` /
`volume_window` 之类自动落入闸内, 不用每次回来改这张表 —— 漏改一张表的成本,
比多拦一个键高得多。
"""

# ---------------------------------------------------------------- 校准登记(批次 20261026gbsscalib)

CALIB_FILENAME: Final = "calib.json"
"""校准登记的文件名(写死)。

与 `REGISTRY_FILENAME` / `HITS_FILENAME` **同目录**(`<data_dir>/buy_sell_signals/`),
但是**第三份文件**: 定义、命中、校准三件事各有各的生命周期, 合成一份会互相覆盖。

## 为什么不挂在 `BUY_SELL_SIGNALS_PERSIST` 下面

**不绑。** 那个 env 管的是「定义注册表落不落盘」。把校准读取也挂在它下面, 会造出
「没开 PERSIST 所以看不到校准」这种假耦合 —— 而校准看不看得见, 只该取决于文件在不在。

缺文件 = 无登记 = 一条都不可见(见 `hits_visibility.CALIB_NOT_PASSED`)。这不是缺陷,
是诚实空态: 没有通过登记的时候, 系统确实什么都不能担保。
"""

CALIB_SCHEMA_VERSION: Final = "1"
"""`calib.json` 的文件级格式版本(字符串, 理由同 `REGISTRY_SCHEMA_VERSION`)。"""

CALIB_STATUSES: Final = frozenset({"passed", "pending", "failed"})
"""校准状态的**封闭**取值域。

封闭是刻意的, 而且刻意只有三种:

- `passed` —— 唯一算「已校准」的值;
- `pending` —— 还没结论;
- `failed` —— 有结论且没过门槛。

**禁用** `ok` / `true` / `verified` / `calibrated` 这类近义词。它们不是"更宽松的写法",
而是把「过没过」这件事变成了可以有多个答案。多一个近义词, 就多一个地方可以悄悄
把 `pending` 写成"通过"。
"""

CALIB_STATUS_PASSED: Final = "passed"
"""唯一代表「已校准」的状态值。比较一律用它, 不写字面量 `"passed"`。"""

CALIB_SAMPLE_N_MIN: Final = 30
"""写 `passed` 所要求的**最少样本数**(N2, 写死)。

`sample_n < 30` 一律**拒写** `passed`(不是静默降级成 `pending`)。

为什么是拒而不是降级: 悄悄降级会让人以为「我写进去了, 只是没过」, 实际是**没写**。
下次他来查登记, 看到一条 `pending` 会以为是自己的回测没过, 而不是自己的调用被拒了。
口径要一致 —— 门槛不满足就是写入失败, 让他当场知道。

30 这个数字本身没有魔法, 它是 ALIGN N2 钉的下限, 不是统计显著性门槛。过 30 不等于
显著, 只等于"够说一句有样本量的话"。真正的显著性口径是 `BONFERRONI_*` 那一组。
"""

CALIB_EVAL_WINDOW_UNIT: Final = "trading_days"
"""`eval_window` 的**单位**(N2, 写死 = **交易日**)。

不写"天"、不写"自然日": 两者在 A 股上差着整整一年的节假日。写 "20 天" 而实际是
20 个自然日, 样本区间会覆盖不到 3 个完整交易周, 而写的人和读的人都以为它盖了一个月。
单位必须显式落进文件(`eval_window_unit` 字段), 不能只靠文档约定。
"""

CALIB_COST_BP_ROUNDTRIP: Final = 18.2
"""回测必须计入的**来回成本**, 单位 bp(写死 18.2)。

`cost_bp_roundtrip != 18.2` 一律拒写 `passed`。

为什么钉死而不是"由调用方自报": 成本是个**可以让任何策略从负变正**的参数。允许自报,
第一个想让它过的人就会填 5bp, 而 5bp 在 A 股连印花税都不够(卖出千分之0.5, 加上
双边佣金与过户费, 来回 18.2bp 是仓内既有的实测口径, 见 `quality_rank` 里
`C_cost_18_2bp_measured_pending` 那条缺口标注)。钉死之后, 这个参数不再是可调项,
而是**准入口径的一部分**。
"""

CALIB_HIT_DEDUPE_KEY_FIELDS: Final = (
    "signal_id",
    "definition_version",
    "symbol",
    "asof",
    "freq_lock_version",
)
"""命中侧去重/对齐键的字段名(顺序即语义顺序)。

## 这不是播报冷却

`PUSH_DEDUPE_COOLDOWN_MIN` / `push_dedupe_cooldown_until` 是**提醒层**的去重:
「同一条信号别在推送通道里重复播报」。本常量是**校准层**的对齐键:
「这条命中是不是在那套被登记过的口径下算出来的」。

两者的窗口、目的、失效条件全不一样。把播报冷却当成本拍的"频率锁定"对象,
会让「30 分钟不重复推」变成「口径锁定」—— 一个改推送静音时间的动作顺手改掉了
信号的可信度判定, 而且改的人不会觉得自己动了信号。

## 本拍只定义键, 不实现写路径

`hits_store` 仍然只读, 没有任何 upsert。这个键当前的唯一用途是「三键对齐」时的
字段名来源, 以及给将来的去重写路径留一个不会被随手改动的定义。
"""

# ---------------------------------------------------------------- 合规

DISCLAIMER_KEY: Final = "research_only_v1"

__all__ = [
    "AUCTION_RULE_ID_NONE",
    "AUCTION_STAT_BUCKET_0925",
    "BONFERRONI_FAMILY_ALPHA",
    "BONFERRONI_M",
    "BONFERRONI_SCHEME_ID",
    "CALIB_COST_BP_ROUNDTRIP",
    "CALIB_EVAL_WINDOW_UNIT",
    "CALIB_FILENAME",
    "CALIB_HIT_DEDUPE_KEY_FIELDS",
    "CALIB_SAMPLE_N_MIN",
    "CALIB_SCHEMA_VERSION",
    "CALIB_STATUSES",
    "CALIB_STATUS_PASSED",
    "DINGTALK_ENV_NAME",
    "DIRECTION_LABELS",
    "DISCLAIMER_KEY",
    "EMPTY_HITS_MESSAGE",
    "EVENT_CLASSES",
    "FREQ_LOCK_CADENCES",
    "FREQ_LOCK_SCHEMA_VERSION",
    "FREQ_LOCK_VERSION",
    "HITS_DINGTALK_DEDUPE_COOLDOWN_MIN",
    "HITS_DINGTALK_DEDUPE_KEY_FIELDS",
    "HITS_DINGTALK_SECRET_ENV_NAME",
    "HITS_DINGTALK_WEBHOOK_ENV_NAME",
    "HITS_FILENAME",
    "HITS_SCHEMA_VERSION",
    "HITS_WEBHOOK_NOT_CONFIGURED",
    "HIT_SOURCE_MODES",
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
    "SHADOW_BUCKET_KEY_FIELDS",
    "SHADOW_SUBDIR",
    "SIGNAL_FAMILIES",
    "VOLUME_UNITS",
    "VOLUME_UNIT_PARAM_KEY",
    "VOLUME_UNIT_TRIGGER_KEYS",
    "design_version",
]
