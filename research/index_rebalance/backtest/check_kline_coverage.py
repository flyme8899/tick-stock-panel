#!/usr/bin/env python3
"""任务 1：验 kline_daily 行情窗口覆盖（**在有 TSP 数据的机器上跑**）。

判定目标：能否覆盖 **2020-10-01 ~ 2026-06-30**。

为什么是这个区间：
- 起点 2020-10-01 = 最早事件（2020-11-27 公告）的公告日前约 20 个交易日；
- 终点 2026-06-30 = 最近一次生效日（2026-06-12）后 10 个交易日。

抽 10 个「指数×事件」（含 2020-11 / 2021-11 / 2024-11 / 2026-05），
对每个纳入股核对窗口内日 K 是否齐全；**缺数率 > 5% 判失败**。

用法（容器内）::

    docker compose exec app python /tmp/check_kline_coverage.py --data-dir /app/data

用法（宿主机开发）::

    $env:PYTHONPATH = "backend"
    backend\\.venv\\Scripts\\python research\\index_rebalance\\backtest\\check_kline_coverage.py --data-dir data

只读：只读 parquet，不写入任何位置（除 --out 指定的 json）。
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl

#: 要求的覆盖区间
WINDOW_START = date(2020, 10, 1)
WINDOW_END = date(2026, 6, 30)

#: 缺数率阈值：超过就判失败（任务书规定 5%）
MISSING_FAIL_RATE = 0.05

#: 抽取的核对样本：「指数 × 事件」10 个，覆盖 2020-11 / 2021-11 / 2024-11 / 2026-05
#: (index_name, index_code, notice_id, announce_date, effective_date)
SAMPLE_EVENTS: list[tuple[str, str, int, str, str]] = [
    ("沪深300", "000300", 11514, "2020-11-27", ""),          # 必须含 2020-11
    ("沪深300", "000300", 13888, "2021-11-26", "2021-12-10"),  # 必须含 2021-11
    ("沪深300", "000300", 15471, "2024-11-29", "2024-12-13"),  # 必须含 2024-11
    ("沪深300", "000300", 3006137, "2026-05-29", "2026-06-12"),  # 必须含 2026-05
    ("中证500", "000905", 15267, "2024-05-31", "2024-06-14"),
    ("中证500", "000905", 3006000, "2025-11-28", "2025-12-12"),
    ("中证500", "000905", 14796, "2023-05-26", "2023-06-09"),
    ("中证1000", "000852", 14497, "2022-11-25", "2022-12-09"),
    ("中证1000", "000852", 15471, "2024-11-29", "2024-12-13"),
    ("中证1000", "000852", 15690, "2025-05-30", "2025-06-13"),
]

HERE = Path(__file__).resolve().parent
PROBE_CACHE = HERE.parent / "probe" / "_cache"


def load_lists(notice_id: int) -> dict:
    """读 probe 抽好的名单缓存。"""
    path = PROBE_CACHE / f"{notice_id}.lists.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def kline_root(data_dir: Path) -> Path:
    return Path(data_dir) / "kline_daily"


def partition_dates(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name[5:] for p in root.glob("date=*")
                  if p.is_dir() and p.name.startswith("date="))


SUFFIXES = (".SH", ".SZ", ".BJ")


def with_suffix(code: str) -> list[str]:
    """裸代码 -> 候选的带后缀写法（库里的 symbol 带交易所后缀）。

    各板块规则（与 ``store.suffixed_symbol`` 一致）：
    沪市 60/68/9/5、深市 00/30/2/1、北交所 8/4/92。
    这里**返回全部候选**让调用方去匹配，避免猜错板块。
    """
    code = str(code).strip()
    if not code:
        return []
    if "." in code:
        return [code]
    return [f"{code}{s}" for s in SUFFIXES] + [code]


def load_window(root: Path, symbols: list[str], start: str, end: str) -> pl.DataFrame:
    """只读窗口内的分区（按分区名裁剪，避免全量扫描）。

    ``symbols`` 传**裸代码**；本函数内部展开成带后缀的候选集合再匹配，
    避免「库里带后缀、名单是裸代码」导致全部判为缺失。
    """
    wanted: set[str] = set()
    for code in symbols:
        wanted.update(with_suffix(code))

    frames = []
    for ds in partition_dates(root):
        if ds < start or ds > end:
            continue
        part = root / f"date={ds}" / "part.parquet"
        if not part.exists():
            continue
        try:
            frames.append(pl.read_parquet(part, columns=["symbol", "date", "close"]))
        except Exception as exc:  # noqa: BLE001 - 坏分区记数不中断
            print(f"    ⚠ 读取失败 {part}: {type(exc).__name__}")
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    frame = pl.concat(frames, how="vertical_relaxed")
    return frame.filter(pl.col("symbol").is_in(sorted(wanted)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验 kline_daily 覆盖（只读）")
    ap.add_argument("--data-dir", type=Path, required=True,
                    help="TSP 数据目录（容器内一般是 /app/data）")
    ap.add_argument("--out", type=Path, default=HERE.parent / "out" / "coverage.json")
    args = ap.parse_args(argv)

    root = kline_root(args.data_dir)
    print("=" * 74)
    print("任务 1：kline_daily 行情窗口覆盖检查（只读）")
    print("=" * 74)
    print(f"数据目录 : {args.data_dir}")
    print(f"日K根目录: {root}")
    print(f"要求覆盖 : {WINDOW_START} .. {WINDOW_END}")
    print()

    if not root.is_dir():
        print(f"❌ 目录不存在：{root}")
        print()
        print("可能原因：")
        print("  1. data_dir 传错（容器内应为 /app/data）")
        print("  2. 这台实例还没同步过日 K（跑一次全量同步）")
        return 2

    dates = partition_dates(root)
    if not dates:
        print(f"❌ {root} 下没有任何 date=YYYY-MM-DD 分区")
        return 2

    print(f"分区数: {len(dates)}")
    print(f"最早分区: {dates[0]}")
    print(f"最晚分区: {dates[-1]}")

    earliest_ok = dates[0] <= WINDOW_START.isoformat()
    latest_ok = dates[-1] >= WINDOW_END.isoformat()
    print(f"\n覆盖起点 {WINDOW_START}: {'✅ 满足' if earliest_ok else '❌ 不满足（晚于要求）'}")
    print(f"覆盖终点 {WINDOW_END}: {'✅ 满足' if latest_ok else '❌ 不满足（早于要求）'}")

    if not (earliest_ok and latest_ok):
        gap_start = (date.fromisoformat(dates[0]) - WINDOW_START).days
        gap_end = (WINDOW_END - date.fromisoformat(dates[-1])).days
        print(f"\n缺口: 起点缺约 {max(0, gap_start)} 天，终点缺约 {max(0, gap_end)} 天")

    print()
    print("=" * 74)
    print("逐事件核对纳入股日 K 完整性")
    print("=" * 74)

    results = []
    for index_name, index_code, notice_id, announce, effective in SAMPLE_EVENTS:
        lists = load_lists(notice_id)
        entry = lists.get(index_name) or {}
        members = [code for code, _ in entry.get("in", [])]
        if not members:
            print(f"  ⚠ {index_name} {announce} (id={notice_id}): 无名单缓存，跳过")
            continue

        # 窗口：公告日前 20 交易日 → 生效日后 10 交易日（自然日近似，留足余量）
        a = date.fromisoformat(announce)
        e = date.fromisoformat(effective) if effective else a + timedelta(days=14)
        w_start = (a - timedelta(days=35)).isoformat()   # ≈20 交易日
        w_end = (e + timedelta(days=18)).isoformat()     # ≈10 交易日

        frame = load_window(root, members, w_start, w_end)
        present = set(frame["symbol"].unique().to_list()) if not frame.is_empty() else set()
        missing = [
            m for m in members
            if not any(cand in present for cand in with_suffix(m))
        ]

        rate = len(missing) / len(members) if members else 1.0
        ok = rate <= MISSING_FAIL_RATE
        print(f"  {'✅' if ok else '❌'} {index_name} {announce}  窗口 {w_start}..{w_end}")
        print(f"       纳入 {len(members)} 只，缺 {len(missing)} 只，缺数率 {rate:.1%}")
        if missing:
            print(f"       缺失示例: {missing[:8]}")
        results.append({
            "index": index_name, "index_code": index_code, "notice_id": notice_id,
            "announce_date": announce, "effective_date": effective,
            "window": [w_start, w_end],
            "members": len(members), "missing": len(missing),
            "missing_rate": round(rate, 4), "pass": ok,
        })

    print()
    print("=" * 74)
    print("判定")
    print("=" * 74)
    failed = [r for r in results if not r["pass"]]
    overall = earliest_ok and latest_ok and results and not failed
    print(f"  区间覆盖: {'✅' if earliest_ok and latest_ok else '❌'}")
    print(f"  抽样 {len(results)} 个事件，失败 {len(failed)} 个")
    for r in failed:
        print(f"    ❌ {r['index']} {r['announce_date']} 缺数率 {r['missing_rate']:.1%}")
    print()
    print(f"  >>> 任务 1: {'通过 ✅ 可进入回测' if overall else '不通过 ❌ 停止回测'}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "data_dir": str(args.data_dir),
        "root": str(root),
        "partition_count": len(dates),
        "earliest": dates[0], "latest": dates[-1],
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "range_pass": earliest_ok and latest_ok,
        "samples": results,
        "overall_pass": bool(overall),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写: {args.out}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
