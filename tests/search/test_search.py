from __future__ import annotations

import socket

import aiohttp
import pytest
from aiohttp import web

from qq_suite.common.errors import ConfigError, ProviderError
from qq_suite.search import SearchResult, SearchSettings, build_search, format_results
from qq_suite.search.providers import BochaSearch, SearXNGSearch, TavilySearch
from qq_suite.search.reader import PageReader, PublicOnlyResolver, check_url, html_to_text


async def test_searxng(fake_service):
    async def handler(request: web.Request) -> web.Response:
        assert request.query["format"] == "json"
        assert request.query["time_range"] == "week"
        results = [{"title": f"t{i}", "url": f"https://e.com/{i}", "content": "c"} for i in range(5)]
        results.append({"title": "no url"})
        return web.json_response({"results": results})

    base, session, _ = await fake_service({("GET", "/search"): handler})
    got = await SearXNGSearch(session, base_url=base).search("天气", max_results=3, time_range="week")
    assert [r.url for r in got] == ["https://e.com/0", "https://e.com/1", "https://e.com/2"]


async def test_tavily_and_bocha(fake_service):
    async def tavily(request: web.Request) -> web.Response:
        body = await request.json()
        assert request.headers["Authorization"] == "Bearer tk" and body["max_results"] == 20
        return web.json_response({"results": [{"title": "a", "url": "https://a", "content": "x"}]})

    async def bocha(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["freshness"] == "oneDay"
        page = {"name": "b", "url": "https://b", "summary": "s", "datePublished": "2026-10-01"}
        return web.json_response({"data": {"webPages": {"value": [page]}}})

    base, session, _ = await fake_service({("POST", "/t"): tavily, ("POST", "/b"): bocha})
    t = TavilySearch(session, api_key="tk")
    t.URL = base + "/t"
    assert (await t.search("q", max_results=99))[0].url == "https://a"
    b = BochaSearch(session, api_key="bk")
    b.URL = base + "/b"
    assert (await b.search("q", time_range="day")) == [SearchResult("b", "https://b", "s", "2026-10-01")]


async def test_bocha_bad_shape(fake_service):
    async def bocha(request: web.Request) -> web.Response:
        return web.json_response({"code": 401})

    base, session, _ = await fake_service({("POST", "/b"): bocha})
    b = BochaSearch(session, api_key="bk")
    b.URL = base + "/b"
    with pytest.raises(ProviderError):
        await b.search("q")


def test_format_results():
    assert format_results([]) == "没有搜到相关结果。"
    text = format_results([SearchResult("标题", "https://x", "a   b\nc" * 200)], snippet_chars=10)
    assert text.splitlines() == ["1. 标题", "   https://x", "   a b ca b c…"]
    compact = format_results(
        [
            SearchResult("新闻", "https://e.com/a?id=1&utm_source=x&spm=y", "短", "2026-10-01T08:00:00Z"),
            SearchResult("重复", "https://e.com/a?id=1", "同一个链接只留一条"),
            SearchResult("旧闻", "https://e.com/b", "", "上周"),
        ]
    )
    assert compact.splitlines() == [
        "1. 新闻（2026-10-01）",
        "   https://e.com/a?id=1",
        "   短",
        "2. 旧闻（上周）",
        "   https://e.com/b",
    ]


async def test_factory():
    async with aiohttp.ClientSession() as session:
        assert build_search(SearchSettings(provider="none"), session) is None
        assert build_search(SearchSettings(base_url="http://s"), session).name == "searxng"
        assert build_search(SearchSettings(provider="tavily", api_key="k"), session).name == "tavily"
        with pytest.raises(ConfigError):
            build_search(SearchSettings(), session)
        with pytest.raises(ConfigError):
            build_search(SearchSettings(provider="bocha"), session)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "http://localhost:8080/",
        "http://127.0.0.1/",
        "http://10.42.0.5/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://db.default.svc.cluster.local:5432/",
        "http://maibot.svc/",
    ],
)
def test_check_url_rejects_private(url):
    with pytest.raises(ProviderError):
        check_url(url)


def test_check_url_allows_public():
    assert check_url(" https://example.com/a?b=1 ") == "https://example.com/a?b=1"
    assert check_url("http://8.8.8.8/") == "http://8.8.8.8/"


class _FakeResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, ips):
        self.ips = ips

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {"hostname": host, "host": ip, "port": port, "family": family, "proto": 0, "flags": 0} for ip in self.ips
        ]

    async def close(self):
        pass


async def test_public_only_resolver():
    mixed = PublicOnlyResolver(_FakeResolver(["10.0.0.1", "93.184.216.34"]))
    assert [r["host"] for r in await mixed.resolve("x.com")] == ["93.184.216.34"]
    with pytest.raises(OSError):
        await PublicOnlyResolver(_FakeResolver(["192.168.1.1", "127.0.0.1"])).resolve("evil.com")


def test_html_to_text():
    html = """<html><head><title> 标 题 </title><style>.x{}</style></head><body>
    <nav>菜单</nav><h1>正文标题</h1><p>第一段&amp;内容</p><script>alert(1)</script>
    <ul><li>一</li><li>二</li></ul><footer>页脚</footer></body></html>"""
    title, text = html_to_text(html)
    assert title == "标 题"
    assert text.splitlines() == ["正文标题", "第一段&内容", "一", "二"]


def test_html_to_text_prefers_main_content():
    body = "正文内容。" * 50
    html = f"""<body><div>侧边推荐</div><div>侧边推荐</div><button>点赞</button>
    <article><h1>标题</h1><p>{body}</p><p>{body}</p></article><div>相关阅读</div></body>"""
    _, text = html_to_text(html)
    assert text.splitlines() == ["标题", body]  # 只取正文容器，紧挨着重复的段落只留一次

    short = "<body><p>导航</p><main><p>很短</p></main><p>其他</p></body>"
    assert html_to_text(short)[1].splitlines() == ["导航", "很短", "其他"]  # 正文容器太短时用全文


async def test_reader_follows_redirect_and_rechecks(fake_service):
    async def start(request: web.Request) -> web.Response:
        raise web.HTTPFound("/page")

    async def page(request: web.Request) -> web.Response:
        body = "<title>T</title><p>" + "字" * 50 + "</p>"
        return web.Response(text=body, content_type="text/html", charset="utf-8")

    async def to_private(request: web.Request) -> web.Response:
        raise web.HTTPFound("http://10.0.0.1/secret")

    base, session, _ = await fake_service(
        {("GET", "/start"): start, ("GET", "/page"): page, ("GET", "/bad"): to_private}
    )
    checked: list[str] = []

    def lenient(url: str) -> str:  # 本地测试服务在 127.0.0.1：只放行它，其余走真校验
        checked.append(url)
        return url if url.startswith(base) else check_url(url)

    reader = PageReader(session, url_checker=lenient)
    content = await reader.read(base + "/start", max_chars=20)
    assert content.title == "T" and content.text == "字" * 20 and content.truncated
    assert checked == [base + "/start", base + "/page"]
    with pytest.raises(ProviderError):
        await reader.read(base + "/bad")
