"""默认 JSON 响应层: 非有限数一律转 null, 且不因非有限数整段 500。

## 为什么值得单独一层测

polars 除零 / 空集 ``mean()`` / ``close/prev - 1`` / pandas 零波动 sharpe 都能产出
``inf`` / ``NaN``。它们走到 Starlette 的 ``JSONResponse`` 时, 那里用
``json.dumps(allow_nan=False)`` 渲染 —— 只要有一个非有限浮点, 抛的是 ``ValueError``,
**整个响应变成 500**, 而不是那一行变 null。用户看到的是「整个页面挂了」,
日志里只有一句 ``Out of range float values are not JSON compliant``。

修复思路是「在出口收口」而不是「在每个调用点补判空」: ``app.main.app`` 的
``default_response_class`` 换成 ``SafeJSONResponse``, 它的 ``render`` 先过
``json_safe`` 再 ``allow_nan=False``。于是任何走默认响应类的路由都不可能吐出
``NaN`` / ``Infinity``, 无论上游有没有记得判。

本文件测三层:
1. ``json_safe`` 纯函数语义 (含 numpy 标量、嵌套、幂等)。
2. ``SafeJSONResponse.render`` 的输出字节合法。
3. 真实 ``TestClient`` 打一条会吐 float 的路由 —— 端到端证明「不 500」。
"""
from __future__ import annotations

import json
import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.json_safe import SafeJSONResponse, json_safe

INF = float("inf")
NINF = float("-inf")
NAN = float("nan")
NON_FINITE = (INF, NINF, NAN)


# --------------------------------------------------------------------------- #
# 1. json_safe 纯函数
# --------------------------------------------------------------------------- #

def test_float_non_finite_becomes_none() -> None:
    for v in NON_FINITE:
        assert json_safe(v) is None


def test_float_finite_kept() -> None:
    for v in (0.0, -0.0, 1.5, -1.5, 1e300, -1e-300):
        out = json_safe(v)
        assert out == v
        assert math.isfinite(out)


def test_nested_structures_recursed() -> None:
    payload = {
        "a": INF,
        "b": [1.0, NAN, {"c": NINF}],
        "d": (2.0, INF),
        "e": {"f": {"g": NAN}},
        "ok": "text",
        "n": 3,
        "t": True,
        "nil": None,
    }
    out = json_safe(payload)
    assert out == {
        "a": None,
        "b": [1.0, None, {"c": None}],
        "d": [2.0, None],
        "e": {"f": {"g": None}},
        "ok": "text",
        "n": 3,
        "t": True,
        "nil": None,
    }


def test_output_is_always_valid_json() -> None:
    """消毒后的对象必须能被严格 JSON 解析 (allow_nan=False)。"""
    payload = {"x": INF, "y": [NAN, NINF, 1.0], "z": {"deep": NAN}}
    text = json.dumps(json_safe(payload), allow_nan=False)
    assert json.loads(text) == {"x": None, "y": [None, None, 1.0], "z": {"deep": None}}


def test_does_not_mutate_input() -> None:
    src = {"a": INF, "b": [NAN]}
    snapshot = {"a": INF, "b": [NAN]}
    json_safe(src)
    # 原对象保持原样 (仍是 inf/nan), 没有被就地改写
    assert src["a"] == INF
    assert math.isnan(src["b"][0])
    assert json_safe(snapshot) == {"a": None, "b": [None]}


def test_idempotent() -> None:
    payload = {"a": INF, "b": [NAN, 1.0], "c": {"d": NINF}}
    once = json_safe(payload)
    assert json_safe(once) == once


def test_numpy_scalars_are_converted() -> None:
    """np.float32 不是 float 子类, 只判 `isinstance(x, float)` 会漏掉它。

    漏掉的后果不是显示难看, 而是 `allow_nan=False` 直接抛 —— 整段 500。
    """
    np = pytest.importorskip("numpy")
    assert json_safe(np.float64(INF)) is None
    assert json_safe(np.float32(NAN)) is None
    assert json_safe(np.float32(1.5)) == pytest.approx(1.5)
    # 整数标量转成原生 int (np.int64 不是 int 子类, json.dumps 认不出)
    out = json_safe(np.int64(7))
    assert out == 7
    assert isinstance(out, int)


def test_datetime_and_path_pass_through_untouched() -> None:
    """本函数**不**把 datetime/Path 转字符串 —— 转不转是上层编码器的事。

    这是踩过的坑: 早先版本在这里 `datetime → isoformat()`, 结果 `api/kline.py` 的
    未压缩分支 (把本函数返回值直接当响应体交回 FastAPI 再编码) 中, 分钟K 的
    `datetime` 字段悄悄变成了 `"2026-01-15T09:31:00"` 字符串, 直接改了 API 契约,
    `tests/test_minute_routing.py` 立刻挂。函数职责只是「清非有限数」。
    """
    from datetime import date, datetime
    from pathlib import Path

    for v in (date(2026, 10, 23), datetime(2026, 10, 23, 9, 30), Path("/tmp/x.parquet")):
        assert json_safe(v) is v


