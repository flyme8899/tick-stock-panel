"""收盘后拉取选股补充字段。

东财函数经 ``call_akshare`` 进共用限流，而且只包住当前线程。
北向持股改拉季度口径，不再调用已经停更的逐日排行。
PE(TTM) 先按交易日分页拉全市场，缺的代码才逐只补历史。
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
    parse_pe_market,
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
        return fetch_northbound_quarter(stop=kwargs.get("stop"))
    if name == "pe_market":
        return fetch_pe_market_page(str(kwargs.get("day") or ""), int(kwargs.get("page") or 1))
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
_PE_REPORT = "RPT_VALUEANALYSIS_DET"
_PE_PAGE_SIZE = 500
_PE_MAX_PAGES = 40
_PE_REFERER = "https://data.eastmoney.com/gzfx/detail/600519.html"


class SelectionStoppedError(Exception):
    """当前子任务被要求停下来。不把半截结果写成失败。"""


def fetch_northbound_quarter(stop: Callable[[], bool] | None = None) -> list[dict]:
    """最近一个有数据的季度持股。逐日接口自 2024-08 起没有数据，这里不调用它。"""
    dates = _northbound_dates()
    if not dates:
        raise RuntimeError("北向季度日期为空")
    for day in dates:
        _raise_if_stopped(stop)
        rows = _northbound_rows(day, stop=stop)
        if rows:
            return rows
    raise RuntimeError("北向季度持股为空")


def sync_northbound(
    ak: Ak,
    data_dir: Path | str,
    stop: Callable[[], bool] | None = None,
) -> dict:
    try:
        raw = ak("northbound_quarter", stop=stop)
        rows = parse_northbound(as_records(raw))
        if not rows:
            raise RuntimeError("北向季度持股为空")
    except SelectionStoppedError:
        return {"ok": False, "stopped": True}
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


def sync_indexes(
    ak: Ak,
    data_dir: Path | str,
    day: str | None = None,
    stop: Callable[[], bool] | None = None,
) -> dict:
    """每个指数单独落盘。一只失败只保留它自己的上次快照，其它指数照常写入。"""
    failed: list[str] = []
    written = 0
    for code, label, source in INDEXES:
        if stop is not None and stop():
            return {"ok": False, "stopped": True, "rows": written}
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
    """先按交易日分页拉全市场 PE(TTM)，缺的代码再逐只补。单只失败直接跳过。"""
    from app.eastmoney.client import CircuitOpenError

    symbols = listed_symbols(data_dir)
    if not symbols:
        note = "没有股票列表"
        status = "ok" if read_frame(data_dir, "pe_ttm").height else "error"
        update_meta(data_dir, "pe_ttm", status=status, note=note)
        return {"complete": False, "done": 0, "total": 0, "note": note}

    checkpoint = load_checkpoint(data_dir)
    if str(checkpoint.get("trade_date") or "") != day:
        checkpoint = {}
    done = {str(item) for item in (checkpoint.get("done") or [])}
    skipped = {str(item) for item in (checkpoint.get("skipped") or [])}
    batch_done = bool(checkpoint.get("batch_done"))
    batch_page = max(1, int(checkpoint.get("batch_page") or 1))
    batch_date = str(checkpoint.get("batch_date") or "") or day
    tried_fallback = bool(checkpoint.get("batch_fallback"))
    wanted = set(symbols)
    buffer: list[dict] = []
    every = max(1, flush_every)

    def halted() -> bool:
        return busy() or (stop is not None and stop())

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
                "total": len(symbols),
                "batch_done": batch_done,
                "batch_page": batch_page,
                "batch_date": batch_date,
                "batch_fallback": tried_fallback,
            },
        )

    def finish(note: str = "", error: str = "") -> dict:
        persist()
        payload = _pe_status(done, skipped, symbols, note)
        if error:
            payload["error"] = error
        elif payload["complete"] and batch_date and batch_date != day:
            payload["note"] = f"PE(TTM) 截至 {batch_date}"
            update_meta(data_dir, "pe_ttm", status="ok", note=payload["note"])
        return payload

    if not batch_done:
        while not batch_done:
            if halted():
                return finish()
            try:
                page = ak("pe_market", day=batch_date, page=batch_page)
            except CircuitOpenError:
                return finish(error="eastmoney 熔断冷却中")
            except Exception as exc:  # noqa: BLE001
                return finish(error=str(exc)[:300])
            if not isinstance(page, dict):
                return finish(error="PE(TTM) 全市场分页返回异常")
            rows = page.get("rows") or []
            pages = int(page.get("pages") or 0)
            if page.get("empty") or pages <= 0 or not isinstance(rows, list) or not rows:
                latest = str(page.get("latest") or "")
                if batch_page == 1 and not tried_fallback and latest and latest != batch_date:
                    tried_fallback = True
                    batch_date = latest
                    batch_page = 1
                    persist()
                    continue
                return finish(error="PE(TTM) 全市场分页为空")
            if pages > _PE_MAX_PAGES:
                return finish(error="PE(TTM) 全市场页数异常")
            for item in parse_pe_market(rows):
                symbol = str(item["symbol"])
                if symbol not in wanted or symbol in done or symbol in skipped:
                    continue
                pe = item.get("pe_ttm")
                if pe is None:
                    skipped.add(symbol)
                    continue
                buffer.append({"symbol": symbol, "pe_ttm": pe, "as_of": batch_date})
                done.add(symbol)
            if batch_page >= pages:
                batch_done = True
            else:
                batch_page += 1
            persist()

    for symbol in symbols:
        if symbol in done or symbol in skipped:
            continue
        if halted():
            return finish()
        try:
            pe = sync_pe_symbol(ak, symbol)
        except CircuitOpenError:
            return finish(error="eastmoney 熔断冷却中")
        except Exception:  # noqa: BLE001
            skipped.add(symbol)
            persist()
            continue
        if pe is None:
            skipped.add(symbol)
            persist()
            continue
        buffer.append({"symbol": symbol, "pe_ttm": pe, "as_of": batch_date or day})
        done.add(symbol)
        if len(buffer) >= every:
            persist()
    return finish()


def fetch_pe_market_page(day: str, page: int) -> dict:
    """估值分析按交易日分页。字段是 PE_TTM，不拉每只股票的历史。"""
    if page < 1 or page > _PE_MAX_PAGES:
        raise RuntimeError("PE(TTM) 全市场页数异常")
    payload = _em_json(
        {
            "reportName": _PE_REPORT,
            "columns": "SECURITY_CODE,TRADE_DATE,PE_TTM",
            "pageNumber": str(page),
            "pageSize": str(_PE_PAGE_SIZE),
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "source": "WEB",
            "client": "WEB",
            "filter": f"(TRADE_DATE='{day}')",
        },
        what="PE(TTM)",
        referer=_PE_REFERER,
    )
    if not payload.get("success"):
        if int(payload.get("code") or 0) == _EMPTY_CODE:
            latest = _latest_pe_day() if page == 1 else ""
            return {"rows": [], "pages": 0, "empty": True, "latest": latest}
        raise RuntimeError(str(payload.get("message") or "PE(TTM) 全市场分页失败")[:300])
    result = payload.get("result") or {}
    rows = result.get("data") or []
    if not isinstance(rows, list):
        raise RuntimeError("PE(TTM) 全市场分页返回异常")
    pages = int(result.get("pages") or 0)
    if pages > _PE_MAX_PAGES:
        raise RuntimeError("PE(TTM) 全市场页数异常")
    return {"rows": rows, "pages": pages, "empty": pages <= 0, "latest": ""}


def _latest_pe_day() -> str:
    """会话日还没有估值时，用一只长期有数据的代码看最近交易日。"""
    payload = _em_json(
        {
            "reportName": _PE_REPORT,
            "columns": "TRADE_DATE",
            "pageNumber": "1",
            "pageSize": "1",
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
            "filter": '(SECURITY_CODE="000001")',
        },
        what="PE(TTM)",
        referer=_PE_REFERER,
    )
    if not payload.get("success"):
        return ""
    data = (payload.get("result") or {}).get("data") or []
    if not data or not isinstance(data[0], dict):
        return ""
    return _day_text(data[0].get("TRADE_DATE"))


def _pe_status(done: set[str], skipped: set[str], symbols: list[str], note: str) -> dict:
    return {
        "complete": len(done) + len(skipped) >= len(symbols),
        "done": len(done),
        "total": len(symbols),
        "note": note,
    }


def _raise_if_stopped(stop: Callable[[], bool] | None) -> None:
    if stop is not None and stop():
        raise SelectionStoppedError


def _em_json(params: dict, *, what: str, referer: str | None = None) -> dict:
    from app.eastmoney.client import em_get
    from app.selection_fields.timeouts import REQUEST_TIMEOUT

    response = em_get(
        _NORTHBOUND_URL,
        params=params,
        headers={"Referer": referer or _NORTHBOUND_REFERER},
        timeout=REQUEST_TIMEOUT,
    )
    reader = getattr(response, "json", None)
    payload = reader() if callable(reader) else None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{what}返回异常")
    return payload


def _northbound_dates() -> list[str]:
    payload = _em_json(
        {
            "reportName": "RPT_MUTUAL_BOARD_HOLDRANKDATE",
            "columns": "TRADE_DATE,DATE_TYPE",
            "pageNumber": "1",
            "pageSize": "10",
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        },
        what="北向季度接口",
    )
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


def _northbound_rows(day: str, stop: Callable[[], bool] | None = None) -> list[dict]:
    rows: list[dict] = []
    page = 1
    pages = 1
    while page <= pages:
        _raise_if_stopped(stop)
        if page > _NORTHBOUND_MAX_PAGES:
            raise RuntimeError("北向季度持股页数异常")
        payload = _em_json(
            {
                "reportName": "RPT_MUTUAL_HOLDRANK_NEW",
                "columns": "SECURITY_CODE,SECUCODE,HOLD_DATE,FREE_SHARES_RATIO",
                "pageNumber": str(page),
                "pageSize": str(_NORTHBOUND_PAGE_SIZE),
                "sortColumns": "HOLD_MARKET_CAP",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
                "filter": f"(HOLD_DATE='{day}')",
            },
            what="北向季度接口",
        )
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
