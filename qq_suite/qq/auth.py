"""QQ 机器人 AccessToken 管理。

文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html
- POST {token_url}，body {"appId", "clientSecret"}；失败时 HTTP 仍是 200，要看 body 里的 code
- 有效期默认 7200 秒，expires_in 可能是字符串；只有剩余不到 60 秒时请求才会拿到新 token
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import aiohttp

from ..common.errors import ProviderError
from ..common.http import read_json

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
# 平台只在最后 60 秒内签发新 token，所以在剩 50 秒时刷新
_REFRESH_BEFORE = 50.0


class TokenProvider:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        app_id: str,
        app_secret: str,
        token_url: str = TOKEN_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_url = token_url
        self._clock = clock
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """收到 401 等鉴权错误时调用，下次 get() 会重新获取。"""
        self._expires_at = 0.0

    async def get(self) -> str:
        if self._token and self._clock() < self._expires_at - _REFRESH_BEFORE:
            return self._token
        async with self._lock:
            if self._token and self._clock() < self._expires_at - _REFRESH_BEFORE:
                return self._token
            await self._fetch()
            return self._token

    async def _fetch(self) -> None:
        body = {"appId": self._app_id, "clientSecret": self._app_secret}
        async with self._session.post(self._token_url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await read_json(resp, "qq_token")
        if not isinstance(payload, dict) or not payload.get("access_token"):
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("message") if isinstance(payload, dict) else payload
            raise ProviderError("qq_token", f"获取 AccessToken 失败：code={code} {message}")
        try:
            ttl = float(payload.get("expires_in", 7200))
        except (TypeError, ValueError):
            ttl = 7200.0
        self._token = str(payload["access_token"])
        self._expires_at = self._clock() + ttl

    @property
    def authorization(self) -> str:
        """当前 token 的 Authorization 头值（调用前须先 await get()）。"""
        return f"QQBot {self._token}"
