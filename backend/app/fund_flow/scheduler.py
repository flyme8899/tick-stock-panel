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
# 个股即时资金流落在 5–10 分钟这一档。6 分钟是固定间隔，文档和页面按这个数写。
# 东财当日排名要翻页，再加 1 秒限流，一次大约一分多钟，不能占住这条线程。
STOCK_INTRADAY_GAP_S = 6 * 60
# 盘中旁路线程的最长允许时长。正常一轮一分多钟；超过这个数视为卡死，
# 允许下一轮重新启动 —— 否则一次卡死会让当天剩余时段再也不出快照。
# 取 12 分钟，是 6 分钟间隔的 2 倍，留足慢网络余量。
INTRADAY_THREAD_TIMEOUT_S = 12 * 60
# 主循环看门狗：单轮 run_once 超过这个时长就记 WARNING。
# 主循环是单线程，卡住一次会让之后所有任务（含收盘分区）全部停摆。
LOOP_STALL_WARN_S = 5 * 60
# 让路的轮数上限。30 秒一轮，120 轮约一小时 —— 重任务再慢也该出结果了。
# 到点就强行放行一次，避免 ``tickflow_sync_busy`` 恒真导致全天不落盘。
DEFER_MAX_STREAK = 120

_worker: FundFlowWorker | None = None
_worker_lock = threading.Lock()


def tickflow_sync_busy(*, include_minute: bool = True) -> bool:
    """日 K 管道或分钟同步正在写盘时返回 True。

    ``include_minute=False`` 时只看日 K 管道。盘中分钟增量每 10 秒一轮、几乎一直
    持锁，资金流盘中快照如果也礼让它，整个下午都落不了盘（线上 10-15 11:00 后停摆）。

    探测失败时返回 False（按「不忙」处理），但**必须留日志**：静默当成不忙会
    让资金流在重任务写盘期间抢跑，且事后完全查不到原因。
    """
    try:
        from app.services.pipeline_jobs import job_store, run_slot_busy

        if job_store.active_id() or run_slot_busy():
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("tickflow_sync_busy 探测 pipeline_jobs 失败, 按不忙处理: %s", exc)
        return False
    if not include_minute:
        return False
    try:
        from app.jobs.daily_pipeline import _get_app_state

        app_state = _get_app_state()
        minute = getattr(app_state, "minute_refresh", None) if app_state else None
        lock = getattr(minute, "_round_lock", None)
        if lock is not None and lock.locked():
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("tickflow_sync_busy 探测分钟同步锁失败, 按不忙处理: %s", exc)
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


def _in_continuous_clock(clock: dt_time) -> bool:
    """连续竞价钟点，不含午休。交易日与否由调用方的 ``trading`` 决定。"""
    return (
        dt_time(9, 30) <= clock <= dt_time(11, 30)
        or dt_time(13, 0) <= clock <= dt_time(15, 0)
    )


def _recent(kind_state: dict, now: datetime, gap_s: float) -> bool:
    stamp = _parse_stamp(kind_state.get("last_attempt_at"))
    if stamp is None:
        return False
    return (now - stamp).total_seconds() < gap_s


def _done_today(kind_state: dict, day: str) -> bool:
    return str(kind_state.get("last_success_date") or "") == day


def _intraday_due(kind_state: dict, now: datetime) -> bool:
    """盘中个股资金流这一轮该不该拉。

    判定依据只有 **成功落盘的时刻**（``last_snapshot`` / ``last_success_at``），
    不看 ``last_attempt_at``。原因：主循环每 30 秒唤醒一次，而 ``_mark_attempt``
    每轮都写 ``last_attempt_at``，且旁路线程正常结束后 ``_intraday_running()``
    立即为假、下一轮就会重新拉起 —— 用 attempt 判间隔会退化成「拉完就再拉」，
    实际间隔只剩拉取耗时（约 1 分多钟），而注释和页面承诺的是 6 分钟。

    落盘成功的写入点是 ``sync_stock_intraday``（内部写 ``last_snapshot``，
    见 jobs._write_intraday / sync_stock_intraday 的东财分支）。因此这里读
    ``last_snapshot``，回退到 ``last_success_at`` 兼容旧状态文件。

    取不到任何成功时刻时（首轮或旧状态）返回 True，保证第一份快照能落盘；
    失败重试由熔断与 ``last_attempt_at`` 在别处兜底，这里不负责限流失败重试。
    """
    stamp = _parse_stamp(
        kind_state.get("last_snapshot") or kind_state.get("last_success_at")
    )
    if stamp is None:
        return True
    return (now - stamp).total_seconds() >= STOCK_INTRADAY_GAP_S


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
        flags.get("stock_intraday")
        and "akshare" not in breakers_open
        and _in_continuous_clock(clock)
        and _intraday_due(kind_state("stock_intraday"), now)
    ):
        due.append("stock_intraday")
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
    if name in {"sector_intraday", "stock_intraday"}:
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
    if _partition_missing(data_dir, "stock", day):
        return True
    frame = store.read_partition(data_dir, "stock", day)
    expected, full = store.board_coverage(data_dir)
    return store.is_partial_ranking(
        store.code_count(frame), universe, frame, expected=expected, full=full,
    )


