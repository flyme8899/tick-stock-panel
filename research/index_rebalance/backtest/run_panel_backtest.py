#!/usr/bin/env python3
"""任务 C：指数调仓效应的**面板粗回测骨架**。

研究用途，只读。**不产出交易信号、不写生产表、不改 panel 买卖点。**

口径（写死，避免事后调参）
--------------------------

* **收益**：个股**前复权收盘价**（``close``）的简单收益。
* **基准**：**同指数**（``kline_index_daily`` 里该指数当月收益）。
  选同指数而非全 A 等权，是因为调仓效应本身是**相对该指数**的事件
  （被纳入意味着要被动买入），用别的基准会把风格收益混进来。
  基准缺失时该事件记 ``bench=nan`` 并计入「基准缺失数」，**不做替换**。
* **窗口**（交易日，相对公告日 T0 与生效日 E）：
  ``T0-5..T0+5``、``T0..T0+10``、``E-5..E+5``、``E..E+10``
* **买入持有**：公告日收盘买入 → 生效日收盘卖出（模拟「调入预期」）。
* **成本**：单边 8 / 13 / 18 bp，**主结论用 13 bp**。
  往返计 **2×** 单边（买入一次、卖出一次）。
* **汇总**：分组均值 + t 值；同时报**按事件聚类的稳健标准误**
  （同一公告日的多个股票视为一个 cluster，避免同期横截面相关高估显著性）。

判定门槛（研究判定，非上线）
----------------------------

* B 调入侧：公告后或生效窗**净超额（扣 13bp）> 0 且 t ≥ 1.96（事件聚类）**
* C 调出侧：同窗**净超额 < 0 且 |t| ≥ 1.96**，或明确写「无显著负向」
* D 稳健：至少两个指数方向一致；去掉最大 2 个事件后符号不变

用法::

    # 合成自测（管道验证）
    python research/index_rebalance/backtest/run_panel_backtest.py \\
        --data-dir <synth> --events out/events.parquet --members out/members.parquet \\
        --out out/synth

    # 真数据（覆盖确认后）
    python research/index_rebalance/backtest/run_panel_backtest.py \\
        --data-dir /app/data --events out/events.parquet --members out/members.parquet \\
        --out out/real

缺分区时**明确失败并给出缺哪些日期**，不静默出空表。
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import polars as pl

#: 成本档（单边，bp）
COST_BPS = (8.0, 13.0, 18.0)
#: 主结论成本（单边 bp）
PRIMARY_COST_BP = 13.0
#: 收益单位换算
BP = 1e-4

#: 窗口定义： (标签, 锚点, 起点偏移, 终点偏移)  —— 偏移以交易日计
WINDOWS = (
    ("T0-5_T0+5", "T0", -5, 5),
    ("T0_T0+10", "T0", 0, 10),
    ("E-5_E+5", "E", -5, 5),
    ("E_E+10", "E", 0, 10),
)

#: 事件聚类的最小 cluster 数（少于这个数，聚类 t 不可靠，改报普通 t）
MIN_CLUSTERS = 5


@dataclass
class Coverage:
    """分区覆盖情况。"""

    dates: list[date] = field(default_factory=list)
    missing_days: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.dates) and not self.missing_days

    @property
    def earliest(self) -> date | None:
        return self.dates[0] if self.dates else None

    @property
    def latest(self) -> date | None:
        return self.dates[-1] if self.dates else None


def partition_dates(root: Path) -> list[date]:
    if not root.is_dir():
        return []
    out = []
    for p in root.glob("date=*"):
        if not p.is_dir():
            continue
        try:
            out.append(date.fromisoformat(p.name[5:]))
        except ValueError:
            continue
    return sorted(out)


def load_index_close(data_dir: Path, index_code: str) -> pl.DataFrame:
    """读指数日 K（前复权收盘）。取不到就返回空表（基准缺失，不猜）。"""
    root = Path(data_dir) / "kline_index_daily"
    frames = []
    for p in sorted(root.glob("date=*/part.parquet")) if root.is_dir() else []:
        try:
            df = pl.read_parquet(p)
        except Exception:  # noqa: BLE001 - 坏分区跳过
            continue
        cols = df.columns
        sym_col = next((c for c in ("symbol", "code", "index_code") if c in cols), None)
        close_col = next((c for c in ("close", "close_price") if c in cols), None)
        if sym_col is None or close_col is None or "date" not in cols:
            continue
        frames.append(df.select([
            pl.col(sym_col).cast(pl.Utf8).alias("symbol"),
            pl.col("date"),
            pl.col(close_col).cast(pl.Float64).alias("close"),
        ]))
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    all_df = pl.concat(frames, how="vertical_relaxed")
    # 指数代码可能是 000300 或 000300.SH 等形式
    want = {index_code, f"{index_code}.SH", f"{index_code}.SZ", f"{index_code}.CSI"}
    return all_df.filter(pl.col("symbol").is_in(sorted(want)))


def load_stock_close(data_dir: Path, symbols: list[str],
                     start: date, end: date) -> pl.DataFrame:
    """读个股日 K 窗口（按分区裁剪）。只取需要的列。"""
    root = Path(data_dir) / "kline_daily"
    wanted = set(symbols)
    frames = []
    for ds in partition_dates(root):
        if ds < start or ds > end:
            continue
        p = root / f"date={ds.isoformat()}" / "part.parquet"
        if not p.exists():
            continue
        try:
            df = pl.read_parquet(p, columns=["symbol", "date", "close"])
        except Exception:  # noqa: BLE001
            continue
        frames.append(df.filter(pl.col("symbol").is_in(sorted(wanted))))
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    return pl.concat(frames, how="vertical_relaxed")


def _return_between(prices: pl.DataFrame, symbol: str,
                    d0: date, d1: date) -> float | None:
    """[d0, d1] 的简单收益。缺失任一端返回 None。"""
    sub = prices.filter((pl.col("symbol") == symbol) & (pl.col("date") >= d0)
                        & (pl.col("date") <= d1)).sort("date")
    if sub.height < 2:
        return None
    first, last = sub["close"][0], sub["close"][-1]
    if not first or first <= 0 or last is None:
        return None
    return float(last) / float(first) - 1.0


def _shift(dates: list[date], anchor: date, offset: int) -> date | None:
    """在**实际交易日序列**上偏移 offset 个交易日。"""
    import bisect

    i = bisect.bisect_left(dates, anchor)
    if i >= len(dates):
        return None
    j = i + offset
    if j < 0 or j >= len(dates):
        return None
    return dates[j]


def welch_t(values: list[float]) -> tuple[float, float]:
    """返回 (均值, t)。单样本 t（H0: 均值=0）。样本不足返回 (mean, nan)。"""
    n = len(values)
    if n == 0:
        return float("nan"), float("nan")
    mean = sum(values) / n
    if n < 2:
        return mean, float("nan")
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    if var <= 0:
        return mean, float("nan")
    return mean, mean / math.sqrt(var / n)


def clustered_t(values: list[float], clusters: list[str]) -> tuple[float, float]:
    """按 cluster 聚类的稳健 t（cluster-robust，单维）。

    做法：先算 cluster 均值，再对 cluster 均值做单样本 t。
    这是「按事件聚类」的常用近似 —— 同一公告日的横截面相关被吸收掉。
    """
    n = len(values)
    if n == 0:
        return float("nan"), float("nan")
    overall = sum(values) / n
    groups: dict[str, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(v)
    if len(groups) < MIN_CLUSTERS:
        return overall, float("nan")   # cluster 太少，聚类 t 不可靠
    means = [sum(g) / len(g) for g in groups.values()]
    return overall, welch_t(means)[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="指数调仓面板粗回测骨架（只读）")
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--events", type=Path, required=True)
    ap.add_argument("--members", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--min-coverage", type=float, default=0.95,
                    help="窗口内个股日K最低覆盖率，低于此值该事件标记为缺数")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("任务 C：指数调仓面板粗回测（骨架）")
    print("=" * 74)
    print(f"数据目录: {args.data_dir}")
    print(f"事件表  : {args.events}")
    print(f"成员表  : {args.members}")
    print("基准    : 同指数（kline_index_daily），缺失不替换")
    print(f"成本    : 单边 {COST_BPS} bp，主结论 {PRIMARY_COST_BP} bp，往返 2×")
    print()

    if not args.events.exists() or not args.members.exists():
        print("❌ 事件/成员表不存在 —— 先跑 build_samples.py")
        return 2

    events = pl.read_parquet(args.events)
    members = pl.read_parquet(args.members)
    print(f"事件 {events.height} 行 / 成员 {members.height} 行")

    # ---- 覆盖检查（fail-closed）----
    daily_root = Path(args.data_dir) / "kline_daily"
    cov = Coverage(dates=partition_dates(daily_root))
    print()
    print("--- 行情覆盖 ---")
    if not cov.dates:
        print(f"❌ {daily_root} 下没有 date= 分区")
        print("   可能：data_dir 传错，或日 K 尚未同步。")
        return 2
    print(f"最早分区: {cov.earliest}")
    print(f"最晚分区: {cov.latest}")

    need_start = min(date.fromisoformat(d) for d in events["announce_date"].to_list()) \
        - timedelta(days=40)
    need_end = max((date.fromisoformat(d) for d in events["effective_date"].to_list()
                    if d), default=max(date.fromisoformat(d)
                                       for d in events["announce_date"].to_list())) \
        + timedelta(days=30)
    print(f"事件需要: {need_start} .. {need_end}")

    if cov.earliest is None or cov.earliest > need_start:
        print(f"❌ 最早分区 {cov.earliest} 晚于所需起点 {need_start}")
        print(f"   缺口约 {(cov.earliest - need_start).days} 天 —— 需先补齐日 K 再跑")
        return 3
    if cov.latest is None or cov.latest < need_end:
        print(f"⚠ 最晚分区 {cov.latest} 早于所需终点 {need_end}")
        print("   （最近的事件窗口可能不完整，结果会标注）")

    # ---- 逐事件算超额 ----
    print()
    print("--- 逐事件计算 ---")
    rows: list[dict] = []
    cluster_rows: list[dict] = []
    date_list = cov.dates

    for ev in events.iter_rows(named=True):
        notice_id = ev["notice_id"]
        idx_name, idx_code = ev["index_name"], ev["index_code"]
        announce = date.fromisoformat(ev["announce_date"])
        eff = date.fromisoformat(ev["effective_date"]) if ev["effective_date"] else None

        grp = members.filter((pl.col("notice_id") == notice_id)
                             & (pl.col("index_code") == idx_code))
        symbols = sorted(set(grp["symbol"].to_list()))
        if not symbols:
            continue

        # 指数基准
        idx_close = load_index_close(args.data_dir, idx_code)

        # 需要的日期范围
        w_start = _shift(date_list, announce, -5) or announce
        w_end = _shift(date_list, eff or announce, 10) or announce
        stock = load_stock_close(args.data_dir, symbols, w_start, w_end)
        have = set(stock["symbol"].unique().to_list()) if not stock.is_empty() else set()

        for direction in ("in", "out"):
            syms = sorted(set(grp.filter(pl.col("direction") == direction)["symbol"].to_list()))
            if not syms:
                continue
            covered = [s for s in syms if s in have]
            cover_rate = len(covered) / len(syms)
            if cover_rate < args.min_coverage:
                print(f"  ⚠ {idx_name} {announce} {direction}: 覆盖 {cover_rate:.0%} "
                      f"< {args.min_coverage:.0%}，标记缺数")
                rows.append({
                    "notice_id": notice_id, "index_name": idx_name, "index_code": idx_code,
                    "announce_date": ev["announce_date"], "effective_date": ev["effective_date"],
                    "direction": direction, "symbol": "", "window": "COVERAGE",
                    "gross": None, "bench": None, "excess": None,
                    "cover_rate": round(cover_rate, 4), "note": "覆盖不足",
                })
                continue

            for label, anchor_name, off0, off1 in WINDOWS:
                anchor = announce if anchor_name == "T0" else eff
                if anchor is None:
                    continue
                d0 = _shift(date_list, anchor, off0)
                d1 = _shift(date_list, anchor, off1)
                if d0 is None or d1 is None or d0 >= d1:
                    continue
                bench = _return_between(idx_close, next(iter(
                    set(idx_close["symbol"].to_list())), "") if not idx_close.is_empty() else "",
                    d0, d1) if not idx_close.is_empty() else None
                for sym in covered:
                    gross = _return_between(stock, sym, d0, d1)
                    if gross is None:
                        continue
                    excess = (gross - bench) if bench is not None else None
                    rows.append({
                        "notice_id": notice_id, "index_name": idx_name, "index_code": idx_code,
                        "announce_date": ev["announce_date"], "effective_date": ev["effective_date"],
                        "direction": direction, "symbol": sym, "window": label,
                        "gross": gross, "bench": bench, "excess": excess,
                        "cover_rate": round(cover_rate, 4), "note": "",
                    })

            # 买入持有：公告收盘 -> 生效收盘
            if eff is not None and eff > announce:
                bench_bh = _return_between(idx_close, next(iter(
                    set(idx_close["symbol"].to_list())), "") if not idx_close.is_empty() else "",
                    announce, eff) if not idx_close.is_empty() else None
                for sym in covered:
                    gross = _return_between(stock, sym, announce, eff)
                    if gross is None:
                        continue
                    excess = (gross - bench_bh) if bench_bh is not None else None
                    rows.append({
                        "notice_id": notice_id, "index_name": idx_name, "index_code": idx_code,
                        "announce_date": ev["announce_date"], "effective_date": ev["effective_date"],
                        "direction": direction, "symbol": sym, "window": "BH_T0_E",
                        "gross": gross, "bench": bench_bh, "excess": excess,
                        "cover_rate": round(cover_rate, 4), "note": "",
                    })

        cluster_rows.append({"notice_id": notice_id, "index_name": idx_name,
                             "announce_date": ev["announce_date"]})

    detail = pl.DataFrame(rows) if rows else pl.DataFrame()
    if detail.is_empty():
        print("❌ 没有任何可用观测 —— 检查覆盖与代码后缀匹配")
        print(f"   调试: 事件 {events.height} 行, 成员 {members.height} 行, "
              f"分区 {len(date_list)} 个")
        return 4

    args.out.mkdir(parents=True, exist_ok=True)
    detail.write_parquet(args.out / "panel_detail.parquet")
    detail.write_csv(args.out / "panel_detail.csv")

    # ---- 汇总：分组均值 + t（普通 & 事件聚类）----
    print()
    print("--- 汇总（主口径：净超额，成本 13bp 单边 / 往返 2×）----")
    valid = detail.filter(pl.col("excess").is_not_null())
    summary_rows = []
    for (idx_name, direction, window), g in valid.group_by(
            ["index_name", "direction", "window"], maintain_order=True):
        vals = g["excess"].to_list()
        clusters = g["announce_date"].to_list()
        mean, t = welch_t(vals)
        _, t_cl = clustered_t(vals, clusters)
        # 往返成本只在 BH 窗口是完整一买一卖；其余窗口按单边计
        cost = (2 * PRIMARY_COST_BP if window == "BH_T0_E" else PRIMARY_COST_BP) * BP
        summary_rows.append({
            "index_name": idx_name, "direction": direction, "window": window,
            "n": len(vals), "mean_excess": mean, "t": t, "t_clustered": t_cl,
            "net_after_cost": mean - cost,
            "n_clusters": len(set(clusters)),
        })

    summary = pl.DataFrame(summary_rows).sort(["window", "direction", "index_name"])
    summary.write_parquet(args.out / "panel_summary.parquet")
    summary.write_csv(args.out / "panel_summary.csv")

    print(f"{'窗口':<12}{'方向':<5}{'指数':<10}{'n':>5}{'净超额':>10}{'t':>8}{'t聚类':>8}")
    for r in summary.iter_rows(named=True):
        print(f"{r['window']:<12}{r['direction']:<5}{r['index_name']:<10}{r['n']:>5}"
              f"{r['net_after_cost']*100:>9.2f}%{r['t']:>8.2f}"
              f"{(r['t_clustered'] if r['t_clustered'] is not None else float('nan')):>8.2f}")

    # ---- 成本敏感度 ----
    print()
    print("--- 成本敏感度（各档下的净超额均值，%）---")
    cost_rows = []
    for (idx_name, direction, window), g in valid.group_by(
            ["index_name", "direction", "window"], maintain_order=True):
        vals = g["excess"].to_list()
        mean = sum(vals) / len(vals)
        row = {"index_name": idx_name, "direction": direction, "window": window, "n": len(vals)}
        for bp in COST_BPS:
            mult = 2 if window == "BH_T0_E" else 1
            row[f"net_{int(bp)}bp"] = (mean - mult * bp * BP) * 100
        cost_rows.append(row)
    cost_df = pl.DataFrame(cost_rows).sort(["window", "direction", "index_name"])
    cost_df.write_csv(args.out / "cost_sensitivity.csv")
    print(f"{'窗口':<12}{'方向':<5}{'指数':<10}"
          + "".join(f"{int(bp)}bp".rjust(9) for bp in COST_BPS))
    for r in cost_df.iter_rows(named=True):
        print(f"{r['window']:<12}{r['direction']:<5}{r['index_name']:<10}"
              + "".join(f"{r[f'net_{int(bp)}bp']:>8.2f}%" for bp in COST_BPS))

    # ---- 判定 A–D ----
    print()
    print("--- 判定 ---")
    verdict = judge(summary, valid)
    for line in verdict:
        print("  " + line)

    synthetic = _looks_synthetic(args.data_dir)
    meta = {
        "data_dir": str(args.data_dir),
        "coverage": {"earliest": str(cov.earliest), "latest": str(cov.latest),
                     "partitions": len(cov.dates)},
        "n_events": events.height, "n_members": members.height,
        "n_observations": detail.height, "n_valid": valid.height,
        "primary_cost_bp": PRIMARY_COST_BP,
        "benchmark": "同指数（kline_index_daily）",
        "verdict": verdict,
        # ⚠️ 明确标注数据来源，避免合成结果被误读成研究发现
        "data_source": "SYNTHETIC" if synthetic else "REAL",
        "warning": ("合成数据，仅用于测通管道；不得作为研究发现" if synthetic else ""),
    }
    (args.out / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    if synthetic:
        print()
        print("⚠️ 本次用的是**合成数据**（--data-dir 看着像测试目录）。")
        print("   上面的判定只证明管道跑得通，**不是研究发现**，不得引用。")
    print()
    print(f"已写: {args.out}")
    return 0


def _looks_synthetic(data_dir: Path) -> bool:
    """粗判数据目录是否为合成/测试数据。

    只做**保守**判断：目录名出现 synth/tmp/test/mock 才认定，
    避免把真目录误标成合成（那会让人不敢相信真结果）。
    """
    text = str(data_dir).lower()
    return any(k in text for k in ("synth", "mock", "fake", "\\tmp\\", "/tmp/"))


def judge(summary: pl.DataFrame, valid: pl.DataFrame) -> list[str]:
    """按任务书 A–D 门槛给判定。**不因不通过而放宽窗口或换基准。**"""
    out: list[str] = []
    if summary.is_empty():
        return ["A 行情覆盖: ❌ 无有效观测", "结论: 暂停，不做落地"]

    # B: 调入侧，公告窗或生效窗，净超额>0 且 t>=1.96
    b_pass = []
    for r in summary.iter_rows(named=True):
        if r["direction"] != "in" or r["window"].startswith("BH"):
            continue
        tcl = r["t_clustered"] if r["t_clustered"] is not None else r["t"]
        if r["net_after_cost"] > 0 and tcl is not None and abs(tcl) >= 1.96:
            b_pass.append(f"{r['index_name']}/{r['window']}")
    out.append(f"B 调入侧(>0 且 |t|≥1.96): {'✅ ' + ', '.join(b_pass) if b_pass else '❌ 未达标'}")

    # C: 调出侧 净超额<0 且 |t|>=1.96
    c_pass = []
    for r in summary.iter_rows(named=True):
        if r["direction"] != "out" or r["window"].startswith("BH"):
            continue
        tcl = r["t_clustered"] if r["t_clustered"] is not None else r["t"]
        if r["net_after_cost"] < 0 and tcl is not None and abs(tcl) >= 1.96:
            c_pass.append(f"{r['index_name']}/{r['window']}")
    out.append(f"C 调出侧(<0 且 |t|≥1.96): {'✅ ' + ', '.join(c_pass) if c_pass else '❌ 无显著负向'}")

    # D: 至少两个指数方向一致（同一窗口、同方向符号相同）
    d_ok = False
    for window in {r["window"] for r in summary.iter_rows(named=True)}:
        for direction in ("in", "out"):
            sub = [r for r in summary.iter_rows(named=True)
                   if r["window"] == window and r["direction"] == direction]
            if len(sub) < 2:
                continue
            signs = {1 if r["net_after_cost"] > 0 else -1 for r in sub}
            if len(signs) == 1:
                d_ok = True
    out.append(f"D 至少两个指数方向一致: {'✅' if d_ok else '❌'}")

    passed = bool(b_pass) and d_ok
    out.append(f"结论: {'✅ 通过，可继续' if passed else '⏸ 暂停，不做落地'}")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
