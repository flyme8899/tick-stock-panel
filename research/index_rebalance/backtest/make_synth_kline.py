#!/usr/bin/env python3
"""合成日 K 生成器 —— **仅用于测通回测管道，不是真实数据**。

用途：在真实行情未就绪（或本机没有数据）时，验证 ``run_panel_backtest.py``
能正确跑完并产出非空结果表；以及验证缺分区时能清晰失败。

生成规则（刻意做成「有已知答案」，便于校验管道）：
- 交易日：2020-10-01 ~ 2026-06-30 的工作日（近似交易日）；
- 个股价格：纳入股在**公告日之后**给一段正超额，剔除股给负超额，
  幅度固定，便于人工核对汇总方向是否与构造一致；
- 指数价格：全市场等权式的平缓上行序列（作为基准）。

用法::

    python research/index_rebalance/backtest/make_synth_kline.py --out /tmp/synth
    python research/index_rebalance/backtest/make_synth_kline.py --out /tmp/synth --drop-before 2021-06-01
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import polars as pl

#: 合成区间的默认范围
DEFAULT_START = date(2020, 10, 1)
DEFAULT_END = date(2026, 6, 30)


def trading_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成日K（测试管道用）")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--members", type=Path,
                    default=Path(__file__).resolve().parent.parent / "out" / "members.parquet")
    ap.add_argument("--start", default=DEFAULT_START.isoformat())
    ap.add_argument("--end", default=DEFAULT_END.isoformat())
    ap.add_argument("--drop-before", default=None,
                    help="删掉该日期之前的分区（用于测试覆盖失败路径）")
    ap.add_argument("--drift", type=float, default=0.002,
                    help="纳入/剔除股的窗口内日漂移（正的越多越强）")
    ap.add_argument("--idio", type=float, default=0.02,
                    help="个股特异性日波动幅度（让 in/out 两组有可区分的噪声）")
    args = ap.parse_args(argv)

    import random

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = trading_days(start, end)

    # ---- 从成员表取所需股票 ----
    if not args.members.exists():
        print(f"❌ 成员表不存在: {args.members}（先跑 build_samples.py）")
        return 2
    members = pl.read_parquet(args.members)
    symbols = sorted(set(members["symbol"].to_list()))
    print(f"成员表股票数: {len(symbols)}")

    # 事件日：每个 symbol 在各事件里的公告日与方向
    # ⚠️ 同一股票常同时出现在「某指数的调出」与「另一指数的调入」（如中证500→中证1000），
    #    两股漂移会**精确抵消**。所以这里按 (symbol, index_name) 分别记账，
    #    再对同一 symbol 求和 —— 抵消是这段合成数据的**已知性质**，不是管道 bug。
    events_by_symbol: dict[str, list[tuple[date, str]]] = {}
    for r in members.iter_rows(named=True):
        d = date.fromisoformat(r["announce_date"])
        events_by_symbol.setdefault(r["symbol"], []).append((d, r["direction"]))

    # 每只股票的固定特异性漂移（按代码定种子，保证可复现）
    idio_drift: dict[str, float] = {}
    for sym in symbols:
        rng = random.Random(f"idio-{sym}")
        idio_drift[sym] = rng.uniform(-args.idio, args.idio) / 100.0

    out_root = Path(args.out)
    stock_root = out_root / "kline_daily"
    index_root = out_root / "kline_index_daily"
    for p in (stock_root, index_root):
        if p.exists():
            import shutil
            shutil.rmtree(p)
        p.mkdir(parents=True)

    # ---- 逐日写分区 ----
    base = 10.0
    idx_base = 4000.0
    written = 0
    for i, d in enumerate(days):
        if args.drop_before and d < date.fromisoformat(args.drop_before):
            continue
        # 个股：大盘缓慢上行 + 事件方向漂移 + 个股特异性漂移（每日复利累积）
        rows = []
        for sym in symbols:
            drift = 0.0
            for ev_day, direction in events_by_symbol.get(sym, []):
                delta = (d - ev_day).days
                if 0 <= delta <= 21:
                    drift += args.drift * (1 if direction == "in" else -1)
            px = (base * (1 + 0.0003 * i + idio_drift[sym] * i) * (1 + drift))
            px = max(px, 0.01)
            rows.append({"symbol": sym, "date": d, "open": px, "high": px * 1.01,
                         "low": px * 0.99, "close": px, "volume": 1e6, "amount": px * 1e6})
        frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
        part = stock_root / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(part / "part.parquet")

        # 指数：三只，平缓上行
        idx_rows = []
        for code in ("000300", "000905", "000852"):
            px = idx_base * (1 + 0.0002 * i)
            idx_rows.append({"symbol": code, "date": d, "open": px, "high": px * 1.01,
                             "low": px * 0.99, "close": px})
        idx_frame = pl.DataFrame(idx_rows).with_columns(pl.col("date").cast(pl.Date))
        ipart = index_root / f"date={d.isoformat()}"
        ipart.mkdir(parents=True, exist_ok=True)
        idx_frame.write_parquet(ipart / "part.parquet")
        written += 1

    print(f"已写 {written} 个交易日分区")
    print(f"  个股: {stock_root}")
    print(f"  指数: {index_root}")
    print(f"  区间: {max(start, date.fromisoformat(args.drop_before)) if args.drop_before else start} .. {end}")
    print()
    print("⚠️ 这是**合成数据**，仅用于测通管道；不得用于任何正式结论。")
    print("   已知性质：同一股票若同时被某指数调出又被另一指数调入，方向漂移会抵消。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
