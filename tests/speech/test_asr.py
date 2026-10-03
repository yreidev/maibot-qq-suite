from __future__ import annotations

import base64

import aiohttp
import pytest
from aiohttp import web

from qq_suite.common.errors import ConfigError, ProviderError
from qq_suite.speech import AudioClip
from qq_suite.speech.asr import ASRSettings, build_asr
from qq_suite.speech.asr.mimo import MiMoASR
from qq_suite.speech.asr.openai_compat import OpenAITranscriptionASR

CLIP = AudioClip(b"RIFF....WAVEfake", "audio/wav")


async def test_openai_compat_uploads_multipart(fake_service):
    seen: dict = {}

    async def handler(request: web.Request) -> web.Response:
        form = await request.post()
        seen["auth"] = request.headers.get("Authorization")
        seen["model"] = form["model"]
        seen["file"] = form["file"].file.read()
        seen["filename"] = form["file"].filename
        return web.json_response({"text": "  你好呀 "})

    base, session, _ = await fake_service({("POST", "/v1/audio/transcriptions"): handler})
    asr = OpenAITranscriptionASR(session, base_url=base + "/v1", api_key="k", model="m1")
    assert await asr.transcribe(CLIP) == "你好呀"
    assert seen == {"auth": "Bearer k", "model": "m1", "file": CLIP.data, "filename": "voice.wav"}


async def test_openai_compat_http_error_is_provider_error(fake_service):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "bad key"}, status=401)

    base, session, _ = await fake_service({("POST", "/audio/transcriptions"): handler})
    asr = OpenAITranscriptionASR(session, base_url=base, api_key="k", model="m")
    with pytest.raises(ProviderError) as info:
        await asr.transcribe(CLIP)
    assert info.value.status == 401


async def test_mimo_asr_request_shape(fake_service):
    seen: dict = {}

    async def handler(request: web.Request) -> web.Response:
        seen["key"] = request.headers.get("api-key")
        seen["body"] = await request.json()
        return web.json_response({"choices": [{"message": {"content": "你好。"}}]})

    base, session, _ = await fake_service({("POST", "/v1/chat/completions"): handler})
    asr = MiMoASR(session, api_key="mk", base_url=base + "/v1", language="zh")
    assert await asr.transcribe(CLIP) == "你好。"
    body = seen["body"]
    assert seen["key"] == "mk"
    assert body["model"] == "mimo-v2.5-asr"
    assert body["asr_options"] == {"language": "zh"}
    data_uri = body["messages"][0]["content"][0]["input_audio"]["data"]
    assert data_uri == "data:audio/wav;base64," + base64.b64encode(CLIP.data).decode()


async def test_mimo_asr_bad_shape(fake_service):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"choices": []})

    base, session, _ = await fake_service({("POST", "/chat/completions"): handler})
    with pytest.raises(ProviderError):
        await MiMoASR(session, api_key="k", base_url=base).transcribe(CLIP)


async def test_factory():
    async with aiohttp.ClientSession() as session:
        assert build_asr(ASRSettings(), session) is None
        assert build_asr(ASRSettings(provider="QQ"), session) is None
        sf = build_asr(ASRSettings(provider="siliconflow", api_key="k"), session)
        assert sf is not None and sf.name == "siliconflow_asr"
        assert build_asr(ASRSettings(provider="mimo", api_key="k"), session).name == "mimo_asr"
        with pytest.raises(ConfigError):
            build_asr(ASRSettings(provider="siliconflow"), session)
        with pytest.raises(ConfigError):
            build_asr(ASRSettings(provider="openai", api_key="k"), session)
        with pytest.raises(ConfigError):
            build_asr(ASRSettings(provider="baidu", api_key="k"), session)
