"""读取网页正文给模型看。

网址由模型给出，所以要防止被用来访问内网（SSRF）：
- 只允许 http/https，拒绝 IP 字面量是内网地址、localhost；
- 会话用 PublicOnlyResolver：域名只要解析到内网地址就拒绝，连接用的也是过滤后的地址，挡住 DNS 重绑定；
- 不自动跟随跳转，每一跳都重新校验。
HTML 用标准库解析，只保留标题和正文文字，不额外引入依赖。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from ..common.errors import ProviderError

_MAX_REDIRECTS = 5
_USER_AGENT = "Mozilla/5.0 (compatible; maibot-qq-suite; +https://github.com/yreidev/maibot-qq-suite)"
_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "template", "nav", "footer", "header", "aside", "form", "iframe",
    "button", "select", "dialog",
}  # fmt: skip
_MAIN_TAGS = {"main", "article"}  # 有正文容器时只取容器里的文字，去掉侧栏、推荐等杂项
_MIN_MAIN_CHARS = 200
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "br", "li", "ul", "ol", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dd", "dt", "figcaption",
}  # fmt: skip
_META_CHARSET = re.compile(rb"charset=[\"']?([a-zA-Z0-9_-]+)")


def is_public_address(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class PublicOnlyResolver(AbstractResolver):
    """只返回公网地址的 DNS 解析器，解析结果全是内网地址时直接报错。"""

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self._inner = inner or DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        infos = await self._inner.resolve(host, port, family)
        allowed = [info for info in infos if is_public_address(info["host"])]
        if not allowed:
            raise OSError(f"{host} 解析到内网地址，拒绝访问")
        return allowed

    async def close(self) -> None:
        await self._inner.close()


def make_reader_session() -> aiohttp.ClientSession:
    """读网页专用的会话：带内网过滤的解析器。调用方负责关闭。"""
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(), limit=8)
    return aiohttp.ClientSession(connector=connector, headers={"User-Agent": _USER_AGENT})


def check_url(url: str) -> str:
    """校验网址的协议和主机，返回规范化后的网址；不合格抛 ProviderError。"""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ProviderError("reader", "只能读取 http/https 网址")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".svc", ".cluster.local")):
        raise ProviderError("reader", "不能读取内网地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # 域名，交给 PublicOnlyResolver 在解析时检查
    else:
        if not is_public_address(host):
            raise ProviderError("reader", "不能读取内网地址")
    return parts.geturl()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._main_depth = 0
        self._chunks: list[str] = []
        self._main_chunks: list[str] = []

    def _emit(self, piece: str) -> None:
        self._chunks.append(piece)
        if self._main_depth:
            self._main_chunks.append(piece)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            if tag in _MAIN_TAGS:
                self._main_depth += 1
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._emit("\n")
            if tag in _MAIN_TAGS and self._main_depth:
                self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._emit(data)

    @staticmethod
    def _tidy(chunks: list[str]) -> str:
        lines: list[str] = []
        for line in "".join(chunks).splitlines():
            line = " ".join(line.split())
            if line and (not lines or line != lines[-1]):  # 去掉空行和紧挨着重复的行
                lines.append(line)
        return "\n".join(lines)

    def text(self) -> str:
        main = self._tidy(self._main_chunks)
        return main if len(main) >= _MIN_MAIN_CHARS else self._tidy(self._chunks)


def html_to_text(html: str) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(parser.title.split()), parser.text()


def _decode(body: bytes, header_charset: str | None) -> str:
    charset = header_charset
    if not charset:
        match = _META_CHARSET.search(body[:4096])
        charset = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class PageContent:
    url: str
    title: str
    text: str
    truncated: bool


class PageReader:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        url_checker: Callable[[str], str] = check_url,
    ) -> None:
        self._session = session
        self._max_bytes = max_bytes
        self._check = url_checker

    async def read(self, url: str, *, max_chars: int = 6000) -> PageContent:
        current = self._check(url)
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                async with self._session.get(
                    current, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20, connect=8)
                ) as resp:
                    if resp.status in {301, 302, 303, 307, 308} and resp.headers.get("Location"):
                        current = self._check(urljoin(current, resp.headers["Location"]))
                        continue
                    if resp.status >= 400:
                        raise ProviderError("reader", f"网页返回 {resp.status}", status=resp.status)
                    content_type = resp.content_type or ""
                    body = await resp.content.read(self._max_bytes)
                    text = _decode(body, resp.charset)
            except aiohttp.ClientError as exc:
                raise ProviderError("reader", f"打不开网页：{exc}") from exc
            except OSError as exc:  # PublicOnlyResolver 拒绝时
                raise ProviderError("reader", str(exc)) from exc
            if "html" in content_type or content_type == "":
                title, content = html_to_text(text)
            elif content_type.startswith("text/") or content_type.endswith("json"):
                title, content = "", text.strip()
            else:
                raise ProviderError("reader", f"不支持的内容类型：{content_type}")
            truncated = len(content) > max_chars
            return PageContent(url=current, title=title, text=content[:max_chars], truncated=truncated)
        raise ProviderError("reader", "跳转次数太多")
