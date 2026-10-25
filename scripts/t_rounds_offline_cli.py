#!/usr/bin/env python3
r"""离线同尺特征派生 + 模式 A 纸面回测（批次 20261025hfeat）。

**仅供研究，不构成投资建议；执行须人工确认。**

这个脚本把 `backend/app/t_rounds/offline/` 的三步串起来：

    读导出分钟 → 判同尺并对齐 → 派生特征 → 模式 A 纸面回测 → 写 Parquet + JSON

它**只读**输入文件，**只写** `--out-dir`。不碰生产分区、不调 `partition_writer`、
不推送、不接 lifespan。跑完就结束。

## 用法

在仓库根目录（与 `./dev.sh` 同一套后端虚拟环境）:

    PYTHONPATH=backend backend/.venv/bin/python scripts/t_rounds_offline_cli.py \
        --minute /data/tsp-reports/minute/kline_minute_20260105.csv.gz \
        --out-dir /tmp/hfeat

混尺数据必须显式给对齐引子（本脚本**不会**替你猜因子）:

    --align-factor 0.5
    --align-factor-by-date 2026-01-05=0.5,2026-01-06=0.25
    --align-raw-basis

查帮助:

    PYTHONPATH=backend backend/.venv/bin/python scripts/t_rounds_offline_cli.py --help

## 为什么强制 `--out-dir`

默认写哪里都是错的：写 `/data/tsp-reports` 会污染生产分区，写当前目录会散落文件。
所以宁可让调用方多打几个字。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DISCLAIMER = "仅供研究，不构成投资建议；执行须人工确认。"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "离线同尺特征派生 + 模式 A 纸面回测。"
            f"{DISCLAIMER}"
        ),
        epilog=(
            "输出: <out-dir>/features.parquet + <out-dir>/summary.json\n"
            "本脚本不写生产分区、不推送、不接 lifespan。\n"
            f"{DISCLAIMER}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--minute",
        action="append",
        required=True,
        metavar="FILE",
        help="kline_minute_YYYYMMDD.csv.gz 路径，可重复给多个",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        metavar="DIR",
        help="输出目录（必填；不默认写生产分区）",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        metavar="SYM",
        help="只要这些标的，可重复；不给=全要",
    )
    parser.add_argument(
        "--align-factor",
        type=float,
        default=None,
        metavar="F",
        help="混尺时全部交易日共用的对齐因子（只缩 amount）",
    )
    parser.add_argument(
        "--align-factor-by-date",
        default=None,
        metavar="YYYY-MM-DD=F,...",
        help="混尺时逐日给因子，例如 2026-01-05=0.5,2026-01-06=0.25",
    )
    parser.add_argument(
        "--align-raw-basis",
        action="store_true",
        help="声明以 amount 为准（不缩放；价才是被复权过的那一侧）",
    )
    parser.add_argument(
        "--sensitivity",
        action="store_true",
        help="额外输出 ε 敏感性列（_sens_ 前缀，不进主判定）",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="除 Parquet 外再写一份同内容 CSV",
    )
    return parser


def _parse_align_hint(args: argparse.Namespace) -> dict | None:
    """把三个 CLI 选项收成一个 `align_hint`。互斥由 argparse 之后的检查保证。"""
    given = [
        args.align_factor is not None,
        args.align_factor_by_date is not None,
        args.align_raw_basis,
    ]
    if sum(given) > 1:
        raise SystemExit(
            "--align-factor / --align-factor-by-date / --align-raw-basis 三选一"
        )
    if args.align_factor is not None:
        return {"factor": args.align_factor}
    if args.align_factor_by_date:
        pairs: dict[str, float] = {}
        for chunk in args.align_factor_by_date.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise SystemExit(f"--align-factor-by-date 里 '{chunk}' 缺 '='")
            day, _, factor = chunk.partition("=")
            pairs[day.strip()] = float(factor)
        if not pairs:
            raise SystemExit("--align-factor-by-date 没解析出任何日期")
        return {"factor_by_date": pairs}
    if args.align_raw_basis:
        return {"raw_basis": True}
    return None


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # 延迟导入：`--help` 不该依赖后端依赖装没装好
    from app.t_rounds.offline import (
        assert_version_matches_pin,
        build_offline_feature_frame,
        load_minute_export,
        run_mode_a_backtest,
        write_offline_report,
    )
    from app.t_rounds.offline.loader import MixedCaliberError

    align_hint = _parse_align_hint(args)
    paths = [Path(p) for p in args.minute]

    print(f"[hfeat] {DISCLAIMER}", file=sys.stderr)
    print(f"[hfeat] 读入 {len(paths)} 个文件", file=sys.stderr)
    try:
        loaded = load_minute_export(
            paths, align_hint=align_hint, symbols=args.symbol
        )
    except MixedCaliberError as exc:
        print(
            f"[hfeat] 混尺未对齐: {exc}\n"
            "        请用 --align-factor / --align-factor-by-date / --align-raw-basis "
            "显式给出引子（本脚本不会自行推测因子）",
            file=sys.stderr,
        )
        return 2

    if loaded.frame is None or loaded.frame.is_empty():
        print("[hfeat] 没有可用数据；跳过原因:", file=sys.stderr)
        for item in loaded.skipped:
            print(f"        - {item}", file=sys.stderr)
        return 3

    print(
        f"[hfeat] 交易日 {len(loaded.dates)} 个, bar {loaded.frame.height} 行, "
        f"口径 {loaded.caliber.note()}",
        file=sys.stderr,
    )

    features = build_offline_feature_frame(loaded, with_sensitivity=args.sensitivity)
    print(f"[hfeat] 特征表 {features.bars} 行", file=sys.stderr)
    print(f"[hfeat] 路径 A 命中 {features.path_a_hits}", file=sys.stderr)
    print(f"[hfeat] 路径 B 命中 {features.path_b_hits}", file=sys.stderr)

    summary = run_mode_a_backtest(features.frame)
    print(
        f"[hfeat] 回测 version={assert_version_matches_pin()} "
        f"ε={summary.epsilon_pct}%",
        file=sys.stderr,
    )
    for g in summary.groups:
        c = g.counts
        print(
            f"        {g.mode:8s} {g.path} 完成={c.completed} 未完成={c.incomplete} "
            f"已放弃={c.abandoned} 配额拒={c.skipped_quota} "
            f"已实现净额={g.realized_net_spread:.4f}",
            file=sys.stderr,
        )

    report = write_offline_report(
        out_dir=args.out_dir,
        feature_frame=features.frame,
        summary=summary,
        caliber=loaded.caliber,
        inputs=loaded.inputs,
        skipped=loaded.skipped,
        write_csv=args.csv,
    )
    print(f"[hfeat] 已写 {report.features_path}", file=sys.stderr)
    print(f"[hfeat] 已写 {report.summary_path}", file=sys.stderr)
    print(
        "[hfeat] 注意: 本产物仅为离线纸面结果; "
        "ε 门禁 / 可卖老股实盘 / 分档成本 / 正式推送闸均未测通",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
