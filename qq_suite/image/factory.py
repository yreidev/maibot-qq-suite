"""根据配置构造画图服务。没开、没填地址或 Key 时返回 None，表示不提供画图。"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from ..common.errors import ConfigError
from .base import ImageGenerator
from .openai_images import OpenAIImageGenerator


@dataclass(frozen=True)
class ImageSettings:
    provider: str = "none"  # openai | none
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-image-2"
    size: str = "1024x1536"
    quality: str = "medium"


def build_image(settings: ImageSettings, session: aiohttp.ClientSession) -> ImageGenerator | None:
    provider = settings.provider.strip().lower()
    if provider in {"", "none"} or not settings.api_key or not settings.base_url:
        return None
    if provider != "openai":
        raise ConfigError(f"不认识的画图服务：{settings.provider}")
    return OpenAIImageGenerator(
        session,
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
        size=settings.size,
        quality=settings.quality,
    )
