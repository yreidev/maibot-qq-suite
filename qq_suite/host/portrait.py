"""聊天命令 /定妆照：由插件在收到 QQ 消息时直接处理，不经过 MaiBot 的命令系统。

原因：用户引用一张照片再发 /定妆照 时，要用到 QQ 消息里被引用消息的编号，MaiBot 的命令拿不到；
而且引用消息的文字前面会加「[引用：…]」，MaiBot 按「以 / 开头」匹配命令也会匹配不上。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from ..image import PhotoAlbum
from ..qq.events import C2CMessage

_PATTERN = re.compile(r"^/定妆照(?:\s+(\S+))?\s*$")
USAGE = "用法：引用一张照片再发 /定妆照；或 /定妆照（最近一张）、/定妆照 2（倒数第 2 张）、/定妆照 看、/定妆照 清除"


def is_portrait_command(message: C2CMessage) -> bool:
    return bool(_PATTERN.match(message.content.strip()))


async def handle_portrait(
    message: C2CMessage,
    album: PhotoAlbum,
    *,
    reply: Callable[[str], Awaitable[object]],
    send_photo: Callable[[bytes], Awaitable[object]],
) -> None:
    match = _PATTERN.match(message.content.strip())
    action = (match.group(1) if match else "") or ""
    if action in {"看", "查看"}:
        portrait = album.reference()
        if portrait is None:
            await reply("还没有定妆照。先让机器人拍一张满意的照片，再引用它发 /定妆照")
        else:
            await send_photo(portrait.data)
        return
    if action in {"清除", "取消", "删除"}:
        await reply("已取消定妆照" if album.clear_reference() else "本来就没有定妆照")
        return
    if action and not action.isdigit():
        await reply(USAGE)
        return
    if message.quoted_ref and not action:
        if album.set_reference_by_ref(message.quoted_ref):
            await reply("已把你引用的那张设为定妆照，之后拍它自己都会照这个长相")
        else:
            await reply("没认出你引用的是哪张：只认得机器人最近拍的照片。也可以发 /定妆照 2 选倒数第 2 张")
        return
    nth = int(action or 1)
    if album.set_reference(nth):
        which = "最近一张照片" if nth == 1 else f"倒数第 {nth} 张照片"
        await reply(f"已把{which}设为定妆照，之后拍它自己都会照这个长相")
    elif album.recent_count() == 0:
        await reply("还没有拍过照片，先让机器人拍一张")
    else:
        await reply(f"只存了最近 {album.recent_count()} 张照片，没有倒数第 {nth} 张")
