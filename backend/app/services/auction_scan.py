"""全市场竞价扫描 — 交易日 09:25 终态快照落盘，供异动页列表读取。

抓取走 ``FuyaoClient.snapshot_all()``（``GET /api/a-share/prices/snapshot``）。
该快照的官方字段是 thscode / ticker / last_price / price_change /
price_change_ratio_pct / open_price / high_price / low_price / prev_price /
volume（股）/ turnover，没有竞价量，也没有量比。集合竞价另有
``/api/a-share/auction/snapshot``，但必须逐批传 thscodes，不是本任务指定的入口。

因此量比的基准量用本地日 K 近 5 个交易日均量 ÷ 240（连续竞价 240 分钟）。
09:25:30–09:28:00 之间快照 ``volume`` 还没进入连续竞价，就是竞价成交量。
日 K 的 volume 是手，快照 volume 是股，相除前先换成同一单位。

若某次快照行上出现量比或竞价量字段，则优先用快照字段，不再用这套推算。
落盘沿用 auction_benchmark 的按日 JSON + ``.part`` 原子替换。
当天文件已在就跳过；窗口外（含盘后）不补跑。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date as date_cls
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now, cn_today

logger = logging.getLogger(__name__)

# 连续竞价 120 + 120 分钟。量比分母是「近 5 日均量 / 240」，不是盘中累计分钟量比。
SESSION_MINUTES = 240
SHARES_PER_HAND = 100
WIDE_MIN_GAP = 0.03
LOOKBACK_DAYS = 5

# 09:25:30 首跑，09:26:30 / 09:27:30 各重试一次。09:28 起不再跑，避免盘后补扫。
SCAN_START = dt_time(9, 25, 30)
SCAN_END = dt_time(9, 28, 0)
MISFIRE_GRACE_S = 60

VOLUME_BASIS_AVG5 = "avg5_div_240"
VOLUME_BASIS_RATIO = "snapshot_volume_ratio"
VOLUME_BASIS_AUCTION_VOL = "snapshot_auction_volume"

_VOLUME_RATIO_KEYS = (
    "volume_ratio",
    "vol_ratio",
    "auction_volume_ratio",
    "quantity_ratio",
)
_AUCTION_VOLUME_KEYS = (
    "auction_volume",
    "call_auction_volume",
    "bidding_volume",
)
_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
_ON = {"1", "true", "yes", "on"}
_RUN_LOCK = threading.Lock()


def schedule_specs() -> list[dict[str, int | str]]:
    """三枪调度：首跑 + 窗口内两次重试。文件已在时后两枪直接跳过。"""
    return [
        {"id": "auction_scan_092530", "hour": 9, "minute": 25, "second": 30},
        {"id": "auction_scan_092630", "hour": 9, "minute": 26, "second": 30},
        {"id": "auction_scan_092730", "hour": 9, "minute": 27, "second": 30},
    ]


def enabled() -> bool:
    """``AUCTION_SCAN_ENABLED`` 未设置或空白时开启。显式 false/0/no/off 才关。"""
    raw = os.environ.get("AUCTION_SCAN_ENABLED")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in _ON


def min_gap() -> float:
    return _env_float("AUCTION_SCAN_MIN_GAP", 0.05)


def min_vol_ratio() -> float:
    return _env_float("AUCTION_SCAN_MIN_VOL_RATIO", 10.0)


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退 %s", name, raw, default)
        return default


def _as_cn(now: datetime | None) -> datetime:
    current = now or cn_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=CN_TZ)
    return current.astimezone(CN_TZ)


def in_scan_window(now: datetime) -> bool:
    clock = _as_cn(now).timetz().replace(tzinfo=None)
    return SCAN_START <= clock < SCAN_END


def cache_path(data_dir: Path, day: date_cls) -> Path:
    return data_dir / "auction_scan" / f"{day.isoformat()}.json"


def _num(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _positive(value: object) -> float | None:
    number = _num(value)
    if number is None or number <= 0:
        return None
    return number


def _field_in_use(rows: list[dict], keys: tuple[str, ...]) -> str | None:
    """快照里第一个「至少有一行是数字」的候选字段。空列不当作有这个字段。"""
    present: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            present.update(row.keys())
    for key in keys:
        if key not in present:
            continue
        for row in rows:
            if isinstance(row, dict) and _num(row.get(key)) is not None:
                return key
    return None


def _trading_days(data_dir: Path) -> list[date_cls]:
    root = data_dir / "kline_daily"
    out: list[date_cls] = []
    try:
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.startswith("date="):
                continue
            try:
                out.append(date_cls.fromisoformat(entry.name[5:]))
            except ValueError:
                continue
    except OSError:
        return []
    return sorted(out)


def avg_daily_volume_hands(data_dir: Path, as_of: date_cls, sessions: int = LOOKBACK_DAYS) -> dict[str, float]:
    """扫描日之前最近若干交易日的日均成交量（手）。当日分区不参与。"""
    days = [day for day in _trading_days(data_dir) if day < as_of][-sessions:]
    frames: list[pl.LazyFrame] = []
    for day in days:
        root = data_dir / "kline_daily" / f"date={day.isoformat()}"
        files = sorted(root.glob("*.parquet"))
        if not files:
            continue
        try:
            frames.append(
                pl.scan_parquet([str(path) for path in files]).select(
                    pl.col("symbol").cast(pl.Utf8),
                    pl.col("volume").cast(pl.Float64),
                )
            )
        except (OSError, pl.exceptions.PolarsError):
            continue
    if not frames:
        return {}
    try:
        df = (
            pl.concat(frames, how="diagonal_relaxed")
            .filter(
                pl.col("symbol").is_not_null()
                & pl.col("volume").is_not_null()
                & (pl.col("volume") > 0)
            )
            .group_by("symbol")
            .agg(pl.col("volume").mean().alias("avg"))
            .collect()
        )
    except (OSError, pl.exceptions.PolarsError):
        logger.warning("竞价扫描读取近 %d 日成交量失败", sessions, exc_info=True)
        return {}
    return dict(zip(df["symbol"].to_list(), df["avg"].to_list(), strict=True))


def _volume_ratio_from_shares(auction_shares: float | None, avg_hands: float | None) -> float | None:
    if auction_shares is None or auction_shares < 0 or not avg_hands or avg_hands <= 0:
        return None
    baseline_shares = avg_hands * SHARES_PER_HAND / SESSION_MINUTES
    if baseline_shares <= 0:
        return None
    return auction_shares / baseline_shares


def _resolve_name(row: dict, symbol: str, name_map: dict[str, str]) -> str:
    raw = row.get("name")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    mapped = name_map.get(symbol)
    if isinstance(mapped, str) and mapped.strip():
        return mapped.strip()
    return symbol


def _auction_price(row: dict) -> float | None:
    return _positive(row.get("open_price")) or _positive(row.get("last_price"))


def _prev_close(row: dict) -> float | None:
    return _positive(row.get("prev_price")) or _positive(row.get("prev_close_price"))


def build_lists(
    rows: list[dict],
    avg_hands: dict[str, float],
    name_map: dict[str, str] | None = None,
    *,
    gap_min: float | None = None,
    ratio_min: float | None = None,
) -> tuple[list[dict], list[dict], str]:
    """返回 (命中列表, 高开≥3% 宽候选, volume_basis)。

    宽候选包含命中列表，方便以后只调阈值、不再抓快照。
    """
    names = name_map or {}
    gap_floor = min_gap() if gap_min is None else gap_min
    ratio_floor = min_vol_ratio() if ratio_min is None else ratio_min
    ratio_key = _field_in_use(rows, _VOLUME_RATIO_KEYS)
    auction_key = _field_in_use(rows, _AUCTION_VOLUME_KEYS)
    if ratio_key:
        basis = VOLUME_BASIS_RATIO
    elif auction_key:
        basis = VOLUME_BASIS_AUCTION_VOL
    else:
        basis = VOLUME_BASIS_AVG5

    hits: list[dict] = []
    wide: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("thscode") or "").strip()
        if not symbol:
            continue
        price = _auction_price(row)
        prev = _prev_close(row)
        if price is None or prev is None:
            continue
        gap = round(price / prev - 1.0, 6)
        if basis == VOLUME_BASIS_RATIO:
            ratio = _num(row.get(ratio_key))
            shares = _num(row.get("volume"))
        elif basis == VOLUME_BASIS_AUCTION_VOL:
            shares = _num(row.get(auction_key))
            ratio = _volume_ratio_from_shares(shares, avg_hands.get(symbol))
        else:
            shares = _num(row.get("volume"))
            ratio = _volume_ratio_from_shares(shares, avg_hands.get(symbol))
        if ratio is not None:
            ratio = round(ratio, 4)
        item = {
            "symbol": symbol,
            "ticker": (str(row["ticker"]).strip() if row.get("ticker") else None) or None,
            "name": _resolve_name(row, symbol, names),
            "gap": gap,
            "vol_ratio": ratio,
            "auction_volume": None if shares is None else round(shares / SHARES_PER_HAND, 4),
            "auction_amount": _amount(row),
        }
        if gap + 1e-9 >= WIDE_MIN_GAP:
            wide.append(item)
        if gap + 1e-9 >= gap_floor and ratio is not None and ratio + 1e-9 >= ratio_floor:
            hits.append(item)

    hits.sort(key=_sort_key)
    wide.sort(key=_sort_key)
    return hits, wide, basis


def _amount(row: dict) -> float | None:
    raw = _num(row.get("auction_amount"))
    if raw is None:
        raw = _num(row.get("turnover"))
    if raw is None or raw < 0:
        return None
    return round(raw, 2)


def _sort_key(item: dict) -> tuple:
    ratio = item.get("vol_ratio")
    return (
        -float(item["gap"]),
        -(float(ratio) if isinstance(ratio, (int, float)) else -1.0),
        str(item["symbol"]),
    )


def _store(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _load(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def list_dates(data_dir: Path) -> list[str]:
    """已落盘日期，新的在前。"""
    root = data_dir / "auction_scan"
    found: list[str] = []
    try:
        for entry in root.iterdir():
            match = _DATE_FILE_RE.match(entry.name)
            if entry.is_file() and match:
                found.append(match.group(1))
    except OSError:
        return []
    return sorted(found, reverse=True)


def _is_trading_day(now: datetime, trading: bool | None) -> bool:
    if trading is not None:
        return trading
    from app.services.trading_day import is_trading_day

    verdict = is_trading_day(now)
    if verdict is False:
        return False
    # 日历探针未知时，工作日仍尝试；周末 is_trading_day 已直接返回 False。
    return _as_cn(now).weekday() < 5


def _make_client():
    from app.plugins.fuyao.client import FuyaoClient, FuyaoError
    from app.plugins.fuyao.provider import get_api_key

    key = get_api_key()
    if not key:
        raise FuyaoError("未配置 FUYAO_API_KEY")
    return FuyaoClient(api_key=key, timeout=30.0)


def run_scan(
    data_dir: Path,
    *,
    now: datetime | None = None,
    trading: bool | None = None,
    client=None,
    name_map: dict[str, str] | None = None,
) -> dict:
    """执行一次扫描。返回 ``{status, reason?}``。失败不落盘，留给窗口内重试。"""
    current = _as_cn(now)
    day = current.date()
    if not enabled():
        return {"status": "skipped", "reason": "disabled", "trade_date": day.isoformat()}
    if not _is_trading_day(current, trading):
        return {"status": "skipped", "reason": "not_trading_day", "trade_date": day.isoformat()}
    if not in_scan_window(current):
        return {"status": "skipped", "reason": "outside_window", "trade_date": day.isoformat()}

    if not _RUN_LOCK.acquire(blocking=False):
        return {"status": "skipped", "reason": "busy", "trade_date": day.isoformat()}
    try:
        path = cache_path(data_dir, day)
        if path.exists():
            return {"status": "skipped", "reason": "exists", "trade_date": day.isoformat()}
        return _fetch_and_store(
            data_dir, day, current, client=client, name_map=name_map or {},
        )
    finally:
        _RUN_LOCK.release()


def _fetch_and_store(
    data_dir: Path,
    day: date_cls,
    now: datetime,
    *,
    client,
    name_map: dict[str, str],
) -> dict:
    from app.plugins.fuyao.client import FuyaoError

    owned = client is None
    if owned:
        try:
            client = _make_client()
        except FuyaoError as exc:
            logger.warning("竞价扫描未执行: %s", exc)
            return {"status": "failed", "reason": "fetch_failed", "trade_date": day.isoformat()}
    try:
        try:
            rows, server_ts = client.snapshot_all()
        except FuyaoError as exc:
            logger.warning("竞价扫描快照失败: %s", exc)
            return {"status": "failed", "reason": "fetch_failed", "trade_date": day.isoformat()}
    finally:
        if owned:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 — 关闭失败不影响结果
                    logger.debug("竞价扫描客户端关闭失败", exc_info=True)
    if not rows:
        return {"status": "failed", "reason": "empty_snapshot", "trade_date": day.isoformat()}

    avg_hands = avg_daily_volume_hands(data_dir, day)
    gap_floor = min_gap()
    ratio_floor = min_vol_ratio()
    hits, wide, basis = build_lists(
        rows, avg_hands, name_map, gap_min=gap_floor, ratio_min=ratio_floor,
    )
    payload = {
        "trade_date": day.isoformat(),
        "scanned_at": now.isoformat(timespec="seconds"),
        "server_ts": int(server_ts or 0),
        "scanned_count": len(rows),
        "volume_basis": basis,
        "volume_unit": "hand",
        "min_gap": gap_floor,
        "min_vol_ratio": ratio_floor,
        "wide_min_gap": WIDE_MIN_GAP,
        "items": hits,
        "candidates": wide,
    }
    _store(cache_path(data_dir, day), payload)
    logger.info(
        "竞价扫描完成 %s: 扫描 %d 只, 命中 %d, 宽候选 %d, 量比口径 %s",
        day.isoformat(), len(rows), len(hits), len(wide), basis,
    )
    return {
        "status": "ok",
        "trade_date": day.isoformat(),
        "scanned_count": len(rows),
        "hit_count": len(hits),
        "candidate_count": len(wide),
        "volume_basis": basis,
    }


def run_scheduled(repo) -> dict:
    """调度器入口。名称来自维表；维表失败时仍落代码。"""
    data_dir = repo.store.data_dir
    names: dict[str, str] = {}
    try:
        names = repo.get_name_map() or {}
    except Exception:  # noqa: BLE001 — 名称缺失不阻断扫描
        logger.warning("竞价扫描读取名称失败", exc_info=True)
    return run_scan(data_dir, name_map=names)


def get_scan(data_dir: Path, target: date_cls | None = None) -> dict:
    """读取某日扫描。文件不在时 ``state=no_data``，并带上已有日期。"""
    day = target or cn_today()
    dates = list_dates(data_dir)
    payload = _load(cache_path(data_dir, day))
    if payload is None:
        return {
            "state": "no_data",
            "trade_date": day.isoformat(),
            "scanned_at": None,
            "server_ts": None,
            "scanned_count": None,
            "volume_basis": None,
            "min_gap": None,
            "min_vol_ratio": None,
            "dates": dates,
            "items": [],
            "message": "当日无采集数据",
        }
    items = payload.get("items")
    return {
        "state": "ok",
        "trade_date": str(payload.get("trade_date") or day.isoformat()),
        "scanned_at": payload.get("scanned_at"),
        "server_ts": payload.get("server_ts"),
        "scanned_count": payload.get("scanned_count"),
        "volume_basis": payload.get("volume_basis"),
        "min_gap": payload.get("min_gap"),
        "min_vol_ratio": payload.get("min_vol_ratio"),
        "dates": dates,
        "items": items if isinstance(items, list) else [],
    }


def register_jobs(scheduler, repo) -> None:
    """挂到现有 APScheduler。三枪都进同一函数，幂等由落盘文件保证。"""
    from apscheduler.triggers.cron import CronTrigger

    def _run() -> None:
        try:
            result = run_scheduled(repo)
        except Exception:
            logger.exception("竞价扫描任务失败")
            return
        logger.info("auction scan %s", result.get("status"))

    for spec in schedule_specs():
        scheduler.add_job(
            _run,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=int(spec["hour"]),
                minute=int(spec["minute"]),
                second=int(spec["second"]),
                timezone="Asia/Shanghai",
            ),
            id=str(spec["id"]),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_S,
            replace_existing=True,
        )
    if enabled():
        logger.info("auction scan scheduled @09:25:30/09:26:30/09:27:30 Asia/Shanghai")
    else:
        logger.info("auction scan jobs registered, AUCTION_SCAN_ENABLED is off")