def test_unknown_type_passes_through_untouched() -> None:
    """未知类型原样返回, 不做 `str()` 偷偷改形态。"""
    class Weird:
        def __str__(self) -> str:
            return "weird"

    v = Weird()
    assert json_safe(v) is v


def test_datetime_still_serialises_through_response_layer() -> None:
    """虽然本函数不转, 响应层最终仍然产出合法 JSON —— 由 json.dumps 的 default 兜底。"""
    from datetime import datetime

    body = SafeJSONResponse({"t": datetime(2026, 10, 23, 9, 30), "x": INF}).body
    assert json.loads(body) == {"t": "2026-10-23T09:30:00", "x": None}


# --------------------------------------------------------------------------- #
# 2. SafeJSONResponse.render
# --------------------------------------------------------------------------- #

def test_render_never_emits_non_finite_literals() -> None:
    resp = SafeJSONResponse({"a": INF, "b": [NAN, NINF], "c": 1.0})
    body = resp.body
    # 原始字节里不能出现这三个字面量 —— 它们不是合法 JSON, 前端 JSON.parse 会抛
    for token in (b"Infinity", b"NaN", b"-Infinity"):
        assert token not in body, body
    assert json.loads(body) == {"a": None, "b": [None, None], "c": 1.0}


def test_render_matches_plain_json_response_for_finite_payload() -> None:
    """有限 payload 的输出必须与原生 JSONResponse **逐字节相同** —— 否则前端缓存、
    快照类测试会因为多一层消毒而漂移。"""
    from starlette.responses import JSONResponse

    payload = {"a": 1, "b": [1.5, -2.25], "c": "中文", "d": None, "e": True}
    assert SafeJSONResponse(payload).body == JSONResponse(payload).body


# --------------------------------------------------------------------------- #
# 3. 端到端: 真实路由吐 inf 也不 500
# --------------------------------------------------------------------------- #

@pytest.fixture()
def app_with_inf_route() -> FastAPI:
    """复刻生产配置: 默认响应类 = SafeJSONResponse。"""
    app = FastAPI(default_response_class=SafeJSONResponse)

    @app.get("/boom")
    async def boom() -> dict:
        # 典型的 polars 产出: 除零得 inf, 空集 mean 得 nan
        return {"ratio": INF, "neg": NINF, "mean": NAN, "ok": 1.25}

    return app


def test_route_returning_inf_is_200_and_valid_json(app_with_inf_route: FastAPI) -> None:
    with TestClient(app_with_inf_route) as client:
        resp = client.get("/boom")
    assert resp.status_code == 200
    # 客户端解析成功本身就是「合法 JSON」的证据
    assert resp.json() == {"ratio": None, "neg": None, "mean": None, "ok": 1.25}
    for token in ("Infinity", "NaN"):
        assert token not in resp.text


def test_plain_json_response_would_have_500ed(app_with_inf_route) -> None:
    """反向对照: 不换默认响应类时, 同样的 data 会 500。

    这条不是为了测 Starlette, 而是把「为什么必须改默认响应类」钉在测试里 ——
    否则以后有人把 `default_response_class` 删掉, 只有正向用例会失败, 看起来像 bug。

    **必须放子进程跑**: FastAPI 的 `DefaultPlaceholder` 是进程级单例, 第一次被访问
    时就地物化成当时某个 app 传进来的响应类, 之后**同进程里所有**未显式指定
    `default_response_class` 的 `FastAPI()` 都跟着用它。本文件前面的用例建过
    `FastAPI(default_response_class=SafeJSONResponse)`, 于是同进程内再造一个「原生
    app」其实并不原生 —— 早先这个用例就因为这条假阳性返回 200, 是进程隔离才照出原形。
    """
    import subprocess
    import sys
    from pathlib import Path

    script = """import json
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()

@app.get("/boom")
async def boom():
    return {"ratio": float("inf")}

resp = TestClient(app, raise_server_exceptions=False).get("/boom")
print(json.dumps({"status": resp.status_code, "text": resp.text}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=180,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["status"] == 500, info
    assert "Internal Server Error" in info["text"]


def test_production_app_uses_safe_response_class() -> None:
    """守住生产 app 的配置本身, 防止被无意改回默认 JSONResponse。"""
    from app.main import app

    assert app.router.default_response_class is SafeJSONResponse


def test_production_openapi_route_still_renders() -> None:
    """换响应类不能把 openapi 之类的大 JSON 弄坏。"""
    import importlib

    import app.main as main_module

    main_module = importlib.reload(main_module)
    paths = {getattr(route, "path", None) for route in main_module.app.routes}
    assert "/api/openapi.json" in paths
    # 直接对路由函数求值, 不启动 lifespan (避免碰真实 data 目录)
    spec = main_module.app.openapi()
    assert json_safe(spec) is not None
    assert json.dumps(json_safe(spec), allow_nan=False)
