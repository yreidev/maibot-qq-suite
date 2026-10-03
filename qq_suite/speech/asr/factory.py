"""根据配置构造语音识别服务商。provider 为 qq（或未配置）时返回 None，表示直接用 QQ 自带的转写文字。"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from ...common.errors import ConfigError
from .base import ASRProvider
from .mimo import MIMO_BASE_URL, MiMoASR
from .openai_compat import OpenAITranscriptionASR

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"


@dataclass(frozen=True)
class ASRSettings:
    provider: str = "qq"  # qq | siliconflow | mimo | openai
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    language: str = "auto"


def build_asr(settings: ASRSettings, session: aiohttp.ClientSession) -> ASRProvider | None:
    provider = settings.provider.strip().lower()
    if provider in {"", "qq"}:
        return None
    if not settings.api_key:
        raise ConfigError(f"语音识别选了 {provider}，但没填 api_key")
    if provider == "siliconflow":
        return OpenAITranscriptionASR(
            session,
            base_url=settings.base_url or SILICONFLOW_BASE_URL,
            api_key=settings.api_key,
            model=settings.model or SILICONFLOW_DEFAULT_MODEL,
            name="siliconflow_asr",
        )
    if provider == "mimo":
        return MiMoASR(
            session,
            api_key=settings.api_key,
            base_url=settings.base_url or MIMO_BASE_URL,
            model=settings.model or "mimo-v2.5-asr",
            language=settings.language or "auto",
        )
    if provider == "openai":
        if not settings.base_url or not settings.model:
            raise ConfigError("通用语音识别（openai）需要填 base_url 和 model")
        return OpenAITranscriptionASR(
            session, base_url=settings.base_url, api_key=settings.api_key, model=settings.model
        )
    raise ConfigError(f"不认识的语音识别服务商：{settings.provider}")
