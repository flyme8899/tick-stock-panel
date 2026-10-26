# 回测策略选择器 · AI 自助生成 + 叠加多选现有策略

> 批次 `20261026gbtstrat` · 基线 tip `9d48969fc9d91a9e2c8eb4ba91165b3790800536`（含 #224，已合**未部署**）
>
> **仅供研究，不构成投资建议；执行须人工确认。** 图表红涨绿跌。

本文钉死两件事的口径：**① 回测页内 AI 自助创建入口的边界**、**② 「叠加」多选现有策略跑当次回测的请求面**。
两件事都在回测页闭环，不需要跳选股页。

---

## 1. 为什么改

改之前，回测页「选择策略」面板有两个问题，用户口径与实现相反：

| 位置 | 改前行为 | 问题 |
|---|---|---|
| **AI 分组空态** | 只有一行「当前分组暂无策略」 | 想用 AI 生成的策略，必须先去选股页 → 策略池 → 创建 → 保存 → 发布，再回到回测页。跨页往返，且回测页自己没有任何入口 |
| **「叠加」tab** | 列表 = `source === 'composite'`，即**只列已存的 `composite_*`** | 想叠两个内置策略，得先建一个 `composite_` 存成文件再回来选。tab 名叫「叠加」，实际是「已存叠加策略清单」 |

用户原话：

> 「ai生成 让用户自己生成 不要你生成完的」
> 「叠加就把现有策略放上可多选就行」
> 「不要去选股页面跨页面处理」

所以本拍只做两件事，不做第三件（不预置样本 AI 策略，不代用户生成）。

---

## 2. AI 自助创建入口

### 2.1 入口与闭环

- **位置**：回测「选择策略」→ **AI 分组**。入口按钮 `创建策略 · AI` **始终可见**，不只在空态 —— 空态时它是唯一出路，非空态时它是追加手段。
- **弹层**：复用 `StrategyBuilderDialog`（`mode="create"`），挂在回测页内，不路由到 `/screener`。
- **链路**：`POST /api/strategies/build`（或 `/build/stream`）→ `code/save` → 必要时 `/{id}/publish`。这条链路由 Builder 本体负责，回测页只负责挂载与收尾。
- **收尾**（`handleBuilderSaved`）：
  1. `staleTime: 0` 强制刷新策略列表（不能等缓存过期，否则新策略不出现在 AI 分组）；
  2. 切到 AI 分组 + 选中新策略，用户可直接点运行；
  3. 若 save 仍为 `research_only` 草稿 → **在回测页就地调 `api.strategyPublish(id)`**。发布失败会 toast 报错并保留草稿，不假装成功。

第 3 点是硬要求：否则用户为了回测一个刚生成好的 AI 策略，还得跑去选股页策略池点发布 —— 那就是"逼用户跨页"。

### 2.2 不做的事

- **不预置样本 AI 策略**（包括历史遗留的 `ai_chaodie_fantan` 类）。仓内由测例 `test_no_sample_ai_strategy_is_added` 守着。
- **不代用户生成**：`build` 的 prompt 由用户在 Builder 里输入，回测页不代填。
- **AI 未配置时不假装可用**：沿用 Builder 既有的 `strategyAiStatus` 与错误文案（设置页同轨），不吞错。

---

## 3. 「叠加」多选现有策略

### 3.1 列表语义（已翻转）

```ts
// 现在的实现（StrategyBacktest.tsx · filteredStrategyList）
strategyGroup === 'composite'
  ? strategyList.filter(isOverlayCandidate)      // builtin / custom / ai
  : strategyGroup === 'all'
    ? strategyList
    : strategyList.filter(st => st.source === strategyGroup)
```

`isOverlayCandidate` 排除三处判据（任一命中即排除）：
`source === 'composite'` / `execution_backend === 'composite'` / `id` 以 `composite_` 开头。

**「叠加」tab 绝不能再写成 `source === strategyGroup`** —— 那会让列表退回成"只列 `composite_`"，与用户口径相反。

### 3.2 禁伪空

只要有非 composite 策略（通常内置就有一批），「叠加」tab **必须**渲染候选项，不允许只显示「当前分组暂无策略」。只有在策略池真的只剩 composite 时，才显示「策略池为空，先创建或载入策略后再叠加」。

### 3.3 多选与合并模式

- 芯片 toggle 多选，已选项打勾并高亮。
- 合并模式：**交集 `intersect`（默认）** / 并集 `union`。默认值与 `CompositeStrategyDialog` 的默认、后端 `DEFAULT_MERGE_MODE` **三处同值**，避免"界面看着是交集、跑出来是并集"。
- 权重：当次多选走**均等权重**（`weight: 1`）。权重不在前端归一 —— 归一是合并器的事。精细滑块是可选优化，未做。
- **少于 2 个不允许运行**：按钮禁用并给出原因；后端同样 400。

---

## 4. N1 · ephemeral overlay 请求面（钉死）

