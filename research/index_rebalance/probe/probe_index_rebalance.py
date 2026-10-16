#!/usr/bin/env python3
"""指数调仓数据可得性探测（只读）。

回答一个问题：能不能拿到足够的历史数据，研究沪深300 / 中证500 / 中证1000
定期调仓的纳入与剔除效应。

**只读**：只访问中证指数官网，不写 TSP 数据目录、不改 backend、不落线上库。
缓存写入本目录 ``_cache/``（研究用，可随时删）。

用法::

    python research/index_rebalance/probe/probe_index_rebalance.py --year 2018
    python research/index_rebalance/probe/probe_index_rebalance.py --year 2018 --full

不带 ``--full`` 时只抓「定期调整结果」公告（样本少、跑得快），
用于评估覆盖度；``--full`` 会把临时调整也纳入。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csindex_source import (
    TARGET_INDEXES,
    Adjustment,
    download,
    harvest_target_events,
    lists_from_attachment,
    lists_from_notice_html,
    merge_lists,
)

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"


def _warm(adj: Adjustment, *, fetch_pdf: bool) -> dict[str, dict]:
    """取一次事件的名单（带磁盘缓存）。

    名单有两个来源，**都要取并合并**：

    1. 公告**正文 HTML** 的表格（2022 年起，部分名单直接写在正文里）
    2. **附件**（完整名单；2021 年及更早是 XLSX，2022 年起是 PDF）
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    pdf_path = CACHE / f"{adj.notice_id}.pdf"
    lists_path = CACHE / f"{adj.notice_id}.lists.json"

    if lists_path.exists():
        return json.loads(lists_path.read_text(encoding="utf-8"))

    parts: list[dict] = []

    notice_path = CACHE / f"notice_{adj.notice_id}.json"
    if notice_path.exists():
        try:
            raw = json.loads(notice_path.read_text(encoding="utf-8"))
            parts.append(lists_from_notice_html(raw.get("content") or ""))
        except (OSError, json.JSONDecodeError):
            pass

    if adj.attachment and fetch_pdf:
        if not pdf_path.exists():
            try:
                pdf_path.write_bytes(download(adj.attachment))
            except Exception as exc:  # noqa: BLE001 - 个别历史附件已失效，跳过即可
                print(f"    ⚠ 附件下载失败 {adj.notice_id}: {str(exc)[:70]}")
        if pdf_path.exists():
            try:
                parts.append(lists_from_attachment(pdf_path.read_bytes()))
            except Exception as exc:  # noqa: BLE001 - 坏附件不该中断整轮
                print(f"    ⚠ 附件解析失败 {adj.notice_id}: {type(exc).__name__}")

    lists = merge_lists(*parts)
    if lists:
        lists_path.write_text(json.dumps(lists, ensure_ascii=False), encoding="utf-8")
    return lists


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="指数调仓数据可得性探测（只读）")
    ap.add_argument("--year", type=int, default=2018, help="起始年份，默认 2018")
    ap.add_argument("--full", action="store_true", help="含临时调整（默认只看定期调整）")
    ap.add_argument("--no-pdf", action="store_true", help="不下载附件（只统计事件，快）")
    ap.add_argument("--detail-gap", type=float, default=6.0,
                    help="详情接口请求间隔秒数，默认 6（中证官网有 WAF，别调太小）")
    ap.add_argument("--out", type=Path, default=CACHE / "events.json",
                    help="事件清单落盘位置")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("指数调仓数据可得性探测（只读）")
    print("=" * 74)
    print(f"目标指数: {', '.join(f'{k}({v})' for k, v in TARGET_INDEXES.items())}")
    print(f"起始年份: {args.year}   含临时调整: {args.full}   取附件: {not args.no_pdf}")
    print(f"详情间隔: {args.detail_gap}s（中证官网对详情接口有 WAF，突发请求会 403）")
    print()

    def on_block(exc: Exception) -> None:
        print(f"    ⚠ 详情接口被拦（{str(exc)[:60]}），等待 60 秒后重试…")
        time.sleep(60)

    print("抓取公告并解析详情…")
    events = harvest_target_events(start_year=args.year, progress=lambda m: print(m),
                                   detail_gap_s=args.detail_gap, on_block=on_block,
                                   cache_dir=CACHE)
    if not args.full:
        events = [e for e in events if e.regular]
    events.sort(key=lambda e: e.announce_date)

    print(f"\n命中目标指数的调样事件: {len(events)}")
    if not events:
        print("没有拿到任何事件 —— 见报告「卡在哪一条」。")
        return 2

    # ---- 覆盖统计 ----
    print()
    print("=" * 74)
    print("覆盖统计")
    print("=" * 74)
    by_year: Counter[str] = Counter()
    by_index: Counter[str] = Counter()
    for e in events:
        by_year[e.announce_date[:4]] += 1
        for name in e.targets:
            by_index[name] += 1
    print(f"  公告日范围: {events[0].announce_date} .. {events[-1].announce_date}")
    print(f"  按年份: {dict(sorted(by_year.items()))}")
    print(f"  按指数: {dict(by_index)}")
    no_eff = [e for e in events if not e.effective_date]
    print(f"  生效日解析成功: {len(events) - len(no_eff)}/{len(events)}")
    with_pdf = [e for e in events if e.attachment]
    print(f"  带名单附件: {len(with_pdf)}/{len(events)}")

    # ---- 抽名单，统计 股票×事件 ----
    print()
    print("=" * 74)
    print("纳入/剔除名单抽取（全部事件）")
    print("=" * 74)
    pairs_total = {"in": 0, "out": 0}
    per_index = {name: {"events": 0, "in": 0, "out": 0} for name in TARGET_INDEXES}
    per_event: list[dict] = []
    fetch_pdf = not args.no_pdf
    for i, e in enumerate(events, 1):
        lists = _warm(e, fetch_pdf=fetch_pdf)
        rec = {
            "notice_id": e.notice_id,
            "title": e.title,
            "announce_date": e.announce_date,
            "effective_date": e.effective_date,
            "regular": e.regular,
            "targets": e.targets,
            "attachment": e.attachment,
            "indexes": {},
        }
        for name, pair in lists.items():
            if name not in TARGET_INDEXES:
                continue
            rec["indexes"][name] = {"out": pair["out"], "in": pair["in"]}
            pairs_total["out"] += len(pair["out"])
            pairs_total["in"] += len(pair["in"])
            per_index[name]["in"] += len(pair["in"])
            per_index[name]["out"] += len(pair["out"])
        for name in rec["indexes"]:
            per_index[name]["events"] += 1
        per_event.append(rec)
        if i % 5 == 0 or i == len(events):
            print(f"  {i}/{len(events)}  累计 纳入 {pairs_total['in']} / 剔除 {pairs_total['out']}")

    got = [r for r in per_event if r["indexes"]]
    print(f"\n  抽到名单的事件: {len(got)}/{len(events)}")
    print(f"  纳入 股票×事件 = {pairs_total['in']}")
    print(f"  剔除 股票×事件 = {pairs_total['out']}")
    print("\n  分指数:")
    for name, s in per_index.items():
        print(f"    {name:<8} 事件 {s['events']:>2}  纳入 {s['in']:>4}  剔除 {s['out']:>4}")

    # 覆盖年份
    years_with = sorted({r["announce_date"][:4] for r in got})
    print(f"\n  有名单的年份: {years_with}")
    missing = [r["announce_date"] for r in per_event if not r["indexes"]]
    if missing:
        print(f"  无名单的事件 {len(missing)} 个: {missing}")

    # ---- 判定 ----
    print()
    print("=" * 74)
    print("判定（对照任务书门槛）")
    print("=" * 74)
    checks = [
        ("调整事件 ≥ 20 次（三指数合计）", len(events) >= 20, f"{len(events)} 次"),
        ("纳入 股票×事件 ≥ 300", pairs_total["in"] >= 300, f"{pairs_total['in']}"),
        ("每个事件都有公告日", all(e.announce_date for e in events),
         f"{sum(1 for e in events if e.announce_date)}/{len(events)}"),
        ("每个事件都有生效日", not no_eff, f"{len(events) - len(no_eff)}/{len(events)}"),
        ("每个事件都有名单", len(got) == len(events), f"{len(got)}/{len(events)}"),
    ]
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}  ({detail})")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(per_event, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"\n事件清单已落盘: {args.out}")
    print(f"附件缓存目录: {CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
