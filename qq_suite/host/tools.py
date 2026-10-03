"""给 planner 用的工具的具体逻辑，不依赖 MaiBot SDK（插件入口只负责注册和转调）。"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..common.errors import QQSuiteError
from ..search import PageReader, SearchProvider, format_results
from ..search.base import TimeRange
from ..speech import AudioClip
from ..speech.tts import TTSProvider

VoiceSender = Callable[[str, str, str], Awaitable[bool]]  # (音频 base64, stream_id, 入库显示文本) -> 是否发出

_TIME_RANGES: dict[str, TimeRange] = {"": "", "day": "day", "week": "week", "month": "month", "year": "year"}


def _ok(content: str, **extra: Any) -> dict[str, Any]:
    return {"success": True, "content": content, **extra}


def _fail(error: str) -> dict[str, Any]:
    return {"success": False, "content": "", "error": error}


@dataclass
class ToolBox:
    search: SearchProvider | None = None
    reader: PageReader | None = None
    tts: TTSProvider | None = None
    tts_fallback: TTSProvider | None = None
    send_voice: VoiceSender | None = None
    max_results: int = 8
    read_max_chars: int = 6000
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    async def web_search(self, query: str, time_range: str = "") -> dict[str, Any]:
        if self.search is None:
            return _fail("联网搜索没有启用")
        if not query.strip():
            return _fail("搜索词为空")
        started = time.monotonic()
        try:
            results = await self.search.search(
                query.strip(), max_results=self.max_results, time_range=_TIME_RANGES.get(time_range.strip(), "")
            )
        except QQSuiteError as exc:
            self.logger.warning("搜索「%s」失败：%s", query.strip(), exc)
            return _fail(f"搜索失败：{exc}")
        content = format_results(results)
        self.logger.info(
            "搜索「%s」（%s）：%d 条，%d 字，用时 %.1f 秒",
            query.strip(), self.search.name, len(results), len(content), time.monotonic() - started,
        )  # fmt: skip
        return _ok(content)

    async def read_url(self, url: str) -> dict[str, Any]:
        if self.reader is None:
            return _fail("网页读取没有启用")
        started = time.monotonic()
        try:
            page = await self.reader.read(url, max_chars=self.read_max_chars)
        except QQSuiteError as exc:
            self.logger.warning("读网页 %s 失败：%s", url, exc)
            return _fail(f"读取失败：{exc}")
        self.logger.info(
            "读网页 %s：%d 字%s，用时 %.1f 秒",
            page.url, len(page.text), "（已截断）" if page.truncated else "", time.monotonic() - started,
        )  # fmt: skip
        head = f"标题：{page.title}\n网址：{page.url}\n\n" if page.title else f"网址：{page.url}\n\n"
        tail = "\n\n（正文较长，已截断）" if page.truncated else ""
        return _ok(head + page.text + tail)

    async def speak(self, text: str, stream_id: str, style: str = "") -> dict[str, Any]:
        if self.tts is None or self.send_voice is None:
            return _fail("语音回复没有启用")
        if not stream_id:
            return _fail("找不到当前聊天")
        started = time.monotonic()
        try:
            clip = await self.tts.synthesize(text, style=style)
        except QQSuiteError as exc:
            self.logger.warning("语音合成失败：%s", exc)
            return _fail(f"语音合成失败：{exc}")
        self.logger.info(
            "语音合成（%s）：%d 字 → %d KB，用时 %.1f 秒",
            self.tts.name, len(text), len(clip.data) // 1024, time.monotonic() - started,
        )  # fmt: skip
        sent = await self._send_clip(clip, stream_id, text)
        if not sent and self.tts_fallback is not None:
            sent = await self._retry_with_fallback(text, stream_id, style)
        if not sent:
            self.logger.warning("语音合成成功，但发送失败")
            return _fail("语音合成成功，但发送失败")
        # 语音就是这一轮的回复，不需要再发文字
        return _ok(f"已用语音说：{text}", stop_after_execution=True)

    async def _send_clip(self, clip: AudioClip, stream_id: str, text: str) -> bool:
        assert self.send_voice is not None
        return await self.send_voice(base64.b64encode(clip.data).decode("ascii"), stream_id, f"[语音] {text}")

    async def _retry_with_fallback(self, text: str, stream_id: str, style: str) -> bool:
        """MP3 发不出去时用 WAV 再试一次；WAV 成功就说明是格式问题，本次运行之后都用 WAV。"""
        assert self.tts_fallback is not None
        self.logger.warning("语音发送失败，改用 WAV 格式重试")
        try:
            clip = await self.tts_fallback.synthesize(text, style=style)
        except QQSuiteError as exc:
            self.logger.warning("WAV 重新合成失败：%s", exc)
            return False
        if not await self._send_clip(clip, stream_id, text):
            return False
        self.logger.warning("WAV 发送成功：本次运行改用 WAV，建议在配置里把「音频格式」改成 WAV")
        self.tts, self.tts_fallback = self.tts_fallback, None
        return True
