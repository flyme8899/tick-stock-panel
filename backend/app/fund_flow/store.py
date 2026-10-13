"""按日落盘的 parquet。

``data/fund_flow/<kind>/date=YYYY-MM-DD/part.parquet``。写入走临时文件替换。
同一分区再次写入时按主键合并，后写入的行覆盖同一主键。
"""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import datetime
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet, atomic_write_text

KINDS = frozenset({
    "stock",
    "stock_intraday",
    "industry",
    "concept",
    "margin",
    "etf_shares",
    "southbound",
    "northbound_turnover",
    "lhb",
})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 全市场 A 股大约五千只。少于此数的分区是半次失败留下的样本，不能当成排名。
FULL_MARKET_MIN = 1000

# 合并主键。后写覆盖先写。
KEYS: dict[str, list[str]] = {
    "stock": ["code", "trade_date"],
    "stock_intraday": ["code", "captured_at"],
    "industry": ["name", "snapshot", "captured_at"],
    "concept": ["name", "snapshot", "captured_at"],
    "margin": ["market", "symbol", "row_kind", "trade_date"],
    "etf_shares": ["code", "trade_date"],
    "southbound": ["trade_date"],
    "northbound_turnover": ["trade_date"],
    "lhb": ["code", "trade_date"],
}

_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "stock": {
        "symbol": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "main_net": pl.Float64,
        "large_net": pl.Float64,
        "super_net": pl.Float64,
        "main_net_5d": pl.Float64,
        "source": pl.Utf8,
    },
    "stock_intraday": {
        "symbol": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "captured_at": pl.Utf8,
        "main_net": pl.Float64,
        # 东财当日排名才有这四列，单位元，符号与主力相同。同花顺即时净额留空。
        "super_net": pl.Float64,
        "large_net": pl.Float64,
        "medium_net": pl.Float64,
        "small_net": pl.Float64,
        "source": pl.Utf8,
    },
    "industry": {
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "captured_at": pl.Utf8,
        "snapshot": pl.Utf8,
        "net_inflow": pl.Float64,
        "rank": pl.Int64,
        "source": pl.Utf8,
    },
    "concept": {
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "captured_at": pl.Utf8,
        "snapshot": pl.Utf8,
        "net_inflow": pl.Float64,
        "rank": pl.Int64,
        "source": pl.Utf8,
    },
    "margin": {
        "market": pl.Utf8,
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "row_kind": pl.Utf8,
        "margin_balance": pl.Float64,
        "short_balance": pl.Float64,
        "margin_buy": pl.Float64,
        "source": pl.Utf8,
    },
    "etf_shares": {
        "symbol": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "shares": pl.Float64,
        "source": pl.Utf8,
    },
    "southbound": {
        "trade_date": pl.Utf8,
        "net_flow": pl.Float64,
        "source": pl.Utf8,
    },
    "northbound_turnover": {
        "trade_date": pl.Utf8,
        "turnover": pl.Float64,
        "source": pl.Utf8,
    },
    "lhb": {
        "symbol": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "net_buy": pl.Float64,
        "change_pct_points": pl.Float64,
        "source": pl.Utf8,
    },
}


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "fund_flow"


def _check(kind: str, trade_date: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"未知资金进出分区: {kind}")
    if not _DATE_RE.match(trade_date):
        raise ValueError(f"交易日格式应为 YYYY-MM-DD: {trade_date}")


def partition_file(data_dir: Path, kind: str, trade_date: str) -> Path:
    _check(kind, trade_date)
    return root(data_dir) / kind / f"date={trade_date}" / "part.parquet"


def empty_frame(kind: str) -> pl.DataFrame:
    schema = _SCHEMAS[kind]
    return pl.DataFrame(schema=schema)


def frame_of(kind: str, rows: list[dict]) -> pl.DataFrame:
    schema = _SCHEMAS[kind]
    if not rows:
        return empty_frame(kind)
    df = pl.DataFrame(rows)
    exprs = []
    for name, dtype in schema.items():
        if name in df.columns:
            exprs.append(pl.col(name).cast(dtype, strict=False))
        else:
            exprs.append(pl.lit(None).cast(dtype).alias(name))
    return df.select(exprs)


