"""宿主机采集钉钉和知识星球。只读命令，结果写到 TSP data/news/inbox。

dws / zsxq-cli 的登录态在宿主机，不在容器里。本模块禁止任何发帖、评论、发送。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path

from app.market_time import cn_now
from app.news.collectors import parse_time, parse_zsxq_payload
from app.news.config import group_id, zsxq_groups

_READONLY = {
    ("dws", "auth", "status"),
    ("dws", "chat", "+chat-messages"),
    ("dws", "chat", "message", "list"),
    ("zsxq-cli", "auth", "status"),
    ("zsxq-cli", "group", "+topics"),
}
# 只放行取签名链接这一条 GET。不能按 api raw 前缀放行，否则 POST 也会通过。
_ZSXQ_FILE_PATH = re.compile(r"^/v2/files/[0-9A-Za-z_-]{1,64}/download_url$")
_logger = logging.getLogger(__name__)


class CommandRejectedError(RuntimeError):
    pass


def _zsxq_download_args(args: list[str]) -> bool:
    """zsxq-cli api raw --method GET --path /v2/files/{id}/download_url。多一个参数就拒绝。"""
    if tuple(args[:2]) != ("api", "raw"):
        return False
    method = ""
    path = ""
    index = 2
    while index < len(args):
        flag = args[index]
        if flag not in {"--method", "--path"} or index + 1 >= len(args):
            return False
        if flag == "--method":
            method = args[index + 1]
        else:
            path = args[index + 1]
        index += 2
    return method == "GET" and _ZSXQ_FILE_PATH.fullmatch(path) is not None


def assert_readonly(argv: list[str]) -> None:
    """只允许登录状态查询、拉消息，以及取附件下载地址。发帖、评论、发送一律拒绝。"""
    if len(argv) < 3:
        raise CommandRejectedError("命令为空")
    name = Path(argv[0]).name
    args = argv[1:]
    if name == "zsxq-cli" and _zsxq_download_args(args):
        return
    allowed = [item[1:] for item in _READONLY if item[0] == name]
    if not any(tuple(args[:len(expected)]) == expected for expected in allowed):
        raise CommandRejectedError(f"拒绝执行非只读命令: {name} {' '.join(args[:3])}")


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    home = Path.home() / ".local" / "bin" / name
    if home.is_file():
        return str(home)
    return None


def auth_state(code: int, stdout: str, stderr: str) -> str:
    blob = f"{stdout}\n{stderr}".lower()
    if code != 0:
        return "expired"
    for word in ("expired", "未登录", "not logged", "login required", "unauthorized", "invalid token", "请先登录"):
        if word in blob:
            return "expired"
    return "ok"


def send_dingtalk(webhook: str, secret: str, content: str, opener=None) -> None:
    """登录失效提醒。只发这一句短文本，不走热点推送的 markdown。"""
    if not webhook:
        return
    from app.news.dingtalk import post_payload, signed_url

    post_payload(
        signed_url(webhook, secret),
        {"msgtype": "text", "text": {"content": content}},
        opener=opener,
    )


def alert_expiry(data_dir: Path, source: str, detail: str, webhook: str, secret: str) -> None:
    """同一来源 6 小时内只提醒一次，正文不含采集内容。"""
    stamp_path = data_dir / "news" / "health" / f"alert-{source}.txt"
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if stamp_path.is_file():
        try:
            if now - float(stamp_path.read_text().strip()) < 6 * 3600:
                return
        except ValueError:
            pass
    label = "钉钉" if source == "dws" else "知识星球"
    send_dingtalk(webhook, secret, f"TSP 资讯采集：{label}登录已失效，请在宿主机重新登录。{detail[:80]}")
    stamp_path.write_text(str(now), encoding="utf-8")


def write_auth(data_dir: Path, source: str, state: str, detail: str) -> None:
    path = data_dir / "news" / "health" / f"{source}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"auth": state, "detail": detail[:200]}, ensure_ascii=False), encoding="utf-8")


def write_inbox(data_dir: Path, source: str, payload, *, suffix: str = "") -> Path:
    inbox = data_dir / "news" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    token = str(int(time.time() * 1000))
    extra = f"-{_cursor_name(suffix)}" if suffix else ""
    target = inbox / f"{source}-{token}{extra}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"source": source, "payload": payload}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target


def dws_argv(binary: str, group_id: str, start: str) -> list[str]:
    argv = [binary, "chat", "+chat-messages", "--group", group_id, "--start", start, "--page-all", "--format", "json"]
    assert_readonly(argv)
    return argv


def zsxq_argv(binary: str, group_id: str, end_time: str = "") -> list[str]:
    argv = [binary, "group", "+topics", "--group-id", group_id, "--json", "--limit", "30"]
    if end_time:
        argv.extend(["--end-time", end_time])
    assert_readonly(argv)
    return argv


def zsxq_file_argv(binary: str, file_id: str) -> list[str]:
    """取签名下载地址。file_id 限定字符集，避免把路径拼出白名单。"""
    if not _ZSXQ_FILE_PATH.fullmatch(f"/v2/files/{file_id}/download_url"):
        raise CommandRejectedError("附件编号无效")
    argv = [
        binary,
        "api",
        "raw",
        "--method",
        "GET",
        "--path",
        f"/v2/files/{file_id}/download_url",
    ]
    assert_readonly(argv)
    return argv


def collect_zsxq_pages(run, binary: str, group_id: str, *, since: str = "", max_pages: int = 8) -> list:
    """翻页直到没有新主题、早于 since，或页数用尽。since 为 ISO 时间，空则只拉最新几页。"""
    end_time = ""
    seen: set[str] = set()
    collected = []
    cutoff = parse_time(since) if since else None
    for _ in range(max_pages):
        proc = run(zsxq_argv(binary, group_id, end_time))
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "zsxq-cli 失败")[:200])
        payload = json.loads(proc.stdout)
        items, page = parse_zsxq_payload(payload)
        fresh = []
        for item in items:
            if item.source_id in seen:
                continue
            seen.add(item.source_id)
            if cutoff and item.published_at < cutoff:
                continue
            fresh.append(item)
        if not fresh:
            break
        collected.extend(fresh)
        if not page.get("has_more") or not page.get("next_end_time"):
            break
        if cutoff and items and min(item.published_at for item in items) < cutoff:
            break
        end_time = page["next_end_time"]
    return [item.raw | {
        "topic_id": item.source_id,
        "content": item.text,
        "title": item.title,
        "create_time": item.published_at.isoformat(),
        "owner": {"name": item.author},
        "images": [{"image_id": media} for media in item.media_ids],
    } for item in collected]


def enabled_from_env_and_prefs(source: str, data_dir: Path) -> bool:
    raw = os.environ.get(f"NEWS_{source.upper()}_ENABLED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    path = data_dir / "user_data" / "preferences.json"
    if not path.is_file():
        return False
    try:
        prefs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    row = (prefs.get("news_sources") or {}).get(source) or {}
    return bool(row.get("enabled"))


def dws_cursor_stamp(now=None) -> str:
    """游标回退两分钟。本轮拉取期间新到的消息，下一轮还能扫到，入库按 messageId 去重。"""
    moment = now or cn_now()
    return (moment - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")


def _cursor_name(source: str) -> str:
    """游标键可以是 zsxq:<gid>。冒号不能做 Windows 文件名。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", source).strip("-")
    return cleaned or "cursor"


