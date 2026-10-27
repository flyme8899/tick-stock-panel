#!/usr/bin/env python3
"""gtja191 实验组自评与对照（只读，不写数据，不改任何生产配置）。

这个脚本做三件事，顺序固定：

1. **自评**：把 `GTJA191_IDS`（13 个）逐个跑一遍 TSP 因子检验，给出 Rank IC / ICIR /
   换手 / 覆盖 / 天数。
2. **对照**：同一区间、同一截面门槛下，把 `alpha158`（158 个）也跑一遍，给出 gtja191
   相对 alpha158 的分位（|IC| 排名百分位、中位数对比）。**对照不是为了宣布谁赢**，
   而是为了让"这批外部公式在 TSP 上到底什么水平"有一个可比较的锚。
3. **成本口径**：显式打印「IC 表口径」与「成本闸口径」两个来回成本，并说明它们**不相等**。

## 为什么成本要分两个口径

任务书成本锚是**来回 18.2bp**，但 TSP 回测默认配置（`fees_pct=0.0002`,
`slippage_bps=5.0`）算出来的来回成本约 14bp。这两个数**不是一回事**：

- 18.2bp 是本批要遵守的成本锚，用于任何"扣费后还行不行"的判断；
- 14bp 是 `FactorBatchConfig` 的历史默认值，IC 表是**未扣费**的，不受它影响。

脚本不偷偷改默认值来凑 18.2。它把两个数都打出来，并在末尾写清「本报告的 IC 不含
任何成本，18.2bp 只在成本闸里用」。混淆这一点会让 IC 看起来比实际能拿到的收益更好。

## 不编造数字

没有 enriched 分区时直接说明并退出（退出码 2），**任何情况下都不写出 IC 数值**。
区间内有效交易日不足、截面过薄时，也用退出码或显式警告说明，不退化成"有个数就行"。

用法（仓库根目录，与 alpha158 自评同一套虚拟环境）:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_gtja191.py

只跑 gtja191、不跑 alpha158 对照:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_gtja191.py --no-baseline

指定区间与样本过滤:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_gtja191.py \\
        --end 2026-09-30 --exclude-limit --exclude-st --min-listed-days 60

完整来源说明见 `docs/gtja191.md`，公式与 blocked 清单见 `backend/app/factors/gtja191.py`。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

# 复用 alpha158 自评里的数据分区、截面门槛、过滤和打印原语。**不复制实现**：
# 两份实现迟早会分叉，而"两份自评口径不一致"比"没有自评"更难查。
from eval_alpha158 import (
    _data_dir,
    _fmt,
    _partition_dates,
    _polars_gate,
    apply_row_filters,
    beijing_today,
    format_cross_section,
    latest_closed_trading_day,
    restrict_evaluation_dates,
)


def _print_missing(data_dir) -> int:
    enriched = data_dir / "kline_daily_enriched"
    raw = data_dir / "kline_daily"
    print("当前环境没有可评估的 A 股日线数据，下面不给出任何 IC 数值。")
    print(f"数据目录: {data_dir}")
    print(f"kline_daily_enriched 分区数: {len(_partition_dates(enriched))}")
    print(f"kline_daily 分区数: {len(_partition_dates(raw))}")
    print("因子检验读的是 kline_daily_enriched。分区里有 part.parquet 之后，在仓库根目录执行:")
    print("  PYTHONPATH=backend backend/.venv/bin/python scripts/eval_gtja191.py")
    return 2


def _print_cost_caliber() -> None:
    """把两个成本口径摆出来。这里不选边，只要求读者知道它们不同。"""
    from app.factors.gtja191 import (
        GTJA191_COST_BP_ROUNDTRIP,
        cost_gate_cost_bp,
        ic_table_cost_bp,
    )

    print()
    print("成本口径（两个数不相等，不要混用）")
    print(f"  任务书成本锚（来回）: {GTJA191_COST_BP_ROUNDTRIP:.1f} bp")
    print(f"  成本闸口径（来回）  : {cost_gate_cost_bp():.1f} bp")
    print(f"  IC 表默认配置（来回）: {ic_table_cost_bp():.1f} bp  ← FactorBatchConfig 历史默认值")
    print("  本报告所有 IC 都是**未扣费**的 Rank IC。成本只在「扣费后还能不能做」的判断里出现，")
    print("  不要拿 IC 直接和成本比较。扣费后收益要看分层多空，不是看 IC 乘一个系数。")


def _print_blocked() -> None:
    from app.factors.gtja191 import GTJA191_BLOCKED

    print()
    print(f"blocked:ops {len(GTJA191_BLOCKED)} 个（不登记假实现，不参与本次自评）")
    for item in GTJA191_BLOCKED:
        print(f"  {item.label:<10} {item.reason}  {item.detail}")


def _net_after_cost_gate(
    service,
    panel,
    factor_name: str,
    symbols: list[str] | None,
    start: date,
    end: date,
    market_dates,
    min_symbols_per_date: int,
    run_id: str,
) -> float | None:
    """按 N5 成本锚（18.2bp）重跑一遍分层多空，返回费后累计收益。

    **为什么不能直接用第一次的 `result.long_short_stats`：** 那一次的 `FactorConfig`
    用的是默认成本（来回 14bp）。拿它过 18.2bp 的门等于每天少扣 4.2bp，结果系统性
    偏乐观，而且从输出上看不出来。

    IC 不重算 —— 成本只影响分层收益，不影响 Rank IC。
    """
    from app.backtest.factor import FactorConfig
    from app.factors.gtja191 import _COST_FEES_PCT, _COST_SLIPPAGE_BPS

    gate_config = FactorConfig(
        factor_name=factor_name,
        symbols=symbols,
        start=start,
        end=end,
        n_groups=5,
        rebalance="daily",
        asset_type="stock",
        fees_pct=_COST_FEES_PCT,
        slippage_bps=_COST_SLIPPAGE_BPS,
    )
    try:
        gate_result = service._evaluate_panel(
            panel,
            gate_config,
            run_id,
            0.0,
            market_trading_dates=market_dates,
            min_symbols_per_date=min_symbols_per_date,
        )
    except Exception:  # noqa: BLE001 — 成本门失败不能伪造一个过门结果
        return None
    return gate_result.long_short_stats.get("total_return")


def _significance_of(result) -> tuple[float | None, float | None]:
    """由 IC 序列推 NW HAC t 与双侧 p。与 `FactorBacktestService._run_batch` 同一套。

    日频 1 日前瞻 → NW 滞后取 1（与 tip 的 `nw_lag=1` 一致）。
    输入是 `FactorResult.ic_series` 里**已经算好**的每日 IC，**不重算 IC**。

    返回 `(t_newey_west, p_value)`；样本不足以算 NW 时退回朴素 t。
    """
    from app.backtest import stats_v2

    ic_values = [row.get("ic") for row in result.ic_series]
    nw = stats_v2.newey_west_t(ic_values, lag=1)
    t_naive = stats_v2.naive_t(ic_values)
    if nw:
        return nw[0], stats_v2.normal_two_sided_p(nw[0])
    if t_naive is not None:
        return t_naive, stats_v2.normal_two_sided_p(t_naive)
    return None, None


def _evaluate_group(
    service,
    *,
    factor_ids: list[str],
    symbols: list[str] | None,
    start: date,
    end: date,
    chunk: int,
    min_symbols_per_date: int,
    exclude_limit: bool,
    exclude_st: bool,
    min_listed_days: int,
    engine,
    instruments,
    tag: str,
    cost_gate: bool = False,
) -> list[dict]:
    """跑一组因子的 IC。返回每因子一行；单因子失败不中止整组。

    `cost_gate=True` 时，每个因子额外按 **N5 成本锚（18.2bp）** 重跑一次分层多空，
    把干净的费后收益和 N2 标签一起写进行里。为什么不能直接用第一次的
    `result.long_short_stats`：那次的 `FactorConfig` 用的是默认成本（来回 14bp），
    拿它过 18.2bp 的门等于少扣 4.2bp/日，结果系统性偏乐观。

    `cost_gate=False`（alpha158 对照）不跑第二遍 —— 对照只需要 |IC| 分位，
    不需要给 158 个因子各贴一个标签，也省一半时间。
    """
    from app.backtest.factor import FactorBatchConfig, FactorBatchItem, FactorConfig
    from app.backtest.stats_v2 import bh_fdr_qvalues
    from app.enriched_generation import EnrichedGenerationUnavailableError
    from app.factors.gtja191 import GTJA191_COST_BP_ROUNDTRIP, blocked_reason
    from app.factors.gtja191_labels import label_of
    from app.factors.registry import get_factor

    rows: list[dict] = []
    for offset in range(0, len(factor_ids), chunk):
        batch_ids = factor_ids[offset:offset + chunk]
        batch_config = FactorBatchConfig(
            factor_names=batch_ids,
            symbols=symbols,
            start=start,
            end=end,
            n_groups=5,
            rebalance="daily",
            asset_type="stock",
        )
        try:
            panel = service._load_factor_panel(batch_config, batch_ids)
        except EnrichedGenerationUnavailableError as exc:
            print(f"  enriched 分区在读取过程中变了，跳过本批: {exc}")
            continue
        if panel.is_empty():
            print(f"  本批没有数据 ({batch_ids[0]} …)，跳过")
            continue
        panel, market_dates, section = restrict_evaluation_dates(
            panel, start=start, end=end, min_symbols=min_symbols_per_date,
        )
        if offset == 0:
            for line in format_cross_section(section, min_symbols_per_date):
                print(f"  {line}")
        if not market_dates:
            print("  没有达到截面门槛的交易日，不计算 IC。")
            return rows
        if exclude_limit and "raw_close" not in panel.columns:
            raw = engine.load_panel(
                symbols,
                panel.get_column("date").min(),
                end,
                columns=["symbol", "date", "raw_close"],
                asset_type="stock",
            )
            if raw.is_empty() or "raw_close" not in raw.columns:
                print("  enriched 没有 raw_close，不能按涨跌停过滤。跳过本组。")
                return rows
            panel = panel.join(
                raw.select("symbol", "date", "raw_close"), on=["symbol", "date"], how="left",
            )
        panel = service._attach_shared_next_return(panel, batch_config, trading_dates=market_dates)
        try:
            panel = apply_row_filters(
                panel,
                exclude_limit=exclude_limit,
                exclude_st=exclude_st,
                min_listed_days=min_listed_days,
                instruments=instruments,
            )
        except RuntimeError as exc:
            print(f"  过滤失败，跳过本组: {exc}")
            return rows
        # ── 第一遍：把本批所有因子跑出来，先不判定 ──
        # BH-FDR 的 q 是**批内**统计量（m = 本批可检验因子数），必须等整批的 p 值
        # 都齐了才能算。所以判定推到第二遍，不能一边跑一边判。
        evaluated: list[tuple[str, str, object, float | None]] = []
        for index, factor_name in enumerate(batch_ids, start=1):
            spec = get_factor(factor_name)
            label = spec.label if spec is not None else factor_name
            factor_config = FactorConfig(
                factor_name=factor_name,
                symbols=symbols,
                start=start,
                end=end,
                n_groups=5,
                rebalance="daily",
                asset_type="stock",
            )
            try:
                result = service._evaluate_panel(
                    panel,
                    factor_config,
                    f"{tag}-{offset + index}",
                    0.0,
                    market_trading_dates=market_dates,
                    min_symbols_per_date=min_symbols_per_date,
                )
            except Exception as exc:  # noqa: BLE001 — 单因子失败不能中止整组
                rows.append({
                    "factor_name": factor_name,
                    "label": label,
                    "ic_mean": None,
                    "ir": None,
                    "turnover": None,
                    "coverage": None,
                    "n_dates": 0,
                    "abs_ic": None,
                    "abs_ir": None,
                    "error": str(exc),
                    "verdict": None,
                })
                continue
            net = None
            if cost_gate:
                net = _net_after_cost_gate(
                    service, panel, factor_name, symbols, start, end,
                    market_dates, min_symbols_per_date,
                    f"{tag}-{offset + index}-gate",
                )
            evaluated.append((factor_name, label, result, net))

        # 批内 BH-FDR：与 `run_batch` 同一套（`stats_v2.bh_fdr_qvalues`），
        # 保证脚本给出的 q 与经 API/回测出来的 q 口径一致。
        significance = [_significance_of(result) for _, _, result, _ in evaluated]
        p_values = [item[1] for item in significance]
        nw_ts = [item[0] for item in significance]
        q_values = bh_fdr_qvalues(p_values)

        for (factor_name, label, result, net), p_value, q_value, nw_t in zip(
            evaluated, p_values, q_values, nw_ts, strict=True,
        ):
            # ⚠ `_evaluate_panel` 返回的是 `FactorResult`，**没有** t_newey_west /
            # p_value / q_value 三个字段（那是 `run_batch` 才挂到 `FactorBatchItem`
            # 上的）。所以这里要显式组一个 `FactorBatchItem` 交给映射，
            # **不能** `replace(result, …)` —— 那会直接 TypeError。
            # IC 一个不重算：ic_mean / ir / ic_series 全部原样搬过来。
            scored = FactorBatchItem(
                factor_name=factor_name,
                label=label,
                group="GTJA191（实验）",
                ic_mean=result.ic_mean,
                ir=result.ir,
                ic_win_rate=result.ic_win_rate,
                long_short_return=net,
                n_symbols=result.n_symbols,
                n_dates=result.n_dates,
                elapsed_ms=result.elapsed_ms,
                error=result.error,
                ic_decay=list(result.ic_decay or []),
                yearly_ic=list(result.yearly_ic or []),
                t_newey_west=nw_t,
                nw_lag=1 if nw_t is not None else None,
                p_value=p_value,
                q_value=q_value,
                ic_series=list(result.ic_series),
            )
            verdict = None
            if cost_gate:
                verdict = label_of(
                    scored,
                    # ⚠ 这里必须是 `len(ic_series)`，**不是** `result.n_dates`。
                    # `FactorResult.n_dates` 是 `panel["date"].n_unique()` —— 面板的
                    # 交易日数，跟 IC 有没有算出来无关。因子在截面上恒定（无方差）时
                    # Rank IC 全为 NaN，`n_dates` 仍然等于 287，`ic_mean` 却是 None。
                    # 拿 `n_dates` 当 IC 日数会让 V8 那条线彻底失效：29 天的样本会被
                    # 读成 287 天，一路判成 alive。
                    n_ic_dates=len(result.ic_series),
                    net_long_short_return=net,
                    cost_bp=GTJA191_COST_BP_ROUNDTRIP,
                    blocked_reason=blocked_reason(factor_name),
                    ic_series=result.ic_series,
                )
            rows.append({
                "factor_name": factor_name,
                "label": label,
                "ic_mean": result.ic_mean,
                "ir": result.ir,
                "turnover": result.turnover,
                "coverage": result.coverage,
                "n_dates": result.n_dates,
                "n_ic_dates": len(result.ic_series),
                "abs_ic": abs(result.ic_mean) if result.ic_mean is not None else None,
                "abs_ir": abs(result.ir) if result.ir is not None else None,
                "error": result.error,
                "verdict": verdict,
                "p_value": p_value,
                "q_value": q_value,
                "t_newey_west": nw_t,
            })
        print(f"  完成 {min(offset + chunk, len(factor_ids))}/{len(factor_ids)}")
        # 显式释放：整组 158 列的面板留在内存里会把容器顶爆。
        del panel
    return rows


def _print_rows(title: str, rows: list[dict]) -> None:
    """三列显著性字段（ICIR / NW-t / q）单独列出来，让过门的依据可核。

    这三个正是 N2 写死表判 `alive` 的门槛。只打 IC 不打它们，读者无法复核标签。
    """
    print()
    print(title)
    with_label = any(row.get("verdict") is not None for row in rows)
    header = (
        f"{'因子':<14} {'名称':<24} {'IC':>8} {'ICIR':>8} {'NW-t':>8} {'q':>7} "
        f"{'换手':>8} {'覆盖':>8} {'IC日':>6}"
    )
    header += f" {'标签':<14}" if with_label else ""
    print(header)
    print("-" * (len(header) + 8))
    for row in rows:
        line = (
            f"{row['factor_name']:<14} {str(row['label'])[:24]:<24} "
            f"{_fmt(row['ic_mean']):>8} {_fmt(row.get('ir')):>8} "
            f"{_fmt(row.get('t_newey_west')):>8} {_fmt(row.get('q_value')):>7} "
            f"{_fmt(row.get('turnover')):>8} "
            f"{_fmt(row.get('coverage')):>8} {row.get('n_ic_dates') or 0:>6}"
        )
        if with_label:
            verdict = row.get("verdict")
            line += f" {(verdict.label if verdict is not None else '—'):<14}"
        print(line)


def _print_verdicts(rows: list[dict]) -> list[dict]:
    """打标签明细与「下线候选」提示。返回候选行，供调用方决定要不要写报告。

    **只打印。** 这里不调用、也不允许调用 `unregister_factor` —— 退役要人拍板，
    脚本能做的是把证据摆整齐，而不是替人做决定。
    """
    labeled = [row for row in rows if row.get("verdict") is not None]
    if not labeled:
        return []
    from app.factors.gtja191 import GTJA191_COST_BP_ROUNDTRIP

    print()
    print("标签依据（FIX N2 写死表）：")
    print("  alive         IC日 ≥ 30 且 |IR| ≥ 0.50 且 |NW-t| ≥ 1.96 且 q ≤ 0.10 且 未命中 reversed")
    print("  dead          IC日 ≥ 30 且不满足 alive / reversed")
    print("  reversed      前后半窗 IC 均值符号相反，且两侧 |IC| ≥ 0.02")
    print("  underpowered  IC日 < 30，或 IR / NW-t / q 因样本不足为 None")
    print(f"  下线候选      = 标签∈{{dead,reversed}} 且 N5 费后多空 ≤ 0（成本 {GTJA191_COST_BP_ROUNDTRIP:.1f} bp 来回）")
    for row in labeled:
        verdict = row["verdict"]
        net = verdict.net_long_short_return
        net_text = "—" if net is None else f"{net:+.4f}"
        print(f"  {row['factor_name']:<12} {verdict.label:<14} 费后多空 {net_text:>9}  {verdict.reason}")

    candidates = [row for row in labeled if row["verdict"].is_delist_candidate]
    print()
    if not candidates:
        print("下线候选：无。")
        return []
    print(
        f"下线候选 {len(candidates)} 个"
        f"（标签 ∈ {{dead, reversed}} 且按 N5 {GTJA191_COST_BP_ROUNDTRIP:.1f}bp 费后未过门）:"
    )
    for row in candidates:
        print(f"  {row['factor_name']:<12} {row['verdict'].label:<10} {row['verdict'].reason}")
    print()
    print("**以上只是提示。** 本脚本不注销任何因子，也不改注册表：")
    print("  - 「候选」是说这批公式在 TSP 上的当前证据不足以支持投产；")
    print("  - 退役/下线要人工拍板，且要走独立的变更流程；")
    print("  - `underpowered` / `degenerate` / `error` / `blocked:*` 不在候选里 —— 那是缺证据，不是有反证。")
    return candidates


def _percentile_of(value: float, population: list[float]) -> float | None:
    """value 在 population 中的百分位（0–100）。population 至少 10 个才给，否则样本太薄没意义。"""
    clean = [item for item in population if item is not None]
    if len(clean) < 10:
        return None
    below = sum(1 for item in clean if item < value)
    equal = sum(1 for item in clean if item == value)
    return 100.0 * (below + 0.5 * equal) / len(clean)


def _print_comparison(gtja_rows: list[dict], base_rows: list[dict]) -> None:
    """gtja191 相对 alpha158 的分位。只在两组都有有效 IC 时才说。"""
    gtja_valid = [row for row in gtja_rows if row["abs_ic"] is not None and row["error"] is None]
    base_valid = [row for row in base_rows if row["abs_ic"] is not None and row["error"] is None]
    if not gtja_valid or len(base_valid) < 10:
        print()
        print(
            "对照跳过：两组有效 IC 样本不足"
            f"（gtja191 {len(gtja_valid)}，alpha158 {len(base_valid)}）。"
        )
        return
    base_ic = sorted(row["abs_ic"] for row in base_valid)
    base_ir = sorted(row["abs_ir"] for row in base_valid if row["abs_ir"] is not None)
    median_ic = base_ic[len(base_ic) // 2]
    print()
    print(f"对照：alpha158 有效 {len(base_valid)} 个，|IC| 中位数 {median_ic:.4f}")
    print(f"{'因子':<14} {'|IC|':>8} {'分位':>7} {'|ICIR|':>8} {'分位':>7}  说明")
    print("-" * 76)
    for row in sorted(gtja_valid, key=lambda item: item["abs_ic"], reverse=True):
        pct = _percentile_of(row["abs_ic"], base_ic)
        ir_pct = _percentile_of(row["abs_ir"], base_ir) if row["abs_ir"] is not None else None
        note = "不如 alpha158 中位数" if row["abs_ic"] < median_ic else ""
        print(
            f"{row['factor_name']:<14} {row['abs_ic']:>8.4f} "
            f"{('—' if pct is None else f'{pct:.0f}%'):>7} "
            f"{_fmt(row['abs_ir']):>8} "
            f"{('—' if ir_pct is None else f'{ir_pct:.0f}%'):>7}  {note}"
        )
    print()
    print("分位 = 该因子 |IC| 在 alpha158 全组 |IC| 分布里的百分位。")
    print("这是**同一区间、同一截面门槛**下的横向位置，不是统计显著性。")
    print("alpha158 内部有大量高度相关的因子，分布本身偏胖；分位 50% 不代表「有用」。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gtja191 实验组自评与 alpha158 对照")
    parser.add_argument("--days", type=int, default=365, help="检验区间长度，自然日，默认 365")
    parser.add_argument("--end", default="", help="截止日 YYYY-MM-DD；默认是早于今天的最近分区")
    parser.add_argument("--chunk", type=int, default=8, help="每批因子数，默认 8")
    parser.add_argument("--symbols", default="", help="逗号分隔的股票代码；留空为全市场")
    parser.add_argument("--no-baseline", action="store_true", help="不跑 alpha158 对照")
    parser.add_argument("--exclude-limit", action="store_true", help="去掉当天涨停或跌停的股票")
    parser.add_argument("--exclude-st", action="store_true", help="去掉当前名称含 ST 的股票")
    parser.add_argument(
        "--min-listed-days", type=int, default=0, help="去掉上市不足 N 个交易日的股票，例如 60",
    )
    parser.add_argument(
        "--min-symbols-per-date",
        type=int,
        default=200,
        help="IC 只用当天股票数不少于 N 的交易日。0 表示不过滤。默认 200",
    )
    args = parser.parse_args(argv)

    gate = _polars_gate()
    if gate:
        print(gate)
        return 1
    for name, value in (
        ("--chunk", args.chunk),
        ("--min-listed-days", args.min_listed_days),
        ("--min-symbols-per-date", args.min_symbols_per_date),
    ):
        if value < 0:
            print(f"{name} 不能为负", file=sys.stderr)
            return 1
    if args.chunk < 1:
        print("--chunk 必须 ≥ 1", file=sys.stderr)
        return 1

    data_dir = _data_dir()
    partitioned = _partition_dates(data_dir / "kline_daily_enriched")
    if not partitioned:
        return _print_missing(data_dir)

    today = beijing_today()
    if args.end:
        end = date.fromisoformat(args.end)
        if end >= today:
            print(f"警告: --end {end.isoformat()} 不是已收盘的历史日，当日分区可能仍在写入。")
    else:
        closed = latest_closed_trading_day(partitioned, today)
        if closed is None:
            print(f"没有早于 {today.isoformat()} 的 enriched 分区，盘中不评估今天。")
            return 2
        end = closed
    start = end - timedelta(days=args.days)

    from app.backtest.engine import BacktestEngine
    from app.backtest.factor import FactorBacktestService
    from app.factors.gtja191 import (
        GTJA191_COST_BP_ROUNDTRIP,
        GTJA191_IDS,
        GTJA191_SOURCE,
        GTJA191_SOURCE_DATE,
    )
    from app.tickflow.repository import DataStore, KlineRepository

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    engine = BacktestEngine(KlineRepository(DataStore(data_dir)))
    service = FactorBacktestService(engine)
    filtering = args.exclude_limit or args.exclude_st or args.min_listed_days > 0
    instruments = engine.repo.get_instruments() if filtering and engine.repo is not None else None

    print("gtja191 实验组自评（只读）")
    print(f"公式来源: {GTJA191_SOURCE}，年份 {GTJA191_SOURCE_DATE}")
    print(f"成本锚（来回）: {GTJA191_COST_BP_ROUNDTRIP:.1f} bp")
    print(
        f"区间 {start.isoformat()} ~ {end.isoformat()}（北京时间今天 {today.isoformat()}），"
        f"截面门槛 {args.min_symbols_per_date} 只/天，每批 {args.chunk} 个。"
    )
    if filtering:
        print("过滤: " + " ".join(part for part in (
            "去掉涨跌停" if args.exclude_limit else "",
            "去掉 ST" if args.exclude_st else "",
            f"上市满 {args.min_listed_days} 个交易日" if args.min_listed_days else "",
        ) if part))
        print("ST 用的是 instruments 里的当前名称，不是历史上每一天的 ST 状态。")
    _print_cost_caliber()
    _print_blocked()

    common = {
        "symbols": symbols,
        "start": start,
        "end": end,
        "chunk": args.chunk,
        "min_symbols_per_date": args.min_symbols_per_date,
        "exclude_limit": args.exclude_limit,
        "exclude_st": args.exclude_st,
        "min_listed_days": args.min_listed_days,
        "engine": engine,
        "instruments": instruments,
    }

    print()
    print(f"=== gtja191 自评（{len(GTJA191_IDS)} 个可算） ===")
    gtja_rows = _evaluate_group(
        service, factor_ids=list(GTJA191_IDS), tag="gtja", cost_gate=True, **common,
    )
    gtja_valid = [row for row in gtja_rows if row["abs_ic"] is not None and row["error"] is None]
    # 没有 IC 的因子也要出现在表里：它们不是"不存在"，而是"没算成"。
    # 只把有 IC 的排上去，会让读者以为这批只有 11 个因子。
    gtja_unmeasured = [row for row in gtja_rows if row["abs_ic"] is None or row["error"]]
    if not gtja_valid:
        print()
        print("gtja191 没有算出有效 IC。请确认 enriched 分区覆盖这段日期，且截面门槛不是过高。")
        for row in gtja_rows[:6]:
            if row["error"]:
                print(f"  {row['factor_name']}: {row['error']}")
        _print_verdicts(gtja_rows)
        return 1
    _print_rows(
        "按 |IC| 从高到低",
        sorted(gtja_valid, key=lambda row: row["abs_ic"], reverse=True) + gtja_unmeasured,
    )
    failed = [row for row in gtja_rows if row["error"]]
    if failed:
        print()
        print(f"计算失败 {len(failed)} 个:")
        for row in failed:
            print(f"  {row['factor_name']}: {row['error']}")

    _print_verdicts(gtja_rows)

    if args.no_baseline:
        print()
        print("按 --no-baseline 跳过 alpha158 对照。没有对照就没有横向位置，别把上表的 |IC| 直接说成「有效」。")
        return 0

    from app.factors.alpha158 import ALPHA158_IDS

    print()
    print(f"=== alpha158 对照（{len(ALPHA158_IDS)} 个，同区间同门槛） ===")
    base_rows = _evaluate_group(service, factor_ids=list(ALPHA158_IDS), tag="a158", **common)
    _print_comparison(gtja_rows, base_rows)
    print()
    print("结论口径提醒: 登记 ≠ 投产。本次自评只说明这批公式在 TSP 上算得出来、什么水平；")
    print("它不进信号白名单、不进挖掘候选、不改任何策略默认值。要不要上线是另一个决定。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0) from None
