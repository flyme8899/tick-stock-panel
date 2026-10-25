# 买卖信号骨架（批次 g + g2 窄化 + gp 可选落盘 + gpush 可选推送 + gding 可选钉钉 + gval 估值健康窄开）

> **仅供研究，不构成投资建议；执行须人工确认。**

本文说明 `backend/app/buy_sell_signals/` 这个包**做了什么**、**明确不做什么**，以及
为什么几条硬约束写成了常量而不是配置项。

> 20261025g2 的改动见文末 [g2 窄化](#g2-窄化批次-20261025g2) 一节；
> 20261025gp 的改动见文末 [gp 可选落盘](#gp-可选落盘批次-20261025gp) 一节；
> 20261025gpush 的改动见文末 [gpush 可选推送](#gpush-可选推送批次-20261025gpush) 一节；
> 20261025gding 的改动见文末 [gding 可选钉钉](#gding-可选钉钉批次-20261025gding) 一节；
> 20261025gval 的改动见文末 [gval 估值/健康窄开](#gval-估值健康窄开批次-20261025gval) 一节。

**现行口径：落盘已开 · 推送已开(SSE/站内) · 钉钉可选(env) · 飞书已取消 · 竞价/放量事件仍未接 · 估值/健康仍 pending。**

## 一句话

实现了买卖信号的**注册表字段 + 状态机骨架 + 日线质量代理挂点 + 申万一级 PIT 只读查询**，
Store 默认用**内存实现**，置 `BUY_SELL_SIGNALS_PERSIST=1` 后改为**落盘到本地 JSON**；
估值与健康只返回 `data_pending` / `design_pending`。

推送出口 `push.try_push()` 已就位，但**默认关**：置 `BUY_SELL_SIGNALS_PUSH=1` 才会真实
扇出，通道 = **SSE（复用 `QuoteService.push_alerts`）+ 站内告警（`alerts.jsonl`）**。
外部通道只接**既有钉钉 webhook**，且要再开一道 `BUY_SELL_SIGNALS_DINGTALK`（默认关，
与 PUSH 是**与**关系）。**飞书已取消**（批次 20261025gding 起从路线图去掉，不再排期）。

**没有**接任何 HTTP 路由、**没有**接日管道（不会自动扫全表晋升/推送）。

**落盘 / 推送 / 钉钉三者正交**：三个 env，互不推导。落盘管"记录活得比进程久"，
推送管"状态变更播出去（站内/SSE）"，钉钉管"播到外部群"。可以只落盘不推，也可以只推
不落盘（不建议，重启即失忆，但代码不拦）。**钉钉真实外发 = PUSH 开 且 DINGTALK 开
且 `DINGTALK_WEBHOOK_URL` 非空**，缺任一即静默跳过，不影响站内/SSE。

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
| `push.py` | **gpush**：`try_push` 推送出口（env 门控），通道 = SSE + 站内 |

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

- 飞书推送（**已取消**：批次 20261025gding 起从路线图去掉，不再排期）
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

绝对分 `q_score`（0–7，七列里取值为正的项数）没有基准：银行里 q=3 和计算机里 q=3 不是一回事。这一层补"同日、
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
- 正式推送闸 / SSE / 飞书（SSE 已在 gpush 交付；飞书在 gding 取消）
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

**一句话**：`DiskSignalRegistryStore` 从"空壳"变成"真能落盘"，但**默认仍关**。
（当时一并**没有**开推送；推送是下一个批次 gpush 才接的，两者是正交的两件事。）

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
- **推送当时未接**（gpush 起改为 env 门控）：`main.py` **至今仍不得** import
  `app.news.push` / `sse_starlette` / 飞书客户端，有静态测试按 **import 语句**扫
  （不是裸子串 —— 注释里写"不接 news.push"是合法的）。gding 起 `push.py` 允许
  import `app.news.dingtalk` 与 `app.config`（只为调既有发送函数、读既有 settings）
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
- [x] 推送当时未接（gpush 起为 env 门控）：`main.py` 至今无 `app.news.push` / `sse_starlette` / 飞书 import
- [x] 单测全绿；不依赖网络 / Pro 分钟 / 真实 `/data`
- [x] 文档脚注「仅供研究，不构成投资建议」

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*

## gpush 可选推送（批次 20261025gpush）

**一句话**：推送出口 `push.try_push()` 接上了，但**默认关**，**通道只有 SSE + 站内**。
外部通道当时未接，由下一批 gding 只补钉钉；**飞书已取消**（gding 起从路线图去掉）。

### 改了什么

| 位置 | 变化 |
|---|---|
| `constants.py` | 新增 `PUSH_ENV_NAME` / `MONITOR_EVENT_TYPE` / `PUSH_PAYLOAD_SCHEMA_VERSION` |
| `push.py` | **新建**：`push_env_enabled()`、`try_push()`、`evaluate_push()`、`build_push_payload()`、`PushResult` / `PushError` / `PushDisabled` / `PushRejected` |
| `__init__.py` | 导出 `try_push` / `push_env_enabled` / `PUSH_ENV_NAME` / `Push*`；模块头补 gpush 段 |
| `main.py` | lifespan 在 store 段之后：**仅当** `BUY_SELL_SIGNALS_PUSH` 开时挂 `app.state.buy_sell_signal_push`；不调用 `try_push` |
| `docs` | 本节 + 上文口径改写 |

### 通道（写死，不得扩）

| 通道 | 实现 | 落点 |
|---|---|---|
| SSE | `QuoteService.push_alerts(list[dict])` → `_broadcast_alerts` → 各 `QuoteSubscriber` | 前端 toast / 订阅队列 |
| 站内 | `alert_store.append_many(data_dir, [event])` | `data_dir/user_data/alerts.jsonl` |

**不新建**推送总线、不新建 SSE 路由（`sse_starlette` 一行都不 import）、不接飞书
（`send_feishu` / `_maybe_send_webhook` 都不调）。`monitor_rules` 里那段
"落盘 → SSE → 飞书"的三步链条，本模块**只取前两步**（钉钉在 gding 另按独立门追加）。

**顺序：先站内、后 SSE**。站内是持久化的，SSE 队列满会丢最旧 —— 先写不会丢的那份。

### 触发条件（全部满足才推）

1. `status == "publishable"` —— 唯一入口仍是 `manual_publish()`
2. `push_gate == "armed"`
3. 按**入口时刻**判定冷却已过（不直接读 `push_eligible`，那个属性内部用系统时钟）
4. `event_class != "limit_reopen"` —— 炸板/回封不推
5. **状态变化**：`last_pushed_at is None` 或 `last_status_change_at > last_pushed_at`

### 「同状态不推」优先于「冷却期满回 armed」

这两条会撞车，优先级写死：

- 冷却期满只把闸门从 `cooldown` **恢复**到 `armed`，恢复本身**不产生推送**
- 恢复后仍要过第 5 条。状态没变 → 照样拒
- 想让同一条信号再推一次，必须**先有新的状态变化**，再等冷却过点

理由：冷却的语义是"别重复播报同一条消息"，不是"时间到了就再播一遍"。反过来做，
用户会在没有任何新信息的情况下周期性收到同一条信号。

### 成功后写回

- `last_pushed_at = now`
- `push_dedupe_cooldown_until = now + PUSH_DEDUPE_COOLDOWN_MIN`（30 分钟，包内既有常量）
- `push_gate = "cooldown"`（经 `state_machine.set_push_gate`）

**写回时机**：两个通道都发过之后才写。任一通道抛错 → 不写回，返回 `ok=False`。
宁可下一次重复推一次，也不要出现"标记为已推、其实没人收到"。

### 怎么开

```bash
# 默认: 推送关, try_push 一行都不写
python -m app.main

# 推送开: SSE + 站内
BUY_SELL_SIGNALS_PUSH=1 python -m app.main

# 落盘(与推送正交, 互不推导)
BUY_SELL_SIGNALS_PERSIST=1 python -m app.main
```

生产是否置 `BUY_SELL_SIGNALS_PUSH=1` **另等部署口令**，不随合入默开。
（`BUY_SELL_SIGNALS_PERSIST=1` 已上，两者勿混为一谈。）

### 没改什么（逐条钉住）

- **白名单未动**：`WHITELIST_TRIAL_QUANTILE = 0.60`、`WHITELIST_PERCENTILE_LOCKED = None`
- **persist 语义未动**：`PERSIST_*` / `registry.json` 路径 / 真值集合全部原样
- **无自动晋升**：没新增状态机边；炸板仍禁 `publishable`
- **日管道未接**：`daily_pipeline.py` 里仍无 `buy_sell_signals` / `try_push`
- **无飞书**（gding 起正式取消，路线图移除）：`push.py` 的 AST 里连 `send_feishu` /
  `_maybe_send_webhook` / `webhook_adapter` 这些标识符都不许出现（有测试逐符号扫）
- **无提醒层耦合**：不 import `trade_marks` / `news.push` 的 BAND 与 cooldown，
  冷却只用包内 `PUSH_DEDUPE_COOLDOWN_MIN`

### gpush 验收自检（ALIGN §5）

- [x] 基线 tip = `08e0cb26…`
- [x] 改动焦点在 `buy_sell_signals` + tests/docs；`main.py` 仅多一段 lifespan；无新建推送总线 / 独立 SSE 路由
- [x] env 名 = `BUY_SELL_SIGNALS_PUSH`；未设 / 单测默认 → 不真实推送
- [x] 仅 `publishable` + `armed` + 冷却已过 + 状态变化才推；同状态重算不推
- [x] 成功后 `last_pushed_at` 有值；冷却 = now + 30 分钟；冷却期阻断
- [x] 通道 = SSE + 站内（复用 `QuoteService.push_alerts`）；本派无飞书（gding 起正式取消）
- [x] 载荷含合规脚注（`DISCLAIMER_TEXT`）与 `monitor_event_type`
- [x] 无自动 `publishable`；炸板不可推；无日管道自动扫晋升
- [x] 无 import `trade_marks` / `news.push` BAND·cooldown；与 `t_rounds` 零耦合
- [x] 文档写清口径；脚注保留
- [x] `WHITELIST_PERCENTILE_LOCKED is None`；`WHITELIST_TRIAL_QUANTILE == 0.60`
- [x] 未接竞价/放量事件生成；未实装估值/健康；未开 MCP 交易
- [x] 单测全绿

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*

## gding 可选钉钉（批次 20261025gding）

**一句话**：在 #205 的 `try_push` 资格门之后，接**既有钉钉 webhook** 作为外部通道，
另加一道 `BUY_SELL_SIGNALS_DINGTALK`（默认关，与 `BUY_SELL_SIGNALS_PUSH` 是**与**关系）。
**飞书已取消**——不是"以后再说"，是从路线图上去掉了。

### 为什么外部通道要独立一道门

生产 `BUY_SELL_SIGNALS_PUSH=1` 早已打开，`DINGTALK_WEBHOOK_URL` 多半也为资讯推送配好了。
如果钉钉只认 PUSH 门，本批代码**一合入、一重启就会立刻往既有群里外发**。

站内消息发错了，刷新一下就没了；群里发错了，收不回来。独立门的价值就在这条非对称上：

    真实外发到钉钉 = push_env_enabled() and dingtalk_env_enabled() and webhook 非空

缺任一 → 钉钉**静默跳过**，站内/SSE 照常，冷却照写。生产置 1 **另等部署口令**，
**合入 ≠ 开钉钉**。

### 改了什么

| 位置 | 变化 |
|---|---|
| `constants.py` | 新增 `DINGTALK_ENV_NAME = "BUY_SELL_SIGNALS_DINGTALK"`；`PUSH_ENV_NAME` 说明改写（飞书取消、外部通道另门控）；**语义未动** |
| `push.py` | 新增 `dingtalk_env_enabled()` / `dingtalk_webhook_ready()` / `build_dingtalk_text()` / `_emit_dingtalk()`；`PushResult` 增 `dingtalk_sent`，`channels` 纳入 `"dingtalk"`；`try_push` 在核心扇出成功后、写回冷却前插入钉钉支路 |
| `__init__.py` | 导出 `DINGTALK_ENV_NAME` / `dingtalk_env_enabled` / `dingtalk_webhook_ready` / `build_dingtalk_text`；模块头补 gding 段，并把旧的「飞书待办」措辞改为「飞书已取消」 |
| `main.py` | lifespan 注释与 info 日志改为「SSE/站内；钉钉另受 BUY_SELL_SIGNALS_DINGTALK 门控」；`buy_sell_signal_push` 增两个**只读**标志 `dingtalk_env` / `dingtalk_webhook` |
| `tests/` | 新建 `test_buy_sell_signal_dingtalk.py`（17 条）；`test_buy_sell_signal_push.py` 的 import 白名单放行 `app.news.dingtalk` / `app.config` |
| `docs` | 本节 + 上文口径改写 |

### 通道：复用既有机器人，不新建

| 项 | 取值 |
|---|---|
| 发送函数 | `app.news.dingtalk.send_markdown(webhook, secret, title, text)` —— 与资讯 / ETF / 价位提醒**同一条路径** |
| 凭证 | 既有 `settings.dingtalk_webhook_url` / `settings.dingtalk_secret`（env `DINGTALK_WEBHOOK_URL` / `DINGTALK_SECRET`） |
| 标题 | `买卖信号 · {display_name}`，截断到 64 字符 |
| 正文 | 信号名 + id、状态 / 闸门 / 版本、事件 / 家族、`message`、合规脚注单列一段 |

**不新建** webhook 配置项、不新建机器人、**不复制** `signed_url` 的 HMAC 加签实现
（AST 测试禁 `hmac` / `hashlib` / `base64` / `urllib.request`）。**不转发新闻正文**——
本包不持有正文，载荷里也没有正文位。

### 触发：与 #205 完全一致，钉钉没有自己的时钟

1. `status == "publishable"` —— 唯一入口仍是 `manual_publish()`
2. `push_gate == "armed"`
3. 按**入口时刻**判定冷却已过
4. `event_class != "limit_reopen"`
5. **状态变化**：`last_pushed_at is None` 或 `last_status_change_at > last_pushed_at`

钉钉支路不是第六个条件，而是同一次 `try_push` 的第四个动作。

### 冷却只写一次，但两侧的失败策略不同

同一次状态变化可以**同时** SSE + 站内 + 钉钉，`last_pushed_at` /
`push_dedupe_cooldown_until`（now + 30）/ `push_gate=cooldown` **只写一次**，
由 `try_push` 尾部统一写回。钉钉支路**不自带** `last_pushed_at` 写操作，也不另算冷却。

| 谁失败 | 写回 | 返回 | 钉钉 |
|---|---|---|---|
| 核心通道（站内/SSE） | **不写** | `ok=False` | **不发**（钉钉挂在核心成功之后） |
| 钉钉 | **照写一次** | `ok=True`（`dingtalk_sent=False`） | 已尝试一次，不重试 |

两条策略不矛盾，各自守的是"哪一侧的重复更不可接受"：核心挂了不写回，是宁可下次重推；
钉钉挂了仍写回，是不愿为外部通道的抖动让站内/SSE 再播一遍。

钉钉失败只 `logger.warning(..., signal_id=..., 异常文本)`，**不打印** url / secret / token。

### 怎么开

```bash
# 默认: 三个门全关, try_push 一行都不写
python -m app.main

# 站内 + SSE(生产现状)
BUY_SELL_SIGNALS_PUSH=1 python -m app.main

# 站内 + SSE + 钉钉(还需既有 DINGTALK_WEBHOOK_URL 非空)
BUY_SELL_SIGNALS_PUSH=1 BUY_SELL_SIGNALS_DINGTALK=1 python -m app.main

# 只落盘不推(仍是最小面, 与上面三者正交)
BUY_SELL_SIGNALS_PERSIST=1 python -m app.main
```

`BUY_SELL_SIGNALS_PERSIST=1` / `BUY_SELL_SIGNALS_PUSH=1` 已上；`BUY_SELL_SIGNALS_DINGTALK`
**另等部署口令**，不随合入默开。

### 没改什么（逐条钉住）

- **白名单未动**：`WHITELIST_TRIAL_QUANTILE = 0.60`、`WHITELIST_PERCENTILE_LOCKED = None`，
  `quality_rank.py` 一行未改
- **PUSH / PERSIST 语义未动**：`PUSH_ENV_NAME` / `push_env_enabled` / `evaluate_push` /
  `PERSIST_*` / `registry.json` 路径 / 真值集合全部原样
- **无飞书**：`send_feishu` / `_maybe_send_webhook` / `webhook_adapter` 仍是禁用标识符
- **无提醒层耦合**：不 import `trade_marks` / `news.push` 的 BAND 与 cooldown；
  允许调 `app.news.dingtalk` **发送函数**，仍禁 import `app.news.push` 业务逻辑
- **无自动晋升 / 无日管道自动扫 / 无 MCP 交易**
- **无竞价/放量事件**、估值/健康仍 `pending`

### gding 验收自检（ALIGN §5）

- [x] 基线 tip = `285a48195f4e2fecad697def82232c7d62a309d5`（含 #205）
- [x] 改动焦点 `buy_sell_signals`（+ tests/docs）；`main.py` 仅文案与一段只读标志；无新建推送总线 / 独立 SSE / 平行机器人
- [x] env 名 = `BUY_SELL_SIGNALS_DINGTALK`；未设 / 单测默认 → 零真实钉钉；与 PUSH 为**与**关系
- [x] 仅 `publishable` + `armed` + `push_eligible` 且状态变化才推；同状态重算不推
- [x] 同一次可 SSE + 站内 + 钉钉，但**只记一次** `last_pushed_at`；冷却 = now + 30；冷却期阻断
- [x] 无 webhook 时钉钉静默跳过且不影响 SSE/站内
- [x] 通道外部 = 既有钉钉 webhook；**无飞书**（路线图已移除）
- [x] 载荷含合规脚注；不转发新闻正文；token / URL 不进日志明文（有 caplog 断言）
- [x] 无自动 `publishable`；炸板不可推；无日管道自动扫晋升
- [x] 无 import `trade_marks` / `news.push` BAND·cooldown；与 `t_rounds` 零耦合
- [x] 文档写清「落盘已开 · 推送已开(SSE/站内) · 钉钉可选(env) · 飞书已取消 · 竞价/放量事件仍未接」
- [x] `WHITELIST_PERCENTILE_LOCKED is None`；`WHITELIST_TRIAL_QUANTILE == 0.60`
- [x] 未接竞价/放量事件生成；未实装估值/健康；未开 MCP 交易
- [x] 单测全绿

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*

---

## gval 估值/健康窄开（批次 20261025gval）

> 基线 tip `cd0b7406bc2d9d6b0f51f5196f1a27ac70010b8f`（含 #207 hfeat squash）。
> 本拍只在 `buy_sell_signals/`（+ tests/docs）落改动，**未部署**。

### 这一拍到底改了什么

一句话：**它几乎没改行为，只把契约钉死了。** 估值与健康在 g / g2 阶段就已经是桩，
这一拍不把桩变成引擎，而是让"桩该是什么形状"从注释里的约定变成**可被测例扫描的符号**。

| 文件 | 改了什么 | 没改什么 |
|---|---|---|
| `valuation_stub.py` | 新增 `VALUATION_SNAPSHOT_PE_FORBIDDEN` / `VALUATION_SNAPSHOT_SOURCE_PREFIXES` / `score_emitted`；REQUIRED_INPUTS 逐项说明缺什么 | `v_score` 仍恒 `None`；签名不变 |
| `health_veto_stub.py` | 新增草稿原因码目录 + 禁止码集 + `passed` 属性；`if hit:` 语义写成表 | `hit` 仍恒 `None`；版本仍 `design_pending` |
| `quality_proxy.py` | 字段契约表 + 原因码闭集常量；`w_value` 进 `to_dict`；0–5 校正为 0–7 | 七列未增删；PIT 判据未动；权重仍 `None` |
| `quality_rank.py` | 一行 docstring（0–5 → 0–7） | `0.60` / `p40` / `locked=None` 一个字节没动 |
| `__init__.py` | gval 说明 + 导出 7 个新符号 | 三 env 导出原样 |

### 软提醒 5 条（原样保留，不降档）

1. **picker / `selection_fields` 的 `pe` 是当日快照，不是历史估值 PIT。** 它没有随日线
   滚动的价格，也没有 price_date × announce_date 的配对。拿它做 PIT 会得到一条"能跑完、
   每个数字都对不上"的序列，而且**不报错**。本拍把这条禁令落成常量
   `VALUATION_SNAPSHOT_PE_FORBIDDEN = True`，并有用例扫"`v_score` 从未被赋值"。
2. **股本 PIT 本机有 ≠ 估值表齐。** 还缺价 × EPS/BPS 的逐日配对，所以 `data_pending`
   仍是对的。四个必需输入缺任何一项都不足以去桩。
3. **`hit=None` 被 `if hit:` 当成假值时，语义是"不阻止"，不是"已健康通过"。**
   见下一节，这是本拍最要紧的一条。
4. **默认不自动部署。** 合入另等盘后口令；本拍默认可不部署。
5. **合入 ≠ 开新闸。** 本拍不要求、也不接受改 `DINGTALK` / `PUSH` / `PERSIST`。

### `if hit:` 到底是什么（写成表，别靠记）

```python
hit = health_veto_asof(sym, asof).health_veto_hit   # 当前恒 None
```

| 表达式 | 当前取值 | 语义 |
|---|---|---|
| `if hit:` | 不进分支 | **不阻止**（因为未知），**不是**"已健康通过" |
| `if not hit:` | 进分支 | 把"未知"当成"健康" —— **误用** |
| `if hit is False:` | 不成立 | 唯一合法的"健康通过"判据，当前永不成立 |
| `if hit is None:` | 成立 | "未知"的唯一正确判据 |

`False` 是一个**结论**（"查过，没命中"）。没有任何规则就返回 `False`，等于向调用方保证
一件我们并不知道的事，而调用方极可能把它当"健康预警已通过"写进日志或界面。所以：

- `health_veto_hit` **恒 `None`**，`blocks_publish` 也返回 `None`；
- 新增 `HealthVetoStub.passed`，实现是 `self.health_veto_hit is False` ——
  刻意的 `is False`，不是 `not ...`；当前恒 `False`；
- 有测例同时断言 `blocked is False`、`passed is False`、`unknown is True`，
  把"不阻止 ≠ 已通过"钉在代码里，而不是只写在文档里。

### 健康原因码草稿（PHASE2 §3.3，本拍钉入常量）

`HEALTH_VETO_REASON_CATALOG_DRAFT`（**草稿**，`HEALTH_REASON_CATALOG_STATUS = "draft"`）：

| 原因码 | 含义（示意） | 触发需要的数据 |
|---|---|---|
| `audit_opinion_bad` | 审计非标 / 无法表示意见 | 审计意见字段（本机未确认可得） |
| `leverage_stress` | 杠杆 / 偿债预警 | `debt_to_asset_ratio` 的跨行业阈值 |
| `cashflow_stress` | 现金流恶化 | 经营现金流序列（阈值未定） |
| `regulatory_risk` | 监管 / 立案 | 公告事件（本机未确认可得） |
| `other_rule` | 升版后扩展 | — |

这五码是**拦下码**，不是通过码：语义是"发现了这个问题"。理解反了，门禁方向就整个翻转。

**禁止码**：`HEALTH_VETO_FORBIDDEN_PASS_CODES = {"data_insufficient_treat_as_pass"}`。

它的意思是"数据不足 → 当成通过"，把未知当健康，与 `hit=None` 的整套设计意图正相反。
缺数据的正确映射是**未知**（`hit=None`）或 `data_abnormal`，**绝不是** pass。本拍不实装
映射，只把禁令落成常量，由两条测例守着（草稿目录里不含它 + 禁止集里含它）。

落地前仍缺三件事，缺一件就不能产出 `hit=true/false`：`rule_catalog` 评审定稿 ·
`historical_replay` · `threshold_calibration`（`HEALTH_RULE_DESIGN_REQUIRED`）。

**健康不进权重**：包里没有 `w_health` 这个字段，也不允许出现 —— 健康是 veto（一票否决），
把否决权摊成权重等于允许"用高估值抵消财务异常"。有 AST 扫描守着。

### 质量契约：字段是什么，不是什么

`q_score` 是**计数**不是强度：判据是"取值为正"，`roe=0.1%` 与 `roe=80%` 都只贡献 1 分。
所以它能做粗筛，不能做排序。本拍把这条写成测例（幅度放大几千倍，`q_score` 不变）。

| 字段 | 是什么 | **不是**什么 |
|---|---|---|
| `q_score` | 七列里取值为正的项数，0–7 | 不是质量因子值，不能当 alpha |
| `score_emitted` | `q_score is not None` | 不表示"分数可用" |
| `q_missing_flag` | 有任一原因码，**或** `q_score is None` | 不表示"质量差" |
| `q_missing_reasons` | 闭集原因码 | 不是自然语言说明 |
| `metrics_asof_announce` | 那期财报的**公告日** | 不是报告期末 |
| `metrics_period_end` | 那期财报的**报告期末** | 不是可知日 —— 二者不可互换 |
| `w_quality` / `w_value` | 恒 `None` | 不是"待填的 0" |

原因码闭集（`QUALITY_MISSING_REASON_CODES` + `QUALITY_MISSING_REASON_PREFIXES`）：

`no_financials_known_at_asof` · `missing_column:<col>` · `null_metric:<col>` ·
`bad_metric:<col>` · `no_kline_source` · `no_kline_rows` · `kline_coverage_low:<x>`

PIT 与 null 口径这一拍**没有改动**，只是补了覆盖：公告日前一天 → 全部 `None` 且
`q_missing_reasons` 含 `no_financials_known_at_asof`；公告日**当天**即已知（`<=` 而非 `<`）；
单列 null 保持 `None`、不进 0，其余列照常出分（缺失按列，不是整条腿作废）。

### 隔离：本拍什么都没动

| 约束 | 守它的手段 |
|---|---|
| 三 env 名与语义不改 | 断言 `PERSIST` / `PUSH` / `DINGTALK` 字面量；扫源码无 `os.environ[...]=` / `putenv` |
| 真值集合不变 | 从 `store._TRUTHY` 与 `push._TRUTHY` 各取一份，断言都等于 `{1,true,yes,on}` |
| 冷却不变 | `PUSH_DEDUPE_COOLDOWN_MIN == 30` |
| 白名单未锁 | `WHITELIST_PERCENTILE_LOCKED is None`、`THRESHOLD_LOCKED is False`、`== 0.60`；扫全包无别处给它赋值 |
| 无假 `v_score` | AST 扫 `v_score` 的 `AnnAssign` 默认值必须 `None`，且无运行期赋值 |
| 无 `hit=False` 伪装 | AST 扫 `health_veto_hit` 不被赋 `False` |
| 无 `w_health` | 扫去掉字符串后的代码 |
| 无竞价冒充 | 扫 `auction_backtest` / `pre_open_1m` / `as_auction`；`AUCTION_STAT_BUCKET_0925 == "auction_0925"` |
| 无提醒层耦合 | 扫 import：`app.services.trade_marks` / `app.news.push` / `VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN` |
| 与 `t_rounds` 正交 | 扫 import 与 `EPSILON_VALUE_PCT` |
| 文案红线 | 扫「估值已测通 / 健康门禁已验证 / 白名单已锁定 / 正式可推」 |

> 静态扫的一个坑：**不能直接搜文本**。`valuation_stub` 的文档里必须能写"`pe_ttm` 不准用"，
> `quality_proxy` 的文档里必须能写"没有 `w_health` 这个字段" —— 全文搜索会把正当的
> 解释判成违规。做法是把 AST 里所有字符串字面量换掉再 dump，再扫代码部分。

### 测试

```bash
cd backend && python -m pytest \
  tests/test_buy_sell_signal_quality_proxy.py \
  tests/test_buy_sell_signal_quality_rank.py \
  tests/test_buy_sell_signal_gval_isolation.py \
  -q
```

三条文件共 120 条。全部用**显式夹具**（小 DataFrame），**不读**本机 `financials_metrics`
parquet，不联网、不带凭证。夹具不得被写成"生产已测通"的结论。

### gval 明确不做

- 逐日 PE/PB 采集任务；任何 `v_score` 实装打分
- 健康规则引擎（`hit=true/false`）、阈值校准、历史回放
- 白名单锁定与 p30/p50 敏感性（P1-2）
- 竞价 / 放量事件（P1-3）
- 改生产 `PERSIST` / `PUSH` / `DINGTALK`；日管道接线；MCP 交易

### gval 验收自检（ALIGN §5）

- [x] 基线 tip = `cd0b7406bc2d9d6b0f51f5196f1a27ac70010b8f`（`main` 未前移，无偏差）；纯增量于 `buy_sell_signals`（+tests/docs）
- [x] `valuation_asof` → `data_pending=True`，`v_score is None`；无 pe 快照冒充 PIT
- [x] `VALUATION_REQUIRED_INPUTS` 单测可见（四项逐字断言）；`tested_status == "untested"`
- [x] `health_veto_asof` → `hit is None`，`rule_version == "design_pending"`；无 `hit=False` 伪装
- [x] 原因码草稿含 PHASE2 五码；`data_insufficient_treat_as_pass` 不在草稿里、在禁止集里
- [x] 文档写清：`hit=None` + `if hit:` ⇒「不阻止」≠「已健康通过」
- [x] 健康不进权重（无 `w_health`）；`w_quality` / `w_value` 仍 `None` + `design_pending`
- [x] 质量：PIT = `announce_date <= asof`；null ≠ 0；未新造因子；夹具不依赖真实 metrics 文件
- [x] 白名单 `locked is None`；试跑 `0.60` / `p40` 未被本拍改写
- [x] 未改 PERSIST / PUSH / DINGTALK env 语义或生产值；冷却 `30` 未动
- [x] 无竞价/放量事件；无 09:30 冒充 09:25
- [x] 单测不依赖真实 PE/PB 表；缺表路径显式 `data_pending`
- [x] 文案无「已测通 / 已锁定 / 正式可推」；脚注可见
- [x] 软提醒 5 条写入本节
- [x] 默认不自动部署；合入另等盘后口令

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*
