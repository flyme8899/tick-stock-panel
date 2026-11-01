# 买卖信号骨架（批次 g + g2 窄化 + gp 可选落盘 + gpush 可选推送 + gding 可选钉钉 + gval 估值健康窄开 + gwlist 白名单未锁 + gauction 放量未开 + gauction2 竞价窄开）

> **仅供研究，不构成投资建议；执行须人工确认。**

本文说明 `backend/app/buy_sell_signals/` 这个包**做了什么**、**明确不做什么**，以及
为什么几条硬约束写成了常量而不是配置项。

> 20261025g2 的改动见文末 [g2 窄化](#g2-窄化批次-20261025g2) 一节；
> 20261025gp 的改动见文末 [gp 可选落盘](#gp-可选落盘批次-20261025gp) 一节；
> 20261025gpush 的改动见文末 [gpush 可选推送](#gpush-可选推送批次-20261025gpush) 一节；
> 20261025gding 的改动见文末 [gding 可选钉钉](#gding-可选钉钉批次-20261025gding) 一节；
> 20261025gval 的改动见文末 [gval 估值/健康窄开](#gval-估值健康窄开批次-20261025gval) 一节。

**现行口径：落盘已开 · 推送已开(SSE/站内) · 钉钉可选(env) · 飞书已取消 · 放量身份/契约已钉、触发仍 pending · 竞价身份/只读适配已钉、四缺口 pending(B1 部分解阻 · B2 分钟 09:25 仍缺) · 估值/健康仍 pending。**

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

---

## gwlist 白名单锁定窄开（批次 20261025gwlist · P1-2）

> 基线 tip `c7f3ca940f44b38badb10f0194eeafe7c7045aa9`（含 #208 gval squash）。
> 本拍只在 `buy_sell_signals/`（+ tests/docs）落改动，**未部署**。

### 一句话：窄开 = 勿真锁

**这一拍不改阈值、不锁白名单，只把"为什么还不能锁"写成可扫的常量与注释。**

`WHITELIST_PERCENTILE_LOCKED` 仍是 `None`，`WHITELIST_TRIAL_QUANTILE` 仍是 `0.60`，
`WHITELIST_PERCENTILE_TRIAL` 仍是 `"p40"` —— 三个字节都没动。改动只有两类：

1. `WHITELIST_LOCK_EVIDENCE_PENDING`：四条 A–D 证据缺口的**机器可读标签**；
2. `WHITELIST_LOCK_EVIDENCE`：同一批缺口的**人读表**（key 与上面完全一致，有测例对比）。

加这两个常量的目的不是"准备锁"，而是让**将来某一次顺手锁**会撞到测例。锁白名单是一个
研究结论，不是一个代码配置项；把结论写进常量而不写测例，等于给它开了后门。

### 口径复核：`p40` 是标签，不是阈值

这一拍最重要的一句复核，因为它在 #202 曾被写错过一次：

| 概念 | 值 | 说明 |
|---|---|---|
| `WHITELIST_PERCENTILE_TRIAL` | `"p40"` | **标签**，字符串。"行业前 40%"这件事的名字 |
| `WHITELIST_TRIAL_QUANTILE` | `0.60` | **数值**，`1 - 0.40`。`q_rank_within_ind` 的比较对象 |
| 判定式 | `pct >= 0.60` | **闭区间**。恰好 0.60 算通过 |
| `>= 0.40` | —— | **#202 遗留、已退休**。它等于"行业前 60%"，不是 p40 |

"p40" 里的 40 是**通过比例**，不是分位阈值。`q_rank_within_ind` 越高越好，所以
"前 40%"落在 `>= 1 - 0.40 = 0.60`。把 `0.40` 直接拿来比较，方向会反成"前 60%"，
而且**不会报错**，只会让名单变长——这正是当年写错时没被发现的原因。

`WHITELIST_THRESHOLD_LOCKED` 是**推导**出来的（`WHITELIST_PERCENTILE_LOCKED is not None`），
不是手写常量。有测例扫它的源码形状必须是 `ast.Compare` + `ast.IsNot`：手写一个 `False`
在锁生效时不会跟着变 `True`，等于留了一条静默失效的路。

### A–D 证据缺口：四条都还是 `data_pending`

ALIGN §2.2 的四道锁前门禁。**四条一条都没跑**，所以 `LOCKED` 只能是 `None`。

| 门 | 要什么 | 现状 | 缺什么 |
|---|---|---|---|
| **A 敏感性** | 0.55 / 0.60 / 0.65（或纸面 p30 / p40 / p50）同尺对照，训练 / 验证**同号** | `data_pending` | 分位附近的对照组合未跑，无报告 |
| **B 样本外** | 逐年 + 近 3 年，去前 5 日稳健（drop5） | `data_pending` | 未跑，无报告 |
| **C 成本** | 来回 **18.2bp** 实测回填 | `data_pending` | 18.2bp 目前**仅作锚**；分档滑点实测仍缺口 |
| **D 显著性** | 买入日聚类 t + Bonferroni 的 p 值实测；m = 6 | `data_pending` | **分母已钉 ≠ p 值已测** |

**A 的"同号"是最容易被忽略的一条。** 只要在 0.55 / 0.60 / 0.65 三档里有一档方向翻转，
就说明超额收益对阈值敏感——那种情况下锁哪一档都是挑数据挑出来的，不是稳健结论。

**C 的 18.2bp 只是锚，不是实测。** 它来自一次口径估算，用来估算"成本大概吃掉多少"，
不能回填成"成本已计入"。分档滑点（按成交额 / 波动分桶）的实测还没做，小市值票的真实
摩擦会明显高于 18.2bp——而白名单筛的恰恰容易偏中小市值。

**D 的 Bonferroni m = 6 是分母，不是结果。** `BONFERRONI_M = 6` 与
`BONFERRONI_FAMILY_ALPHA = 0.05 / 6 ≈ 0.008333` 只是把"同时检验 6 个族成员"这件事钉下来，
**没有任何 p 值被算出来**。分母定了 ≠ 显著性测过了，这两件事在汇报里经常被混成一句。

### 软提醒 6 条（原样保留，不降档）

1. **本拍不锁白名单。** `WHITELIST_PERCENTILE_LOCKED is None` 是本拍的核心交付，
   不是"还没来得及写"。任何把它改成 `"p40"` / `"p50"` / `0.6` 的提交都与本拍结论相反。
2. **试跑值不得写进 locked。** 试跑的 0.60 是"拿来观察的"，locked 是"拿来执行的"。
   把前者抄进后者，等于把一次未验证的观察升级成生产规则，而代码上看不出区别。
3. **A–D 仍是 `data_pending`。** 四条里任一条没跑，锁定就没有依据。删掉 pending 标签
   比删掉代码更危险——代码删了会红，标签删了不会。
4. **`q_score` 是计数，不是强度。** 白名单是在这个粗筛之上再排名，两层都不能当 alpha 用。
   详见 gval 节的字段契约表。
5. **默认不自动部署。** 合入另等盘后口令；本拍默认可不部署。
6. **合入 ≠ 开新闸。** 本拍不要求、也不接受改 `DINGTALK` / `PUSH` / `PERSIST`。

### 真锁另拍：锁的是一份报告，不是一次改常量

将来真锁时要交的不是这四行常量变非空，而是一份能回答这四个问题的报告：

- 换 0.55 / 0.65，名单变动多少？训练段和验证段**方向是否一致**？
- 逐年看，有没有某一年整体失效？去掉最好的 5 天后还剩多少？
- 把成本从 18.2bp 锚换成**实测分档**，净超额还剩多少？
- 6 个族成员里，白名单这一项的 p 值过没过 `0.05 / 6`？

四个问题都有数，才谈得上把 `None` 改成 `"p40"`。反过来，先改常量再补论证，
论证很容易变成"为已锁的结论找证据"。

### 隔离：本拍什么都没动

| 约束 | 守它的手段 |
|---|---|
| 试跑口径不变 | `TRIAL == "p40"`、`QUANTILE == 0.60`、判定是 `>=`（闭区间） |
| 无 `0.40` 冒充当前规则 | 扫代码常量里不出现 `0.4` |
| 白名单未锁定 | `LOCKED is None`、`THRESHOLD_LOCKED is False` 且**推导**而来 |
| 全包无别处赋值 | AST 扫：任何非空值赋给 `LOCKED` 一律判违规 |
| A–D 四条齐全 | 标签集恰好 4 个、全部以 `pending` 结尾、人读表 key 与之相等 |
| pending 不进判定 | 扫判定行附近不引用 pending 集（不能变成"锁依据"） |
| 三 env 名与语义不改 | 断言 `PERSIST` / `PUSH` / `DINGTALK` 字面量；扫源码无 `os.environ[...]=` / `putenv` |
| 真值集合不变 | `store._TRUTHY` 与 `push._TRUTHY` 都等于 `{1,true,yes,on}` |
| 质量/估值/健康零碰 | #208 三模块无任何锁逻辑；`quality_rank` 里不出现 `v_score` / `health_veto_hit` |
| 无锁引擎 | 全包无 lock engine 模块 |
| 无竞价冒充 | 扫 `auction_backtest` / `pre_open_1m` / `as_auction` |
| 无提醒层耦合 | 扫 import：`app.services.trade_marks` / `app.news.push` / `VWAP_BAND` / `T_COOLDOWN_MIN` |
| 与 `t_rounds` 正交 | 扫 import 与 `EPSILON_VALUE_PCT` |
| 文案红线 | 扫「白名单已锁定 / 锁定已生效 / 验证通过」 |

> 静态扫的同一个坑，本拍再踩不到第二遍：**不能直接搜文本**。本包的文档必须能写
> "禁止把 `p40` 写进 LOCKED"，全文搜索会把正当的禁令判成违规。做法仍是把 AST 里
> 所有字符串字面量换成占位符再 dump，只扫代码部分。

### 测试

```bash
cd backend && python -m pytest \
  tests/test_buy_sell_signal_quality_rank.py \
  tests/test_buy_sell_signal_gval_isolation.py \
  tests/test_buy_sell_signal_gwlist_isolation.py \
  -q
```

第三条（gwlist 隔离扫）是本拍的新增主体。它的分量故意大于代码改动：本拍代码几乎没变，
真正的风险是**将来某一次"顺手"**，而这些顺手一个都不会让既有测例变红。

### gwlist 明确不做

- 把 `0.60` / `p40` 写进 `WHITELIST_PERCENTILE_LOCKED`；实装任何锁定流程
- 跑 A–D 任何一条（本机无成本模型、无分档滑点表、无买入日聚类样本）
- 估值打分实装、健康规则引擎、质量新因子
- 竞价 / 放量事件（P1-3）
- 改生产 `PERSIST` / `PUSH` / `DINGTALK`；日管道接线；MCP 交易

### gwlist 验收自检（ALIGN §5）

- [x] 基线 tip = `c7f3ca940f44b38badb10f0194eeafe7c7045aa9`（`main` 未前移，无偏差）；纯增量于 `buy_sell_signals`（+tests/docs）
- [x] `WHITELIST_PERCENTILE_LOCKED` 仍为 `None`；`WHITELIST_THRESHOLD_LOCKED is False`
- [x] 试跑 `"p40"` / `0.60` 未被改写；判定仍 `>=`（闭区间）；无 `>= 0.40` 遗留当现规
- [x] `THRESHOLD_LOCKED` 是**推导**值，非手写 `True`/`False`
- [x] A–D 四条 `data_pending` 常量与人读表齐备，key 一一对应；四条都写明缺什么
- [x] `BONFERRONI_M = 6` 与族 α 仅作分母；文档与注释都写明「分母已钉 ≠ p 值已测」
- [x] 18.2bp 明确标注为**锚**而非实测回填
- [x] 质量/估值/健康**零碰**（#208 行为字节不变）；`hit` 仍 `None`、`v_score` 仍 `None`
- [x] 未改 PERSIST / PUSH / DINGTALK env 语义或生产值；冷却 `30` 未动
- [x] 无竞价/放量事件；无锁引擎模块；与 `t_rounds` 无耦合
- [x] 文案无「已锁定 / 锁定已生效 / 验证通过」；脚注可见
- [x] 软提醒 6 条写入本节
- [x] 默认不自动部署；合入另等盘后口令

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*

---

## 放量窄开（gauction / gvol）（批次 20261025gauction · P1-3）

> 基线 tip `9dfba4ed2bff1ea8fe7300fb36b295828563fed1`（含 #209 gwlist squash）。
> 本拍只在 `buy_sell_signals/`（+ tests/docs）落改动，**未部署**。

### 一句话：窄开 = 勿真开，且竞价整段不做

**这一拍不建触发引擎、不钉任何阈值，只把「放量突破长什么样」钉成可扫常量，
并写清它为什么还不能跑。** 新增 `volume_breakout_stub.py` 一个空桩文件；
`models.py` 只加 docstring、`constants.py` 只加注释，**行为字节零改**。

### 身份钉（PHASE3 §3）

| 字段 | 值 | 说明 |
|---|---|---|
| `signal_id` | `"volume_breakout"` | 注册表唯一键 |
| `event_class` | `"intraday_volume"` | ∈ `EVENT_CLASSES`，**不是** `auction` |
| `family` | `"minute_event"` | ∈ `SIGNAL_FAMILIES`，意味着必须有分钟级证据 |
| `auction_stat_bucket` | `None`（恒） | 放量永不进竞价桶 |
| `auction_rule_id` | `None` | 沿用 `AUCTION_RULE_ID_NONE`：根本不经过竞价规则 |

**一个容易混的地方**：评估态与生命周期态是两个域。

| 域 | 取值 | 语义 |
|---|---|---|
| `VOLUME_BREAKOUT_STATUS`（评估） | `data_pending` | 数据没到，无法判断 |
| `SignalRecord.status`（生命周期） | `candidate` / `observe` / `publishable` / `retired` / `data_abnormal` | 在注册表里走到哪一步 |

`data_pending` **不在** `SIGNAL_STATUSES` 里，也**不应该**加进去 —— 往生命周期
取值域塞一个评估态，状态机会多出一条谁都不知道怎么迁移的状态。构造记录时用
`candidate`（还没验证），评估态走 `metadata` 带过去。

### 分列契约：放量不得写入 `auction_0925`

竞价是 09:25 集合竞价的**离散撮合**，放量是开盘后的**连续成交**。两者信息集不同，
合在一起算出来的均值 / 命中率没有解释意义（`NOISE_MIX_FORBIDDEN = True`）。

`SignalRecord` 在 tip 上已经有通用拒绝规则（非 `auction` 类不得带竞价桶），
本拍只是**显式覆盖放量这条路径**并测下来：`event_class="intraday_volume"` +
`auction_stat_bucket="auction_0925"` → **构造即抛 `SignalValidationError`**。

**缺竞价包不得用放量回填竞价。** 「09:25 那个桶空着，先用开盘后第一根 1 分钟 K 线
顶上」是**永久禁止**的做法：冒充不会报错，只会让竞价类信号的统计全部失真。

### 噪声档：可空桩，但不扩封闭集

`post_open_noise_bucket` 的合法取值是封闭集（`POST_OPEN_NOISE_BUCKETS`：
`open_0930_1000` / `intraday_1000_1430` / `close_1430_1500`）。

PHASE3 §3 的示意名 `post_open_15m` **尚未入集**，本拍**不扩集**。扩集等于悄悄修改
一个封闭取值域，而封闭域的意义正是"不让人随手往里加"。噪声档本拍只作空桩
（`False` / `None`）。

### 两条证据缺口：全 `data_pending`

| 缺口 | 现状 |
|---|---|
| `http_minute_listing_auth_pending` | 取分钟数据的 HTTP 入口 **401**，凭证未交；本仓不写、也不该写任何密码或 token |
| `threshold_lock_data_pending` | 量比 / 突破幅度 / 窗口分钟数均未锁定，**一个数值都没钉** |

> **`threshold_lock_data_pending` 是"负锁"，不是"待办"**（批次 `20261101gthresh`）。
> 它和上面那条并排放在同一个集合里，但两者缺的东西不一样：`http_minute` 缺一个
> **外部凭证**（拿到就能通），这条缺一个**解释** —— 四个口径回测全负，
> 负号解释清楚之前，补什么都不能清。语义已由
> [`threshold_neg_lock`](../backend/app/buy_sell_signals/threshold_neg_lock.py)
> （`threshold_neg_lock_v1`）契约化：`assert_threshold_lock_still_pending` 咬住
> "标签必须还在"，`assert_no_tradeable_threshold_nail` 咬住"禁钉可交易数"。
> **负锁 ≠ 删标签**；**钉数 ＝ fabricate**；**本拍 ≠ 清零刀**；
> **语义锁面 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING**；标签**仍开**、COUNT **仍 6**；
> 可钉证据路径清单见[门禁文档 §4.1.1](./buy-sell-signal-trigger-gate.md)。

> **已清（批次 20261101gevid）**：`trigger_evidence_table_data_pending` —— 已由
> [`volume_breakout_evidence_table`](../backend/app/buy_sell_signals/volume_breakout_evidence_table.py)
> 把既有负证据形式化为四行触发表而消解，**已从 pending 集合删除**（非改后缀留存）。
> **清零 ≠ 可翻闸**；**表 PRESENT ≠ 可开 TRIGGERING**；**strongly_negative ≠ 阈值已锁**。
>
> **已清（批次 20261101gevid3）**：`local_minute_lib_data_pending` —— 已由
> [`local_minute_lib`](../backend/app/buy_sell_signals/local_minute_lib.py)
> 把 `settings.data_dir / "kline_minute"`（Hive `date=*/part.parquet`）钉成
> `local_minute_lib_v1` 路径契约 + 只读探针而消解，**已从 pending 集合删除**。
> 运行时数据在 `data/kline_minute`（5.1G · 362 分区），gitignore `data/**`，**不入仓**；
> 测例用 `tmp_path` 夹具。**清零 ≠ 可翻闸**；**落盘 ≠ 可开 TRIGGERING**；**探针 PRESENT ≠ 可开 TRIGGERING**。

### 仓内已有的反证（本拍最该被看见的一条）

`app/custom/dsa/quant_evidence.yaml` 里的 `volume_breakout` 条目记录了四个近似口径的
回测结论：

| 口径 | 收益率 | 样本 |
|---|---|---|
| 放量创60日新高 | **−23.86%** | 78 |
| 趋势突破 | **−32.44%** | 77（夏普 −4.09） |
| 平台整理突破 | **−13.80%** | 79 |
| 布林突破 | **−19.81%** | 110 |

四个口径**全为负**，`verdict: strongly_negative`。这不是本拍造的数，是从既有文件抄来的，
并用测例核对源文件（`test_negative_evidence_matches_the_source_file`）。

所以这一拍更准确的说法不是「数据还没到，等到了就能跑」，而是
**「已有证据指向负期望，开闸前必须先解释这个负号」**。阈值可以标定，负号解释不了就不能开。

### 空桩语义：`triggered` 恒 `None`，不是 `False`

```python
stub = evaluate_volume_breakout("600000")   # 当前恒 data_pending
```

| 表达式 | 当前取值 | 语义 |
|---|---|---|
| `if stub.triggered:` | 不进分支 | **无法判断**，**不是**"没放量" |
| `if not stub.triggered:` | 进分支 | 把"不知道"当成"没放量" —— **误用** |
| `stub.triggered is False` | 不成立 | 唯一合法的"确认没放量"，当前永不成立 |
| `stub.passed` | `False` | 实现是 `triggered is True`，刻意的 `is True` |

`False` 是一个结论（"查过，没放量"）。没有阈值、没有分钟数据、没有触发表就返回
`False`，等于向调用方保证一件我们并不知道的事。量比与突破幅度同理，**恒 `None`** ——
填任何数都是假数。

### 软提醒 7 条（原样保留，不降档）

1. **竞价高阻塞已坐实**：无 09:25 包；分钟最早 09:30；**禁冒充**（永久）。
2. **放量有远端分钟源 ≠ 本机可跑**：本机无挂载 / 401 → 只 pending，不假装触达。
3. **MINUTE_CALIBER**：导出是混合复权；离线 VWAP / 量比**勿直接宣称门禁已测**。
4. **18.2bp 是成本锚不是已实测回填**；分档滑点仍缺口。
5. **Bonferroni 分母已钉 ≠ 显著性已测**；本派不跑 p（m=6，`independent_family_v1` 仅元数据）。
6. **默认不自动部署**；合入另等盘后口令；**合入 ≠ 改三 env**。
7. **飞书已取消**；**红涨绿跌不进假设族**；炸板仍观察。

### 本拍与另拍的边界（写进文档，免得下次重新讨论）

| 项 | 本拍 | 另拍条件 |
|---|---|---|
| 竞价 `auction_0925` 实现 / 回测 / 晨报计数 | **禁止** | 真实 09:25 定价包 + 字段可映射 PHASE3 §2 |
| 09:30 冒充 09:25 | **永久禁止** | —— |
| 放量触发引擎 + 阈值锁定 | **禁止**（仅 pending 桩） | 本机分钟证据 + 触发表 + **负号解释** |
| 真锁白名单 | **禁止** | A–D 证据齐（#209 另拍） |
| 估值打分 / 健康引擎 | **禁止** | #208 解 pending 另拍 |

### 测试

```bash
cd backend && python -m pytest \
  tests/test_buy_sell_signal_registry.py \
  tests/test_buy_sell_signal_gwlist_isolation.py \
  tests/test_buy_sell_signal_gval_isolation.py \
  tests/test_buy_sell_signal_gvol_isolation.py \
  -q
```

`test_buy_sell_signal_gvol_isolation.py` 是本拍新增主体。与 gwlist 同理：
本拍代码几乎没变，风险全在"将来某一次顺手"。

> 静态扫的同一个坑：**不能直接搜文本**。本包的文档必须能写"禁止把
> `volume_breakout` 写进 `auction_0925`"，全文搜索会把正当禁令判成违规。
> 做法仍是把 AST 字符串字面量换占位符再扫代码部分。

### gauction 明确不做

- 放量触发引擎、量比 / 突破幅度 / 窗口分钟数阈值（一个数值都没钉）
- 假量触发表、假胜率表、"放量已测通"叙事
- **竞价整段**：引擎 / 回测 / 晨报计数 / 09:25 路径（另等 09:25 真实包）
- 扩 `POST_OPEN_NOISE_BUCKETS`；削弱 `models` 混桶拒斥
- 改 `push.py` / `store.py` / `main.py` lifespan / `jobs/daily_pipeline.py` / `t_rounds`
- 改三 env；质量 / 估值 / 健康 / 白名单行为（#208 / #209 字节未改）

### gauction 验收自检（ALIGN §6）

- [x] 基线 tip `9dfba4ed…`（`main` 未前移）；纯增量于 `buy_sell_signals`（+tests/docs）
- [x] **竞价不在 diff 实现范围**（无 auction 引擎 / 回测 / 09:25 冒充路径）
- [x] 放量仅定义 / 契约 / 桩 + `data_pending`；无假量触发表
- [x] `auction_stat_bucket` 对放量路径为 `None`；混桶测例**构造即抛错**
- [x] 身份常量可见：`volume_breakout` / `intraday_volume` / bucket `None`
- [x] pending 四条（本机分钟库 / 触发表 / HTTP 凭证 / 阈值锁定）单测可见
  - **历史快照**：验收时为四条。现状已清两条（触发表 #265 / 本机分钟库 #-A-#9），
    剩两条（HTTP 凭证 / 阈值锁定）—— 现行账见门禁文档
    [§4.1](./buy-sell-signal-trigger-gate.md) 与
    [`local_minute_lib`](../backend/app/buy_sell_signals/local_minute_lib.py)。
- [x] 未回退 #208 估值 / 健康 pending；未回退 #209 `LOCKED=None` / `0.60` / `p40`
- [x] 未改 PERSIST / PUSH / DINGTALK；未碰 `t_rounds` / 飞书 / DSA / 17–19
- [x] 质量 / 估值 / 健康 / 白名单行为本拍零碰（#208 / #209 字节未改）
- [x] 文案无「竞价已就绪 / 放量正式可推 / 放量已测通 / 白名单已锁定」；脚注可见
- [x] 无假数填回测表；阈值未钉死为「已锁」
- [x] 未扩 `POST_OPEN_NOISE_BUCKETS`；未削弱 `models` 混桶拒斥
- [x] 单测全绿；gwlist / registry 回归仍绿
- [x] 软提醒 **7** 条写入本节
- [x] 默认不自动部署；合入另等盘后口令

*参数以 PARAM_NAIL / 回测门禁为准。仅供研究，不构成投资建议；执行须人工确认。*

---

## 竞价窄开（gauction2 · `auction_0925`）（批次 20261026gauction2）

> 本节是 **放量（#210 / gvol）的对称腿**：同一套「身份钉 + 空桩 + 证据缺口」骨架，
> 语义严格互斥。**不是重做放量**，`volume_breakout_stub.py` 一个字节未改。

### 从 blocked 到**部分**解阻

旧的 `20261025gauction` 拍把竞价整段标为 blocked，理由是 B1「没有竞价数据包」。
现在这条理由**部分**不成立了：生产环境有 `data/auction_scan/` 多日 JSON，
`get_scan` 能读到「当天命中了哪些票」。

但**只有 B1 部分解阻，B2 仍然 blocked**：

| 缺口 | 状态 | 说明 |
|---|---|---|
| **B1** 无竞价数据包 | **部分解阻** | 有 `auction_scan` 落盘存档可读；但落盘**没有** `auction_price` / `prev_close` |
| **B2** 分钟 09:25 bar | **仍 blocked** | 分钟库最早仍是 09:30；标签 `minute_0925_still_absent` 保留 |

**部分解阻 ≠ 整拍可推。** 能读到「那天命中了哪几只票」，和「这条信号经过验证」
是两件事。本节做的只是前者。

### 身份钉

| 字段 | 取值 | 说明 |
|---|---|---|
| `signal_id` | `auction_abnormal` | 全仓唯一的竞价异动信号 |
| `event_class` | `auction` | 唯一允许带竞价桶的类别 |
| `family` | `minute_event` | 必须有分钟级证据才能谈标定，不用日线代理糊 |
| `auction_stat_bucket` | `auction_0925`（**恒**） | 只有这一个桶 |
| `auction_rule_id` | `None` | 阈值未标定，本拍**不锁规则** |
| `post_open_noise_flag` | `False`（**恒**） | 09:25 早于连续竞价，不属于任何开盘后时段 |
| `post_open_noise_bucket` | `None`（**禁带**） | 带非 None 会被 `SignalRecord` 拒 |
| `triggering_enabled` | `False`（**恒**） | 改成 True 只会让空桩撒谎 |

`models.SignalRecord.__post_init__` 在 tip 上**已经**双向拒绝混桶（竞价带开盘后桶 →
拒；放量带 `auction_0925` → 拒）。本拍只把竞价这条路径**显式覆盖**并测下来，
没有重新实现分列校验，也没有削弱它。

### 只读存档：读什么、不读什么

信号侧**只读**既有落盘：

```
data/auction_scan/{YYYY-MM-DD}.json     ← 经 get_scan(data_dir, target) 读
```

**不做**这三件事：

1. **不重抓快照**。同一天不同时刻重抓拿到不同快照，于是「这条信号那天有没有出现」
   取决于你什么时候跑它 —— 这样的评估没有意义。
2. **不跑 `SCAN_WINDOW`**。窗口逻辑属于扫描服务（`app/services/auction_scan.py`），
   信号侧不拥有它，也不该为了「让信号有数据」去驱动它。
3. **不盘后补扫**。开盘后抓到的快照已经不是集合竞价撮合那一刻的信息集。

缺文件 → **fail-closed**：`state=no_data` → 评估 `reason="no_data"` /
`status="data_pending"`，绝不「静默填空候选当触发」。

### 字段映射表（落盘 → 候选）

| 存档 / 派生 | 候选字段 | 备注 |
|---|---|---|
| `trade_date` + 固定 **09:25:00 Asia/Shanghai** | `auction_ts` | **不读分钟 bar**；签名里根本没有分钟数据可传 |
| `scanned_at` | `metadata["scanned_at"]` | 这个结论基于哪一刻的快照 |
| `item.symbol` | `symbol` | 缺则整条跳过，不拿空串当标的 |
| `item.ticker` / `item.name` | `ticker` / `display_name` | 展示用 |
| `item.gap` | `metadata["gap"]` | 由 scan 内价算出；价本身没落盘 |
| `item.vol_ratio` | `metadata["vol_ratio"]` | **UI 阈值来源，不当已锁门禁** |
| `item.auction_volume` | `auction_volume` | 有则填，缺则 `None`（**不填 0**） |
| `item.auction_amount` | `metadata["auction_amount"]` | |
| （无）`auction_price` / `prev_close` | `auction_price = None` | 见 `auction_price_not_persisted` |

**为什么 `auction_price` 恒 `None` 而不是用 `gap` 倒推**：`gap` 是相对昨收的比例，
反推价格还需要 `prev_close` —— 而 `prev_close` 同样没落盘。两个未落盘量的比值
看起来像个价，实际不可复核。

### 四条证据缺口（全部 pending）

| 标签 | 含义 | 清掉的条件（另拍） |
|---|---|---|
| `auction_price_not_persisted` | 落盘 item 只有 `gap` / `vol_ratio` / `auction_volume` / `auction_amount`，**没有** `auction_price` / `prev_close` | 另拍补 scan 落盘 schema（零行为破坏）或接入等价可信价源 |
| `threshold_lock_data_pending` | UI 列表的 `min_gap` / `min_vol_ratio` 是**页面筛选参数**，不是 PHASE3 回测门禁 | 回测标定后锁 `auction_rule_id` + version |
| `backtest_evidence_data_pending` | 没有竞价异动的回测表 / 触发表 / 显著性证据 | 另拍证据表 + 门禁样本 |
| `minute_0925_still_absent` | 分钟库最早仍 09:30 | **保留**；**不**阻塞读 scan 存档；**永不**用 09:30 清掉 |

**`min_vol_ratio` 不是门禁。** 它是页面上「你看列表时想筛多严」的参数，
随时可能被调。把它写进代码当阈值，等于把一个 UI 偏好升级成统计口径。

### 永久硬禁：09:30 冒充 09:25

`auction_ts` 由 `trade_date` + 固定 `09:25:00+08:00` 推出，函数签名只接受一个
`trade_date`——**没有**分钟数据可以传进来。这是「不冒充」的机械保证，不是口头约定。

用 09:30 或任意开盘后 bar 冒充 09:25 是**永久禁止**的：两者是离散撮合与连续成交
两个信息集，混用的结果**不会报错**，只会让竞价类信号的统计全部失真。

### 与 #210 放量正交

| 项 | 放量（#210） | 竞价（本拍） |
|---|---|---|
| `event_class` | `intraday_volume` | `auction` |
| `auction_stat_bucket` | 恒 `None` | 恒 `auction_0925` |
| `post_open_noise_bucket` | 可空桩（`None`） | **不得携带** |
| 信息集 | 开盘后连续成交 | 09:25 集合竞价离散撮合 |

两个方向都不许混桶。本拍 `volume_breakout_stub.py` **字节未改**（有测例用 git
比对守着）。

### 评估态恒 pending

三种路径，结论都是 `data_pending`：

| 情形 | `reason` | 候选 |
|---|---|---|
| 没有 `data_dir` / 存档缺失 | `no_data` | 空 |
| 读到存档但没有可映射条目 | `no_candidates` | 空 |
| 读到存档且有条目 | `evidence_pending` | 若干候选，全部带四条 pending |

第三种**最容易误读**：明明映射出了候选、`gap` 和 `vol_ratio` 都有值，看起来像
「能算了」。但那些值是存档里现成的筛选结果，不是这条信号的触发结论。

`triggered` 恒 `None`（「无法判断」），**不是** `False`（「查过，没异动」）——
在没有阈值、没有回测、没有 09:25 分钟证据的情况下返回 `False`，等于向调用方
保证一件我们并不知道的事。

### 对外口径

未验收前只说「ALIGN / 竞价窄开 stub（定义 + 读存档 + pending）」。

**不宣称**：回测已经通过、可以按它交易、可以对外推送、阈值已锁定、
调用链已生产验证。**部分解阻 ≠ 整拍可推。**

### 本节验收

- [x] 身份常量钉死（`auction_abnormal` / `auction` / `auction_0925` / `flag=False` / 禁带桶）
- [x] 混桶用例 RED（竞价带开盘后桶 → 拒；放量带竞价桶 → 拒）
- [x] 有合成 JSON → 可映射候选但仍带四条 `evidence_pending`；无文件 → fail-closed / `no_data`
- [x] 四条 pending 标签机器可读；`auction_price` 诚实 `None`
- [x] **未**改 `volume_breakout_stub` 行为（git 字节比对守着）
- [x] **未**改三 env；**未**改 `SCAN_WINDOW` / 落盘 schema / 盘后补扫
- [x] **未**出现 09:30 冒充 09:25（`auction_ts` 签名只收 `trade_date`）
- [x] **未**宣称回测已通过 / 可交易 / 可推送
- [x] 软提醒 **10** 条写入本节与 PR
- [x] 默认不部署；合入另等盘后口令

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。*

---

## 命中层（批次 20261026gbsshits · P-hits）

### 一句话

在定义层之上新增**命中记录**: 「某只票, 在某时点, 按某版定义, 被判定命中」。
监控中心只读展示已校准的命中, 空则显示「暂无已校准的买卖信号」。

### 命中 ≠ 定义

这是本节最重要的一条区分, 也是本拍存在的理由。

| | 定义层 `SignalRecord` | 命中层 `SignalHit` |
|---|---|---|
| 回答的问题 | 这个信号是什么、能不能发布 | 哪只票什么时候命中了它 |
| 有 `symbol` 吗 | **没有** | 有, 必填 |
| 数量级 | 几十条, 人工维护 | 按票 × 时间增长 |
| 可变性 | 状态可变（走状态机） | 一旦写入不可改写 |
| 状态机 | `candidate → … → publishable` | 没有状态机 |

把两者塞进一个类型会立刻产生三个问题: 定义改版会连带改写历史命中; `definition_version`
无处安放; 定义的状态机被命中的"无状态"污染。所以命中层是**新的一层**, 只从定义
**拷贝** `family` / `event_class`, 不做任何反向写入。

**特别说明**: `/api/alerts` 里那条 `monitor_event_type=buy_sell_signal_status_changed`
是**定义状态变更**（「这条信号被批准发布了」）, 不是个股命中。它没有 `symbol` ——
命中层要求 `symbol` 必填, 所以一条定义状态事件在数据结构上**无法**被当成命中。
监控的「买卖信号」tab 读的是 `/api/buy-sell-signals/hits`, 不是 `/api/alerts`。

### 命中 schema

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 首批 `"1"` |
| `namespace` | 是 | 恒 `buy_sell_signals` |
| `hit_id` | 是 | 稳定唯一 id |
| `signal_id` | 是 | 指向定义 |
| `definition_version` | 是 | **钉死**命中产生时所依据的定义版本 |
| `symbol` | 是 | 个股代码（命中层才有） |
| `asof` | 是 | 评估/数据时点 |
| `source` | 是 | `{evaluator, mode}`, `mode ∈ {fixture, shadow, live}` |
| `family` / `event_class` | 是 | ∈ `SIGNAL_FAMILIES` / `EVENT_CLASSES`, 从定义拷贝 |
| `direction_label` | 是 | 封闭集 `research_buy` / `research_sell` / `research_watch` |
| `disclaimer` / `disclaimer_key` | 是 | 合规三件套 |
| `definition_status_at_hit` | 否 | 写入时的状态快照, **仅供审计** |
| `stale` / `asof_date` | 派生 | 由 API 计算, 不落盘 |

**`definition_version` 为什么要钉死**: 定义改版后, 旧命中仍须按当时那一版解释。
读侧按"当前版"重读, 会让一条按 v1 口径算出来的命中被 v2 的措辞描述, 且从输出上
完全看不出来。

**载荷里不许有**: 价格、数量、仓位、订单号、可执行指令、模拟盘下单字段。
这不是"暂时没做", 是命中层的定位 —— 它是研究记录, 不是交易指令。`from_dict` 遇到
这些键**报错而不是丢弃**: 一份带 `price` 的命中文件说明写入侧出了问题, 静默丢掉
会让问题一直藏着。

### N1 · 只读店 `data/buy_sell_signals/hits.json`

路径: `Path(settings.data_dir) / "buy_sell_signals" / "hits.json"`（与 `registry.json`
同目录, 但是两份文件）。文件级 schema:

```json
{
  "schema_version": "1",
  "namespace": "buy_sell_signals",
  "hits": [ /* SignalHit.to_dict() */ ]
}
```

- **只有读**: `DiskHitStore` 没有 `upsert` / `write` / `append`。本拍没有任何代码路径
  写 live 命中 —— 没有影子调度、没有启动扫描、没有日管道写入。
- **与 `BUY_SELL_SIGNALS_PERSIST` 无关**: 那个 env 管的是定义注册表落不落盘。把命中
  读取也挂在它下面会造出"没开 PERSIST 所以看不到命中"这种耦合。
- 缺文件 / 空文件 → 0 条命中 → 空态。这是**全新部署的合法状态**, 不是缺陷。
- 单条记录反序列化失败 → **跳过该条**, 其余照常（一条脏数据不该让整个 tab 打不开）。
- 顶层形状不对 / JSON 语法错误 → 0 条 + warning（注册表那边是抛错, 因为它是唯一事实源
  且会被覆写; 命中层是只读派生数据, 显示空态比让监控页白屏合理）。

### N2 · `GET /api/buy-sell-signals/hits`

Query: `family`（可选）、`symbol`（可选）、`today`（仅测试用, 不在 OpenAPI 里）。

响应:

```json
{
  "schema_version": "1",
  "namespace": "buy_sell_signals",
  "empty_message": "暂无已校准的买卖信号",
  "hits": [],
  "count": 0,
  "rejected": {"definition_not_publishable": 2},
  "scope_applied": true,
  "scope_size": 12
}
```

**闸在服务端**, 顺序如下:

1. `family` 不在 `{minute_event, daily_fundamental}` → 返回空（不是 400）
2. 命中 family 是 `composite` → 丢弃（首批不暴露）
3. 查定义注册表 `get(signal_id)`
4. 定义不存在 → 丢弃
5. 定义当前 `status != "publishable"` → 丢弃
6. `event_class == "limit_reopen"` → 丢弃（双保险）
7. 基本面命中若不在自选 ∪ 持仓内 → 丢弃

第 6 条特意不冗余: 状态机已经有双重拦截让 `limit_reopen` 到不了 `publishable`,
但注册表文件是可以被外部直接写的, 一条被手改成 `publishable` 的炸板定义在状态机
之外是合法的。炸板/回封漏进来会被读成"信号有效", 而它本质上只是一个"刚刚发生过
的事实"。多一行的成本换这一道, 值。

第 1 条为什么不报 400: 「这个 tab 现在没有这类数据」和「你传错了参数」对使用者
来说结果一样是空, 没必要让他看到一个红色错误框。

### 空态文案

**「暂无已校准的买卖信号」**（`EMPTY_HITS_MESSAGE`, 逐字写死）。

不许改成「今天没信号」。后者在读者眼里等价于「系统在跑、只是今天没有」, 而真实
情况往往是「评估器还没锁定阈值, 一条都还没产出」。把后者说成前者, 就是在暗示系统
比实际更就绪。

前端不自己拼空态文案, 直接渲染响应里的 `empty_message` —— 两处各写一份迟早分叉。

### N4 · 过期判定

规则: `asof` 日期（上海）< 今日（上海）→ 过期。

- 带时区的 `asof` 先转 `Asia/Shanghai` 再取日期（UTC 16:30 是上海次日 00:00）。
- 纯日期按该日。
- **非法 `asof` 一律视为过期** —— fail-closed。一条连时间都读不出来的记录, 按
  「当前」呈现就是在替它担保。
- **不做**精细交易日历（跳周末/节假日找"最近交易日"）: 那需要一份准确的节假日表,
  是另一拍的事。所以节后第一天, 节前最后一个交易日的命中会显示为过期 —— 已知且
  可接受的保守偏差。

UI 上过期条目不隐藏、不禁用, 只打「数据过期」标。藏起来等于假装它没发生过。

### N3 · 范围闸: 自选 ∪ 持仓（只作用于基本面）

- 自选: `app.services.watchlist.list_symbols()`
- 持仓: `paper.overview()` 的 holdings
- **基本面命中**过闸, **事件命中不过闸**（两者都仍须过 publishable 闸）

为什么分开: 基本面命中是日频全市场量级, 不过滤会堆上千条无关的票; 事件命中一天
几条到几十条, 过滤反而会让用户看不到别处的异动。

**范围闸不是权限**: 过了 publishable 的命中, 事件类一律可见。这是一条产品口径,
不是安全边界, 所以它放在可见性闸**之后**。

两者都读不出来时返回空集 —— 保守方向: 让人以为"没有信号", 而不是"看到了一堆
不该看的"。

### SSE: 只补两个字段

`build_sse_alert` 的 `keep` 元组**加入** `family`、`event_class`。

共用载荷 `build_push_payload` 里早就有这两个字段, 是 SSE 这一层把它们过滤掉了,
于是所有订阅者拿到的推送都缺分类, 只能靠 `display_name` 猜这是事件类还是基本面类。

这是**呈现修复**, 不是新推送路径: 门控（三 env）、触发时机、载荷其余内容一律未动。
定义状态推送**仍然没有 `symbol`** —— 给它补 symbol 等于让订阅端以为"有票命中了"。

### P-fund-scope: 基本面日表的呈现规则

| 规则 | 实现 |
|---|---|
| 形态 | 收盘日表 + 显式 `asof`, 不是盘中逐条 toast 流 |
| toast | **不弹** AlertToast（`BuySellHitsList` 里没有 toast 调用） |
| 范围 | 自选 ∪ 持仓（N3） |
| 过期 | 带「数据过期」标（N4） |
| 存储 | **不写** `alerts.jsonl`; 读路径走命中 store/API, 不劫持 BSS `try_push` |

### 本拍明确不做

- `VOLUME_BREAKOUT_TRIGGERING_ENABLED` 翻 `True`; 真触发; 影子调度; 新 crontab
- 评估器 / 日管道写 live 命中（命中层本拍**只读**）
- ~~命中钉钉推送（P-ding-later, 另口令）~~ → **已由批次 20261027gbssding 交付**（独立 webhook，见下节）
- P-calib（阈值锁定 + 回测）
- 三 env（`BUY_SELL_SIGNALS_PERSIST` / `PUSH` / `DINGTALK`）字面量与语义
- 全 A 范围（N3 另口令）; 精细交易日历（N4 另口令）
- `composite` 展示入口

### 本节验收

- [x] 命中 schema 必填齐全; 禁价格/数量字段（参数化逐词测）; `direction_label` 封闭集
- [x] `hits.json` 只读; 缺文件 → 空态; 测例可注入; 单条脏数据只跳过自己
- [x] `GET /api/buy-sell-signals/hits` 服务端闸; 不返回定义状态事件
- [x] 夹具: publishable 可见; candidate / observe / limit_reopen 不可见
- [x] `limit_reopen` 即使定义被手改成 publishable 仍不可见（双保险真的独立）
- [x] 空态文案逐字「暂无已校准的买卖信号」, 且不含「今天没信号」这类变体
- [x] `build_sse_alert` keep 含 `family` / `event_class`, 且既有 13 个字段一个不丢
- [x] 定义状态推送仍无 `symbol`
- [x] N4 三条: 昨日 → stale; 今日 → 非 stale; 非法 → stale
- [x] 基本面不弹 toast; 不写 `alerts.jsonl`; 范围闸只作用于基本面
- [x] 命中新文件不 import `app.t_rounds`; 三 env 字面量未变; TRIGGERING 仍 `False`
- [x] 默认不部署; 合入另等盘后口令

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。*

---

## P-calib：频率锁定戳 + 回测校准登记 + 可见性 AND（批次 20261026gbsscalib）

### 一句话

在 #228 命中层之上叠两层东西：**频率锁定戳**（这套口径按什么节拍、用什么阈值参数评估，
钉死 `definition_version`）+ **回测校准登记**（这套口径跑出过什么、过没过门槛），
并把监控可见性从「`publishable` 即可见」收紧成 **`publishable` ∧ `calib passed`**。

### 阈值锁定 ≠ 开触发

这是本节最容易被读歪的一句，写在最前面。

| 有 | 不代表 |
|---|---|
| `FreqLockStamp` 存在、指纹稳定 | 阈值已被证据支持 |
| `calib.json` 存在、有 `passed` 条目 | 触发闸开了 |
| 一条命中可见 | 系统在产 live 信号 |

- `VOLUME_BREAKOUT_TRIGGERING_ENABLED` 仍是 `False`，`AUCTION_ABNORMAL_TRIGGERING_ENABLED`
  仍是 `False`，本拍**零碰**。
- `VOLUME_BREAKOUT_EVIDENCE_PENDING` 的**四条 pending 一条没清**，
  包括 `threshold_lock_data_pending`。"阈值被写下来了一个值"和"这个值被证据支持"
  是两件事：前者只需要有人填一个数，后者需要一份能解释负号（`quant_evidence.yaml` 里
  `volume_breakout` 四个口径全负）的回测表。
- 本拍交付的是**容器**，不是结论。生产可以继续空着，这是诚实的空态。

### 两层「频率」

| 层 | 内容 | 落点 |
|---|---|---|
| ① 评估节拍 | `minute_event`（事件/时点）vs `daily_fundamental`（日频收盘, Asia/Shanghai） | `FreqLockStamp.cadence`, 封闭集 `FREQ_LOCK_CADENCES` |
| ② 阈值参数集 | 量比 / 突破幅度 / 窗口分钟…… | `FreqLockStamp.params`, 参与内容指纹 |

`composite` **不在**节拍封闭集里：它将来由若干子信号合成，节拍取决于被合成的那些，
本拍不给它随便挑一个。

### K4 / 评估单位声明（P-bss-k4）

> 批次 `20261027gbssk4`。**补 K4**：让评估口径把「量比按手还是按股」写出来。
> **不是**「#7 量审计已过」；**不是**开放触发；**不是**可放量；**不是** G1 全市场换算。

上面第 ② 层说「阈值参数集：量比 / 突破幅度 / 窗口分钟……参与内容指纹」。这里补一条
前提：**量比这类数，光有数值不够，还得说清单位**。主仓口径是「手」，DSA 侧是「股」
（详见 [`plugin-development.md` 的「跨层单位对照（主仓手 / DSA 股）」](plugin-development.md)），
两者差 100 倍。`volume_ratio_min: 2.5` 这行字本身看不出是哪种 —— 不说清，歧义会一路
静默到结论里。

所以：`FreqLockStamp.params` 里**一旦出现量相关键**，就必须带 `volume_unit`。

| 项 | 值 |
|---|---|
| 字段名 | `volume_unit`（落在 `params` 内） |
| 合法值 | 恰为 **`手`** 或 **`股`**（封闭集 `VOLUME_UNITS`） |
| 触发强制的键 | `volume_ratio` / `volume_ratio_min` / `volume_ratio_max`，**以及**任何以 `volume_` 开头且 ≠ `volume_unit` 的键（如 `volume_ma`） |
| 缺单位 | **拒**：`FreqLockError`，文案含「缺」与 `volume_unit`。**不默认「手」** |
| 非法值 | 非封闭集（`lot` / `share` / `hand` / `千股` / 首尾带空格 / 空串）→ **拒** |
| 混形态 | list / tuple / set / 逗号拼接（`手,股`）→ **拒**；本拍**不**挑一个、**不**做隐式换算 |
| 空 `params` | **仍允许**。「节拍已定、阈值未标定」是合法中间态 |
| 无量相关键 | **不强制**。只有 `breakout_pct_min` / `window_minutes` 时不要求单位 |

**单位进指纹**：`volume_unit` 在 `params` 内，自然参与 `canonical_json`。同阈值只换单位
→ 指纹变 → 是**另一套口径**，旧登记的 `passed` 不得复用。

**闸装在哪**：`freq_lock.require_volume_unit()`，并由 `FreqLockStamp.__post_init__`
自动调用。放在 `__post_init__` 而不是只提供助手，是因为「助手存在但调用方漏调」是这类
检查最典型的静默洞 —— 直接构造、`from_dict`、夹具三条入口现在过同一道闸。

**stub 侧**：`volume_breakout_stub` 的契约多一条 —— **没有合法 `volume_unit` 就不许出现
非空 `volume_ratio`**。空桩两边都是 `None`，天然成立；这条约束真正约束的是将来那个真实
评估器。`VOLUME_BREAKOUT_TRIGGERING_ENABLED` **仍 `False`**。

> **本拍没给空桩加 `volume_unit` 字段**。N4 把该字段列为「可选」，而闸装在
> `FreqLockStamp` 上，不依赖空桩有字段 —— 回滚到这个文件的 tip 字节，零功能损失。
>
> 顺带记一笔：`gauction2` 隔离测里那两条要求「与 tip 逐字节一致」的守卫
> （`test_volume_stub_file_is_byte_identical_to_tip` / `test_auction_scan_service_is_unchanged`）
> 用 `git diff HEAD` 做基准，而 PR 里 `HEAD` 是 merge ref、工作区与其干净一致，
> 于是 `git diff` **恒为 0**，守卫在 CI 里**空转**。本拍不改它们（超出授权），
> 但将来要加字段，须**先把这两条守卫的基准改对**（改成 PR base），再见字段。

| 本拍是 | 本拍**不是** |
|---|---|
| 装上一道「不写单位就拒评」的闸 | 量审计（K8，另口令）｜
| 让单位写进指纹，换单位＝换口径 | 证明任何一条放量阈值是对的 |
| 夹具补单位 + 新测 45 条（含 1 条 xfail 探针） | 开放触发 / 放量 / 触发表 / 影子调度 |
| 文档索引到 #237 跨层对照 | G1 / B4 的运行时统一换算 |

### 指纹是内容算的，不是手写版本号

```text
freq_lock_fingerprint(signal_id, definition_version, cadence, params) =
  sha256(canonical_json({signal_id, definition_version, cadence, params}))   # 全 64 位
```

两条理由：

1. **手写版本号会忘。** `freq_lock_v1` 这种标签，改了阈值忘了 bump 就完全失效 ——
   而"改了两个阈值忘了改版本"恰恰是最容易发生的事。指纹从参数值本身算出来，
   改任何一个值必变。
2. **标签仍然保留。** 它覆盖指纹覆盖不到的那一类：只改了代码逻辑、常量没动。
   那时指纹一模一样，唯一能说"这是第几版口径"的就是标签。

所以**标签与指纹各管一半，一起用**：三键（`signal_id` + `definition_version` +
`freq_lock_version`）对齐，指纹再对一次。

> **M6：伪称干净**：把量比阈值从 2.5 改成 1.8，版本标签仍写 `freq_lock_v1`。
> 单看标签看不出来，但指纹变了。`FreqLockStamp.matches()` 要求三键**且**指纹都一致，
> 于是"标签相同、指纹不同"被识别为**另一套口径**，旧登记的 `passed` 不得复用。

### 校准登记：`calib.json`

| 项 | 值 |
|---|---|
| 路径 | `<data_dir>/buy_sell_signals/calib.json` |
| 文件级 schema | `{"schema_version":"1","namespace":"buy_sell_signals","entries":[...]}` |
| 与 `BUY_SELL_SIGNALS_PERSIST` | **无关**。那个 env 管定义注册表落不落盘；校准看不看得见只取决于文件在不在 |
| 缺文件 | 无登记 → **一条都不可见**（honest empty） |
| 坏 JSON / 顶层形状不对 | 同上，退化 + warning |
| 单条脏数据 | 只跳过那一条，其余照常 |

`status` 是**封闭集** `{passed, pending, failed}`，**只有 `passed` 算已校准**。
`pending` / `failed` / 没有登记在可见性闸上与"空集"完全等价。
刻意不接受 `ok` / `true` / `verified` 这类近义词 —— 多一个近义词就多一个地方
可以悄悄把 `pending` 写成"通过"。

### `CalibRecord` 字段

| 字段 | 约束 |
|---|---|
| `signal_id` / `definition_version` / `freq_lock_version` | 非空。三键，与戳、命中一致 |
| `status` | ∈ `{passed, pending, failed}` |
| `eval_window` | 正整数 |
| `eval_window_unit` | 恒 `"trading_days"`（**N2：单位＝交易日**） |
| `sample_n` | 非负整数；`passed` 时 **≥ 30**（**N2**） |
| `cost_bp_roundtrip` | `passed` 时必须 **= 18.2**（来回，**N2**） |
| `baseline_ref` | `passed` 时非空 |
| `interval` | `{lo, hi}`；`passed` 时必须有 |
| `report_id` / `source` | `passed` 时非空，指向回测产物 |
| `passed_at` | `passed` 时必填，带时区（Asia/Shanghai） |

### 门槛是硬拒，不是降级

`sample_n < 30`、`cost_bp_roundtrip ≠ 18.2`、缺任何必填引用 → **抛错**，
不静默转成 `pending`。

理由：悄悄降级会让人以为"我写进去了，只是没过"，实际是**没写**。下次来查登记看到
一条 `pending`，只会以为自己的回测没过，而不是自己的调用被拒了。口径要一致 ——
门槛不满足就是写入失败，让他当场知道。

成本为什么钉死而不是自报：成本是一个能让任何策略从负变正的参数。允许自报，第一个
想让它过的人就会填 5bp，而 5bp 在 A 股连印花税都不够（卖出千分之 0.5，加双边佣金与
过户费，来回 18.2bp 是仓内既有口径）。

### 可见性：AND，且缺一即不可见

```text
可见 ⟺ publishable
      ∧ ¬limit_reopen
      ∧ family ∈ EXPOSED_FAMILIES
      ∧ (基本面 → symbol ∈ 自选 ∪ 持仓)
      ∧ 存在与命中三键对齐的 calib passed
```

前四条是 #228 的闸，**原样保留**；第五条是本拍叠上去的。新增拒绝原因
`VisibilityReject.CALIB_NOT_PASSED`。

为什么是 AND：前四条回答"这条命中**该不该**发出来"（定义敢不敢担保、事件类型允不允许、
范围对不对），第五条回答"这套口径**有没有**拿得出手的成绩"。两个问题都答"是"才该显示。
做成 OR 等于让"定义是 publishable"替"回测没过"背书 —— 而定义发布只是一次人工审批动作，
它不产生任何关于收益/胜率的证据。

**三键取命中自带的**（不是定义当前的）：
- `definition_version` 用命中的：一条按 v1 算的命中，不该因为定义升到 v2 而自动获得
  v2 的校准结论背书。
- `freq_lock_version` 用命中镜像字段：缺失即不可见。连按哪套口径算的都不知道，
  不能拿"当前戳"顶上去。

### 命中无权自称已校准

`SignalHit` 上**没有**、也**禁止**加 `calibrated` 布尔字段（延续 #228 纪律）。
一个可手改的 `true`，从任何 diff 上都看不出来，但会让所有闸形同虚设。
命中侧只有 `freq_lock_version` 这一个**标签**，而标签本身不构成担保 ——
它只参与对齐，对不上就不可见。

> **M11**：往命中 JSON 里塞 `calibrated: true`，闸不认（`from_dict` 会丢弃未知键，
> 且 `visible_hits` 只看 `calib.json`）。

### 空态文案一个字节都没动

「暂无已校准的买卖信号」（`EMPTY_HITS_MESSAGE`）。

这句话里的"已校准"三个字，是本拍唯一一处对用户承诺"闸在生效"的地方。改成"今天没信号"
就等于把"还没有任何东西通过校准"说成"系统在跑、今天恰好空着"。

> **M9**：把文案改成「今天没信号」→ 三条测例同时红（`constants` 逐字、API 响应、
> 可见性矩阵）。

### 校准闸在服务端，不在前端

前端过滤只防手滑，防不住直接调接口 —— 绕过前端拿到的就是全部命中。
`Monitor.tsx` 的 `buy_sell` tab 本拍**默认不改**：后端 `empty_message` 已钉，
前端照渲染即可。若在 `Monitor.tsx` 里按 `calibrated` 过滤，看起来一模一样，
安全性完全不同。

### 正交：`regime_caliber` 零共享

| | `app.services.regime_caliber` | 本节 |
|---|---|---|
| 描述什么 | **市场环境**（涨跌家数、宽度、状态分档）怎么算 | **某条 BSS 信号**用什么节拍/阈值评估 |
| 落在哪 | `SignalRecord.regime_caliber_version` | `FreqLockStamp` / `calib.json` |
| 是否代表"信号已校准" | **否** | 是（`passed`） |

两者只是**都用了"内容指纹"这个技术手段**。本拍没有 import `regime_caliber` 的任何符号，
没有读写它的 stamp 文件，也没有改 `SignalRecord.regime_caliber_version` 的含义。

> **M12**：`regime_caliber_version` 有值 ≠ 命中可见。测例直接构造一条带该字段的定义，
> 在 `CalibIndex([])` 下断言 `hits == []`。

### 本拍明确不做

- ~~P-calib（阈值锁定 + 回测）~~ → **本拍即 P-calib 的交付**（戳 + 登记 + 可见性 AND）
- **N3：离线 shadow 物化 CLI —— 不做**（仍算条件可交；另口令。若后续要做，
  必须显式一次性、无调度、无 lifespan 注册）
- `VOLUME_BREAKOUT_TRIGGERING_ENABLED` / `AUCTION_ABNORMAL_TRIGGERING_ENABLED` 翻 `True`；
  真触发；影子调度；新 crontab；`main` lifespan 自动扫写 hits
- 清 `threshold_lock_data_pending`（四条 pending 一条不少）
- 三 env（`BUY_SELL_SIGNALS_PERSIST` / `PUSH` / `DINGTALK`）字面量、真值集合、生产值
- ~~命中钉钉推送（P-ding-later，另拍）~~ → **已由批次 20261027gbssding 交付**（见下节）
- 生产路径自动产 `passed`（**没有任何代码路径**会自动写一条 `passed`）
- ~~命中钉钉~~ / Opt-A（仍 defer）/ #6 / 整包 UX / DecisionWorkspace IA / 竞价·放量实现扩 / 17–19
- 精细交易日历（N8，保持 #228 简易规则）

### 生产可以继续空

当前生产真值：

- `hits.json` 大概率不存在 → 命中 0 条
- `calib.json` **一定**不存在 → `passed` 0 条
- 于是监控 tab 显示「暂无已校准的买卖信号」

这是**正确且诚实**的状态，不是缺陷。没有通过校准证据的时候，系统确实什么都不能担保 ——
把它说成"今天没信号"或"正在初始化"，才是对使用者不诚实。

### 本节验收

- [x] 存在 `FreqLockStamp`（`freq_lock_version` + 内容指纹 + 钉 `definition_version`）
- [x] 同参指纹稳；改任一参与字段（`signal_id` / `definition_version` / `cadence` / `params`）指纹必变
- [x] 指纹对键序不敏感（`sort_keys`）；对数值近邻敏感（2.5 vs 2.55 不同）
- [x] 戳文件里的 `fingerprint` 读时重算，对不上即报错
- [x] `calib.json` 路径与文件名钉死；与 `BUY_SELL_SIGNALS_PERSIST` **无耦合**（env 开/关都是磁盘店）
- [x] `status` 封闭集三元；`ok`/`true`/`verified`/`PASSED`/空串全部拒绝
- [x] `sample_n=29` 写 `passed` → 拒；`=30` → 通过
- [x] `cost_bp_roundtrip ∈ {0,5,18.19,18.21,20,36.4}` 写 `passed` → 全拒；`=18.2` → 通过
- [x] 缺 `baseline_ref` / `interval` / `report_id` / `source` / `passed_at` → 拒
- [x] `eval_window_unit` 写 `calendar_days` → 拒；`eval_window ≤ 0` / 非整数 / `bool` → 拒
- [x] `passed_at` 无时区 → 拒；序列化后带 `+08:00`
- [x] 反序列化不是后门：手改 JSON 塞 `sample_n=3` 的 `passed` 读不进来
- [x] 缺文件 / 坏 JSON / 顶层形状不对 → 空（= 全不可见）；单条脏数据只跳过自己
- [x] **M1** publishable + 无登记 → 不可见；`calib=None` 等同空索引（不是"跳过闸"）
- [x] **M1c** publishable + 无 `freq_lock_version` → 不可见
- [x] **M2** publishable + `pending` / `failed` → 不可见
- [x] **M3** candidate + `passed` → 不可见（reject 原因是 `not_publishable`，不是 calib）
- [x] **M4** limit_reopen + `passed`（定义被手改 publishable）→ 不可见
- [x] **M5** 生产盘店不 import 夹具；夹具登记带 `fixture://` 前缀
- [x] **M6** 同标签异参数 → `matches()` False；登记版本/定义版本对不上 → 不可见
- [x] **M7/M8** 门槛拒绝后索引为空 → 命中不可见（端到端连通）
- [x] **M9** 空态文案逐字；改字三条测例同时红
- [x] **M10** 两 TRIGGERING 仍 `False`；`threshold_lock_data_pending` 仍在
- [x] **M11** `SignalHit` 无 `calibrated` 字段；注入该键也不可见
- [x] **M12** `regime_caliber_version` 有值不构成校准；闸逻辑里不出现该符号
- [x] 新文件不 import `app.services.regime_caliber` / `app.t_rounds*`；无 `APScheduler` / `add_job` / `crontab`
- [x] 包内除 `calib_models` / `constants` / `calib_store` / `hits_fixtures` 外，
      无任何文件给 `status` 赋 `"passed"`
- [x] 响应含只读摘要 `calib_gate: "applied"` + `calib_passed_keys`（只回条数）
- [x] `/hits/scope` 增 `calib_file` / `calib_file_exists` / `calib_passed_keys`，仍不回明细
- [x] 既有 #228 命中测 91 条全绿（夹具补 `passed`，属契约收紧的合理连带）
- [x] 默认不部署；合入另等盘后口令

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*

---

## P-ding-later：命中钉钉独立 webhook（批次 20261027gbssding）

> 仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp（本节非回测，仅旁注）。

### 一句话

BSS **命中**的钉钉外发只走独立键 `BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL`（+ 可选
`BUY_SELL_SIGNALS_HITS_DINGTALK_SECRET`）；只发通过 **publishable ∧ calib_passed**（复用
`hits_visibility.visible_hits`）的命中；缺这个键就**拒发**，**永不**回落到
`DINGTALK_WEBHOOK_URL`；推送正文含 symbol / direction_label / 免责，**不含**价格仓位；
空态「暂无已校准的买卖信号」一个字节都没动。

### 三条钉钉通道，互不相通

定义态、命中、复盘是三条**并列**的链路，不是"新旧版本"。三者的键、门、失败原因全不同：

| 通道 | webhook 来源 | 门 | 拒发原因字符串 |
|---|---|---|---|
| 资讯热点等 | `DINGTALK_WEBHOOK_URL` | `NEWS_PUSH_*` | — |
| BSS **定义状态变更** | `DINGTALK_WEBHOOK_URL` | `PUSH` ∧ `BUY_SELL_SIGNALS_DINGTALK` ∧ URL 非空 | （静默跳过） |
| DSA 狙击 | `DINGTALK_WEBHOOK_URL` | sniper 前缀 / 冷却 | — |
| DSA **定时复盘** | `DSA_REVIEW_DINGTALK_WEBHOOK_URL` | #227 日 cap | `review_webhook_not_configured` |
| **BSS 命中** | **`BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL`** | URL 非空 ∧ 可见性闸 ∧ 非 fixture ∧ 冷却未命中 | `hits_webhook_not_configured` |

为什么命中要单开一个键：命中是**逐股逐日**的，定义变更是一周几次。挂在共群上，一次
命中批量就足以把"哪条信号被改过状态"刷掉——而后者恰恰更需要人看一眼。宁可运维多一个
"配了没"的位置，也不能让两种语义抢同一个出口。

### 缺配就是拒发，不是降级

| 条件 | 行为 |
|---|---|
| 命中 URL 空 / 全空白 | `hits_webhook_not_configured`，`attempts = 0`，**一个字节都不发** |
| 旧 `DINGTALK_WEBHOOK_URL` 非空、命中 URL 空 | **仍然拒发**；旧 URL 一次都没被请求 |
| 命中 URL == 旧 URL | **允许**发送 + `warning`「注意力竞争未解除」（不拒发、不回落） |
| 命中未过可见性闸 | `not_visible` + 具体 `VisibilityReject` |
| `source.mode == "fixture"` | `fixture_not_pushable`（测例须显式 `allow_fixture=True`） |

「配好了没」和「该不该发」是两件事。回落到共群会把前者伪装成"配好了"，
而错发到群里的消息**收不回来**。

### 只发已校准的命中：复用同一道闸，不另写

外发前的判定**直接调** `visible_hits([hit], registry, scope_symbols=..., calib=...)`，
要求返回恰好 1 条。不在这里另写一套"软闸"，理由是：监控页那道闸和外发这道闸如果要各写
一遍，总有一天会不一致——而不一致的方向通常是"外发松了"。

命中记录里**没有** `calibrated` 字段（延续 #228 / #231 纪律），本模块也不看任何自述，
只认独立 `calib.json` 经 `CalibIndex` 给出的 `passed`。

### 挂点：显式调用，不自动扫

`HitStore` 是**只读**协议（只有 `list_all`，无 upsert），生产也没有写 live 命中的代码。
所以本拍不假装"已接线自动推送"：唯一入口是显式调用 `try_push_hit(hit, registry, calib=...)`。

**明确不做**：新增定时器 / 调度框架 / `lifespan` / `daily_pipeline` 遍历命中表外发；
在只读 store 里塞写钩伪装自动推；启动时扫盘补发历史命中。

没配 webhook 或没人调用 → 生产**零外发**。这是预期状态，不是缺陷。

### 去重冷却：30 分钟，只在成功后写

去重键 `(signal_id, definition_version, symbol, asof, freq_lock_version)` 五元组。
少任何一个都会错：少了 `symbol` 把"同一条信号打在不同票上"当一条；少了 `asof` 把
"昨天命中今天又命中"当重复；少了版本键会让改版/换口径后的新结论被旧冷却挡住。

| 项 | 值 |
|---|---|
| 冷却时长 | `HITS_DINGTALK_DEDUPE_COOLDOWN_MIN = 30`（**独立常量**，不共用定义态的 `PUSH_DEDUPE_COOLDOWN_MIN`） |
| 载体 | 进程内 `dict`（键 → 冷却截止时刻，统一换算到 Asia/Shanghai 比较） |
| 写冷却时机 | **仅 `send_markdown` 成功后**。失败 / 缺配 / 不可见 **都不写** |
| 跨重启 | 不持久化；重启后短窗内可能重复一条（可接受，本拍不做） |

失败不写冷却这条是刻意的：一次网络抖动若把冷却写上了，这条命中就会静默 30 分钟，
而失败恰恰是最该重试的情形。

### 正文边界

| 允许 | 禁止 |
|---|---|
| `symbol` / `direction_label` / `signal_id` / `definition_version` / `asof` / `family` / `event_class` / `hit_id` / `DISCLAIMER_TEXT` | 价格 / 数量 / 仓位 / 订单 / 可执行指令 / 模拟盘字段 |

标题前缀是「**买卖信号命中** · {symbol}」，与定义态的「买卖信号 · {display_name}」
刻意不同——两个通道可能被填成同一个群，群里必须一眼分得清"被命中"和"被改状态"。

### 没有额外 enable 旗标（N3 写死＝不做）

门 = URL strip 后非空 ∧ 可见性闸 ∧ 非 fixture ∧ 冷却未命中。**不加**
`BUY_SELL_SIGNALS_HITS_DINGTALK_ENABLED`：多一个旗标就多一个"配了但忘了开"的位置。
对标 #227 复盘通道的既有做法。

### 本拍明确不做

- ~~命中钉钉推送（P-ding-later）~~ → **本拍即 P-ding-later 的交付**
- 日 cap（N4，另口令）；跨重启冷却持久化
- 生产第二机器人 webhook 的填值（**部署口令**，本仓只保证缺配拒发）
- `DSA_REVIEW_*` / 复盘日 cap / `market_review_push` 的任何改写
- 三 env（`BUY_SELL_SIGNALS_PERSIST` / `PUSH` / `DINGTALK`）字面量、语义、生产值
- `EMPTY_HITS_MESSAGE` 字面；可见性 AND 语义（只复用，不放松）
- 两处 `*_TRIGGERING_ENABLED` 翻 `True`；影子调度；新 crontab；lifespan 扫写 hits
- Opt-A（仍 defer）/ #6 / 整包 UX / IA / 竞价·放量实现扩 / 17–19 / MCP 下单

### 本节验收

- [x] 命中键与三 env 字面量各自钉死；`.env.example` 只有注释掉的空占位
- [x] 发送目标 == 命中 URL；静态断言 `hits_dingtalk` 中 `dingtalk_webhook_url` 只出现在
      同群比对函数里，`send_markdown` 第一个实参恒为 `url`
- [x] 缺配 → `hits_webhook_not_configured`、`attempts=0`、旧 URL 一次都没被请求（含 settings 有值时）
- [x] candidate / observe / limit_reopen / pending / 无 passed / 缺口径标签 → 全部不外发
- [x] fixture 默认拒发；`allow_fixture` 仅测例；fixture 门在缺配门之前
- [x] 正文含 symbol + direction_label + 免责；禁词逐个断言；标题与定义态可区分
- [x] 定义态 `_emit_dingtalk` 仍打共群 URL；命中模块不引用定义态门常量
- [x] 命中模块不出现复盘通道符号；本派不涉及复盘文件
- [x] `EMPTY_HITS_MESSAGE` 字节级不变；命中模块不碰它
- [x] 两处 `*_TRIGGERING_ENABLED` 仍 `False`；无调度 / 扫表 / 下单痕迹；`HitStore` 仍只读
- [x] **V1–V12** 全覆盖（缺配拒发 / 发送目标 / 禁回落 / 可见性 / fixture / 空态 / 三 env /
      复盘零碰 / 无真实凭证 / TRIGGERING / 冷却 / 同 URL warning）
- [x] 默认不部署；合入另等盘后口令

### 状态自述（P-bss-obs）

> 批次 `20261027gbssobs`。**只读露出，零行为扩**：不新增 `try_push_hit` 调用方、
> 不翻 TRIGGERING、不改三 env / 命中 webhook 生产值、不新增 hits/calib 写路径。

上面几节说的是「钉钉该不该发、发给谁」。本节只解决另一个问题：**运维在哪儿看它到底配没配**。
答案是复用已有的只读口 `GET /api/buy-sell-signals/hits/scope`（**并入**，不新开路由，N3）——
「为什么基本面是空的」和「为什么没有钉钉消息」本来就该在同一个地方回答。

响应在既有空态统计之外，并入一个嵌套对象 `hits_dingtalk`，其值**原样取**自既有
`hits_dingtalk_status()`（`hits_dingtalk.py`），这里不重写 configured 判定：

| 字段 | 含义 |
|---|---|
| `hits_dingtalk.configured` | 命中专用 webhook 是否非空（= `hits_webhook_ready()`） |
| `hits_dingtalk.shares_with_common_group` | 是否与共群 `DINGTALK_WEBHOOK_URL` 相同（**仅布尔**） |
| `hits_dingtalk.env_name` | 常量名 `BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL` |
| `hits_dingtalk.cooldown_min` | `30`（`HITS_DINGTALK_DEDUPE_COOLDOWN_MIN`） |
| `hits_dingtalk.reason_when_missing` | `hits_webhook_not_configured` |
| `volume_breakout_triggering_enabled` | 读 `Final`，现 **False** |
| `auction_abnormal_triggering_enabled` | 读 `Final`，现 **False** |

`notes` 刻意**不进**响应（N2）：那三句是给人读的说明，已写在本节与其上几节里，
塞进 JSON 只会让响应变长。**也不加** `empty_reason_chain` 数组（N4）：空态顺序靠
上表字段天然可观测，再造一个数组等于把同一份判断写两遍、两处会漂。

**禁**：把本节读成「已接线自动推送」。露出状态 ≠ 有调用方；`try_push_hit` 的
生产调用方本拍**一条都没加**，`HitStore` 仍是只读协议。

### 空态原因链顺序

读 scope 时按这个**固定顺序**往下查，前一步没通过就没必要看后面：

1. **`hits_file_exists`** — 命中文件在不在。评估器还没跑过时常为 `false`，这是最常见原因。
2. **两 TRIGGERING** — `volume_breakout_triggering_enabled` / `auction_abnormal_triggering_enabled`。
   现均为 **False**：放量突破与竞价异常的触发链路**本来就没开**。翻 True ＝ 另口令。
3. **`calib_passed_keys`** — 校准通过条数。`0` ＝ 没有任何已校准的信号可发布，
   命中再多也一条都不可见（可见性闸见上几节）。
4. **`hits_dingtalk.configured`** — 命中 webhook 配了没。`false` ＝ 缺配拒发，
   这是**预期状态不是缺陷**（见本节开头「缺配就是拒发」）。

顺序刻意如此：文件 → 开关 → 校准 → 配置。前三个回答「有没有东西可发」，
最后一个回答「配了没」。反过来读容易把「没配」当成「没信号」。

### P3a 可见性闸：只放 live（批次 20261028gp3a）

> 批次 `20261028gp3a` · P-bss-p3a。**三个闸，零写入**：不改 `HitStore`（仍只读）、
> 不写 hits/calib、不翻两 TRIGGERING、不新增 `try_push_hit` 调用方、不碰三 env。

前几节回答的是「一条命中要不要显示」。本节补一个更靠前的问题：**这条记录是从哪来的**。
影子回放（`mode=shadow`）、测例夹具（`mode=fixture`）与生产命中**可能落在同一个 store 里**
——共用一个 `hits.json` 是最省事的实现，也正因如此，单靠「路径约定」或「目录分开」挡不住它。

#### 闸 1：`visible_hits` 只放 `mode == "live"`

在 `hits_visibility.visible_hits` 里新增拒因 `NON_LIVE_MODE`，位置在所有 family / definition /
calib 判定**之前**。

| 输入 `source.mode` | 结果 |
|---|---|
| `"live"` | 继续往下过既有的 7 道闸 |
| `"shadow"` / `"fixture"` | `NON_LIVE_MODE` |
| 缺失 / `""` / 未知值（如 `"replay"`） | `NON_LIVE_MODE` |

判定用 `getattr(getattr(hit, "source", None), "mode", "") != "live"`，是 **fail-closed**
而不是 `in {"shadow", "fixture"}`：后者会把「以后有人加了第四种 mode」默认放行。

位置刻意放最前，理由有两条。一是语义顺序——来源不明的数据不该先去查它有没有被批准、
有没有成绩。二是**任意 calib 都不能洗白**：后面那道 calib passed 永远没机会把它放出来。
（所以测例里「shadow 命中 + 全套 passed 校准登记 → 0 可见」是该闸的关键负控。）

#### 闸 2：`try_push_hit` 拒发影子命中，且**没有** `allow_shadow`

`hits_dingtalk.REASON_SHADOW = "shadow_not_pushable"`，门 1 之后即为门 1b，**在缺配判定之前**。

它与 `fixture_not_pushable` **故意不对称**：

- `fixture` 有 `allow_fixture` 开关。理由成立——夹具命中是测例自己造的，测「格式对不对」
  时得让它走到发送那一步。
- `shadow` **没有** `allow_shadow`，也不许将来加。影子回放的意义就是「假装是生产」，
  而唯一真正想看它发出去的人，是**正在测发送链路的人**——那种测例应该用 fixture。
  给了 `allow_shadow`，「影子进了生产通道」就从「配置错误」降级成「一个参数」。
  有一条测例直接对 `inspect.signature` 断言：有 `allow_fixture`，**没有** `allow_shadow`。

门序 1 → **1b** → 2 → 3 → 4 → 5（1b 在缺配之前，所以缺配时 shadow 命中报的是
`shadow_not_pushable` 而不是 `hits_webhook_not_configured`——这才是真实原因）。
影子命中不写冷却记录。

#### 闸 3：`GET /hits/scope` 露出扁平计数 `non_live_mode_rejected`

```json
{ "non_live_mode_rejected": 1 }
```

- **扁平 int**，不是嵌套对象（N1）。
- **常态 0**：空 store 是 0，全是 live 也是 0。**非 0 ＝ 报警**——「生产 store 里混进了
  不该在这儿的东西」，而不是「用户看不到信号」的正常原因。
- **只回数字**：不回 symbol / hit_id / 明细数组。把明细列出来等于从只读口把影子数据
  放出来，与本拍目的正相反。
- 跑的是与 `GET /hits` **同构**的那一次 `visible_hits`（同一个 registry / scope / calib 索引），
  区别只是只取计数。同构很重要，否则两边会漂。
- 它**不参与**上面那条空态主因链，只是旁注。

> 读 scope 时若要把这个数字当真，注意 `hits_scope` 用的是 `DiskHitStore(data_dir)`
> （生产 live store 那条路径），**不读** `app.state.buy_sell_signal_hit_store`。
> 这也意味着测试里想让它非 0，必须把 `mode=shadow` 的命中真写进 `hits.json`。

#### 本拍明确不做

- 不清洗存量 `hits.json` 里的影子记录（**零写入**：闸在读取侧，不在写入侧）
- 不做 `shadow → live` 的迁移脚本、不加 `--promote` 之类开关
- 不翻两 `*_TRIGGERING_ENABLED`，不签发 K8
- 不改 `EMPTY_HITS_MESSAGE`，不改既有 7 条拒因的语义

### P1 真实日频定义注册（daily_fundamental · 批次 20261028gp1）

> 批次 `20261028gp1` · P-bss-p1。**未部署**；**非触发**；**非放量**。
> 注册完成 ≠ calib 已过 ≠ 可开 TRIGGERING。

前三节讲的是「命中要不要显示、外发不发」。本节补一层更底下的东西：**注册表里到底有没有
一条真的日频定义**。

到 #240 为止，答案是「没有」——仓里只有夹具能造出 `daily_fundamental` 记录，而夹具是
`publishable` 的（它必须如此，否则测不了「publishable 才可见」）。一条 publishable 的
夹具当成生产真值，等于凭空跳过人工发布闸。所以本拍新增一个**独立工厂**，
产出**初态 `candidate`** 的真实定义。

#### 注册对象

| 项 | 值 |
|---|---|
| `signal_id` | `bss.daily_quality_value`（**不是**夹具 `fx.pub.daily_quality_value`） |
| `version` / `definition_version` | `v1`（**不是**夹具 `fixture_v1`，也不是 `design_version()` 默认值） |
| `family` / `event_class` | `daily_fundamental` / `daily_quality_value` |
| 初态 `status` | **`candidate`**（本拍**不调** `manual_publish`） |
| `auto_promote_forbidden` / `manual_publish_required` | `True` / `True` |
| `FreqLockStamp.cadence` | `daily_fundamental` |
| 模块 | `backend/app/buy_sell_signals/daily_fundamental_definition.py` |

`version` 必须显式传 `v1`：`SignalRecord.version` 的默认是 `design_version()`，拼出来是
`design_pin_v1+independent_family_v1:m6` —— 那是 Bonferroni 多重检验口径的标签，不是
定义版本。混用会让三键对齐（`signal_id` + `definition_version` + `freq_lock_version`）
永远对不上，而从输出上看不出为什么。

#### PIT：`announce_date <= asof`

**锚在 `quality_proxy`，本拍只声明、不改它的行为。**

| 项 | 契约 |
|---|---|
| 截断键 | `announce_date`（公告日 / 可得日） |
| 判据 | `announce_date <= asof` |
| 禁止 | 用 `period_end`（报告期末）做可知性截断 |
| 空值 | 公告前财务字段保持 `None`，**不填 0** |
| 落点 | `FreqLockStamp.params` 与 `SignalRecord.metadata` **两处都有** |

用 `period_end` 判是前视：三季报 9 月 30 日截止、10 月底才公告，按 `period_end <= asof`
判的话，10 月 1 日就能「知道」10 月 28 日才公布的数字。方向永远对策略有利，所以它是
唯一一处「错得很隐蔽且永远帮多头」的错。

两处都写是**冗余但不是重复**：`FreqLockStamp` 是评估侧容器、可能被单独序列化流转；
`metadata` 跟着 `SignalRecord` 走，而注册表才是最常被读的那个入口。只写一处，读的人
就会以为「这条定义没声明 PIT」。

#### 样本 ≥30：全市场影子计数

| 项 | 契约 |
|---|---|
| 阈值 | `CALIB_SAMPLE_N_MIN`（现 30），**引用不另写字面量** |
| 计数域 | `universe_shadow` —— 全市场影子（`source.mode=shadow`） |
| 禁止 | 用 `/hits` 按 scope（自选 ∪ 持仓）过滤后的可见条数充当 ≥30 |

拿 scope 后的条数凑 30，等于把「这只票在你的自选里」当成统计前提：同一个信号，自选
40 只的用户「已达标」，自选 5 只的用户「没达标」。门槛跟着用户配置变，那就不是门槛。

本拍**只钉口径**（常量 + 元数据 + 测例断言），**不**实现影子写入（P3b）、**不**跑回放
凑满 30（P2）。所以：**样本还没凑、calib 还没跑、门槛还没过。**

#### 禁选 `volume_breakout`

它是 `minute_event` 家族的桩，R2-5 明文不选作日频。测例用负向断言钉住：真实定义的
`signal_id` 不是它、`family` 不是 `minute_event`，且两处 TRIGGERING 仍 `False`。

#### 字段细化（N1）不挡最小注册

沿用 `quality_proxy.QUALITY_METRIC_COLUMNS` 七列（`roe` / `gross_margin` /
`debt_to_asset_ratio` / `net_income_yoy` / `revenue_yoy` / `eps` / `bps`），
`weighting_status="design_pending"`，`w_quality` / `w_value` 保持 `None`。

「不挡」不等于「先填个 0.5 凑着」——填了值，下游就会当成已标定的权重。

#### 零碰

不改三 env 与 hits webhook 生产值；不写 `HitStore`；不调 `calib.upsert`；
不接评估器 / P3b；不翻两 TRIGGERING；不回退 #240 的三闸。
（P2.5 口径锁见批次 `20261028gp25`，P2 评估器与截断回放见批次 `20261028gp2`，两者均
**只读**本条定义、不改它；且**锁面在场 ≠ 评估已过门 ≠ 本条定义可放量**。）
生产 `main` **不**自动注册这条定义（本拍只有测例与显式调用会注册）。

### P2.5 口径预登记锁（批次 20261028gp25 · P-bss-p2.5）

> 批次 `20261028gp25` · P-bss-p2.5。**未部署**；**非触发**；**非放量**。
> **口径锁 ≠ 可放量**：样本未凑、评估未跑、calib 未过、可见性与 TRIGGERING 一律未动。

P1 把「真实日频定义」注册进了注册表。本节回答下一个问题：**拿什么口径去评估它**。

答案是「先锁口径，再跑评估」。原因很简单：一件事的后验，只要指标可以换，就一定能
挑出一个「过」的。T+1 不过看 T+5，T+5 不过看 T+20；命中率不过看 Wilson 下界，
下界不过看均值；显著性不过就把 Bonferroni 换成 Holm 或 BH。每一步单独看都
「只是换个更合适的口径」，连起来就是**事后选指标**。

事后选指标的危险不在于算出假数——算出的每个数都是真的——而在于它们是
「在所有可选项里挑了最好的那个」之后的数，而报告里不会写「我挑了 17 次」。
读的人看到「主 horizon T+5，命中率显著」，看不到那 16 个没过的。

所以本拍把选择**在开跑之前**做掉，写进代码。

#### 模块

`backend/app/buy_sell_signals/calib_p25_lock.py` —— 纯常量 + 纯函数，**零 I/O**：
不读文件、不写文件、不碰 `HitStore`、不调 `calib.upsert`、不读 env、不引用任何
`TRIGGERING_*`。测例用源码级的「不得出现」断言把这几条钉住。

#### 锁面九项

| 项 | 锁值 | 要点 |
|---|---|---|
| 主 horizon | `CALIB_PRIMARY_HORIZON_TD = 5`（T+5 交易日） | **唯一**主指标 |
| 辅 horizon | `CALIB_AUX_HORIZONS_TD = (1, 20)` | 只报不当门；两个都必报 |
| 成本 | `P25_COST_BP_ROUNDTRIP is CALIB_COST_BP_ROUNDTRIP`（18.2 bp **来回**） | 引用不另写 |
| 显著判据 | Wilson 95% **下界** > 0.5（`WILSON_POSITIVE_RULE`） | 严格大于，非点估计 |
| 样本下限 | `P25_SAMPLE_N_MIN is CALIB_SAMPLE_N_MIN`（30） | 引用不另写 |
| 对齐键 | `P25_ALIGN_KEY_FIELDS = ("signal_id","definition_version","freq_lock_version")` | 与 `CalibRecord` / `FreqLockStamp` 逐位一致 |
| 跨版本 | `P25_CROSS_VERSION_MERGE_FORBIDDEN is True` | 不同 `definition_version` 不许合并凑样本 |
| 多重检验 | 引用 `BONFERRONI_{SCHEME_ID,M,FAMILY_ALPHA}`；`P25_HOLM_BH_SUBSTITUTE_FORBIDDEN is True` | 禁 Holm / BH 替换 |
| 失败 / 换轨 / 定序 | 任一门未过 → `"failed"`（**不是** `pending`）；`P25_PRIMARY_SWAP_FORBIDDEN is True`；`P25_BEFORE_P2 is True` | 有结论且没过就说没过 |

#### 为什么判据是 Wilson 下界，不是命中率

点估计 `k/n > 0.5` 在 n=30 时几乎不构成证据：18/30 的点估计 0.60 看着「过六成」，
但 95% 下界只有 0.423——重抽一次就可能掉到 0.42。用点估计当门，等于用
「一次投硬币的结果」当「硬币不偏」的证据。

Wilson 下界把样本量算进判据。30 个样本时：

| 命中 / 样本 | 点估计 | Wilson 95% 下界 | 判据 |
|---|---|---|---|
| 20 / 30 | 0.667 | 0.4878 | **不过** |
| 21 / 30 | 0.700 | 0.5212 | 过 |

也就是说 30 个样本里至少要对 21 个（实际命中率 70%）才够。这个门槛刻意比
「看起来合理」高——30 本来就是下限而非目标。n=300 时 60% 的命中率下界约 0.543
就过了，因为**样本多本身就构成证据**。

判据写成严格 `> 0.5`（另加 `P25_EPS = 1e-12` 的**单侧**浮点容差），不留
「刚好卡线算过」这个口子。

#### 为什么禁跨版本合并样本

v1 跑 15 个没过，改两行参数出 v2 再跑 15 个，合并算 30 个——这是**用两个不同
口径的样本凑一个门槛**。合并后的样本来自两个不同分布（参数变了），「命中率」
在合并样本上根本没有定义。

更实际的问题：如果允许合并，「改参数重跑」就成了一条永远能过的路——每轮凑不够
就发个新版本接着凑。`hits_visibility` 的三键对齐已经隐含这条，本拍把它写成
**显式常量**，让「想合并」成为一个必须改常量并让测例红的动作。

#### 为什么禁 Holm / BH

Bonferroni 在族内是**最保守**的。想让它过的人第一反应往往不是伪造数据，而是
「换成 Holm 吧，更有效」。Holm 确实 power 更高，但它也**更容易过**——而这里要的
恰恰是那个更保守的结论。

更深一层：一旦方案可变，`m=6` 与 `α=0.0083` 这两个钉子就失去约束力，因为任何人
都可以声称「按 BH，q=0.1 算过了」。方案是口径的一部分，不是实现细节。

#### 为什么失败是 `failed` 而不是 `pending`

`pending` 的语义是「还没有结论」。样本凑够、评估跑完、某一门没达标——这已经
**有结论**了，结论是「没过」。写成 `pending` 会让它看起来像「等着就好」，
而它其实在等一个永远不会来的东西。

`CALIB_STATUSES` 是封闭三值，这里取其中「已有结论且没过」的那一个。四舍五入成
`pending` 是本仓明文禁止的悄悄降级。

#### `p25_gate_verdict()` 的边界

本模块提供一个**判据**函数，不是**评估器**：

```python
p25_gate_verdict(*, sample_n, hits, cross_version_merged=False,
                 substituted_test=None, primary_swapped=False) -> "passed" | "failed"
```

输入全是摘要（样本数、命中数、几个「有没有违规」的布尔），输出一个状态字符串。
它不读数据、不算收益、不查注册表、不写 calib。

存在的意义是让「什么算过」这句话有一个可测的实现：P2 真跑评估时应当调这个函数
得出结论，而不是自己再写一遍 if 链——那样两处判据迟早分叉。

五个子门全部必须过：样本 ≥30 → Wilson 下界 >0.5 → 未跨版本合并 → 未替换检验 →
未换主指标。**没有 `pending` 分支**：走到这里说明有结论了。

#### 与 P2 的定序

`P25_BEFORE_P2 is True`：P2.5 是 P2 的**前置**。P2 真跑回放时应当**读**这里的
常量，而不是自己再定一套主 horizon。定序反过来（先跑 P2 再补锁面）等于先看结果
再定口径，那时锁面已经没法起约束作用。

#### 零碰

不改三 env 与 hits webhook 生产值；不写 `HitStore`；不调 `calib.upsert`；
本拍(P2.5)**不接**评估器 / P3b；P2 评估器已在批次 `20261028gp2` 落地并**读**本锁面
（见下节「P2 评估器与截断回放」）；不翻两 TRIGGERING；不回退 #240 的三闸；不引 scipy /
statsmodels（Wilson 只需一个正态分位数，写死在模块内）。生产 `main` **不**自动
调用任何 P2.5 判定。

#### 测例

`backend/tests/test_buy_sell_signal_calib_p25_lock.py`，G-T1 – G-T13 共 60 条。
重点不是「常量能读到」，而是**口径不能被悄悄换掉**：主 horizon 只能一个、
辅 horizon 两个都在、成本与样本下限必须是**同一对象**（`is`）、三键字段序必须与
两条 `align_key` 的实际取值逐位一致、跨版本/换检/换主指标都必须真的让判定
返回 `failed`、判定**从不**返回 `pending`。

复跑：`PYTHONPATH=.:backend python3 -m pytest backend/tests/test_buy_sell_signal_calib_p25_lock.py -q`

### P2 评估器与截断回放（批次 20261028gp2 · P-bss-p2）

`backend/app/buy_sell_signals/calib_p2_eval.py`。这是 P2 的**评估器 + 截断回放**，
对象是 #241 注册的 `bss.daily_quality_value`，**不是**新增一条信号定义。

#### 读锁面, 不自写口径

评估器里**没有**任何一个可以自己改的口径字面量：

| 口径 | 取值 | 来源 |
|---|---|---|
| 主 horizon | `CALIB_PRIMARY_HORIZON_TD`（T+5） | #242 `calib_p25_lock` |
| 辅 horizon | `CALIB_AUX_HORIZONS_TD`（T+1 / T+20） | #242 |
| 成本 | `CALIB_COST_BP_ROUNDTRIP`（18.2bp） | `constants` |
| 胜出判据 | `wilson_hit_rate_lower_bound` > `WILSON_HIT_RATE_LB_MIN`（0.5） | #242 |
| 样本下限 | `CALIB_SAMPLE_N_MIN`（30），按三键不跨版本 | `constants` + #242 |
| 失败状态 | `P25_ANY_GATE_FAIL_STATUS`（`"failed"`） | #242 |

判定**直接调 `p25_gate_verdict`**，不在评估器里再写第二套 if。测例 G-T2 用剥串扫描
证明 `18.2` / `0.5` / `30` / `= 5` 这些数字**一个都不出现**在评估器源码里。

#### PIT 与样本域

- **PIT 截断键 = `announce_date`**（公告日），**不是** `period_end`（报告期末）。
  两者在真实数据里常常反向：早公告晚期末 / 晚公告早期末。G-T4 刻意构造这种反向
  数据，用 `asof` 截断后只应留下公告日在前的那一批。
- **样本域 = `universe_shadow` 影子行**（`EvalRow.shadow is True`）。非影子行
  与 `announce_date > asof` 的行一律不入样。
- 行集合按 `(asof, symbol, announce_date)` 定序 —— 回放可复现的前提。

#### 截断回放：两次逐字段一致

`truncated_replay_once(...)` 返回一整个 dict；`truncated_replay_twice_equal(...)`
跑两次并返回 `(first, second)`。测例逐字段比对**含 `fingerprint`**：

```
schema_version / signal_id / definition_version / freq_lock_version / cadence
asof / pit_cutoff / sample_domain / sample_n / cost_bp_roundtrip
primary / aux / gate_status / path_status / fingerprint
```

`fingerprint` 复用 `freq_lock_fingerprint`（sha256 全 64 位），因此**只对口径、
不对样本**：换 `params` 或 `definition_version` 指纹必变；样本换一批而口径不变，
指纹相同。这一条由 G-T14 双向钉住，避免拿指纹冒充「回放已复现」。

#### 路径状态 ≠ calib 状态 ≠ 可放量

评估器另起两个**与 `CALIB_STATUSES` 不重叠**的路径状态：

| 常量 | 值 | 含义 |
|---|---|---|
| `P2_PATH_STATUS_REACHED` | `"path_reached"` | 回放跑通了，门没过 |
| `P2_PATH_STATUS_GATE_MET` | `"gate_formally_met"` | 回放跑通，且锁面判定为 `passed` |

G-T7 断言 `P2_PATH_STATUS_REACHED not in CALIB_STATUSES`：路径状态**不能**
被读成 calib 结论。三句话必须分清：

> **路径已通 ≠ calib 已过 ≠ 可放量 ≠ `TRIGGERING_ENABLED`。**

两 `TRIGGERING_ENABLED` 本拍仍 `False`；评估器不写 `HitStore`、不调 `calib.upsert`、
不挂路由、不读任何 webhook 环境变量（G-T9 / G-T10 守着）。

#### 零碰

不改三 env 与 hits webhook 生产值；不写盘；不接 P3b 影子写入；不翻两
TRIGGERING；不回退 #240 / #241 / #242 / #243 任何符号；不引 scipy / statsmodels。
评估器是**纯函数 + 纯常量，零 I/O** —— 它不会自己去取数，输入由调用方给。

> 「不接 P3b 影子写入」是 **P2 那一拍**的范围声明。P3b 已在批次 `20261028gp3b`
> 落地（见下节「影子生产写入 · 独立三键分桶」）；P2 评估器本身**至今仍是纯函数**，
> 不会被 P3b 反向接进来 —— 谁写盘由调用方决定，评估器不知道桶的存在。

#### 测例

`backend/tests/test_buy_sell_signal_calib_p2_eval.py`，G-T1 – G-T17 共 63 条。
重点不在「能跑出数」，而在**口径不可偷换**：主 horizon 不能被改成 20、辅指标
全赢也不能顶替主指标（G-T11：T+1 全命中 / T+5 全亏 → 仍 `failed`）、截断键不能
被换成 `period_end`、非影子行不能混入、两次回放不能不一致、指纹不能被当成
「回放已复现」的证据。

其中 **G-T15 / G-T16 是判别性样本**，专门用来抓「判据与指标被悄悄换掉」：

- G-T15：n=30、hits=20 → 点估计 **0.6667（过）** 而 Wilson 下界 **0.4878（不过）**，
  结论必须 `failed`。只断言 `wilson_rule` 字符串、或只看全赢样本，都抓不住
  「判据被换成点估计」。配对样本 n=30、hits=21 → 下界 0.5212（过）→ `passed`。
- G-T16：T+1 全赢 / T+5 全亏 / T+20 全赢，两个辅指标都给 `passed`，只有真正在
  T+5 上算的那个给 `failed` → 结论 `failed`。这样「主 horizon 被换成辅之一」
  必被抓，而硬编码一个 `primary_horizon_td = 5` 字段骗不过去。

复跑：`PYTHONPATH=.:backend python3 -m pytest backend/tests/test_buy_sell_signal_calib_p2_eval.py -q`

### 影子生产写入 · 独立三键分桶（批次 20261028gp3b · P-bss-p3b）

`backend/app/buy_sell_signals/hits_shadow_store.py`。补齐 R2-7② 的**影子生产写入
路径** —— 在这之前, 影子命中只能活在测例的 `tmp_path` 里, 落不到 `data/`。

#### 分桶是物理隔离, 不是一个标志位

路径写死为三键目录:

```
<data_dir>/buy_sell_signals/shadow/<signal_id>/<definition_version>/<freq_lock_version>/hits.json
```

| 常量 | 值 | 说明 |
|---|---|---|
| `SHADOW_SUBDIR` | `"shadow"` | 挂在 `PERSIST_SUBDIR`（`buy_sell_signals`）之下 |
| `SHADOW_BUCKET_KEY_FIELDS` | `("signal_id", "definition_version", "freq_lock_version")` | 顺序即路径层级顺序 |
| `SHADOW_MODE` | `"shadow"` | 与 `HIT_SOURCE_MODES` 中的那一项同一个字面量 |

**为什么不混进 live `hits.json`**: 一旦同文件, "这条是否已过闸"就必须靠读每条的
`source.mode` 来判断 —— 那是**逻辑隔离**, 任何一处漏读 `mode` 就直接把影子暴给用户。
分桶是**物理隔离**: live 读口 `DiskHitStore.list_all()` 走的是
`<data_dir>/buy_sell_signals/hits.json`, 与 `shadow/` 子树毫无交集, 读不到就是读不到。
G-T8 双向钉住: 先写影子若干条 → `DiskHitStore.list_all() == []`; 再让 live 文件
存在 → 影子写入后 live 文件**逐字节不变**。

**为什么不共用一个 `shadow` 标志位**: 三键（信号 / 定义版本 / 频率锁版本）任意一个
变化都意味着「这批数不是同一口径的」。目录层级让**跨口径串数在文件系统层面不可能发
生**, 而不是靠查询时过滤。G-T11 用同一 `hit_id`、不同 `freq_lock_version` 写两次,
断言两个桶各自 1 条、互不覆盖。

#### 写入契约：只收 shadow, 三键不可空, 拒穿越

| 检查 | 违规时 | 理由 |
|---|---|---|
| `hit.source.mode == "shadow"` | `ShadowWriteError` | `live` / `fixture` 不该借这个口落盘 |
| `freq_lock_version` 非空 | `ShadowWriteError` | 空串会让所有口径塌进同一个桶 |
| 三键均非空且不含 `.` / `..` / `/` / `\` / NUL | `ShadowWriteError` | 键来自信号定义, 不该也不可能带路径分隔符; 带了一定是构造错误或注入 |

`upsert_shadow_hit(hit)` 单条写; `write_shadow_hits(hits)` 批量写, 返回
`{桶路径: 该桶条数}`, **全有或全无** —— 批量里任意一条不合契约, 一条都不落盘
（先全量校验, 再分组写）。

**幂等按 `hit_id`**: 同 `hit_id` 再写一次是**替换**（不是追加, 也不会出现两条同 id）;
不同 `hit_id` 是追加。这让"同一天重跑两次管道"不会把桶撑大。

#### 原子写

`tempfile.mkstemp(dir=<桶目录>)` 写临时文件 → `os.replace` 覆盖目标。同文件系统内
`os.replace` 是原子的, 因此**任何时刻磁盘上的 `hits.json` 要么是旧版, 要么是新版,
不会是半截 JSON**。进程被杀在写一半时, 留下的是一个 `.tmp` 孤儿文件, 而不是一个
读不出来的桶。临时文件刻意建在**桶目录内**（不是 `/tmp`）, 否则跨文件系统
`os.replace` 会退化成非原子的 `copy + unlink`。

#### 落盘 ≠ 可见 ≠ 可推

这一句是本拍最需要反复确认的:

> **影子能落盘了。P3a 三道闸一点没松。**

G-T6 是逐字验证: 写入 → 从桶里读回（`len(back) == 1`）→ 喂给
`visible_hits(..., registry=None)` → `filtered.hits == []` 且拒因里
`NON_LIVE_MODE >= 1`; 再喂 `try_push_hit` → `reason == shadow_not_pushable`,
`opener` **零调用**。

`HitStore` Protocol 本拍**零改动**, 仍只有 `list_all()` —— 没加 upsert / write /
save / append（G-T8c 扫描 `hits_store.py` 源码守着）。影子写入路径是**新增的独立
模块**, 不是给既有只读口开一个写口。

#### 零碰

不改三 env（`PERSIST` / `PUSH` / `DINGTALK`）; 不改 `push.py` / `store.py` /
`main.py`; 不翻两 `TRIGGERING_ENABLED`; **不加 `allow_shadow` 之类的开关**
（有一个开关, 就有一个把它打开的地方 —— 影子要不要可见不是配置项, 是不做）;
不接日管道自动写; 不回退 #240 / #241 / #242 / #243 / #244 / #245 任何符号。

#### 测例

`backend/tests/test_buy_sell_signal_hits_shadow_store.py`, G-T2 – G-T14 共 40 条。
重点不在「能写能读」, 而在**隔离不可绕过**: 路径层级不能被压平、影子不能被 live
读口读到、写入后闸不能松、三键不能塌、批量不能写一半、原子写不能被换成直接
`write_text`。

其中三条是**判别性样本**:

- **G-T6**：写进去之后 `visible_hits` 仍 0 条 —— 只测「写入成功」抓不住「顺手把
  影子塞进可见集」。
- **G-T8b**：live 文件存在时逐字节比对 —— 只测「live 读口读不到影子」抓不住
  「影子写入顺手把 live 文件重写了」。
- **G-T11**：同 `hit_id` 跨 `freq_lock_version` —— 只测「同桶内幂等」抓不住
  「三键被拼成一个桶」。
- **G-T13**：30 条夹具落同一桶 —— 特意在测例里写明 **「30 条只是样本落位,
  不等于样本达标」**, 免得把「桶里有 30 条」读成 `CALIB_SAMPLE_N_MIN` 已满足。

复跑：`PYTHONPATH=.:backend python3 -m pytest backend/tests/test_buy_sell_signal_hits_shadow_store.py -q`

### 配置清单（env / 缺配拒发 / 不回落 / 冷却）

| 项 | 值 |
|---|---|
| 命中 webhook env | `BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL` |
| 可选 secret env | `BUY_SELL_SIGNALS_HITS_DINGTALK_SECRET`（空 ＝ 该机器人未开加签） |
| 缺配 | 拒发 `hits_webhook_not_configured`；`attempts = 0`，一个字节都不发 |
| 回落共群 | **禁止**；不读 `DINGTALK_WEBHOOK_URL` 作发送目标（只用于同群比对告警） |
| 冷却 | `HITS_DINGTALK_DEDUPE_COOLDOWN_MIN = 30`，**仅成功后写** |
| 额外 enable 旗标 | **无**：URL 非空即门 |
| 状态入口 | `GET /api/buy-sell-signals/hits/scope` → `hits_dingtalk.*` |
| 空态链 | 见上一小节 |

生产填命中专用机器人 webhook ＝ **部署口令**；本仓只保证缺配拒发与状态可观测。
`hits_dingtalk` 响应里**不含** url / secret，别指望从这个口把地址读出来。

### T1 切档规则（批次 20261030gt1）

展示层 **弱 / 中 / 强** 切档规则（**无量＝无档**；日频 / 事件两族**各一量**；规则版本
可审计；展示三项必填；展示边 Bonferroni 不占 `m=6`；**T1≺T2**）见：
[buy-sell-signal-tier-cut.md](./buy-sell-signal-tier-cut.md)。

常量锁面：[`backend/app/buy_sell_signals/tier_cut_lock.py`](../backend/app/buy_sell_signals/tier_cut_lock.py)
（纯常量 + 断言，无 I/O；**不算行情、不写 hits、不接线 UI / 钉钉**）。

本拍＝**T1 规则文档 + 常量锁面**；**不是** T2 展示字段接线；**不是** schema 进 hits；
**不是**可放量。规则版本 `tier_cut_v1`；改量 / 阈值 / 映射须 bump。
定序写死 **T1 ≺ T2**：T1 未锁，T2 无从接线。

### T2 展示层字段契约（批次 20261030gt2）

展示层弱/中/强 × 方向的**字段契约**（载荷**必填四元组**＝轨 + asof 粒度 + mode + tier
或**显式无档**；**禁六词动作指令式**；**禁价量仓单**；只读接线 T1；门禁影子 ≠ live）见：
[buy-sell-signal-tier-display.md](./buy-sell-signal-tier-display.md)。

常量锁面：[`backend/app/buy_sell_signals/tier_display_lock.py`](../backend/app/buy_sell_signals/tier_display_lock.py)
（纯常量 + 断言，无 I/O；只读 import `tier_cut_lock`；**不算行情、不写 hits、不接线 UI / 钉钉**）。

本拍＝**T2 字段契约文档 + 薄锁面**；**不是** schema 进 hits；**不是**真推落地；
**不是**算档 / UI 真接线；**不是**可放量。锁面版本 `tier_display_v1`；
**T1≺T2（T1 已满足）**。

### R2-3 独立影子观测口契约（批次 20261031gshadow）

独立只读影子观测口契约（**默认关 / 显式开**；空态/过期标记 `closed`/`empty`/`stale`/`ok`；
**空桶不得报 `ok`**；内容**不并入** `/hits/scope`；只读 P3b 三键分桶；兼容 T1/T2 四元组；
禁六词与价量仓单；门禁**路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING**）见：
[buy-sell-signal-shadow-obs.md](./buy-sell-signal-shadow-obs.md)。

常量锁面：[`backend/app/buy_sell_signals/shadow_obs_lock.py`](../backend/app/buy_sell_signals/shadow_obs_lock.py)
（纯常量 + 断言，无 I/O 写路径；只读交叉 T1/T2 + P3a；**不算档、不写 hits、
不接线完整 UI / 钉钉**）。

本拍＝**R2-3 观测口契约文档 + 薄锁面**；**不是**完整 UI 开通；**不是** schema 进 hits；
**不是**真推落地；**不是**可放量；**不是**部署 #260。锁面版本 `shadow_obs_v1`；
默认关；P3b `list_shadow_hits`＝核验钩子≠本观测口。**tip ≠ prod**（prod 仍 `7b55d024…`）。

### 触发/放量门禁契约（批次 20261031gtrig）

触发/放量**门禁**契约（**不是**触发引擎、**不是**放量开闸、**不是**授权翻闸；两
`*_TRIGGERING_ENABLED` **Final=False 非 env**；翻闸另口令；前置条件表**表≠已可翻**；
与 COMPLETE/calib/K8/影子观测口关系；证据缺口 **已清 2 / 剩 6**（VOLUME 2 + AUCTION 4；**清零一刀 ≠ 可翻闸**；**落盘 ≠ 可开 TRIGGERING**；**探针 PRESENT ≠ 可开 TRIGGERING**；**负锁语义 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING**；**负锁 ≠ 删标签**；**钉数 ＝ fabricate**；**本拍 ≠ 清零刀**）；禁六词与价量仓单；
门禁**路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING**；**K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING**；
**门禁契约 ≠ 授权翻闸**）见：
[buy-sell-signal-trigger-gate.md](./buy-sell-signal-trigger-gate.md)。

常量锁面：[`backend/app/buy_sell_signals/trigger_gate_lock.py`](../backend/app/buy_sell_signals/trigger_gate_lock.py)
（纯常量 + 断言，无 I/O 写路径；只读交叉 stub 两 TRIGGERING + COMPLETE +
shadow_obs/T1/T2；**不改 stub 取值、不算行情、不调度、不真推**）。

阈值负锁语义锁面：[`backend/app/buy_sell_signals/threshold_neg_lock.py`](../backend/app/buy_sell_signals/threshold_neg_lock.py)
（`threshold_neg_lock_v1`；纯常量 + 断言，**无 I/O**；咬住「`threshold_lock_data_pending` 必须仍在」+
「禁钉可交易量比/突破%/窗口分钟数」+ 两条只读交叉；**不清标签、不降 COUNT、不翻闸**）。

本拍＝**触发/放量门禁契约文档 + 薄锁面**；**不是**真翻 TRIGGERING；**不是**宣称可放量；
**不是**完整触发引擎；**不是** schema 进 hits；**不是**改三 env。锁面版本 `trigger_gate_v1`；
COMPLETE 仍 False；**tip = prod = `c1b19fc8…`**（#261 已 live）。

### hits schema 真推门禁契约（批次 20261031gschema）

hits schema **真推门禁**契约（**不是**立刻真推上线、**不是**完整 schema 迁移、
**不是**授权真推 live；`HITS_SCHEMA_VERSION` 仍 `"1"`；真推另口令；
前置条件表**表 ≠ 已可真推**；与 P3a/shadow/trigger_gate/live 模板关系；
禁六词与价量仓单；门禁**路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING**；
**K8 ≠ 可放量 ≠ 可开 TRIGGERING**；**门禁契约 ≠ 授权翻闸**；
**schema 门禁契约 ≠ 授权真推 live**）见：
[buy-sell-signal-schema-push.md](./buy-sell-signal-schema-push.md)。

常量锁面：[`backend/app/buy_sell_signals/schema_push_lock.py`](../backend/app/buy_sell_signals/schema_push_lock.py)
（纯常量 + 断言，无 I/O 写路径；只读交叉 schema version + 两 TRIGGERING + COMPLETE +
trigger_gate/shadow_obs/T1/T2；**模块别名活引用**；**不改 models/模板、不 bump schema、
不算行情、不调度、不真推**）。

本拍＝**hits schema 真推门禁契约文档 + 薄锁面**；**不是**真推 live；**不是** bump schema；
**不是**真翻 TRIGGERING；**不是**宣称可放量；**不是**改三 env。锁面版本 `schema_push_v1`；
COMPLETE 仍 False；schema 仍 `"1"`；**tip = prod = `5569c8b5…`**（#262+#261+#260+#259… live）。

### hits schema 真推 live 开档（批次 20261031gschemalive）

hits schema **真推 live 开档/落地层**契约（**不是** bump schema、**不是** 新增 live
写入/调度、**不是** 翻 TRIGGERING、**不是** 可放量；`HITS_SCHEMA_VERSION` 仍 `"1"`；
旁注「研究档 · 未放量」已落到模板；前置核对清单**表齐 ≠ 可放量 ≠ 可翻闸 ≠ 可调度写 live**；
`build_hit_dingtalk_text` **仅追加**旁注、`try_push_hit` 门序零碰；
门禁**路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING**；**K8 ≠ 可放量 ≠ 可开 TRIGGERING**；
**门禁契约 ≠ 授权翻闸**；**schema 门禁契约 ≠ 授权真推 live**；
**开档/旁注落地 ≠ 可放量 ≠ 翻闸 ≠ 已有 live 写入**）见：
[buy-sell-signal-schema-push-live.md](./buy-sell-signal-schema-push-live.md)。

常量锁面：[`backend/app/buy_sell_signals/schema_push_live_lock.py`](../backend/app/buy_sell_signals/schema_push_live_lock.py)
（纯常量 + 断言，无 I/O 写路径；只读交叉 schema_push_v1 + schema version + 两 TRIGGERING +
COMPLETE + trigger_gate/shadow_obs/T1/T2；**模块别名活引用**；不 import `hits_dingtalk`
——旁注一致性由测例交叉咬住；**不改 models 字段集、不 bump schema、不算行情、不调度、
不写 live、不改 try_push_hit 门序**）。

本拍＝**真推 live 开档契约文档 + 旁注最小触碰 + 薄锁面**；**不是** bump schema；
**不是** 真翻 TRIGGERING；**不是** 宣称可放量；**不是** 改三 env；**不是** 新增 live
写入。锁面版本 `schema_push_live_v1`；COMPLETE 仍 False；schema 仍 `"1"`；
**tip = prod = `5ca44098…`**（#263+#262+#261+#260+#259… live）。

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*
