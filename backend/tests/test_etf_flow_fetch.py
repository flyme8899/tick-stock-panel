"""ETF领航者：DeepSeek Fetch 兜底（清洗链接、还原转义、解析表格、重试和用量）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.news import mp_fetch
from app.news.etf_flow import (
    ArticlePage,
    ArticleRef,
    ExtractFailedError,
    _ingest_ref,
    _public_error,
    _store_page,
    normalize_flow,
)
from app.news.extract import Lexicon
from app.news.mp_fetch import (
    FETCH_TIMEOUT,
    FetchFallbackError,
    fetch_article,
    parse_flow_markdown,
    sanitize_wechat_url,
    unescape_markdown,
)
from app.news.service import get_store, reset_store_for_tests

FIXTURES = Path(__file__).parent / "fixtures" / "etf_flow"
KEY = "sk-deepseek-test-key-123456"
LONG = (
    "https://mp.weixin.qq.com/s?__biz=MzkzMjYzNDk5NQ%3D%3D&mid=2247490079&idx=1"
    "&sn=2c4167ebf6a527f91b54fb5c3668ba60"
)
TITLE = "10月13日ETF基金申购和赎回"
PUBLISHED = datetime(2026, 10, 14, 7, 31, tzinfo=CN_TZ)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "image/jpeg"}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FetchClient:
    """DeepSeek fetch 按队列返回；视觉请求一律返回非表格内容；微信页按需报错。"""

    def __init__(self, replies, *, wechat_error: Exception | None = None):
        self.replies = list(replies)
        self.fetch_calls: list[tuple] = []
        self.vision_calls = 0
        self.wechat_error = wechat_error

    def post(self, url, headers=None, json=None, timeout=None):
        if url == mp_fetch.FETCH_URL:
            self.fetch_calls.append((json, headers, timeout))
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        self.vision_calls += 1
        return Resp({"choices": [{"message": {"content": "不是表格"}}]})

    def get(self, url, params=None, headers=None):
        if url.startswith("https://mp.weixin.qq.com/") and self.wechat_error is not None:
            raise self.wechat_error
        if url.startswith("https://mmbiz.qpic.cn/"):
            return Resp(text="img")
        raise AssertionError(url)

    def close(self):
        pass


@pytest.fixture
def fetch_env(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("VISION_AI_API_KEY", "vision-test-key")
    monkeypatch.setattr(settings, "vision_ai_api_key", "vision-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.setenv("ETF_FLOW_FETCH_FALLBACK", "true")
    monkeypatch.setattr(settings, "dingtalk_webhook_url", "")
    monkeypatch.setattr("app.news.service.get_lexicon", lambda repo=None: Lexicon([], []))
    monkeypatch.setattr(
        "app.news.service.cn_now", lambda: datetime(2026, 10, 14, 8, 30, tzinfo=CN_TZ)
    )
    return tmp_path


def _content(name: str) -> str:
    return unescape_markdown(_fixture(name)["content"])


def test_unescape_restores_markdown_characters():
    raw = r"\# 标题" "\n" r"\| 512480 \| 半导体ETF \| \-0\.23 \| 0\.96 \|"
    assert unescape_markdown(raw) == "# 标题\n| 512480 | 半导体ETF | -0.23 | 0.96 |"


def test_table_layout_parses_categories_and_etfs():
    parsed = parse_flow_markdown(_content("deepseek_fetch_table.json"))
    cats = {row["name"]: row for row in parsed["categories"]}
    assert list(cats)[:3] == ["金融地产", "医药", "高端制造"]
    assert len(cats) == 8
    assert cats["金融地产"] == {
        "name": "金融地产",
        "net_1d": 3.24,
        "net_5d": 12.38,
        "net_20d": 28.97,
    }
    # 20 日那一格混进了水印「公众号：E-55领航者」，不能读成 -55。
    assert cats["港股通"]["net_1d"] == -1.42
    assert cats["港股通"]["net_5d"] == -5.92
    assert cats["港股通"]["net_20d"] is None
    etfs = {row["code"]: row for row in parsed["etfs"]}
    assert etfs["512480"] == {
        "name": "半导体ETF",
        "code": "512480",
        "net_1d": 0.96,
        "net_5d": 14.42,
        "net_20d": 10.76,
    }
    assert etfs["515880"]["net_1d"] == -1.82
    # 高端制造表识别了两遍，只留一份。
    assert len(etfs) == len(parsed["etfs"])
    assert parsed["unit"] == "亿"
    extracted = normalize_flow(parsed)
    assert len(extracted["etfs"]) == 80
    assert extracted["overview"] == {"net_1d": None, "net_5d": None, "net_20d": None}


def test_vertical_layout_drops_rows_with_missing_cells():
    parsed = parse_flow_markdown(_content("deepseek_fetch_vertical.json"))
    etfs = {row["code"]: row for row in parsed["etfs"]}
    assert etfs["515220"] == {
        "name": "煤炭ETF",
        "code": "515220",
        "net_1d": 3.09,
        "net_5d": 10.09,
        "net_20d": 5.69,
    }
    assert etfs["159905"]["net_1d"] == 0.045
    # 只有 4 个、5 个数字的行对不上列，整行丢掉；结尾混进杂字的一行也丢掉。
    assert "159993" not in etfs
    assert "510230" not in etfs
    assert "513750" not in etfs
    cats = {row["name"]: row for row in parsed["categories"]}
    # 第二张表缺了商品一行，按当日数值对齐，商品只留当日。
    assert cats["商品"] == {"name": "商品", "net_1d": 1.42, "net_5d": None, "net_20d": None}
    assert cats["医药"] == {"name": "医药", "net_1d": 0.44, "net_5d": 4.76, "net_20d": -9.53}
    assert cats["红利"] == {"name": "红利", "net_1d": -4.76, "net_5d": -0.57, "net_20d": 20.07}


def test_sanitize_strips_junk_glued_to_sn():
    dirty = LONG + "ETF"
    assert sanitize_wechat_url(dirty) == LONG
    assert sanitize_wechat_url(LONG + "&chksm=abc&scene=27#rd") == LONG
    assert sanitize_wechat_url(LONG.replace("%3D%3D", "==")) == LONG
    assert sanitize_wechat_url("https://mp.weixin.qq.com/s/AbCdEfGhIj") == ""
    assert sanitize_wechat_url(LONG.replace("mp.weixin.qq.com", "evil.example")) == ""
    assert sanitize_wechat_url(LONG.replace("https://", "http://")) == ""
    assert sanitize_wechat_url("https://mp.weixin.qq.com/s?__biz=MzkzMjYzNDk5NQ%3D%3D&mid=1") == ""


def test_fetch_sends_clean_url_with_timeout_and_counts_usage(fetch_env):
    client = FetchClient([Resp(_fixture("deepseek_fetch_table.json"))])
    fetched = fetch_article(LONG + "ETF", client, data_dir=fetch_env)
    assert fetched["title"] == TITLE
    assert "| 512480 | 半导体ETF |" in fetched["content"]
    body, headers, timeout = client.fetch_calls[0]
    assert body == {"url": LONG}
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert timeout == FETCH_TIMEOUT == 60.0
    usage = mp_fetch.usage(fetch_env)
    assert usage["total"] == {"requests": 1, "ok": 1}


def test_fetch_retries_once_with_alternate_url_after_timeout(fetch_env):
    client = FetchClient([httpx.ReadTimeout("slow"), Resp(_fixture("deepseek_fetch_table.json"))])
    fetch_article(LONG, client, data_dir=fetch_env)
    assert len(client.fetch_calls) == 2
    assert client.fetch_calls[1][0]["url"].startswith(
        "https://mp.weixin.qq.com/s?__biz=MzkzMjYzNDk5NQ==&"
    )
    assert mp_fetch.usage(fetch_env)["total"]["requests"] == 2


def test_fetch_gives_up_after_one_retry_and_does_not_retry_4xx(fetch_env, caplog):
    caplog.set_level(logging.INFO)
    short = Resp({"title": TITLE, "content": "作者：ETF领航者"})
    client = FetchClient([short, short, short])
    with pytest.raises(FetchFallbackError, match="过短"):
        fetch_article(LONG, client, data_dir=fetch_env)
    assert len(client.fetch_calls) == 2
    denied = FetchClient([Resp({"error": "bad key"}, status=401), short])
    with pytest.raises(FetchFallbackError, match="401"):
        fetch_article(LONG, denied, data_dir=fetch_env)
    assert len(denied.fetch_calls) == 1
    usage = mp_fetch.usage(fetch_env)["total"]
    assert usage == {"requests": 3, "failed": 2}
    assert KEY not in caplog.text


def test_fetch_requires_key(fetch_env, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    with pytest.raises(FetchFallbackError, match="DEEPSEEK_API_KEY"):
        fetch_article(LONG, FetchClient([]), data_dir=fetch_env)


def _page(images):
    return ArticlePage(
        article_id="2c4167ebf6a527f91b54fb5c3668ba60",
        title=TITLE,
        url=LONG,
        published_at=PUBLISHED,
        images=images,
        channel="wechat",
    )


def test_vision_failure_falls_back_to_fetch_and_stores(fetch_env):
    client = FetchClient([Resp(_fixture("deepseek_fetch_table.json"))])
    saved = _store_page(client, _page(["https://mmbiz.qpic.cn/table1.jpg"]))
    assert saved["inserted"] == 1
    assert client.vision_calls >= 1
    row = (
        get_store()
        ._conn.execute("SELECT source_id, clean_text, raw_json FROM news_items")
        .fetchone()
    )
    assert row["source_id"] == "wechat:2c4167ebf6a527f91b54fb5c3668ba60"
    raw = json.loads(row["raw_json"])
    assert raw["via"] == "deepseek_fetch"
    assert raw["trade_date"] == "2026-10-13"
    cats = {item["name"]: item for item in raw["extracted"]["categories"]}
    assert cats["金融地产"]["net_1d"] == 3.24
    assert "金融地产" in row["clean_text"]
    assert KEY not in row["raw_json"]


def test_page_without_images_uses_fetch(fetch_env):
    client = FetchClient([Resp(_fixture("deepseek_fetch_vertical.json"))])
    page = ArticlePage(
        "1fa34c66c3aaeb6e1a0da44c3ddf7a4d",
        "10月15日ETF基金申购和赎回",
        "https://mp.weixin.qq.com/s?__biz=MzkzMjYzNDk5NQ%3D%3D&mid=2247490098&idx=1"
        "&sn=1fa34c66c3aaeb6e1a0da44c3ddf7a4d",
        datetime(2026, 10, 16, 7, 27, tzinfo=CN_TZ),
        [],
        "wechat",
    )
    saved = _store_page(client, page)
    assert saved["inserted"] == 1
    assert client.vision_calls == 0
    raw = json.loads(get_store()._conn.execute("SELECT raw_json FROM news_items").fetchone()[0])
    assert raw["via"] == "deepseek_fetch"
    assert raw["trade_date"] == "2026-10-15"


def test_flag_off_keeps_the_original_error(fetch_env, monkeypatch):
    monkeypatch.setenv("ETF_FLOW_FETCH_FALLBACK", "false")
    client = FetchClient([Resp(_fixture("deepseek_fetch_table.json"))])
    with pytest.raises(ExtractFailedError):
        _store_page(client, _page(["https://mmbiz.qpic.cn/table1.jpg"]))
    assert client.fetch_calls == []


def test_fetch_failure_keeps_the_original_error(fetch_env):
    short = Resp({"title": TITLE, "content": "作者：ETF领航者"})
    client = FetchClient([short, short])
    with pytest.raises(ExtractFailedError, match="视觉模型"):
        _store_page(client, _page(["https://mmbiz.qpic.cn/table1.jpg"]))
    assert len(client.fetch_calls) == 2
    assert get_store()._conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 0


def test_mismatched_fetch_title_is_rejected(fetch_env):
    other = dict(_fixture("deepseek_fetch_table.json"), title="10月9日ETF基金申购和赎回")
    client = FetchClient([Resp(other)])
    with pytest.raises(ExtractFailedError):
        _store_page(client, _page([]))


def test_wechat_page_error_falls_back_for_listed_long_link(fetch_env):
    client = FetchClient(
        [Resp(_fixture("deepseek_fetch_table.json"))],
        wechat_error=httpx.ConnectError("blocked"),
    )
    ref = ArticleRef("", TITLE, LONG + "ETF", PUBLISHED, "wechat")
    saved = _ingest_ref(client, ref)
    assert saved["inserted"] == 1
    assert client.fetch_calls[0][0] == {"url": LONG}
    row = get_store()._conn.execute("SELECT source_id, url FROM news_items").fetchone()
    assert row["source_id"] == "wechat:2c4167ebf6a527f91b54fb5c3668ba60"
    assert row["url"] == LONG


def test_netease_page_error_does_not_use_fetch(fetch_env):
    client = FetchClient([], wechat_error=httpx.ConnectError("blocked"))
    ref = ArticleRef(
        "L990T4N00556ADVD", TITLE, "https://www.163.com/dy/article/x.html", PUBLISHED, "netease"
    )
    with pytest.raises(AssertionError, match=r"163\.com"):
        _ingest_ref(client, ref)
    assert client.fetch_calls == []


def test_public_error_redacts_deepseek_key(fetch_env):
    assert KEY not in _public_error(RuntimeError(f"boom {KEY}"))


def test_cli_fetch_check_prints_rows_without_storing(fetch_env, monkeypatch, capsys):
    from app.news import etf_flow

    client = FetchClient([Resp(_fixture("deepseek_fetch_table.json"))])
    monkeypatch.setattr(etf_flow, "_page_client", lambda: client)
    code = etf_flow.main(["--data-dir", str(fetch_env), "--fetch-check", LONG + "ETF"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trade_date"] == "2026-10-13"
    assert out["categories"] == 8
    assert out["etfs"] == 80
    assert client.fetch_calls[0][0] == {"url": LONG}
    assert get_store()._conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 0
    assert mp_fetch.usage(fetch_env)["total"]["ok"] == 1
