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

### 四条证据缺口：全 `data_pending`

| 缺口 | 现状 |
|---|---|
| `local_minute_lib_data_pending` | 本机既没挂载也没落盘分钟数据；远端 SSH 历史有源 **≠** 本机可跑 |
| `trigger_evidence_table_data_pending` | 没有放量突破的回测 / 触发表，量比与突破幅度**没有任何标定依据** |
| `http_minute_listing_auth_pending` | 取分钟数据的 HTTP 入口 **401**，凭证未交；本仓不写、也不该写任何密码或 token |
| `threshold_lock_data_pending` | 量比 / 突破幅度 / 窗口分钟数均未锁定，**一个数值都没钉** |

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
