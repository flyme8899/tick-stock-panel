"""S3 研究包 → 扩展表 ``kronos_pred`` 的只读导入。

上游产物（features.csv / predictions.csv / run_meta.json）**只读**：这里绝不写回
研究包目录，只在 ``data/ext_data/kronos_pred/`` 下按日分区落 parquet。

三条纪律：

1. **不改源**。导入前后对源文件算 sha256，写进批标记供门禁审计；一旦发现导入
   过程改动了源（实现 bug），抛错而不是悄悄继续。
2. **缺日不填 0**。某天没有数据就不写那个分区，缺口进 ``gaps`` 列表。用 0 填分布
   列等于凭空造出「完全一致 / 零分歧」的假信号。
3. **单点只作对照**。``pred_ret_h5/h20`` 从 predictions.csv 取，是可选列；缺就整列
   不出现，不补 None 假列。

导入完成写两份东西：

- 分区 parquet（``data/ext_data/kronos_pred/timeseries/date=YYYY-MM-DD/part.parquet``）
- 批成功标记（``data/kronos/last_batch.json``），这是 ``reachable`` 的离线判据。
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.custom.kronos import config
from app.custom.kronos.schema import (
    CONFIG_DESCRIPTION,
    CONFIG_ID,
    CONFIG_LABEL,
    distribution_field_names,
    optional_columns,
    primary_fields,
)

logger = logging.getLogger(__name__)

FEATURES_NAME = "features.csv"
PREDICTIONS_NAME = "predictions.csv"
RUN_META_NAME = "run_meta.json"

# 导入时按日分区，单日行数上限只做防御，不是业务约束。
MAX_ROWS_PER_PARTITION = 50_000


class KronosImportError(RuntimeError):
    """导入失败（缺必需列、源被改动等）。名字避开内置 ImportError。"""


@dataclass
class ImportResult:
    rows: int = 0
    dates: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    skipped_optional: list[str] = field(default_factory=list)
    source_sha: dict[str, str] = field(default_factory=dict)
    marker_path: str = ""

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "dates": self.dates,
            "gaps": self.gaps,
            "skipped_optional": self.skipped_optional,
            "source_sha": self.source_sha,
            "marker_path": self.marker_path,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        return header, list(reader)


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.lower() in {"nan", "none", "null", "na"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(raw: str | None) -> int | None:
    value = _to_float(raw)
    return None if value is None else int(value)


def load_predictions(path: Path) -> dict[tuple[str, str], dict[str, float | None]]:
    """``predictions.csv`` → {(symbol, date): {pred_ret_h5, pred_ret_h20}}。

    列名不强制：上游若只给 ``pred_ret`` 一列，按 horizon 后缀缺省处理（不猜）。
    """
    if not path.is_file():
        return {}
    header, rows = _read_csv(path)
    wanted = {column.name for column in optional_columns()}
    present = wanted & set(header)
    if not present:
        return {}
    out: dict[tuple[str, str], dict[str, float | None]] = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        day = (row.get("date") or "").strip()
        if not symbol or not day:
            continue
        out[(symbol, day)] = {name: _to_float(row.get(name)) for name in present}
    return out


def build_rows(
    features_path: Path,
    predictions_path: Path | None = None,
) -> tuple[list[dict], list[str]]:
    """把 ``features.csv`` 转成按日分组的行。返回 (rows, skipped_optional)。"""
    header, rows = _read_csv(features_path)
    required = set(primary_fields())
    missing = sorted(required - set(header))
    if missing:
        raise KronosImportError(f"{FEATURES_NAME} 缺必需列：{', '.join(missing)}")

    predictions = load_predictions(predictions_path) if predictions_path else {}
    optional_names = [column.name for column in optional_columns()]
    filled = [v for v in predictions.values() if v]
    present_optional = [name for name in optional_names if any(name in v for v in filled)]
    skipped_optional = [name for name in optional_names if name not in present_optional]

    out: list[dict] = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        day = (row.get("date") or "").strip()
        if not symbol or not day:
            # 键缺失的行直接跳过并记日志，不猜日期、不补代码。
            logger.warning("Kronos 导入跳过缺键行：symbol=%r date=%r", symbol, day)
            continue
        record: dict = {"date": day, "symbol": symbol}
        for name in distribution_field_names():
            record[name] = _to_float(row.get(name))
        record["n_paths"] = _to_int(row.get("n_paths"))
        record["seed"] = _to_int(row.get("seed"))
        extra = predictions.get((symbol, day), {})
        for name in present_optional:
            record[name] = extra.get(name)
        out.append(record)
    if len(out) > MAX_ROWS_PER_PARTITION * 400:
        raise KronosImportError(f"行数异常（{len(out)}），疑似文件选错")
    return out, skipped_optional


def _build_frame(records: list[dict], include_optional: bool):
    import polars as pl

    schema: dict = {
        "symbol": pl.Utf8,
        "date": pl.Utf8,
    }
    for name in distribution_field_names():
        schema[name] = pl.Float64
    schema["n_paths"] = pl.Int64
    schema["seed"] = pl.Int64
    if include_optional:
        for column in optional_columns():
            schema[column.name] = pl.Float64
    return pl.DataFrame(records, schema=schema, orient="row")


def ingest(
    package_dir: Path,
    *,
    data_dir: Path | None = None,
    write_marker: bool = True,
    expected_sha: dict[str, str] | None = None,
) -> ImportResult:
    """把研究包导入扩展表。只读 ``package_dir``，写 ``data_dir``。

    ``expected_sha`` 给了就校验（值不符抛错）；不给就只记录实际 sha，不假装核对过。
    """
    features = package_dir / FEATURES_NAME
    if not features.is_file():
        raise KronosImportError(f"缺少 {FEATURES_NAME}：{features}")

    result = ImportResult()
    before = {}
    for name in (FEATURES_NAME, PREDICTIONS_NAME, RUN_META_NAME):
        path = package_dir / name
        if path.is_file():
            before[name] = _sha256(path)
    result.source_sha = dict(before)

    if expected_sha:
        mismatched = [
            name for name, sha in expected_sha.items()
            if before.get(name) is not None and before[name] != sha
        ]
        if mismatched:
            raise KronosImportError(f"源文件 sha256 与预期不符：{', '.join(mismatched)}")

    records, skipped_optional = build_rows(
        features,
        package_dir / PREDICTIONS_NAME if (package_dir / PREDICTIONS_NAME).is_file() else None,
    )
    result.skipped_optional = skipped_optional

    include_optional = any(column.name not in skipped_optional for column in optional_columns())
    frame = _build_frame(records, include_optional)

    import polars as pl

    from app.custom.kronos.schema import schema_columns
    from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet

    target = Path(data_dir) if data_dir is not None else Path(_settings_data_dir())
    cfg = ExtConfig(
        id=CONFIG_ID,
        label=CONFIG_LABEL,
        mode="timeseries",
        fields=[ExtField(c.name, c.dtype, c.label) for c in schema_columns()],
        description=CONFIG_DESCRIPTION,
    )
    ExtConfigStore(target).upsert(cfg)

    days = sorted({record["date"] for record in records})
    written = 0
    for day in days:
        part = frame.filter(pl.col("date") == day).drop("date")
        if part.is_empty():
            result.gaps.append(day)
            continue
        written += write_ext_parquet(part, cfg, target, snapshot_date=_as_date(day))
        result.dates.append(day)
    result.rows = written

    # 源文件导入前后 sha 必须一致：这一步证明「只读引用」不是口号。
    for name, sha in before.items():
        if _sha256(package_dir / name) != sha:
            raise KronosImportError(f"导入过程改动了源文件 {name}，已中止")

    if write_marker:
        result.marker_path = str(_write_marker(result, target))
    logger.info("Kronos S3 导入完成：%s 行 / %s 天", result.rows, len(result.dates))
    return result


def _settings_data_dir() -> str:
    from app.config import settings

    return str(settings.data_dir)


def _as_date(day: str):
    from datetime import date

    try:
        return date.fromisoformat(day)
    except ValueError as exc:
        raise KronosImportError(f"日期格式不合规：{day!r}") from exc


def _write_marker(result: ImportResult, data_dir: Path) -> Path:
    """写批成功标记（``reachable`` 的离线判据）。"""
    target = config.data_root() if config.data_root_override() else Path(data_dir) / "kronos"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "last_batch.json"
    payload = {
        "finished_at": datetime.now(UTC).isoformat(),
        "config_id": CONFIG_ID,
        "rows": result.rows,
        "dates": result.dates,
        "source_sha": result.source_sha,
        "skipped_optional": result.skipped_optional,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
