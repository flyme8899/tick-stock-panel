"""离线报告写出：特征表 Parquet + 摘要 JSON + 元数据（批次 20261025hfeat）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 写到哪里

**只写调用方显式给定的目录**（`out_dir` 必填，没有默认值）。

没有默认值是有意的：这个模块一旦有 `out_dir="data/..."` 这样的默认，就会有人
在日管道或脚本里不带参数地调它，于是回测产物落进生产数据目录，跟真实分区混在
一起。那时候"哪份文件是回测、哪份是生产"只能靠文件名猜。

也**不调** `partition_writer`：本包不往生产分区写一个字节。

## 三份产物

| 文件 | 内容 |
|---|---|
| `features.parquet` | 特征表（主产物，`astype` 不做任何隐式转换） |
| `summary.json` | 指标摘要 + 元数据（`dates` / `inputs` / `version` / `caliber_note` / 脚注） |
| `features.csv` | 可选；`write_csv=True` 时写同内容 CSV，方便人眼扫 |

摘要与元数据写**同一个** JSON：拆成两个文件的结果是总有一个被忘在外发包里，
而缺了 `version` 的指标摘要等于没有口径。

## 元数据里必须有什么（少一项这份报告就不可复现）

- `dates`：实际参与计算的交易日清单
- `inputs`：读到的输入文件清单（缺日/跳过的原因另列 `skipped`）
- `version`：`design_pin_v1+eps_pct:0.30` —— **从 `constants.design_version()` 取**，
  不在这里拼字符串
- `epsilon_value_pct`：0.30
- `caliber_note`：同尺判定的结论（逐日 ratio 的最坏偏离 / 混尺日数）
- `virtual_lot_assumption`：虚拟底仓的假设说明（用了虚拟底仓就必须写）
- `disclaimer`：合规脚注

## 本批次（20261025gtrounds ⑥）新增的四条口径说明

1. `path_a_definition` —— 路径 A = B + `require_prior_cross` + 一根延迟。A **不是**
   与 B 平行的第二个信号；A 与 B 的盈亏差是**敏感度开关**，不是信号优劣的结论。
   不写这条，读者会把"A 比 B 少赚"当成"这个信号不行"，而真实原因只是样本少。
2. `expectancy_definition` —— 期望值 = (Σ completed 净价差 + Σ 盯市) / 实际开出轮次，
   分母含 `limit_locked`、不含 `skipped_quota`。与 `realized_avg_net_spread`
   （只数赢的那部分）必须分清：两个分母不同的平均数混用会得出相反结论。
3. `limit_locked` —— 涨跌停锁死平不掉的轮次**留在分母**，不算数据问题。
   数据来源本批次没有，显式写 `limit_lock_source: data_pending`，**不猜**。
4. `cost_bp_tier` / `cost_sensitivity` —— **两套口径并存，且必须分得清**：
   - 主摘要（`groups` / `expectancy`）仍走 `design_pending` + `0.0`，即**不扣成本**。
     这是对照基准，本拍（5c）**没有**改它 —— 改了等于把主口径偷偷换成扣成本口径，
     读旧报告的人不会知道。
   - `cost_sensitivity` 段是**同一批轮次**在五档成本假设下的重放（批次
     20261025gcost5c）：`0bp`/`5bp`/`10bp`/`15bp`/`20bp`，每档经 `complete_round`
     真扣。`expectancy_gross` = 主口径（不扣成本），`expectancy_net` = 扣该档后。
   两套口径同在一份报告里，正是为了让读者一眼看出「成本假设换一个，期望值差多少」，
   而不是拿一个扣成本数去和旧报告的毛数比。
   - `cost_gate_scope` 明写这条边界，并明写**仍然禁止**「信号可做 / ε 最优 / 可交易」。
   - 精确**拆分费率表**（佣金/印花税/滑点各占多少，G1）仍是 `design_pending`：
     本拍给的是「如果双边成本是 X」的敏感性曲线，不是「真实成本是 X」的断言。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

import polars as pl

from .. import constants as C
from ..disclaimer import DISCLAIMER_TEXT
from .backtest import (
    EXPECTANCY_DEFINITION,
    PATH_A_DEFINITION,
    VIRTUAL_LOT_ASSUMPTION,
    BacktestSummary,
)
from .loader import CaliberReport
from .costs import FOOTNOTE_RETAIL_ASSUMPTION
from .sensitivity import build_sensitivity_payload

FEATURES_PARQUET: Final = "features.parquet"
FEATURES_CSV: Final = "features.csv"
SUMMARY_JSON: Final = "summary.json"

REPORT_SCHEMA_VERSION: Final = "1"
"""报告格式版本（字符串，理由同 g 侧 `REGISTRY_SCHEMA_VERSION`）。

