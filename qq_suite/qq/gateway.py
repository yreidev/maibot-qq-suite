"""QQ 机器人 WebSocket 网关：保持长连接，收事件。

文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html
流程：Hello(op10) → Identify(op2) 或 Resume(op6) → READY / RESUMED → 按 Hello 给的间隔发心跳(op1)、收 ACK(op11)。
关闭码：https://bot.q.qq.com/wiki/develop/api-v2/gateway/error/error.html
- 4006/4007/4900~4913：会话失效，重新 Identify
- 4001/4002/4010~4014/4914/4915：参数、权限或机器人状态问题，重连无用，长时间退避后再试并报错
- 其他（含 4008/4009、网络断开）：Resume
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiohttp

from .auth import TokenProvider

INTENT_C2C = 1 << 25  # GROUP_AND_C2C_EVENT：单聊消息、好友增删等
_REIDENTIFY_CODES = {4006, 4007, *range(4900, 4914)}
_FATAL_CODES = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}
_FATAL_RETRY_SECONDS = 600.0


class GatewayURLSource(Protocol):
    async def gateway_url(self) -> str: ...


class GatewayHandler(Protocol):
    async def on_ready(self, bot_id: str) -> None: ...
    async def on_dispatch(self, event_type: str, data: dict[str, Any], event_id: str) -> None: ...
    async def on_disconnected(self, reason: str) -> None: ...


@dataclass
class _Session:
    session_id: str = ""
    seq: int | None = None
    bot_id: str = ""

    def clear(self) -> None:
        self.session_id = ""
        self.seq = None


class GatewayFatalError(RuntimeError):
    pass


@dataclass
class Backoff:
    base: float = 1.0
    cap: float = 60.0
    attempts: int = 0
    rand: Callable[[], float] = field(default=random.random)

    def next(self) -> float:
        delay = min(self.cap, self.base * 2**self.attempts)
        self.attempts += 1
        return delay * (0.5 + self.rand() / 2)

    def reset(self) -> None:
        self.attempts = 0


class GatewayClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        urls: GatewayURLSource,
        tokens: TokenProvider,
        handler: GatewayHandler,
        *,
        intents: int = INTENT_C2C,
        logger: logging.Logger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = session
        self._urls = urls
        self._tokens = tokens
        self._handler = handler
        self._intents = intents
        self._log = logger or logging.getLogger(__name__)
        self._sleep = sleep
        self._state = _Session()
        self._url = ""
        self._backoff = Backoff()
        self._stopping = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._acked = True
        self._forced_code: int | None = None  # 我们主动断开时的原因，不依赖对端回的关闭码

    @property
    def bot_id(self) -> str:
        return self._state.bot_id

    async def stop(self) -> None:
        self._stopping.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def run_forever(self) -> None:
        """一直重连直到 stop()。异常都在这里吞掉并退避，不会冒泡打断插件。"""
        while not self._stopping.is_set():
            reason = "连接断开"
            delay: float | None = None
            try:
                close_code = await self._connect_once()
                reason = f"网关关闭 code={close_code}"
                delay = self._apply_close_code(close_code)
            except GatewayFatalError as exc:
                reason = str(exc)
                delay = _FATAL_RETRY_SECONDS
            except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
                reason = f"连接出错：{exc!r}"
                self._url = ""  # 地址可能变了，下次重新取
            except Exception as exc:  # 对方协议异常等，记日志后照常重连
                self._log.exception("QQ 网关未预期的错误")
                reason = f"未预期的错误：{exc!r}"
            if self._stopping.is_set():
                break
            with contextlib.suppress(Exception):
                await self._handler.on_disconnected(reason)
            wait = delay if delay is not None else self._backoff.next()
            self._log.warning("QQ 网关断开（%s），%.1f 秒后重连", reason, wait)
            await self._sleep(wait)

    def _apply_close_code(self, code: int | None) -> float | None:
        if code in _FATAL_CODES:
            self._state.clear()
            self._log.error("QQ 网关关闭码 %s：机器人配置、权限或状态有问题，重连无用，请检查开放平台设置", code)
            return _FATAL_RETRY_SECONDS
        if code in _REIDENTIFY_CODES:
            self._state.clear()
        if code == 4004:  # 身份校验失败：刷新 token
            self._tokens.invalidate()
            self._state.clear()
        return None

    async def _connect_once(self) -> int | None:
        if not self._url:
            self._url = await self._urls.gateway_url()  # /gateway 只有 2 QPM，成功后缓存
        self._forced_code = None
        timeout = aiohttp.ClientWSTimeout(ws_receive=None, ws_close=3.0)
        async with self._http.ws_connect(self._url, heartbeat=None, max_msg_size=0, timeout=timeout) as ws:
            self._ws = ws
            hello = await self._receive_json(ws, wait=20)
            if hello.get("op") != 10:
                raise ValueError(f"首条消息不是 Hello：op={hello.get('op')}")
            interval = float((hello.get("d") or {}).get("heartbeat_interval", 45000)) / 1000
            await self._authenticate(ws)
            heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                await self._read_loop(ws)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            return self._forced_code if self._forced_code is not None else ws.close_code

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        token = await self._tokens.get()
        if self._state.session_id:
            payload = {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self._state.session_id,
                                      "seq": self._state.seq or 0}}  # fmt: skip
        else:
            payload = {"op": 2, "d": {"token": f"QQBot {token}", "intents": self._intents, "shard": [0, 1]}}
        await ws.send_json(payload)

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse, interval: float) -> None:
        self._acked = True
        while not ws.closed:
            await asyncio.sleep(interval)
            if not self._acked:  # 一个周期没收到 ACK：连接已死，断开后 Resume
                self._log.warning("QQ 网关心跳无响应，重连")
                self._forced_code = 4009
                await ws.close(code=4009)
                return
            self._acked = False
            await ws.send_json({"op": 1, "d": self._state.seq})

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
                continue
            frame = json.loads(msg.data)
            op = frame.get("op")
            if op == 0:
                await self._on_dispatch(frame)
            elif op == 11:
                self._acked = True
            elif op == 1:
                await ws.send_json({"op": 1, "d": self._state.seq})
            elif op == 7:  # 服务端要求重连：保留会话，Resume
                self._forced_code = 4009
                await ws.close(code=4009)
                break
            elif op == 9:  # 会话无效：清空后重新 Identify
                self._state.clear()
                self._forced_code = 4006
                await ws.close(code=4006)
                break

    async def _on_dispatch(self, frame: dict[str, Any]) -> None:
        event_type = str(frame.get("t") or "")
        data = frame.get("d")
        if event_type == "READY" and isinstance(data, dict):
            self._state.session_id = str(data.get("session_id") or "")
            self._state.bot_id = str((data.get("user") or {}).get("id") or "")
            self._backoff.reset()
            self._log.info("QQ 网关已就绪：bot_id=%s", self._state.bot_id)
            await self._handler.on_ready(self._state.bot_id)
        elif event_type == "RESUMED":
            self._backoff.reset()
            self._log.info("QQ 网关已恢复会话")
            if self._state.bot_id:
                await self._handler.on_ready(self._state.bot_id)
        elif isinstance(data, dict):
            try:
                await self._handler.on_dispatch(event_type, data, str(frame.get("id") or ""))
            except Exception:
                self._log.exception("处理 QQ 事件 %s 出错", event_type)
        # 官方建议处理完事件再记录序号，Resume 时据此补发
        if isinstance(frame.get("s"), int):
            self._state.seq = frame["s"]

    @staticmethod
    async def _receive_json(ws: aiohttp.ClientWebSocketResponse, *, wait: float) -> dict[str, Any]:
        msg = await ws.receive(timeout=wait)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ValueError(f"网关没有发来文本帧：{msg.type}")
        return json.loads(msg.data)
