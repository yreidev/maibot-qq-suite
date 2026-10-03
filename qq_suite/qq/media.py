"""下载用户发来的附件（图片、语音等）。

只信任 QQ 自家的媒体域名，避免附件地址被用来访问内网或其他站点。
实测（2026-10）：语音在 qqbot.ugcimg.cn，图片在 *.nt.qq.com.cn 等。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import aiohttp

from ..common.errors import ProviderError

TRUSTED_HOST_SUFFIXES = (".qq.com", ".qq.com.cn", ".ugcimg.cn", ".qpic.cn", ".gtimg.cn")
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def is_trusted_media_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return False
    return any(host.endswith(suffix) or host == suffix.lstrip(".") for suffix in TRUSTED_HOST_SUFFIXES)


async def download(session: aiohttp.ClientSession, url: str, *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> bytes:
    if not is_trusted_media_url(url):
        raise ProviderError("qq_media", f"附件地址不在 QQ 媒体域名内：{urlsplit(url).hostname}")
    async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=60, connect=10)) as resp:
        if resp.status != 200:
            raise ProviderError("qq_media", "下载附件失败", status=resp.status)
        data = await resp.content.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ProviderError("qq_media", "附件超过大小上限")
    return data