格式将来变了要升这个号，让旧报告能被识别 —— 否则读报告的人会拿两套字段名去对齐。
"""


@dataclass(frozen=True, slots=True)
class OfflineReport:
    """一次写出的产物路径与其中的元数据（便于调用方在日志/PR 里引用）。"""

    out_dir: Path
    features_path: Path
    summary_path: Path
    csv_path: Path | None
    meta: dict[str, Any]


def _json_safe(value: Any) -> Any:
    """把 date / Path / NaN 转成 JSON 能吃的形状。

    `NaN` 必须转 `None`，不能留 `NaN` —— `json.dumps` 默认会写出字面量 `NaN`，
    那不是合法 JSON，下游 `json.loads` 在严格模式下会拒。
    """
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return None if value != value else value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def build_report_meta(
    *,
    summary: BacktestSummary,
    caliber: CaliberReport | None,
    inputs: tuple[str, ...],
    skipped: tuple[str, ...] = (),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组报告元数据。**唯一**的元数据组装处，报告与测试都走这里。"""
    meta: dict[str, Any] = {
        "namespace": C.NAMESPACE,
        "package": "t_rounds.offline",
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "version": summary.version,
        "expected_version": C.design_version(),
        "epsilon_value_pct": summary.epsilon_pct,
        "epsilon_definition": C.EPSILON_DEFINITION,
        "research_window_start": C.RESEARCH_WINDOW_START,
        "minute_export_earliest": C.MINUTE_EXPORT_EARLIEST,
        "auction_impersonation_forbidden": C.AUCTION_IMPERSONATION_FORBIDDEN,
        "dates": [d.isoformat() for d in summary.dates],
        "inputs": list(inputs),
        "skipped": list(skipped),
        "caliber_note": (
            caliber.note() if caliber is not None else "未提供同尺判定; 本报告口径未确认"
        ),
        "caliber_days": (
            [
                {
                    "symbol": d.symbol,
                    "trade_date": d.trade_date.isoformat(),
                    "ratio": d.ratio,
                    "bars": d.bars,
                    "caliber": d.caliber,
                }
                for d in caliber.days
            ]
            if caliber is not None
            else []
        ),
        "vwap_same_basis_note": C.VWAP_SAME_BASIS_NOTE,
        "virtual_lot_assumption": VIRTUAL_LOT_ASSUMPTION,
        # ---- 本批次 ⑥: 口径说明必须进 JSON, 不能只躺在 docstring 里 ----
        "path_a_definition": PATH_A_DEFINITION,
        "require_prior_cross": summary.require_prior_cross,
        "expectancy_definition": EXPECTANCY_DEFINITION,
        "expectancy_is_the_only_strategy_level_number": True,
        "limit_locked_policy": (
            "涨跌停锁死平不掉的轮次留在期望值分母里, 不计盯市, 也不算数据问题"
            "(那是流动性问题, 数据本身是完整的)"
        ),
        "limit_lock_source": C.LIMIT_LOCK_SOURCE_PENDING,
        "cost_gate_scope": (
            "5b 契约: cost_bp_tier 白名单 = design_pending ∪ {0bp,5bp,10bp,15bp,20bp}, "
            "非零档位必须配 cost_amount>0 (禁 silent 0)。"
            "5c 已接档: cost_sensitivity 段为同一批轮次在 0bp/5bp/10bp/15bp/20bp 五档下"
            "的重放, 每档经 complete_round 真扣; 名义口径 = "
            "((leg1_price + leg2_price) / 2.0) * qty, cost_amount = (cost_rate_pct/100)*notional。"
            "主摘要 groups/expectancy 仍为 design_pending + 0 = 未扣任何成本的对照口径, "
            "与 cost_sensitivity 的 expectancy_net 不可混用。"
            "成本只对 completed 轮扣减(有已实现价差); 未回摆轮的盯市盈亏 mtm_pnl 不扣成本。"
            "精确拆分费率表(佣金/印花税/滑点, G1)仍为 design_pending: "
            "本表是「若双边成本为 X」的敏感性曲线, 不是「真实成本为 X」的断言; "
            + FOOTNOTE_RETAIL_ASSUMPTION
            + "。仍禁止据此宣称信号可做 / ε 最优 / 可交易。"
        ),
        "cost_sensitivity": build_sensitivity_payload(summary),
        "not_claimed_verified": (
            "本报告仅为离线同尺特征回测; ε 门禁、可卖老股实盘、分档成本、正式推送闸均未测通; "
            "涨跌停(limit_locked)缺数据来源, 涨跌停锁死的轮次尚未识别; "
            "修完路径前视之后 A/B 的盈亏差仍不能当成信号优劣的结论"
        ),
        "disclaimer": DISCLAIMER_TEXT,
        "footnote": (
            "仅供研究, 不构成投资建议; 执行须人工确认。"
            "所有轮次建在纸面虚拟底仓上, 未建模真实持仓/冻结/T+1 可用量; "
            "「已实现」一栏只含两腿都成交的轮次, 不能当策略成绩; "
            "谈策略本身请看 expectancy(已含未回摆轮的盯市盈亏)。"
        ),
    }
    if extra:
        meta.update(extra)
    return _json_safe(meta)


