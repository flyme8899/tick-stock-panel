#!/usr/bin/env python3.11
"""把 alphafeed 分钟K 灌进 TSP 的 kline_minute 数据集。

为什么不用 TSP 自带的 /api/kline/sync_minute:
  - 它是全市场同步 (5882 只), 而回测真正会碰到的票只有几百只, 全量同步浪费配额和时间。
  - /api/kline/sync_minute_single 又限制 days<=30, 覆盖不够。
所以这里绕过 API, 直接按 TSP 的存储契约 (Hive 分区 date=YYYY-MM-DD/part.parquet) 写盘。

存储契约 (实测自 /app/data/kline_minute/date=2026-09-30/part.parquet):
  symbol:str, datetime:datetime64[us], open/high/low/close/volume/amount: float64

用法:
  ./sync_minute.py --pool /tmp/minute_pool.json --start 2026-08-04 --end 2026-10-08
  ./sync_minute.py --pool-file ... --batch 50 --dry-run
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

SRC_URL = os.getenv("ALPHAFEED_SOURCE_URL", "http://127.0.0.1:3021")
TSP_DATA = os.getenv("TSP_DATA_DIR", "/workspace/tsp_upstream/data")
MINUTE_DIR = os.path.join(TSP_DATA, "kline_minute")
MAX_WORKERS = int(os.getenv("SYNC_WORKERS", "4"))
HTTP_TIMEOUT = int(os.getenv("SYNC_HTTP_TIMEOUT", "180"))


def fetch_batch(symbols: list[str], start: str, end: str, period: str = "1m"):
    """向适配层要一批标的的分钟K。"""
    r = requests.post(
        f"{SRC_URL}/minute",
        json={"symbols": symbols, "period": period,
              "start_time": start, "end_time": end},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True, help="标的池 JSON 文件 (list[str])")
    ap.add_argument("--start", default="2026-08-04")
    ap.add_argument("--end", default="2026-10-08")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--period", default="1m")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    pool = json.load(open(a.pool))
    if isinstance(pool, dict):
        pool = list(pool.keys())
    pool = sorted(set(pool))
    print(f"标的池 {len(pool)} 只, 区间 {a.start} ~ {a.end}, 批大小 {a.batch}")

    chunks = [pool[i:i + a.batch] for i in range(0, len(pool), a.batch)]
    frames, errors, done = [], [], 0
    t0 = time.time()

    def one(chunk):
        try:
            return fetch_batch(chunk, a.start, a.end, a.period), None
        except Exception as exc:
            return None, str(exc)[:200]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for resp, err in ex.map(one, chunks):
            done += 1
            if err:
                errors.append(err)
                print(f"  [{done}/{len(chunks)}] 失败: {err}")
                continue
            rows = resp.get("data") or []
            if resp.get("errors"):
                errors.extend(resp["errors"])
            if rows:
                frames.append(pd.DataFrame(rows))
            print(f"  [{done}/{len(chunks)}] {len(rows)} 根", end="\r", flush=True)

    print()
    if not frames:
        print("没拉到任何数据, 中止。")
        return 1

    df = pd.concat(frames, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # 类型必须与 TSP 自己的写法严格一致, 否则 polars scan_parquet 跨分区扫描会因
    # schema 冲突直接报错 (实测: volume Float64 vs Int64 → 整个查询失败 →
    # minute_fill 静默降级回日线价)。
    df["volume"] = df["volume"].fillna(0).astype("int64")
    df["symbol"] = df["symbol"].astype(str)
    df["datetime"] = df["datetime"].astype("datetime64[us]")
    df = df.dropna(subset=["symbol", "datetime", "close"])
    df = df.drop_duplicates(subset=["symbol", "datetime"]).sort_values(["datetime", "symbol"])
    df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")

    print(f"合计 {len(df):,} 根, 覆盖 {df['date'].nunique()} 个交易日, "
          f"{df['symbol'].nunique()} 只标的, 耗时 {time.time() - t0:.1f}s")
    print(f"时间跨度: {df['datetime'].min()} ~ {df['datetime'].max()}")
    if errors:
        print(f"错误 {len(errors)} 条, 例: {errors[:2]}")

    if a.dry_run:
        print("--dry-run, 不写盘")
        return 0

    os.makedirs(MINUTE_DIR, exist_ok=True)
    total_files, total_rows = 0, 0
    for date, g in df.groupby("date"):
        pdir = os.path.join(MINUTE_DIR, f"date={date}")
        os.makedirs(pdir, exist_ok=True)
        path = os.path.join(pdir, "part.parquet")
        out = g.drop(columns=["date"])
        if os.path.exists(path):
            old = pd.read_parquet(path)
            old["datetime"] = pd.to_datetime(old["datetime"])
            merged = pd.concat([old, out], ignore_index=True)
            merged = merged.drop_duplicates(subset=["symbol", "datetime"], keep="last")
            merged = merged.sort_values(["datetime", "symbol"])
        else:
            merged = out.sort_values(["datetime", "symbol"])
        # 写盘前再固化一次类型 —— 合并会把老分区的类型带进来:
        # 老分区若是 float64 volume (现网 date=2026-09-30 就曾是这样),
        # concat 后整列升为 float64 再写回, 等于把刚修好的分区重新写坏,
        # 而 polars 跨分区扫描会因为这一个分区让整张表查不动。
        merged["volume"] = pd.to_numeric(merged["volume"], errors="coerce").fillna(0).astype("int64")
        for c in ("open", "high", "low", "close", "amount"):
            merged[c] = pd.to_numeric(merged[c], errors="coerce").astype("float64")
        merged["symbol"] = merged["symbol"].astype(str)
        merged["datetime"] = pd.to_datetime(merged["datetime"]).astype("datetime64[us]")
        merged.to_parquet(path, index=False)
        total_files += 1
        total_rows += len(merged)

    print(f"已写入 {total_files} 个日期分区, 累计 {total_rows:,} 根 -> {MINUTE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
