"""语音合成的统一接口。"""

from __future__ import annotations

from typing import Protocol

from .. import AudioClip


class TTSProvider(Protocol):
    name: str

    async def synthesize(self, text: str, *, style: str = "") -> AudioClip:
        """把 text 合成为语音。style 是可选的语气指令，如「开心地说」。出错抛 ProviderError。"""
        ...