def write_offline_report(
    *,
    out_dir: Path | str,
    feature_frame: pl.DataFrame,
    summary: BacktestSummary,
    caliber: CaliberReport | None = None,
    inputs: tuple[str, ...] = (),
    skipped: tuple[str, ...] = (),
    write_csv: bool = False,
    extra_meta: dict[str, Any] | None = None,
) -> OfflineReport:
    """把特征表与摘要写到 `out_dir`。**调用方必须显式给目录。**

    参数:
      out_dir: 输出目录。不存在则创建（只创建这一层，不猜父路径）。
      feature_frame: 特征表。
      summary: `run_mode_a_backtest()` 的产物。
      caliber: 同尺判定报告，写进 `caliber_note` 与逐日明细。
      inputs / skipped: 读到的与跳过的输入清单。
      write_csv: 是否额外写一份同内容 CSV。
      extra_meta: 追加到元数据的字段（会被 `_json_safe` 处理）。

    返回 `OfflineReport`（含三份产物的绝对路径与元数据）。

    **不做的事**：不写生产分区、不调 `partition_writer`、不推送、不改任何 env。
    """
    if not str(out_dir):
        raise ValueError("out_dir 必填: 本模块不提供默认输出目录(避免回测产物落到生产数据目录)")
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    features_path = target / FEATURES_PARQUET
    feature_frame.write_parquet(features_path)

    csv_path: Path | None = None
    if write_csv:
        csv_path = target / FEATURES_CSV
        feature_frame.write_csv(csv_path)

    payload = dict(summary.as_dict())
    payload.update(
        build_report_meta(
            summary=summary,
            caliber=caliber,
            inputs=inputs,
            skipped=skipped,
            extra=extra_meta,
        )
    )
    summary_path = target / SUMMARY_JSON
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    return OfflineReport(
        out_dir=target,
        features_path=features_path,
        summary_path=summary_path,
        csv_path=csv_path,
        meta=payload,
    )


__all__ = [
    "FEATURES_CSV",
    "FEATURES_PARQUET",
    "REPORT_SCHEMA_VERSION",
    "SUMMARY_JSON",
    "OfflineReport",
    "build_report_meta",
    "write_offline_report",
]
