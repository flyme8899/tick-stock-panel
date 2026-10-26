#!/usr/bin/env python3
"""把 Kronos S3 研究包导入扩展表 ``kronos_pred``（只读源文件）。

用法::

    # 预演：只报要写几天/几行、源 sha，不落盘
    python scripts/kronos_import_s3.py --package /path/to/kronos_pred_s3_mini_n20_20261021 --dry-run

    # 真导入（默认写 data/ext_data/kronos_pred/ 与 data/kronos/last_batch.json）
    python scripts/kronos_import_s3.py --package /path/to/包

    # 校验上游 sha（不给就只记录实际 sha，不假装核对过）
    python scripts/kronos_import_s3.py --package ... --expect features.csv=<sha>

语义边界：

- **不重训、不推理**。只搬既有产物。
- **不改源文件**。导入前后比对 sha，变了就中止。
- **缺日不填 0**。没有的交易日就报缺口。
- 本机研究包不在仓里；脚本只认传入的目录，不猜默认路径。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_ROOT / "backend"))


def _parse_expect(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        name, _, sha = item.partition("=")
        if name and sha:
            out[name.strip()] = sha.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kronos S3 → 扩展表 kronos_pred（只读源）")
    parser.add_argument("--package", required=True, help="S3 研究包目录（含 features.csv）")
    parser.add_argument("--data-dir", default="", help="TSP 数据目录，默认取 settings.data_dir")
    parser.add_argument(
        "--expect",
        action="append",
        metavar="FILE=SHA256",
        help="校验源文件 sha256，可重复",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    from app.custom.kronos import ingest
    from app.custom.kronos.schema import CONFIG_ID

    package = Path(args.package).expanduser()
    if not package.is_dir():
        print(f"研究包目录不存在：{package}", file=sys.stderr)
        return 2

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None

    if args.dry_run:
        features = package / ingest.FEATURES_NAME
        if not features.is_file():
            print(f"缺少 {ingest.FEATURES_NAME}：{features}", file=sys.stderr)
            return 2
        records, skipped = ingest.build_rows(
            features,
            package / ingest.PREDICTIONS_NAME
            if (package / ingest.PREDICTIONS_NAME).is_file()
            else None,
        )
        days = sorted({row["date"] for row in records})
        payload = {
            "config_id": CONFIG_ID,
            "rows": len(records),
            "dates": days,
            "skipped_optional": skipped,
            "source_sha": {
                name: ingest._sha256(package / name)
                for name in (ingest.FEATURES_NAME, ingest.PREDICTIONS_NAME, ingest.RUN_META_NAME)
                if (package / name).is_file()
            },
            "dry_run": True,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else _summary(payload))
        return 0

    try:
        result = ingest.ingest(
            package,
            data_dir=data_dir,
            expected_sha=_parse_expect(args.expect) or None,
        )
    except ingest.KronosImportError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1

    payload = result.to_dict()
    payload["config_id"] = CONFIG_ID
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else _summary(payload))
    if result.gaps:
        print("缺口（未写入的分区，不填 0）：" + ", ".join(result.gaps), file=sys.stderr)
    return 0


def _summary(payload: dict) -> str:
    lines = [
        f"配置 {payload['config_id']}：{payload['rows']} 行 / {len(payload['dates'])} 天",
        "日期：" + ", ".join(payload["dates"]),
    ]
    if payload.get("skipped_optional"):
        lines.append("未导入的可选列：" + ", ".join(payload["skipped_optional"]))
    if payload.get("source_sha"):
        lines.append("源 sha256：")
        lines.extend(f"  {name} {sha}" for name, sha in payload["source_sha"].items())
    if payload.get("marker_path"):
        lines.append("批成功标记：" + payload["marker_path"])
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
