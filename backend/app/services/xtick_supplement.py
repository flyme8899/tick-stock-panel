"""XTick 历史竞价补充。只读官方「竞价数据-历史接口」，不进入主行情。

公开目录 ``http://xtick.top/assets/apidoc.json`` 中该接口为
``/doc/hot/bidhistory``，说明是「仅保留集合竞价期间的最后一条竞价数据和开盘数据」。
2026-11-06 的生产观察是 HTTP 200、ZIP 内 ``data.json``（单票 1 行，``all`` 5580 行）。
本模块按这个契约取数，失败只返回补充状态，不写主行情、不参与评分、风控或触发。

``seq`` 的官方 range 与 description 首句互相矛盾，这里保留原值，不采信任何一套说法。
``close`` 的官方说明是前收盘价，对外只叫 ``prev_close``。
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import threading
import time
import zipfile
from collections.abc import Callable
from datetime import date as date_cls
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.market_time import CN_TZ
from app.secrets_store import get_env_backed_secret
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

DATASET = "bidhistory"
SOURCE = "xtick"
DOCUMENTED_BASE = "http://api.xtick.top"
DOCUMENTED_PATH = "/doc/hot/bidhistory"
# 生产观察 all 为 5580 行。上限只留余量；超出整包拒绝，避免半截全市场被当成完整结果。
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_ZIP_ENTRIES = 16
MAX_ROWS = 8000
MAX_COMPRESSION_RATIO = 200
# 公开样例 time=1770307200000，是毫秒。更短的数不猜成秒。
_MS_MIN = 10**12
_MS_MAX = 10**14
_AUCTION_READY = dt_time(9, 26)
_CODE_RE = re.compile(r"^\d{6}$")
_TOKEN_QUERY_RE = re.compile(r"(?i)(token=)[^&\s]+")
_RECORD_KEYS = {
    "type", "code", "time", "seq", "price", "close", "jjzf", "jjl", "jje", "nol", "noe", "trend",
}
_ON = {"1", "true", "yes", "on"}
_LOCK = threading.Lock()
_LAST_HTTP_MONO: float | None = None

Transport = Callable[[str, float], tuple[int, bytes]]

SEQ_DOC = {
    "conflict": True,
    "adopted_label": None,
    "range_text": "0-开盘数据，1-集合竞价最后一条数据",
    "description_head": "seq为0表示集合竞价最后一条（9:25），seq为1表示倒数第二条",
    "description_tail": "一般来讲，seq为0是开盘数据，seq为1是集合竞价最后一条数据",
}


class SupplementError(Exception):
    def __init__(self, kind: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


def reset_rate_limit_for_tests() -> None:
    global _LAST_HTTP_MONO
    with _LOCK:
        _LAST_HTTP_MONO = None


def enabled() -> bool:
    return os.environ.get("XTICK_SUPPLEMENT_ENABLED", "").strip().lower() in _ON


def allow_all() -> bool:
    return os.environ.get("XTICK_BIDHISTORY_ALLOW_ALL", "").strip().lower() in _ON


def resolve_token() -> str:
    """secrets.json 的 ``xtick_api_token`` 优先，否则读 ``XTICK_API_TOKEN``。"""
    return get_env_backed_secret("xtick_api_token", "XTICK_API_TOKEN")


def http_timeout_s() -> float:
    raw = os.environ.get("XTICK_HTTP_TIMEOUT_S", "").strip()
    if not raw:
        return 15.0
    try:
        value = float(raw)
    except ValueError:
        return 15.0
    return min(60.0, max(1.0, value))


def min_interval_s() -> float:
    raw = os.environ.get("XTICK_MIN_INTERVAL_S", "").strip()
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return min(30.0, max(0.0, value))


def redact(text: str, token: str = "") -> str:
    cleaned = _TOKEN_QUERY_RE.sub(r"\1***", text or "")
    if token:
        cleaned = cleaned.replace(token, "***")
    return cleaned


def status_payload() -> dict[str, Any]:
    return {
        "source": SOURCE,
        "dataset": DATASET,
        "role": "supplement",
        "live": False,
        "enabled": enabled(),
        "credential_configured": bool(resolve_token()),
        "allow_all": allow_all(),
        "affects_scoring": False,
        "affects_risk": False,
        "affects_triggers": False,
        "primary_source_unchanged": True,
        "auth_note": "公开目录该接口 apiAuth 仅为 [4]。页面档位顺序是青铜/白银/黄金/至尊/量化，编号不能当成配额。",
        "quota_note": "公开目录没有写至尊版无限配额。默认只允许单票、单日、手动查询，不自动轮询。",
    }


def query_bidhistory(
    data_dir: Path,
    *,
    code: str,
    trade_date: str,
    seq: str | None = None,
    now: datetime | None = None,
    transport: Transport | None = None,
    mono: float | None = None,
) -> dict[str, Any]:
    """查询一个交易日的历史竞价终态。任何失败都返回状态，不抛给主链。"""
    clock = now.astimezone(CN_TZ) if now is not None else datetime.now(CN_TZ)
    token = ""
    try:
        token = resolve_token()
        payload = _query(
            data_dir,
            code=code,
            trade_date=trade_date,
            seq=seq,
            clock=clock,
            token=token,
            transport=transport or default_transport,
            mono=time.monotonic() if mono is None else mono,
        )
    except SupplementError as exc:
        log = logger.info if exc.kind == "invalid_request" else logger.warning
        log(
            "xtick bidhistory %s code=%s date=%s",
            exc.kind,
            _safe_code(code),
            _safe_date(trade_date),
        )
        payload = _envelope(code, trade_date, seq, clock)
        payload["state"] = {
            "rate_limited": "rate_limited",
            "invalid_request": "invalid_request",
        }.get(exc.kind, "error")
        payload["message"] = exc.message
        payload["error"] = {
            "kind": exc.kind,
            "message": exc.message,
            **({} if exc.http_status is None else {"http_status": exc.http_status}),
        }
    except Exception as exc:  # noqa: BLE001 — 补充源必须吞掉，不能冒泡到竞价主列表
        logger.warning(
            "xtick bidhistory isolated failure code=%s date=%s detail=%s",
            _safe_code(code),
            _safe_date(trade_date),
            redact(str(exc), token),
        )
        payload = _envelope(code, trade_date, seq, clock)
        payload["state"] = "error"
        payload["error"] = {"kind": "internal", "message": "补充源内部错误，主行情未改动。"}
    return _without_secret(payload, token)


def build_bidhistory_url(base: str, *, code: str, trade_date: str, seq: str | None, token: str) -> str:
    params = {
        "type": "1",
        "code": code,
        "startDate": trade_date,
        "endDate": trade_date,
        "token": token,
    }
    if seq is not None:
        params["seq"] = seq
    return f"{base.rstrip('/')}{DOCUMENTED_PATH}?{urlencode(params)}"


def default_transport(url: str, timeout: float) -> tuple[int, bytes]:
    try:
        with (
            httpx.Client(timeout=timeout, follow_redirects=False) as client,
            client.stream("GET", url) as resp,
        ):
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    raise SupplementError("too_large", "上游响应超过体积上限，已中止且未解析。")
                chunks.append(chunk)
            return resp.status_code, b"".join(chunks)
    except SupplementError:
        raise
    except httpx.TimeoutException as exc:
        raise SupplementError("timeout", "上游超时，未采用响应。") from exc
    except httpx.HTTPError as exc:
        raise SupplementError("transport", "上游连接失败，未采用响应。") from exc


def parse_upstream_body(body: bytes) -> list[dict[str, Any]]:
    if not body or not body.strip():
        raise SupplementError("empty_body", "上游返回空响应体。")
    if body.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raw = _read_zip_data_json(body)
    else:
        raw = body
    return _parse_records(raw)


def _query(
    data_dir: Path,
    *,
    code: str,
    trade_date: str,
    seq: str | None,
    clock: datetime,
    token: str,
    transport: Transport,
    mono: float,
) -> dict[str, Any]:
    payload = _envelope(code, trade_date, seq, clock)
    if not enabled():
        payload["state"] = "disabled"
        payload["message"] = "XTICK_SUPPLEMENT_ENABLED 未开启。主行情、竞价扫描和信号都未请求 XTick。"
        return payload
    normalized_code, normalized_seq, day = _validate(code, trade_date, seq)
    payload["code"] = normalized_code
    payload["trade_date"] = day.isoformat()
    payload["seq_requested"] = normalized_seq
    if not token:
        payload["state"] = "missing_credential"
        payload["message"] = "未配置 XTick token。可写运行时环境变量 XTICK_API_TOKEN，或 secrets.json 的 xtick_api_token。"
        return payload
    refusal = _date_refusal(day, clock)
    if refusal is not None:
        payload["state"], payload["freshness"], payload["message"] = refusal
        return payload
    cached = _read_cache(data_dir, day, normalized_code, normalized_seq)
    if cached is not None:
        cached["cache"] = "hit"
        cached["as_of"] = _iso(clock)
        cached["live"] = False
        cached["freshness"] = _freshness(day, clock)
        if cached.get("state") == "ok":
            cached["message"] = _freshness_message(cached["freshness"])
        return cached
    if not _reserve_http_slot(mono):
        raise SupplementError("rate_limited", "距上一次上游请求太近，本次未发出，避免连续消耗额度。")
    url = build_bidhistory_url(
        _api_base(),
        code=normalized_code,
        trade_date=day.isoformat(),
        seq=normalized_seq,
        token=token,
    )
    status, body = transport(url, http_timeout_s())
    if status == 429:
        raise SupplementError("rate_limited", "上游返回 429，本次未采用响应。", http_status=429)
    if status != 200:
        raise SupplementError(
            "http_status",
            f"上游返回 {status}，响应体未采用。",
            http_status=status,
        )
    records = parse_upstream_body(body)
    rows = [_normalize_row(item, day) for item in records]
    payload["rows"] = rows
    payload["row_count"] = len(rows)
    payload["cache"] = "miss"
    payload["fetched_at"] = _iso(clock)
    if not rows:
        payload["state"] = "missing"
        payload["freshness"] = _freshness(day, clock)
        payload["message"] = "上游没有记录。空结果也可能是限流，不能据此判断当天没有竞价。"
    else:
        payload["state"] = "ok"
        payload["freshness"] = _freshness(day, clock)
        payload["message"] = _freshness_message(payload["freshness"])
        if any(not row["time_matches_trade_date"] for row in rows):
            payload["warnings"] = ["vendor_time_date_mismatch"]
    # 空结果不落盘。上游限流也可能表现为空，缓存住会把缺失固化成历史事实。
    if rows:
        if token and token in json.dumps(payload, ensure_ascii=False):
            raise SupplementError("redaction", "上游记录含凭据，已丢弃且未落盘。")
        _write_cache(data_dir, day, normalized_code, normalized_seq, payload)
    return payload


def _envelope(code: str, trade_date: str, seq: str | None, clock: datetime) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "dataset": DATASET,
        "role": "supplement",
        "live": False,
        "affects_scoring": False,
        "affects_risk": False,
        "affects_triggers": False,
        "primary_source_unchanged": True,
        "state": "error",
        "code": code.strip(),
        "trade_date": trade_date.strip(),
        "seq_requested": seq,
        "seq_doc": SEQ_DOC,
        "freshness": "unknown",
        "as_of": _iso(clock),
        "fetched_at": None,
        "cache": "skipped",
        "rows": [],
        "row_count": 0,
        "message": "",
        "error": None,
        "warnings": [],
        "unit_note": (
            "官方字段说明没写竞价量、竞价额的单位。公开样例满足 "
            "金额 = 价格 × 数量 × 100。等式成立时标注，不换算，也不写入主行情成交量。"
        ),
    }


def _validate(code: str, trade_date: str, seq: str | None) -> tuple[str, str | None, date_cls]:
    text = code.strip()
    lowered = text.lower()
    if lowered == "all":
        if not allow_all():
            raise SupplementError(
                "invalid_request",
                "全市场 all 默认关闭。需要时才设置 XTICK_BIDHISTORY_ALLOW_ALL，避免一次拉出数千行。",
            )
        normalized = "all"
    elif _CODE_RE.fullmatch(text):
        normalized = text
    else:
        raise SupplementError("invalid_request", "代码只接受 6 位数字。批量代码不在本轮范围。")
    try:
        day = date_cls.fromisoformat(trade_date.strip())
    except ValueError as exc:
        raise SupplementError("invalid_request", "交易日格式应为 YYYY-MM-DD。") from exc
    if day.isoformat() != trade_date.strip():
        raise SupplementError("invalid_request", "交易日格式应为 YYYY-MM-DD。")
    if seq is None or str(seq).strip() == "":
        normalized_seq = None
    elif str(seq).strip() in {"0", "1"}:
        normalized_seq = str(seq).strip()
    else:
        raise SupplementError("invalid_request", "seq 只接受 0、1，或留空沿用上游默认。")
    return normalized, normalized_seq, day


def _date_refusal(day: date_cls, clock: datetime) -> tuple[str, str, str] | None:
    today = clock.date()
    if day > today:
        return "invalid_request", "unknown", "不能查询未来日期。"
    if day.weekday() >= 5:
        return "invalid_request", "unknown", "周六日不向上游请求，避免空调用消耗额度。法定假日没有在这里拦截。"
    if day == today and clock.time() < _AUCTION_READY:
        return (
            "not_ready",
            "not_ready",
            "北京时间未到 09:26。历史竞价接口只保留终态，此时间前不请求，避免把未完成竞价当成实时。",
        )
    return None


def _freshness(day: date_cls, clock: datetime) -> str:
    if day < clock.date():
        return "historical"
    if day == clock.date():
        return "same_day_final"
    return "unknown"


def _freshness_message(freshness: str) -> str:
    if freshness == "historical":
        return "查询日早于北京时间今天。这是历史补充，不是实时行情。"
    if freshness == "same_day_final":
        return "已过北京时间 09:26，按当日竞价终态读取。不是盘中实时行情，不写入主行情。"
    return "补充记录。"


def _normalize_row(item: dict[str, Any], day: date_cls) -> dict[str, Any]:
    code = item.get("code")
    code_text = str(code).strip() if code is not None else ""
    price = _as_float(item.get("price"))
    prev_close = _as_float(item.get("close"))
    jjl = _as_number(item.get("jjl"))
    jje = _as_number(item.get("jje"))
    nol = _as_number(item.get("nol"))
    noe = _as_number(item.get("noe"))
    time_ms = _as_int(item.get("time"))
    time_bj = None
    time_date = None
    midnight = False
    if time_ms is not None and _MS_MIN <= time_ms <= _MS_MAX:
        stamp = datetime.fromtimestamp(time_ms / 1000, CN_TZ)
        time_bj = stamp.strftime("%Y-%m-%d %H:%M:%S")
        time_date = stamp.date().isoformat()
        midnight = stamp.hour == 0 and stamp.minute == 0 and stamp.second == 0
    trend = _as_int(item.get("trend"))
    return {
        "type": _as_int(item.get("type")),
        "code": code_text or None,
        "symbol": _display_symbol(code_text),
        "seq": _as_int(item.get("seq")),
        "time_ms": time_ms,
        "time_bj": time_bj,
        "time_calendar_date": time_date,
        "time_matches_trade_date": time_date == day.isoformat() if time_date else False,
        "time_is_midnight": midnight,
        "price": price,
        "prev_close": prev_close,
        "jjzf_percent": _as_float(item.get("jjzf")),
        "jjl": jjl,
        "jje": jje,
        "nol": nol,
        "noe": noe,
        "trend": trend,
        "trend_label": _trend_label(trend),
        "amount_identity": {
            "jj": _identity(price, jjl, jje),
            "unmatched": _identity(price, nol, noe),
        },
    }


def _trend_label(trend: int | None) -> str | None:
    if trend == -1:
        return "主动性卖"
    if trend == 0:
        return "中性盘"
    if trend == 1:
        return "主动性买"
    return None


def _identity(price: float | None, qty: float | None, amount: float | None) -> str:
    if price is None or qty is None or amount is None:
        return "incomplete"
    expected = price * qty * 100
    tolerance = max(1.0, abs(amount) * 1e-6)
    if math.isfinite(expected) and abs(expected - amount) <= tolerance:
        return "matches_price_times_qty_times_100"
    return "mismatch"


def _display_symbol(code: str) -> str | None:
    """只给能对上现有板块前缀的代码生成跳转符号。9 开头除 920 外不猜测交易所。"""
    text = code.strip().upper()
    if text.endswith((".SH", ".SZ", ".BJ")) and _CODE_RE.fullmatch(text[:6]):
        return text
    if not _CODE_RE.fullmatch(text):
        return None
    if text.startswith(("920", "8", "43")):
        return f"{text}.BJ"
    if text.startswith(("60", "68")):
        return f"{text}.SH"
    if text.startswith(("00", "30")):
        return f"{text}.SZ"
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _as_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value) if value.is_integer() else value
    return None


def _read_zip_data_json(body: bytes) -> bytes:
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        raise SupplementError("zip", "响应不是可读的 ZIP。") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise SupplementError("too_large", "ZIP 条目过多，已拒绝。")
    uncompressed = 0
    data_info = None
    for info in infos:
        name = info.filename
        if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
            raise SupplementError("zip", "ZIP 路径不安全，已拒绝。")
        uncompressed += info.file_size
        if uncompressed > MAX_UNCOMPRESSED_BYTES or info.file_size > MAX_UNCOMPRESSED_BYTES:
            raise SupplementError("too_large", "ZIP 解压后超过体积上限，已拒绝。")
        if (
            info.compress_size
            and info.file_size > 1024 * 1024
            and info.file_size > info.compress_size * MAX_COMPRESSION_RATIO
        ):
            raise SupplementError("too_large", "ZIP 压缩比异常，已拒绝。")
        if name == "data.json":
            data_info = info
    if data_info is None:
        raise SupplementError("zip", "ZIP 中没有 data.json。")
    raw = archive.read(data_info)
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise SupplementError("too_large", "data.json 超过体积上限，已拒绝。")
    return raw


def _parse_records(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SupplementError("json", "data.json 不是 UTF-8。") from exc
    stripped = text.strip()
    if not stripped:
        raise SupplementError("empty_body", "data.json 为空。")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = _parse_ndjson(text)
    else:
        parsed = _coerce_parsed(parsed)
    if len(parsed) > MAX_ROWS:
        raise SupplementError("too_large", f"记录数超过 {MAX_ROWS}，整包拒绝。")
    return parsed


def _coerce_parsed(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        if any(not isinstance(item, dict) for item in parsed):
            raise SupplementError("json", "记录不是 JSON 对象。")
        return list(parsed)
    if isinstance(parsed, dict):
        if not _RECORD_KEYS.intersection(parsed):
            raise SupplementError("json", "无法识别的 JSON 结构。")
        return [parsed]
    raise SupplementError("json", "无法识别的 JSON 结构。")


def _parse_ndjson(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    saw_payload = False
    for line in text.splitlines():
        item = line.strip().rstrip(",")
        if not item or item in {"[", "]"}:
            continue
        saw_payload = True
        if len(rows) >= MAX_ROWS:
            raise SupplementError("too_large", f"记录数超过 {MAX_ROWS}，整包拒绝。")
        try:
            parsed = json.loads(item)
        except json.JSONDecodeError as exc:
            raise SupplementError("json", "data.json 无法按 JSON 或逐行 JSON 解析。") from exc
        if not isinstance(parsed, dict):
            raise SupplementError("json", "逐行记录不是 JSON 对象。")
        rows.append(parsed)
    if not saw_payload:
        raise SupplementError("json", "data.json 无法按 JSON 或逐行 JSON 解析。")
    return rows


def _cache_path(data_dir: Path, day: date_cls, code: str, seq: str | None) -> Path:
    seq_part = "any" if seq is None else seq
    return (
        Path(data_dir)
        / "xtick_supplement"
        / "bidhistory"
        / f"date={day.isoformat()}"
        / f"{code}__seq-{seq_part}.json"
    )


def _read_cache(data_dir: Path, day: date_cls, code: str, seq: str | None) -> dict[str, Any] | None:
    path = _cache_path(data_dir, day, code, seq)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("dataset") != DATASET:
        return None
    return data


def _write_cache(
    data_dir: Path,
    day: date_cls,
    code: str,
    seq: str | None,
    payload: dict[str, Any],
) -> None:
    path = _cache_path(data_dir, day, code, seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = dict(payload)
    stored["cache"] = "hit"
    atomic_write_text(path, json.dumps(stored, ensure_ascii=False, separators=(",", ":")))


def _reserve_http_slot(mono: float) -> bool:
    global _LAST_HTTP_MONO
    interval = min_interval_s()
    with _LOCK:
        if _LAST_HTTP_MONO is not None and mono - _LAST_HTTP_MONO < interval:
            return False
        _LAST_HTTP_MONO = mono
    return True


def _api_base() -> str:
    raw = os.environ.get("XTICK_API_BASE", "").strip() or DOCUMENTED_BASE
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SupplementError("invalid_request", "XTICK_API_BASE 不是可用的 http(s) 地址。")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise SupplementError("invalid_request", "XTICK_API_BASE 不能携带账号、查询串或片段。")
    return raw.rstrip("/")


def _without_secret(payload: dict[str, Any], token: str) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False)
    if token and token in encoded:
        logger.warning("xtick bidhistory dropped a payload that contained the token")
        safe = _envelope(
            str(payload.get("code") or ""),
            str(payload.get("trade_date") or ""),
            None,
            datetime.now(CN_TZ),
        )
        safe["state"] = "error"
        safe["error"] = {"kind": "redaction", "message": "响应含凭据，已丢弃。"}
        return safe
    return payload


def _safe_code(code: str) -> str:
    text = (code or "").strip()
    if text.lower() == "all" or _CODE_RE.fullmatch(text):
        return text.lower() if text.lower() == "all" else text
    return "invalid"


def _safe_date(value: str) -> str:
    try:
        return date_cls.fromisoformat((value or "").strip()).isoformat()
    except ValueError:
        return "invalid"


def _iso(clock: datetime) -> str:
    return clock.astimezone(CN_TZ).isoformat(timespec="seconds")
