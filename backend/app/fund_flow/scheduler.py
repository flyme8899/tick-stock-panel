"""资金进出后台调度。

任务挂在现有 APScheduler 上，但 ``kick`` 只唤醒线程，不占用界面上的数据任务槽，
也不进重任务执行槽。真正的拉取在单独的低优先级守护线程里，一次只跑一个数据集。
TickFlow 日 K / 分钟同步占着槽时，这一轮让路，下一次再试。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path

from app.fund_flow import config, jobs, store
from app.fund_flow.circuit import CircuitBreaker
from app.fund_flow.jobs import RunResult
from app.market_time import CN_TZ, cn_now

logger = logging.getLogger(__name__)

RETRY_GAP_S = 30 * 60
SECTOR_GAP_S = 180

_worker: FundFlowWorker | None = None
_worker_lock = threading.Lock()


def tickflow_sync_busy() -> bool:
    """日 K 管道或分钟同步正在写盘时返回 True。"""
    try:
        from app.services.pipeline_jobs import job_store, run_slot_busy

        if job_store.active_id() or run_slot_busy():
            return True
    except Exception:  # noqa: BLE001
        return False
    try:
        from app.jobs.daily_pipeline import _get_app_state

        app_state = _get_app_state()
        minute = getattr(app_state, "minute_refresh", None) if app_state else None
        lock = getattr(minute, "_round_lock", None)
        if lock is not None and lock.locked():
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _parse_stamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed


def _recent(kind_state: dict, now: datetime, gap_s: float) -> bool:
    stamp = _parse_stamp(kind_state.get("last_attempt_at"))
    if stamp is None:
        return False
    return (now - stamp).total_seconds() < gap_s


def _done_today(kind_state: dict, day: str) -> bool:
    return str(kind_state.get("last_success_date") or "") == day


def due_tasks(
    now: datetime,
    *,
    trading: bool,
    state: dict,
    flags: dict[str, bool],
    breakers_open: set[str],
) -> list[str]:
    """返回这一轮该跑的数据集名字。交易日、时刻和开关都在这里判断。"""
    if not flags.get("enabled") or not trading:
        return []
    clock = now.timetz().replace(tzinfo=None)
    day = now.date().isoformat()
    kinds = state.get("kinds") or {}
    due: list[str] = []

    def kind_state(name: str) -> dict:
        raw = kinds.get(name) or {}
        return raw if isinstance(raw, dict) else {}

    if (
        flags.get("margin")
        and "akshare" not in breakers_open
        and dt_time(8, 30) <= clock < dt_time(16, 0)
        and not _done_today(kind_state("margin"), day)
        and not _recent(kind_state("margin"), now, RETRY_GAP_S)
    ):
        due.append("margin")
    if (
        flags.get("sector")
        and "akshare" not in breakers_open
        and dt_time(9, 30) <= clock <= dt_time(15, 0)
        and not _recent(kind_state("sector_intraday"), now, SECTOR_GAP_S)
    ):
        due.append("sector_intraday")
    if (
        flags.get("sector")
        and "akshare" not in breakers_open
        and clock >= dt_time(15, 5)
        and not _done_today(kind_state("sector_close"), day)
        and not _recent(kind_state("sector_close"), now, RETRY_GAP_S)
    ):
        due.append("sector_close")
    if (
        flags.get("stock")
        and "efinance" not in breakers_open
        and clock >= dt_time(16, 30)
        and not _done_today(kind_state("stock"), day)
        and not _recent(kind_state("stock"), now, RETRY_GAP_S)
    ):
        due.append("stock")
    if (
        flags.get("etf_shares")
        and "akshare" not in breakers_open
        and clock >= dt_time(16, 40)
        and not _done_today(kind_state("etf_shares"), day)
        and not _recent(kind_state("etf_shares"), now, RETRY_GAP_S)
    ):
        due.append("etf_shares")
    if (
        flags.get("southbound")
        and "akshare" not in breakers_open
        and clock >= dt_time(16, 40)
        and not _done_today(kind_state("southbound"), day)
        and not _recent(kind_state("southbound"), now, RETRY_GAP_S)
    ):
        due.append("southbound")
    if (
        flags.get("northbound_turnover")
        and "akshare" not in breakers_open
        and clock >= dt_time(16, 40)
        and not _done_today(kind_state("northbound_turnover"), day)
        and not _recent(kind_state("northbound_turnover"), now, RETRY_GAP_S)
    ):
        due.append("northbound_turnover")
    if (
        flags.get("lhb_backup")
        and "akshare" not in breakers_open
        and clock >= dt_time(17, 30)
        and not _done_today(kind_state("lhb"), day)
        and not _recent(kind_state("lhb"), now, RETRY_GAP_S)
    ):
        due.append("lhb")
    if (
        flags.get("margin")
        and "akshare" not in breakers_open
        and clock >= dt_time(20, 0)
        and not _done_today(kind_state("margin_evening"), day)
        and kind_state("margin").get("target_covered") != _margin_target(now)
        and not _recent(kind_state("margin_evening"), now, RETRY_GAP_S)
    ):
        due.append("margin_evening")
    return due


def _margin_target(now: datetime, known_days: list[date] | None = None) -> str:
    return jobs.previous_session(now.date(), known_days).isoformat()


def session_day(
    now: datetime,
    *,
    trading: bool,
    known_days: list[date] | None,
    ready: dt_time,
) -> str:
    """最近一个应该已经落盘的交易日。收盘前用上一交易日，周末同样回退。"""
    clock = now.timetz().replace(tzinfo=None)
    if trading and clock >= ready:
        return now.date().isoformat()
    return jobs.previous_session(now.date(), known_days).isoformat()


def task_day(
    name: str,
    now: datetime,
    *,
    trading: bool,
    known_days: list[date] | None,
) -> str:
    if name in {"margin", "margin_evening"}:
        return _margin_target(now, known_days)
    if name == "sector_intraday":
        return now.date().isoformat()
    ready = {
        "stock": dt_time(16, 30),
        "sector_close": dt_time(15, 5),
        "etf_shares": dt_time(16, 40),
        "southbound": dt_time(16, 40),
        "northbound_turnover": dt_time(16, 40),
        "lhb": dt_time(17, 30),
    }.get(name, dt_time(16, 30))
    return session_day(now, trading=trading, known_days=known_days, ready=ready)


def _partition_missing(data_dir: Path, kind: str, day: str) -> bool:
    return not store.partition_file(data_dir, kind, day).exists()


def _stock_incomplete(data_dir: Path, day: str, universe: int) -> bool:
    count = 0 if _partition_missing(data_dir, "stock", day) else store.code_count(
        store.read_partition(data_dir, "stock", day),
    )
    return store.is_partial_ranking(count, universe)


def _sector_close_missing(data_dir: Path, day: str) -> bool:
    for kind in ("industry", "concept"):
        frame = store.read_partition(data_dir, kind, day)
        if frame.is_empty() or "snapshot" not in frame.columns:
            return True
        if "close" not in frame.get_column("snapshot").to_list():
            return True
    return False


def catchup_tasks(
    now: datetime,
    *,
    trading: bool,
    state: dict,
    flags: dict[str, bool],
    breakers_open: set[str],
    data_dir: Path,
    known_days: list[date] | None,
    universe: int,
) -> list[str]:
    """开关在收盘后或非交易日打开时，补最近一个交易日还缺的分区。"""
    if not flags.get("enabled"):
        return []
    kinds = state.get("kinds") or {}
    today = now.date().isoformat()

    def kind_state(name: str) -> dict:
        raw = kinds.get(name) or {}
        return raw if isinstance(raw, dict) else {}

    def cooled(name: str) -> bool:
        return _done_today(kind_state(name), today) or _recent(kind_state(name), now, RETRY_GAP_S)

    due: list[str] = []
    stock_day = task_day("stock", now, trading=trading, known_days=known_days)
    if (
        flags.get("stock")
        and "efinance" not in breakers_open
        and not cooled("stock")
        and _stock_incomplete(data_dir, stock_day, universe)
    ):
        due.append("stock")
    margin_day = task_day("margin", now, trading=trading, known_days=known_days)
    if flags.get("margin") and "akshare" not in breakers_open and not cooled("margin"):
        covered = str(kind_state("margin").get("target_covered") or "")
        if covered != margin_day or _partition_missing(data_dir, "margin", margin_day):
            due.append("margin")
    close_day = task_day("sector_close", now, trading=trading, known_days=known_days)
    if (
        flags.get("sector")
        and "akshare" not in breakers_open
        and not cooled("sector_close")
        and _sector_close_missing(data_dir, close_day)
    ):
        due.append("sector_close")
    for task, flag, kind, ready_name in (
        ("etf_shares", "etf_shares", "etf_shares", "etf_shares"),
        ("southbound", "southbound", "southbound", "southbound"),
        ("northbound_turnover", "northbound_turnover", "northbound_turnover", "northbound_turnover"),
        ("lhb", "lhb_backup", "lhb", "lhb"),
    ):
        if not flags.get(flag) or "akshare" in breakers_open or cooled(task):
            continue
        day = task_day(ready_name, now, trading=trading, known_days=known_days)
        if _partition_missing(data_dir, kind, day):
            due.append(task)
    return due


def current_flags() -> dict[str, bool]:
    return {
        "enabled": config.enabled(),
        "stock": config.stock_enabled(),
        "sector": config.sector_enabled(),
        "margin": config.margin_enabled(),
        "etf_shares": config.etf_shares_enabled(),
        "southbound": config.southbound_enabled(),
        "northbound_turnover": config.northbound_turnover_enabled(),
        "lhb_backup": config.lhb_backup_enabled(),
    }


def load_symbols(data_dir: Path) -> list[str]:
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return []
    import polars as pl

    frame = pl.read_parquet(path, columns=["symbol"])
    found = []
    for symbol in frame["symbol"].to_list():
        text = str(symbol)
        if text.endswith((".SH", ".SZ", ".BJ")) and jobs.normalize.code6(text):
            found.append(text)
    return found


def known_trading_days(data_dir: Path) -> list[date]:
    root = Path(data_dir) / "kline_daily"
    if not root.exists():
        return []
    found = []
    for path in root.glob("date=*"):
        if not path.is_dir():
            continue
        try:
            found.append(date.fromisoformat(path.name.removeprefix("date=")))
        except ValueError:
            continue
    return found


class FundFlowWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self.breakers = {
            "efinance": CircuitBreaker("efinance", clock=time_time),
            "mx": CircuitBreaker("mx", clock=time_time),
            "akshare": CircuitBreaker("akshare", clock=time_time),
        }
        self.deferred_reason: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="fund-flow", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    def kick(self) -> None:
        """调度器调用。只唤醒，不在调度线程里拉数。"""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(30)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.run_once()
            except Exception:
                logger.exception("资金进出本轮失败")

    def run_once(
        self,
        *,
        now: datetime | None = None,
        data_dir: Path | None = None,
        clients=None,
        symbols: list[str] | None = None,
        trading: bool | None = None,
        busy: bool | None = None,
    ) -> list[str]:
        """跑当前到点的任务。返回实际启动的名字。忙或未开时返回空。"""
        if not self._run_lock.acquire(blocking=False):
            return []
        try:
            return self._run_once(
                now=now, data_dir=data_dir, clients=clients,
                symbols=symbols, trading=trading, busy=busy,
            )
        finally:
            self._run_lock.release()

    def _run_once(self, *, now, data_dir, clients, symbols, trading, busy) -> list[str]:
        from app.config import settings

        now = now or cn_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=CN_TZ)
        data_dir = Path(data_dir or settings.data_dir)
        self._restore_breakers(data_dir)
        flags = current_flags()
        if trading is None:
            trading = _default_trading(now)
        if busy is None:
            busy = tickflow_sync_busy()
        if busy:
            self.deferred_reason = "tickflow_sync"
            store.update_kind_state(data_dir, "_scheduler", deferred="tickflow_sync")
            return []
        self.deferred_reason = None
        state = store.load_state(data_dir)
        open_sources = {name for name, breaker in self.breakers.items() if not breaker.allow()}
        known = known_trading_days(data_dir)
        if symbols is None:
            symbols = load_symbols(data_dir)
        planned = due_tasks(
            now, trading=trading, state=state, flags=flags, breakers_open=open_sources,
        )
        for name in catchup_tasks(
            now,
            trading=trading,
            state=state,
            flags=flags,
            breakers_open=open_sources,
            data_dir=data_dir,
            known_days=known,
            universe=len(symbols),
        ):
            if name not in planned:
                planned.append(name)
        if not planned:
            self._persist_breakers(data_dir)
            return []
        if clients is None:
            from app.fund_flow.sources import live_clients

            clients = live_clients(data_dir)
        ran: list[str] = []
        for name in planned:
            if self._stop.is_set():
                break
            self._mark_attempt(data_dir, name, now)
            self._execute(
                data_dir, name, now, clients, symbols,
                trading=trading, known_days=known,
            )
            ran.append(name)
            if tickflow_sync_busy():
                self.deferred_reason = "tickflow_sync"
                break
        self._persist_breakers(data_dir)
        return ran

    def _execute(
        self,
        data_dir: Path,
        name: str,
        now: datetime,
        clients,
        symbols: list[str],
        *,
        trading: bool,
        known_days: list[date],
    ) -> RunResult:
        day = now.date().isoformat()
        trade_day = task_day(name, now, trading=trading, known_days=known_days)
        captured = f"{trade_day}T{now.strftime('%H:%M:%S')}"
        ak = self.breakers["akshare"]
        if name == "stock":
            result = jobs.sync_stock(
                data_dir, symbols, clients,
                efinance=self.breakers["efinance"], mx=self.breakers["mx"],
                trade_date=trade_day,
            )
            if not result.errors:
                _mark_success(data_dir, "stock", day)
            return result
        if name == "sector_intraday":
            for kind in ("industry", "concept"):
                result = jobs.sync_sectors(
                    data_dir, clients, kind=kind, trade_date=trade_day,
                    captured_at=captured, snapshot="intraday", breaker=ak,
                )
                if result.errors:
                    return result
            _mark_success(data_dir, "industry", day)
            return result
        if name == "sector_close":
            for kind in ("industry", "concept"):
                result = jobs.sync_sectors(
                    data_dir, clients, kind=kind, trade_date=trade_day,
                    captured_at=captured, snapshot="close", breaker=ak,
                )
                if result.errors:
                    return result
            _mark_success(data_dir, "sector_close", day)
            return result
        if name in {"margin", "margin_evening"}:
            result = jobs.sync_margin(
                data_dir, clients, trade_date=trade_day, breaker=ak, known_days=known_days,
            )
            if not result.errors and result.wrote:
                store.update_kind_state(data_dir, "margin", target_covered=trade_day)
                _mark_success(data_dir, name, day)
            elif name == "margin_evening":
                store.update_kind_state(data_dir, "margin_evening", last_error=result.errors[-1] if result.errors else None)
            return result
        if name == "etf_shares":
            result = jobs.sync_etf_shares(data_dir, clients, trade_date=trade_day, breaker=ak)
            if not result.errors:
                _mark_success(data_dir, "etf_shares", day)
            return result
        if name == "southbound":
            result = jobs.sync_southbound(data_dir, clients, breaker=ak)
            if not result.errors:
                _mark_success(data_dir, "southbound", day)
            return result
        if name == "northbound_turnover":
            result = jobs.sync_northbound_turnover(data_dir, clients, breaker=ak)
            if not result.errors:
                _mark_success(data_dir, "northbound_turnover", day)
            return result
        if name == "lhb":
            result = jobs.sync_lhb_backup(data_dir, clients, trade_date=trade_day, breaker=ak)
            if not result.errors:
                _mark_success(data_dir, "lhb", day)
            return result
        return RunResult(kind=name, skipped="unknown")

    def _mark_attempt(self, data_dir: Path, name: str, now: datetime) -> None:
        store.update_kind_state(data_dir, name, last_attempt_at=now.isoformat(timespec="seconds"))

    def _restore_breakers(self, data_dir: Path) -> None:
        saved = (store.load_state(data_dir).get("sources") or {})
        for name, breaker in self.breakers.items():
            row = saved.get(name) or {}
            try:
                breaker.open_until = float(row.get("open_until") or 0)
                breaker.consecutive_failures = int(row.get("consecutive_failures") or 0)
            except (TypeError, ValueError):
                continue

    def _persist_breakers(self, data_dir: Path) -> None:
        payload = store.load_state(data_dir)
        payload["sources"] = {name: breaker.snapshot() for name, breaker in self.breakers.items()}
        store.save_state(data_dir, payload)


def time_time() -> float:
    import time
    return time.time()


def _mark_success(data_dir: Path, kind: str, day: str) -> None:
    store.update_kind_state(data_dir, kind, last_success_date=day, last_error=None)


def _default_trading(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    try:
        from app.services.trading_day import is_trading_day

        verdict = is_trading_day(now)
    except Exception:  # noqa: BLE001
        return True
    return verdict is not False


def get_worker() -> FundFlowWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = FundFlowWorker()
        return _worker


def register_jobs(scheduler) -> None:
    """在现有调度器上挂一个 30 秒唤醒。拉取本身不进这个线程。"""
    from apscheduler.triggers.interval import IntervalTrigger

    worker = get_worker()
    scheduler.add_job(
        worker.kick,
        trigger=IntervalTrigger(seconds=30),
        id="fund_flow_kick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        replace_existing=True,
    )


def reset_worker_for_tests() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.stop()
        _worker = None
