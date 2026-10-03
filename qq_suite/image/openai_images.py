"""OpenAI 兼容的图像接口（gpt-image 系列）：第三方中转或官方都行，按官方 Images API 的能力调用。

依据 openai-python 的 types/image_generate_params.py、image_edit_params.py（2026-10）：
- 不带参考图：POST {地址}/images/generations，JSON
- 带参考图：POST {地址}/images/edits，multipart；参考图字段名 image[]（数组），
  最多 16 张，png / webp / jpg，每张小于 50MB
- quality：low / medium / high（2.5 系列另有 xhigh / max）；size：1024x1024、1536x1024、1024x1536 等
- input_fidelity（贴合参考图的程度）：gpt-image-2 会忽略，2.5 系列支持
- 返回 data[0].b64_json；有的中转只给 data[0].url，两种都认
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import aiohttp

from ..common.errors import ConfigError, ProviderError
from ..common.http import bearer, read_json
from .base import Picture

MODELS = ("gpt-image-2", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare")
_FORMATS = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
_TIMEOUT = aiohttp.ClientTimeout(total=300, connect=15)  # 高质量出图要一两分钟
MAX_REFERENCES = 16
_MAX_DOWNLOAD = 30 * 1024 * 1024


class OpenAIImageGenerator:
    name = "openai_images"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str,
        base_url: str,
        model: str,
        size: str = "1024x1536",
        quality: str = "medium",
        output_format: str = "jpeg",
        compression: int = 90,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError("图像接口地址要以 http:// 或 https:// 开头")
        if not model.strip():
            raise ConfigError("没有选画图模型")
        if output_format not in _FORMATS:
            raise ConfigError(f"不支持的图片格式：{output_format}")
        self._session = session
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model.strip()
        self._size = size
        self._quality = quality
        self._format = output_format
        self._compression = compression

    def _params(self, prompt: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
            "size": self._size,
            "quality": self._quality,
            "output_format": self._format,
        }
        if self._format in {"jpeg", "webp"}:
            params["output_compression"] = self._compression  # 体积小，上传到 QQ 快
        return params

    async def generate(self, prompt: str, *, references: Sequence[Picture] = ()) -> Picture:
        if not prompt.strip():
            raise ProviderError(self.name, "没有画面描述")
        refs = list(references)[:MAX_REFERENCES]
        if refs:
            payload = await self._edit(prompt, refs)
        else:
            async with self._session.post(
                self._base + "/images/generations",
                json=self._params(prompt),
                headers=bearer(self._api_key),
                timeout=_TIMEOUT,
            ) as resp:
                payload = await read_json(resp, self.name)
        return await self._picture(payload)

    async def _edit(self, prompt: str, refs: list[Picture]) -> Any:
        form = aiohttp.FormData()
        for key, value in self._params(prompt).items():
            form.add_field(key, str(value))
        if self._model.startswith("gpt-image-2.5"):
            form.add_field("input_fidelity", "high")  # 尽量保持参考图里人物的长相
        for index, picture in enumerate(refs):
            form.add_field(
                "image[]", picture.data, filename=f"reference-{index}{picture.suffix}", content_type=picture.mime_type
            )
        async with self._session.post(
            self._base + "/images/edits", data=form, headers=bearer(self._api_key), timeout=_TIMEOUT
        ) as resp:
            return await read_json(resp, self.name)

    async def _picture(self, payload: Any) -> Picture:
        try:
            item = payload["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, "返回里没有图片") from exc
        fmt = payload.get("output_format") if isinstance(payload, dict) else None
        mime = _FORMATS.get(fmt or self._format, "image/jpeg")
        if item.get("b64_json"):
            try:
                return Picture(base64.b64decode(item["b64_json"], validate=True), mime)
            except (ValueError, TypeError) as exc:
                raise ProviderError(self.name, "图片不是合法的 base64") from exc
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            raise ProviderError(self.name, "返回里没有图片")
        async with self._session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status >= 400:
                raise ProviderError(self.name, f"下载生成的图片失败：{resp.status}", status=resp.status)
            data = await resp.content.read(_MAX_DOWNLOAD + 1)
            content_type = resp.content_type
        if len(data) > _MAX_DOWNLOAD:
            raise ProviderError(self.name, "生成的图片太大")
        return Picture(data, content_type if content_type in _FORMATS.values() else mime)
