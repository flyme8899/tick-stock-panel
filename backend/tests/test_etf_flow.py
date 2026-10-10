"""ETF领航者：网易列表、搜狗备份、视觉抽取和交易日早晨窗口。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.news.config import (
    source_configured,
    source_enabled,
    vision_base_url,
    vision_generation,
    vision_model,
)
from app.news.etf_flow import (
    ArticleRef,
    ExtractFailedError,
    _call_vision,
    _fetch,
    _public_error,
    apply_trade_date,
    choose_flow_article,
    collect_etf_flow,
    day_was_trading,
    etf_wait_seconds,
    expects_publication,
    flow_mentions,
    is_check_morning,
    normalize_flow,
    parse_netease_article,
    parse_netease_media,
    parse_sogou_account,
    parse_vision_response,
    sogou_begin,
    trade_date_from_title,
    vision_payload,
)
from app.news.extract import Lexicon
from app.news.service import get_store, health_payload, hot_candidates, reset_store_for_tests

FIXTURES = Path(__file__).parent / "fixtures" / "etf_flow"
SATURDAY = datetime(2026, 10, 10, 7, 40, tzinfo=CN_TZ)
DEADLINE = datetime(2026, 10, 10, 9, 0, tzinfo=CN_TZ)
MONDAY = datetime(2026, 10, 12, 7, 40, tzinfo=CN_TZ)
SUNDAY = datetime(2026, 10, 11, 8, 0, tzinfo=CN_TZ)
WECHAT_FINAL = "https://mp.weixin.qq.com/s?__biz=abc&mid=9&idx=1&sn=a1b2c3d4"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _vision() -> dict:
    return json.loads(_text("vision_ok.json"))


class Resp:
    def __init__(self, text, url, status=200, headers=None, payload=None):
        self.text = text
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class Client:
    def __init__(
        self,
        *,
        media: str,
        article: str = "",
        vision: dict | None = None,
        sogou: str = "",
        wechat: str = "",
    ):
        self.media = media
        self.article = article
        self.vision = vision if vision is not None else _vision()
        self.sogou = sogou
        self.wechat = wechat
        self.gets: list[str] = []
        self.posts: list[tuple] = []

    def get(self, url, params=None, headers=None):
        self.gets.append(url)
        if url.startswith("https://www.163.com/dy/media/"):
            return Resp(self.media, url)
        if url.startswith("https://www.163.com/dy/article/"):
            return Resp(self.article, url)
        if url.startswith("https://weixin.sogou.com/weixin"):
            return Resp(self.sogou, url)
        if url.startswith("https://weixin.sogou.com/link"):
            return Resp("", url, status=302, headers={"location": WECHAT_FINAL})
        if url.startswith("https://mp.weixin.qq.com/"):
            return Resp(self.wechat, url)
        raise AssertionError(url)

    def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        return Resp("", url, payload=self.vision)


@pytest.fixture
def news_db(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setenv("VISION_AI_API_KEY", "vision-test-key")
    monkeypatch.delenv("VISION_AI_BASE_URL", raising=False)
    monkeypatch.delenv("VISION_AI_MODEL", raising=False)
    monkeypatch.setattr(settings, "vision_ai_api_key", "vision-test-key")
    monkeypatch.setattr(settings, "vision_ai_base_url", "https://tokenhub.tencentmaas.com/v1")
    monkeypatch.setattr(settings, "vision_ai_model", "deepseek/deepseek-v4-flash-vision-exp")
    monkeypatch.setattr(settings, "ai_api_key", "deepseek-secret")
    monkeypatch.setattr(settings, "ai_model", "deepseek-chat")
    monkeypatch.setattr(settings, "ai_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "dingtalk_webhook_url", "")
    monkeypatch.setattr(settings, "dingtalk_secret", "")
    monkeypatch.setattr("app.news.service.get_lexicon", lambda repo=None: Lexicon([], []))
    monkeypatch.setattr(
        "app.news.service.cn_now", lambda: datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ)
    )
    return tmp_path


def _collect(client, tmp_path, now, **flags):
    return collect_etf_flow(client, now=now, data_dir=tmp_path, **flags)


def test_parsers_keep_flow_article_and_drop_foreign_images():
    refs = parse_netease_media(_text("netease_media.html"))
    chosen, reason = choose_flow_article(refs, day=date(2026, 10, 10), expect=True)
    assert reason == "today"
    assert chosen is not None
    assert chosen.article_id == "L8S4S48P0556ADVD"
    assert chosen.url == "https://www.163.com/dy/article/L8S4S48P0556ADVD.html"
    page = parse_netease_article(_text("netease_article.html"), chosen)
    assert page.images == ["https://nimg.ws.126.net/a.jpg", "https://nimg.ws.126.net/b.jpg"]

    sogou = parse_sogou_account(_text("sogou_account.html"), SATURDAY)
    assert [item.url for item in sogou] == [
        "https://weixin.sogou.com/link?url=article1",
        "https://weixin.sogou.com/link?url=old",
    ]
    today, why = choose_flow_article(sogou, day=date(2026, 10, 10), expect=True)
    assert why == "today"
    assert today is not None
    assert "article1" in today.url


def test_vision_response_normalizes_numbers_and_title_date_wins():
    parsed = parse_vision_response(_vision())
    assert parsed["overview"]["net_1d"] == 12.5
    assert parsed["overview"]["net_20d"] == -4.25
    assert parsed["unit"] == "亿元"
    assert parsed["trade_date"] == "1999-01-01"
    published = datetime(2026, 10, 10, 7, 38, tzinfo=CN_TZ)
    assert trade_date_from_title("10月9日ETF基金申购和赎回", published) == "2026-10-09"
    assert (
        trade_date_from_title(
            "12月31日ETF基金申购和赎回", datetime(2026, 1, 2, 7, 30, tzinfo=CN_TZ)
        )
        == "2025-12-31"
    )
    assert trade_date_from_title("2月31日ETF基金申购和赎回", published) == ""
    assert normalize_flow({"etfs": [{"name": "甲", "code": "1234567", "net_1d": "赎回2.5"}]})[
        "etfs"
    ] == [
        {"name": "甲", "code": "", "net_1d": -2.5, "net_5d": None, "net_20d": None},
    ]
    aliased = normalize_flow(
        {
            "broad_etfs": [
                {"name": "宽基甲", "code": "510300", "day": 1.25, "d5": "2", "d20": -0.5},
                {"name": "坏代码", "code": "51030", "day": 3},
                {"name": "七位", "code": "5103001", "day": 4},
            ]
        }
    )
    assert aliased["etfs"][0] == {
        "name": "宽基甲",
        "code": "510300",
        "net_1d": 1.25,
        "net_5d": 2.0,
        "net_20d": -0.5,
    }
    assert [row["code"] for row in aliased["etfs"]] == ["510300", "", ""]
    conflicted = normalize_flow(
        {"etfs": [{"name": "甲", "code": "510300", "net_1d": 1, "day": -1, "net_5d": 2}]}
    )
    assert conflicted["etfs"] == [
        {"name": "甲", "code": "510300", "net_1d": None, "net_5d": 2.0, "net_20d": None},
    ]
    broad_etfs = [
        {"name": f"宽基{i:02d}", "code": f"510{i:03d}", "net_1d": float(i + 1)} for i in range(20)
    ]
    broad_etfs.append({"name": "沪深300", "code": "000300", "net_1d": 9})
    moved = normalize_flow({"broad_index": broad_etfs})
    assert moved["broad_index"] == [
        {"name": "沪深300", "code": "000300", "net_1d": 9.0, "net_5d": None, "net_20d": None}
    ]
    assert len(moved["etfs"]) == 20
    assert all(len(row["code"]) == 6 for row in moved["etfs"])
    ranked, _sectors = flow_mentions(moved)
    assert len(ranked) == 8
    assert ranked[0].code == "510019"
    with pytest.raises(ValueError, match="没有抽出"):
        normalize_flow({"overview": {"net_1d": None}, "etfs": []})
    stocks, sectors = flow_mentions(parsed)
    assert [item.code for item in stocks] == [
        "510050",
        "159915",
        "159901",
        "512000",
        "512100",
        "510330",
        "510310",
        "510500",
    ]
    assert "510300" not in [item.code for item in stocks]
    assert sectors[:2] == ["沪深300", "中证500"]
    assert "科技" in sectors


def test_vision_payload_uses_image_url_and_drops_other_hosts():
    body = vision_payload(
        ["https://nimg.ws.126.net/a.jpg", "https://evil.example/x.jpg", "http://127.0.0.1/secret"],
        model="glm-5.3-flash",
    )
    parts = body["messages"][0]["content"]
    urls = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
    assert urls == ["https://nimg.ws.126.net/a.jpg"]
    assert body["model"] == "glm-5.3-flash"
    assert body["max_tokens"] >= 8192
    assert "thinking" not in body
    assert "enable_thinking" not in body
    assert "reasoning_effort" not in body
    assert "deepseek" not in json.dumps(body)


def test_short_vision_tasks_disable_thinking_without_touching_ocr():
    ocr_models = (
        "deepseek/deepseek-v4-flash-vision-exp",
        "glm-5.3-flash",
        "mimo-v2.6-flash",
    )
    for model in ocr_models:
        body = vision_payload(["https://nimg.ws.126.net/a.jpg"], model=model)
        assert body["max_tokens"] >= 8192
        assert "thinking" not in body
        assert "reasoning_effort" not in body
        assert vision_generation(model, ocr=True) == {"max_tokens": body["max_tokens"]}
    deepseek = vision_generation("deepseek/deepseek-v4-flash-vision-exp", ocr=False)
    assert deepseek["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in deepseek
    assert deepseek["max_tokens"] < 8192
    glm = vision_generation("glm-5.3-flash", ocr=False)
    assert glm == {"max_tokens": deepseek["max_tokens"], "reasoning_effort": "low"}
    assert "thinking" not in glm
    mimo = vision_generation("mimo-v2.6-flash", ocr=False)
    assert mimo["thinking"] == {"type": "disabled"}
    unknown = vision_generation("other-vision", ocr=False)
    assert unknown["max_tokens"] >= 8192
    assert "thinking" not in unknown
    assert "reasoning_effort" not in unknown


def _completion(content) -> dict:
    return {"choices": [{"message": {"content": content, "reasoning_content": "思考"}}]}


def test_vision_call_batches_one_image_and_retries_empty_content(news_db):
    answers = [
        _completion(None),
        _vision(),
        _completion(
            json.dumps(
                {
                    "etfs": [
                        {"name": "宽基甲", "code": "510880", "day": 0.4, "d5": 1, "d20": -2},
                        {"name": "坏代码", "code": "1234567", "day": 9},
                    ]
                },
                ensure_ascii=False,
            )
        ),
        _completion(
            json.dumps(
                {
                    "etfs": [
                        {"name": "宽基甲", "code": "510880", "day": 0.4, "d5": 1, "d20": -2},
                        {"name": "坏代码", "code": "1234567", "day": 9},
                    ]
                },
                ensure_ascii=False,
            )
        ),
    ]

    class Seq:
        def __init__(self):
            self.posts = []

        def post(self, url, headers=None, json=None):
            self.posts.append((url, headers, json))
            return Resp("", url, payload=answers[len(self.posts) - 1])

    client = Seq()
    extracted = _call_vision(
        client,
        [
            "https://nimg.ws.126.net/a.jpg",
            "https://nimg.ws.126.net/b.jpg",
            "https://evil.example/skip.jpg",
        ],
    )
    assert len(client.posts) == 4
    sent = []
    for _url, _headers, body in client.posts:
        assert body["max_tokens"] >= 8192
        assert "thinking" not in body
        assert "enable_thinking" not in body
        assert "reasoning_effort" not in body
        urls = [
            part["image_url"]["url"]
            for part in body["messages"][0]["content"]
            if part["type"] == "image_url"
        ]
        assert urls
        sent.extend(urls)
    assert sent == [
        "https://nimg.ws.126.net/a.jpg",
        "https://nimg.ws.126.net/a.jpg",
        "https://nimg.ws.126.net/b.jpg",
        "https://nimg.ws.126.net/b.jpg",
    ]
    assert extracted["overview"]["net_1d"] == 12.5
    by_name = {row["name"]: row for row in extracted["etfs"]}
    assert by_name["宽基甲"]["code"] == "510880"
    assert by_name["宽基甲"]["net_1d"] == 0.4
    assert by_name["宽基甲"]["net_20d"] == -2
    assert by_name["坏代码"]["code"] == ""
    assert "510050" in by_name["最大申购"]["code"]


def test_empty_content_retries_once_then_skips_that_image(news_db):
    class Seq:
        def __init__(self):
            self.posts = []

        def post(self, url, headers=None, json=None):
            self.posts.append(json)
            if len(self.posts) <= 2:
                return Resp("", url, payload=_completion("  "))
            return Resp("", url, payload=_vision())

    client = Seq()
    extracted = _call_vision(
        client,
        ["https://nimg.ws.126.net/a.jpg", "https://nimg.ws.126.net/b.jpg"],
    )
    assert len(client.posts) == 4
    assert extracted["overview"]["net_1d"] == 12.5
    assert [body["messages"][0]["content"][1]["image_url"]["url"] for body in client.posts] == [
        "https://nimg.ws.126.net/a.jpg",
        "https://nimg.ws.126.net/a.jpg",
        "https://nimg.ws.126.net/b.jpg",
        "https://nimg.ws.126.net/b.jpg",
    ]


def test_empty_content_on_the_only_image_fails_after_one_retry(news_db):
    class Seq:
        def __init__(self):
            self.posts = []

        def post(self, url, headers=None, json=None):
            self.posts.append(json)
            return Resp("", url, payload=_completion(""))

    client = Seq()
    with pytest.raises(ExtractFailedError, match="没有返回内容"):
        _call_vision(client, ["https://nimg.ws.126.net/a.jpg"])
    assert len(client.posts) == 2


def test_non_empty_garbage_is_not_retried(news_db):
    class Seq:
        def __init__(self):
            self.posts = []

        def post(self, url, headers=None, json=None):
            self.posts.append(json)
            return Resp("", url, payload=_completion("不是表格"))

    client = Seq()
    with pytest.raises(ExtractFailedError, match="没有抽出"):
        _call_vision(client, ["https://nimg.ws.126.net/a.jpg", "https://nimg.ws.126.net/b.jpg"])
    assert len(client.posts) == 1


def test_sign_mismatch_across_retries_clears_only_that_cell(news_db):
    first = {
        "overview": {"net_1d": 1.0, "net_5d": 2.0, "net_20d": -3.0},
        "etfs": [
            {"name": "甲", "code": "510300", "net_1d": 4.0, "net_5d": -1.0, "net_20d": 0.2},
            {"name": "坏代码", "code": "12345", "net_1d": 8},
        ],
    }
    second = {
        "overview": {"net_1d": -1.0, "net_5d": 2.5, "net_20d": -3.0},
        "etfs": [
            {"name": "甲", "code": "510300.SH", "net_1d": -4.0, "net_5d": -1.2, "net_20d": 0.2},
        ],
    }

    answers = [
        _completion(json.dumps(first, ensure_ascii=False)),
        _completion(json.dumps(second, ensure_ascii=False)),
    ]

    class Seq:
        def __init__(self):
            self.posts = []

        def post(self, url, headers=None, json=None):
            self.posts.append(json)
            return Resp("", url, payload=answers[len(self.posts) - 1])

    client = Seq()
    extracted = _call_vision(client, ["https://nimg.ws.126.net/a.jpg"])
    assert len(client.posts) == 2
    assert extracted["overview"]["net_1d"] is None
    assert extracted["overview"]["net_5d"] == 2.0
    assert extracted["overview"]["net_20d"] == -3.0
    row = next(item for item in extracted["etfs"] if item["name"] == "甲")
    assert row["code"] == "510300"
    assert row["net_1d"] is None
    assert row["net_5d"] == -1.0
    assert row["net_20d"] == 0.2
    assert all(item["code"] == "" or len(item["code"]) == 6 for item in extracted["etfs"])


def test_redirect_stops_before_leaving_allowlist():
    class RedirectClient:
        def __init__(self):
            self.gets = []

        def get(self, url, params=None, headers=None):
            self.gets.append(url)
            return Resp("", url, status=302, headers={"location": "http://127.0.0.1/secret"})

    client = RedirectClient()
    with pytest.raises(RuntimeError, match="允许的站点"):
        _fetch(
            client,
            "https://weixin.sogou.com/link?url=1",
            allow_hosts={"weixin.sogou.com", "mp.weixin.qq.com"},
        )
    assert client.gets == ["https://weixin.sogou.com/link?url=1"]


def test_schedule_includes_saturday_after_friday_and_skips_sunday():
    assert expects_publication(SATURDAY, True) is True
    assert expects_publication(SATURDAY, False) is False
    assert expects_publication(MONDAY, None) is False
    assert expects_publication(datetime(2026, 10, 13, 8, 0, tzinfo=CN_TZ), None) is True
    assert is_check_morning(SATURDAY, False, True) is True
    assert is_check_morning(SUNDAY, False, False) is False
    assert is_check_morning(MONDAY, True, False) is True
    morning = datetime(2026, 10, 10, 8, 0, tzinfo=CN_TZ)
    assert etf_wait_seconds(morning, pending=True) == 15 * 60
    late = datetime(2026, 10, 10, 8, 50, tzinfo=CN_TZ)
    assert etf_wait_seconds(late, pending=True) == int((DEADLINE - late).total_seconds())
    almost = datetime(2026, 10, 10, 8, 59, 50, tzinfo=CN_TZ)
    assert etf_wait_seconds(almost, pending=True) == 30
    early = datetime(2026, 10, 10, 7, 0, tzinfo=CN_TZ)
    assert etf_wait_seconds(early, pending=False) == 35 * 60
    done = datetime(2026, 10, 10, 7, 40, tzinfo=CN_TZ)
    nxt = datetime(2026, 10, 11, 7, 35, tzinfo=CN_TZ)
    assert etf_wait_seconds(done, pending=False) == int((nxt - done).total_seconds())


def test_weekend_probe_does_not_ask_calendar(monkeypatch):
    calls = []
    monkeypatch.setattr("app.news.etf_flow._fuyao_contains", lambda day: calls.append(day) or None)
    assert day_was_trading(date(2026, 10, 10)) is False
    assert calls == []
    assert day_was_trading(date(2026, 10, 9)) is None
    assert calls == [date(2026, 10, 9)]


def test_sogou_budget_caps_a_day_and_resets(tmp_path):
    path = tmp_path / "budget.json"
    start = datetime(2026, 10, 10, 8, 0, tzinfo=CN_TZ)
    opened = [sogou_begin(path, start + timedelta(minutes=21 * i)) for i in range(6)]
    assert opened == [True, True, True, True, False, False]
    assert sogou_begin(path, start + timedelta(minutes=10)) is False
    assert sogou_begin(path, datetime(2026, 10, 12, 8, 0, tzinfo=CN_TZ)) is True


def test_source_stays_off_without_vision_key(monkeypatch):
    monkeypatch.delenv("VISION_AI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "vision_ai_api_key", "")
    monkeypatch.setenv("NEWS_ETF_FLOW_ENABLED", "true")
    assert source_configured("etf_flow") is False
    assert source_enabled("etf_flow") is False
    monkeypatch.setenv("VISION_AI_API_KEY", "vision-test-key")
    assert source_configured("etf_flow") is True
    assert source_enabled("etf_flow") is True
    monkeypatch.setenv("NEWS_ETF_FLOW_ENABLED", "false")
    assert source_enabled("etf_flow") is False
    monkeypatch.setenv("VISION_AI_BASE_URL", "https://vision.example.test/v1/")
    monkeypatch.setenv("VISION_AI_MODEL", "glm-from-env")
    monkeypatch.setattr(settings, "ai_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "ai_model", "deepseek-chat")
    assert vision_base_url() == "https://vision.example.test/v1"
    assert vision_model() == "glm-from-env"
    monkeypatch.delenv("VISION_AI_MODEL", raising=False)
    monkeypatch.setattr(settings, "vision_ai_model", "")
    assert vision_model() == "deepseek/deepseek-v4-flash-vision-exp"


def test_netease_ingest_feeds_hot_and_dsa_without_text_llm(news_db, monkeypatch):
    monkeypatch.setenv("NEWS_ETF_FLOW_ENABLED", "true")
    client = Client(media=_text("netease_media.html"), article=_text("netease_article.html"))
    saved = _collect(client, news_db, SATURDAY, today_trading=False, yesterday_trading=True)
    assert saved["inserted"] == 1
    assert saved["found"] is True
    assert saved["pending"] is False
    assert len(client.posts) == 4
    assert all("163.com" in url for url in client.gets)
    images = []
    for url, headers, body in client.posts:
        assert url == "https://tokenhub.tencentmaas.com/v1/chat/completions"
        assert headers["Authorization"] == "Bearer vision-test-key"
        encoded = json.dumps({"url": url, "headers": headers, "body": body}, ensure_ascii=False)
        assert "deepseek-secret" not in encoded
        assert "deepseek-chat" not in encoded
        assert body["model"] == "deepseek/deepseek-v4-flash-vision-exp"
        assert body["max_tokens"] >= 8192
        assert "thinking" not in body
        assert "enable_thinking" not in body
        assert "reasoning_effort" not in body
        parts = [
            part["image_url"]["url"]
            for part in body["messages"][0]["content"]
            if part["type"] == "image_url"
        ]
        assert len(parts) == 1
        images.extend(parts)
    assert images == [
        "https://nimg.ws.126.net/a.jpg",
        "https://nimg.ws.126.net/a.jpg",
        "https://nimg.ws.126.net/b.jpg",
        "https://nimg.ws.126.net/b.jpg",
    ]

    row = (
        get_store()
        ._conn.execute(
            "SELECT source_id, title, url, media_ids, raw_json, clean_text FROM news_items"
        )
        .fetchone()
    )
    assert row["source_id"] == "netease:L8S4S48P0556ADVD"
    assert row["title"] == "10月9日ETF基金申购和赎回"
    assert row["url"] == "https://www.163.com/dy/article/L8S4S48P0556ADVD.html"
    assert json.loads(row["media_ids"]) == [
        "https://nimg.ws.126.net/a.jpg",
        "https://nimg.ws.126.net/b.jpg",
    ]
    raw = json.loads(row["raw_json"])
    assert raw["channel"] == "netease"
    assert raw["trade_date"] == "2026-10-09"
    assert raw["extracted"]["overview"]["net_1d"] == 12.5
    assert "12.5亿元" in row["clean_text"]
    assert "evil.example" not in row["raw_json"]

    again = _collect(client, news_db, SATURDAY, today_trading=False, yesterday_trading=True)
    assert again["found"] is True
    assert again["inserted"] == 0
    assert len(client.posts) == 4
    assert len(client.gets) == 2

    monkeypatch.setattr("app.news.config.feed_token", lambda: "feed-secret")
    from app.api.news import dsa_feed

    feed = dsa_feed(source="etf_flow", limit=10, header_token="feed-secret")
    assert feed["name"] == "ETF领航者"
    item = feed["items"][0]
    assert item["symbols"][0] == "510050.SH"
    assert "159915.SZ" in item["symbols"]
    assert "510500.SH" in item["symbols"]
    assert "510300.SH" not in item["symbols"]
    assert len(item["symbols"]) == 8
    assert item["sectors"][:2] == ["沪深300", "中证500"]
    assert "科技" in item["sectors"]
    assert "12.5" in item["summary"]
    ranked = hot_candidates(kind="stock", limit=20)
    assert any(row.key == "510050.SH" for row in ranked)
    sectors = hot_candidates(kind="sector", limit=20)
    assert any(row.key == "科技" for row in sectors)
    ids = [source["id"] for source in health_payload()["sources"]]
    assert "etf_flow" in ids


def test_sogou_backup_when_netease_has_no_today_article(news_db):
    client = Client(
        media=_text("netease_media_stale.html"),
        sogou=_text("sogou_account.html"),
        wechat=_text("wechat_article.html"),
    )
    saved = _collect(client, news_db, SATURDAY, today_trading=False, yesterday_trading=True)
    assert saved["inserted"] == 1
    assert any(url.startswith("https://weixin.sogou.com/weixin") for url in client.gets)
    assert any(url.startswith("https://mp.weixin.qq.com/") for url in client.gets)
    assert not any("/dy/article/" in url for url in client.gets)
    row = (
        get_store()
        ._conn.execute("SELECT source_id, url, media_ids, raw_json FROM news_items")
        .fetchone()
    )
    assert row["source_id"] == "wechat:a1b2c3d4"
    assert "sn=a1b2c3d4" in row["url"]
    assert json.loads(row["media_ids"]) == [
        "https://mmbiz.qpic.cn/table1.jpg",
        "https://mmbiz.qpic.cn/table2.jpg",
    ]
    assert json.loads(row["raw_json"])["channel"] == "wechat"
    assert "not-content" not in row["raw_json"]


def test_sogou_budget_blocks_backup_before_deadline(news_db):
    budget = news_db / "news" / "etf_sogou_budget.json"
    budget.parent.mkdir(parents=True)
    budget.write_text(
        json.dumps(
            {
                "date": "2026-10-10",
                "count": 4,
                "last_at": "2026-10-10T07:00:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    client = Client(media=_text("netease_media_stale.html"), sogou=_text("sogou_account.html"))
    saved = _collect(
        client,
        news_db,
        datetime(2026, 10, 10, 8, 0, tzinfo=CN_TZ),
        today_trading=False,
        yesterday_trading=True,
    )
    assert saved["found"] is False
    assert saved["pending"] is True
    assert not any("sogou.com" in url for url in client.gets)
    assert client.posts == []


def test_missing_article_alerts_at_deadline_without_numbers(news_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        settings, "dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test"
    )
    monkeypatch.setattr(
        "app.news.etf_flow.send_dingtalk", lambda *args, **kwargs: sent.append(args)
    )
    client = Client(media=_text("netease_media_stale.html"), sogou="<ul class='news-list2'></ul>")
    early = _collect(
        client,
        news_db,
        datetime(2026, 10, 10, 8, 0, tzinfo=CN_TZ),
        today_trading=False,
        yesterday_trading=True,
    )
    assert early["pending"] is True
    assert sent == []
    late = _collect(client, news_db, DEADLINE, today_trading=False, yesterday_trading=True)
    assert late["found"] is False
    assert late["pending"] is False
    assert len(sent) == 1
    message = sent[0][2]
    assert "仍未采集到" in message
    assert "510300" not in message
    assert "http" not in message
    rows = {row["source"]: row for row in get_store().health_rows()}
    assert "仍未采集到" in rows["etf_flow"]["last_error"]
    _collect(client, news_db, DEADLINE, today_trading=False, yesterday_trading=True)
    assert len(sent) == 1


def test_vision_failure_retries_then_alerts_extract(news_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        settings, "dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test"
    )
    monkeypatch.setattr(
        "app.news.etf_flow.send_dingtalk", lambda *args, **kwargs: sent.append(args)
    )
    client = Client(
        media=_text("netease_media.html"),
        article=_text("netease_article.html"),
        vision={"choices": [{"message": {"content": "不是表格"}}]},
    )
    early = _collect(client, news_db, SATURDAY, today_trading=False, yesterday_trading=True)
    assert early["found"] is False
    assert early["pending"] is True
    assert get_store()._conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0] == 0
    assert sent == []
    late = _collect(client, news_db, DEADLINE, today_trading=False, yesterday_trading=True)
    assert late["pending"] is False
    assert "表格抽取" in sent[0][2]
    assert "12.5" not in sent[0][2]


def test_monday_catchup_skips_sogou_and_known_article(news_db):
    client = Client(media=_text("netease_media.html"), article=_text("netease_article.html"))
    first = _collect(client, news_db, MONDAY, today_trading=True, yesterday_trading=False)
    assert first["inserted"] == 1
    assert not any("sogou.com" in url for url in client.gets)
    posts = len(client.posts)
    article_gets = [url for url in client.gets if "/dy/article/" in url]
    second = _collect(client, news_db, MONDAY, today_trading=True, yesterday_trading=False)
    assert second["found"] is True
    assert len(client.posts) == posts
    assert [url for url in client.gets if "/dy/article/" in url] == article_gets


def test_closed_morning_and_early_window_do_not_fetch(news_db):
    class Boom:
        def get(self, *args, **kwargs):
            raise AssertionError("should not fetch")

        def post(self, *args, **kwargs):
            raise AssertionError("should not fetch")

    sunday = _collect(Boom(), news_db, SUNDAY, today_trading=False, yesterday_trading=False)
    assert sunday["skipped"] is True
    assert sunday["pending"] is False
    early = _collect(
        Boom(),
        news_db,
        datetime(2026, 10, 10, 7, 0, tzinfo=CN_TZ),
        today_trading=False,
        yesterday_trading=True,
    )
    assert early["skipped"] is True


def test_missing_vision_key_does_not_fetch(news_db, monkeypatch):
    monkeypatch.delenv("VISION_AI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "vision_ai_api_key", "")

    class Boom:
        def get(self, *args, **kwargs):
            raise AssertionError("should not fetch")

    result = _collect(Boom(), news_db, SATURDAY, today_trading=False, yesterday_trading=True)
    assert result["skipped"] is True
    assert "VISION_AI_API_KEY" in result["error"]


def test_run_due_routes_etf_flow(monkeypatch):
    monkeypatch.setattr(
        "app.news.service.collect_etf_flow", lambda: {"found": True, "pending": False}
    )
    from app.news.service import run_due

    assert run_due("etf_flow")["found"] is True


def test_choose_catchup_keeps_latest_flow_when_today_is_absent():
    older = ArticleRef(
        "old",
        "10月8日ETF基金申购和赎回",
        "https://www.163.com/dy/article/OLDARTICLE123456.html",
        datetime(2026, 10, 9, 7, 36, tzinfo=CN_TZ),
        "netease",
    )
    newer = ArticleRef(
        "new",
        "10月9日ETF基金申购和赎回",
        "https://www.163.com/dy/article/L8S4S48P0556ADVD.html",
        datetime(2026, 10, 10, 7, 38, tzinfo=CN_TZ),
        "netease",
    )
    chosen, reason = choose_flow_article([older, newer], day=date(2026, 10, 12), expect=False)
    assert reason == "catchup"
    assert chosen is not None and chosen.article_id == "new"
    missing, why = choose_flow_article([older, newer], day=date(2026, 10, 12), expect=True)
    assert missing is None and why == "missing"


def test_sogou_month_day_does_not_land_in_the_future():
    html = """
    <ul><li>
      <a href="https://weixin.sogou.com/link?url=acct">ETF领航者</a>
      <a href="https://weixin.sogou.com/link?url=article1">12月31日ETF基金申购和赎回</a>
      <span>12月31日</span>
    </li></ul>
    """
    now = datetime(2026, 1, 2, 8, 0, tzinfo=CN_TZ)
    refs = parse_sogou_account(html, now)
    assert len(refs) == 1
    assert refs[0].published_at.date() == date(2025, 12, 31)
    assert refs[0].published_at.timetz().replace(tzinfo=None) >= datetime(2026, 1, 2, 7, 35).time()


def test_model_trade_date_after_publish_is_dropped_when_title_has_no_day():
    published = datetime(2026, 10, 10, 7, 38, tzinfo=CN_TZ)
    kept = apply_trade_date("无关标题", published, {"trade_date": "2026-10-11", "unit": "亿元"})
    assert kept["trade_date"] == ""
    titled = apply_trade_date(
        "10月9日ETF基金申购和赎回", published, {"trade_date": "2026-10-11"}
    )
    assert titled["trade_date"] == "2026-10-09"


def test_public_error_redacts_vision_key_and_dingtalk_token(monkeypatch):
    monkeypatch.setattr(settings, "vision_ai_api_key", "vision-test-key")
    monkeypatch.setenv("VISION_AI_API_KEY", "vision-test-key")
    text = _public_error(
        RuntimeError(
            "Bearer vision-test-key failed "
            "https://oapi.dingtalk.com/robot/send?access_token=sekret&sign=abc123"
        )
    )
    assert "vision-test-key" not in text
    assert "sekret" not in text
    assert "abc123" not in text
    assert "access_token=***" in text


def test_monday_extract_failure_does_not_alert(news_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        settings, "dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test"
    )
    monkeypatch.setattr(
        "app.news.etf_flow.send_dingtalk", lambda *args, **kwargs: sent.append(args)
    )
    client = Client(
        media=_text("netease_media.html"),
        article=_text("netease_article.html"),
        vision={"choices": [{"message": {"content": "不是表格"}}]},
    )
    late = _collect(
        client,
        news_db,
        datetime(2026, 10, 12, 9, 0, tzinfo=CN_TZ),
        today_trading=True,
        yesterday_trading=False,
    )
    assert late["found"] is False
    assert late["pending"] is False
    assert sent == []
    rows = {row["source"]: row for row in get_store().health_rows()}
    assert rows["etf_flow"]["last_error"]
