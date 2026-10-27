# DSA 证据账本审计口径（P-ledger + P-qe）

> 仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。

本文写清 DSA 上游「纸面战绩」在 TSP 侧怎么被审计、哪些地方现在还不能信、
以及 `quant_evidence` 的时效门禁。**任何展示面在审计未过之前，都必须带
「未经 TSP 审计」徽章。**

## 1. 真源：用上游的表，不另起

| 真源 | 位置 | 装什么 |
|---|---|---|
| `analysis_history` | `vendor/daily_stock_analysis/src/storage.py`（`AnalysisHistory`） | 建议快照：`operation_advice` / `ideal_buy` / `stop_loss` / `take_profit` / `created_at` / `query_id` |
| `backtest_results` | 同文件（`BacktestResult`） | 上游结算：`start_price` / `end_close` / `outcome` / `hit_stop_loss` / `hit_take_profit` / `simulated_*` |

**不另起 append-only 旁路表。** 这一条不是洁癖：一旦再开一张表，两边就会分叉，
到时候「到底哪个才是真账本」会变成一个没人能回答的问题。审计层只读，
读写权仍在 DSA 手里。

上游已有只读端点 `GET /backtest/results`（`api/v1/endpoints/backtest.py`），
TSP 经既有 proxy 消费（`ledger_audit.fetch_ledger_items`）。

> 注意区分 `/backtest/results`（读）与 `/backtest/run`（写库）。
> 审计路径只打前者，永远不会因为「看一眼账本」而触发一次回测。

## 2. 三道审计（A1–A7）

### A1 入场价偏乐观

上游 `backtest_service.py:L187`：

```python
start_price=float(daily_window.start_bar.close)
```

**收盘入场。** 含义是：信号日收盘那一刻，你已经知道了当天的全部走势，
再按收盘价买进去——真实交易做不到。这条路径**系统性漏掉隔夜跳空**：
利好出来第二天直接高开 5%，模型按昨天的收盘价买入，白捡 5%。

TSP 侧并列 `entry_close`（上游原值）与 `entry_next_open`（次日开盘）。
两列都留着，谁也不覆盖谁，并且显式标注口径：

- 有次日开盘：`close 与 next_open 并列`
- 缺次日开盘：`close（上游原值·漏隔夜跳空）`

缺就是缺，不许拿 close 顶上去凑一个「次日开盘」。

### A2 一字涨停 / 不可成交不进分母

上游 service / engine **没有**涨停剔除逻辑。

一字涨停（开=高=低=收，且相对昨收达到涨停）当天既买不到也卖不出。把它算进
胜率分母，相当于给「买不到的赢」记分——统计上叫幸存者偏差，实战里叫「纸上
富贵」。

TSP 侧标记 `unfillable="limit_up_one_word"` 并**排除出分母**，但仍在
`unfillable_count` 里计数可见。**剔除是可见的，不是悄悄消失的。**

判定采取保守策略：缺昨收、缺任一价格、昨收非正 → 判不出 → **不剔除**。
宁可留着一条可能不可成交的样本，也不凭猜把样本丢掉。

涨停但当日换过手（开≠低）→ 能买得到 → **不剔除**。

### A3 窗口 / 中性带 / ambiguous

tip 锚定值（本拍不改上游默认，改须显式）：

| 参数 | 值 |
|---|---|
| `engine_version` | `v1` |
| `eval_window_days` | 10（service 默认；UI 传 5） |
| `neutral_band_pct` | 2.0% |

同根 K 线同时触及止盈与止损时，上游记 `first_hit="ambiguous"` 并**先按止损**
处理。这是保守选择，但会让结果偏悲观——写在这里，免得日后对不上账时找不到原因。

### A4 同期机械基线

机械基线来自 `quant_evidence.yaml`（TSP 24 策略全市场回测），与 LLM 纸面战绩
**不合并分母**。

这两件事量的根本不是一回事：

- LLM 纸面：`analysis_history` 里的单条建议，被 DSA 用它的引擎结算。
- 机械基线：TSP 打分选股，无差别执行，等权持有。

把它们加在一起算一个「总胜率」，得到的是一个既不代表策略也不代表模型的数字。
样本不足时只给方向，不给百分比。

### A5 lookahead 截断登记

证据重放路径登记 `run_truncated_replay` / `assert_truncated`
（`backend/app/backtest/lookahead_fixture/`），截断失效 fail-closed。

参考 #217 / #223 的 `test_t10` / `test_t10b`。本拍**只登记，不改夹具行为**，
也不重做 M02。

### A6 来源打标

| 来源 | 触发点 | 特点 |
|---|---|---|
| `analysis_bridge` | `wrap_history_save` 自动 | 分析落库即登记，无需人工动作 |
| `manual_register` | `sniper_alerts.register` 手动 | **整段替换该股线并清 fired** |

