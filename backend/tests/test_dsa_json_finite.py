"""DSA 工具报文非有限数值：先复现 vendor 形态的非法 JSON，再验证钩子。

不导入 LiteLLM，不调用模型。vendor 文件保持原样；钩子包住两条热路径。
"""
from __future__ import annotations

import json
import logging
import math
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from app.custom.dsa import dsa_bootstrap, json_finite

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_ADAPTER = _ROOT / "vendor/daily_stock_analysis/src/agent/llm_adapter.py"
_PIN = "ce364e457aab288863a5707e7b3df79786ad07f2"


def _reject_nonfinite(token: str) -> float:
    raise ValueError(f"non-standard JSON constant: {token}")


def _strict_loads(raw: str):
    return json.loads(raw, parse_constant=_reject_nonfinite)


def _vendor_serialize(result):
    """与钉上 ``serialize_tool_result`` 同一形态，用来证明修复前会写出 NaN。"""
    if result is None:
        return json.dumps({"result": None})
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)
    if hasattr(result, "__dict__"):
        try:
            payload = {key: value for key, value in result.__dict__.items() if not key.startswith("_")}
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


class _VendorAdapter:
    """与钉上 ``_convert_messages`` 的两个 ``json.dumps`` 调用点同形。"""

    def _convert_messages(self, messages, *, target_model=None):
        converted = []
        for message in messages:
            if message["role"] == "tool":
                content = message["content"]
                converted.append({
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                })
            elif message["role"] == "assistant" and message.get("tool_calls"):
                converted.append({
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": [
                        {
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"]),
                            }
                        }
                        for call in message["tool_calls"]
                    ],
                })
            else:
                converted.append({"role": message["role"], "content": message["content"]})
        return converted


def _nested_payload():
    return {
        "price": float("nan"),
        "volume": float("inf"),
        "spread": float("-inf"),
        "history": [1.5, float("nan"), {"deep": float("inf")}],
        "label": "NaN",
        "count": 3,
        "numpy_price": np.float64("nan"),
        "numpy_volume": np.float64("inf"),
        "numpy_ok": np.float64(1.25),
        "numpy_count": np.int64(7),
    }


def test_vendor_shape_emits_nonstandard_json_before_the_hook() -> None:
    raw = _vendor_serialize({"price": float("nan"), "volume": float("inf")})

    with pytest.raises(ValueError, match="NaN|Infinity"):
        _strict_loads(raw)


def test_serialize_path_sanitizes_nested_and_numpy_without_mutating() -> None:
    payload = _nested_payload()
    history = payload["history"]
    inner = history[2]
    wrapped = json_finite._wrap_serialize(_vendor_serialize)

    raw = wrapped(payload)
    parsed = _strict_loads(raw)

    assert parsed["price"] is None
    assert parsed["volume"] is None
    assert parsed["spread"] is None
    assert parsed["history"] == [1.5, None, {"deep": None}]
    assert parsed["label"] == "NaN"
    assert parsed["count"] == 3
    assert parsed["numpy_price"] is None
    assert parsed["numpy_volume"] is None
    assert parsed["numpy_ok"] == 1.25
    assert parsed["numpy_count"] == 7
    assert math.isnan(payload["price"])
    assert math.isnan(float(payload["numpy_price"]))
    assert payload["history"] is history
    assert payload["history"][2] is inner
    assert math.isnan(payload["history"][1])


def test_serialize_path_keeps_plain_text_and_existing_strings() -> None:
    wrapped = json_finite._wrap_serialize(_vendor_serialize)
    prose = "请看说明里的 NaN / Infinity，这不是 JSON"
    already = '{"price": NaN}'

    assert wrapped(prose) == prose
    assert wrapped(already) == already
    assert wrapped(None) == '{"result": null}'


def test_convert_path_sanitizes_tool_content_and_arguments() -> None:
    payload = _nested_payload()
    arguments = {
        "threshold": float("nan"),
        "gains": {"day": float("inf"), "week": np.float64("-inf")},
        "series": [np.float64("nan"), 2.25],
        "name": "Infinity",
    }
    history = payload["history"]
    gains = arguments["gains"]
    original_convert = _VendorAdapter._convert_messages
    _VendorAdapter._convert_messages = json_finite._wrap_convert(original_convert)
    messages = [
        {"role": "tool", "tool_call_id": "call_1", "content": payload},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [{"id": "call_1", "name": "echo", "arguments": arguments}],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "普通文本 NaN"},
        {
            "role": "assistant",
            "content": "raw",
            "tool_calls": [{"id": "call_3", "name": "echo", "arguments": '{"ok": 1}'}],
        },
    ]

    try:
        converted = _VendorAdapter()._convert_messages(messages)
    finally:
        _VendorAdapter._convert_messages = original_convert
    content = _strict_loads(converted[0]["content"])
    args = _strict_loads(converted[1]["tool_calls"][0]["function"]["arguments"])

    assert content["numpy_ok"] == 1.25
    assert content["history"][1] is None
    assert args["threshold"] is None
    assert args["gains"] == {"day": None, "week": None}
    assert args["series"] == [None, 2.25]
    assert args["name"] == "Infinity"
    assert converted[2]["content"] == "普通文本 NaN"
    bare = _VendorAdapter()
    bare_args = bare._convert_messages([messages[3]])[0]["tool_calls"][0]["function"]["arguments"]
    assert converted[3]["tool_calls"][0]["function"]["arguments"] == bare_args
    assert math.isnan(payload["price"])
    assert payload["history"] is history
    assert math.isnan(arguments["threshold"])
    assert arguments["gains"] is gains
    assert math.isinf(arguments["gains"]["day"])


