"""DSA 上游钉的单一真源：四处必须写成同一个 40 位 commit。

``docs/dsa-upgrade.md`` 约定这四处是同一份钉的不同投影：

* ``backend/app/custom/dsa/catalog.py`` 的 ``UPSTREAM_COMMIT``（运行时真源）
* ``vendor/daily_stock_analysis/VENDOR.md``
* ``docs/dsa-integration.md`` 文首
* ``THIRD_PARTY_NOTICES.md``

只改一处（比如同步了快照忘了改许可说明）在运行时不报错，但溯源会断。这里用
正则从文件里抽钉，不求值 Python，也不改任何产品代码。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_SHA_RE = re.compile(r"[0-9a-f]{40}")
# 跟随上游滚动的浮动引用，写进钉里等于放弃「人工选定 commit」。
_FLOATING_REFS = {"latest", "main", "master", "head", "develop", "dev", "trunk"}

_PIN_PATTERNS = {
    "catalog.py": (
        "backend/app/custom/dsa/catalog.py",
        re.compile(r'^UPSTREAM_COMMIT\s*=\s*"([^"]+)"', re.MULTILINE),
    ),
    "VENDOR.md": (
        "vendor/daily_stock_analysis/VENDOR.md",
        re.compile(r"Upstream commit:\s*`([^`]+)`"),
    ),
    "dsa-integration.md": (
        "docs/dsa-integration.md",
        re.compile(r"提交\s*`([^`]+)`"),
    ),
    "THIRD_PARTY_NOTICES.md": (
        "THIRD_PARTY_NOTICES.md",
        re.compile(r"Upstream commit:\s*`([^`]+)`"),
    ),
}


def _read_pin(label: str, root: Path | None = None) -> str:
    relative, pattern = _PIN_PATTERNS[label]
    text = ((root or _ROOT) / relative).read_text(encoding="utf-8")
    found = pattern.search(text)
    assert found is not None, f"{relative} 里没找到钉，匹配式 {pattern.pattern!r}"
    return found.group(1).strip()


@pytest.mark.parametrize("label", sorted(_PIN_PATTERNS))
def test_each_pin_is_full_sha(label: str) -> None:
    pin = _read_pin(label)

    assert _SHA_RE.fullmatch(pin), f"{label} 的钉不是 40 位小写十六进制：{pin!r}"
    assert pin.lower() not in _FLOATING_REFS


def test_all_four_pins_are_identical() -> None:
    pins = {label: _read_pin(label) for label in _PIN_PATTERNS}

    assert len(set(pins.values())) == 1, f"四处钉不一致：{pins}"


def test_catalog_constant_matches_documents() -> None:
    """运行时真源与三处文档钉逐字一致（防止只改文档不改常量）。"""
    runtime = _read_pin("catalog.py")
    documents = {label: _read_pin(label) for label in _PIN_PATTERNS if label != "catalog.py"}

    for label, pin in documents.items():
        assert pin == runtime, f"{label} 的钉 {pin} 与 catalog.py 的 {runtime} 不一致"


def test_catalog_payload_exposes_the_pin() -> None:
    """``catalog_payload()`` 要把钉透出去，前端和自检读的是同一份。"""
    import sys

    backend = _ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from app.custom.dsa import catalog  # noqa: PLC0415

    assert catalog.catalog_payload()["upstream_commit"] == catalog.UPSTREAM_COMMIT
    assert catalog.UPSTREAM_COMMIT == _read_pin("catalog.py")


def _render_pin(label: str, pin: str) -> str:
    """按各文件自己的写法渲染一条钉，供临时目录复现用。"""
    if label in {"VENDOR.md", "THIRD_PARTY_NOTICES.md"}:
        return f"- Upstream commit: `{pin}`\n"
    if label == "dsa-integration.md":
        return f"快照来自上游提交 `{pin}`，作为旁路服务运行。\n"
    return f'UPSTREAM_COMMIT = "{pin}"\n'


def test_inconsistent_pin_is_detected(tmp_path: Path) -> None:
    """把其中一份改成别的 commit，比对必须发现不一致（守住上面的断言）。"""
    for label, (relative, _) in _PIN_PATTERNS.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_pin(label, _read_pin(label)), encoding="utf-8")

    tampered = tmp_path / _PIN_PATTERNS["VENDOR.md"][0]
    tampered.write_text(_render_pin("VENDOR.md", "0" * 40), encoding="utf-8")

    pins = {label: _read_pin(label, tmp_path) for label in _PIN_PATTERNS}

    assert len(set(pins.values())) == 2, pins


def test_floating_pin_is_rejected() -> None:
    """``latest`` / ``main`` 这类浮动引用不满足 40 位十六进制。"""
    for value in ("latest", "main", "HEAD", "ce364e4"):
        assert not _SHA_RE.fullmatch(value), value
