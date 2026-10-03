"""小米 MiMo-V2.5 语音合成，三种模式：

- preset  预置音色（mimo-v2.5-tts），voice 填音色 ID，如 茉莉、冰糖
- design  音色设计（mimo-v2.5-tts-voicedesign），用文字描述生成音色，不传 voice
- clone   音色复刻（mimo-v2.5-tts-voiceclone），voice 传参考录音的 data URI

接口约定（https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/speech-synthesis-v2.5）：
要合成的文字放在 assistant 消息里；user 消息放语气指令，design 模式下放音色描述（必填）。
输出格式：接口支持 wav / mp3 / pcm / pcm16（文档只写了 wav、pcm16，2026-10 实测 mp3 可用，体积约为 wav 的 1/6）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

import aiohttp

from ...common.errors import ConfigError, ProviderError
from ...common.http import DEFAULT_TIMEOUT, read_json
from .. import AudioClip

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
_MODELS = {
    "preset": "mimo-v2.5-tts",
    "design": "mimo-v2.5-tts-voicedesign",
    "clone": "mimo-v2.5-tts-voiceclone",
}
_MIME = {"wav": "audio/wav", "mp3": "audio/mpeg"}
_MAX_CLONE_B64 = 10 * 1024 * 1024

Mode = Literal["preset", "design", "clone"]
AudioFormat = Literal["wav", "mp3"]


@dataclass(frozen=True)
class VoiceProfile:
    mode: Mode = "design"
    voice: str = "茉莉"  # preset 模式的音色 ID
    design_prompt: str = ""  # design 模式的音色描述
    clone_sample: AudioClip | None = None  # clone 模式的参考录音


class MiMoTTS:
    name = "mimo_tts"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str,
        profile: VoiceProfile,
        base_url: str = MIMO_BASE_URL,
        audio_format: AudioFormat = "wav",
    ) -> None:
        if audio_format not in _MIME:
            raise ConfigError(f"不支持的音频格式：{audio_format}")
        if profile.mode not in _MODELS:
            raise ConfigError(f"不认识的音色模式：{profile.mode}")
        if profile.mode == "design" and not profile.design_prompt.strip():
            raise ConfigError("音色设计模式需要填音色描述（design_prompt）")
        if profile.mode == "clone":
            if profile.clone_sample is None:
                raise ConfigError("音色复刻模式需要提供参考录音")
            if profile.clone_sample.mime_type not in {"audio/wav", "audio/mpeg", "audio/mp3"}:
                raise ConfigError("参考录音只支持 wav 或 mp3")
        self._session = session
        self._base_url = base_url
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._profile = profile
        self._format = audio_format

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def with_format(self, audio_format: AudioFormat) -> MiMoTTS:
        """同样的音色配置，换一种输出格式。"""
        return MiMoTTS(
            self._session,
            api_key=self._api_key,
            profile=self._profile,
            base_url=self._base_url,
            audio_format=audio_format,
        )

    def build_request(self, text: str, style: str = "") -> dict:
        profile = self._profile
        audio: dict[str, str] = {"format": self._format}
        if profile.mode == "preset":
            audio["voice"] = profile.voice
            instruction = style
        elif profile.mode == "design":
            # 音色描述必填；语气指令接在描述后面
            instruction = profile.design_prompt.strip() + (f"\n{style}" if style else "")
        else:
            sample = profile.clone_sample
            assert sample is not None
            encoded = base64.b64encode(sample.data).decode("ascii")
            if len(encoded) > _MAX_CLONE_B64:
                raise ConfigError("参考录音 base64 后超过 10MB")
            audio["voice"] = f"data:{sample.mime_type};base64,{encoded}"
            instruction = style
        return {
            "model": _MODELS[profile.mode],
            "messages": [
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": text},
            ],
            "audio": audio,
        }

    async def synthesize(self, text: str, *, style: str = "") -> AudioClip:
        if not text.strip():
            raise ProviderError(self.name, "没有要合成的文字")
        body = self.build_request(text, style)
        async with self._session.post(
            self._url, json=body, headers={"api-key": self._api_key}, timeout=DEFAULT_TIMEOUT
        ) as resp:
            payload = await read_json(resp, self.name)
        try:
            data = payload["choices"][0]["message"]["audio"]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, "返回里没有音频") from exc
        try:
            audio = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderError(self.name, "音频不是合法的 base64") from exc
        if not audio:
            raise ProviderError(self.name, "返回的音频为空")
        return AudioClip(audio, _MIME[self._format])