def _stock_day_complete(data_dir: Path, day: str, universe: int) -> bool:
    """这一天的 ``stock`` 分区是否已够当全市场排名。

    与 ``_stock_incomplete`` 互补，供落盘判据使用：上游熔断 / 部分失败时，
    只要整批兜底把样本补齐了，这一天就算拿到了。
    """
    return not _stock_incomplete(data_dir, day, universe)


# 同一交易日的板块缺口最多补几次。上游同花顺个股榜长期不覆盖深主板，
# 缺口不是「这次拉失败」而是数据源的已知局限；不设上限就会每 30 分钟
# 全量重跑一轮。达到上限后只记 WARNING，等次日新分区再看。
BOARD_GAP_MAX_TRIES = 2


def _board_gap_exhausted(kind_state: dict, day: str) -> bool:
    """这一交易日的板块缺口补拉是否已经用满次数。"""
    if str(kind_state.get("board_gap_date") or "") != day:
        return False
    try:
        return int(kind_state.get("board_gap_tries") or 0) >= BOARD_GAP_MAX_TRIES
    except (TypeError, ValueError):
        return False


def _stock_gap_only(data_dir: Path, day: str) -> bool:
    """分区存在、总数够，只是缺整段板块 —— 这种要受补拉次数限制。"""
    if _partition_missing(data_dir, "stock", day):
        return False
    frame = store.read_partition(data_dir, "stock", day)
    if frame.is_empty():
        return False
    expected, full = store.board_coverage(data_dir)
    return bool(store.missing_boards(frame, expected=expected, full=full))


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
        and not (
            _stock_gap_only(data_dir, stock_day)
            and _board_gap_exhausted(kind_state("stock"), stock_day)
        )
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
        "stock_intraday": config.stock_intraday_enabled(),
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
        self._intraday_thread: threading.Thread | None = None
        self._intraday_started_at: float | None = None
        # 连续让路计数与首次让路时刻。让路是一次性的礼让，不该变成永久停摆：
        # ``tickflow_sync`` 的判据一旦恒真，主循环会一轮轮空转、什么都不落盘。
        # 超过 ``DEFER_MAX_STREAK`` 轮就强行放行一次，宁可和重任务抢一下，
        # 也不能整天不出数据。
        self._defer_streak = 0
        self._defer_since: float | None = None
        # 每次启动盘中拉取自增。线程杀不掉，只能靠世代号让旧结果自我作废。
        self._intraday_generation = 0

    def _defer_allowed(self, now: datetime) -> tuple[bool, str | None]:
        """这一轮是否还让路。返回 ``(是否让路, 原因说明)``。

        连续让路到 ``DEFER_MAX_STREAK`` 轮就放行，并把原因记进日志 ——
        让路必须可见、有上限，否则就是静默停摆。
        """
        self._defer_streak += 1
        if self._defer_since is None:
            self._defer_since = time_time()
        waited = time_time() - self._defer_since
        if self._defer_streak > DEFER_MAX_STREAK:
            logger.warning(
                "资金流已连续让路 %d 轮 / %.0f 秒（tickflow_sync 一直忙），本轮强制放行",
                self._defer_streak,
                waited,
            )
            return False, "tickflow_sync_busy_forced"
        return True, None

    def _defer_reset(self) -> None:
        self._defer_streak = 0
        self._defer_since = None

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
            started = time_time()
            ran: list[str] = []
            try:
                # 盘中个股要翻页，放到旁路线程，避免这一轮的板块和收盘任务干等。
                ran = self.run_once(detach_intraday=True) or []
            except Exception:
                logger.exception("资金进出本轮失败")
            finally:
                # 主循环是单线程：一轮里任何一处网络读卡住，之后到点的收盘任务
                # （stock / etf_shares / southbound 等）都会一起停摆。这里只做
                # 事后告警 —— 真正的超时交给各客户端自身的超时设置。
                elapsed = time_time() - started
                # 每轮都留痕：启动了什么、耗时多久、是否让路。13:44 那类「快照
                # 突然不再更新」的事后排查，全靠这一行日志自证卡在哪一步。
                logger.info(
                    "资金流一轮完成 耗时=%.0fs 启动=%s 让路=%s",
                    elapsed,
                    ",".join(ran) if ran else "-",
                    self.deferred_reason or "-",
                )
                if elapsed > LOOP_STALL_WARN_S:
                    logger.warning("资金进出一轮耗时 %.0f 秒，已超过看门狗阈值", elapsed)

    def run_once(
        self,
        *,
        now: datetime | None = None,
        data_dir: Path | None = None,
        clients=None,
        symbols: list[str] | None = None,
        trading: bool | None = None,
        busy: bool | None = None,
        detach_intraday: bool = False,
    ) -> list[str]:
        """跑当前到点的任务。返回实际启动的名字。忙或未开时返回空。

        ``detach_intraday`` 为真时，盘中个股在旁路线程里拉，本轮不等它结束。
        测试默认仍在当前线程跑完，方便直接读落盘。
        """
        if not self._run_lock.acquire(blocking=False):
            return []
        try:
            return self._run_once(
                now=now, data_dir=data_dir, clients=clients,
                symbols=symbols, trading=trading, busy=busy,
                detach_intraday=detach_intraday,
            )
        finally:
            self._run_lock.release()

    def _run_once(
        self, *, now, data_dir, clients, symbols, trading, busy, detach_intraday: bool = False,
    ) -> list[str]:
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
            # 交易时段不再礼让分钟增量：它常驻持锁，礼让等于盘中快照全停。
            busy = tickflow_sync_busy(include_minute=not trading)
        if busy and self._defer_allowed(now)[0]:
            self.deferred_reason = "tickflow_sync"
            # 心跳独立写：绝不因为让路而让「循环还活着」的信号倒退。
            store.touch_heartbeat(data_dir, deferred="tickflow_sync")
            return []
        self.deferred_reason = None
        self._defer_reset()
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
            store.touch_heartbeat(data_dir, deferred=None)
            self._persist_breakers(data_dir)
            return []
        store.touch_heartbeat(data_dir, deferred=None)
        if clients is None:
            from app.fund_flow.sources import live_clients

            clients = live_clients(data_dir)
        ran: list[str] = []
        ordered = list(planned)
        if detach_intraday and "stock_intraday" in ordered:
            # 先把慢任务丢出去，后面的板块快照不用等分页结束。
            ordered = ["stock_intraday", *[name for name in ordered if name != "stock_intraday"]]
        for name in ordered:
            if self._stop.is_set():
                break
            if name == "stock_intraday" and detach_intraday:
                if self._intraday_running():
                    logger.info("盘中个股主力净流入仍在拉取，本轮不重复启动")
                    continue
                self._mark_attempt(data_dir, name, now)
                self._spawn_stock_intraday(
                    data_dir, now, clients, trading=trading, known_days=known,
                )
                ran.append(name)
                continue
            self._mark_attempt(data_dir, name, now)
            self._execute(
                data_dir, name, now, clients, symbols,
                trading=trading, known_days=known,
            )
            ran.append(name)
            if tickflow_sync_busy(include_minute=not trading):
                self.deferred_reason = "tickflow_sync"
                break
        self._persist_breakers(data_dir)
        return ran

    def _intraday_running(self) -> bool:
        """旁路盘中线程是否仍在跑。

        只看 ``is_alive()`` 不够：线程卡在网络读上时永远为真，当天剩余时段
        再也不会启动新快照（线上曾停在 13:44）。超过 ``INTRADAY_THREAD_TIMEOUT_S``
        的一律按卡死处理，让下一轮重新拉。

        注意：Python 线程杀不掉，超时只是「不再等它」，旧线程仍在跑。所以每次
        启动都带一个 ``generation`` 序号，旧代线程结束时发现自己不是当前代就
        丢弃结果 —— 否则新旧两个线程会同时写分区、同时回写熔断状态。
        """
        thread = self._intraday_thread
        if thread is None or not thread.is_alive():
            return False
        started = self._intraday_started_at
        if started is None:
            return True
        if (time_time() - started) > INTRADAY_THREAD_TIMEOUT_S:
            logger.warning(
                "盘中个股主力净流入线程已运行 %.0f 秒，判定卡死，本轮重新启动"
                "（旧线程 gen=%d 仍在跑，其结果将被丢弃）",
                time_time() - started,
                self._intraday_generation,
            )
            return False
        return True

    def wait_intraday(self, timeout: float = 5) -> None:
        """测试里等旁路拉取结束。生产循环不调用。"""
        thread = self._intraday_thread
        if thread is not None:
            thread.join(timeout)

    def _spawn_stock_intraday(
        self,
        data_dir: Path,
        now: datetime,
        clients,
        *,
        trading: bool,
        known_days: list[date],
    ) -> None:
        day = now.date().isoformat()
        trade_day = task_day("stock_intraday", now, trading=trading, known_days=known_days)
        captured = f"{trade_day}T{now.strftime('%H:%M:%S')}"
        breaker = self.breakers["akshare"]
        self._intraday_generation += 1
        generation = self._intraday_generation

        def current_generation() -> bool:
            """这一代是否仍是当前代。线程之间只读一个 int，无需加锁。"""
            return generation == self._intraday_generation

        def target() -> None:
            try:
                # 世代回调一路传进 jobs：``sync_stock_intraday`` **内部**就会落盘，
                # 校验必须发生在写之前。等函数返回再查就晚了 —— 旧代已经写进去了。
                result = jobs.sync_stock_intraday(
                    data_dir,
                    clients,
                    trade_date=trade_day,
                    captured_at=captured,
                    breaker=breaker,
                    is_current=current_generation,
                )
                if not current_generation():
                    logger.warning(
                        "盘中快照 gen=%d 已过期（当前 gen=%d），丢弃本次结果 %s",
                        generation,
                        self._intraday_generation,
                        captured,
                    )
                    return
                if not result.errors:
                    _mark_success(data_dir, "stock_intraday", day)
                    # 成功时刻单独落一个字段, 供 ``_intraday_due`` 判间隔。
                    # 不依赖 ``last_attempt_at``(每轮都写), 也不依赖 job 内部的
                    # ``last_snapshot``(那是快照采集时刻, 语义上属于数据而非调度)。
                    store.update_kind_state(
                        data_dir, "stock_intraday",
                        last_success_at=now.isoformat(timespec="seconds"),
                    )
                    logger.info(
                        "盘中快照已落盘 gen=%d captured=%s 写入=%s",
                        generation,
                        captured,
                        result.wrote,
                    )
            except Exception:
                logger.exception("盘中个股主力净流入拉取失败")
            finally:
                # 过期代连熔断状态都不该回写：那是新一代的账，旧代写进去只会
                # 用「解卡后迟到的成功」把新代的失败盖掉。
                if current_generation():
                    try:
                        self._persist_breakers(data_dir)
                    except Exception:
                        logger.exception("盘中个股主力净流入写回熔断状态失败")

        thread = threading.Thread(target=target, name="fund-flow-intraday", daemon=True)
        self._intraday_thread = thread
        self._intraday_started_at = time_time()
        thread.start()

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
            # 落盘判据看「这一天的分区是否够当全市场排名」，而不是「有没有 error」。
            # 上游熔断时 sync_stock 仍会用全市场排名兜底并把数据写盘；只要样本达标，
            # 这一天就算拿到了，不能因为同时记了一条熔断 error 就永远不标成功 ——
            # 那会导致 30 分钟一轮的无限重试，且收盘分区一直不落盘。
            if not result.errors or _stock_day_complete(data_dir, trade_day, len(symbols)):
                _mark_success(data_dir, "stock", day)
            return result
        if name == "stock_intraday":
            result = jobs.sync_stock_intraday(
                data_dir, clients, trade_date=trade_day, captured_at=captured, breaker=ak,
            )
            if not result.errors:
                _mark_success(data_dir, "stock_intraday", day)
                store.update_kind_state(
                    data_dir, "stock_intraday",
                    last_success_at=now.isoformat(timespec="seconds"),
                )
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
        fields: dict[str, object] = {"last_attempt_at": now.isoformat(timespec="seconds")}
        if name == "stock":
            # 板块缺口重拉要计数，避免上游长期不覆盖时无限全量重跑。
            # 每次尝试前就把计数写下去 —— 中途崩了也算用过一次，宁可少拉不可多拉。
            day = task_day("stock", now, trading=_default_trading(now),
                           known_days=known_trading_days(data_dir))
            if _stock_gap_only(data_dir, day):
                tried = store.load_state(data_dir)
                state = ((tried.get("kinds") or {}).get("stock") or {})
                prev = 0
                if str(state.get("board_gap_date") or "") == day:
                    try:
                        prev = int(state.get("board_gap_tries") or 0)
                    except (TypeError, ValueError):
                        prev = 0
                fields["board_gap_date"] = day
                fields["board_gap_tries"] = prev + 1
        store.update_kind_state(data_dir, name, **fields)

    def _restore_breakers(self, data_dir: Path) -> None:
        saved = (store.load_state(data_dir).get("sources") or {})
        # 旁路线程正在打东财时，磁盘上的熔断计数可能还是上一轮的。不要把它盖回去。
        intraday_busy = self._intraday_running()
        for name, breaker in self.breakers.items():
            if name == "akshare" and intraday_busy:
                continue
            row = saved.get(name) or {}
            try:
                breaker.open_until = float(row.get("open_until") or 0)
                breaker.consecutive_failures = int(row.get("consecutive_failures") or 0)
            except (TypeError, ValueError):
                continue

    def _persist_breakers(self, data_dir: Path) -> None:
        def mutate(payload: dict) -> None:
            payload["sources"] = {
                name: breaker.snapshot() for name, breaker in self.breakers.items()
            }

        store.mutate_state(data_dir, mutate)


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
