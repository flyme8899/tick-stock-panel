"""板块监控目标目录与实时聚合快照。"""
from __future__ import annotations

import hashlib
import math
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.services import preferences
from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.index_const import CORE_INDEX_NAMES as CORE_INDICES
SECTOR_KINDS = {"index", "concept", "industry"}
ALL_A_KIND = "all_a"
ALL_A_NAME = "全A"
_VALUE_SEP = re.compile(r"[\u3001,\uff0c;\uff1b|]+")
_NULL_VALUES = {"nan", "none", "null", "<na>", "n/a", "-"}
_HISTORY_SECONDS = 31 * 60
_WINDOW_TOLERANCE_SECONDS = 90


def all_a_snapshot_key(min_valid: int, min_coverage: float) -> str:
    """同一门槛共享一条等权基线; 门槛变化即换键, 避免松紧规则互相污染窗口。"""
    return f"market:all_a:{int(min_valid)}:{float(min_coverage):.4f}"


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _dimension_kind(field_name: str, field_label: str) -> str | None:
    text = f"{field_name} {field_label}".lower()
    if any(word in text for word in ("概念", "题材", "concept", "theme")):
        return "concept"
    if any(word in text for word in ("行业", "申万", "中信", "industry", "sector")):
        return "industry"
    return None


def _target_key(kind: str, source_id: str, field: str, value: str, level: int | None) -> str:
    raw = f"{kind}\0{source_id}\0{field}\0{level or 0}\0{value}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


