"""钉钉自定义机器人加签和发送。

登录失效提醒与热点推送共用加签，正文各自组装，避免两种通知混在同一段文案里。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request


def signed_url(webhook: str, secret: str, *, timestamp_ms: int | None = None) -> str:
    """按钉钉加签规则在 webhook 后附上 timestamp 和 sign。没有密钥则原样返回。"""
    if not webhook or not secret:
        return webhook
    stamp = str(timestamp_ms if timestamp_ms is not None else round(time.time() * 1000))
    digest = hmac.new(secret.encode(), f"{stamp}\n{secret}".encode(), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={stamp}&sign={sign}"


def post_payload(url: str, payload: dict, opener=None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    call = opener or urllib.request.urlopen
    response = call(request, timeout=10)
    read = getattr(response, "read", None)
    if read is None:
        return
    raw = read()
    close = getattr(response, "close", None)
    if close:
        close()
    if not raw:
        return
    try:
        data = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(data, dict) and data.get("errcode") not in (0, None):
        raise RuntimeError(f"钉钉返回 {data.get('errcode')}")


def send_markdown(webhook: str, secret: str, title: str, text: str, opener=None, *, timestamp_ms: int | None = None) -> None:
    """发送 markdown。标题用于通知栏，正文第一行仍要写清类型。"""
    if not webhook:
        raise RuntimeError("未配置钉钉机器人")
    url = signed_url(webhook, secret, timestamp_ms=timestamp_ms)
    post_payload(url, {
        "msgtype": "markdown",
        "markdown": {"title": title[:64], "text": text},
    }, opener=opener)
