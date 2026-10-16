#!/usr/bin/env python3
"""任务 B：把 probe 抽出的名单洗成干净的事件表与成员表。

输入：``probe/_cache/`` 里的公告详情与名单 JSON（probe 阶段产出）。
输出：``out/events.parquet``、``out/members.parquet``（并附同名 CSV 便于人工查看）。

清洗规则
--------

1. **只保留三个目标指数**：沪深300(000300) / 中证500(000905) / 中证1000(000852)。
2. **剔除 2020-11-27（公告 11514）**：该公告把沪深300 与中证香港100 的名单
   混排在同一分节，公告自述沪深300 更换 26 只，但抽出 55/56 只 —— 无法可靠拆分，
   按任务书「剔除或人工拆分」**选择剔除**（拆分需人工逐行判断，成本高且易错）。
3. **方向**：``in`` 纳入 / ``out`` 剔除；``alt`` 备选名单**不进主表**，单独输出。
4. **股票代码保留裸代码**，另存 ``symbol``（带 .SH/.SZ/.BJ 后缀），
   回测匹配用 ``symbol``，展示用 ``code``。

字段
----

events.parquet::

    notice_id, index_code, index_name, announce_date, effective_date,
    direction, in_count, out_count, source_url

members.parquet::

    notice_id, index_code, index_name, announce_date, effective_date,
    direction, code, symbol, name

用法::

    python research/index_rebalance/backtest/build_samples.py
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import polars as pl

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = ROOT / "probe" / "_cache"
OUT = ROOT / "out"

#: 目标指数：名称 -> 代码
INDEXES = {"沪深300": "000300", "中证500": "000905", "中证1000": "000852"}

#: 剔除的歧义公告（2020-11-27 沪深300 与中证香港100 混排）
EXCLUDE_NOTICES = {11514}

#: 生效日的多种写法。实测公告里有三种：
#:   「于2026年6月12日收市后生效」
#:   「于2021年6月11日收盘后生效」
#:   「决定于2020年12月14日调整」（无「生效」二字）
_EFFECTIVE_PATTERNS = (
    re.compile(r"于?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:收市后|收盘后)(?:正式)?生效"),
    re.compile(r"于?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:起)?(?:正式)?生效"),
    re.compile(r"于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*调整"),
)
_TAG = re.compile(r"<[^>]+>")

#: 交易所后缀规则（与 backend store.suffixed_symbol 一致）
_SH_PREFIX = ("60", "68", "9", "5")
_SZ_PREFIX = ("00", "30", "20", "1")


def suffixed(code: str) -> str:
    """6 位 A 股代码补交易所后缀。认不出的原样返回，绝不编造。"""
    c = str(code or "").strip().upper()
    if not c:
        return ""
    if "." in c:
        return c
    if len(c) != 6 or not c.isdigit():
        return c
    if c.startswith("92") or c[0] in "48":
        return f"{c}.BJ"
    if c.startswith(_SH_PREFIX):
        return f"{c}.SH"
    if c.startswith(_SZ_PREFIX):
        return f"{c}.SZ"
    return c


def strip_html(html: str) -> str:
    text = _TAG.sub("", html or "")
    return text.replace("&nbsp;", " ").replace("　", " ")


def parse_effective(content: str) -> str:
    """从公告正文解析生效日。解析不出返回空串（不猜）。"""
    text = strip_html(content)
    for pat in _EFFECTIVE_PATTERNS:
        m = pat.search(text)
        if m:
            y, mo, d = (int(g) for g in m.groups())
            try:
                from datetime import date
                return date(y, mo, d).isoformat()
            except ValueError:
                continue
    return ""


def load_notices() -> dict[int, dict]:
    out = {}
    for p in CACHE.glob("notice_*.json"):
        try:
            out[int(p.stem.split("_")[1])] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, IndexError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成干净事件表与成员表")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    notices = load_notices()
    lists_files = sorted(CACHE.glob("*.lists.json"))
    if not lists_files:
        print(f"❌ {CACHE} 下没有 *.lists.json —— 先跑 probe_index_rebalance.py")
        return 2

    events: list[dict] = []
    members: list[dict] = []
    alts: list[dict] = []
    excluded: list[dict] = []
    row_check: list[dict] = []

    for path in lists_files:
        notice_id = int(path.name.split(".")[0])
        lists = json.loads(path.read_text(encoding="utf-8"))
        notice = notices.get(notice_id) or {}
        announce = str(notice.get("publishDate") or "")
        effective = parse_effective(notice.get("content") or "")

        if notice_id in EXCLUDE_NOTICES:
            for name, buckets in lists.items():
                if name in INDEXES:
                    excluded.append({
                        "notice_id": notice_id, "announce_date": announce,
                        "index_name": name,
                        "reason": "沪深300 与中证香港100 混排，无法可靠拆分",
                        "in_count": len(buckets.get("in", [])),
                        "out_count": len(buckets.get("out", [])),
                    })
            continue

        for name, code in INDEXES.items():
            buckets = lists.get(name)
            if not buckets:
                continue
            n_in, n_out = len(buckets.get("in", [])), len(buckets.get("out", []))
            if n_in == 0 and n_out == 0:
                continue
            events.append({
                "notice_id": notice_id,
                "index_code": code,
                "index_name": name,
                "announce_date": announce,
                "effective_date": effective,
                "in_count": n_in,
                "out_count": n_out,
                "source_url": (notice.get("enclosureList") or [{}])[0].get("fileUrl", "")
                if notice.get("enclosureList") else "",
            })
            for direction, key in (("in", "in"), ("out", "out")):
                for raw_code, raw_name in buckets.get(key, []):
                    members.append({
                        "notice_id": notice_id,
                        "index_code": code,
                        "index_name": name,
                        "announce_date": announce,
                        "effective_date": effective,
                        "direction": direction,
                        "code": str(raw_code),
                        "symbol": suffixed(raw_code),
                        "name": str(raw_name),
                    })
            for raw_code, raw_name in buckets.get("alt", []):
                alts.append({
                    "notice_id": notice_id, "index_code": code, "index_name": name,
                    "announce_date": announce, "code": str(raw_code),
                    "symbol": suffixed(raw_code), "name": str(raw_name),
                })
            row_check.append({
                "notice_id": notice_id,
                "announce_date": announce,
                "effective_date": effective or "（未解析）",
                "index_name": name,
                "in": n_in, "out": n_out,
                "in_eq_out": "是" if n_in == n_out else "否",
            })

    if not events:
        print("❌ 没有生成任何事件")
        return 2

    ev_df = pl.DataFrame(events).sort(["announce_date", "index_code"])
    mb_df = pl.DataFrame(members).sort(["announce_date", "index_code", "direction", "code"])
    alt_df = pl.DataFrame(alts) if alts else pl.DataFrame(
        schema={"notice_id": pl.Int64, "index_code": pl.Utf8, "index_name": pl.Utf8,
                "announce_date": pl.Utf8, "code": pl.Utf8, "symbol": pl.Utf8, "name": pl.Utf8})

    args.out.mkdir(parents=True, exist_ok=True)
    ev_df.write_parquet(args.out / "events.parquet")
    mb_df.write_parquet(args.out / "members.parquet")
    alt_df.write_parquet(args.out / "alternates.parquet")
    ev_df.write_csv(args.out / "events.csv")
    mb_df.write_csv(args.out / "members.csv")

    print("=" * 74)
    print("任务 B：样本清洗结果")
    print("=" * 74)
    print(f"事件表 events.parquet  : {ev_df.height} 行")
    print(f"成员表 members.parquet : {mb_df.height} 行")
    print(f"备选 alternates.parquet: {alt_df.height} 行（不进主结论）")
    print()

    print("=== 行数核对表 ===")
    print(f"{'公告日':<12}{'生效日':<12}{'指数':<10}{'纳入':>5}{'剔除':>5}  入=出")
    for r in sorted(row_check, key=lambda x: (x["announce_date"], x["index_name"])):
        print(f"{r['announce_date']:<12}{r['effective_date']:<12}{r['index_name']:<10}"
              f"{r['in']:>5}{r['out']:>5}  {r['in_eq_out']}")

    print()
    print("=== 分指数汇总 ===")
    per = (mb_df.group_by(["index_name", "direction"]).len()
           .sort(["index_name", "direction"]))
    for row in per.iter_rows(named=True):
        print(f"  {row['index_name']:<10} {row['direction']:<4} {row['len']:>5}")

    print()
    print("=== 剔除的歧义公告 ===")
    for x in excluded:
        print(f"  ⚠ {x['announce_date']} id={x['notice_id']} {x['index_name']}: "
              f"自述 26 / 抽出 in{x['in_count']} out{x['out_count']} —— {x['reason']}")

    # 生效日缺失清单（回测时生效窗会跳过这些事件）
    missing_eff = ev_df.filter(pl.col("effective_date") == "")
    if missing_eff.height:
        print()
        print("=== 生效日未解析（生效窗将跳过）===")
        for row in missing_eff.iter_rows(named=True):
            print(f"  {row['announce_date']} id={row['notice_id']} {row['index_name']}")

    print()
    print(f"已写: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
