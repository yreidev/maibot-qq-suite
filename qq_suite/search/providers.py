"""各家搜索服务的实现：SearXNG（自建、免费）、Tavily、博查。"""

from __future__ import annotations

from typing import ClassVar

import aiohttp

from ..common.errors import ProviderError
from ..common.http import DEFAULT_TIMEOUT, bearer, read_json
from .base import SearchResult, TimeRange

_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5)


def _clamp(n: int) -> int:
    return max(1, min(int(n), 20))


class SearXNGSearch:
    """自建 SearXNG：GET {base}/search?q=...&format=json（实例需在 settings.yml 里开启 json 格式）。"""

    name = "searxng"

    def __init__(self, session: aiohttp.ClientSession, *, base_url: str, language: str = "auto") -> None:
        self._session = session
        self._url = base_url.rstrip("/") + "/search"
        self._language = language

    async def search(self, query: str, *, max_results: int = 8, time_range: TimeRange = "") -> list[SearchResult]:
        params = {"q": query, "format": "json", "language": self._language}
        if time_range:
            params["time_range"] = time_range
        async with self._session.get(self._url, params=params, timeout=_TIMEOUT) as resp:
            payload = await read_json(resp, self.name)
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ProviderError(self.name, "返回里没有 results")
        return [
            SearchResult(
                title=str(it.get("title") or ""),
                url=str(it.get("url") or ""),
                snippet=str(it.get("content") or ""),
                published=str(it.get("publishedDate") or ""),
            )
            for it in items[: _clamp(max_results)]
            if isinstance(it, dict) and it.get("url")
        ]


class TavilySearch:
    """Tavily：POST https://api.tavily.com/search（https://docs.tavily.com/documentation/api-reference/endpoint/search）。"""

    name = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, session: aiohttp.ClientSession, *, api_key: str) -> None:
        self._session = session
        self._api_key = api_key

    async def search(self, query: str, *, max_results: int = 8, time_range: TimeRange = "") -> list[SearchResult]:
        body: dict[str, object] = {"query": query, "max_results": _clamp(max_results), "search_depth": "basic"}
        if time_range:
            body["time_range"] = time_range
        async with self._session.post(self.URL, json=body, headers=bearer(self._api_key), timeout=_TIMEOUT) as resp:
            payload = await read_json(resp, self.name)
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ProviderError(self.name, "返回里没有 results")
        return [
            SearchResult(
                title=str(it.get("title") or ""), url=str(it.get("url") or ""), snippet=str(it.get("content") or "")
            )
            for it in items
            if isinstance(it, dict) and it.get("url")
        ]


class BochaSearch:
    """博查：POST https://api.bochaai.com/v1/web-search。"""

    name = "bocha"
    URL = "https://api.bochaai.com/v1/web-search"
    _FRESHNESS: ClassVar[dict[str, str]] = {
        "": "noLimit",
        "day": "oneDay",
        "week": "oneWeek",
        "month": "oneMonth",
        "year": "oneYear",
    }

    def __init__(self, session: aiohttp.ClientSession, *, api_key: str) -> None:
        self._session = session
        self._api_key = api_key

    async def search(self, query: str, *, max_results: int = 8, time_range: TimeRange = "") -> list[SearchResult]:
        body = {
            "query": query,
            "count": _clamp(max_results),
            "summary": True,
            "freshness": self._FRESHNESS.get(time_range, "noLimit"),
        }
        headers = bearer(self._api_key)
        async with self._session.post(self.URL, json=body, headers=headers, timeout=DEFAULT_TIMEOUT) as resp:
            payload = await read_json(resp, self.name)
        try:
            items = payload["data"]["webPages"]["value"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(self.name, "返回结构不对，找不到 data.webPages.value") from exc
        return [
            SearchResult(
                title=str(it.get("name") or ""),
                url=str(it.get("url") or ""),
                snippet=str(it.get("summary") or it.get("snippet") or ""),
                published=str(it.get("datePublished") or ""),
            )
            for it in items or []
            if isinstance(it, dict) and it.get("url")
        ]
