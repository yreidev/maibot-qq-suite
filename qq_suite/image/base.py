"""画图的统一接口。新增一家服务商只需实现 ImageGenerator 并在 factory 里登记。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

_SUFFIX = {"image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg"}
_MIME = {suffix: mime for mime, suffix in _SUFFIX.items()} | {".jpeg": "image/jpeg"}


@dataclass(frozen=True)
class Picture:
    data: bytes
    mime_type: str = "image/jpeg"

    @property
    def suffix(self) -> str:
        return _SUFFIX.get(self.mime_type, ".jpg")

    @staticmethod
    def mime_of(suffix: str) -> str | None:
        """按文件后缀判断图片类型；不是 png / jpg / webp 返回 None。"""
        return _MIME.get(suffix.lower())


class ImageGenerator(Protocol):
    name: str

    async def generate(self, prompt: str, *, references: Sequence[Picture] = ()) -> Picture:
        """按描述生成一张图；references 是参考图（保持人物长相、延续上一张）。出错抛 ProviderError。"""
        ...
