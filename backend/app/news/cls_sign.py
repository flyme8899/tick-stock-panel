"""财联社电报 sign。

与 RSSHub `lib/routes/cls/utils.ts` 一致：参数按键名排序拼成 query，
sign = md5(sha1(query))，sign 本身不参与计算。ASCII 参数不需要再 urlencode。
"""
from __future__ import annotations

import hashlib


def sign_query(params: dict[str, str]) -> str:
    query = "&".join(f"{key}={params[key]}" for key in sorted(params))
    sha1 = hashlib.sha1(query.encode("utf-8")).hexdigest()
    return hashlib.md5(sha1.encode("utf-8")).hexdigest()


CLS_BASE_PARAMS = {
    "appName": "CailianpressWeb",
    "os": "web",
    "sv": "8.7.9",
}


def cls_query(extra: dict[str, str] | None = None) -> dict[str, str]:
    merged = {**CLS_BASE_PARAMS}
    for key, value in (extra or {}).items():
        if value is None or value == "":
            continue
        merged[key] = str(value)
    signed = dict(merged)
    signed["sign"] = sign_query(merged)
    return signed