手动登记会覆盖，导致线史不可回看。这是已知约束：**展示层说明即可，不另起表**。
覆盖本身不是 bug，是那个接口的语义——但用户有权知道自己在看的是最后一版还是全部。

### A7 未审计标

`ledger_audit.TSP_AUDIT_BADGE = "未经 TSP 审计"`，`audit_status` 恒 `unaudited`。

`audit_status_for()` 刻意**不接受** env / 参数翻转：审计状态要写在审计记录里，
不能靠一个环境变量假装已过审。前面所有 A 项都是「口径已经说清」，不是
「账本已经可信」。这两件事之间还差一次真正的审计。

## 3. P-qe 时效门禁

证据有保质期。`quant_evidence.yaml` 的 `_meta` 现在带：

| 字段 | 作用 |
|---|---|
| `as_of` | 证据窗锚定日（= `period` 右端） |
| `expires_on` | 显式过期日；设了就优先于天数规则 |
| `env_label` | 证据窗行情环境（当前：弱势下行，上证同期 -4.00%） |
| `l5_entry_exit_status` | L5 表述定性状态（当前 `pending`） |

三道闸，任一不过就**不注入**：

1. **时效**：`as_of` 距今 > 90 日历日，或过了 `expires_on` → `expired=True`。
   判不出时效（缺 `as_of` / `period` / `updated`）也按过期处理——判不出来不等于新鲜。
2. **样本门槛**：`sample_sizes` **任一项** < 100 → 该条不注入。
   不是取平均，也不是取最大。有三项、其中一项 94 笔，说明这一项撑不住结论。
3. **环境标签**：缺 `env_label` 时 summary 标 `env_label_missing`。
   没有环境标签的证据，不能当「全环境适用」用。

### 3.1 出厂证据当前的状态（重要）

按 100 笔门槛，`quant_evidence.yaml` 的 6 条证据**全部不过闸**：

| 策略 | 最小匹配样本 |
|---|---|
| `ma_golden_cross` | 59 |
| `shrink_pullback` | 94 |
| `volume_breakout` | 77 |
| `bull_trend` | 77 |
| `dragon_head` | 55 |
| `bottom_volume` | 71 |

结果是：**技能提示里不再追加量化回测参考。** 这不是 bug，是门槛生效后的
应有结果——曾经能注入，是因为当时没有门槛。

补样本量或补回测是**另一拍**的事。在那之前，宁可让技能提示少一段，
也不要把 59 笔得出的结论当「这类形态负期望」这种量级判断的依据。

`dsa_bootstrap` 的钩子装上后，未注入的会打 `量化证据未追加到 <skill>：过期或
样本不足` 日志，避免「没注入」和「本来就没这条证据」在日志里长得一样。

### 3.2 bootstrap 过期显式跳过

`dsa_bootstrap` 保持「失败不挡启动」不变。过期或样本不足时**显式**不追加并记日志，
不追加空段落，不改技能原文。

## 4. L5 表述歧义（未定性）

`quant_evidence.yaml` 文首写「次日开盘建仓 / 信号日收盘清仓」。这句话抄自
TSP 内置回测报告，**尚未**逐句比对上游 `backtest_service` / `backtest_engine`
的实现原文。

`_meta.l5_entry_exit_status: "pending"` 记录该状态。定性之前，引用这个口径
必须显式标注「未定性·勿引用」。

## 5. P-diag 门禁读出（2026-10-26 采集）

> 来源：`PDIAG_20261026gdsaledger.md` · sha256 `269c507d9619a11ea517100c2f26ad663bbaa05cc70809aac6b1f40d1c341866`
> 采集时间 2026-10-26 ~17:02–17:04（Asia/Shanghai）

| 项 | 读出 |
|---|---|
| 浏览器 L2 通/断累计 | **data_pending**（本机读不到所有者 `localStorage`） |
| 服务端 reachable | **true**（app 内 proxy `health() → reachable=true`） |
| DSA health | **ok**（HTTP 200；容器 Up healthy） |
| `analysis_history` 近 7 日 | **66** |
| `analysis_history` 近 14 日 | **144** |
| `analysis_history` 总条数（旁证） | 151 |
| `backtest_results` 总条数（旁证） | 13（全部 `completed`） |
| L2「断」是否占多数 | **否**（按服务端替代证据；浏览器累计不可读，不作「断≫通」主张） |
| 账本是否因 P-diag defer | **不必 defer** |

诚实边界：浏览器端的通断累计**没有读到**，这一项仍是 `data_pending`。上面的结论建立在
服务端替代证据上（proxy 可达 + 近 14 日有 144 条写入），不是建立在浏览器数字上。

