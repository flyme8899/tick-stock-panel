"""扩展表 schema、S3 导入、registry 暴露与门禁门闩（G-T5 / G-T6 / G-T7）。

重点：

- **G-T5** 列齐（agree/dir_entropy/p90_p10/std/mean/median × h5/h20），导入后
  registry 可见 ``ext_kronos_pred_*``，且**默认不在信号默认集**（门禁未过）。
- **G-T6** 对裁剪夹具 ingest 成功，**源文件 sha 不变**，缺日不填 0。
- **G-T7** 无门禁记录 → 把它加进信号条件被拒，且有明确原因。
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.custom.kronos import config, gates, ingest, schema
from app.factors import ext_factors
from app.factors.registry import all_factors

_HEADER = [
    "date", "symbol", "n_paths", "seed",
    "agree_h5", "dir_entropy_h5", "p90_p10_h5", "std_h5", "mean_h5", "median_h5",
    "agree_h20", "dir_entropy_h20", "p90_p10_h20", "std_h20", "mean_h20", "median_h20",
]

_ROWS = [
    ["2026-10-20", "600519.SH", "20", "7", "0.65", "0.42", "0.031", "0.012", "0.004", "0.003",
     "0.60", "0.51", "0.058", "0.021", "0.007", "0.006"],
    ["2026-10-20", "000001.SZ", "20", "7", "0.55", "0.58", "0.028", "0.011", "0.001", "0.001",
     "0.52", "0.62", "0.049", "0.019", "0.002", "0.002"],
    ["2026-10-22", "600519.SH", "20", "7", "0.70", "0.35", "0.034", "0.013", "0.006", "0.005",
     "0.63", "0.47", "0.061", "0.023", "0.009", "0.008"],
]


def _read_part(data_dir: Path, day: str) -> pl.DataFrame:
    return pl.read_parquet(
        data_dir / "ext_data" / schema.CONFIG_ID / "timeseries" / f"date={day}" / "part.parquet"
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """裁剪过的 S3 夹具：两天有数据，10-21 故意缺（测缺口不填 0）。"""
    root = tmp_path / "pkg"
    root.mkdir()
    with (root / "features.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_HEADER)
        writer.writerows(_ROWS)
    with (root / "predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "symbol", "pred_ret_h5", "pred_ret_h20"])
        writer.writerows([
            ["2026-10-20", "600519.SH", "0.004", "0.007"],
            ["2026-10-22", "600519.SH", "0.006", "0.009"],
        ])
    (root / "run_meta.json").write_text(json.dumps({"model": "kronos-mini"}), encoding="utf-8")
    return root


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", target)
    monkeypatch.setenv("KRONOS_DATA_DIR", str(target / "kronos"))
    _reset_ext_state()
    yield target
    _reset_ext_state()


def _reset_ext_state() -> None:
    from app.services.ext_data import _load_all_cache

    _load_all_cache.clear()
    ext_factors._frame_cache.clear()
    ext_factors._sync_state = None
    from app.factors import registry as _registry

    for fid in [k for k in list(_registry._REGISTRY) if k.startswith(ext_factors.EXT_PREFIX)]:
        _registry.unregister_factor(fid)


# ── G-T5 schema ────────────────────────────────────────

def test_distribution_columns_cover_s3() -> None:
    names = set(schema.distribution_field_names())

    for metric in ("agree", "dir_entropy", "p90_p10", "std", "mean", "median"):
        for horizon in (5, 20):
            assert f"{metric}_h{horizon}" in names
    assert len(names) == 12


def test_all_column_names_are_ascii() -> None:
    """中文字段名会被 registry 跳过 DSL 注册，这里必须全 ASCII。"""
    for column in schema.schema_columns():
        assert column.name.isascii(), column.name


def test_optional_columns_are_marked_research_only() -> None:
    payload = schema.schema_payload()

    assert payload["optional_fields"] == ["pred_ret_h5", "pred_ret_h20"]
    assert "研究对照" in payload["note"]


def test_primary_fields_exclude_pred_ret() -> None:
    """主列不能含单点预测——点预测不当 alpha。"""
    assert not any(name.startswith("pred_ret") for name in schema.primary_fields())


def test_registry_exposes_kronos_family(package: Path, data_dir: Path) -> None:
    ingest.ingest(package)

    ids = {spec.id for spec in all_factors() if gates.is_kronos_factor(spec.id)}

    assert len(ids) == 16
    assert "ext_kronos_pred_agree_h5" in ids
    assert "ext_kronos_pred_median_h20" in ids


def test_timeseries_mode_and_float_dtype(package: Path, data_dir: Path) -> None:
    ingest.ingest(package)

    cfg_path = data_dir / "ext_data" / schema.CONFIG_ID / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    by_name = {f["name"]: f["dtype"] for f in cfg["fields"]}

    assert cfg["mode"] == "timeseries"
    assert by_name["agree_h5"] == "float"
    assert by_name["n_paths"] == "int"


# ── G-T6 导入 ──────────────────────────────────────────

def test_ingest_writes_one_partition_per_day(package: Path, data_dir: Path) -> None:
    result = ingest.ingest(package)

    assert result.rows == 3
    assert result.dates == ["2026-10-20", "2026-10-22"]
    base = data_dir / "ext_data" / schema.CONFIG_ID / "timeseries"
    parts = sorted(base.glob("date=*/part.parquet"))
    assert len(parts) == 2


def test_ingest_does_not_touch_source(package: Path, data_dir: Path) -> None:
    names = ("features.csv", "predictions.csv", "run_meta.json")
    before = {name: _sha(package / name) for name in names}

    result = ingest.ingest(package)

    after = {name: _sha(package / name) for name in before}
    assert after == before, "导入改动了源文件"
    assert result.source_sha == before


def test_ingest_records_expected_sha_mismatch(package: Path, data_dir: Path) -> None:
    with pytest.raises(ingest.KronosImportError, match="sha256"):
        ingest.ingest(package, expected_sha={"features.csv": "0" * 64})


def test_ingest_reports_missing_days_as_gaps(package: Path, data_dir: Path) -> None:
    """10-21 上游缺：不写分区、不进 0，缺口单独报。"""
    result = ingest.ingest(package)

    assert "2026-10-21" not in result.dates
    gap_dir = data_dir / "ext_data" / schema.CONFIG_ID / "timeseries" / "date=2026-10-21"
    assert not gap_dir.exists(), "缺的日期不该有分区"


def test_ingest_without_predictions_skips_optional(tmp_path: Path, data_dir: Path) -> None:
    root = tmp_path / "no_pred"
    root.mkdir()
    with (root / "features.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_HEADER)
        writer.writerows(_ROWS)

    result = ingest.ingest(root)

    assert set(result.skipped_optional) == {"pred_ret_h5", "pred_ret_h20"}
    frame = _read_part(data_dir, "2026-10-20")
    assert "pred_ret_h5" not in frame.columns


def test_ingest_rejects_missing_required_column(tmp_path: Path, data_dir: Path) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    with (root / "features.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "symbol", "agree_h5"])
        writer.writerow(["2026-10-20", "600519.SH", "0.6"])

    with pytest.raises(ingest.KronosImportError, match="缺必需列"):
        ingest.ingest(root)


def test_ingest_writes_batch_marker(package: Path, data_dir: Path) -> None:
    result = ingest.ingest(package)

    marker = Path(result.marker_path)
    assert marker.is_file()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["rows"] == 3
    assert payload["config_id"] == schema.CONFIG_ID
    assert payload["source_sha"]


def test_blank_values_stay_null_not_zero(tmp_path: Path, data_dir: Path) -> None:
    """上游空值不填 0：分布列空就是空。"""
    root = tmp_path / "blank"
    root.mkdir()
    row = list(_ROWS[0])
    row[_HEADER.index("agree_h5")] = ""
    with (root / "features.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_HEADER)
        writer.writerow(row)

    ingest.ingest(root)

    frame = _read_part(data_dir, "2026-10-20")
    assert frame["agree_h5"][0] is None, "空值被填成了数字"


# ── G-T7 门禁门闩 ─────────────────────────────────────

def test_kronos_factors_blocked_without_gate(package: Path, data_dir: Path) -> None:
    ingest.ingest(package)
    ids = [spec.id for spec in all_factors() if gates.is_kronos_factor(spec.id)]

    with pytest.raises(gates.KronosGateError) as excinfo:
        gates.assert_allowed_in_signals(ids)

    message = str(excinfo.value)
    assert "未过门禁" in message
    assert "费后检验" in message


def test_non_kronos_factors_pass_through() -> None:
    gates.assert_allowed_in_signals(["close", "ma20", "ext_tags_hot"])


def test_filter_signal_fields_drops_kronos_without_gate(data_dir: Path) -> None:
    fields = ["close", "ext_kronos_pred_agree_h5", "ext_tags_hot"]

    assert gates.filter_signal_fields(fields) == frozenset({"close", "ext_tags_hot"})


def test_gate_passed_allows_only_downgrade_usage(data_dir: Path) -> None:
    (data_dir / "kronos").mkdir(parents=True, exist_ok=True)
    (data_dir / "kronos" / "gate.json").write_text(
        json.dumps({
            "passed": True,
            "cost_after_tax": "买 6.6bp / 卖 11.6bp",
            "out_of_sample": "walkforward 记录",
            "risk_downgrade_only": True,
            "usage": "risk_downgrade_only",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        }),
        encoding="utf-8",
    )

    assert config.gate_passed()[0] is True
    gates.assert_allowed_in_signals(["ext_kronos_pred_agree_h5"])
    kept = gates.filter_signal_fields(["ext_kronos_pred_agree_h5"])
    assert kept == frozenset({"ext_kronos_pred_agree_h5"})


def test_gate_upgrade_usage_still_blocked(data_dir: Path) -> None:
    """即便 passed=true，写成加码用途也不放行。"""
    (data_dir / "kronos").mkdir(parents=True, exist_ok=True)
    (data_dir / "kronos" / "gate.json").write_text(
        json.dumps({
            "passed": True,
            "cost_after_tax": "买 6.6bp",
            "out_of_sample": "记录",
            "risk_downgrade_only": True,
            "usage": "add_position",
        }),
        encoding="utf-8",
    )

    with pytest.raises(gates.KronosGateError):
        gates.assert_allowed_in_signals(["ext_kronos_pred_agree_h5"])


def test_gate_is_not_wired_into_bss(package: Path, data_dir: Path) -> None:
    """门闩只在「别人来接」时拦，不往 BSS 写东西。"""
    import inspect

    from app.custom.kronos import gates as module

    source = inspect.getsource(module)
    assert "buy_sell_signals" in source  # 只在文档串/提示里提到
    assert "import buy_sell_signals" not in source
    assert "from app.buy_sell_signals" not in source