def _looks_like_raw_etf(rows: list[dict]) -> bool:
    return any(
        "基金份额" in row or "基金代码" in row or "基金规模(份)" in row
        for row in rows
    )


def prepare_rows(kind: str, rows: list[dict]) -> list[dict]:
    """ETF 原始行先收成内部字段。其它种类不在这里猜列名。"""
    if kind == "etf_shares" and _looks_like_raw_etf(rows):
        from app.fund_flow.normalize import normalize_etf_shares

        return normalize_etf_shares(rows)
    return rows


def assert_schema(kind: str, rows: list[dict]) -> None:
    """写入前核对列名和主键。原始中文列不能进 parquet。"""
    schema = _SCHEMAS[kind]
    keys = KEYS[kind]
    for row in rows:
        unknown = sorted(name for name in row if name not in schema)
        if unknown:
            raise ValueError(f"{kind} 写入含未知列: {', '.join(unknown)}")
        missing = [name for name in keys if name not in row or row[name] is None]
        if missing:
            raise ValueError(f"{kind} 写入缺少主键: {', '.join(missing)}")


def align_frame(kind: str, frame: pl.DataFrame) -> pl.DataFrame:
    """把分区收成内部列。缺主键的分区能按 ETF 原始列还原，否则整段跳过。"""
    schema = _SCHEMAS[kind]
    if frame.is_empty():
        return empty_frame(kind)
    names = set(frame.columns)
    if set(schema).issubset(names):
        return frame.select(list(schema.keys()))
    rows = frame.to_dicts()
    if kind == "etf_shares" and _looks_like_raw_etf(rows):
        prepared = prepare_rows(kind, rows)
        if prepared:
            return frame_of(kind, prepared)
    if all(name in names for name in KEYS[kind]):
        return frame_of(kind, rows)
    return empty_frame(kind)