def read_cursor(data_dir: Path, source: str, default: str) -> str:
    path = data_dir / "news" / "cursors" / f"{_cursor_name(source)}.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip() or default
    return default


def write_cursor(data_dir: Path, source: str, value: str) -> None:
    path = data_dir / "news" / "cursors" / f"{_cursor_name(source)}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def run_host(data_dir: Path, run, *, backfill_since: str = "") -> dict:
    """跑一轮宿主机采集。run(argv) -> 有 returncode/stdout/stderr 的对象。"""
    summary = {}
    webhook = os.environ.get("DINGTALK_WEBHOOK_URL", "")
    secret = os.environ.get("DINGTALK_SECRET", "")
    if enabled_from_env_and_prefs("dws", data_dir):
        summary["dws"] = _run_dws(data_dir, run, webhook, secret)
    if enabled_from_env_and_prefs("zsxq", data_dir):
        summary["zsxq"] = _run_zsxq(data_dir, run, webhook, secret, backfill_since)
    return summary


def _run_dws(data_dir: Path, run, webhook: str, secret: str) -> str:
    group = group_id("dws")
    if not group:
        write_auth(data_dir, "dws", "missing", "未配置群号")
        return "unconfigured"
    binary = which("dws")
    if not binary:
        write_auth(data_dir, "dws", "missing", "未找到 dws")
        return "missing"
    status_argv = [binary, "auth", "status"]
    assert_readonly(status_argv)
    status = run(status_argv)
    state = auth_state(status.returncode, status.stdout, status.stderr)
    write_auth(data_dir, "dws", state, status.stderr or status.stdout)
    if state != "ok":
        alert_expiry(data_dir, "dws", status.stderr or status.stdout, webhook, secret)
        return state
    start = read_cursor(data_dir, "dws", (cn_now() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"))
    proc = run(dws_argv(binary, group, start))
    if proc.returncode != 0:
        write_auth(data_dir, "dws", "ok", (proc.stderr or proc.stdout)[:200])
        return "fetch-failed"
    payload = json.loads(proc.stdout)
    write_inbox(data_dir, "dws", payload)
    write_cursor(data_dir, "dws", dws_cursor_stamp())
    return "ok"


def _run_zsxq(data_dir: Path, run, webhook: str, secret: str, backfill_since: str) -> str:
    groups = zsxq_groups()
    if not groups:
        write_auth(data_dir, "zsxq", "missing", "未配置星球号")
        return "unconfigured"
    binary = which("zsxq-cli")
    if not binary:
        write_auth(data_dir, "zsxq", "missing", "未找到 zsxq-cli")
        return "missing"
    status_argv = [binary, "auth", "status"]
    assert_readonly(status_argv)
    status = run(status_argv)
    state = auth_state(status.returncode, status.stdout, status.stderr)
    write_auth(data_dir, "zsxq", state, status.stderr or status.stdout)
    if state != "ok":
        alert_expiry(data_dir, "zsxq", status.stderr or status.stdout, webhook, secret)
        return state
    default_since = (cn_now() - timedelta(days=2)).isoformat()
    pages = 400 if backfill_since else 8
    errors: list[str] = []
    batched: list = []
    for gid, name in groups:
        # 游标按星球分开。一个星球推进不能把另一个星球的新帖滤掉。
        key = f"zsxq:{gid}"
        since = backfill_since or read_cursor(data_dir, key, default_since)
        try:
            topics = collect_zsxq_pages(run, binary, gid, since=since, max_pages=pages)
        except (RuntimeError, json.JSONDecodeError, OSError, ValueError) as exc:
            errors.append(str(exc)[:200])
            continue
        for topic in topics:
            topic["group_id"] = gid
            topic["group_name"] = name
        if topics:
            write_inbox(data_dir, "zsxq", topics, suffix=gid)
            batched.extend(topics)
        write_cursor(data_dir, key, cn_now().isoformat())
    # 文本游标已经推进。附件失败只留在自己的索引里，下轮再试，不把这一轮收成失败。
    _sync_zsxq_files(data_dir, batched, run, binary)
    if errors:
        write_auth(data_dir, "zsxq", "ok", errors[0])
        return "fetch-failed"
    return "ok"


def _sync_zsxq_files(data_dir: Path, topics: list, run, binary: str) -> None:
    try:
        from app.news.zsxq_files import sync_zsxq_files

        sync_zsxq_files(data_dir, topics, run, binary)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "://" in text or "zsxq.com" in text:
            text = "附件下载失败"
        _logger.warning("知识星球附件未完成: %s", text[:120])
