"""JSON 出口的非有限数消毒 —— 全仓唯一实现。

## 为什么要有这一层

polars 除零、空集 ``mean()``、``close / prev - 1``、pandas 的零波动 sharpe 都能产出
``inf`` / ``-inf`` / ``NaN``。这些东西往下走有两条路, 两条都出事:

1. **默认 ``JSONResponse``**: Starlette 用 ``json.dumps(..., allow_nan=False)`` 渲染,
   碰到一个非有限浮点就抛 ``ValueError`` —— 不是那一行变成 null, 而是**整段响应 500**。
2. **手写 ``json.dumps``**: 默认 ``allow_nan=True``, 会输出 ``NaN`` / ``Infinity``
   字面量。那不是合法 JSON, 前端 ``JSON.parse`` 直接抛异常, 而且 Python 自己都
   ``json.loads`` 不回去。

两种症状都不该由调用方逐个记得处理 —— 那是「谁来兜底」的经典漏网。本模块在**响应层**
统一收口: ``SafeJSONResponse.render`` 先过 :func:`json_safe`, 再以 ``allow_nan=False``
渲染。于是任何走默认响应类的路由都不可能吐出非有限数, 也不因此 500。

## 语义

- ``float`` / ``numpy.floating`` 非有限 → ``None`` (JSON ``null``); 有限值原样保留。
- ``coerce=True`` 时额外: ``datetime``/``date``/``time`` → ``isoformat()``,
  ``Path`` → ``str()``, 其它未知类型 → ``str()``。
- ``dict`` / ``Mapping`` / ``list`` / ``tuple`` / ``set`` 递归。
- ``numpy`` 标量 → 先 ``.item()`` 转原生再递归 (``np.int64`` 不是 ``int`` 子类,
  ``json.dumps`` 认不出来)。
- 其它类型**原样返回** (转字符串由上层编码器/`default=` 负责, 见函数内注释)。

比「只判 float + dict/list」的老实现多出来的部分是**刻意**的: 响应里混进
``np.float32`` 时, 只判 ``isinstance(x, float)`` 会漏掉 (``np.float64`` 倒是 ``float``
子类), 然后 ``allow_nan=False`` 照样 500。统一按最宽语义处理, 顺带把原先散在
``api/backtest.py`` / ``services/backtest.py`` / ``factor_backtest_runs.py`` 里各写一遍的
numpy 分支收回来。

## 不覆盖的范围 (follow-up)

- SSE / ``StreamingResponse`` 里手写的 ``json.dumps``: 它们不经过本模块的
  ``render``, 仍需调用方自己消毒 (见 ``api/backtest.py`` 的 job 推送、``api/events.py``)。
- 直接 ``Response(content=...)`` 的二进制路径 (gzip kline 等): 由各自已有实现负责,
  且 ``api/kline.py`` 的 gzip 分支早已 ``_json_safe`` + ``allow_nan=False``。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse

__all__ = ["SafeJSONResponse", "json_safe"]


def json_safe(value: Any, *, coerce: bool = False) -> Any:
    """递归把非有限浮点换成 ``None``; ``coerce=True`` 时顺带收成可序列化形态。

    纯函数, 不改入参 (构造新容器)。幂等: ``json_safe(json_safe(x)) == json_safe(x)``。

    为什么有 ``coerce`` 这个开关 —— 两个调用场景的**契约要求正相反**, 强行统一会
    悄悄改 API:

    - ``coerce=False`` (默认, 给**响应层**和 ``api/kline.py`` 用): 只清非有限数,
      ``datetime`` / ``Path`` / 未知类型**原样返回**。因为这两处的返回值后面还要过
      FastAPI 的 ``jsonable_encoder`` 再编码一次; 这里先转成字符串, 分钟K 的
      ``datetime`` 字段就会从 ``datetime`` 变成 ``"2026-01-15T09:31:00"`` 字符串,
      直接改契约 (``tests/test_minute_routing.py`` 就是这么挂的)。
    - ``coerce=True`` (给 ``services/backtest.py`` 用): 这些地方是**最后一道**,
      返回值直接进 ``json.dumps``, 没人再帮忙编码 —— 于是必须把 ``datetime`` 转
      ``isoformat()``、numpy 标量转原生。原先本地实现的语义 (被
      ``tests/test_backtest_stats_json_safe.py`` 钉着: ``_json_safe(date) == "2026-01-02"``)
      就是这个, 收敛时必须原样保住。
    """
    if coerce:
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
    # 非有限浮点必须最先拦: np.float64 是 float 的子类, 走下面任何分支都已经晚了。
    if isinstance(value, float):
        return value if math.isfinite(value) else None

    # None / bool / int 无需处理。bool 必须在 int 之前判定? 不需要 —— 这里原样返回,
    # json.dumps 对 bool 的处理本来就是正确的。
    if value is None or isinstance(value, (str, bool, int)):
        return value


    # numpy 标量: np.int64 / np.float32 / np.bool_ 都不是对应的 Python 内建类型子类,
    # json.dumps 直接 TypeError。先 .item() 拽回原生再递归 (np.float32 的 inf 就能
    # 在递归第一行走 float 分支被换成 None)。
    module = type(value).__module__
    if module == "numpy" or module.startswith("numpy."):
        item = getattr(value, "item", None)
        if callable(item):
            try:
                return json_safe(item(), coerce=coerce)
            except (TypeError, ValueError):  # 形态不一的 numpy 对象, 退回字符串
                return str(value)
        return str(value)

    if isinstance(value, Mapping):
        return {str(key): json_safe(item, coerce=coerce) for key, item in value.items()}

    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item, coerce=coerce) for item in value]

    # 非 coerce 模式原样返回: 本模块的职责是「清非有限数」, 不是「决定序列化形态」。
    # 真到 json.dumps 那一步不认识的类型由调用方的 `default=` 兜底; 响应层则由
    # FastAPI 的 jsonable_encoder 先处理 (它认识 datetime/Path/ORM 等)。
    # coerce 模式没人兜底, 按老语义退成字符串。
    return str(value) if coerce else value


def _default_encoder(value: Any) -> Any:
    """``json.dumps`` 兜底: 先试 ``isoformat``, 再不认识就转字符串。

    与 ``api/kline.py`` gzip 分支里那个 ``default`` lambda 同款语义, 保持在实现层
    一处维护。非有限数在这之前已被 :func:`json_safe` 清掉, 不会走到这里。
    """
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


# --------------------------------------------------------------------------- #
# 默认响应类
# --------------------------------------------------------------------------- #
class SafeJSONResponse(JSONResponse):
    """默认响应类: 渲染前先消毒, 永久关掉 ``allow_nan``。

    与 ``JSONResponse`` 的差别只有一处 —— ``render`` 里多一行 :func:`json_safe`。
    ``allow_nan=False`` 保持 Starlette 原值 (它本来就是 False), 这里显式写出来是为了
    让「本层绝不放非有限数出门」这件事在代码里可读, 不依赖上游默认值。

    之所以放在**响应类**而不是中间件: 中间件要先把 body 读出来再重新序列化, 等于
    每个响应多一次 parse + dumps (kline 批量响应几 MB, 代价不小), 而且会破坏
    ``StreamingResponse`` / ``FileResponse`` 的流式语义。响应类只在真正的 JSON 路径上
    生效, 零额外开销。
    """

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(
            json_safe(content),
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
            # 补一层 default: 走 FastAPI 路径时 jsonable_encoder 已经把
            # datetime/Path 之类转好了, 但**直接构造** SafeJSONResponse 的场景
            # (测试、手工拼响应) 没有那一层, 不给 default 就是 TypeError。
            # 放在这里等于给两条路径同一条兜底, 且不改 json_safe 的契约。
            default=_default_encoder,
        ).encode("utf-8")
