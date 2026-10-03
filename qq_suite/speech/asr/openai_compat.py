"""OpenAI 兼容的语音转写接口（POST {base_url}/audio/transcriptions，multipart 上传）。

硅基流动、自建 Whisper 服务等都走这个实现，区别只在地址、密钥和模型名。
"""

from __future__ import annotations

import aiohttp

from ...common.errors import ProviderError
from ...common.http import DEFAULT_TIMEOUT, bearer, read_json
from .. import AudioClip


class OpenAITranscriptionASR:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        api_key: str,
        model: str,
        name: str = "openai_compat",
    ) -> None:
        self._session = session
        self._url = base_url.rstrip("/") + "/audio/transcriptions"
        self._api_key = api_key
        self._model = model
        self.name = name

    async def transcribe(self, clip: AudioClip) -> str:
        form = aiohttp.FormData()
        form.add_field("model", self._model)
        form.add_field("file", clip.data, filename=f"voice.{clip.extension}", content_type=clip.mime_type)
        async with self._session.post(
            self._url, data=form, headers=bearer(self._api_key), timeout=DEFAULT_TIMEOUT
        ) as resp:
            payload = await read_json(resp, self.name)
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise ProviderError(self.name, "返回里没有 text 字段")
        return text.strip()
