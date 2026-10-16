#!/usr/bin/env python3
"""对照检查：把抽出的名单与官网公告正文自述的更换只数逐一核对。

任务书要求的「名单一致率」有两层含义，本脚本做的是**可自动化、可复现**的那一层：

1. **内部一致性**：公告正文写「沪深300指数更换19只样本」，抽出的调入名单就该是 19 只。
   这一层能自动算，且非常有力 —— 正文与名单来自同一公告的不同部分（文本 vs 表格/附件）。
2. **对官网页面**：需要人工打开官网逐个核对（或与官网「拟生效样本」比对）。
   本脚本把待核对的样本导出成 CSV，便于人工抽查。

用法::

    python research/index_rebalance/probe/verify_consistency.py
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"


def main() -> int:
    events_path = CACHE / "events.json"
    if not events_path.exists():
        print("缺少 events.json —— 先跑 probe_index_rebalance.py")
        return 2
    events = json.loads(events_path.read_text(encoding="utf-8"))
    got = [e for e in events if e.get("indexes")]
    if not got:
        print("没有抽到名单的事件")
        return 2

    print("=" * 74)
    print("一致性核对：公告正文自述更换只数  vs  抽出的名单条数")
    print("=" * 74)

    rows = []
    total_checked = total_match = 0
    for e in got:
        notice_path = CACHE / f"notice_{e['notice_id']}.json"
        if not notice_path.exists():
            continue
        raw = json.loads(notice_path.read_text(encoding="utf-8"))
        content = raw.get("content") or ""
        text = re.sub(r"<[^>]+>", "", content)
        # 「沪深300指数更换19只样本」
        stated = {}
        for name, count in re.findall(
                r"(沪深\s*300|中证\s*500|中证\s*1000)\s*指数更换\s*(\d+)\s*只", text):
            stated[re.sub(r"\s+", "", name)] = int(count)

        for idx_name, pair in e["indexes"].items():
            n_in = len(pair["in"])
            n_out = len(pair["out"])
            want = stated.get(idx_name)
            ok = (want is None) or (want == n_in == n_out)
            if want is not None:
                total_checked += 1
                total_match += 1 if ok else 0
            rows.append({
                "公告日": e["announce_date"],
                "生效日": e.get("effective_date") or "",
                "指数": idx_name,
                "正文自述": want if want is not None else "",
                "抽出调入": n_in,
                "抽出调出": n_out,
                "一致": "" if want is None else ("是" if ok else "否"),
            })

    width = 10
    print(f"{'公告日':<{width}} {'生效日':<{width}} {'指数':<8} {'自述':>4} {'调入':>4} {'调出':>4} 一致")
    print("-" * 60)
    for r in rows:
        print(f"{r['公告日']:<{width}} {r['生效日']:<{width}} {r['指数']:<8} "
              f"{r['正文自述']!s:>4} {r['抽出调入']:>4} {r['抽出调出']:>4} {r['一致']}")

    print()
    if total_checked:
        rate = total_match / total_checked * 100
        print(f"可自动核对项: {total_checked}  一致: {total_match}  一致率: {rate:.1f}%")
    else:
        print("本批公告正文未自述更换只数，无法自动核对（需人工比对官网）")

    out = HERE / "consistency_check.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n明细已导出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
