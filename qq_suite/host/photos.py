"""拍照工具的具体逻辑：拼画面描述、挑参考图、生成、存档、发送。不依赖 MaiBot SDK，便于单测。

参考图怎么挑：
- 画「自己」：带上定妆照；还没有定妆照时带上一张照片，长相也能尽量一致
- 「接着上一张」：再带上最近一张（在连贯时长以内），服装和场景前后连贯
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..common.errors import QQSuiteError
from ..image import ImageGenerator, PhotoAlbum, Picture

ImageSender = Callable[[str, str], Awaitable[bool]]  # (图片 base64, stream_id) -> 是否发出


def build_prompt(
    scene: str,
    *,
    style: str,
    appearance: str = "",
    selfie: bool,
    with_portrait: bool,
    with_previous: bool,
) -> str:
    """把画面描述、画风、人物外貌和参考图说明拼成给画图模型的提示词。"""
    lines = [f"画风：{style.strip()}"] if style.strip() else []
    if selfie and appearance.strip():
        lines.append(f"照片里的人物：{appearance.strip()}")
    refs = []
    if with_portrait:
        refs.append("第 1 张参考图是人物的定妆照，人物的长相、发型、身材要和它一致")
    if with_previous:
        refs.append("最后一张参考图是上一张照片，延续它的服装、场景和光线，让两张照片看起来是连着拍的")
    lines.extend(refs)
    lines.append(f"画面：{scene.strip()}")
    return "\n".join(lines)


@dataclass
class PhotoStudio:
    painter: ImageGenerator
    album: PhotoAlbum
    send_image: ImageSender
    appearance: str = ""
    style: str = ""
    follow_seconds: float = 3 * 3600
    daily_limit: int = 20
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def _references(self, *, selfie: bool, follow_previous: bool) -> tuple[list[Picture], bool, bool]:
        refs: list[Picture] = []
        portrait = self.album.reference() if selfie else None
        if portrait is not None:
            refs.append(portrait)
        previous = None
        if follow_previous and self.follow_seconds > 0:
            previous = self.album.latest(max_age=self.follow_seconds)
        elif selfie and portrait is None:
            previous = self.album.latest()  # 还没有定妆照：拿上一张保持长相
        if previous is not None:
            refs.append(previous)
        return refs, portrait is not None, previous is not None and follow_previous

    async def take(
        self, scene: str, stream_id: str, *, selfie: bool = True, follow_previous: bool = False
    ) -> dict[str, Any]:
        if not scene.strip():
            return {"success": False, "content": "", "error": "没有说要拍什么"}
        if not stream_id:
            return {"success": False, "content": "", "error": "找不到当前聊天"}
        if self.daily_limit and self.album.count_today() >= self.daily_limit:
            return {"success": False, "content": "", "error": f"今天已经拍了 {self.daily_limit} 张，额度用完了"}
        refs, with_portrait, with_previous = self._references(selfie=selfie, follow_previous=follow_previous)
        prompt = build_prompt(
            scene,
            style=self.style,
            appearance=self.appearance,
            selfie=selfie,
            with_portrait=with_portrait,
            with_previous=with_previous,
        )
        started = time.monotonic()
        try:
            picture = await self.painter.generate(prompt, references=refs)
        except QQSuiteError as exc:
            self.logger.warning("拍照失败：%s", exc)
            return {"success": False, "content": "", "error": f"拍照失败：{exc}"}
        self.album.save(picture)
        self.logger.info(
            "拍照（%s）：参考图 %d 张，%d KB，用时 %.1f 秒",
            self.painter.name,
            len(refs),
            len(picture.data) // 1024,
            time.monotonic() - started,
        )
        if not await self.send_image(base64.b64encode(picture.data).decode("ascii"), stream_id):
            return {"success": False, "content": "", "error": "照片拍好了，但发送失败"}
        return {"success": True, "content": f"已发送照片：{scene.strip()}"}