日后若所有者浏览器显示「断≫通」而服务端仍通，应按「设备 localStorage 单端」解释，
**不自动推翻本结**。

## 6. 与其它批次的关系

| 上游 | 关系 |
|---|---|
| #224 gauction2 竞价窄开 | 零碰行为，不 import `buy_sell_signals` |
| #223 M02 lookahead 补测 | 零碰夹具行为，只登记验收 |
| #221 DSA 升级 SOP | 零碰 |
| #220 OpenBB | 零碰 |
| #219 Token | 不回开 `paper:trade` |
| #211 / #214 漏斗 / 狙击 | 不回退 |

合入 ≠ 部署；合入 ≠ 改三 env；合入 ≠ 宣称审计已过。

## 7. 下一步（另拍）

| 缺口 | 清掉条件 |
|---|---|
| A1 完整并列 | 补次日开盘数据源，`entry_next_open` 常态有值 |
| A2 完整判定 | 补 `prev_close`，一字涨停判定不再保守放行 |
| A4 机械基线对比列 | 同期同池对齐 + 样本量区间 |
| qe 样本门槛 | 扩样本或重跑回测，让 6 条至少各有一项 ≥ 100 |
| L5 定性 | 逐句比对上游实现原文 |
| 审计状态翻转 | 真正的审计记录（本拍不提供翻转路径） |

---

## 8. 消费侧：研报页「上次建议结果」卡（批次 20261026gdsaux · P-ux）

个股研报页（`Reports`）在「报告全文」与「狙击登记」之间挂了一张只读卡，按**当前股票代码**回看该股上一次 DSA 建议的事后结果。它是账本的**消费方**，不新增任何口径。

| 项 | 说明 |
|---|---|
| 数据源 | 只读 `GET /api/dsa/ledger-audit?code={当前股}&limit=20`（本节 A1–A7 口径原样复用） |
| 前端 query | `fetchDsaLedgerAudit({ code, limit })`；无参调用仍打全量，`LedgerAudit` 审计台不受影响 |
| 「上次」定义 | 该股 `rows` 按 `analysis_history_id` 降序取最新 1 条；同 id 时后出现的算更新；展开合计 ≤3 条 |
| 不可达 | 空态文案带出 `summary.unavailable_reason`，**不**用模拟数据填充 |
| 无结算行 | 空态：「该股暂无账本结算记录。生成研报并完成上游评估后，这里会显示上次建议的事后结果。」 |
| 徽章 | 恒展示「未经 TSP 审计」（与后端 `TSP_AUDIT_BADGE` 同句） |
| 胜率 | 单股卡**不**印胜率百分比 —— 全账本 `win_rate_pct` 不是「该股胜率」 |
| 收益 | 若展示 `simulated_return_pct`，并排注「成本来回 18.2bp」；不动审计引擎 |
| `entry_next_open` | 上游没注入就写「未注入」，前端**不**用收盘价或跳空倒推 |
| 写路径 | 零写：不调 `backtest/run`、不改 register、不写 sqlite |

红涨绿跌（赢红 / 亏绿）。

> 仅供研究，不构成投资建议；执行须人工确认。成本来回 18.2bp（示收益须注）。

---

*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。*

## 9. P-arg-snap：四字段 + snapshot 冻结（批次 20261027gargsnap）

在 A1–A7 **之上并列增量**，不改任何既有分母 / 入场 / 徽章语义。
借 TradingAgents 的**机制**（多空两段论据 + 风控否决 + 决策时点 PIT 冻结），
**零代码搬运** —— 不 import TradingAgents，不引任何多代理编排依赖。

### 9.1 四字段契约

| 字段 | 类型 | 语义 | 空态 |
|---|---|---|---|
| `bull_case` | `string \| null` | 建议生成时的**多头论据**摘要（只读快照） | 无材料 → `null`，UI 写「无多头论据」；**禁 fabricate** |
| `bear_case` | `string \| null` | 建议生成时的**空头论据**摘要（只读快照） | 同上「无空头论据」 |
| `risk_veto` | `{veto: bool, reason: string \| null} \| null` | 风控是否否决该建议；`veto=true` **必须**有非空 reason | 无风控结论 → `null`（**≠** `veto=false`）；缺 reason 的 true → 降 `null` |
| `snapshot_asof` | `string \| null`（ISO-8601 **+08:00**） | 论据所依据的行情快照时点，须结算前冻结 | 未知 → `null`；**禁止**事后回填 |

并列只读字段：