def write_rows(data_dir: Path, kind: str, trade_date: str, rows: list[dict]) -> int:
    """合并写入一个交易日分区。返回该分区合并后的行数。"""
    _check(kind, trade_date)
    rows = prepare_rows(kind, rows)
    if not rows:
        path = partition_file(data_dir, kind, trade_date)
        if path.exists():
            return align_frame(kind, pl.read_parquet(path)).height
        return 0
    assert_schema(kind, rows)
    incoming = frame_of(kind, rows)
    path = partition_file(data_dir, kind, trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = align_frame(kind, pl.read_parquet(path))
        merged = pl.concat([previous, incoming], how="vertical_relaxed")
    else:
        merged = incoming
    keys = [key for key in KEYS[kind] if key in merged.columns]
    merged = merged.unique(subset=keys, keep="last")
    atomic_write_parquet(merged, path)
    return merged.height


def read_partition(data_dir: Path, kind: str, trade_date: str) -> pl.DataFrame:
    path = partition_file(data_dir, kind, trade_date)
    if not path.exists():
        return empty_frame(kind)
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return empty_frame(kind)
    return align_frame(kind, frame)


def list_dates(data_dir: Path, kind: str) -> list[str]:
    if kind not in KINDS:
        raise ValueError(f"未知资金进出分区: {kind}")
    base = root(data_dir) / kind
    if not base.exists():
        return []
    found = []
    for path in base.glob("date=*"):
        if path.is_dir() and (path / "part.parquet").exists():
            found.append(path.name.removeprefix("date="))
    return sorted(found)


def read_range(data_dir: Path, kind: str, *, start: str | None = None, end: str | None = None) -> pl.DataFrame:
    dates = [
        item for item in list_dates(data_dir, kind)
        if (start is None or item >= start) and (end is None or item <= end)
    ]
    if not dates:
        return empty_frame(kind)
    frames = [read_partition(data_dir, kind, item) for item in dates]
    return pl.concat(frames, how="vertical_relaxed")


# 各板块的代码前缀。用来分桶校验覆盖：同花顺的个股资金榜只给创业板/科创板/
# 沪主板，深主板（000/001/002/003）整段缺失，总数 3723 仍高于 FULL_MARKET_MIN，
# 只看总数会把「缺了 1612 只深主板」误判成「已齐」，从此永不重拉。
_BOARD_PREFIXES: dict[str, tuple[str, ...]] = {
    "sh_main": ("600", "601", "603", "605"),
    "star": ("688", "689"),
    "sz_main": ("000", "001", "002", "003"),
    "sz_gem": ("300", "301", "302"),
    # 北交所：430/830 等老代码用 43x/83x，「8」兜住 82x/83x/87x/88x/889；
    # 920 是新段（线上 348 只），不能漏。
    "bj": ("430", "8", "920"),
}

# 单桶「有拉到」的判定比例。分区里某个板块的只数低于 该板块在维表里的应有只数 ×
# 这个比例，视为该板块整段缺失。用比例而不是固定只数：1 只的小库不该因为
# 「深主板不足 20 只」被判不完整。
_BOARD_MIN_RATIO = 0.2

# 没有维表时的固定门槛：一个板块至少要有这么多只才算「拉到过」。
_BOARD_MIN = 20


def _board_counts(codes) -> dict[str, int]:
    counts: dict[str, int] = {}
    for code in codes:
        board = board_of(str(code or ""))
        if board == "unknown":
            continue
        counts[board] = counts.get(board, 0) + 1
    return counts


def board_of(code: str | None) -> str:
    """代码落到哪个板块桶。认不出返回 ``unknown``，不参与覆盖判定。"""
    text = str(code or "").strip()
    if len(text) < 3:
        return "unknown"
    head = text[:3]
    for board, prefixes in _BOARD_PREFIXES.items():
        if any(head.startswith(prefix) for prefix in prefixes):
            return board
    return "unknown"


def _instrument_frame(data_dir: Path) -> pl.DataFrame | None:
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    return frame if not frame.is_empty() else None


def listed_boards(data_dir: Path) -> set[str] | None:
    """本地 instruments 里实际存在的板块集合。没有维表时返回 None（退化成固定全集）。

    有维表就用它当期望全集：小库里只有沪市时，就不会因为「深主板为空」反复判不完整。
    """
    frame = _instrument_frame(data_dir)
    if frame is None:
        return None
    found = set(_board_counts(frame.get_column("code").drop_nulls().to_list()))
    return found or None


def missing_boards(frame: pl.DataFrame, *, expected: set[str] | None = None,
                   full: dict[str, int] | None = None) -> list[str]:
    """分区里整段缺失的板块。

    ``expected``/``full`` 由本地维表推导时按「该板块应有只数的比例」判定，小库不误判；
    都没有时按固定全集 + 固定门槛检查。空表返回空列表，交给总数判定。
    """
    if frame.is_empty() or "code" not in frame.columns:
        return []
    counts = _board_counts(frame.get_column("code").drop_nulls().to_list())
    scope = expected if expected is not None else set(_BOARD_PREFIXES)
    gaps = []
    for board in scope:
        have = counts.get(board, 0)
        if full is not None:
            need = full.get(board, 0)
            if have >= max(1, int(need * _BOARD_MIN_RATIO)):
                continue
        elif have >= _BOARD_MIN:
            continue
        gaps.append(board)
    return sorted(gaps)


def board_coverage(data_dir: Path) -> tuple[set[str] | None, dict[str, int] | None]:
    """从维表推出 (期望板块集合, 各板块应有只数)。无维表时 (None, None)。"""
    frame = _instrument_frame(data_dir)
    if frame is None:
        return None, None
    counts = _board_counts(frame.get_column("code").drop_nulls().to_list())
    if not counts:
        return None, None
    return set(counts), counts


def ranking_limit(universe: int | None) -> int:
    """全市场门槛。已知股票池更小时，池子本身就是全集。"""
    if universe is None or universe <= 0:
        return FULL_MARKET_MIN
    return min(FULL_MARKET_MIN, universe)


def is_partial_ranking(
    count: int,
    universe: int | None,
    frame: pl.DataFrame | None = None,
    *,
    expected: set[str] | None = None,
    full: dict[str, int] | None = None,
) -> bool:
    """分区够不够当「全市场排名」。

    总数不足门槛 → 不完整。总数够了但某个板块整段缺失（深主板被上游漏掉时
    就是这种）也要判不完整，否则会永远跳过重拉。frame 省略时退化成只看总数。
    ``expected``/``full`` 传入时按维表限定板块范围与门槛（小库不误判）。
    """
    if count < ranking_limit(universe):
        return True
    if frame is None:
        return False
    return bool(missing_boards(frame, expected=expected, full=full))


def code_count(frame: pl.DataFrame) -> int:
    """分区里有多少只不同的股票。空表或缺代码列算 0。"""
    if frame.is_empty() or "code" not in frame.columns:
        return 0
    return int(frame.get_column("code").drop_nulls().n_unique())


def latest_date(data_dir: Path, kind: str) -> str | None:
    dates = list_dates(data_dir, kind)
    return dates[-1] if dates else None


# 深市汇总在源里是亿元（约 1e4），库内口径是元（约 1e12）。
# 绝对值小于 1e7 且非 0 的深市汇总仍是亿元。已换成元的余额远大于这条线，再跑不会乘第二次。
_SZSE_SUMMARY_YI_MAX = 1e7
_SZSE_SUMMARY_MONEY = ("margin_balance", "short_balance", "margin_buy")


def repair_szse_margin_summaries(data_dir: Path) -> int:
    """把仍以亿元落盘的深市两融汇总乘成元。只改 szse 的 summary，明细和沪市不动。

    返回这次乘过的字段个数。没有需要改的分区时不写文件。
    """
    scaled = 0
    for day in list_dates(data_dir, "margin"):
        frame = read_partition(data_dir, "margin", day)
        if frame.is_empty() or "row_kind" not in frame.columns:
            continue
        rows = frame.to_dicts()
        touched = False
        for row in rows:
            if str(row.get("market") or "") != "szse" or str(row.get("row_kind") or "") != "summary":
                continue
            for field in _SZSE_SUMMARY_MONEY:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if not math.isfinite(value) or value == 0 or abs(value) >= _SZSE_SUMMARY_YI_MAX:
                    continue
                row[field] = float(value) * 1e8
                touched = True
                scaled += 1
        if touched:
            write_rows(data_dir, "margin", day, rows)
    return scaled


def state_path(data_dir: Path) -> Path:
    return root(data_dir) / "_state" / "status.json"


def load_state(data_dir: Path) -> dict:
    path = state_path(data_dir)
    if not path.exists():
        return {"kinds": {}, "sources": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"kinds": {}, "sources": {}}
    if not isinstance(payload, dict):
        return {"kinds": {}, "sources": {}}
    payload.setdefault("kinds", {})
    payload.setdefault("sources", {})
    return payload


def save_state(data_dir: Path, payload: dict) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


# 盘中拉取在旁路线程里写状态，调度线程同一轮也会写。读改写必须包在同一把锁里。
_state_lock = threading.Lock()


def mutate_state(data_dir: Path, mutator) -> dict:
    """在锁里读出状态、改完再写回。``mutator`` 就地修改 payload。"""
    with _state_lock:
        payload = load_state(data_dir)
        mutator(payload)
        save_state(data_dir, payload)
        return payload


def update_kind_state(data_dir: Path, kind: str, **fields: object) -> dict:
    def mutate(payload: dict) -> None:
        current = dict(payload["kinds"].get(kind) or {})
        current.update(fields)
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload["kinds"][kind] = current

    saved = mutate_state(data_dir, mutate)
    return dict(saved["kinds"][kind])
