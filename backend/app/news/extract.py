"""从正文和结构化字段里抽出股票、板块。

股票名按最长匹配。重名（两只股票同名）不参与正文匹配，只认代码。
板块词典由调用方传入（申万行业 / 概念），不在这里写死名单。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_CODE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:(SH|SZ|BJ|SS)[\.]?)?(\d{6})(?:\.(SH|SZ|BJ|SS))?(?![0-9])"
)
_EXCHANGE = {"SS": "SH", "SH": "SH", "SZ": "SZ", "BJ": "BJ"}


@dataclass(frozen=True)
class Mention:
    kind: str
    key: str
    name: str
    code: str
    origin: str


@dataclass(frozen=True)
class StructuredStock:
    name: str = ""
    code: str = ""


def code6_of(raw: str) -> str:
    match = _CODE_RE.search(raw or "")
    if not match:
        return ""
    return match.group(2)


def exchange_of(raw: str, code: str) -> str:
    match = _CODE_RE.search(raw or "")
    if match:
        prefix = (match.group(1) or "").upper()
        suffix = (match.group(3) or "").upper()
        token = suffix or prefix
        if token in _EXCHANGE:
            return _EXCHANGE[token]
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("8", "4")) or code.startswith("92"):
        return "BJ"
    return "SZ"


def canonical_symbol(raw: str, known: dict[str, str] | None = None) -> str:
    """6 位代码 → `600519.SH`。词典里已有则沿用维表 symbol。"""
    code = code6_of(raw)
    if not code:
        return ""
    if known and code in known:
        return known[code]
    if code.startswith("20"):
        return ""
    return f"{code}.{exchange_of(raw, code)}"


class Lexicon:
    def __init__(self, stocks: list[tuple[str, str, str]], sectors: list[str] | None = None):
        """stocks: (symbol, name, code)。code 可以是 6 位或带后缀的 symbol。"""
        self.by_code: dict[str, tuple[str, str]] = {}
        name_hits: dict[str, set[str]] = {}
        for symbol, name, code in stocks:
            code6 = code6_of(code) or code6_of(symbol)
            symbol = (symbol or "").strip()
            name = (name or "").strip()
            if code6 and symbol:
                self.by_code.setdefault(code6, (symbol, name))
            if name and len(name) >= 2 and symbol:
                name_hits.setdefault(name, set()).add(symbol)
        self._name_symbol: dict[str, str] = {
            name: next(iter(symbols))
            for name, symbols in name_hits.items()
            if len(symbols) == 1
        }
        self._names = _index(self._name_symbol)
        self._sectors = _index({name: name for name in (sectors or []) if name and len(name) >= 2})

    def resolve(self, raw: str, fallback_name: str = "") -> tuple[str, str, str]:
        code = code6_of(raw)
        if code and code in self.by_code:
            symbol, name = self.by_code[code]
            return symbol, name or fallback_name, code
        symbol = canonical_symbol(raw, {c: s for c, (s, _) in self.by_code.items()})
        if not symbol:
            return "", fallback_name, code
        return symbol, fallback_name or symbol, code

    def extract(
        self,
        text: str,
        structured_stocks: list[StructuredStock] | None = None,
        structured_sectors: list[str] | None = None,
    ) -> list[Mention]:
        found: list[Mention] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, key: str, name: str, code: str, origin: str) -> None:
            key = (key or "").strip()
            if not key or (kind, key) in seen:
                return
            seen.add((kind, key))
            found.append(Mention(kind, key, (name or key).strip(), code, origin))

        for stock in structured_stocks or []:
            symbol, name, code = self.resolve(stock.code, stock.name)
            if symbol:
                add("stock", symbol, name or stock.name, code, "structured")
            elif stock.name and stock.name in self._name_symbol:
                symbol = self._name_symbol[stock.name]
                code = code6_of(symbol)
                add("stock", symbol, stock.name, code, "structured")
        for sector in structured_sectors or []:
            sector = (sector or "").strip()
            if sector:
                add("sector", sector, sector, "", "structured")

        body = text or ""
        for match in _CODE_RE.finditer(body):
            raw = match.group(0)
            symbol, name, code = self.resolve(raw)
            if symbol and (code in self.by_code or not code.startswith("20")):
                if code.startswith("20") and code not in self.by_code:
                    continue
                add("stock", symbol, name, code, "text")
        for name, _start, _end in _scan(body, self._names):
            symbol = self._name_symbol[name]
            code = code6_of(symbol)
            known = self.by_code.get(code)
            add("stock", symbol, known[1] if known and known[1] else name, code, "text")
        for name, _start, _end in _scan(body, self._sectors):
            add("sector", name, name, "", "text")
        return found


def _index(mapping: dict[str, str]) -> dict[str, list[str]]:
    bucket: dict[str, list[str]] = {}
    for name in mapping:
        bucket.setdefault(name[0], []).append(name)
    for names in bucket.values():
        names.sort(key=len, reverse=True)
    return bucket


def _scan(text: str, index: dict[str, list[str]]) -> list[tuple[str, int, int]]:
    hits: list[tuple[str, int, int]] = []
    i = 0
    length = len(text)
    while i < length:
        names = index.get(text[i])
        if not names:
            i += 1
            continue
        matched = ""
        for name in names:
            if text.startswith(name, i):
                matched = name
                break
        if not matched:
            i += 1
            continue
        hits.append((matched, i, i + len(matched)))
        i += len(matched)
    return hits


def parse_llm_payload(raw: str, lexicon: Lexicon) -> list[Mention]:
    """只接受词典里存在的名称或代码，丢掉模型编出来的标的。"""
    text = (raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    stocks: list[StructuredStock] = []
    for item in payload.get("stocks") or []:
        if isinstance(item, str):
            stocks.append(StructuredStock(name=item))
        elif isinstance(item, dict):
            stocks.append(StructuredStock(
                name=str(item.get("name") or ""),
                code=str(item.get("code") or item.get("symbol") or ""),
            ))
    sectors = [str(item) for item in (payload.get("sectors") or []) if str(item).strip()]
    mentions = lexicon.extract("", stocks, sectors)
    return [item for item in mentions if item.origin == "structured" and _known(item, lexicon)]


def _known(mention: Mention, lexicon: Lexicon) -> bool:
    if mention.kind == "sector":
        return any(mention.key == name for names in lexicon._sectors.values() for name in names)
    code = mention.code or code6_of(mention.key)
    return bool(code and code in lexicon.by_code)
