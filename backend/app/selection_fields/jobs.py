"""收盘后拉取选股补充字段。

东财函数经 ``call_akshare`` 进共用限流，而且只包住当前线程。
北向持股改拉季度口径，不再调用已经停更的逐日排行。
中证指数走中证指数公司，创业板50 走国证，都不是东财。
每个指数单独落盘。测试传入假的 ``ak``，这里不在模块导入时加载 akshare。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.selection_fields.parse import (
    INDEXES,
    ashare_symbol,
    code6,
    northbound_note,
    parse_holders,
    parse_index_members,
    parse_northbound,
    parse_pe_ttm,
    parse_unlock_schedule,
)
from app.selection_fields.store import (
    commit,
    commit_index,
    load_checkpoint,
    read_frame,
    read_index_meta,
    save_checkpoint,
    update_meta,
    upsert_pe_rows,
)

Ak = Callable[..., Any]


def as_records(frame: Any) -> list[dict]:
    if frame is None:
        return []
    if isinstance(frame, list):
        return [row for row in frame if isinstance(row, dict)]
    to_dicts = getattr(frame, "to_dicts", None)
    if callable(to_dicts):
        return list(to_dicts())
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            raw = to_dict(orient="records")
        except TypeError:
            raw = to_dict()
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
    return []


def live_ak(name: str, /, **kwargs: Any) -> Any:
    if name == "northbound_quarter":
        return fetch_northbound_quarter()
    from app.eastmoney.akshare_gate import call_akshare

    return call_akshare(name, **kwargs)


def listed_symbols(data_dir: Path | str) -> list[str]:
    """A 股代码。先读股票列表，没有再退到最近一份日 K。"""
    symbols = _symbols_from_parquet(Path(data_dir) / "instruments")
    if symbols:
        return symbols
    return _symbols_from_enriched(Path(data_dir))


_NORTHBOUND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_NORTHBOUND_REFERER = "https://data.eastmoney.com/hsgtV2/hsgtDetail/bkzj.html"
_NORTHBOUND_PAGE_SIZE = 500
_NORTHBOUND_MAX_PAGES = 20
_EMPTY_CODE = 9201


def fetch_northbound_quarter() -> list[dict]:
    """最近一个有数据的季度持股。逐日接口自 2024-08 起没有数据，这里不调用它。"""
    dates = _northbound_dates()
    if not dates:
        raise RuntimeError("北向季度日期为空")
    for day in dates:
        rows = _northbound_rows(day)
        if rows:
            return rows
    raise RuntimeError("北向季度持股为空")


def sync_northbound(ak: Ak, data_dir: Path | str) -> dict:
    try:
        raw = ak("northbound_quarter")
        rows = parse_northbound(as_records(raw))
        if not rows:
            raise RuntimeError("北向季度持股为空")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:300]
        commit(data_dir, "northbound", [], ok=False, error=message)
        return {"ok": False, "error": message}
    day = next((str(row.get("trade_date") or "") for row in rows if row.get("trade_date")), "")
    commit(data_dir, "northbound", rows, ok=True, note=northbound_note(day))
    return {"ok": True, "rows": len(rows)}


def sync_holders(ak: Ak, data_dir: Path | str) -> dict:
    try:
        raw = ak("stock_zh_a_gdhs", symbol="最新")
        rows = parse_holders(as_records(raw))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:300]
        commit(data_dir, "holders", [], ok=False, error=message)
        return {"ok": False, "error": message}
    commit(data_dir, "holders", rows, ok=True)
    return {"ok": True, "rows": len(rows)}


def sync_indexes(ak: Ak, data_dir: Path | str, day: str | None = None) -> dict:
    """每个指数单独落盘。一只失败只保留它自己的上次快照，其它指数照常写入。"""
    failed: list[str] = []
    written = 0
    for code, label, source in INDEXES:
        meta = read_index_meta(data_dir, code)
        if day and meta.get("status") == "ok" and str(meta.get("as_of") or "") == day:
            written += 1
            continue
        try:
            raw = ak(source, symbol=code)
            mapping = parse_index_members(as_records(raw), label)
            if not mapping:
                raise RuntimeError(f"{label}成分为空")
        except Exception as exc:  # noqa: BLE001
            failed.append(label)
            commit_index(data_dir, code, [], ok=False, error=str(exc)[:300])
            continue
        rows = [{"symbol": symbol, "index_membership": label} for symbol in sorted(mapping)]
        commit_index(data_dir, code, rows, ok=True, as_of=day or "")
        written += 1
    if failed:
        message = f"指数成分未完成：{'、'.join(failed)}"
        return {"ok": False, "error": message, "rows": written}
    return {"ok": True, "rows": written}


def sync_unlock(ak: Ak, data_dir: Path | str, today: date) -> dict:
    start = today.strftime("%Y%m%d")
    end = (today + timedelta(days=120)).strftime("%Y%m%d")
    try:
        raw = ak("stock_restricted_release_detail_em", start_date=start, end_date=end)
        rows = parse_unlock_schedule(as_records(raw), today=today)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:300]
        commit(data_dir, "unlock", [], ok=False, error=message)
        return {"ok": False, "error": message}
    commit(data_dir, "unlock", rows, ok=True)
    return {"ok": True, "rows": len(rows)}


def sync_pe_symbol(ak: Ak, symbol: str) -> float | None:
    raw = ak("stock_value_em", symbol=code6(symbol))
    return parse_pe_ttm(as_records(raw))


def advance_pe(
    data_dir: Path | str,
    ak: Ak,
    day: str,
    *,
    flush_every: int,
    busy: Callable[[], bool],
    stop: Callable[[], bool] | None = None,
) -> dict:
    """续跑当天 PE。已完成的代码跳过。一次失败就停，避免熔断时把全市场记成失败。"""
    symbols = listed_symbols(data_dir)
    if not symbols:
        note = "没有股票列表"
        status = "ok" if read_frame(data_dir, "pe_ttm").height else "error"
        update_meta(data_dir, "pe_ttm", status=status, note=note)
        return {"complete": False, "done": 0, "total": 0, "note": note}

    checkpoint = load_checkpoint(data_dir)
    if str(checkpoint.get("trade_date") or "") != day:
        checkpoint = {
            "trade_date": day,
            "done": [],
            "skipped": [],
            "attempts": {},
            "total": len(symbols),
        }
    done = {str(item) for item in (checkpoint.get("done") or [])}
    skipped = {str(item) for item in (checkpoint.get("skipped") or [])}
    attempts = {str(key): int(value) for key, value in (checkpoint.get("attempts") or {}).items()}
    remaining = [symbol for symbol in symbols if symbol not in done and symbol not in skipped]
    buffer: list[dict] = []
    every = max(1, flush_every)

    def persist() -> None:
        if buffer:
            upsert_pe_rows(data_dir, buffer)
            buffer.clear()
        save_checkpoint(
            data_dir,
            {
                "trade_date": day,
                "done": sorted(done),
                "skipped": sorted(skipped),
                "attempts": attempts,
                "total": len(symbols),
            },
        )

    for symbol in remaining:
        if busy() or (stop is not None and stop()):
            persist()
            break
        try:
            pe = sync_pe_symbol(ak, symbol)
        except Exception:  # noqa: BLE001
            attempts[symbol] = attempts.get(symbol, 0) + 1
            if attempts[symbol] >= 2:
                skipped.add(symbol)
                attempts.pop(symbol, None)
            persist()
            break
        buffer.append({"symbol": symbol, "pe_ttm": pe, "as_of": day})
        done.add(symbol)
        attempts.pop(symbol, None)
        if len(buffer) >= every:
            persist()
    else:
        persist()

    finished = len(done) + len(skipped)
    return {
        "complete": finished >= len(symbols),
        "done": len(done),
        "total": len(symbols),
        "note": "",
    }


def _em_json(params: dict) -> dict:
    from app.eastmoney.client import em_get
    from app.selection_fields.timeouts import REQUEST_TIMEOUT

    response = em_get(
        _NORTHBOUND_URL,
        params=params,
        headers={"Referer": _NORTHBOUND_REFERER},
        timeout=REQUEST_TIMEOUT,
    )
    reader = getattr(response, "json", None)
    payload = reader() if callable(reader) else None
    if not isinstance(payload, dict):
        raise RuntimeError("北向季度接口返回异常")
    return payload


def _northbound_dates() -> list[str]:
    payload = _em_json({
        "reportName": "RPT_MUTUAL_BOARD_HOLDRANKDATE",
        "columns": "TRADE_DATE,DATE_TYPE",
        "pageNumber": "1",
        "pageSize": "10",
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    })
    if not payload.get("success"):
        if int(payload.get("code") or 0) == _EMPTY_CODE:
            return []
        raise RuntimeError(str(payload.get("message") or "北向季度日期失败")[:300])
    days: list[str] = []
    for item in (payload.get("result") or {}).get("data") or []:
        if not isinstance(item, dict):
            continue
        day = _day_text(item.get("TRADE_DATE"))
        if day and day not in days:
            days.append(day)
    return days


def _northbound_rows(day: str) -> list[dict]:
    rows: list[dict] = []
    page = 1
    pages = 1
    while page <= pages:
        if page > _NORTHBOUND_MAX_PAGES:
            raise RuntimeError("北向季度持股页数异常")
        payload = _em_json({
            "reportName": "RPT_MUTUAL_HOLDRANK_NEW",
            "columns": "SECURITY_CODE,SECUCODE,HOLD_DATE,FREE_SHARES_RATIO",
            "pageNumber": str(page),
            "pageSize": str(_NORTHBOUND_PAGE_SIZE),
            "sortColumns": "HOLD_MARKET_CAP",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
            "filter": f"(HOLD_DATE='{day}')",
        })
        if not payload.get("success"):
            if page == 1 and int(payload.get("code") or 0) == _EMPTY_CODE:
                return []
            raise RuntimeError(str(payload.get("message") or "北向季度持股失败")[:300])
        result = payload.get("result") or {}
        chunk = result.get("data") or []
        if not isinstance(chunk, list):
            raise RuntimeError("北向季度持股返回异常")
        rows.extend(item for item in chunk if isinstance(item, dict))
        pages = int(result.get("pages") or 1)
        if pages > _NORTHBOUND_MAX_PAGES:
            raise RuntimeError("北向季度持股页数异常")
        if not chunk:
            break
        page += 1
    return rows


def _day_text(value: object) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _symbols_from_parquet(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    frames: list[pl.DataFrame] = []
    for path in sorted(directory.rglob("*.parquet")):
        try:
            frames.append(pl.read_parquet(path, columns=["symbol"]))
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        return []
    values = pl.concat(frames, how="vertical")["symbol"].to_list()
    return sorted({symbol for raw in values if (symbol := ashare_symbol(raw))})


def _symbols_from_enriched(data_dir: Path) -> list[str]:
    root = data_dir / "kline_daily_enriched"
    if not root.exists():
        return []
    parts = sorted(path for path in root.glob("date=*") if path.is_dir())
    target = parts[-1] / "part.parquet" if parts else None
    paths = [target] if target and target.exists() else sorted(root.rglob("*.parquet"))
    if not paths:
        return []
    try:
        frame = pl.read_parquet(paths[-1], columns=["symbol"])
    except Exception:  # noqa: BLE001
        return []
    return sorted({symbol for raw in frame["symbol"].to_list() if (symbol := ashare_symbol(raw))})
