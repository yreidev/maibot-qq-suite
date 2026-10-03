"""联网搜索的统一接口。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TimeRange = Literal["", "day", "week", "month", "year"]
_TRACKING = ("utm_", "spm", "share_", "fbclid", "gclid")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published: str = ""


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, max_results: int = 8, time_range: TimeRange = "") -> list[SearchResult]:
        """返回按相关度排序的结果；出错抛 ProviderError。"""
        ...


def _clean_url(url: str) -> str:
    """去掉 utm_ 之类的跟踪参数，省 token。"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith(_TRACKING)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def format_results(results: list[SearchResult], *, snippet_chars: int = 160) -> str:
    """把结果排成给模型看的紧凑纯文本：序号、标题（日期）、链接、摘要。结果会进入后续对话上下文，越短越省。"""
    if not results:
        return "没有搜到相关结果。"
    lines: list[str] = []
    seen: set[str] = set()
    for item in results:
        url = _clean_url(item.url)
        if url in seen:
            continue
        seen.add(url)
        date = item.published[:10] if _ISO_DATE.match(item.published) else item.published
        title = " ".join(item.title.split()) + (f"（{date}）" if date else "")
        lines.append(f"{len(seen)}. {title}".rstrip())
        lines.append(f"   {url}")
        snippet = " ".join(item.snippet.split())
        if snippet:
            lines.append(f"   {snippet[:snippet_chars]}" + ("…" if len(snippet) > snippet_chars else ""))
    return "\n".join(lines)
