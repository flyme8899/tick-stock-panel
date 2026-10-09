"""只读分钟 K 诊断: 混合 Float64 / Int64 分区。"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import polars as pl

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_minute_kline.py"
_SPEC = importlib.util.spec_from_file_location("check_minute_kline", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_minute_kline = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check_minute_kline
_SPEC.loader.exec_module(check_minute_kline)


def _frame(*, day: int, volume_dtype: pl.DataType, duplicate: bool = False) -> pl.DataFrame:
    stamps = [datetime(2026, 9, day, 9, 31), datetime(2026, 9, day, 9, 32)]
    symbols = ["000001.SZ", "600000.SH"]
    if duplicate:
        stamps.append(stamps[0])
        symbols.append(symbols[0])
    volume = [100, 110] if not duplicate else [100, 110, 100]
    amount = [10.0, 11.0] if not duplicate else [10.0, 11.0, 10.0]
    size = len(symbols)
    return pl.DataFrame(
        {
            "symbol": symbols,
            "datetime": pl.Series(stamps, dtype=pl.Datetime("us")),
            "open": [1.0] * size,
            "high": [1.2] * size,
            "low": [0.9] * size,
            "close": [1.1] * size,
            "volume": pl.Series(volume, dtype=volume_dtype),
            "amount": pl.Series(amount, dtype=pl.Float64),
        }
    )


def _write(root: Path, day: str, frame: pl.DataFrame) -> Path:
    directory = root / f"date={day}"
    directory.mkdir(parents=True)
    path = directory / "part.parquet"
    frame.write_parquet(path)
    return path


def _snapshot(root: Path) -> dict[str, int]:
    return {
        str(path.relative_to(root)): path.stat().st_mtime_ns
        for path in root.rglob("*")
        if path.is_file()
    }


def test_clean_store_is_ok(tmp_path: Path) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-01", _frame(day=1, volume_dtype=pl.Float64))
    before = _snapshot(root)

    report = check_minute_kline.check_store(root)

    assert report.ok is True
    assert report.scan_ok is True
    assert report.partitions[0].rows == 2
    assert report.partitions[0].stocks == 2
    assert report.partitions[0].duplicate_keys == 0
    assert _snapshot(root) == before
    text = check_minute_kline.format_report(report)
    assert "结论: OK" in text
    assert "有问题" not in text.split("结论:", 1)[1]


def test_mixed_dtype_and_duplicates_are_reported(tmp_path: Path, capsys) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-01", _frame(day=1, volume_dtype=pl.Float64))
    _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))
    _write(root, "2026-09-03", _frame(day=3, volume_dtype=pl.Float64, duplicate=True))
    (root / "date=2026-09-01" / "part.parquet.tmp").write_bytes(b"not-used")
    before = _snapshot(root)

    report = check_minute_kline.check_store(root)

    assert report.ok is False
    assert report.scan_ok is False
    assert "Int64" in report.scan_error
    by_date = {item.date: item for item in report.partitions}
    assert by_date["2026-09-01"].ok is True
    assert any("volume=Int64" in item for item in by_date["2026-09-02"].problems)
    assert by_date["2026-09-03"].duplicate_keys == 1
    assert by_date["2026-09-03"].extra_rows == 1
    assert report.tmp_files == ["part.parquet.tmp"]
    assert _snapshot(root) == before

    code = check_minute_kline.main(["--path", str(root)])
    text = capsys.readouterr().out
    assert code == 1
    assert "结论: 有问题" in text
    assert "2026-09-02" in text
    assert "2026-09-03" in text
    assert "本脚本不会执行" in text
    assert "sync_minute" in text
    assert _snapshot(root) == before


def test_missing_directory_exits_2(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope"
    code = check_minute_kline.main(["--path", str(missing)])
    text = capsys.readouterr().out
    assert code == 2
    assert "结论: 有问题" in text
    assert "目录不存在" in text


def test_default_root_follows_settings(monkeypatch, tmp_path: Path) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert check_minute_kline.default_minute_root() == tmp_path / "kline_minute"
