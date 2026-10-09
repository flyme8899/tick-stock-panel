#!/usr/bin/env python3
"""回放本地分钟 K，统计做T和异动在一组阈值下每天触发几次。

不写行情，不发钉钉。分钟分区是 Hive 目录 data/kline_minute/date=*/part.parquet。
成交量按根累加，均价用 amount / (volume × 100)，和推送同一套规则。

    PYTHONPATH=backend backend/.venv/bin/python scripts/replay_push_rules.py --symbols 600519.SH,000001.SZ --days 5

60 日新高、新低依赖盘前日线极值，这个脚本不统计。异动只数涨停、炸板、跌停、翘板。
按每一分钟走一遍。线上大约每 3 分钟看一次，所以这里的次数是推送次数的上限。
当前默认是均价偏离 1.5%、贴近高低 0.3%、冷却 60 分钟。网格仍把更密和更疏的档位一起打出来。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.news.push import count_rule_edges, session_signal_sets

VWAP_GRID = (0.010, 0.015, 0.020, 0.025)
RANGE_GRID = (0.003, 0.005)
COOLDOWN_GRID = (10, 20, 30, 60, 120)


def default_minute_root() -> Path:
    from app.config import settings
    return Path(settings.data_dir) / "kline_minute"


def load_minutes(root: Path, symbols: list[str], start: date, end: date) -> pl.DataFrame:
    paths = sorted(root.glob("date=*/part.parquet"))
    chosen = []
    for path in paths:
        token = path.parent.name.removeprefix("date=")
        try:
            day = date.fromisoformat(token[:10])
        except ValueError:
            continue
        if start <= day <= end:
            chosen.append(path)
    if not chosen:
        return pl.DataFrame()
    frame = pl.scan_parquet([str(path) for path in chosen]).collect()
    if frame.is_empty() or "symbol" not in frame.columns or "datetime" not in frame.columns:
        return pl.DataFrame()
    frame = frame.filter(pl.col("symbol").is_in(symbols))
    if "date" not in frame.columns:
        frame = frame.with_columns(pl.col("datetime").dt.date().alias("date"))
    return frame.sort(["symbol", "date", "datetime"])


def replay(frame: pl.DataFrame) -> list[dict]:
    if frame.is_empty():
        return []
    rows: list[dict] = []
    last_close: dict[str, float] = {}
    for (symbol, day), part in frame.group_by(["symbol", "date"], maintain_order=True):
        symbol = str(symbol)
        day = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        bars = part.select([
            name for name in ("open", "high", "low", "close", "volume", "amount") if name in part.columns
        ]).to_dicts()
        prev = last_close.get(symbol)
        if bars:
            close = bars[-1].get("close")
            if close:
                last_close[symbol] = float(close)
        if prev is None:
            continue
        t_sets = {
            (vwap, band): session_signal_sets(
                bars, mode="t", symbol=symbol, prev_close=prev,
                vwap_band=vwap, range_band=band, trade_date=day,
            )
            for vwap in VWAP_GRID
            for band in RANGE_GRID
        }
        abnormal = session_signal_sets(
            bars, mode="abnormal", symbol=symbol, prev_close=prev, trade_date=day,
        )
        for cooldown in COOLDOWN_GRID:
            for (vwap, band), series in t_sets.items():
                for rule, count in sorted(count_rule_edges(series, cooldown).items()):
                    rows.append({
                        "symbol": symbol,
                        "date": day.isoformat(),
                        "kind": "t_trade",
                        "rule": rule,
                        "vwap_pct": vwap,
                        "range_pct": band,
                        "cooldown_min": cooldown,
                        "triggers": count,
                    })
            for rule, count in sorted(count_rule_edges(abnormal, cooldown).items()):
                if rule in {"new_high", "new_low"}:
                    continue
                rows.append({
                    "symbol": symbol,
                    "date": day.isoformat(),
                    "kind": "abnormal",
                    "rule": rule,
                    "vwap_pct": "",
                    "range_pct": "",
                    "cooldown_min": cooldown,
                    "triggers": count,
                })
    return rows


def render(rows: list[dict]) -> str:
    if not rows:
        return "没有触发。第一天只用来取昨收，或者这些阈值下没有从无到有的信号。\n"
    lines = ["symbol,date,kind,rule,vwap_pct,range_pct,cooldown_min,triggers"]
    for row in rows:
        lines.append(
            f"{row['symbol']},{row['date']},{row['kind']},{row['rule']},"
            f"{row['vwap_pct']},{row['range_pct']},{row['cooldown_min']},{row['triggers']}"
        )
    lines.append("")
    lines.append("合计（全部股票、全部日期）")
    totals: dict[tuple, int] = {}
    for row in rows:
        key = (row["kind"], row["rule"], row["vwap_pct"], row["range_pct"], row["cooldown_min"])
        totals[key] = totals.get(key, 0) + int(row["triggers"])
    lines.append("kind,rule,vwap_pct,range_pct,cooldown_min,triggers")
    for key in sorted(totals):
        kind, rule, vwap, band, cooldown = key
        lines.append(f"{kind},{rule},{vwap},{band},{cooldown},{totals[key]}")
    lines.append("")
    return "\n".join(lines)


def parse_symbols(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回放分钟 K，统计推送规则触发次数")
    parser.add_argument("--symbols", required=True, help="逗号分隔，如 600519.SH,000001.SZ")
    parser.add_argument("--days", type=int, default=5, help="回看的自然日数，含今天，默认 5")
    parser.add_argument("--path", help="分钟分区根目录，默认 data/kline_minute")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD，默认今天")
    args = parser.parse_args(argv)
    if args.days < 2:
        print("至少需要 2 天：第一天只提供昨收。", file=sys.stderr)
        return 2
    root = Path(args.path) if args.path else default_minute_root()
    if not root.is_dir():
        print(f"分钟目录不存在: {root}", file=sys.stderr)
        return 2
    from app.market_time import cn_today
    end = date.fromisoformat(args.end) if args.end else cn_today()
    start = end - timedelta(days=args.days - 1)
    symbols = parse_symbols(args.symbols)
    frame = load_minutes(root, symbols, start, end)
    print(render(replay(frame)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
