"""按日落盘的 parquet。

``data/fund_flow/<kind>/date=YYYY-MM-DD/part.parquet``。写入走临时文件替换。
同一分区再次写入时按主键合并，后写入的行覆盖同一主键。

``etf_shares`` 另记首次入库时刻 ``ingested_at``（Asia/Shanghai）。重复写入不覆盖。
份额、名称、代码或来源变了才写 ``updated_at``。历史分区没有这两列时读出来是空，不补时刻。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
from datetime import datetime
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now
from app.services.fs_utils import atomic_write_parquet, atomic_write_text, partition_writer

logger = logging.getLogger(__name__)

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

# 个股日频里这些来源和东财数据中心同一口径。同花顺兜底不能盖掉它们。
# akshare_ths 的净额经常符号相反、量级差几个数量级。
EASTMONEY_STOCK_SOURCES = frozenset({
    "efinance",
    "eastmoney_sz_main",
    "eastmoney_bj",
    "eastmoney_datacenter",
    "akshare_em_rank",
})
THS_STOCK_SOURCE = "akshare_ths"

# 带时区的微秒时间戳。比 ISO 文本短，时区写在类型上，不跟服务器本地时区走。
_STAMP_DTYPE = pl.Datetime(time_unit="us", time_zone="Asia/Shanghai")
_STAMP_COLS = ("ingested_at", "updated_at")
# 这些列变了才刷新 updated_at。主键和入库时刻本身不算业务值。
_ETF_VALUE_COLS = ("symbol", "name", "shares", "source")

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
        "ingested_at": _STAMP_DTYPE,
        "updated_at": _STAMP_DTYPE,
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


# 个股资金流的 symbol 统一写成带交易所后缀（600000.SH）。历史分区裸代码和带后缀混用，
# 读的时候也按同一规则收口，研究侧不用再自己猜。code 列始终是 6 位裸代码。
SUFFIX_SYMBOL_KINDS = frozenset({"stock", "stock_intraday"})


def suffixed_symbol(raw: object) -> str:
    """6 位 A 股代码补交易所后缀。认不出的原样返回，绝不编造。"""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        head, _, tail = text.partition(".")
        if tail in {"SH", "SZ", "BJ"} and len(head) == 6 and head.isdigit():
            return f"{head}.{tail}"
        return text
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix) and len(text) == 8 and text[2:].isdigit():
            return f"{text[2:]}.{prefix}"
    if len(text) != 6 or not text.isdigit():
        return text
    if text.startswith("92") or text[0] in "48":
        return f"{text}.BJ"
    if text[0] in "569":
        return f"{text}.SH"
    if text[0] in "023" or text.startswith("1"):
        return f"{text}.SZ"
    return text


def _suffix_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        item = dict(row)
        base = item.get("symbol") or item.get("code")
        if base:
            item["symbol"] = suffixed_symbol(base)
        out.append(item)
    return out


def _suffix_frame(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty() or "symbol" not in frame.columns:
        return frame
    source = pl.when(pl.col("symbol").fill_null("") == "").then(pl.col("code")).otherwise(pl.col("symbol")) \
        if "code" in frame.columns else pl.col("symbol")
    return frame.with_columns(
        source.map_elements(suffixed_symbol, return_dtype=pl.Utf8).alias("symbol")
    )


def prepare_rows(kind: str, rows: list[dict]) -> list[dict]:
    """ETF 原始行先收成内部字段；个股资金流 symbol 补后缀。其它种类不在这里猜列名。"""
    if kind == "etf_shares" and _looks_like_raw_etf(rows):
        from app.fund_flow.normalize import normalize_etf_shares

        return normalize_etf_shares(rows)
    if kind in SUFFIX_SYMBOL_KINDS:
        return _suffix_rows(rows)
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
    """把分区收成内部列；个股资金流顺带把 symbol 收成带后缀。"""
    aligned = _align_frame(kind, frame)
    if kind in SUFFIX_SYMBOL_KINDS:
        return _suffix_frame(aligned)
    return aligned


def _project_schema(kind: str, frame: pl.DataFrame) -> pl.DataFrame:
    """按内部列投影。缺列补空，不把整表收成 Python 行。"""
    schema = _SCHEMAS[kind]
    exprs = []
    for name, dtype in schema.items():
        if name in frame.columns:
            exprs.append(pl.col(name).cast(dtype, strict=False))
        else:
            exprs.append(pl.lit(None).cast(dtype).alias(name))
    return frame.select(exprs)


def _align_frame(kind: str, frame: pl.DataFrame) -> pl.DataFrame:
    """把分区收成内部列。缺主键的分区能按 ETF 原始列还原，否则整段跳过。"""
    schema = _SCHEMAS[kind]
    if frame.is_empty():
        return empty_frame(kind)
    names = set(frame.columns)
    if set(schema).issubset(names):
        return frame.select(list(schema.keys()))
    # 历史 etf_shares 没有入库时刻。主键已在时用表达式补空，避免每次读取
    # （净流入要连读多日）把分区收成 Python 字典。
    if all(name in names for name in KEYS[kind]):
        return _project_schema(kind, frame)
    rows = frame.to_dicts()
    if kind == "etf_shares" and _looks_like_raw_etf(rows):
        prepared = prepare_rows(kind, rows)
        if prepared:
            return frame_of(kind, prepared)
    return empty_frame(kind)


def _drop_ths_clobbering_eastmoney(frame: pl.DataFrame) -> pl.DataFrame:
    """同一主键已有东财行时，丢掉同花顺行，避免后写把净额盖错。"""
    if frame.is_empty() or "source" not in frame.columns or "code" not in frame.columns:
        return frame
    protected = (
        frame.filter(pl.col("source").is_in(sorted(EASTMONEY_STOCK_SOURCES)))
        .select(["code", "trade_date"])
        .unique()
    )
    if protected.is_empty():
        return frame
    blocked = (
        frame.filter(pl.col("source") == THS_STOCK_SOURCE)
        .select(["code", "trade_date"])
        .unique()
        .join(protected, on=["code", "trade_date"], how="inner")
    )
    if blocked.is_empty():
        return frame
    logger.info("个股资金流跳过 %s 个主键的同花顺行，避免覆盖东财", blocked.height)
    marked = blocked.with_columns(pl.lit(THS_STOCK_SOURCE).alias("source"))
    return frame.join(marked, on=["code", "trade_date", "source"], how="anti")


def _cn_stamp(moment: datetime | None) -> datetime:
    """入库时刻固定到北京时间、秒级。不使用服务器本地时区。"""
    if moment is None:
        moment = cn_now()
    moment = moment.replace(tzinfo=CN_TZ) if moment.tzinfo is None else moment.astimezone(CN_TZ)
    return moment.replace(microsecond=0)


def _without_stamps(rows: list[dict]) -> list[dict]:
    """调用方带入的时刻不算数，避免把补数时间写成首次可见时间。"""
    return [
        {key: value for key, value in row.items() if key not in _STAMP_COLS}
        for row in rows
    ]


def _values_unchanged() -> pl.Expr:
    checks: list[pl.Expr] = []
    for name in _ETF_VALUE_COLS:
        left = pl.col(name)
        right = pl.col(f"_prev_{name}")
        same = left.eq_missing(right)
        if _SCHEMAS["etf_shares"][name] == pl.Float64:
            same = same | (left.is_nan() & right.is_nan())
        checks.append(same)
    return pl.all_horizontal(checks)


def _merge_etf_shares(
    previous: pl.DataFrame | None,
    incoming: pl.DataFrame,
    moment: datetime,
) -> pl.DataFrame:
    """同一主键保留首次 ``ingested_at``。业务列变了才写 ``updated_at``。

    只合并这一个交易日分区，不读其它日期。历史行的入库时刻是空时保持为空。
    """
    keys = KEYS["etf_shares"]
    columns = list(_SCHEMAS["etf_shares"])
    incoming = _project_schema("etf_shares", incoming).unique(subset=keys, keep="last")
    stamp = pl.lit(moment).cast(_STAMP_DTYPE)
    blank = pl.lit(None).cast(_STAMP_DTYPE)
    if previous is None or previous.is_empty():
        return incoming.with_columns(
            stamp.alias("ingested_at"),
            blank.alias("updated_at"),
        ).select(columns)

    previous = _project_schema("etf_shares", previous).unique(subset=keys, keep="last")
    kept = previous.join(incoming.select(keys), on=keys, how="anti")
    prev = previous.select(
        *keys,
        *[pl.col(name).alias(f"_prev_{name}") for name in (*_ETF_VALUE_COLS, *_STAMP_COLS)],
        pl.lit(True).alias("_existed"),
    )
    joined = incoming.drop(list(_STAMP_COLS)).join(prev, on=keys, how="left")
    existed = pl.col("_existed").fill_null(False)
    changed = ~_values_unchanged()
    stamped = joined.with_columns(
        pl.when(existed).then(pl.col("_prev_ingested_at")).otherwise(stamp).alias("ingested_at"),
        pl.when(~existed).then(blank).when(changed).then(stamp).otherwise(
            pl.col("_prev_updated_at"),
        ).alias("updated_at"),
    ).select(columns)
    if kept.is_empty():
        return stamped
    return pl.concat([kept.select(columns), stamped], how="vertical_relaxed")


_LOCK_RETRY_S = 0.2
"""撞上分区文件锁后的退避间隔。

