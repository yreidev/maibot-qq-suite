"""根据配置构造语音合成服务商。provider 为空或 none 时返回 None，表示不启用语音回复。"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from ...common.errors import ConfigError
from .. import AudioClip
from .base import TTSProvider
from .mimo import MIMO_BASE_URL, MiMoTTS, VoiceProfile


@dataclass(frozen=True)
class TTSSettings:
    provider: str = "mimo"  # mimo | none
    api_key: str = ""
    base_url: str = ""
    mode: str = "design"  # preset | design | clone
    voice: str = "茉莉"
    design_prompt: str = ""
    clone_sample: AudioClip | None = None
    audio_format: str = "mp3"  # mp3 | wav


def build_tts(settings: TTSSettings, session: aiohttp.ClientSession) -> TTSProvider | None:
    provider = settings.provider.strip().lower()
    if provider in {"", "none"} or not settings.api_key:
        return None
    if provider != "mimo":
        raise ConfigError(f"不认识的语音合成服务商：{settings.provider}")
    profile = VoiceProfile(
        mode=settings.mode,  # type: ignore[arg-type]  由 MiMoTTS 校验
        voice=settings.voice,
        design_prompt=settings.design_prompt,
        clone_sample=settings.clone_sample,
    )
    return MiMoTTS(
        session,
        api_key=settings.api_key,
        profile=profile,
        base_url=settings.base_url or MIMO_BASE_URL,
        audio_format=settings.audio_format,  # type: ignore[arg-type]  由 MiMoTTS 校验
    )