### 4.1 字段

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `overlay_children` | `list[{strategy_id: str, weight: float}]` | `null` | 非空即走叠加路径；**至少 2 个**，1 个拒绝并提示改单选 |
| `merge_mode` | `"union" \| "intersect"` | `"intersect"` | 合并模式；与 `CompositeStrategyDialog` 同默认 |
| `min_confirm` | `int` | `0` | intersect 下最少命中数；`0` = 全部子策略（与 `merge_signal_matrices` 的既有语义一致） |

- **`strategy_id` 占位**：走叠加路径时，`strategy_id` 一律被替换为 **`__ephemeral_overlay__`**（`StrategyBacktestConfig.__post_init__` 里钉死）。请求里传什么都不作数 —— 否则前端传的 id 与服务端合成的 id 会形成两个真源，日志里分不清哪个是真的。
- SSE 与 cancel 走 query string，`overlay_children` 是 JSON 字符串（与 `params` / `overrides` 同风格）。
- **job key 包含这三个字段**：不同的叠加组合不得命中同一个 job，否则用户会看到别人的结果。

### 4.2 不落盘

服务端在**内存里**合成一个 `execution_backend="composite"` 的 `StrategyDef`，注册进"将要执行这次回测的那个引擎"，跑完随进程消失。

合成位置必须在 `StrategyBacktestService.run()` 内、而不是 API 侧：真正跑回测的是 worker **子进程**，它按 `_strategy_dirs` 重新构建引擎并从磁盘加载。在 API 侧合成的话，子进程里 `get()` 会直接 `unknown strategy`。

测例 `test_overlay_backtest_runs_without_saving_composite` 断言跑完后 `data/strategies/composite/` 下仍为空；`test_overlay_module_never_writes_files` 扫模块源码确认没有任何写盘调用。

### 4.3 复用合并语义，不改选股口径

合成后的定义走与落盘 composite **完全相同**的路径：

```
_resolve_composite_feature_plan  →  _generate_composite_signal_matrix
                                 →  app.strategy.composite.merge_signal_matrices
```

- **不强制先 `POST /composite/save`**：仓里一个 `composite_*` 都没有也能跑（测例已覆盖）。
- **选股侧 `merge_results` 一字未改**：`test_scored_merge_results_untouched` 守着。本拍只往 `composite.py` 里新增了 `MERGE_MODES` / `DEFAULT_MERGE_MODE` / `normalize_merge_mode` 三个共享常量与函数（合并模式本来就该有一个真源）。
- **落盘 composite 的单选路径口径不变**：它的 `merge_mode` 仍从 `params` 读，默认 `union`；只有 ephemeral overlay 才从请求字段读。这两条分别有测例钉住。

### 4.4 禁嵌套

任一子策略满足 `source == 'composite'` / `execution_backend == 'composite'` / `id` 前缀 `composite_` → 拒绝。前端列表过滤是第一道，后端仍拒是第二道（fail-closed，不依赖前端）。

---

## 5. 结果归因

叠加回测的 `strategy_info` 带：

```json
{
  "source": "composite",
  "execution_backend": "composite",
  "composite_children": [{"id": "boll_breakout", "weight": 1.0}, {"id": "macd_golden", "weight": 1.0}],
  "ephemeral_overlay": true,
  "merge_mode": "intersect",
  "min_confirm": 0
}
```

`ephemeral_overlay` / `merge_mode` / `min_confirm` **只在 ephemeral 时出现**。已存 `composite_*` 的结果里没有这三个键 —— 两者形态相近，但绝不能混为一谈。前端据此在归因行打「当次（未落盘）」标记。

---

## 6. 已知限制（如实记录）

1. **默认 `intersect` 在信号稀疏时跑不出交易。** 交集要求所有子策略同一天命中；两个弱相关策略可能迟迟无交集，回测会返回「在指定区间内未产生买入信号」。这是**正确行为，不是 bug** —— 换成并集兜底才是改口径。测例 `test_intersect_without_common_hits_reports_no_signal_not_fake_trades` 钉住了这一点。
2. **权重只能均等。** 精细权重滑块未做（任务书标为可选优化）。
3. **「另存为叠加策略」未做。** 可选优化，不影响当次回测。
4. **叠加不带单个策略的 params / overrides。** 子策略各自的用户 override 由服务端按 `override_loader` 加载；把当前选中策略的配置塞进请求会串到某一个子策略上，所以显式不带。

---

## 7. 零碰

| 项 | 状态 |
|---|---|
| 生产 `.env` 三闸（`BUY_SELL_SIGNALS_PERSIST` / `PUSH` / `DINGTALK`） | 未改 |
| #224 竞价 09:25 stub 行为 | 未改 |
| #225 gdsaledger（`ledger_audit` / `quant_evidence`） | 未改，未把工作树切到其 head |
| OpenBB / DSA-upgrade / Kronos / marketplace | 未碰 |
| Opt-A / DecisionWorkspace IA / MCP 下单 / 计划表 17–19 / 飞书 | 未塞 |
| 部署 | **默认不部署**；合入另等盘后口令 |

---

**仅供研究，不构成投资建议；执行须人工确认。**