def test_object_result_sanitizes_public_fields_only() -> None:
    class Row:
        def __init__(self) -> None:
            self.price = float("nan")
            self.label = "NaN"
            self._secret = float("inf")

    row = Row()
    wrapped = json_finite._wrap_serialize(_vendor_serialize)
    parsed = _strict_loads(wrapped(row))

    assert parsed == {"price": None, "label": "NaN"}
    assert math.isnan(row.price)
    assert math.isinf(row._secret)


def _plant_dsa_modules(monkeypatch):
    adapter = ModuleType("src.agent.llm_adapter")
    adapter.LLMToolAdapter = _VendorAdapter  # type: ignore[attr-defined]
    execution = ModuleType("src.agent.tools.execution")
    execution.serialize_tool_result = _vendor_serialize  # type: ignore[attr-defined]
    runner = ModuleType("src.agent.runner")
    runner.serialize_tool_result = _vendor_serialize  # type: ignore[attr-defined]
    packages = {
        "src": ModuleType("src"),
        "src.agent": ModuleType("src.agent"),
        "src.agent.tools": ModuleType("src.agent.tools"),
        "src.agent.llm_adapter": adapter,
        "src.agent.tools.execution": execution,
        "src.agent.runner": runner,
    }
    for name, module in packages.items():
        if name.count(".") < 2:
            module.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, module)
    return adapter, execution, runner


def test_install_patches_both_paths_and_is_idempotent(monkeypatch, caplog) -> None:
    adapter, execution, runner = _plant_dsa_modules(monkeypatch)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.json_finite"):
        json_finite.install()
        wrapped_serialize = execution.serialize_tool_result
        wrapped_convert = adapter.LLMToolAdapter._convert_messages
        json_finite.install()

    assert execution.serialize_tool_result is wrapped_serialize
    assert adapter.LLMToolAdapter._convert_messages is wrapped_convert
    assert getattr(wrapped_serialize, "_tsp_json_finite", False)
    assert getattr(wrapped_convert, "_tsp_json_finite", False)
    assert runner.serialize_tool_result is wrapped_serialize
    parsed = _strict_loads(execution.serialize_tool_result({"price": np.float64("nan"), "ok": 1.5}))
    assert parsed == {"price": None, "ok": 1.5}
    assert any("已安装 DSA 非有限值 JSON 钩子" in record.getMessage() for record in caplog.records)


def test_install_does_not_claim_success_without_dsa(monkeypatch, caplog) -> None:
    for name in (
        "src",
        "src.agent",
        "src.agent.llm_adapter",
        "src.agent.tools",
        "src.agent.tools.execution",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.json_finite"):
        json_finite.install()

    messages = [record.getMessage() for record in caplog.records]
    assert "当前不是 DSA 进程，跳过非有限值 JSON 钩子" in messages
    assert not any("已安装 DSA 非有限值 JSON 钩子" in message for message in messages)


def test_bootstrap_loads_real_hook_without_pretending_when_dsa_is_absent(caplog, monkeypatch) -> None:
    for name in ("src", "src.agent", "src.agent.llm_adapter", "src.agent.tools", "src.agent.tools.execution"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    with caplog.at_level(logging.INFO):
        dsa_bootstrap._install_json_finite()

    messages = [record.getMessage() for record in caplog.records]
    assert "未找到 json_finite.py" not in "\n".join(messages)
    assert "当前不是 DSA 进程，跳过非有限值 JSON 钩子" in messages
    assert "已安装 DSA 非有限值 JSON 钩子" not in "\n".join(messages)


def test_vendor_sources_and_pin_stay_put() -> None:
    adapter = _VENDOR_ADAPTER.read_text(encoding="utf-8")
    catalog = (_ROOT / "backend/app/custom/dsa/catalog.py").read_text(encoding="utf-8")

    assert 'json.dumps(msg["content"])' in adapter
    assert 'json.dumps(tc["arguments"])' in adapter
    assert f'UPSTREAM_COMMIT = "{_PIN}"' in catalog
