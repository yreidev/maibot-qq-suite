"""公共测试夹具：用 aiohttp 起本地假服务，测试真实的 HTTP 往返，不 mock 客户端内部。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web

pytest_plugins = ["aiohttp.pytest_plugin"]

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@pytest.fixture
async def fake_service(aiohttp_server):
    """fake_service({("POST", "/path"): handler}) -> (base_url, session, calls)。calls 记录收到的请求。"""
    sessions: list[aiohttp.ClientSession] = []

    async def make(routes: dict[tuple[str, str], Handler]):
        calls: list[web.Request] = []
        app = web.Application()

        def wrap(handler: Handler) -> Handler:
            async def inner(request: web.Request) -> web.StreamResponse:
                calls.append(request)
                return await handler(request)

            return inner

        for (method, path), handler in routes.items():
            app.router.add_route(method, path, wrap(handler))
        server = await aiohttp_server(app)
        session = aiohttp.ClientSession()
        sessions.append(session)
        return str(server.make_url("")).rstrip("/"), session, calls

    yield make
    for session in sessions:
        await session.close()
