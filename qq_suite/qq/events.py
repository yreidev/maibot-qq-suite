"""单聊入站事件的数据结构。

C2C_MESSAGE_CREATE：https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html
消息类型（含引用消息）：https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/overview.html
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Attachment:
    content_type: str
    url: str
    filename: str = ""
    size: int = 0
    voice_wav_url: str = ""
    asr_refer_text: str = ""

    @property
    def kind(self) -> str:
        """image | voice | video | file"""
        ct = self.content_type.lower()
        if ct == "voice" or ct.startswith("audio/"):
            return "voice"
        if ct.startswith("image/"):
            return "image"
        if ct.startswith("video/"):
            return "video"
        return "file"

    @staticmethod
    def from_payload(raw: dict[str, Any]) -> Attachment:
        def url_of(key: str) -> str:
            value = str(raw.get(key) or "").strip()
            # 附件地址偶尔不带协议头
            return f"https://{value}" if value and "://" not in value else value

        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return Attachment(
            content_type=str(raw.get("content_type") or ""),
            url=url_of("url"),
            filename=str(raw.get("filename") or ""),
            size=size,
            voice_wav_url=url_of("voice_wav_url"),
            asr_refer_text=str(raw.get("asr_refer_text") or "").strip(),
        )


def _attachments(raw: Any) -> tuple[Attachment, ...]:
    return tuple(Attachment.from_payload(a) for a in raw or () if isinstance(a, dict))


# message_type：0 文本 / 3 ARK 卡片 / 101 并行消息 / 102 聊天记录 / 103 引用消息（被引用的内容在 msg_elements）
MESSAGE_TYPE_QUOTE = 103
# 被引用消息的编号：message_scene.ext 里的 "ref_msg_idx=REFIDX_…"，和机器人发消息时返回的 ext_info.ref_idx 是同一个值。
# 官方文档里没找到这段说明，依据是多个开源实现（cohub、nekro-agent 的 QQ 适配器）的做法，未确认
_MAX_ELEMENTS = 5


@dataclass(frozen=True)
class MessageElement:
    """msg_elements 里的一条：引用消息时就是被引用的那条。"""

    content: str
    attachments: tuple[Attachment, ...] = ()
    msg_idx: str = ""


def _scene_ext(raw: Any) -> dict[str, str]:
    """message_scene.ext 是 ["k=v", ...] 形式的列表。"""
    ext = raw.get("ext") if isinstance(raw, dict) else None
    result: dict[str, str] = {}
    for item in ext if isinstance(ext, list) else ():
        key, sep, value = str(item).partition("=")
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result


def _elements(raw: Any, depth: int = 0) -> list[MessageElement]:
    """msg_elements 可以递归嵌套（引用了一条引用消息），展平成列表，限制层数和条数。"""
    result: list[MessageElement] = []
    if depth > 2 or not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        result.append(
            MessageElement(
                str(item.get("content") or "").strip(),
                _attachments(item.get("attachments")),
                str(item.get("msg_idx") or ""),
            )
        )
        result.extend(_elements(item.get("msg_elements"), depth + 1))
    return result[:_MAX_ELEMENTS]


@dataclass(frozen=True)
class C2CMessage:
    id: str  # 被动回复用的 msg_id，也是去重键
    user_openid: str
    content: str
    timestamp: str
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    message_type: int = 0
    elements: tuple[MessageElement, ...] = field(default_factory=tuple)
    quoted_ref: str = ""  # 被引用消息的编号，没引用时为空

    @staticmethod
    def from_payload(d: dict[str, Any]) -> C2CMessage:
        author = d.get("author") or {}
        openid = str(author.get("user_openid") or author.get("id") or "")
        try:
            message_type = int(d.get("message_type") or 0)
        except (TypeError, ValueError):
            message_type = 0
        elements = tuple(_elements(d.get("msg_elements")))
        quoted_ref = _scene_ext(d.get("message_scene")).get("ref_msg_idx", "")
        if not quoted_ref and message_type == MESSAGE_TYPE_QUOTE and elements:
            quoted_ref = elements[0].msg_idx
        return C2CMessage(
            id=str(d.get("id") or ""),
            user_openid=openid,
            content=str(d.get("content") or "").strip(),
            timestamp=str(d.get("timestamp") or ""),
            attachments=_attachments(d.get("attachments")),
            message_type=message_type,
            elements=elements,
            quoted_ref=quoted_ref,
        )
