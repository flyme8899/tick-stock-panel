"""收盘后拉取选股补充字段。

东财函数经 ``call_akshare`` 进共用限流。中证指数公司的成分接口不是东财，
原样走 akshare。测试传入假的 ``ak``，这里不在模块导入时加载 akshare。
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
    combine_index_members,
    parse_holders,
    parse_index_members,
    parse_northbound,
    parse_pe_ttm,
    parse_unlock_schedule,
)
from app.selection_fields.store import (
    commit,
    load_checkpoint,
    read_frame,
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
    from app.eastmoney.akshare_gate import call_akshare

    return call_akshare(name, **kwargs)


def listed_symbols(data_dir: Path | str) -> list[str]:
    """A 股代码。先读股票列表，没有再退到最近一份日 K。"""
    symbols = _symbols_from_parquet(Path(data_dir) / "instruments")
    if symbols:
        return symbols
    return _symbols_from_enriched(Path(data_dir))


def sync_northbound(ak: Ak, data_dir: Path | str) -> dict:
    try:
        raw = ak("stock_hsgt_hold_stock_em", market="北向", indicator="今日排行")
        rows = parse_northbound(as_records(raw))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:300]
        commit(data_dir, "northbound", [], ok=False, error=message)
        return {"ok": False, "error": message}
    commit(data_dir, "northbound", rows, ok=True)
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


def sync_indexes(ak: Ak, data_dir: Path | str) -> dict:
    """任一指数失败都不替换已有成分快照。"""
    parts: list[tuple[str, dict[str, str]]] = []
    failed: list[str] = []
    for code, label in INDEXES:
        try:
            raw = ak("index_stock_cons_csindex", symbol=code)
        except Exception:  # noqa: BLE001
            failed.append(label)
            continue
        parts.append((label, parse_index_members(as_records(raw), label)))
    if failed:
        message = f"指数成分未完成：{'、'.join(failed)}"
        update_meta(data_dir, "index_members", status="error", error=message)
        return {"ok": False, "error": message}
    rows = combine_index_members(parts)
    commit(data_dir, "index_members", rows, ok=True)
    return {"ok": True, "rows": len(rows)}


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
