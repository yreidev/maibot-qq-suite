"""联网搜索与网页读取，与 QQ、MaiBot 都无关。"""

from .base import SearchProvider, SearchResult, format_results
from .factory import SearchSettings, build_search
from .reader import PageContent, PageReader, make_reader_session

__all__ = [
    "PageContent",
    "PageReader",
    "SearchProvider",
    "SearchResult",
    "SearchSettings",
    "build_search",
    "format_results",
    "make_reader_session",
]
