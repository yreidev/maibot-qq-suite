"""网关测试：本地起一个假的 QQ WebSocket 网关，按脚本和客户端对话。"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from qq_suite.qq.auth import TokenProvider
from qq_suite.qq.gateway import GatewayClient


class Recorder:
    def __init__(self) -> None:
        self.ready: list[str] = []
        self.events: list[tuple[str, dict, str]] = []
        self.disconnects: list[str] = []

    async def on_ready(self, bot_id: str) -> None:
        self.ready.append(bot_id)

    async def on_dispatch(self, event_type: str, data: dict, event_id: str) -> None:
        self.events.append((event_type, data, event_id))

    async def on_disconnected(self, reason: str) -> None:
        self.disconnects.append(reason)


async def _drain(ws: web.WebSocketResponse) -> None:
    """一直读到客户端关闭，让关闭握手正常完成。"""
    async for _ in ws:
        pass


class FakeGateway:
    """每次连接按顺序取一个剧本；剧本是 async 函数 (ws, first_client_frame) -> None。"""

    def __init__(self, scripts) -> None:
        self.scripts = list(scripts)
        self.client_frames: list[dict] = []
        self.gateway_calls = 0

    async def gateway_url(self) -> str:
        self.gateway_calls += 1
        return self.url

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 60000}})
        first = json.loads((await ws.receive()).data)
        self.client_frames.append(first)
        script = self.scripts.pop(0) if self.scripts else None
        if script is None:
            await _drain(ws)
        else:
            await script(ws, first)
        return ws


async def _setup(aiohttp_server, fake_service, scripts):
    gw = FakeGateway(scripts)
    app = web.Application()
    app.router.add_get("/ws", gw.handler)
    server = await aiohttp_server(app)
    gw.url = str(server.make_url("/ws")).replace("http://", "ws://")

    async def token(request: web.Request) -> web.Response:
        return web.json_response({"access_token": "TK", "expires_in": 7200})

    base, session, _ = await fake_service({("POST", "/tok"): token})
    tokens = TokenProvider(session, app_id="a", app_secret="s", token_url=base + "/tok")
    rec = Recorder()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    client = GatewayClient(session, gw, tokens, rec, sleep=fake_sleep)
    return gw, client, rec, sleeps


async def _wait_until(predicate, limit=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not predicate():
        assert loop.time() < end, "等待超时"
        await asyncio.sleep(0.01)


async def test_identify_dispatch_then_resume(aiohttp_server, fake_service):
    async def first_conn(ws, first):
        assert first["op"] == 2
        assert first["d"]["token"] == "QQBot TK" and first["d"]["intents"] == 1 << 25
        await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1", "user": {"id": "BOT"}}})
        msg = {"id": "ROBOT1.0_a", "author": {"user_openid": "U"}, "content": "hi"}
        await ws.send_json({"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE", "id": "EV1", "d": msg})
        await asyncio.sleep(0.05)
        await ws.close(code=4009)  # 连接过期：应 Resume

    async def second_conn(ws, first):
        assert first == {"op": 6, "d": {"token": "QQBot TK", "session_id": "S1", "seq": 2}}
        await ws.send_json({"op": 0, "s": 3, "t": "RESUMED", "d": ""})
        await _drain(ws)

    gw, client, rec, sleeps = await _setup(aiohttp_server, fake_service, [first_conn, second_conn])
    task = asyncio.create_task(client.run_forever())
    await _wait_until(lambda: len(rec.ready) == 2)
    await client.stop()
    await asyncio.wait_for(task, 3)
    assert rec.ready == ["BOT", "BOT"]  # Resume 后也报告同一个机器人 ID
    assert rec.events == [
        ("C2C_MESSAGE_CREATE", {"id": "ROBOT1.0_a", "author": {"user_openid": "U"}, "content": "hi"}, "EV1")
    ]
    assert gw.gateway_calls == 1  # 网关地址被缓存
    assert len(rec.disconnects) == 1 and len(sleeps) == 1 and sleeps[0] < 2


async def test_invalid_session_reidentifies(aiohttp_server, fake_service):
    async def first_conn(ws, first):
        await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1", "user": {"id": "BOT"}}})
        await ws.send_json({"op": 9, "d": False})
        await _drain(ws)

    async def second_conn(ws, first):
        assert first["op"] == 2  # 会话被清空，重新 Identify
        await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S2", "user": {"id": "BOT"}}})
        await _drain(ws)

    gw, client, rec, _ = await _setup(aiohttp_server, fake_service, [first_conn, second_conn])
    task = asyncio.create_task(client.run_forever())
    await _wait_until(lambda: len(rec.ready) == 2)
    await client.stop()
    await asyncio.wait_for(task, 3)
    assert [f["op"] for f in gw.client_frames] == [2, 2]


async def test_fatal_close_code_backs_off_long(aiohttp_server, fake_service):
    async def first_conn(ws, first):
        await ws.close(code=4014)  # intent 无权限

    _gw, client, _rec, sleeps = await _setup(aiohttp_server, fake_service, [first_conn])
    task = asyncio.create_task(client.run_forever())
    await _wait_until(lambda: len(sleeps) >= 1)
    await client.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sleeps[0] == 600.0


async def test_heartbeat_sends_latest_seq(aiohttp_server, fake_service):
    beats: list[dict] = []

    async def conn(ws, first):
        await ws.send_json({"op": 0, "s": 7, "t": "READY", "d": {"session_id": "S", "user": {"id": "B"}}})
        await ws.send_json({"op": 1})  # 服务端要求立即心跳
        msg = await ws.receive()
        beats.append(json.loads(msg.data))
        await _drain(ws)

    _gw, client, _rec, _ = await _setup(aiohttp_server, fake_service, [conn])
    task = asyncio.create_task(client.run_forever())
    await _wait_until(lambda: beats)
    await client.stop()
    await asyncio.wait_for(task, 3)
    assert beats == [{"op": 1, "d": 7}]
