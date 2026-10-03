from __future__ import annotations

import base64

import aiohttp
import pytest
from aiohttp import web

from qq_suite.common.errors import ConfigError, ProviderError
from qq_suite.speech import AudioClip
from qq_suite.speech.tts import TTSSettings, VoiceProfile, build_tts
from qq_suite.speech.tts.mimo import MiMoTTS

WAV = b"RIFF\x00\x00\x00\x00WAVEdata"


def _tts(session, **profile) -> MiMoTTS:
    return MiMoTTS(session, api_key="k", profile=VoiceProfile(**profile), base_url="http://unused")


async def test_build_request_modes():
    async with aiohttp.ClientSession() as session:
        preset = _tts(session, mode="preset", voice="冰糖").build_request("晚安", "温柔地说")
        assert preset["model"] == "mimo-v2.5-tts"
        assert preset["audio"] == {"format": "wav", "voice": "冰糖"}
        assert preset["messages"] == [
            {"role": "user", "content": "温柔地说"},
            {"role": "assistant", "content": "晚安"},
        ]

        design = _tts(session, mode="design", design_prompt="沉稳男声").build_request("晚安", "低声")
        assert design["model"] == "mimo-v2.5-tts-voicedesign"
        assert "voice" not in design["audio"]
        assert design["messages"][0]["content"] == "沉稳男声\n低声"

        sample = AudioClip(b"abc", "audio/mpeg")
        clone = _tts(session, mode="clone", clone_sample=sample).build_request("晚安")
        assert clone["model"] == "mimo-v2.5-tts-voiceclone"
        assert clone["audio"]["voice"] == "data:audio/mpeg;base64," + base64.b64encode(b"abc").decode()


async def test_profile_validation():
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ConfigError):
            _tts(session, mode="design", design_prompt="  ")
        with pytest.raises(ConfigError):
            _tts(session, mode="clone")
        with pytest.raises(ConfigError):
            _tts(session, mode="clone", clone_sample=AudioClip(b"x", "audio/ogg"))
        with pytest.raises(ConfigError):
            _tts(session, mode="sing")


async def test_synthesize_decodes_audio(fake_service):
    async def handler(request: web.Request) -> web.Response:
        assert request.headers["api-key"] == "k"
        audio = base64.b64encode(WAV).decode()
        return web.json_response({"choices": [{"message": {"audio": {"data": audio}}}]})

    base, session, calls = await fake_service({("POST", "/v1/chat/completions"): handler})
    tts = MiMoTTS(session, api_key="k", profile=VoiceProfile(mode="preset"), base_url=base + "/v1")
    clip = await tts.synthesize("你好")
    assert clip == AudioClip(WAV, "audio/wav")
    assert len(calls) == 1


async def test_synthesize_errors(fake_service):
    async def no_audio(request: web.Request) -> web.Response:
        return web.json_response({"choices": [{"message": {"content": "x"}}]})

    base, session, _ = await fake_service({("POST", "/chat/completions"): no_audio})
    tts = MiMoTTS(session, api_key="k", profile=VoiceProfile(mode="preset"), base_url=base)
    with pytest.raises(ProviderError):
        await tts.synthesize("你好")
    with pytest.raises(ProviderError):
        await tts.synthesize("   ")


async def test_factory():
    async with aiohttp.ClientSession() as session:
        assert build_tts(TTSSettings(api_key=""), session) is None
        assert build_tts(TTSSettings(provider="none", api_key="k"), session) is None
        made = build_tts(TTSSettings(api_key="k", mode="preset"), session)
        assert made is not None and made.name == "mimo_tts"
        with pytest.raises(ConfigError):
            build_tts(TTSSettings(provider="azure", api_key="k"), session)


async def test_audio_format_and_with_format(fake_service):
    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        audio = b"\xff\xf3MP3" if body["audio"]["format"] == "mp3" else WAV
        return web.json_response({"choices": [{"message": {"audio": {"data": base64.b64encode(audio).decode()}}}]})

    base, session, _ = await fake_service({("POST", "/v1/chat/completions"): handler})
    made = build_tts(TTSSettings(api_key="k", mode="preset", base_url=base + "/v1"), session)
    assert made.audio_format == "mp3"  # 默认 MP3
    assert made.build_request("晚安")["audio"]["format"] == "mp3"
    assert await made.synthesize("晚安") == AudioClip(b"\xff\xf3MP3", "audio/mpeg")
    wav = made.with_format("wav")
    assert await wav.synthesize("晚安") == AudioClip(WAV, "audio/wav")  # 同一个接口地址
    with pytest.raises(ConfigError):
        MiMoTTS(session, api_key="k", profile=VoiceProfile(mode="preset"), audio_format="ogg")
