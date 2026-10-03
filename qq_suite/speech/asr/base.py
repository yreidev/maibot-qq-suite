"""语音识别的统一接口。新增一家服务商只需实现 ASRProvider 并在 factory 里登记。"""

from __future__ import annotations

from typing import Protocol

from .. import AudioClip


class ASRProvider(Protocol):
    name: str

    async def transcribe(self, clip: AudioClip) -> str:
        """返回识别出的文字；服务商出错时抛 ProviderError。"""
        ...
