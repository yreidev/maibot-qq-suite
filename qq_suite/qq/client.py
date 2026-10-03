"""QQ 单聊客户端门面：把鉴权、网关、OpenAPI、被动回复名额、附件下载组合起来。

对外只暴露「收到私聊消息」回调和「发文字 / 发语音 / 发图片 / 正在输入」方法，不知道 MaiBot 的存在。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..common.errors import ProviderError
from .api import API_BASE, FileType, QQAPIError, QQOpenAPI
from .auth import TOKEN_URL, TokenProvider
from .events import C2CMessage
from .gateway import INTENT_C2C, GatewayClient
from .media import download
from .replies import ReplyTarget, ReplyTracker
from .text import QQ_TEXT_LIMIT, split_text, strip_markdown

MessageHandler = Callable[[C2CMessage], Awaitable[None]]
StateHandler = Callable[[bool, str], Awaitable[None]]  # (是否在线, 机器人自身 ID)


@dataclass(frozen=True)
class QQSettings:
    app_id: str
    app_secret: str
    api_base: str = API_BASE
    token_url: str = TOKEN_URL
    intents: int = INTENT_C2C
    markdown: bool = False  # True：用 Markdown 消息发文字；False：去掉 Markdown 符号后按普通文字发
    max_message_chars: int = 2000  # 单条文字的长度，超过就切成多条
    typing_indicator: bool = False  # 收到消息后发「正在输入」


def _short(openid: str) -> str:
    return openid[:6] + "…" if len(openid) > 6 else openid


class QQBotClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: QQSettings,
        *,
        on_message: MessageHandler,
        on_state: StateHandler | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._log = logger or logging.getLogger(__name__)
        self._session = session
        self._settings = settings
        self._tokens = TokenProvider(
            session, app_id=settings.app_id, app_secret=settings.app_secret, token_url=settings.token_url
        )
        self.api = QQOpenAPI(session, self._tokens, base_url=settings.api_base)
        self._gateway = GatewayClient(session, self.api, self._tokens, self, intents=settings.intents, logger=self._log)
        self._replies = ReplyTracker()
        self._on_message = on_message
        self._on_state = on_state
        self._seen: OrderedDict[str, None] = OrderedDict()  # 平台可能重复推送同一条消息
        self._task: asyncio.Task[None] | None = None
        self._markdown = settings.markdown
        self._typing_failed = False

    @property
    def bot_id(self) -> str:
        return self._gateway.bot_id

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._gateway.run_forever(), name="qq-gateway")

    async def stop(self) -> None:
        await self._gateway.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ---- GatewayHandler ----
    async def on_ready(self, bot_id: str) -> None:
        if self._on_state:
            await self._on_state(True, bot_id)

    async def on_disconnected(self, reason: str) -> None:
        if self._on_state:
            await self._on_state(False, self.bot_id)

    async def on_dispatch(self, event_type: str, data: dict[str, Any], event_id: str) -> None:
        if event_type != "C2C_MESSAGE_CREATE":
            self._log.debug("忽略 QQ 事件 %s", event_type)
            return
        message = C2CMessage.from_payload(data)
        if not message.id or not message.user_openid or message.id in self._seen:
            return
        self._seen[message.id] = None
        while len(self._seen) > 2000:
            self._seen.popitem(last=False)
        self._replies.record_inbound(message.user_openid, message.id)
        self._log.debug(
            "收到私聊 %s：类型 %s，文字 %d 字，附件 %s，引用/附带 %d 条",
            _short(message.user_openid),
            message.message_type,
            len(message.content),
            [a.kind for a in message.attachments] or "无",
            len(message.elements),
        )
        await self._on_message(message)

    # ---- 发送 ----
    def _target(self, openid: str, what: str) -> ReplyTarget:
        target = self._replies.next_target(openid)
        if target.msg_id is None:
            self._log.warning(
                "给 %s 的被动回复名额已用完（每条消息 4 次、1 小时内），%s改发主动消息，可能失败", _short(openid), what
            )
        return target

    async def send_text(self, openid: str, text: str) -> None:
        if not self._markdown:
            text = strip_markdown(text)
        chunks = split_text(text, self._settings.max_message_chars)
        slots = self._replies.available(openid)
        if 0 < slots < len(chunks):
            # 名额不够发这么多条：放宽到单条上限，尽量少占名额
            chunks = split_text(text, QQ_TEXT_LIMIT)
        for index, chunk in enumerate(chunks, 1):
            target = self._target(openid, "文字")
            self._log.debug(
                "发文字给 %s：第 %d/%d 条，%d 字，msg_seq=%d",
                _short(openid),
                index,
                len(chunks),
                len(chunk),
                target.msg_seq,
            )
            await self._send_chunk(openid, chunk, target)

    async def _send_chunk(self, openid: str, chunk: str, target: ReplyTarget) -> None:
        if not self._markdown:
            await self.api.send_text(openid, chunk, msg_id=target.msg_id, msg_seq=target.msg_seq)
            return
        try:
            await self.api.send_markdown(openid, chunk, msg_id=target.msg_id, msg_seq=target.msg_seq)
        except QQAPIError as exc:
            # 没有 Markdown 权限等：本次运行改回普通文字，免得每条都先失败一次
            self._markdown = False
            self._log.warning("Markdown 消息发送失败，本次运行改用普通文字：%s", exc)
            retry = self._target(openid, "文字")
            await self.api.send_text(openid, strip_markdown(chunk), msg_id=retry.msg_id, msg_seq=retry.msg_seq)

    async def send_media(self, openid: str, file_type: FileType, data: bytes) -> None:
        kind = {FileType.IMAGE: "图片", FileType.VOICE: "语音", FileType.VIDEO: "视频"}.get(file_type, "文件")
        started = asyncio.get_running_loop().time()
        file_info = await self.api.upload_media(openid, file_type, data)
        uploaded = asyncio.get_running_loop().time()
        target = self._target(openid, kind)
        await self.api.send_media(openid, file_info, msg_id=target.msg_id, msg_seq=target.msg_seq)
        self._log.info(
            "发%s给 %s：%d KB，上传 %.1f 秒，发送 %.1f 秒，msg_seq=%d",
            kind,
            _short(openid),
            len(data) // 1024,
            uploaded - started,
            asyncio.get_running_loop().time() - uploaded,
            target.msg_seq,
        )

    async def notify_typing(self, openid: str) -> None:
        """显示「正在输入」（实验功能，默认关闭）。占用一次被动回复名额；没有名额或失败时什么都不做。"""
        if not self._settings.typing_indicator:
            return
        target = self._replies.take_passive(openid)
        if target is None or target.msg_id is None:
            return
        try:
            # 只是提示，不能拖慢消息转交，最多等 3 秒
            await asyncio.wait_for(self.api.send_input_notify(openid, msg_id=target.msg_id, msg_seq=target.msg_seq), 3)
        except (TimeoutError, ProviderError, aiohttp.ClientError) as exc:
            log = self._log.debug if self._typing_failed else self._log.warning
            self._typing_failed = True
            log("「正在输入」发送失败（可在配置里关掉）：%s", exc)

    async def download(self, url: str) -> bytes:
        return await download(self._session, url)
