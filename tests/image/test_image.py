from __future__ import annotations

import base64
import time

import aiohttp
import pytest
from aiohttp import web

from qq_suite.common.errors import ConfigError, ProviderError
from qq_suite.image import ImageSettings, OpenAIImageGenerator, PhotoAlbum, Picture, build_image

JPEG = b"\xff\xd8\xff\xe0JPEG"


def _ok(data: bytes = JPEG, **extra) -> web.Response:
    return web.json_response({"created": 1, "data": [{"b64_json": base64.b64encode(data).decode()}], **extra})


async def test_generate_without_references(fake_service):
    async def generations(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer k"
        body = await request.json()
        assert body == {
            "model": "gpt-image-2",
            "prompt": "一只猫",
            "n": 1,
            "size": "1024x1536",
            "quality": "medium",
            "output_format": "jpeg",
            "output_compression": 90,
        }
        return _ok(output_format="jpeg")

    base, session, _ = await fake_service({("POST", "/v1/images/generations"): generations})
    painter = OpenAIImageGenerator(session, api_key="k", base_url=base + "/v1/", model="gpt-image-2")
    assert await painter.generate("一只猫") == Picture(JPEG, "image/jpeg")


@pytest.mark.parametrize(("model", "fidelity"), [("gpt-image-2", None), ("gpt-image-2.5-flare", "high")])
async def test_edit_with_references(fake_service, model, fidelity):
    seen = {}

    async def edits(request: web.Request) -> web.Response:
        form = await request.post()
        seen["fields"] = {k: v for k, v in form.items() if isinstance(v, str)}
        seen["images"] = [(f.content_type, f.file.read()) for f in form.getall("image[]")]
        return _ok()

    base, session, _ = await fake_service({("POST", "/v1/images/edits"): edits})
    painter = OpenAIImageGenerator(session, api_key="k", base_url=base + "/v1", model=model, quality="high")
    refs = [Picture(b"PNG", "image/png"), Picture(b"JPG", "image/jpeg")]
    await painter.generate("她在喝咖啡", references=refs)
    assert seen["images"] == [("image/png", b"PNG"), ("image/jpeg", b"JPG")]
    fields = seen["fields"]
    assert (fields["model"], fields["prompt"], fields["quality"], fields["n"]) == (model, "她在喝咖啡", "high", "1")
    assert fields.get("input_fidelity") == fidelity  # gpt-image-2 会忽略这个参数，不发


async def test_url_response_and_errors(fake_service):
    async def generations(request: web.Request) -> web.Response:
        prompt = (await request.json())["prompt"]
        if prompt == "url":
            return web.json_response({"data": [{"url": str(request.url.with_path("/files/a.png"))}]})
        if prompt == "empty":
            return web.json_response({"data": []})
        return web.json_response({"error": {"code": "moderation_blocked", "message": "blocked"}}, status=400)

    async def file(request: web.Request) -> web.Response:
        return web.Response(body=b"PNGDATA", content_type="image/png")

    routes = {("POST", "/v1/images/generations"): generations, ("GET", "/files/a.png"): file}
    base, session, _ = await fake_service(routes)
    painter = OpenAIImageGenerator(session, api_key="k", base_url=base + "/v1", model="gpt-image-2")
    assert await painter.generate("url") == Picture(b"PNGDATA", "image/png")
    with pytest.raises(ProviderError, match="没有图片"):
        await painter.generate("empty")
    with pytest.raises(ProviderError, match="moderation_blocked"):
        await painter.generate("bad")
    with pytest.raises(ProviderError):
        await painter.generate("  ")


async def test_factory_and_validation():
    async with aiohttp.ClientSession() as session:
        assert build_image(ImageSettings(), session) is None
        assert build_image(ImageSettings("openai", "https://x/v1", ""), session) is None
        assert build_image(ImageSettings("openai", "", "k"), session) is None
        assert build_image(ImageSettings("openai", "https://x/v1", "k"), session).name == "openai_images"
        with pytest.raises(ConfigError):
            build_image(ImageSettings("openai", "ftp://x", "k"), session)
        with pytest.raises(ConfigError):
            build_image(ImageSettings("midjourney", "https://x", "k"), session)


class Clock:
    def __init__(self) -> None:
        self.now = time.mktime((2026, 10, 3, 21, 0, 0, 0, 0, -1))

    def __call__(self) -> float:
        return self.now


def test_album(tmp_path):
    clock = Clock()
    album = PhotoAlbum(tmp_path, keep=3, clock=clock)
    assert album.latest() is None and album.reference() is None and not album.set_reference_from_latest()
    for i in range(4):
        album.save(Picture(f"p{i}".encode(), "image/jpeg"))
        clock.now += 60
    assert len(list((tmp_path / "history").iterdir())) == 3  # 只留最近 3 张
    assert album.latest() == Picture(b"p3", "image/jpeg")
    assert album.count_today() == 3
    clock.now += 3600
    assert album.latest(max_age=1800) is None and album.latest(max_age=7200) is not None
    assert album.set_reference_from_latest() and album.reference() == Picture(b"p3", "image/jpeg")
    album.save(Picture(b"png", "image/png"))
    assert album.set_reference_from_latest() and album.reference() == Picture(b"png", "image/png")
    assert [p.name for p in tmp_path.glob("reference.*")] == ["reference.png"]  # 换定妆照不留旧的
    assert album.clear_reference() and album.reference() is None and not album.clear_reference()
    clock.now += 86400
    assert album.count_today() == 0
