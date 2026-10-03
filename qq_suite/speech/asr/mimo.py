"""小米 MiMo 语音识别（mimo-v2.5-asr），走 chat/completions + input_audio。

文档：https://mimo.mi.com/docs/zh-CN/api/audio/Speech-Recognition （只收 wav/mp3，base64 后不超过 10MB）
"""

from __future__ import annotations

import base64

import aiohttp

from ...common.errors import ProviderError
from ...common.http import DEFAULT_TIMEOUT, read_json
from .. import AudioClip

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
_MAX_B64_BYTES = 10 * 1024 * 1024


class MiMoASR:
    name = "mimo_asr"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str,
        base_url: str = MIMO_BASE_URL,
        model: str = "mimo-v2.5-asr",
        language: str = "auto",
    ) -> None:
        self._session = session
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._language = language

    async def transcribe(self, clip: AudioClip) -> str:
        encoded = base64.b64encode(clip.data).decode("ascii")
        if len(encoded) > _MAX_B64_BYTES:
            raise ProviderError(self.name, "音频超过 10MB 上限")
        body = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": f"data:{clip.mime_type};base64,{encoded}"}}
                    ],
                }
            ],
            "asr_options": {"language": self._language},
        }
        async with self._session.post(
            self._url, json=body, headers={"api-key": self._api_key}, timeout=DEFAULT_TIMEOUT
        ) as resp:
            payload = await read_json(resp, self.name)
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, "返回结构不对，找不到识别文字") from exc
        if not isinstance(text, str):
            raise ProviderError(self.name, "识别结果不是文字")
        return text.strip()
