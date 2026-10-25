# 买卖信号骨架（批次 20261024g + g2 窄化 20261025g2 + gp 可选落盘 20261025gp）

> **仅供研究，不构成投资建议；执行须人工确认。**

本文说明 `backend/app/buy_sell_signals/` 这个包**做了什么**、**明确不做什么**，以及
为什么几条硬约束写成了常量而不是配置项。

> 20261025g2 的改动见文末 [g2 窄化](#g2-窄化批次-20261025g2) 一节；
> 20261025gp 的改动见文末 [gp 可选落盘](#gp-可选落盘批次-20261025gp) 一节。

## 一句话

实现了买卖信号的**注册表字段 + 状态机骨架 + 日线质量代理挂点 + 申万一级 PIT 只读查询**，
Store 默认用**内存实现**，置 `BUY_SELL_SIGNALS_PERSIST=1` 后改为**落盘到本地 JSON**；
估值与健康只返回 `data_pending` / `design_pending`。

**没有**接任何 HTTP 路由、**没有**接日管道、**没有**接推送。

**落盘 ≠ 推送**：开了落盘也只是把注册表写进 `data/buy_sell_signals/registry.json`，
没有 SSE、没有飞书、没有 `news.push`。

## 包结构

| 文件 | 职责 |
|---|---|
| `constants.py` | 元数据 / Bonferroni 口径 / 与提醒层分家的冷却字段名 |
| `models.py` | `SignalRecord`（frozen dataclass）+ 字段校验 |
| `state_machine.py` | 转移表 + 人工发布闸 + 硬禁自动晋升 |
| `store.py` | `SignalRegistryStore` 协议 + `InMemorySignalRegistryStore` + `DiskSignalRegistryStore`（env 门控，批次 gp 起真落盘）+ `build_signal_registry_store` 工厂 |
| `sw_pit.py` | 申万一级 PIT：`history × l1_map`，按 `valid_from`/`valid_to` 取当时归属 |
| `quality_proxy.py` | 日线质量代理：财务按 `announce_date` 做 PIT |
| `quality_rank.py` | **g2**：申万一级行业内的质量相对分位 + 白名单试跑判定 |
| `rebalance_snapshot.py` | **g2**：月度再平衡快照（纯内存，不调度不写盘） |
| `valuation_stub.py` | 估值接口桩，恒返回 `data_pending` |
| `health_veto_stub.py` | 健康 veto 接口桩，恒返回 `design_pending` / `hit=None` |
| `disclaimer.py` | 合规脚注常量 |

## 三条硬约束，以及为什么它们是常量

### 1. 状态机没有自动晋升路径

`ALLOWED_TRANSITIONS` 里**没有任何一条边指向 `publishable`**。唯一入口是
`manual_publish(record, publisher=..., checklist=..., note=...)`：

- checklist 六项**全部显式为 `True`**（缺项、`False`、`"yes"` 都拒）
- 带了清单外的键也拒（拼错名字时那一项等于没确认，但看起来像完整清单）
- 当前状态必须是 `observe`（`candidate` 得先观察一轮）
- `publisher` / `note` 不能空（发布是需要问责的动作）

写成表而不是散落的 if：加一条通往 `publishable` 的边，`test_transition_table_has_no_inbound_edge_to_publishable`
会立刻红。而且 `transition()` 在函数入口就对 `target == "publishable"` 直接抛错，
不依赖表。

### 2. `limit_reopen` 永远不能发布

炸板/回封这类事件的样本量极不稳定，且回封后的走势与统计口径强相关。在专门验证之前
给它发布资格，等于允许一条没验证过的规则对外生效。`manual_publish()` 与 `transition()`
两处各拦一次（刻意的重复）。

### 3. `data_abnormal` 不能静默回到 `candidate`

状态表里**没有**这条边。想回去只能 `rerun_from_abnormal(operator=..., reason=...)`，
要求显式说明是谁、为什么。数据异常往往意味着上一步的判定依据本身是脏的，静默恢复
会把脏数据带进下一轮。

## 竞价桶与开盘后桶为什么必须分列

`auction_stat_bucket` 只有 `auction_0925` 一个合法值，且只属于 `event_class="auction"`。
`NOISE_MIX_FORBIDDEN = True` 是模块级常量而不是记录字段——记录上留个可配置开关，
迟早有人打开它。

`event_class="auction"` 的记录携带 `post_open_noise_bucket` 会在构造时直接抛错。这是
唯一的混桶口子，堵在这里比事后拆数据便宜。

**本批次不做竞价回测。** 特别地：**不允许**用 09:30 之后第一根 1 分钟 K 线冒充 09:25
—— 集合竞价撮合与连续竞价是完全不同的信息集。

## 冷却字段为什么叫 `push_dedupe_cooldown_until`

提醒层在本仓至少有两种"冷却"：

| 名字 | 语义 | 归属 |
|---|---|---|
| `T_COOLDOWN_MIN = 60` | 同一只票一天内不再出一**轮**做 T | 做 T / 提醒 |
| `push_dedupe_cooldown_until` | 同一条信号别重复**播报** | 提醒层去重 |

两者共用一个名字，将来做 T（旁支 h）与本包混在一起时一定会有人搞错。所以本包只用
后者，且名字带 `push_` 前缀。

`test_constant_names_do_not_collide_with_reminder_layer` 会检查本包常量名与
`trade_marks` / `news.push` 的常量名没有交集；另有测试逐文件扫描，确认本包**没有**
import `VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`。

`push_eligible` 判断的是"**当前时间**是否已过冷却点"，不是"冷却字段是否为空"。冷却
过期后字段里仍留着时间戳（它是历史事实），若写成 `is None`，任何推过一次的信号会
永久不可推。

## 申万 PIT 为什么不能退到同花顺

`ext_hy_ths` / `industry_ths` 是同花顺的**当前快照**，没有 `valid_from`/`valid_to`。
拿它当历史归属，等于用今天的分类去回看三年前的信号——而且从结果上完全看不出来。

`load_sector_tables()` 一旦发现来源名带 `ths` 或「同花顺」字样就**直接抛
`IndustrySourceRejected`**，不做任何 fallback。这与
`test_fundamental_screens.py::test_m2_missing_industry_file_is_an_error` 是同一种立场：
缺行业就报错，别猜。

`valid_to` 取**开区间**（`[from, to)`），相邻两段首尾相接时不会出现同一天两条都命中。

查不到映射时返回 `None` + `reason`，**不按行业代码前缀猜 L1**——申万内部码跨版本会变含义，
按前两位切只在一个版本内成立。

## 质量代理的 PIT 与 `None` vs `0`

财务只认 `announce_date <= asof` 的行。用 `period_end` 判是错的：三季报 9 月 30 日截止
但 10 月底才公告，用报告期判，你在 10 月 1 日就知道了一家 10 月 28 日才公布的好业绩。

公告前字段一律 `None`，**绝不填 0**。0 是一颗真实数字（ROE=0 表示不赚钱），`None` 才是
"还不知道"。混起来之后 `q_missing_flag` 就失去意义。

`w_quality` / `w_value` 保持 `None` + `weighting_status="design_pending"`。权重要靠回测定，
骨架阶段随手填个 0.3/0.7，之后所有人都会把它当结论。

## 估值 / 健康：pending 且未测通

| 模块 | 返回 | 说明 |
|---|---|---|
| `valuation_stub` | `data_pending=True`，`v_score=None` | 缺逐日估值序列（见 `VALUATION_REQUIRED_INPUTS`） |
| `health_veto_stub` | `design_pending`，`health_veto_hit=None` | 规则引擎未实现 |

`health_veto_hit` 是 `None` 而不是 `False`：`False` 是一个**结论**（"查过，健康"），
返回它等于向调用方保证一件我们并不知道的事。`blocks_publish` 同样返回 `None`。

**不把 `financials_metrics` 里的 `pe` / `pe_ttm` 当估值 PIT**：那是选股器的时点快照，
没有随日线滚动的价格，也没有与公告日的配对关系。用它当逐日估值序列，回测能跑完，
只是每个数字都对不上。

## 存储：默认不落盘，落盘由 env 门控（批次 gp 起真落盘）

三道门，任何一道没开都退化成内存实现：

1. **构造门**：`DiskSignalRegistryStore(..., enable_persist=False)`（默认）直接抛
   `PersistDisabledError`。这条自 g 轮就有，没有放宽。
2. **工厂门**：`build_signal_registry_store(data_dir=None)` 是**唯一**的正常入口。
   它读 `BUY_SELL_SIGNALS_PERSIST`（真值集合 `{"1","true","yes","on"}`，大小写与空白
   都归一）。未设 / 空 / 假值 → 返回 `InMemorySignalRegistryStore`。
3. **接线门**：`main.py` 的 lifespan 只调工厂，且**只存到 `app.state.buy_sell_signal_store`**，
   不挂路由、不订阅事件、不接日管道。`main.py` 里出现 `DiskSignalRegistryStore(` 会被
   静态测试拦下（防有人绕过工厂硬开）。

落盘位置写死为 `settings.data_dir / "buy_sell_signals" / "registry.json"`，不写
`partition_writer` 的生产分区，也不碰 `/data/tsp-reports`：注册表是"少量记录、按 id 查"，
与分区 parquet 的形状不符。硬套会得到一个既慢又难查的实现，还会在 `data/` 下多出一批
无人认领的分区。

### 落盘格式与不变式

```json
{ "namespace": "...", "schema_version": "1",
  "records": [ { "signal_id": "...", "version": "...", "...": "..." } ] }
```

- **原子写**：走 `fs_utils.atomic_write_text`（临时文件 + rename），不会留半个文件
- **非有限浮点 → `null`**：序列化前过 `json_safe(..., coerce=True)`，`allow_nan=False`
  落盘。`metadata` 是不校验的自由字典，脏 NaN 能进来，因此必须有这层兜底
- **文档是坏的 → 报错，不重建**：JSON 解析失败 / 缺 `records` / 元素不是 dict /
  `from_dict` 校验失败 / 同一 `signal_id` 出现两条 `publish_pointer=True`，一律抛
  `SignalRegistryCorruptError`。**不自动重建空表** —— 「文件坏了」和「表是空的」必须区分，
  否则一次解析事故就静默抹掉整份注册表
- **空目录/文件不存在 → 合法空表**：首次启动不报错
- `schema_version` 是**字符串** `"1"`，不是 int。写成 int 会落进「本包常量不得与
  `t_rounds` 门禁同值」那条静态扫描（撞 `MAX_ROUNDS_PER_DAY = 1`），逼后人给版本号乱跳
- 指针互斥：同一 `signal_id` 至多一条 `publish_pointer=True`，`upsert` 与
  `set_publish_pointer` 都会先查后写

## 测试

```bash
cd backend
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest \
  tests/test_buy_sell_signal_registry.py \
  tests/test_buy_sell_signal_sw_pit.py \
  tests/test_buy_sell_signal_quality_proxy.py \
  tests/test_buy_sell_signal_quality_rank.py \
  tests/test_buy_sell_signal_rebalance_snapshot.py -q
```

225 条（g 轮 130 + g2 轮 66 + gp 轮 29），不依赖网络 / Pro 分钟 capability / 真实 `/data/tsp-reports`。
夹具全部在 `tmp_path` 里现造；落盘测试只写 `tmp_path`，不碰 `settings.data_dir` 之外的地方。

## ALIGN §4 自检

- [x] 状态机：`candidate → … → publishable` 禁止自动晋升；`manual_publish_required` 默认 true
- [x] Store：接口与内存实现分离；默认不落盘（gp 起落盘为 env 门控的可选项，默认仍关）
- [x] 竞价与开盘后噪声字段分列；无混桶 API
- [x] 行业：申万 PIT 路径；禁同花顺当前态 silent 复用（抛错而非降级）
- [x] 健康/估值：pending 标记可见；无「已测通」文案
- [x] 与 `trade_marks` / `news/push` 提醒常量无数值耦合（静态检查守着）
- [x] 单测：状态迁移、手动闸、Store 内存往返；不依赖 Pro 分钟 capability
- [x] 文档：脚注「仅供研究，不构成投资建议」

## 本批次明确不做

- 正式 SSE / 飞书推送
- 默认落盘 / persist 开关默认开
- 竞价回测、分钟放量、炸板回测
- 做 T（旁支 h）轮次
- 完整健康规则引擎
- 分档成本实测
- HTTP 路由（可后置只读查询路由）

## 后续接入前要补的东西

1. **磁盘 Store 的真实实现**：先单独设计（大概率单文件 + 版本号，或进 SQLite），不要
   硬套分区 parquet
2. **健康规则引擎**：规则清单 + 历史触发回放 + 跨行业阈值校准，三件缺一不可
3. **逐日估值序列**：见 `VALUATION_REQUIRED_INPUTS`
4. **权重**：`w_quality` / `w_value` 要靠回测定，定完之后把 `weighting_status` 改掉
5. **注册真实数据源**：`sw_pit` 的列名适配覆盖了常见别名，但落地前要对着真实表跑一遍

---

# g2 窄化（批次 20261025g2）

> **仅供研究，不构成投资建议；执行须人工确认。**

阶段 2 的**窄切片**：只做三件互相咬合的事，其余一律不做。

| 做了什么 | 文件 |
|---|---|
| ① `limit_reopen` 构造即 `observe`（#200 终审小修，REVIEW_pr200 建议 1） | `models.py` |
| ② 申万一级行业内的质量相对分位 | `quality_rank.py` |
| ② 白名单**试跑** `p40`（未锁定） | `quality_rank.py` |
| ② 月度再平衡快照（纯内存工厂） | `rebalance_snapshot.py` |

## ① 炸板为什么构造即 `observe`

原实现里 `event_class="limit_reopen"` 的记录默认是 `candidate`，调用方必须记得再走一次
`transition(rec, "observe")`。**少一次必须记得做的动作，就少一次忘记做。**

改动只在 `__post_init__` 里加一句：`status == "candidate"` 且是炸板 → 改成 `observe`。

三个刻意的取舍：

- **只改默认值，不改显式意图**。调用方显式传 `status="publishable"` 仍会被
  "publishable 必须有人签过字" 拒掉。如果连显式传值也一起改回 `observe`，调用方会以为
  发布成功了、实际记录停在 observe —— 一个不报错却什么都没发生的 bug。
- **推送闸不动**。初始 `push_gate` 仍是 `manual_pending`，`push_eligible` 恒 `False`。
  这次修的是起点，不是闸门。
- **`state_machine` 的双重拦截原样保留**。初始 `observe` **不等于**"观察一轮之后就能
  发布"：`transition(..., "publishable")` 与 `manual_publish()` 两处仍然直接拒炸板。

有一条测试专门做"反向验证"：把这段修复注释掉，`test_limit_reopen_defaults_to_observe`
等三条立刻失败。

## ② 行业内相对分位：约定写死

绝对分 `q_score`（0–5）没有基准：银行里 q=3 和计算机里 q=3 不是一回事。这一层补"同日、
同申万一级行业内的相对位置"。

**只在同一天、同一 L1 内部比较**，不跨行业、不跨日。跨行业比较要先证明行业间可比，
那件事本批次没做。

| 约定 | 取值 |
|---|---|
| `q_rank_within_ind` 范围 | `[0, 1]`，**百分位越高越好** |
| 公式 | `(平均秩 - 1) / (n - 1)` |
| 并列 | 取**平均秩**（`[10,20,20,30]` → `[0, 0.5, 0.5, 1]`） |
| 全员同分 | `0.5`（谁也没排在谁前面） |
| 单票行业 | **missing**（`singleton_industry`） |

为什么不用 `rank / n`：那个公式下最小值拿到 `1/n` 而不是 0，单票时恒为 1 —— 下游按
`>= 0.60` 判，结果是"小行业里的票天然更容易进白名单"。

为什么单票行业必须 missing：只有一只票时"行业分位"恒为 1，那个 1 是算术产物，不是相对
位置。给它 rank 就是伪造一个不存在的比较。

### 缺数据一律 missing，`None` 不填 0

| 原因码 | 触发条件 |
|---|---|
| `no_industry` | 没有申万一级归属（含空串 / 键不存在） |
| `no_q_score` | `q_score is None` |
| `q_missing` | 质量代理自己标了缺失 |
| `singleton_industry` | 本行业只有这一只有资格的票 |

**0 是一个真实的百分位（行业垫底）**。填 0 会让缺数据的票看起来"排名很低"，从而在白名单
里被静默排除 —— 而它们其实只是"没算出来"。这两者在效果上一样，在原因上完全不同，
事后也没法从结果里区分。

缺资格票**不进入别人的分母**：4 只票里 1 只缺分，剩下 3 只按 n=3 排名，不是 n=4。

## 白名单试跑 `p40`：行业前 40%，且反复强调"未锁定"

> **语义（g2.1 写死）**：`p40` = 行业前 40%（分位越高越好，取分位上半部）
> = `q_rank_within_ind >= (1 - 0.40) = 0.60`（**闭区间**）。

```python
WHITELIST_PERCENTILE_TRIAL = "p40"      # 试跑档标签：行业前 40%
WHITELIST_PERCENTILE_LOCKED = None      # 恒 None
WHITELIST_TRIAL_QUANTILE = 0.60         # 前 40% = 1 - 0.40，判定用 >= 0.60（闭区间）
```

- 判定式是 `q_rank_within_ind >= 0.60`，**闭区间**。恰好落在 0.60 的票算通过 ——
  方向写反会静默砍掉一半，而且从名单上看不出来。
- **批次 20261025g21 纠偏**：#202 / 旧 FIX_TASKS_g2 的示例 `>= 0.40` **已废弃**。
  分位越高越好时，`>= 0.40` 留下的是行业**前 60%**，与纸面"行业前 40%"相反。
  本批次只改阈值、文档与测例；标签仍叫 `p40`，`locked` 仍空，分位公式未动。
  **禁止**把「`>=0.40` 即 p40」与现行「前 40% = `>=0.60`」混用。
- 没跑过 p30/p50 敏感性，所以不能说 0.60 比其它档好。
- `WHITELIST_PERCENTILE_LOCKED` 恒 `None`，且**没有任何代码路径**能写入它。
  `RebalanceSnapshot.__post_init__` 里还加了一条硬校验：传非空直接抛错。将来真锁定，
  必须连着改那里 —— 那是一次显式决定，不是改个赋值。
- 每条结果都带 `whitelist_threshold_locked=False`，下游只读一个字段也能看出是试跑。

**白名单不是权重**：进不进名单，`w_quality` / `w_value` / `weighting_status` 都不变。

## 再平衡快照：记账，不是调度

```text
rebalance_id, pit_asof, score_version, universe_n, scored_n, whitelist_n_p40
```

用途只有一个：让"某个月的名单是怎么来的"可复核。没有这份留痕，半年后没人能说清某只票
当时为什么在名单上。

- **不挂 cron、不订阅事件、不看交易日历**
- **不写盘**、不碰 `DiskSignalRegistryStore`、不接 `main.py` 日管道（有静态测试守着）
- `whitelist_n_p40` 的字段名随试跑档位走 —— 换档之后新旧快照不会长得一模一样。
  g2.1 保留该字段名（不为改名而改名），但**计数走新阈值 `>= 0.60`**

`score_version()` = `design_pin_v1+quality:design_pending`，引用已有的
`SIGNAL_DESIGN_PIN` 与 `QUALITY_WEIGHT_STATUS`，不另造版本号。

## g2 与 h（`t_rounds`）的分家

- 本包**不 import** `app.t_rounds`：静态测试逐文件扫 import 语句
- 两边**不共用数值常量**：测试比对"常量名 + 值"，只放过 `NAMESPACE` / `DISCLAIMER_TEXT`
  这类按设计就该各自持有一份的文案
- 冷却仍然只用 `push_dedupe_cooldown_until`（提醒层去重），没有第二个 `cooldown`

## g2 明确不做

- 估值腿实装 / 健康规则引擎（`health_veto_hit` 保持 `None`，**不是** `False`）
- 权重标定、白名单锁定、p30/p50 敏感性
- 正式推送闸 / SSE / 飞书
- 落盘、persist 默认开、生产分区写入
- 竞价回测、09:30 首根 1m 冒充 09:25
- 再平衡的生产调度（cron / 批跑 / 写分区）
- 改 `t_rounds` / `trade_marks` / `news.push` / `main.py`
- 完整 `vetoed|degraded` 枚举（可后置）

## g2 验收自检（ALIGN §5）

- [x] 基线 tip = `5ded52b2…`；改动全部落在 `buy_sell_signals` + 对应 tests/docs
- [x] `limit_reopen` 初始即 `observe`；仍禁 `publishable`（双拦保留）
- [x] 行业内相对分位可单测（人造同行业多票）；缺行业/缺分 → missing，不填 0
- [x] `whitelist_percentile_trial=p40` 可见；`locked` 为空；无「已锁定」文案（否定语境放过）
- [x] 估值/健康 stub 未假装有表；`health_veto_hit` 非 `False` 伪装通过
- [x] 权重仍 `design_pending`；无假权重数字当结论
- [x] Store 默认不落盘（未设 `BUY_SELL_SIGNALS_PERSIST` → `InMemory`）；main 只经工厂写
      `app.state`，未接路由 / 日管道 / push
- [x] 与 `t_rounds` / `trade_marks` / `news.push` 无数值或符号耦合（静态检查）
- [x] 单测全绿；不依赖 Pro 分钟 / 竞价包
- [x] 文档脚注「仅供研究，不构成投资建议」

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*

## gp 可选落盘（批次 20261025gp）

**一句话**：`DiskSignalRegistryStore` 从"空壳"变成"真能落盘"，但**默认仍关**、
**只开落盘不开推送**。

### 改了什么

| 位置 | 变化 |
|---|---|
| `constants.py` | 新增 `PERSIST_ENV_NAME` / `PERSIST_SUBDIR` / `REGISTRY_FILENAME` / `REGISTRY_SCHEMA_VERSION` |
| `models.py` | 新增 `SignalRecord.from_dict` 与模块级 `signal_record_from_dict`：过滤派生键、解析时区、仍走 `__post_init__` 校验 |
| `store.py` | `DiskSignalRegistryStore` 实装 `_load` / `_flush` 与全套读写；新增 `SignalRegistryCorruptError`、`persist_env_enabled()`、`build_signal_registry_store()` |
| `main.py` | lifespan 里经工厂建 store 存到 `app.state`；**不挂路由、不建任务、不订阅事件** |
| `docs` | 本节 + 上文存储章节重写 |

### 没改什么（逐条钉住）

- **白名单阈值未动**：`WHITELIST_TRIAL_QUANTILE = 0.60`、`WHITELIST_PERCENTILE_TRIAL = "p40"`、
  `WHITELIST_PERCENTILE_LOCKED = None` 全部照旧。gp 不碰 g2.1 的口径
- **推送仍未接**：`main.py` 不得 import `app.news.push` / `sse_starlette` / 飞书客户端，
  有静态测试按 **import 语句**扫（不是裸子串 —— 注释里写"不接 news.push"是合法的）
- **日管道仍未接**：`daily_pipeline.py` 里不得出现 `DiskSignalRegistryStore` 或
  `buy_sell_signals`
- **`partition_writer` 仍禁**：AST 扫 `store.py`，名字不得出现
- **`t_rounds` 仍不 import**：静态测试逐文件扫

### 怎么开

```bash
# 默认: 内存, 进程退出即消失
python -m app.main

# 落盘: 写 data/buy_sell_signals/registry.json
BUY_SELL_SIGNALS_PERSIST=1 python -m app.main
```

生产是否置 1 **另等部署口令**，不随合入默开。

### gp 验收自检（ALIGN §5）

- [x] 基线 tip = `c6adb0bf…`；改动落在 `buy_sell_signals` + `tests/` + `docs/`，`main.py` 只有 lifespan 一段
- [x] 未设 env → `InMemorySignalRegistryStore`，且**不创建任何目录**（测试断言 `tmp_path` 为空）
- [x] 设 env → 写 `buy_sell_signals/registry.json`，重开进程能还原记录，往返无损
- [x] env 名写死为 `BUY_SELL_SIGNALS_PERSIST`，真值集合固定，无第二个开关
- [x] 落盘文件损坏 → `SignalRegistryCorruptError`，**不自动重建空表**，原文件保留
- [x] 同一 `signal_id` 两条 `publish_pointer=True`（磁盘上被人改过）→ 报错
- [x] 非有限浮点 → 落盘为 `null`，`json.loads` 严格解析通过
- [x] `from_dict` 丢弃派生键与免责声明快照；naive datetime 直接拒（不猜时区）
- [x] 白名单口径回归：`0.60` / `p40` / `locked=None` 未变（g2.1 测试仍红-绿值守）
- [x] 推送仍未接：`main.py` 无 `app.news.push` / `sse_starlette` / 飞书 import
- [x] 单测全绿；不依赖网络 / Pro 分钟 / 真实 `/data`
- [x] 文档脚注「仅供研究，不构成投资建议」

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*
