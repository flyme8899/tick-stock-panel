"""生成热门事件「事件→板块→个股」用的申万行业静态数据。

只在更新数据时手动运行，线上运行时不调用（线上只读生成的 JSON，不联网）。

输入（研究目录 research/sector_mapping/data/ 下的离线文件）：
  sw_clf_history.xls     申万官网 StockClassifyUse_stock.xls（个股行业变动历史，申万 2021 版内部码）
  sw_code_to_name.json   申万内部码 → 行业名（用东财行业成分 Jaccard 反推）
  dc_boardtype.jsonl     东财个股→板块归属（只用来取当前上市股票、证券简称和一级行业名校验）

输出：backend/app/news/data/sw_industry.json
  只含【当前】行业归属（每只股票取最新一条记录），不含历史变动。

用法：
  python scripts/build_sw_sector_data.py --src ../research/sector_mapping/data --out app/news/data/sw_industry.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

# 申万 2021 版一级行业（内部码前两位）。生成时用东财一级行业成分做多数票校验，不一致就报错。
SW_L1 = {
    "11": "农林牧渔", "22": "基础化工", "23": "钢铁", "24": "有色金属", "27": "电子",
    "28": "汽车", "33": "家用电器", "34": "食品饮料", "35": "纺织服饰", "36": "轻工制造",
    "37": "医药生物", "41": "公用事业", "42": "交通运输", "43": "房地产", "45": "商贸零售",
    "46": "社会服务", "48": "银行", "49": "非银金融", "51": "综合", "61": "建筑材料",
    "62": "建筑装饰", "63": "电力设备", "64": "机械设备", "65": "国防军工", "71": "计算机",
    "72": "传媒", "73": "通信", "74": "煤炭", "75": "石油石化", "76": "环保", "77": "美容护理",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-jaccard", type=float, default=0.5)
    args = ap.parse_args()
    src = Path(args.src)

    code_names = json.loads((src / "sw_code_to_name.json").read_text(encoding="utf-8"))
    code_names = {k: v for k, v in code_names.items() if float(v.get("jaccard") or 0) >= args.min_jaccard}

    # 当前上市股票 + 简称 + 东财一级行业（校验用）
    secu: dict[str, str] = {}
    names: dict[str, str] = {}
    em_l1: dict[str, str] = {}
    with (src / "dc_boardtype.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            code = str(row.get("SECURITY_CODE") or "")
            if not code:
                continue
            secu[code] = str(row.get("SECUCODE") or "")
            names[code] = str(row.get("SECURITY_NAME_ABBR") or "")
            if row.get("BOARD_TYPE") == "行业" and str(row.get("BOARD_LEVEL")) == "1":
                em_l1[code] = str(row.get("BOARD_NAME") or "")

    df = pd.read_excel(src / "sw_clf_history.xls", header=0, dtype=str)
    df = df.rename(columns={"股票代码": "code", "计入日期": "since", "行业代码": "ind"})
    df["code"] = df["code"].str.strip().str.zfill(6)
    df["ind"] = df["ind"].str.strip()
    df["since"] = pd.to_datetime(df["since"], errors="coerce")
    latest = df.sort_values("since").groupby("code").tail(1)

    members: dict[str, list[str]] = defaultdict(list)
    stock_names: dict[str, str] = {}
    votes: dict[str, Counter] = defaultdict(Counter)
    skipped_unlisted = skipped_unmapped = 0
    for code, ind in zip(latest["code"], latest["ind"], strict=True):
        symbol = secu.get(code)
        if not symbol:
            skipped_unlisted += 1
            continue
        if ind not in code_names:
            skipped_unmapped += 1
            continue
        members[ind].append(symbol)
        stock_names[symbol] = names.get(code) or symbol
        if code in em_l1:
            votes[ind[:2]][em_l1[code]] += 1

    for prefix, counter in votes.items():
        top, n = counter.most_common(1)[0]
        share = n / sum(counter.values())
        expected = SW_L1.get(prefix)
        if expected is None or (top != expected and share > 0.5):
            raise SystemExit(f"一级行业校验失败: {prefix} 期望 {expected} 东财多数 {top} ({share:.0%})")

    industries = {
        code: {"name": code_names[code]["name"], "jaccard": round(float(code_names[code]["jaccard"]), 3)}
        for code in sorted(members)
    }
    out = {
        "version": "2026-10-17",
        "source": "申万 StockClassifyUse_stock.xls（最新一条归属）+ 行业名由东财行业成分反推；仅当前成分",
        "l1": SW_L1,
        "industries": industries,
        "members": {code: sorted(members[code]) for code in sorted(members)},
        "names": dict(sorted(stock_names.items())),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n", encoding="utf-8")
    print(f"industries={len(industries)} stocks={len(stock_names)} skipped_unlisted={skipped_unlisted} skipped_unmapped={skipped_unmapped}")
    for prefix in sorted(votes):
        top, n = votes[prefix].most_common(1)[0]
        print(prefix, SW_L1[prefix], "东财多数:", top, f"{n}/{sum(votes[prefix].values())}")


if __name__ == "__main__":
    main()
