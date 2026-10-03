"""QQ 单聊消息 ⇄ MaiBot 消息字典 的转换，不依赖 MaiBot SDK，便于单测。

入站字典形状见 MaiBot host/message_utils.py：段列表键名是 raw_message；user_nickname 必须非空；
语音段的 data 填好转写文本后，宿主就不会再自己做语音识别。
出站时目标用户在 message_info.additional_config.platform_io_target_user_id。
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from ..common.errors import QQSuiteError
from ..qq.api import FileType
from ..qq.events import MESSAGE_TYPE_QUOTE, Attachment, C2CMessage, MessageElement
from ..speech import AudioClip
from ..speech.asr import ASRProvider

PLATFORM = "qq"
_TEXT_FALLBACK = {"video": "[视频]", "file": "[文件]"}
_QUOTE_MAX_CHARS = 300


def _attachment_label(attachment: Attachment) -> str:
    if attachment.kind == "image":
        return "[图片]"
    if attachment.kind == "voice":
        return f"[语音: {attachment.asr_refer_text}]" if attachment.asr_refer_text else "[语音]"
    label = _TEXT_FALLBACK.get(attachment.kind, "[文件]")
    return f"{label[:-1]}：{attachment.filename}]" if attachment.filename else label


def _element_text(element: MessageElement) -> str:
    parts = [element.content, *(_attachment_label(a) for a in element.attachments)]
    text = " ".join(p for p in parts if p)
    return text if len(text) <= _QUOTE_MAX_CHARS else text[:_QUOTE_MAX_CHARS] + "…"


def quote_text(message: C2CMessage) -> str:
    """引用消息里被引用的内容，写成「[引用：…]」放在用户这条消息前面，让模型知道指的是哪句。"""
    texts = [t for t in (_element_text(e) for e in message.elements) if t]
    if not texts:
        return ""
    label = "引用" if message.message_type == MESSAGE_TYPE_QUOTE else "附带消息"
    return f"[{label}：{' / '.join(texts)}]"


class MediaFetcher(Protocol):
    async def download(self, url: str) -> bytes: ...


@dataclass
class InboundBuilder:
    """把一条 QQ 私聊消息变成 MaiBot 入站消息字典。附件下载、语音识别的失败都降级成文字占位，不丢消息。"""

    fetcher: MediaFetcher
    asr: ASRProvider | None = None
    nickname_of: Callable[[str], str] = lambda openid: f"QQ用户{openid[:6]}"
    logger: logging.Logger = logging.getLogger(__name__)  # noqa: RUF009

    async def build(self, message: C2CMessage, bot_id: str) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        quoted = quote_text(message)
        if quoted:
            segments.append({"type": "text", "data": quoted + "\n"})
        if message.content:
            segments.append({"type": "text", "data": message.content})
        for attachment in message.attachments:
            segments.append(await self._attachment_segment(attachment))
        if not segments:
            segments.append({"type": "text", "data": "[空消息]"})
        kinds = {a.kind for a in message.attachments}
        return {
            "message_id": message.id,
            "platform": PLATFORM,
            "message_info": {
                "user_info": {
                    "user_id": message.user_openid,
                    "user_nickname": self.nickname_of(message.user_openid) or message.user_openid,
                    "user_cardname": None,
                },
                "group_info": None,
                "additional_config": {"platform_io_account_id": bot_id},
            },
            "raw_message": segments,
            "is_picture": not message.content and kinds == {"image"},
        }

    async def _attachment_segment(self, attachment: Attachment) -> dict[str, Any]:
        kind = attachment.kind
        if kind == "image":
            data = await self._try_download(attachment.url)
            if data is None:
                return {"type": "text", "data": "[图片]"}
            return {"type": "image", "data": "", "binary_data_base64": base64.b64encode(data).decode("ascii")}
        if kind == "voice":
            return await self._voice_segment(attachment)
        return {"type": "text", "data": _attachment_label(attachment)}

    async def _voice_segment(self, attachment: Attachment) -> dict[str, Any]:
        # 优先下载平台转好的 WAV，各家识别服务都认；原始语音通常是 SILK
        audio = await self._try_download(attachment.voice_wav_url) if attachment.voice_wav_url else None
        text = ""
        if audio is not None and self.asr is not None:
            started = time.monotonic()
            try:
                text = await self.asr.transcribe(AudioClip(audio, "audio/wav"))
            except QQSuiteError as exc:
                self.logger.warning("语音识别失败，改用 QQ 自带转写：%s", exc)
            else:
                self.logger.info(
                    "语音识别（%s）用时 %.1f 秒，%d 字", self.asr.name, time.monotonic() - started, len(text)
                )
        text = text or attachment.asr_refer_text
        segment: dict[str, Any] = {"type": "voice", "data": f"[语音: {text}]" if text else "[语音，没听清]"}
        if audio is not None:
            segment["binary_data_base64"] = base64.b64encode(audio).decode("ascii")
        return segment

    async def _try_download(self, url: str) -> bytes | None:
        if not url:
            return None
        try:
            return await self.fetcher.download(url)
        except QQSuiteError as exc:
            self.logger.warning("下载附件失败：%s", exc)
            return None


class QQSender(Protocol):
    async def send_text(self, openid: str, text: str) -> None: ...
    async def send_media(self, openid: str, file_type: FileType, data: bytes) -> None: ...


@dataclass
class OutboundDispatcher:
    """把 MaiBot 出站消息字典拆成 QQ 的文字 / 图片 / 语音逐条发送。"""

    sender: QQSender
    is_allowed: Callable[[str], bool] = lambda openid: True

    async def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        info = message.get("message_info") or {}
        extra = info.get("additional_config") or {}
        openid = str(extra.get("platform_io_target_user_id") or "")
        if not openid:
            if extra.get("platform_io_target_group_id"):
                return {"success": False, "error": "本插件只支持 QQ 私聊，不支持群聊"}
            return {"success": False, "error": "出站消息缺少目标用户"}
        if not self.is_allowed(openid):
            return {"success": False, "error": "目标用户不在白名单内"}
        sent = 0
        for action in _plan(message.get("raw_message") or []):
            await action(self.sender, openid)
            sent += 1
        if sent == 0:
            return {"success": False, "error": "没有可发送的内容"}
        return {"success": True}


SendAction = Callable[[QQSender, str], Awaitable[None]]


def _plan(segments: Iterable[Any]) -> list[SendAction]:
    """合并相邻文字，图片和语音各自单独发送，保持原有顺序。"""
    actions: list[SendAction] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "".join(buffer).strip()
        buffer.clear()
        if text:
            actions.append(lambda sender, openid, text=text: sender.send_text(openid, text))

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        if seg_type == "text":
            buffer.append(str(seg.get("data") or ""))
        elif seg_type in {"image", "emoji", "voice"} and seg.get("binary_data_base64"):
            flush()
            data = base64.b64decode(seg["binary_data_base64"])
            file_type = FileType.VOICE if seg_type == "voice" else FileType.IMAGE
            actions.append(lambda sender, openid, ft=file_type, d=data: sender.send_media(openid, ft, d))
        elif seg_type == "at":
            continue  # 私聊里 @ 没有意义
        elif seg_type == "reply":
            continue  # 被动回复已按 msg_id 关联，不再单独引用
    flush()
    return actions