class SectorMonitorService:
    """缓存板块成员关系, 并按启用规则构建轻量实时快照。"""

    all_a_snapshot_key = staticmethod(all_a_snapshot_key)

    def __init__(self, repo) -> None:
        self._repo = repo
        self._data_dir: Path = repo.store.data_dir
        self._catalog_signature: tuple[tuple[str, int, int], ...] | None = None
        self._catalog: dict[str, list[dict]] = {kind: [] for kind in SECTOR_KINDS}
        self._targets_by_key: dict[str, dict] = {}
        self._members_by_key: dict[str, set[str]] = {}
        # 行业+概念成分去重后的全市场集合, 只在目录签名变化时重建。
        self._all_a_members: set[str] = set()
        self._all_a_member_list: list[str] = []
        self._history: dict[str, deque[tuple[float, float]]] = {}
        self._history_day: str | None = None

    def list_targets(self) -> dict[str, list[dict]]:
        self._ensure_catalog()
        return {kind: [dict(item) for item in self._catalog[kind]] for kind in self._catalog}

    def all_a_member_count(self) -> int:
        """全A 分母: 已缓存的行业/概念成分去重数量, 不读行情、不扫库。"""
        self._ensure_catalog()
        return len(self._all_a_members)

    def missing_target_keys(self, targets: list[dict]) -> list[str]:
        self._ensure_catalog()
        return [str(target.get("key") or "") for target in targets if target.get("key") not in self._targets_by_key]

    def unavailable_target_keys(self, targets: list[dict]) -> list[str]:
        self._ensure_catalog()
        return [
            str(target.get("key") or "")
            for target in targets
            if target.get("key") in self._targets_by_key
            and not self._targets_by_key[target["key"]].get("available", True)
        ]

    def build_snapshots(
        self,
        stock_df: pl.DataFrame,
        index_df: pl.DataFrame,
        targets: list[dict],
        windows: set[int],
        *,
        now: float,
    ) -> dict[str, dict]:
        if not targets:
            return {}
        self._ensure_catalog()
        self._reset_history_for_day(now)

        kinds = [self._resolved_kind(raw) for raw in targets]
        # 个股行展开只服务具体概念/行业。全A 与纯指数规则不把整帧收成 Python dict。
        stock_rows = (
            self._row_map(stock_df, index_values_are_percent=False)
            if any(kind in {"concept", "industry"} for kind in kinds)
            else {}
        )
        index_rows = (
            self._row_map(index_df, index_values_are_percent=True)
            if any(kind == "index" for kind in kinds)
            else {}
        )
        all_a_stats = self._all_a_stats(stock_df) if any(kind == "all_a" for kind in kinds) else None
        snapshots: dict[str, dict] = {}

        for raw_target, kind in zip(targets, kinds, strict=True):
            key = str(raw_target.get("key") or "")
            if kind == "all_a":
                snapshot = self._all_a_snapshot(all_a_stats or {}, raw_target)
            else:
                target = self._targets_by_key.get(key)
                if not target:
                    continue
                if target["kind"] == "index":
                    snapshot = self._index_snapshot(target, index_rows)
                else:
                    snapshot = self._dimension_snapshot(target, stock_rows)
            if snapshot is None:
                continue

            change_pct = snapshot.get("change_pct")
            history = self._history.setdefault(key, deque())
            if snapshot["valid"] and change_pct is not None:
                history.append((now, float(change_pct)))
                while history and history[0][0] < now - _HISTORY_SECONDS:
                    history.popleft()

            snapshot["window_changes"] = {
                window: self._window_change(history, now, window, change_pct)
                for window in windows
            }
            snapshots[key] = snapshot
        return snapshots

    def _ensure_catalog(self) -> None:
        signature = self._data_signature()
        if signature == self._catalog_signature:
            return
        catalog = {kind: [] for kind in SECTOR_KINDS}
        targets_by_key: dict[str, dict] = {}
        members_by_key: dict[str, set[str]] = {}

        index_names = dict(CORE_INDICES)
        try:
            indices = self._repo.get_index_instruments()
            if not indices.is_empty() and "symbol" in indices.columns:
                for row in indices.to_dicts():
                    if row.get("asset_type") == "etf":
                        continue
                    symbol = str(row.get("symbol") or "").strip().upper()
                    if symbol:
                        index_names[symbol] = str(row.get("name") or symbol)
        except Exception:
            pass

        # 指数标的恒可实时监控: quote_service 对核心四只 + 启用规则标的显式拉取
        for symbol, name in sorted(index_names.items()):
            target = {
                "key": f"index:{symbol}",
                "kind": "index",
                "name": name,
                "symbol": symbol,
                "available": True,
                "member_count": 1,
            }
            catalog["index"].append(target)
            targets_by_key[target["key"]] = target
        catalog["index"].sort(key=lambda item: (not item["available"], item["symbol"]))

        for config in ExtConfigStore(self._data_dir).load_all():
            df = self._read_ext_dataframe(config)
            if df.is_empty():
                continue
            symbol_col = self._symbol_column(config, df)
            if not symbol_col:
                continue
            for field in config.fields:
                kind = _dimension_kind(field.name, field.label)
                if kind is None or field.name not in df.columns:
                    continue
                for row in df.select([symbol_col, field.name]).iter_rows(named=True):
                    symbol = str(row.get(symbol_col) or "").strip().upper()
                    if not symbol:
                        continue
                    for raw_value in self._dimension_values(row.get(field.name)):
                        paths = self._industry_paths(raw_value) if kind == "industry" else [(raw_value, None, raw_value)]
                        for value, level, name in paths:
                            key = _target_key(kind, config.id, field.name, value, level)
                            members_by_key.setdefault(key, set()).add(symbol)
                            if key not in targets_by_key:
                                target = {
                                    "key": key,
                                    "kind": kind,
                                    "name": name,
                                    "source_id": config.id,
                                    "field": field.name,
                                    "source_field": f"{config.id}.{field.name}",
                                    "value": value,
                                    "level": level,
                                    "available": True,
                                }
                                targets_by_key[key] = target
                                catalog[kind].append(target)

        all_a_members: set[str] = set()
        for kind in ("concept", "industry"):
            for target in catalog[kind]:
                members = members_by_key.get(target["key"], set())
                target["member_count"] = len(members)
                all_a_members.update(members)
            catalog[kind].sort(key=lambda item: (item.get("level") or 0, item["name"], item["value"]))

        self._catalog_signature = signature
        self._catalog = catalog
        self._targets_by_key = targets_by_key
        self._members_by_key = members_by_key
        self._all_a_members = all_a_members
        self._all_a_member_list = sorted(all_a_members)
        self._history.clear()

    def _data_signature(self) -> tuple[tuple[str, int, int], ...]:
        base = self._data_dir / "ext_data"
        paths: list[Path] = []
        for config in ExtConfigStore(self._data_dir).load_all():
            if not any(_dimension_kind(field.name, field.label) for field in config.fields):
                continue
            config_dir = base / config.id
            paths.extend(config_dir.rglob("config.json"))
            paths.extend(config_dir.rglob("*.parquet"))
        signature = [
            (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            for path in sorted(paths)
            if path.is_file()
        ]
        return tuple(signature)

    def _read_ext_dataframe(self, config: ExtConfig) -> pl.DataFrame:
        base = self._data_dir / "ext_data" / config.id
        if config.mode == "timeseries":
            files = sorted((base / "timeseries").rglob("*.parquet"))
            files = files[-1:] if files else []
        else:
            files = sorted(base.glob("*.parquet"))
        if not files:
            return pl.DataFrame()
        try:
            return pl.read_parquet(files)
        except Exception:
            return pl.DataFrame()

    @staticmethod
    def _symbol_column(config: ExtConfig, df: pl.DataFrame) -> str | None:
        candidates = ["symbol", "code", "股票代码", "代码"]
        for mapping in (config.symbol_map, config.code_map):
            if isinstance(mapping, dict) and mapping.get("type") == "mapped":
                candidates.append(str(mapping.get("col") or ""))
        return next((column for column in candidates if column in df.columns), None)

    @staticmethod
    def _dimension_values(raw: Any) -> list[str]:
        if raw is None:
            return []
        return [
            value.strip()
            for value in _VALUE_SEP.split(str(raw))
            if value.strip() and value.strip().casefold() not in _NULL_VALUES
        ]

    @staticmethod
    def _industry_paths(raw: str) -> list[tuple[str, int, str]]:
        parts = [part.strip() for part in raw.split("-") if part.strip()]
        if not parts:
            return []
        return [
            ("-".join(parts[:level]), level, " / ".join(parts[:level]))
            for level in range(1, len(parts) + 1)
        ]

    @staticmethod
    def _row_map(df: pl.DataFrame, *, index_values_are_percent: bool) -> dict[str, dict]:
        if df.is_empty() or "symbol" not in df.columns:
            return {}
        rows: dict[str, dict] = {}
        for row in df.to_dicts():
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            change_pct = _finite(row.get("change_pct"))
            if change_pct is not None and index_values_are_percent:
                change_pct /= 100
            rows[symbol] = {**row, "change_pct": change_pct}
        return rows

    @staticmethod
    def _index_snapshot(target: dict, index_rows: dict[str, dict]) -> dict | None:
        row = index_rows.get(str(target.get("symbol") or "").upper())
        if not row or row.get("change_pct") is None:
            return None
        return {
            **target,
            "valid": True,
            "change_pct": row["change_pct"],
            "price": _finite(row.get("close") or row.get("last_price")),
            "coverage_ratio": 1.0,
            "valid_count": 1,
            "total_count": 1,
            "up_count": int(row["change_pct"] > 0),
            "down_count": int(row["change_pct"] < 0),
            "leader": None,
        }

    def _dimension_snapshot(self, target: dict, stock_rows: dict[str, dict]) -> dict | None:
        members = self._members_by_key.get(target["key"], set())
        if not members:
            return None
        valid_rows = [
            stock_rows[symbol]
            for symbol in members
            if symbol in stock_rows and stock_rows[symbol].get("change_pct") is not None
        ]
        total_count = len(members)
        valid_count = len(valid_rows)
        coverage_ratio = valid_count / total_count if total_count else 0.0
        valid = total_count >= 5 and coverage_ratio >= 0.8
        changes = [float(row["change_pct"]) for row in valid_rows]
        leader = max(valid_rows, key=lambda row: row["change_pct"]) if valid_rows else None
        return {
            **target,
            "valid": valid,
            "change_pct": sum(changes) / len(changes) if changes else None,
            "price": None,
            "coverage_ratio": coverage_ratio,
            "valid_count": valid_count,
            "total_count": total_count,
            "up_count": sum(value > 0 for value in changes),
            "down_count": sum(value < 0 for value in changes),
            "leader": {
                "symbol": leader.get("symbol"),
                "name": leader.get("name"),
                "change_pct": leader.get("change_pct"),
            } if leader else None,
        }

    def _resolved_kind(self, raw_target: dict) -> str | None:
        if raw_target.get("kind") == ALL_A_KIND:
            return ALL_A_KIND
        target = self._targets_by_key.get(str(raw_target.get("key") or ""))
        if not target:
            return None
        return str(target.get("kind") or "")

    def _all_a_stats(self, stock_df: pl.DataFrame) -> dict:
        """等权聚合全A。分母是目录缓存, 分子只来自本轮已在内存中的行情帧。"""
        total = len(self._all_a_members)
        empty = {
            "change_pct": None,
            "coverage_ratio": 0.0,
            "valid_count": 0,
            "total_count": total,
            "up_count": 0,
            "down_count": 0,
            "leader": None,
        }
        if (
            total == 0
            or stock_df.is_empty()
            or "symbol" not in stock_df.columns
            or "change_pct" not in stock_df.columns
        ):
            return empty
        frame = stock_df.with_columns(
            pl.col("symbol").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("_symbol"),
            pl.col("change_pct").cast(pl.Float64, strict=False).alias("_change"),
        ).filter(
            pl.col("_symbol").is_in(self._all_a_member_list)
            & pl.col("_change").is_not_null()
            & pl.col("_change").is_finite()
        )
        if frame.is_empty():
            return empty
        frame = frame.unique(subset=["_symbol"], keep="first")
        valid_count = frame.height
        summary = frame.select(
            pl.col("_change").mean().alias("change_pct"),
            (pl.col("_change") > 0).sum().alias("up_count"),
            (pl.col("_change") < 0).sum().alias("down_count"),
        ).row(0, named=True)
        leader_row = frame.sort("_change", descending=True).row(0, named=True)
        leader_name = leader_row.get("name") if "name" in frame.columns else None
        return {
            "change_pct": float(summary["change_pct"]),
            "coverage_ratio": valid_count / total,
            "valid_count": valid_count,
            "total_count": total,
            "up_count": int(summary["up_count"]),
            "down_count": int(summary["down_count"]),
            "leader": {
                "symbol": leader_row.get("_symbol"),
                "name": leader_name,
                "change_pct": float(leader_row["_change"]),
            },
        }

    @staticmethod
    def _all_a_snapshot(stats: dict, target: dict) -> dict:
        min_valid = int(target.get("min_valid_count") or 5)
        min_coverage = target.get("min_coverage")
        coverage_floor = 0.8 if min_coverage is None else float(min_coverage)
        valid_count = int(stats.get("valid_count") or 0)
        coverage = float(stats.get("coverage_ratio") or 0.0)
        change = stats.get("change_pct")
        valid = (
            change is not None
            and valid_count >= min_valid
            and coverage + 1e-9 >= coverage_floor
        )
        return {
            "key": target.get("key"),
            "kind": ALL_A_KIND,
            "name": ALL_A_NAME,
            "valid": valid,
            "change_pct": change,
            "price": None,
            "coverage_ratio": coverage,
            "valid_count": valid_count,
            "total_count": int(stats.get("total_count") or 0),
            "up_count": int(stats.get("up_count") or 0),
            "down_count": int(stats.get("down_count") or 0),
            "leader": stats.get("leader"),
            "symbol": None,
            "source_field": None,
            "value": None,
            "level": None,
        }

    @staticmethod
    def _window_change(
        history: deque[tuple[float, float]],
        now: float,
        window: int,
        current: float | None,
    ) -> float | None:
        if current is None:
            return None
        cutoff = now - window * 60
        for timestamp, previous in reversed(history):
            if timestamp <= cutoff:
                if timestamp < cutoff - _WINDOW_TOLERANCE_SECONDS:
                    return None
                return current - previous
        return None

    def _reset_history_for_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now).date().isoformat()
        if self._history_day == day:
            return
        self._history_day = day
        self._history.clear()