争锁窗口只有一次"读分区 → 合并 → ``os.replace``"，实测亚秒级；0.2 秒退避、10 次
重试 ≈ 2 秒，足够穿过同进程并发和一次快速回补的写入，又不至于让一次真正的长时间
占用把采集线程拖很久。与 ``replace_with_retry`` 的 (10 次 × 0.5 秒) 同一量级。
"""
_LOCK_ATTEMPTS = 10
"""文件锁重试次数。耗尽后抛 `PartitionLockBusy`，失败可见。"""


def _partition_key(kind: str, trade_date: str) -> str:
    return f"{kind}:{trade_date}"


def write_rows(
    data_dir: Path,
    kind: str,
    trade_date: str,
    rows: list[dict],
    *,
    now: datetime | None = None,
) -> int:
    """合并写入一个交易日分区。返回该分区合并后的行数。

    个股分区里，``akshare_ths`` 不能覆盖已有的东财或数据中心行。其它来源仍是后写覆盖先写。
    ``etf_shares`` 的 ``ingested_at`` 只在主键第一次出现时写入，之后保留。
    ``now`` 只用于测试注入北京时间；缺省取当前北京时间。

    「读旧分区 → 合并 → 写回」整段在同一分区的锁里完成，否则并发写会互相覆盖:
    两个线程都读到同一份旧内容，各自合并后分别 ``os.replace``，后落盘的那个把先落盘
    的整份覆盖掉 —— 另一个线程写入的行直接消失，而且不报错。

    锁分两层（``partition_writer``）: 线程锁挡同进程其它线程，文件锁挡**其它进程**
    —— 回补 CLI 与 Web 进程写同一个 ``date=`` 分区是完全可能的组合，原先只有线程锁，
    跨进程那条路径上 ``atomic_write_parquet`` 只能保证不写出半截文件，挡不住整份覆盖。

    **写者之间是耐心的**: 撞锁时按 ``_LOCK_RETRY_S`` 退避重试，而不是一次冲突就放弃
    整批采集 —— 争锁的窗口只有一次"读分区 + 合并 + replace"，等几百毫秒就能过。重试
    ``_LOCK_ATTEMPTS`` 次仍拿不到才抛 `app.services.fs_utils.PartitionLockBusy`，
    不让它静默跳过。
    """
    with partition_writer(
        Path(data_dir),
        _partition_key(kind, trade_date),
        retry_s=_LOCK_RETRY_S,
        attempts=_LOCK_ATTEMPTS,
        on_busy="info",
    ):
        return _write_rows_locked(data_dir, kind, trade_date, rows, now=now)


def _write_rows_locked(
    data_dir: Path,
    kind: str,
    trade_date: str,
    rows: list[dict],
    *,
    now: datetime | None = None,
) -> int:
    _check(kind, trade_date)
    rows = prepare_rows(kind, rows)
    if kind == "etf_shares":
        rows = _without_stamps(rows)
    if not rows:
        path = partition_file(data_dir, kind, trade_date)
        if path.exists():
            return align_frame(kind, pl.read_parquet(path)).height
        return 0
    assert_schema(kind, rows)
    incoming = frame_of(kind, rows)
    path = partition_file(data_dir, kind, trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "etf_shares":
        previous = align_frame(kind, pl.read_parquet(path)) if path.exists() else None
        merged = _merge_etf_shares(previous, incoming, _cn_stamp(now))
    else:
        if path.exists():
            previous = align_frame(kind, pl.read_parquet(path))
            merged = pl.concat([previous, incoming], how="vertical_relaxed")
        else:
            merged = incoming
        if kind == "stock":
            merged = _drop_ths_clobbering_eastmoney(merged)
        keys = [key for key in KEYS[kind] if key in merged.columns]
        merged = merged.unique(subset=keys, keep="last")
    atomic_write_parquet(merged, path)
    return merged.height


def etf_shares_view_sql(data_dir: Path) -> str:
    """ETF 份额的 DuckDB 视图。

    ``union_by_name`` 让没有 ``ingested_at`` 的历史分区和后来写入的分区能一起读。
    视图定义本身不扫描行。查出来的历史入库时刻是空，不补。
    """
    directory = (root(data_dir) / "etf_shares").as_posix().replace("'", "''")
    return (
        "CREATE OR REPLACE VIEW etf_shares AS "
        f"SELECT * FROM read_parquet('{directory}/**/*.parquet', union_by_name=true)"
    )


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


# 心跳采样点。``None`` 表示同步取当前时钟；测试可注入以避开真实时间。
_heartbeat_clock = None


def set_heartbeat_clock(clock) -> None:
    """测试用：注入心跳时钟。``None`` 恢复默认。"""
    global _heartbeat_clock
    _heartbeat_clock = clock


def _now_stamp() -> str:
    clock = _heartbeat_clock
    return (clock() if clock is not None else datetime.now()).isoformat(timespec="seconds")


def update_kind_state(data_dir: Path, kind: str, **fields: object) -> dict:
    """写 ``kind`` 的字段，并刷新它的 ``updated_at``。

    心跳字段 ``_scheduler`` 走 ``touch_heartbeat``，不用这个 —— 见那里的说明。
    """

    def mutate(payload: dict) -> None:
        current = dict(payload["kinds"].get(kind) or {})
        current.update(fields)
        current["updated_at"] = _now_stamp()
        payload["kinds"][kind] = current

    saved = mutate_state(data_dir, mutate)
    return dict(saved["kinds"][kind])


def touch_heartbeat(data_dir: Path, **fields: object) -> dict:
    """只刷新 ``_scheduler`` 心跳，不碰其它 ``kind``。

    独立成一条路径的原因：``_scheduler.updated_at`` 是「资金流循环还活着」的
    唯一信号，运维靠它判断调度器是否停摆。而 ``update_kind_state`` 是读改写整份
    payload —— 调用方若拿着一份陈旧快照（例如本轮开头 ``load_state`` 的结果），
    就会把早已过期的 ``_scheduler`` 原样写回，让心跳倒退。

    线上曾出现：文件 mtime 是 17:36（说明确实在写），而 ``_scheduler.updated_at``
    却停在 15:42 —— 心跳被并发的读改写复活成了旧值，停摆信号彻底失效。
    这里只改一个字段，且**不允许把更旧的时间写回**，从根上堵死这种倒退。
    """

    def mutate(payload: dict) -> None:
        current = dict(payload["kinds"].get("_scheduler") or {})
        current.update(fields)
        stamp = _now_stamp()
        previous = current.get("updated_at")
        # 心跳只前进不后退。同一秒内的并发写会得到相同的秒级时间戳，不算倒退。
        if isinstance(previous, str) and previous > stamp:
            current["updated_at"] = previous
        else:
            current["updated_at"] = stamp
        payload["kinds"]["_scheduler"] = current

    saved = mutate_state(data_dir, mutate)
    return dict(saved["kinds"]["_scheduler"])