| 字段 | 类型 | 语义 |
|---|---|---|
| `advice_generated_at` | `string \| null` | 建议生成时刻；默认＝`AnalysisHistory.created_at` 规范化后 |
| `snapshot_late` | `bool` | `snapshot_asof` 存在且 **>** `advice_generated_at`（秒级同区）。缺失时为 `false` |
| `in_argument_quality_denominator` | `bool` | 是否计入论据质量分母（见 9.3） |

### 9.2 冻结与 late 判定

| 规则 | 期望 |
|---|---|
| 合法 | `snapshot_asof` **≤** `advice_generated_at` → 可进论据质量统计 |
| 迟到 | `snapshot_asof` **>** `advice_generated_at` → `snapshot_late=true`；**不计入**论据质量 |
| 缺失 | `snapshot_asof=null` → **不计入**论据质量（另计「缺快照」数，但不折进分母） |
| 冻结 | 结算 / `backtest/run` / 人工重评 **不得**改写已冻结的四字段；审计层只读建议时刻侧材料 |

**与 A2 分账**：`snapshot_late` **≠** `unfillable`。迟到样本仍可按 A1–A7 进 / 出**胜率**分母，
**仅**从**论据质量**统计中排除。两套账合并会让"论据迟到"污染胜率，或让"不可成交"污染论据质量。

**N4 时间口径**：全部 ISO-8601 带 `+08:00`，比较走**秒级**同区（截断亚秒，不四舍五入）。
裸 datetime 无时区按 Asia/Shanghai 解释（猜 UTC 会整体偏 8 小时，让本该 late 的快照看起来合法）；
只有日期则按当日 `00:00:00+08:00`。

**N5 `null` ≠ `false`**：

| 上游 | 输出 |
|---|---|
| 无风控材料 | `null` |
| 明确未否决（`trigger=none`） | `{veto: false, reason: null}` |
| 明确否决且有 reason | `{veto: true, reason: "<非空>"}` |
| 否决但缺 reason | `null`（非法结论不输出半个） |
| `trigger=risk_downgrade` | `null`（降级是仓位打折，不是风控放行；输出 `veto:false` 会被读成"审过了没问题"） |

### 9.3 论据质量分母（本拍最小契约）

- **分母候选** = 有结算行 ∧ `snapshot_late≠true` ∧ `snapshot_asof` 非空。
- **本拍不加**「多空至少一段非空」门槛（挂档）。
- **不给**论据质量百分比：`argument_quality_pct` 恒 `null`。门槛仍挂档，样本不足亦不给。
- `summary` 新增键：`argument_quality_denominator` / `argument_quality_excluded_late` /
  `argument_quality_excluded_missing_snapshot` / `argument_quality_suppressed` / `argument_quality_pct`。

### 9.4 只读解析（N1）与禁事后回填（N2）

解析面按优先级：item 顶层 → `raw_result` → `context_snapshot`（只下探一层）。

| 字段 | 候选键 |
|---|---|
| 多空 | `bull_case` / `strongest_bullish_signal` / `bullish_case` / `bull_argument`（bear 同构） |
| `snapshot_asof` | `snapshot_asof` / `data_asof` / `market_data_asof` / `as_of`（须能解析成时间） |

**`evaluated_at` / `analysis_date` / 结算日 / 「今天」全部不在候选键里。** 它们是"事后"的时刻，
拿它们当快照时点等于用结算后行情回填，是 lookahead 伪装成 PIT。历史缺字段就是 `null`，然后不计入。

上游 `BacktestResult` **没有** `created_at` 列；建议生成时刻只存在于 `AnalysisHistory.created_at`。
因此 `_result_to_dict` 做了**最小只读透传**（新增 `created_at` / `context_snapshot` 两个键），
**零结算语义变更**，也不改结算公式。

### 9.5 展示（N3 最小）

`LastAdviceRowView` 下挂一条**可折叠**「论据快照」行，展示多空摘要（过长截断 + title 全文）、
风控结论、`snapshot_asof` / `advice_generated_at`、`snapshot_late` 徽章。
完整「论据质量」大卡 / 独立仪表盘 **挂档**，不挡主验收。

渲染纪律写死三条：`risk_veto === null` 显示「无风控结论」而**不是**「未否决」；
缺失字段显示「未记录」而不补时间；`snapshot_late` 必须有可见标记。

### 9.6 本拍明确不做

不接 TradingAgents 运行时 / 不动 DSA skill / 不进 #228 hits / 不改 A1–A7 分母与徽章语义 /
不另起旁路账本表 / 不为读钩触发 `backtest/run` / 不碰三 env 与 hits ding / 不碰 Opt-A 与 P-risk-sop。

> 仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。

> 本文的「论据质量」是单条建议的证据成色，**不等于**信号层准入；准入四闸（lookahead / 纸面 / 三 env·push_gate / t_rounds KPI）见 [risk-desk-checklist.md](./risk-desk-checklist.md)。
