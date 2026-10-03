from __future__ import annotations

import base64
import time

from qq_suite.common.errors import ProviderError
from qq_suite.host.photos import PhotoStudio, build_prompt
from qq_suite.image import PhotoAlbum, Picture


class FakePainter:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Picture]]] = []
        self.fail = False

    async def generate(self, prompt, *, references=()):
        if self.fail:
            raise ProviderError("fake", "moderation_blocked")
        self.calls.append((prompt, list(references)))
        return Picture(f"photo{len(self.calls)}".encode(), "image/jpeg")


def _studio(tmp_path, *, sent=None, ok=True, **kwargs):
    clock = [time.mktime((2026, 10, 3, 20, 0, 0, 0, 0, -1))]
    album = PhotoAlbum(tmp_path, clock=lambda: clock[0])

    async def send_image(b64, stream_id):
        if sent is not None:
            sent.append((base64.b64decode(b64), stream_id))
        return ok

    painter = FakePainter()
    studio = PhotoStudio(painter, album, send_image, appearance="黑色长发", style="写实照片", **kwargs)
    return studio, painter, album, clock


def test_build_prompt():
    text = build_prompt(
        "在喝咖啡", style="写实", appearance="长发", selfie=True, with_portrait=True, with_previous=True
    )
    assert text.splitlines()[0] == "画风：写实"
    assert "照片里的人物：长发" in text and "定妆照" in text and "上一张照片" in text
    assert text.endswith("画面：在喝咖啡")
    scenery = build_prompt("晚霞", style="", appearance="长发", selfie=False, with_portrait=False, with_previous=False)
    assert scenery == "画面：晚霞"


async def test_reference_choice(tmp_path):
    sent: list = []
    studio, painter, album, clock = _studio(tmp_path, sent=sent)
    r = await studio.take("在书房敲代码", "S1")
    assert r == {"success": True, "content": "已发送照片：在书房敲代码"}
    assert painter.calls[-1][1] == [] and sent == [(b"photo1", "S1")]

    await studio.take("在厨房", "S1")  # 没有定妆照：不参考上一张，方便重拍挑长相
    assert painter.calls[-1][1] == []

    album.set_reference_from_latest()  # 定妆照 = photo2
    await studio.take("在阳台", "S1")
    prompt, refs = painter.calls[-1]
    assert refs == [Picture(b"photo2", "image/jpeg")] and "定妆照" in prompt and "上一张" not in prompt

    clock[0] += 60
    await studio.take("换个姿势", "S1", follow_previous=True)
    prompt, refs = painter.calls[-1]
    assert refs == [Picture(b"photo2", "image/jpeg"), Picture(b"photo3", "image/jpeg")] and "上一张照片" in prompt

    await studio.take("窗外的晚霞", "S1", selfie=False)
    prompt, refs = painter.calls[-1]
    assert refs == [] and "黑色长发" not in prompt

    clock[0] += 4 * 3600  # 超过连贯时长：不再参考上一张
    await studio.take("再来一张", "S1", follow_previous=True)
    assert painter.calls[-1][1] == [Picture(b"photo2", "image/jpeg")]


async def test_limits_and_failures(tmp_path):
    studio, _, _, _ = _studio(tmp_path, daily_limit=1)
    assert (await studio.take("  ", "S1"))["error"] == "没有说要拍什么"
    assert (await studio.take("x", ""))["error"] == "找不到当前聊天"
    assert (await studio.take("x", "S1"))["success"] is True
    assert "额度用完" in (await studio.take("x", "S1"))["error"]

    failing, painter, _, _ = _studio(tmp_path / "b")
    painter.fail = True
    assert "moderation_blocked" in (await failing.take("x", "S1"))["error"]

    unsent, _, _, _ = _studio(tmp_path / "c", ok=False)
    assert (await unsent.take("x", "S1"))["error"] == "照片拍好了，但发送失败"
