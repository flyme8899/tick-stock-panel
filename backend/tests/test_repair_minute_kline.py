"""分钟 K 修复: 默认不改文件；执行时备份、转换、回滚；写入中则拒绝。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "repair_minute_kline.py"
_SPEC = importlib.util.spec_from_file_location("repair_minute_kline", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
repair_minute_kline = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = repair_minute_kline
_SPEC.loader.exec_module(repair_minute_kline)

_CN = timezone(timedelta(hours=8))
_NOW = datetime(2026, 10, 9, 15, 0, tzinfo=_CN)
_TODAY = _NOW.date()
_IDLE = repair_minute_kline.WriterStatus(False, False, [], [], "http://test")


def _frame(*, day: int, month: int = 9, year: int = 2026, volume_dtype: pl.DataType, rows: int = 2) -> pl.DataFrame:
    stamps = [datetime(year, month, day, 9, 31 + i) for i in range(rows)]
    symbols = ["000001.SZ", "600000.SH"] * ((rows + 1) // 2)
    symbols = symbols[:rows]
    return pl.DataFrame(
        {
            "symbol": symbols,
            "datetime": pl.Series(stamps, dtype=pl.Datetime("us")),
            "open": [1.0] * rows,
            "high": [1.2] * rows,
            "low": [0.9] * rows,
            "close": [1.1] * rows,
            "volume": pl.Series([100 + i for i in range(rows)], dtype=volume_dtype),
            "amount": pl.Series([10.0] * rows, dtype=pl.Float64),
        }
    )


def _write(root: Path, day: str, frame: pl.DataFrame) -> Path:
    directory = root / f"date={day}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "part.parquet"
    frame.write_parquet(path)
    return path


def _snapshot(root: Path) -> dict[str, int]:
    return {
        str(path.relative_to(root)): path.stat().st_mtime_ns
        for path in root.rglob("*")
        if path.is_file()
    }


def _run(root: Path, extra: list[str] | None = None, **kwargs):
    argv = ["--path", str(root), *(extra or [])]
    defaults = {
        "writer": _IDLE,
        "today": _TODAY,
        "now": _NOW,
        "batch": 100,
        "rpm": 24,
        "segment_days": 20,
    }
    defaults.update(kwargs)
    return repair_minute_kline.main(argv, **defaults)


def test_dry_run_prints_plan_and_touches_nothing(tmp_path: Path, capsys) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-01", _frame(day=1, volume_dtype=pl.Float64))
    bad = _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))
    before = _snapshot(tmp_path)

    code = _run(root)
    text = capsys.readouterr().out

    assert code == 0
    assert "不修改数据" in text
    assert "date=2026-09-02" in text
    assert "计划: 重拉" in text
    assert f"文件 {bad.stat().st_size} 字节" in text
    assert "行 2" in text
    assert "股票 2" in text
    assert "kline_minute_repair_" in text
    assert "sync_minute_batch" in text
    assert "仅转换类型补不回" in text or "补不回" in text
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "backup").exists()


def test_apply_refetch_backs_up_and_leaves_float(tmp_path: Path, capsys) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-01", _frame(day=1, volume_dtype=pl.Float64))
    original = _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))
    original_bytes = original.read_bytes()
    seen: list[repair_minute_kline.RefetchRequest] = []

    def refetch(request) -> None:
        seen.append(request)
        _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Float64, rows=4))

    code = _run(root, ["--apply", "--yes"], refetch=refetch)
    text = capsys.readouterr().out

    assert code == 0
    assert seen and seen[0].dates == ["2026-09-02"]
    assert seen[0].symbols == ["000001.SZ", "600000.SH"]
    assert seen[0].rpm == 24
    assert "结论: OK" in text
    assert "重拉" in text
    live = root / "date=2026-09-02" / "part.parquet"
    assert pl.read_parquet_schema(live)["volume"] == pl.Float64
    assert pl.read_parquet(live).height == 4
    backups = list((tmp_path / "backup").glob("kline_minute_repair_*/date=2026-09-02/part.parquet"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes
    assert pl.read_parquet_schema(backups[0])["volume"] == pl.Int64
    displaced = list((tmp_path / "backup").glob("kline_minute_repair_*/displaced/date=2026-09-02/part.parquet"))
    assert len(displaced) == 1
    assert displaced[0].read_bytes() == original_bytes
    untouched = root / "date=2026-09-01" / "part.parquet"
    assert pl.read_parquet_schema(untouched)["volume"] == pl.Float64


def test_apply_casts_when_refetch_writes_nothing_and_rollback_restores(tmp_path: Path, capsys) -> None:
    root = tmp_path / "kline_minute"
    original = _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))
    original_bytes = original.read_bytes()

    code = _run(root, ["--apply", "--yes"], refetch=lambda request: None)
    text = capsys.readouterr().out

    assert code == 0
    assert "仅转换类型" in text
    assert "结论: OK" in text
    live = root / "date=2026-09-02" / "part.parquet"
    assert live.is_file()
    assert pl.read_parquet_schema(live)["volume"] == pl.Float64
    assert pl.read_parquet(live).height == 2
    backup_dirs = list((tmp_path / "backup").glob("kline_minute_repair_*"))
    assert len(backup_dirs) == 1
    assert (backup_dirs[0] / "date=2026-09-02" / "part.parquet").read_bytes() == original_bytes

    code = _run(root, ["--rollback", str(backup_dirs[0])])
    text = capsys.readouterr().out
    assert code == 1
    assert "已恢复 date=2026-09-02" in text
    assert "结论: 有问题" in text
    assert live.read_bytes() == original_bytes
    assert pl.read_parquet_schema(live)["volume"] == pl.Int64
    assert (backup_dirs[0] / "date=2026-09-02" / "part.parquet").read_bytes() == original_bytes


def test_old_date_is_cast_without_calling_refetch(tmp_path: Path, capsys) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2024-01-02", _frame(day=2, month=1, year=2024, volume_dtype=pl.Int64))
    called = {"n": 0}

    def refetch(request) -> None:
        called["n"] += 1

    code = _run(root, ["--apply", "--yes"], refetch=refetch)
    text = capsys.readouterr().out

    assert code == 0
    assert called["n"] == 0
    assert "计划: 仅转换类型" in text
    assert "超过 365 天" in text
    live = root / "date=2024-01-02" / "part.parquet"
    assert pl.read_parquet_schema(live)["volume"] == pl.Float64
    assert pl.read_parquet(live).height == 2


def test_refuse_when_writer_active_or_unknown(tmp_path: Path, capsys) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))
    before = _snapshot(tmp_path)
    active = repair_minute_kline.WriterStatus(
        True, False, ["job_store abc 状态 running，1 秒内有更新"], [], "http://test"
    )

    code = _run(root, ["--apply", "--yes"], writer=active)
    text = capsys.readouterr().out
    assert code == 3
    assert "已拒绝执行" in text
    assert _snapshot(tmp_path) == before

    unknown = repair_minute_kline.WriterStatus(False, True, ["无法确认分钟刷新状态"], [], "http://test")
    code = _run(root, ["--apply", "--yes"], writer=unknown)
    assert code == 3
    assert _snapshot(tmp_path) == before

    code = _run(root, ["--apply", "--yes", "--force"], writer=active, refetch=lambda request: None)
    text = capsys.readouterr().out
    assert code == 0
    assert "跳过写入检查" in text
    assert pl.read_parquet_schema(root / "date=2026-09-02" / "part.parquet")["volume"] == pl.Float64


def test_resume_after_interrupted_refetch(tmp_path: Path, capsys) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))

    def boom(request) -> None:
        raise RuntimeError("interrupted")

    code = _run(root, ["--apply", "--yes"], refetch=boom)
    text = capsys.readouterr().out
    assert code == 1
    live = root / "date=2026-09-02" / "part.parquet"
    assert live.is_file()
    assert pl.read_parquet_schema(live)["volume"] == pl.Float64
    assert pl.read_parquet(live).height == 2
    assert "继续" in text
    status_files = list((tmp_path / "backup").glob("kline_minute_repair_*/status.json"))
    assert len(status_files) == 1
    status = json.loads(status_files[0].read_text(encoding="utf-8"))
    assert status["dates"]["2026-09-02"]["state"] == "provisional"

    def refill(request) -> None:
        _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Float64, rows=4))

    code = _run(root, ["--apply", "--yes"], refetch=refill)
    text = capsys.readouterr().out
    assert code == 0
    assert pl.read_parquet(live).height == 4
    assert "重拉" in text
    status = json.loads(status_files[0].read_text(encoding="utf-8"))
    assert status["dates"]["2026-09-02"]["state"] == "done"
    assert status["dates"]["2026-09-02"]["mode"] == "refetch"


def test_dates_override_leaves_other_partitions(tmp_path: Path) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))
    other = _write(root, "2026-09-03", _frame(day=3, volume_dtype=pl.Int64))
    before = other.stat().st_mtime_ns

    code = _run(root, ["--apply", "--yes", "--dates", "2026-09-02"], refetch=lambda request: None)

    assert code == 1
    assert pl.read_parquet_schema(root / "date=2026-09-02" / "part.parquet")["volume"] == pl.Float64
    assert other.stat().st_mtime_ns == before
    assert pl.read_parquet_schema(other)["volume"] == pl.Int64


def test_detect_writers_reads_job_files_without_jobstore(tmp_path: Path) -> None:
    job_dir = tmp_path / "job_store"
    job_dir.mkdir()
    running = job_dir / "abc.json"
    running.write_text(json.dumps({"id": "abc", "status": "running"}), encoding="utf-8")
    stale = job_dir / "old.json"
    stale.write_text(json.dumps({"id": "old", "status": "pending"}), encoding="utf-8")
    now_ts = time.time()
    old_ts = now_ts - 7 * 3600
    os.utime(stale, (old_ts, old_ts))

    status = repair_minute_kline.detect_writers(
        tmp_path,
        fetch=lambda url: {"running": False},
        now_ts=now_ts,
    )
    assert status.active is True
    assert status.http_unknown is False
    assert any("abc" in item for item in status.reasons)
    assert any("old" in item for item in status.stale)

    def _down(url: str) -> dict:
        raise OSError("down")

    quiet = repair_minute_kline.detect_writers(tmp_path / "empty", fetch=_down)
    assert quiet.active is False
    assert quiet.http_unknown is True

    refreshing = repair_minute_kline.detect_writers(
        tmp_path / "empty",
        fetch=lambda url: {"available": True, "running": True},
    )
    assert refreshing.active is True


def test_cancel_before_confirm_touches_nothing(tmp_path: Path) -> None:
    root = tmp_path / "kline_minute"
    _write(root, "2026-09-02", _frame(day=2, volume_dtype=pl.Int64))
    before = _snapshot(tmp_path)

    code = _run(root, ["--apply"], confirm=lambda: False)

    assert code == 2
    assert _snapshot(tmp_path) == before
