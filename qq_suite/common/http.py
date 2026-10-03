"""HTTP 辅助函数。

各服务商实现都由调用方注入 ``aiohttp.ClientSession``，自己不创建会话：
会话的生命周期归插件入口管理，测试时也可以换成指向本地测试服务器的会话。
"""

from __future__ import annotations

from typing import Any

import aiohttp

from .errors import ProviderError

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10)
_ERROR_BODY_LIMIT = 300


def bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def read_json(resp: aiohttp.ClientResponse, provider: str) -> Any:
    """读取 JSON 响应；非 2xx 或不是 JSON 时抛 ProviderError，错误信息截断以免刷屏。"""
    if resp.status >= 400:
        body = (await resp.text(errors="replace"))[:_ERROR_BODY_LIMIT]
        raise ProviderError(provider, body or resp.reason or "请求失败", status=resp.status)
    try:
        return await resp.json(content_type=None)
    except ValueError as exc:
        raise ProviderError(provider, "返回的不是 JSON", status=resp.status) from exc
