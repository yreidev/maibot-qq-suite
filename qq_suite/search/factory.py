"""根据配置构造搜索服务商。"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from ..common.errors import ConfigError
from .base import SearchProvider
from .providers import BochaSearch, SearXNGSearch, TavilySearch


@dataclass(frozen=True)
class SearchSettings:
    provider: str = "searxng"  # searxng | tavily | bocha | none
    base_url: str = ""  # searxng 实例地址
    api_key: str = ""  # tavily / bocha 的密钥
    language: str = "auto"


def build_search(settings: SearchSettings, session: aiohttp.ClientSession) -> SearchProvider | None:
    provider = settings.provider.strip().lower()
    if provider in {"", "none"}:
        return None
    if provider == "searxng":
        if not settings.base_url:
            raise ConfigError("SearXNG 需要填 base_url")
        return SearXNGSearch(session, base_url=settings.base_url, language=settings.language or "auto")
    if not settings.api_key:
        raise ConfigError(f"搜索服务商 {provider} 需要填 api_key")
    if provider == "tavily":
        return TavilySearch(session, api_key=settings.api_key)
    if provider == "bocha":
        return BochaSearch(session, api_key=settings.api_key)
    raise ConfigError(f"不认识的搜索服务商：{settings.provider}")
