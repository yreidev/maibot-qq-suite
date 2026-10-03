from __future__ import annotations

from qq_suite.host.portrait import handle_portrait, is_portrait_command
from qq_suite.image import PhotoAlbum, Picture
from qq_suite.qq.events import C2CMessage


def _msg(text: str, quoted_ref: str = "") -> C2CMessage:
    return C2CMessage(id="M", user_openid="U", content=text, timestamp="", quoted_ref=quoted_ref)


def _append(target):
    async def push(value):
        target.append(value)

    return push


def test_is_portrait_command():
    assert is_portrait_command(_msg("/定妆照")) and is_portrait_command(_msg(" /定妆照 2 "))
    assert not is_portrait_command(_msg("/定妆照好看")) and not is_portrait_command(_msg("定妆照"))


async def test_portrait_flow(tmp_path):
    album = PhotoAlbum(tmp_path, clock=lambda: 1_791_000_000.0)  # 时钟定住：几张照片同一时刻拍，不依赖机器快慢
    replies: list[str] = []
    photos: list[bytes] = []

    async def run(text, quoted_ref=""):
        await handle_portrait(_msg(text, quoted_ref), album, reply=_append(replies), send_photo=_append(photos))
        return replies[-1] if replies else None

    assert "还没有拍过照片" in await run("/定妆照")
    for name in (b"one", b"two", b"three"):
        album.save(Picture(name, "image/jpeg"))
    assert album.remember_sent(b"one", "REFIDX_1") and album.remember_sent(b"three", "REFIDX_3")
    assert not album.remember_sent(b"emoji", "REFIDX_x")  # 不是拍的照片：不记

    assert "引用的那张" in await run("/定妆照", quoted_ref="REFIDX_1")
    assert album.reference() == Picture(b"one", "image/jpeg")
    assert "没认出" in await run("/定妆照", quoted_ref="REFIDX_unknown")
    assert album.reference() == Picture(b"one", "image/jpeg")  # 认不出时不改

    assert "倒数第 2 张" in await run("/定妆照 2")
    assert album.reference() == Picture(b"two", "image/jpeg")
    assert "最近一张" in await run("/定妆照")
    assert album.reference() == Picture(b"three", "image/jpeg")
    assert await run("/定妆照 9") == "只存了最近 3 张照片，没有倒数第 9 张"

    count = len(replies)
    await run("/定妆照 看")
    assert photos == [b"three"] and len(replies) == count
    assert await run("/定妆照 清除") == "已取消定妆照"
    assert "还没有定妆照" in await run("/定妆照 看")
    assert (await run("/定妆照 乱写")).startswith("用法")
