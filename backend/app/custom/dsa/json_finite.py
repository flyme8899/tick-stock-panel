"""把 DSA 工具报文里的非有限数值收成严格 JSON。

sidecar 由 ``dsa_bootstrap`` 在 ``main.py`` 之前调用 ``install()``。
不改 ``vendor/daily_stock_analysis``，也不改钉住的上游提交。

两条热路径都要盖住：

* ``serialize_tool_result``：runner 先把工具结果变成字符串，再送进适配器。
* ``LLMToolAdapter._convert_messages``：非字符串的 tool content，以及
  字典形态的 ``tool_calls[].arguments``。

已经是字符串的内容原样通过，包括普通说明文字和字面量 ``NaN``。
不把这类文本再解析或重写成 JSON。
"""
from __future__ import annotations

import logging
import math
import sys
from typing import Any, Callable

logger = logging.getLogger("tsp.dsa.json_finite")

_MARKER = "_tsp_json_finite"
_REBIND_MODULES = ("src.agent.runner", "src.agent.tool_surface")


def _numpy():
    try:
        import numpy as np
    except ImportError:
        return None
    return np


def normalize_nonfinite(value: Any) -> Any:
    """递归复制容器，把非有限浮点换成 ``None``。不改传入对象。"""
    if isinstance(value, bool) or isinstance(value, str) or value is None:
        return value
    numpy = _numpy()
    if numpy is not None:
        if isinstance(value, numpy.floating):
            if not numpy.isfinite(value):
                return None
            return float(value)
        if isinstance(value, numpy.integer):
            return int(value)
        if isinstance(value, numpy.bool_):
            return bool(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {key: normalize_nonfinite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_nonfinite(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_nonfinite(item) for item in value)
    return value


def _wire_messages(messages: Any) -> Any:
    """给适配器一份副本。字符串 content / arguments 保持原样。"""
    if not isinstance(messages, list):
        return messages
    wired: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            wired.append(message)
            continue
        role = message.get("role")
        if role == "tool" and not isinstance(message.get("content"), str):
            copied = dict(message)
            copied["content"] = normalize_nonfinite(message.get("content"))
            wired.append(copied)
            continue
        calls = message.get("tool_calls")
        if role == "assistant" and isinstance(calls, list):
            replaced = False
            next_calls: list[Any] = []
            for call in calls:
                if (
                    isinstance(call, dict)
                    and "arguments" in call
                    and not isinstance(call.get("arguments"), str)
                ):
                    call = dict(call)
                    call["arguments"] = normalize_nonfinite(call.get("arguments"))
                    replaced = True
                next_calls.append(call)
            if replaced:
                copied = dict(message)
                copied["tool_calls"] = next_calls
                wired.append(copied)
                continue
        wired.append(message)
    return wired


def _public_view(result: Any) -> Any:
    """给 ``serialize_tool_result`` 一个只含已清洗公开属性的替身。"""

    class _View:
        pass

    view = _View()
    view.__dict__.update(
        {
            key: normalize_nonfinite(item)
            for key, item in vars(result).items()
            if not str(key).startswith("_")
        }
    )
    return view


def _wrap_convert(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(self: Any, messages: Any, *args: Any, **kwargs: Any) -> Any:
        return original(self, _wire_messages(messages), *args, **kwargs)

    setattr(wrapped, _MARKER, True)
    return wrapped


def _wrap_serialize(original: Callable[[Any], str]) -> Callable[[Any], str]:
    def wrapped(result: Any) -> str:
        if isinstance(result, str):
            return original(result)
        if isinstance(result, (dict, list)):
            return original(normalize_nonfinite(result))
        numpy = _numpy()
        if numpy is not None and isinstance(result, (numpy.floating, numpy.integer, numpy.bool_)):
            return original(normalize_nonfinite(result))
        if hasattr(result, "__dict__"):
            return original(_public_view(result))
        return original(result)

    setattr(wrapped, _MARKER, True)
    return wrapped


def _patch_method(owner: Any, name: str, wrapper: Callable[[Callable[..., Any]], Callable[..., Any]]) -> None:
    current = getattr(owner, name)
    if getattr(current, _MARKER, False):
        return
    setattr(owner, name, wrapper(current))


def install() -> None:
    """包住两条序列化入口。DSA 模块不在时只记日志，不假装已经装上。"""
    try:
        import src.agent.llm_adapter as adapter
        import src.agent.tools.execution as execution
    except ImportError:
        logger.info("当前不是 DSA 进程，跳过非有限值 JSON 钩子")
        return
    if not hasattr(adapter, "LLMToolAdapter") or not hasattr(execution, "serialize_tool_result"):
        logger.info("DSA 工具序列化入口不在，跳过非有限值 JSON 钩子")
        return
    _patch_method(adapter.LLMToolAdapter, "_convert_messages", _wrap_convert)
    _patch_method(execution, "serialize_tool_result", _wrap_serialize)
    wrapped = execution.serialize_tool_result
    for module_name in _REBIND_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        current = getattr(module, "serialize_tool_result", None)
        if current is None or getattr(current, _MARKER, False):
            continue
        setattr(module, "serialize_tool_result", wrapped)
    logger.info("已安装 DSA 非有限值 JSON 钩子")
