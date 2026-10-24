"""因子回测运行记录。

每次单因子或批量检验落一条 run 到 ``data/research/factor_backtest/runs``，
结构与挖掘 run 一样是目录 + JSON。列表只读 manifest，完整 IC / 分层结果在
result.json。新文件的属主对齐 data 目录，避免 root 写入后和现有数据不一致。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.services.json_safe import json_safe as _json_safe_shared

logger = logging.getLogger(__name__)

MAX_RETAINED_RUNS = 200
MAX_LIST_LIMIT = 200
RUN_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "interrupted"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = RUN_STATUSES - ACTIVE_STATUSES
_RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_LOCK = threading.RLock()
_SCHEMA_VERSION = 1


class FactorRunStoreError(RuntimeError):
    pass


class FactorRunValidationError(FactorRunStoreError, ValueError):
    pass


class FactorRunBusyError(FactorRunStoreError):
    pass


def json_safe(value: Any) -> Any:
    """本模块专用: 走 `coerce=True` 语义 (结果直接落盘 / 直出 json.dumps, 无上层编码)。"""
    return _json_safe_shared(value, coerce=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise FactorRunStoreError(f"failed to write {path.name}") from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def align_owner(path: Path, reference: Path) -> None:
    """把新建文件的 uid/gid 对齐到 data 目录。无权限时保持进程属主。"""
    try:
        ref = reference.stat()
    except OSError:
        return
    targets = [path]
    if path.is_dir():
        targets.extend(path.rglob("*"))
    for current in targets:
        try:
            st = current.stat()
            if st.st_uid == ref.st_uid and st.st_gid == ref.st_gid:
                continue
            os.chown(current, ref.st_uid, ref.st_gid)
        except OSError:
            continue


class FactorBacktestRunStore:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.runs_root = (self.data_dir / "research" / "factor_backtest" / "runs").resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        align_owner(self.runs_root, self.data_dir)
        # 父目录也是这次可能新建的，一并对齐。
        align_owner(self.runs_root.parent, self.data_dir)

    def create(self, kind: str, request: Mapping[str, Any], username: str | None) -> dict[str, Any]:
        if kind not in {"factor", "batch"}:
            raise FactorRunValidationError(f"unsupported factor run kind: {kind}")
        run_id = uuid.uuid4().hex
        now = _now_iso()
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "run_id": run_id,
            "kind": kind,
            "status": "queued",
            "username": username or None,
            "request": json_safe(dict(request)),
            "summary": None,
            "error": None,
            "elapsed_ms": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        }
        with _LOCK:
            run_dir = self._run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=False)
            _atomic_write_json(run_dir / "manifest.json", manifest)
            align_owner(run_dir, self.data_dir)
            self._prune_locked()
        return manifest

    def get(self, run_id: str, *, include_result: bool = False) -> dict[str, Any] | None:
        safe_id = self._validate_run_id(run_id)
        with _LOCK:
            manifest = self._read_manifest(safe_id)
            if manifest is None or not include_result:
                return manifest
            loaded = dict(manifest)
            loaded["result"] = self._read_result(safe_id)
            return loaded

    def list_runs(self, *, limit: int = MAX_LIST_LIMIT) -> list[dict[str, Any]]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise FactorRunValidationError("limit must be between 1 and 200")
        with _LOCK:
            manifests = [item for item in self._scan_locked() if item is not None]
        manifests.sort(key=_sort_key, reverse=True)
        return manifests[:limit]

    def delete(self, run_id: str) -> None:
        safe_id = self._validate_run_id(run_id)
        with _LOCK:
            manifest = self._read_manifest(safe_id)
            if manifest is None:
                raise KeyError(safe_id)
            if manifest["status"] in ACTIVE_STATUSES:
                raise FactorRunBusyError("进行中的因子回测不能删除")
            shutil.rmtree(self._run_dir(safe_id))

    def transition(self, run_id: str, status: str, *, error: str | None = None) -> dict[str, Any]:
        if status not in RUN_STATUSES:
            raise FactorRunValidationError(f"unsupported factor run status: {status}")
        safe_id = self._validate_run_id(run_id)
        with _LOCK:
            manifest = self._require(safe_id)
            current = manifest["status"]
            if current == status:
                return manifest
            if current not in ACTIVE_STATUSES or status == "queued":
                raise FactorRunValidationError(f"cannot transition from {current} to {status}")
            now = _now_iso()
            manifest["status"] = status
            if status == "running" and not manifest.get("started_at"):
                manifest["started_at"] = now
            if status in TERMINAL_STATUSES:
                manifest["finished_at"] = now
            if error is not None:
                manifest["error"] = error
            _atomic_write_json(self._run_dir(safe_id) / "manifest.json", manifest)
            align_owner(self._run_dir(safe_id), self.data_dir)
            return manifest

    def complete(
        self, run_id: str, result: Mapping[str, Any], *, elapsed_ms: float
    ) -> dict[str, Any]:
        safe_id = self._validate_run_id(run_id)
        safe_result = json_safe(dict(result))
        if not isinstance(safe_result, dict):
            raise FactorRunValidationError("factor run result must be a mapping")
        with _LOCK:
            manifest = self._require(safe_id)
            if manifest["status"] != "running":
                return manifest
            run_dir = self._run_dir(safe_id)
            _atomic_write_json(run_dir / "result.json", safe_result)
            now = _now_iso()
            manifest["status"] = "succeeded"
            manifest["finished_at"] = now
            manifest["elapsed_ms"] = elapsed_ms
            manifest["summary"] = _summary(manifest["kind"], safe_result)
            manifest["error"] = None
            _atomic_write_json(run_dir / "manifest.json", manifest)
            align_owner(run_dir, self.data_dir)
            return manifest

    def fail(self, run_id: str, exc: BaseException, *, elapsed_ms: float) -> None:
        message = str(exc).strip() or exc.__class__.__name__
        if len(message) > 4000:
            message = message[:4000]
        safe_id = self._validate_run_id(run_id)
        with _LOCK:
            manifest = self._read_manifest(safe_id)
            if manifest is None or manifest["status"] not in ACTIVE_STATUSES:
                return
            now = _now_iso()
            manifest["status"] = "failed"
            manifest["finished_at"] = now
            manifest["elapsed_ms"] = elapsed_ms
            manifest["error"] = message
            _atomic_write_json(self._run_dir(safe_id) / "manifest.json", manifest)
            align_owner(self._run_dir(safe_id), self.data_dir)

    def recover_interrupted(self) -> int:
        """进程重启后，没有对应线程的排队/运行中记录标为已中断。"""
        recovered = 0
        with _LOCK:
            for manifest in self._scan_locked():
                if manifest is None or manifest["status"] not in ACTIVE_STATUSES:
                    continue
                now = _now_iso()
                manifest["status"] = "interrupted"
                manifest["finished_at"] = now
                manifest["error"] = manifest.get("error") or "服务重启，任务已中断"
                _atomic_write_json(self._run_dir(manifest["run_id"]) / "manifest.json", manifest)
                recovered += 1
        return recovered

    def _prune_locked(self) -> None:
        manifests = [item for item in self._scan_locked() if item is not None]
        if len(manifests) <= MAX_RETAINED_RUNS:
            return
        manifests.sort(key=_sort_key)
        extra = len(manifests) - MAX_RETAINED_RUNS
        for manifest in manifests:
            if extra <= 0:
                break
            if manifest["status"] in ACTIVE_STATUSES:
                continue
            shutil.rmtree(self._run_dir(manifest["run_id"]), ignore_errors=True)
            extra -= 1

    def _scan_locked(self) -> list[dict[str, Any] | None]:
        try:
            paths = list(self.runs_root.glob("*/manifest.json"))
        except OSError as exc:
            raise FactorRunStoreError("failed to scan factor backtest runs") from exc
        found: list[dict[str, Any] | None] = []
        for path in paths:
            run_id = path.parent.name
            if not _RUN_ID_PATTERN.fullmatch(run_id):
                continue
            try:
                found.append(self._read_manifest(run_id))
            except FactorRunStoreError:
                logger.warning("skip corrupt factor backtest run %s", run_id)
                continue
        return found

    def _read_manifest(self, run_id: str) -> dict[str, Any] | None:
        path = self._run_dir(run_id) / "manifest.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FactorRunStoreError(f"invalid manifest for {run_id}") from exc
        if not isinstance(value, dict) or value.get("status") not in RUN_STATUSES:
            raise FactorRunStoreError(f"invalid manifest for {run_id}")
        value["run_id"] = run_id
        value.pop("result", None)
        return value

    def _read_result(self, run_id: str) -> dict[str, Any] | None:
        path = self._run_dir(run_id) / "result.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FactorRunStoreError(f"invalid result for {run_id}") from exc
        if not isinstance(value, dict):
            raise FactorRunStoreError(f"invalid result for {run_id}")
        return value

    def _require(self, run_id: str) -> dict[str, Any]:
        manifest = self._read_manifest(run_id)
        if manifest is None:
            raise KeyError(run_id)
        return manifest

    def _run_dir(self, run_id: str) -> Path:
        safe_id = self._validate_run_id(run_id)
        candidate = (self.runs_root / safe_id).resolve()
        if not candidate.is_relative_to(self.runs_root):
            raise FactorRunValidationError("run path escapes factor backtest runs root")
        return candidate

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
            raise FactorRunValidationError("run_id contains unsafe characters")
        return run_id


def _worker_kind(kind: str) -> str:
    if kind == "factor":
        return "factor"
    if kind == "batch":
        return "factor_batch"
    raise FactorRunValidationError(f"unsupported factor run kind: {kind}")


def _summary(kind: str, result: Mapping[str, Any]) -> dict[str, Any]:
    base = {
        "n_symbols": result.get("n_symbols"),
        "n_dates": result.get("n_dates"),
        "elapsed_ms": result.get("elapsed_ms"),
        "error": result.get("error") if isinstance(result.get("error"), str) else None,
    }
    if kind == "batch":
        rows = result.get("results")
        base["n_factors"] = len(rows) if isinstance(rows, list) else 0
        return base
    base["ic_mean"] = result.get("ic_mean")
    base["ir"] = result.get("ir")
    base["ic_win_rate"] = result.get("ic_win_rate")
    return base


def _sort_key(manifest: Mapping[str, Any]) -> tuple[str, str]:
    return (str(manifest.get("created_at") or ""), str(manifest.get("run_id") or ""))


class FactorBacktestJobManager:
    """提交后后台执行。父进程同一时间只放行一个因子回测子进程。"""

    def __init__(self, data_dir: Path | str, *, worker_runner=None) -> None:
        self._data_dir = Path(data_dir)
        self.store = FactorBacktestRunStore(self._data_dir)
        self._worker_runner = worker_runner
        self._threads: dict[str, threading.Thread] = {}
        self._thread_lock = threading.Lock()
        self._shutdown = False

    def submit(self, kind: str, config, username: str | None) -> dict[str, Any]:
        manifest, task = self._create(kind, config, username)
        self._start_thread(manifest["run_id"], task)
        return manifest

    def run_blocking(self, kind: str, config, username: str | None) -> dict[str, Any]:
        """同步接口：跑完后仍返回原来的结果字典，同时把这条 run 落盘。"""
        manifest, task = self._create(kind, config, username)
        self._execute(manifest["run_id"], task)
        loaded = self.store.get(manifest["run_id"], include_result=True)
        if (
            loaded is None
            or loaded["status"] != "succeeded"
            or not isinstance(loaded.get("result"), dict)
        ):
            error = (loaded or {}).get("error") or "因子回测失败"
            raise FactorRunStoreError(str(error))
        return loaded["result"]

    def recover_interrupted(self) -> int:
        return self.store.recover_interrupted()

    def shutdown(self) -> None:
        self._shutdown = True
        with self._thread_lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=1.0)

    def _create(
        self, kind: str, config, username: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._shutdown:
            raise FactorRunStoreError("factor backtest jobs are shutting down")
        from app.backtest.worker import make_worker_task

        task = make_worker_task(_worker_kind(kind), self._data_dir, config)
        manifest = self.store.create(kind, task["config"], username)
        return manifest, task

    def _start_thread(self, run_id: str, task: dict[str, Any]) -> None:
        thread = threading.Thread(
            target=self._thread_main,
            args=(run_id, task),
            name=f"factor-backtest-{run_id[:8]}",
            daemon=True,
        )
        with self._thread_lock:
            self._threads[run_id] = thread
        thread.start()

    def _thread_main(self, run_id: str, task: dict[str, Any]) -> None:
        try:
            self._execute(run_id, task)
        except Exception:
            logger.exception("factor backtest run failed: %s", run_id)
        finally:
            with self._thread_lock:
                self._threads.pop(run_id, None)

    def _execute(self, run_id: str, task: dict[str, Any]) -> None:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter

        started = time.perf_counter()
        try:
            with shared_heavy_job_limiter.slot("exclusive"):
                current = self.store.get(run_id)
                if current is None or current["status"] not in ACTIVE_STATUSES:
                    return
                if current["status"] == "queued":
                    self.store.transition(run_id, "running")
                result = self._run_worker(task)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            self.store.fail(run_id, exc, elapsed_ms=elapsed_ms)
            raise
        if not isinstance(result, dict):
            self.store.fail(
                run_id, TypeError("factor worker result must be a dict"), elapsed_ms=elapsed_ms
            )
            raise FactorRunStoreError("factor worker result must be a dict")
        try:
            self.store.complete(run_id, result, elapsed_ms=elapsed_ms)
        except Exception as exc:  # noqa: BLE001 — 落盘失败也要把 run 标成失败，避免一直停在运行中
            self.store.fail(run_id, exc, elapsed_ms=elapsed_ms)
            raise

    def _run_worker(self, task: dict[str, Any]) -> dict[str, Any]:
        if self._worker_runner is not None:
            return self._worker_runner(task)
        from app.backtest.worker import run_worker_task

        return run_worker_task(task)
