"""语音能力：识别（asr）和合成（tts），与 QQ、MaiBot 都无关。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioClip:
    """一段音频及其 MIME 类型（如 audio/wav、audio/mpeg）。"""

    data: bytes
    mime_type: str = "audio/wav"

    @property
    def extension(self) -> str:
        return {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/silk": "silk"}.get(self.mime_type, "wav")
