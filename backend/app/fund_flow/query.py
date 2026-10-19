"""给 API 读的查询。只读本地 parquet，不触发出网。"""
from __future__ import annotations

import re
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.fund_flow import etf_inflow, etf_meta, factors, normalize, store
from app.fund_flow.scheduler import current_flags, get_worker
from app.fund_flow.units import percent_points_to_decimal
from app.market_time import CN_TZ, cn_now, cn_today

# 收盘时刻。过了这个点，当天盘中快照就不再代表「今日」，改读当天收盘分区。
# 用 15:00 而不是收盘数据就绪的 16:30：15:00 一过，盘中快照的技术含义就变了
# （定格的最后一笔竞价），继续拿它当「今日」会把旧快照当成实时。
_INTRADAY_FRESH_UNTIL = dt_time(15, 0)
# 收盘后仍可采用的当天盘中快照的最早取样时刻。14:45 之后基本接近收盘，
# 拿它当「收盘前快照」尚可接受；更早的（如 13:44）定格在午盘，会误导。
_STALE_INTRADAY_BEFORE = dt_time(14, 45)


def _dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings
    return Path(settings.data_dir)


def health(data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    state = store.load_state(root)
    worker = get_worker()
    kinds = {}
    for kind in (
        "stock", "stock_intraday", "industry", "concept", "margin", "etf_shares",
        "southbound", "northbound_turnover", "lhb",
    ):
        latest = store.latest_date(root, kind)
        rows = 0
        if latest:
            rows = store.read_partition(root, kind, latest).height
        saved = dict((state.get("kinds") or {}).get(kind) or {})
        kinds[kind] = {
            "latest_date": latest,
            "rows": rows,
            "last_error": saved.get("last_error"),
            "last_success_date": saved.get("last_success_date"),
            "last_attempt_at": saved.get("last_attempt_at"),
        }
    scheduler_state = (state.get("kinds") or {}).get("_scheduler") or {}
    return {
        "enabled": current_flags()["enabled"],
        "flags": current_flags(),
        "deferred_reason": worker.deferred_reason or scheduler_state.get("deferred"),
        "kinds": kinds,
        "sources": state.get("sources") or {},
    }


def _amount(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:
            return None
        return float(value)
    return None


def _snapshot_source(frame: pl.DataFrame) -> str | None:
    """一份快照只有一个源。混进来源时不猜，交给每行自己的 source。"""
    if frame.is_empty() or "source" not in frame.columns:
        return None
    found: list[str] = []
    for value in frame.get_column("source").drop_nulls().unique().to_list():
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)
    if len(found) == 1:
        return found[0]
    return None


def intraday_latest(
    symbols: list[str] | None = None,
    *,
    data_dir: Path | None = None,
    today: str | None = None,
) -> dict:
    """今天最近一份盘中个股资金快照。没有快照时 items 为空，不把缺失写成 0。

    东财行的 ``main_net`` 是今日主力净流入（元，正数净流入）。同花顺备用行仍是
    即时「净额」（流入减流出）。``source`` 写在响应上，也写在每一行。
    只读当天分区。昨天的快照不带到今天，避免开盘前把昨日盘中数当成即时。
    ``symbols`` 为空列表时不返回行，仍带上快照时间和来源。``None`` 表示不按代码过滤。
    """
    root = _dir(data_dir)
    day = today or cn_today().isoformat()
    frame = store.read_partition(root, "stock_intraday", day)
    captured = None
    if not frame.is_empty() and "captured_at" in frame.columns:
        captured = frame.select(pl.col("captured_at").max()).item()
        if captured:
            frame = frame.filter(pl.col("captured_at") == captured)
        else:
            frame = store.empty_frame("stock_intraday")
    else:
        frame = store.empty_frame("stock_intraday")
    snapshot_source = _snapshot_source(frame)
    if symbols is not None:
        codes = [code for symbol in symbols if (code := normalize.code6(symbol))]
        if codes and not frame.is_empty():
            frame = frame.filter(pl.col("code").is_in(codes))
        else:
            frame = store.empty_frame("stock_intraday")
    columns = [
        "code", "name", "main_net", "super_net", "large_net", "medium_net", "small_net", "source",
    ]
    items = []
    if not frame.is_empty():
        for row in frame.select(columns).iter_rows(named=True):
            amount = _amount(row.get("main_net"))
            if amount is None:
                continue
            items.append({
                "code": row.get("code"),
                "name": row.get("name"),
                "main_net": amount,
                "super_net": _amount(row.get("super_net")),
                "large_net": _amount(row.get("large_net")),
                "medium_net": _amount(row.get("medium_net")),
                "small_net": _amount(row.get("small_net")),
                "source": row.get("source") or None,
            })
    return {
        "trade_date": day if captured else None,
        "captured_at": captured,
        "source": snapshot_source,
        "items": items,
    }


def stock_series(symbol: str, *, limit: int = 120, data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    code = normalize.code6(symbol)
    try:
        frame = store.read_range(root, "stock")
    except (KeyError, pl.exceptions.ColumnNotFoundError):
        frame = store.empty_frame("stock")
    if code and not frame.is_empty() and "code" in frame.columns:
        frame = frame.filter(pl.col("code") == code).sort("trade_date")
    else:
        frame = store.empty_frame("stock")
    rows = frame.tail(limit).to_dicts()
    main = factors.main_net_frame(root)
    five = None
    if code and not main.is_empty():
        matched = main.filter(pl.col("code") == code).sort("trade_date")
        if not matched.is_empty():
            five = matched.tail(1).to_dicts()[0].get("ff_main_net_5d")
    return {
        "symbol": symbol,
        "code": code,
        "main_net_5d": five,
        "items": rows,
    }


def sectors(kind: str, *, trade_date: str | None = None, data_dir: Path | None = None) -> dict:
    if kind not in {"industry", "concept"}:
        raise ValueError("kind 只能是 industry 或 concept")
    root = _dir(data_dir)
    day = trade_date or store.latest_date(root, kind)
    if day is None:
        return {"kind": kind, "trade_date": None, "snapshot": None, "items": []}
    ranked = factors.latest_sector_ranks(root, kind, day)
    snapshot = "close"
    part = _partition(root, kind, day)
    if (
        not part.is_empty()
        and "snapshot" in part.columns
        and part.filter(pl.col("snapshot") == "close").is_empty()
    ):
        snapshot = "intraday"
    return {
        "kind": kind,
        "trade_date": day,
        "snapshot": snapshot,
        "items": ranked.sort("rank").to_dicts(),
    }


def margin(*, trade_date: str | None = None, detail_limit: int = 100, data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    store.repair_szse_margin_summaries(root)
    day = trade_date or store.latest_date(root, "margin")
    if day is None:
        return {"trade_date": None, "summary": [], "details": [], "detail_count": 0}
    frame = _partition(root, "margin", day)
    if frame.is_empty() or "row_kind" not in frame.columns:
        return {"trade_date": day, "summary": [], "details": [], "detail_count": 0}
    summary = frame.filter(pl.col("row_kind") == "summary").to_dicts()
    details = frame.filter(pl.col("row_kind") == "detail")
    return {
        "trade_date": day,
        "summary": summary,
        "details": details.head(detail_limit).to_dicts(),
        "detail_count": details.height,
    }


def _partition(root: Path, kind: str, trade_date: str) -> pl.DataFrame:
    """读一个分区。缺列或坏文件变成空表，不把 KeyError 变成 500。"""
    try:
        return store.read_partition(root, kind, trade_date)
    except (KeyError, pl.exceptions.ColumnNotFoundError):
        return store.empty_frame(kind)


def etf_shares(*, trade_date: str | None = None, data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    day = trade_date or store.latest_date(root, "etf_shares")
    if day is None:
        return {"trade_date": None, "etf_flow_linked": False, "items": []}
    frame = _partition(root, "etf_shares", day)
    linked = _etf_flow_map(root, day)
    inflows = etf_inflow.latest_inflows(root, as_of=day)
    baseline_days, baselines = _window_baselines(root, day)
    rebase = _rebase_factors_for_windows(root, baseline_days, day)
    meta = etf_meta.load_index(root)
    items = []
    for row in frame.iter_rows(named=True):
        code = row.get("code")
        if not code:
            continue
        fields = _etf_money_fields(code, linked, inflows)
        presented = _etf_presented(code, row.get("name"), meta.get(code))
        items.append({
            **{key: _json_ready(value) for key, value in row.items()},
            "name": presented["name"],
            "short_name": presented["short_name"],
            **fields,
            **_share_change_fields(
                code,
                row.get("shares"),
                baselines,
                day=day,
                baseline_days=baseline_days,
                rebase_factors=rebase.get(code, []),
            ),
            "etf_flow_linked": fields["flow_net"] is not None,
        })
    return {
        "trade_date": day,
        "etf_flow_linked": any(item["etf_flow_linked"] for item in items),
        "items": items,
    }


def _json_ready(value: object) -> object:
    """带时区的入库时刻写成 +08:00 文本。空值保持空，不补成写入接口的当前时间。"""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=CN_TZ)
        return moment.astimezone(CN_TZ).isoformat(timespec="seconds")
    return value


def _etf_flow_map(data_dir: Path, trade_date: str) -> dict[str, dict[str, float | None]]:
    """领航者净流入（亿元）。按交易日和代码读 news.sqlite，对不上就空。"""
    return etf_inflow.navigator_by_code(data_dir, trade_date)


def _etf_money_fields(
    code: str,
    navigator: dict[str, dict[str, float | None]],
    inflows: dict[str, dict[str, float | None]],
) -> dict:
    """份额推算的净流入和领航者金额都是亿元。缺数留空，不写成 0。"""
    article = navigator.get(code) or {}
    computed = inflows.get(code) or {}
    flow_net = article.get("net_1d")
    net_1d = computed.get("net_inflow_1d")
    return {
        "net_inflow_1d": net_1d,
        "net_inflow_5d": computed.get("net_inflow_5d"),
        "net_inflow_20d": computed.get("net_inflow_20d"),
        "flow_net": flow_net,
        "flow_net_5d": article.get("net_5d"),
        "flow_net_20d": article.get("net_20d"),
        "flow_diverged": etf_inflow.flows_diverged(net_1d, flow_net),
    }


def lhb_backup_payload(data_dir: Path, trade_date: str) -> dict | None:
    """收成龙虎榜页面已有的形状。涨跌幅从百分点换成小数，和 fuyao 的 change 一致。"""
    frame = store.read_partition(data_dir, "lhb", trade_date)
    if frame.is_empty():
        return None
    items = []
    for row in frame.to_dicts():
        items.append({
            "thscode": row["code"],
            "ticker": row["code"],
            "name": row["name"],
            "change": percent_points_to_decimal(row.get("change_pct_points")),
            "net_value": row.get("net_buy"),
            "range_days": 1,
        })
    board = {
        "trade_date": trade_date,
        "stock_count": len(items),
        "count": len(items),
        "stock_items": items,
        "hot_money_items": [],
    }
    empty = {**board, "stock_count": 0, "count": 0, "stock_items": []}
    return {
        "state": "ok",
        "source": "akshare_lhb",
        "requested_date": trade_date,
        "trade_date": trade_date,
        "all": board,
        "org": empty,
        "hot_money": empty,
    }


def dsa_context(data_dir: Path | None = None) -> dict:
    """给 DSA 的一段文字。没有落盘数据时 items 为空，不编造数字。"""
    root = _dir(data_dir)
    lines = []
    published = None
    stock_day = store.latest_date(root, "stock")
    if stock_day:
        frame = _partition(root, "stock", stock_day)
        ranked = _stock_sides(root, frame, "main_net")
        if ranked is not None:
            inflow, _outflow = ranked
            top = inflow[:5]
            if top:
                published = stock_day
                lines.append("主力净流入居前: " + "；".join(
                    f"{row.get('name') or row.get('code')} {_yi(row.get('main_net'))}" for row in top
                ))
    for kind, label in (("industry", "行业"), ("concept", "概念")):
        day = store.latest_date(root, kind)
        if not day:
            continue
        ranked = factors.latest_sector_ranks(root, kind, day).sort("rank").head(5)
        rows = ranked.to_dicts()
        if not rows:
            continue
        published = published or day
        lines.append(f"{label}净流入排名: " + "；".join(
            f"{row['name']} 第{row['rank']} {_yi(row['net_inflow'])}" for row in rows
        ))
    south_day = store.latest_date(root, "southbound")
    if south_day:
        south = _partition(root, "southbound", south_day)
        if not south.is_empty() and "net_flow" in south.columns and south["net_flow"][0] is not None:
            published = published or south_day
            lines.append(f"南向净流入 {_yi(south['net_flow'][0])}")
    if not lines:
        return {"items": []}
    return {
        "items": [{
            "title": f"资金进出 {published}",
            "summary": "。".join(lines),
            "published_at": f"{published}T16:30:00+08:00",
        }],
    }


_PARTIAL_NOTE = "个股样本不完整，不作为全市场排名。"
_PARTIAL_5D_NOTE = "5日样本不完整，不作为排名。"
_BOARD_SIDE_LIMIT = 10
_MARGIN_COVERAGE = {
    ("sse", "szse"): ("both", "沪深合计"),
    ("sse",): ("sse", "仅沪市"),
    ("szse",): ("szse", "仅深市"),
}


def _listed_count(root: Path) -> int | None:
    path = Path(root) / "instruments" / "instruments.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if frame.is_empty():
        return None
    return frame.height


def _instrument_names(root: Path) -> dict[str, str]:
    path = Path(root) / "instruments" / "instruments.parquet"
    if not path.exists():
        return {}
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return {}
    if frame.is_empty() or "name" not in frame.columns:
        return {}
    found: dict[str, str] = {}
    symbols = frame["symbol"].to_list() if "symbol" in frame.columns else [""] * frame.height
    codes = frame["code"].to_list() if "code" in frame.columns else [""] * frame.height
    for symbol, code, name in zip(symbols, codes, frame["name"].to_list(), strict=False):
        text = str(name or "").strip()
        if not text or text.lower() in {"none", "nan"}:
            continue
        for key in (normalize.code6(str(code or "")), normalize.code6(str(symbol or "")), str(symbol or "")):
            if key:
                found[key] = text
    return found


def _with_names(rows: list[dict], names: dict[str, str]) -> list[dict]:
    named = []
    for row in rows:
        code = normalize.code6(str(row.get("code") or row.get("symbol") or ""))
        name = str(row.get("name") or "").strip()
        if not name or name.lower() in {"none", "nan"}:
            name = names.get(code) or names.get(str(row.get("symbol") or "")) or ""
        named.append({**row, "code": code or row.get("code"), "name": name or None})
    return named


def _stock_sides(
    root: Path,
    frame: pl.DataFrame,
    column: str,
) -> tuple[list[dict], list[dict]] | None:
    """按总数判定样本够不够，再返回涨/跌两侧。

    这里**只看总数**，不看板块缺口：上游同花顺个股榜本来就缺深主板
    （线上 3723 只，缺 sz_main），板块缺口是数据源的已知局限，不是这一次
    拉取失败。若把板块覆盖也算进来，全市场个股榜会永久变空、DSA 的
    「主力净流入居前」也一起消失。缺口改由 :func:`board_gaps` 作为提示透出。
    """
    if frame.is_empty() or column not in frame.columns or "code" not in frame.columns:
        return None
    usable = frame.filter(pl.col(column).is_not_null()).unique(subset=["code"], keep="last")
    if store.is_partial_ranking(usable.height, _listed_count(root)):
        return None
    keep = [name for name in ("symbol", "code", "name", column, "large_net", "super_net") if name in usable.columns]
    names = _instrument_names(root)
    inflow = _with_names(
        usable.filter(pl.col(column) > 0).sort(column, descending=True).head(_BOARD_SIDE_LIMIT).select(keep).to_dicts(),
        names,
    )
    outflow = _with_names(
        usable.filter(pl.col(column) < 0).sort(column, descending=False).head(_BOARD_SIDE_LIMIT).select(keep).to_dicts(),
        names,
    )
    return inflow, outflow


def board_gaps(root: Path, frame: pl.DataFrame) -> list[str]:
    """这一份分区相对维表整段缺失的板块名。只作展示提示，不参与「够不够」判定。"""
    expected, full = store.board_coverage(root)
    return store.missing_boards(frame, expected=expected, full=full)


_BOARD_GAP_LABEL = {
    "sh_main": "沪市主板",
    "star": "科创板",
    "sz_main": "深市主板",
    "sz_gem": "创业板",
    "bj": "北交所",
}


def board_gap_note(gaps: list[str]) -> str | None:
    """把缺失板块拼成一句人话，给页面上标注用。没有缺口返回 None。"""
    names = [_BOARD_GAP_LABEL.get(gap, gap) for gap in gaps]
    if not names:
        return None
    return f"上游个股资金榜未覆盖：{'、'.join(names)}"


def _empty_stock(day: str | None, *, partial: bool = False, note: str | None = None) -> dict:
    return {
        "trade_date": day,
        "snapshot": None,
        "captured_at": None,
        "source": None,
        "partial": partial,
        "note": note,
        "items": [],
        "inflow": [],
        "outflow": [],
    }


# 沪市只认跟踪指数代码。短简称「科创50E」不是科创50（它跟踪科创成长）。
_SSE_BROAD_INDEX = {
    "000300": "沪深300",
    "000688": "科创50",
    "399006": "创业板",
}
# 深市没有指数代码，按扩位名严格匹配。风格切片不是宽基。
_STYLE_NAME = re.compile(r"成长|价值|增强|红利|ESG", re.IGNORECASE)
# 「创业板ETF」「创业板50ETF」是宽基。主题名（创业板成长ETF）对不上。
_CYB_NAME = re.compile(r"创业板(?:50)?ETF|创业板指")
# 上交所份额接口的短简称，例如 588020「科创50E」。后面多一个字母就不是科创50。
_KC50_ABBR = re.compile(r"^科创50[A-Za-z]$")
# 份额接口给沪市代码加的当日状态前缀，不是名称的一部分。
_SSE_STATUS_PREFIX = re.compile(r"^(?:XD|XR|DR|UC|N)")
_BOARD_SECTOR_LIMIT = 12
_BOARD_HISTORY = 40
_BOARD_ETF_OTHERS = 12


def _broad_label(
    code: str | None,
    name: str | None,
    index_code: str | None = None,
) -> str | None:
    """宽基名单。沪深300 / 科创50 在页面上标国家队代理，创业板只标指数名。

    沪市代码一旦有跟踪指数，只看指数代码，不再用名称子串。深市和还没有
    指数代码的沪市按名称匹配，并排除成长、价值、增强、红利、ESG。
    """
    code6 = normalize.code6(code or "") or ""
    index = (index_code or "").strip()
    if code6.startswith("5") and index:
        return _SSE_BROAD_INDEX.get(index)
    text = (name or "").strip()
    if not text or _STYLE_NAME.search(text):
        return None
    if "沪深300" in text:
        return "沪深300"
    if "科创50" in text and _KC50_ABBR.fullmatch(text) is None:
        return "科创50"
    if _CYB_NAME.search(text):
        return "创业板"
    return None


def _etf_presented(code: str, stored_name: object, meta: dict | None) -> dict:
    """查询输出的名称。不改份额分区里的原文。"""
    stored = str(stored_name).strip() if stored_name else ""
    info = meta or {}
    full = str(info.get("full_name") or "").strip()
    short = str(info.get("short_name") or "").strip()
    if full:
        name = full
    elif stored and code.startswith("5"):
        stripped = _SSE_STATUS_PREFIX.sub("", stored, count=1)
        name = stripped or stored
    else:
        name = stored
    return {
        "name": name or None,
        "short_name": short or None,
        "index_code": str(info.get("index_code") or "").strip(),
    }


def board(data_dir: Path | None = None) -> dict:
    """资金页一次读完。只读已落盘分区，没有的块留空，不把缺失写成 0。"""
    root = _dir(data_dir)
    store.repair_szse_margin_summaries(root)
    return {
        "industry": _sector_board(root, "industry"),
        "concept": _sector_board(root, "concept"),
        "stocks_today": _stocks_today(root),
        "stocks_5d": _stocks_5d(root),
        "margin": _margin_trend(root),
        "southbound": _southbound_trend(root),
        "etf_shares": _etf_changes(root),
    }


def _sector_board(root: Path, kind: str) -> dict:
    """流入、流出各取限额。``items`` 仍等于流入，旧前端只读这一列。"""
    payload = sectors(kind, data_dir=root)
    rows = payload["items"]
    inflow = sorted(
        (row for row in rows if (_amount(row.get("net_inflow")) or 0) > 0),
        key=lambda row: row["net_inflow"],
        reverse=True,
    )[:_BOARD_SECTOR_LIMIT]
    outflow = sorted(
        (row for row in rows if (_amount(row.get("net_inflow")) or 0) < 0),
        key=lambda row: row["net_inflow"],
    )[:_BOARD_SECTOR_LIMIT]
    payload["inflow"] = inflow
    payload["outflow"] = outflow
    payload["items"] = inflow
    return payload


def _intraday_still_fresh(captured: str | None, now: datetime | None = None) -> bool:
    """当天盘中快照是否仍能代表「今日」。

    判据看**快照自身的取样时刻**（``captured_at``），不看墙上时钟 —— 这是评审
    指出的关键差异。只看时钟会有个回归：15:00 一过就不认当天盘中，而收盘分区
    要到 16:30 左右才就绪，这段时间会退回**上一交易日**，比原来显示的 14:5x
    快照还旧。

    真正要拦的是「快照定格在午盘」：那种快照拿来说「今日」会误导。所以：

    * 交易日盘中（``now`` 早于收盘）→ 当天任意快照都新鲜；
    * 收盘后 → 只认 ``captured_at`` 不早于 ``_STALE_INTRADAY_BEFORE``（14:45）
      的当天快照，视作「收盘前快照」；更早的（如 13:44）判为陈旧，交给收盘分区。
    """
    if not captured:
        return False
    try:
        stamp = datetime.fromisoformat(str(captured))
    except ValueError:
        return False
    # ``astimezone()`` 对 naive 值是按**系统本地时区**解释的 —— 落在 CI（UTC）上会
    # 平白挪 8 小时。所以先补时区再换算：naive 的一律当东八区本地时间。
    current = now or cn_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    current = current.astimezone(CN_TZ).replace(tzinfo=None)
    # 快照时间戳可能是 aware 也可能是 naive。naive 的按东八区解释（落盘的就是
    # 中国本地时间），aware 的先转东八区再丢时区 —— 直接 ``replace(tzinfo=None)``
    # 只是丢掉时区标记，非东八区的 aware 时间会比出来差好几个小时。
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CN_TZ)
    stamp = stamp.astimezone(CN_TZ).replace(tzinfo=None)
    # 快照日期不是今天 → 不新鲜（非交易日、跨日残留都走这条）。
    if stamp.date() != current.date():
        return False
    if current.time() < _INTRADAY_FRESH_UNTIL:
        return True
    return stamp.time() >= _STALE_INTRADAY_BEFORE


def _stocks_today(root: Path, *, now: datetime | None = None) -> dict:
    """今日优先用当天最近一份盘中快照。样本不够或没有快照时，仍读 ``stock`` 收盘。

    收盘后（见 ``_intraday_still_fresh``）只采用「收盘前快照」（14:45 及以后）；
    更早的快照判为陈旧，改读当天收盘分区；收盘分区尚未落盘时退回上一交易日，
    而不是把定格在午盘的旧快照冒充「今日实时」。
    """
    day = cn_today().isoformat()
    intra = _partition(root, "stock_intraday", day)
    captured = None
    if not intra.is_empty() and "captured_at" in intra.columns:
        captured = intra.select(pl.col("captured_at").max()).item()
        if captured:
            intra = intra.filter(pl.col("captured_at") == captured)
        else:
            intra = store.empty_frame("stock_intraday")
    else:
        intra = store.empty_frame("stock_intraday")
    ranked = None
    if _intraday_still_fresh(captured, now):
        ranked = _stock_sides(root, intra, "main_net")
    if ranked is not None:
        inflow, outflow = ranked
        current = now or cn_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=CN_TZ)
        current = current.astimezone(CN_TZ).replace(tzinfo=None)
        return {
            "trade_date": day,
            "snapshot": "intraday",
            "captured_at": captured,
            # 收盘后采用的盘中快照是「收盘前」的定格，标出来免得被当成实时。
            "stale_after_close": current.time() >= _INTRADAY_FRESH_UNTIL,
            "source": _snapshot_source(intra),
            "partial": False,
            "note": board_gap_note(board_gaps(root, intra)),
            "items": inflow,
            "inflow": inflow,
            "outflow": outflow,
        }
    day = store.latest_date(root, "stock")
    if day is None:
        return _empty_stock(None)
    frame = _partition(root, "stock", day)
    ranked = _stock_sides(root, frame, "main_net")
    if ranked is None:
        partial = store.code_count(frame) > 0
        return _empty_stock(day, partial=partial, note=_PARTIAL_NOTE if partial else None)
    inflow, outflow = ranked
    return {
        "trade_date": day,
        "snapshot": "close",
        "captured_at": None,
        "source": None,
        "partial": False,
        "note": board_gap_note(board_gaps(root, frame)),
        "items": inflow,
        "inflow": inflow,
        "outflow": outflow,
    }


def _five_day_frame(root: Path, day: str) -> pl.DataFrame:
    """优先用已落盘的 5 日合计，没有时用排名接口写入的 ``main_net_5d``。"""
    part = _partition(root, "stock", day)
    if part.is_empty() or "code" not in part.columns:
        return part
    factor = factors.main_net_frame(root)
    if not factor.is_empty():
        latest = factor.filter(pl.col("trade_date") == day).select("code", "ff_main_net_5d")
        if not latest.is_empty():
            part = part.join(latest, on="code", how="left")
    if "ff_main_net_5d" not in part.columns:
        part = part.with_columns(pl.lit(None).cast(pl.Float64).alias("ff_main_net_5d"))
    if "main_net_5d" not in part.columns:
        part = part.with_columns(pl.lit(None).cast(pl.Float64).alias("main_net_5d"))
    return part.with_columns(
        pl.coalesce(pl.col("ff_main_net_5d"), pl.col("main_net_5d")).alias("ff_main_net_5d"),
    )


def _stocks_5d(root: Path) -> dict:
    """全市场 5 日主力。样本不够时不把少数几只排成榜。"""
    day = store.latest_date(root, "stock")
    if day is None:
        return _empty_stock(None)
    frame = _five_day_frame(root, day)
    ranked = _stock_sides(root, frame, "ff_main_net_5d")
    if ranked is None:
        usable = 0
        if not frame.is_empty() and "ff_main_net_5d" in frame.columns:
            usable = frame.filter(pl.col("ff_main_net_5d").is_not_null()).height
        partial = usable > 0
        return _empty_stock(day, partial=partial, note=_PARTIAL_5D_NOTE if partial else None)
    inflow, outflow = ranked
    return {
        "trade_date": day,
        "partial": False,
        "note": board_gap_note(board_gaps(root, frame)),
        "items": inflow,
        "inflow": inflow,
        "outflow": outflow,
    }


def _margin_day(day: str, rows: list[dict]) -> dict:
    by_market = {}
    for row in rows:
        market = str(row.get("market") or "")
        if market in {"sse", "szse"} and row.get("margin_balance") is not None:
            by_market[market] = row
    present = tuple(market for market in ("sse", "szse") if market in by_market)
    coverage, label = _MARGIN_COVERAGE.get(present, ("none", ""))
    total = None
    if present:
        total = sum(by_market[market]["margin_balance"] for market in present)
    shorts = [
        by_market[market]["short_balance"]
        for market in present
        if by_market[market].get("short_balance") is not None
    ]
    return {
        "trade_date": day,
        "margin_balance": total,
        "short_balance": sum(shorts) if shorts else None,
        "coverage": coverage,
        "label": label,
        "sse_margin_balance": (by_market.get("sse") or {}).get("margin_balance"),
        "szse_margin_balance": (by_market.get("szse") or {}).get("margin_balance"),
    }


def _margin_trend(root: Path) -> dict:
    dates = store.list_dates(root, "margin")[-_BOARD_HISTORY:]
    items = []
    days = []
    for day in dates:
        frame = _partition(root, "margin", day)
        if frame.is_empty() or "row_kind" not in frame.columns:
            continue
        summary = frame.filter(pl.col("row_kind") == "summary")
        rows = summary.select("trade_date", "market", "margin_balance", "short_balance").to_dicts()
        if not rows:
            continue
        items.extend(rows)
        days.append(_margin_day(day, rows))
    return {"items": items, "days": days}


def _southbound_trend(root: Path) -> dict:
    try:
        frame = store.read_range(root, "southbound")
    except (KeyError, pl.exceptions.ColumnNotFoundError):
        return {"items": []}
    if frame.is_empty() or "trade_date" not in frame.columns or "net_flow" not in frame.columns:
        return {"items": []}
    ranked = frame.sort("trade_date").tail(_BOARD_HISTORY)
    return {"items": ranked.select("trade_date", "net_flow").to_dicts()}


# ETF 份额变化的窗口，单位是**落盘份数**而非交易日数。1 指「比上一份落盘」,
# 5/20 指「比往前第 N 份落盘」。基准按落盘日期往前推，不按自然日 —— 跨周末和
# 长假时自然日会取到更早的日期，与「5 日变化」的直觉不符。
#
# 用「份数」而不是「交易日数」是刻意的：``etf_shares`` 中间可能缺天（线上就出过
# 10-13 没落盘），这时「前 1 个交易日」实际会变成 2 个交易日之前。而落盘记录本身
# 是唯一可靠的锚点 —— 说「较前 N 份落盘」永远准确，前端 tooltip 也照这个措辞。
# 看板和 ``/api/fund-flow/etf-shares`` 都用这组窗口，字段名是 change_{N}d。
# 窗口跨过已确认的登记日时，基准份额乘上其后的折算倍数，换算成当前份额口径。
# 确认规则与净流入相同：只有 ``_shares_rebased`` 认定真跳变才乘 f。
_ETF_WINDOWS = (1, 5, 20)


def _window_baseline_dates(dates: list[str], day: str) -> dict[int, str | None]:
    """每个窗口对应的基准落盘日。不足 N 份落盘，或这一天没有分区时为 None。"""
    try:
        index = dates.index(day)
    except ValueError:
        return {window: None for window in _ETF_WINDOWS}
    return {
        window: (dates[index - window] if index - window >= 0 else None)
        for window in _ETF_WINDOWS
    }


def _window_baselines(
    root: Path,
    day: str,
    dates: list[str] | None = None,
) -> tuple[dict[int, str | None], dict[int, dict[str, float]]]:
    """基准落盘日，以及每个窗口日的 code → 份额。看板和 etf-shares 共用。"""
    if dates is None:
        dates = store.list_dates(root, "etf_shares")
    baseline_days = _window_baseline_dates(dates, day)
    baselines = {
        window: _shares_by_code(root, base_day)
        for window, base_day in baseline_days.items()
    }
    return baseline_days, baselines


def _rebase_factors_for_windows(
    root: Path,
    baseline_days: dict[int, str | None],
    day: str,
) -> dict[str, list[tuple[str, float]]]:
    """各窗口可能跨过的、已确认登记日因子。没有基准日时为空。"""
    bases = [base for base in baseline_days.values() if base]
    if not bases:
        return {}
    return etf_inflow.confirmed_rebase_factors(
        root,
        after=min(bases),
        on_or_before=day,
    )


def _share_scale(
    rebase_factors: list[tuple[str, float]],
    baseline_day: str,
    day: str,
) -> float:
    """基准日之后、截至当日的已确认因子连乘。没有折算时为 1。"""
    scale = 1.0
    for record_day, factor in rebase_factors:
        if baseline_day < record_day <= day and factor > 0:
            scale *= factor
    return scale


def _share_change_fields(
    code: str,
    shares: float | None,
    baselines: dict[int, dict[str, float]],
    *,
    day: str,
    baseline_days: dict[int, str | None],
    rebase_factors: list[tuple[str, float]],
) -> dict[str, float | None]:
    """份额差，单位份，口径是当前份额。

    登记日的份额已经按 f 换成新份额，基准日没有。窗口跨过已确认的登记日时，
    用 ``shares_now − baseline × Πf``，避免把拆分或缩股算成申赎。
    窗口不足或当天份额缺失时留空，不写成 0。
    """
    fields: dict[str, float | None] = {}
    for window in _ETF_WINDOWS:
        base = baselines[window].get(code)
        base_day = baseline_days.get(window)
        if shares is None or base is None or base_day is None:
            fields[f"change_{window}d"] = None
            continue
        scale = _share_scale(rebase_factors, base_day, day)
        fields[f"change_{window}d"] = shares - base * scale
    return fields


def _shares_by_code(root: Path, day: str | None) -> dict[str, float]:
    if day is None:
        return {}
    frame = _partition(root, "etf_shares", day)
    if frame.is_empty() or "code" not in frame.columns or "shares" not in frame.columns:
        return {}
    out: dict[str, float] = {}
    for code, shares in frame.select("code", "shares").iter_rows():
        if code and shares is not None:
            out[code] = shares
    return out


def _etf_changes(root: Path) -> dict:
    dates = store.list_dates(root, "etf_shares")
    if not dates:
        return {
            "trade_date": None,
            "prev_trade_date": None,
            "windows": list(_ETF_WINDOWS),
            "baselines": {},
            "items": [],
        }
    day = dates[-1]
    baseline_days, baselines = _window_baselines(root, day, dates)
    rebase = _rebase_factors_for_windows(root, baseline_days, day)
    prev_day = baseline_days.get(1)
    linked = _etf_flow_map(root, day)
    inflows = etf_inflow.latest_inflows(root, as_of=day)
    meta = etf_meta.load_index(root)
    current = _partition(root, "etf_shares", day)
    broad = []
    others = []
    # 看板不展示入库时刻。只取份额计算要用的列，避免把时间戳收成 Python 对象。
    shown = [name for name in ("code", "name", "shares") if name in current.columns]
    for row in current.select(shown).iter_rows(named=True):
        code = row.get("code")
        if not code:
            continue
        presented = _etf_presented(code, row.get("name"), meta.get(code))
        label = _broad_label(code, presented["name"], presented["index_code"])
        shares = row.get("shares")
        changes = _share_change_fields(
            code,
            shares,
            baselines,
            day=day,
            baseline_days=baseline_days,
            rebase_factors=rebase.get(code, []),
        )
        # 1 日变化与 share_change 同源，独立成字段便于前端统一按窗口渲染。
        change = changes["change_1d"]
        item = {
            "code": code,
            "name": presented["name"],
            "short_name": presented["short_name"],
            "shares": shares,
            "prev_shares": baselines[1].get(code),
            "share_change": change,
            **changes,
            "broad": label,
            **_etf_money_fields(code, linked, inflows),
        }
        if label:
            broad.append(item)
        elif change is not None:
            others.append(item)
    # 落盘只有份额、没有市值/净值；同系列 ETF 份额与规模同序，整表按份额降序作规模代理。
    broad.sort(key=lambda item: item.get("shares") or 0, reverse=True)
    others.sort(key=lambda item: item.get("shares") or 0, reverse=True)
    return {
        "trade_date": day,
        "prev_trade_date": prev_day,
        "windows": list(_ETF_WINDOWS),
        "baselines": {
            str(window): baseline_days[window] for window in _ETF_WINDOWS
        },
        "items": broad + others[:_BOARD_ETF_OTHERS],
    }


def _yi(value: float | None) -> str:
    if value is None:
        return "无"
    return f"{value / 1e8:.2f}亿元"
