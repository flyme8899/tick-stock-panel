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


def test_top_level_numpy_scalar_stays_a_json_literal() -> None:
    """裸标量不能走进原函数的 None 分支，否则会变成 {"result": null}。"""
    wrapped = json_finite._wrap_serialize(_vendor_serialize)

    assert wrapped(np.float64("nan")) == "null"
    assert wrapped(np.float64("inf")) == "null"
    assert wrapped(np.float64(1.25)) == "1.25"
    assert wrapped(np.int64(7)) == "7"
    assert wrapped(None) == '{"result": null}'


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


def test_adapter_import_failure_still_patches_vendor_shaped_serialize(monkeypatch, caplog) -> None:
    _adapter, execution, _runner = _plant_dsa_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "src.agent.llm_adapter", None)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.json_finite"):
        json_finite.install()

    raw = execution.serialize_tool_result({"price": float("nan"), "label": "NaN"})
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert _strict_loads(raw) == {"price": None, "label": "NaN"}
    assert "DSA 适配器未导入，跳过 _convert_messages 清洗" in messages
    assert "已安装 DSA 非有限值 JSON 钩子" not in messages
    assert "当前不是 DSA 进程，跳过非有限值 JSON 钩子" not in messages


def test_pinned_vendor_serialize_tool_result_is_compatible(monkeypatch) -> None:
    """直接调用钉上的 serialize_tool_result，不经过手写仿品。"""
    vendor = str(_ROOT / "vendor/daily_stock_analysis")
    monkeypatch.syspath_prepend(vendor)
    stale = [name for name in sys.modules if name == "src" or name.startswith("src.")]
    for name in stale:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "src.agent.llm_adapter", None)

    import src.agent.tools.execution as execution

    payload = {
        "price": float("nan"),
        "volume": float("inf"),
        "label": "NaN",
        "ok": np.float64(1.25),
        "nested": [float("-inf"), {"deep": np.float64("nan")}],
    }
    nested = payload["nested"]
    before = execution.serialize_tool_result(payload)
    with pytest.raises(ValueError):
        _strict_loads(before)
    prose = "请看说明里的 NaN，不是 JSON"
    assert execution.serialize_tool_result(prose) == prose

    json_finite.install()
    raw = execution.serialize_tool_result(payload)

    assert getattr(execution.serialize_tool_result, "_tsp_json_finite", False)
    assert _strict_loads(raw) == {
        "price": None,
        "volume": None,
        "label": "NaN",
        "ok": 1.25,
        "nested": [None, {"deep": None}],
    }
    assert execution.serialize_tool_result(prose) == prose
    assert execution.serialize_tool_result('{"price": NaN}') == '{"price": NaN}'
    assert execution.serialize_tool_result(np.float64("nan")) == "null"
    assert execution.serialize_tool_result(np.float64(1.25)) == "1.25"
    assert execution.serialize_tool_result(None) == '{"result": null}'
    assert math.isnan(payload["price"])
    assert payload["nested"] is nested


def test_pinned_adapter_convert_messages_when_litellm_is_installed(monkeypatch) -> None:
    """钉上的 LLMToolAdapter._convert_messages。没有 litellm 时跳过，不假装跑过。"""
    pytest.importorskip("litellm")
    monkeypatch.setenv("ENV_FILE", "/tmp/empty-dsa.env")
    Path("/tmp/empty-dsa.env").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(_ROOT / "vendor/daily_stock_analysis"))
    for name in [key for key in sys.modules if key == "src" or key.startswith("src.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    import src.agent.llm_adapter as adapter_mod

    payload = {"price": float("nan"), "label": "NaN", "ok": np.float64(1.25)}
    bare = adapter_mod.LLMToolAdapter.__new__(adapter_mod.LLMToolAdapter)
    before = bare._convert_messages([
        {"role": "tool", "tool_call_id": "c1", "content": dict(payload)},
    ])
    with pytest.raises(ValueError):
        _strict_loads(before[0]["content"])

    json_finite.install()
    patched = adapter_mod.LLMToolAdapter.__new__(adapter_mod.LLMToolAdapter)
    converted = patched._convert_messages([
        {"role": "tool", "tool_call_id": "c1", "content": payload},
        {"role": "tool", "tool_call_id": "c2", "content": "普通文本 NaN"},
    ])

    assert _strict_loads(converted[0]["content"]) == {"price": None, "label": "NaN", "ok": 1.25}
    assert converted[1]["content"] == "普通文本 NaN"
    assert math.isnan(payload["price"])
    assert getattr(adapter_mod.LLMToolAdapter._convert_messages, "_tsp_json_finite", False)


def _block_dsa_imports(monkeypatch) -> None:
    """前面的用例可能已经导入钉上的 src。清掉缓存并拦住再导入。"""
    for name in [key for key in sys.modules if key == "src" or key.startswith("src.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = __import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name.startswith("src.agent.llm_adapter") or name.startswith("src.agent.tools"):
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", blocked)


def test_install_does_not_claim_success_without_dsa(monkeypatch, caplog) -> None:
    _block_dsa_imports(monkeypatch)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.json_finite"):
        json_finite.install()

    messages = [record.getMessage() for record in caplog.records]
    assert "当前不是 DSA 进程，跳过非有限值 JSON 钩子" in messages
    assert not any("已安装 DSA 非有限值 JSON 钩子" in message for message in messages)


def test_bootstrap_loads_real_hook_without_pretending_when_dsa_is_absent(caplog, monkeypatch) -> None:
    _block_dsa_imports(monkeypatch)

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
