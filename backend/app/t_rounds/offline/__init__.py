"""h 特征回测 · 离线同尺评估包（批次 20261025hfeat）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 这个包是什么

`t_rounds` 的**离线**子包。它把「已经核过的分钟数据」变成「可复现的特征表 +
模式 A 纸面回测摘要」，仅此而已。

它**不是**生产链路的一部分：

- 没有 HTTP 路由，没有 lifespan 接线，没有日管道定时任务
- 不落生产分区，不调 `partition_writer`
- 不推送（没有 SSE / 钉钉 / 飞书），`formal_push_allowed` 恒 False
- 与 `app.buy_sell_signals` **零符号关系**（不 import、不写它的 Store、不碰它的 env）

## 为什么单独一个子包，而不是塞进 `t_rounds/*.py`

`t_rounds` 的生产模块必须保持「没有 IO、没有评估、没有回测」—— 它们只定义记录与
不变量。一旦把「读 CSV、跑回测、写 Parquet」混进去，那些模块就有了副作用，而
副作用是没法用单元测试证明"不会在生产路径上被调用"的。

所以离线部分整体下移到 `offline/`，由 `tests/test_t_rounds_offline_isolation.py`
静态守着它对生产模块的依赖方向（只能调公共纯函数，不能反向被 import）。

## 同尺是这一批的全部前提

`MINUTE_CALIBER_CHECK_20261024.md` 的结论：分钟导出里 `close` 可能是前复权、`amount`
仍是原始成交额。两者混着算 `amount / (volume * 100)` 得到的 VWAP 不是那天的真实
均价，而**从结果上看不出来**——它只是让 ε 带的位置偏了几个 bp。

所以本包的 `loader` 把「对齐」做成**显式的一步**，并且：

1. 单尺输入（`.raw_basis` 或单口径导出）直接放行；
2. 混尺输入必须先给定对齐引子（`align_hint`），否则**抛错**，绝不猜；
3. `sanitize_minute_amount` 只能在对齐**之后**调用 —— 它在混尺时会用
   `close × volume × 100` 把不一致的额改掉，正好把该报的错掩盖成"数据是干净的"。

## 六条软提醒（来自 MINUTE_CALIBER_CHECK + REVIEW，本包逐条落实）

| # | 提醒 | 本包怎么落实 |
|---|---|---|
| 1 | 同尺后再 VWAP | `loader.align_minute_frame()` 是进特征的唯一入口；报告写 `caliber_note` |
| 2 | 近端 ratio≈1 不能推断历史日可用 | `loader.assess_caliber()` 逐日给 ratio，报告按日列；不做全局 `ratio≈1` 的短路 |
| 3 | `sanitize_minute_amount` 会掩盖混尺 | 只在 `caliber == "single"` 之后调用（`features.build_offline_feature_frame` 里） |
| 4 | 虚拟底仓须标注 | 回测摘要强制 `virtual_lot_assumption`；记录 `virtual_position_flag=True` |
| 5 | 默认不自动部署 | 本包纯离线；合入不等于部署 |
| 6 | 竞价 / 估值健康 / 白名单锁定 / 计划表 17–19 暂缓 | 零实现；`AUCTION_IMPERSONATION_FORBIDDEN` 有测试守着 |

## 交付形态

```
offline/
  __init__.py     导出公开 API
  loader.py       同尺加载: 判口径 → 对齐 → (可选) sanitize
  features.py     调 build_feature_frame + 路径 A 边沿 + 路径 B 进带标记
  gates.py        路径 A ∥ B 分列; ε 主钉 + 敏感性分列
  backtest.py     模式 A 正/倒 T 纸面配对摘要(内存 Store + 状态机)
  report.py       写特征表 Parquet + 摘要 JSON + 元数据
```

输出：特征表 **Parquet**、指标摘要 **JSON**（含 `dates` / `inputs` /
`version=design_pin_v1+eps_pct:0.30` / `caliber_note` / 脚注）。

## 明确不做

全量 walk-forward 定稿 · ε 升版重钉 · 分档成本实测 · Regime 产品标定 ·
正式推送闸 · 竞价/放量事件 · 估值/健康实装 · 白名单锁定 · 计划表 17–19 ·
MCP 交易 · 任何生产 env 改动。
"""
from __future__ import annotations

from .backtest import BacktestSummary, run_mode_a_backtest
from .features import (
    OfflineFeatureResult,
    assert_version_matches_pin,
    build_offline_feature_frame,
)
from .gates import (
    EPSILON_PCT_MAIN,
    GATE_PATH_A,
    GATE_PATH_B,
    SENSITIVITY_EPSILON_PCT,
    epsilon_fraction,
    sensitivity_columns,
)
from .loader import (
    CALIBER_MIXED,
    CALIBER_SINGLE,
    CaliberReport,
    LoadedMinutes,
    align_minute_frame,
    assert_no_market_open_impersonation,
    assess_caliber,
    load_minute_export,
    shift_within_day,
)
from .report import OfflineReport, write_offline_report

__all__ = [
    "CALIBER_MIXED",
    "CALIBER_SINGLE",
    "EPSILON_PCT_MAIN",
    "GATE_PATH_A",
    "GATE_PATH_B",
    "SENSITIVITY_EPSILON_PCT",
    "BacktestSummary",
    "CaliberReport",
    "LoadedMinutes",
    "OfflineFeatureResult",
    "OfflineReport",
    "align_minute_frame",
    "assert_no_market_open_impersonation",
    "assert_version_matches_pin",
    "assess_caliber",
    "build_offline_feature_frame",
    "epsilon_fraction",
    "load_minute_export",
    "run_mode_a_backtest",
    "sensitivity_columns",
    "shift_within_day",
    "write_offline_report",
]
