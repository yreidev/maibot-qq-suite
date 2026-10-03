"""按配置组装各功能模块。每个模块单独构造，一个出错只会关掉它自己，不影响其他模块。

功能分节只决定「用哪家、用什么模型」，密钥从对应的服务商分节取；官方接口地址固定用常量，不读配置
（小米 MiMo 例外：按量付费和 Token Plan 是两个官方地址，配置里只能二选一）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import aiohttp

from ..common.errors import ConfigError
from ..qq import QQSettings
from ..qq.text import QQ_TEXT_LIMIT
from ..search import PageReader, SearchProvider, SearchSettings, build_search
from ..speech import AudioClip
from ..speech.asr import ASRProvider, ASRSettings, build_asr
from ..speech.tts import TTSProvider, TTSSettings, build_tts
from .config import (
    ASR_PROVIDERS,
    MIMO_ASR_LANGUAGES,
    SEARCH_PROVIDERS,
    SEARXNG_LANGUAGES,
    SILICONFLOW_BASE_URL,
    TTS_FORMATS,
    TTS_MODES,
    TTS_PROVIDERS,
    QQSuiteConfig,
    resolve_secret,
)

_CLONE_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
T = TypeVar("T")


@dataclass
class Modules:
    qq: QQSettings | None = None
    asr: ASRProvider | None = None
    tts: TTSProvider | None = None
    tts_fallback: TTSProvider | None = None  # 主格式（MP3）发不出去时改用的 WAV
    search: SearchProvider | None = None
    reader: PageReader | None = None
    problems: list[str] = field(default_factory=list)


def asr_settings(cfg: QQSuiteConfig) -> ASRSettings:
    if not cfg.asr.enabled:
        return ASRSettings("qq")
    provider = ASR_PROVIDERS[cfg.asr.provider]
    if provider == "siliconflow":
        return ASRSettings(
            provider, resolve_secret(cfg.siliconflow.api_key), SILICONFLOW_BASE_URL, cfg.asr.siliconflow_model
        )
    if provider == "mimo":
        language = MIMO_ASR_LANGUAGES[cfg.asr.mimo_language]
        return ASRSettings(provider, resolve_secret(cfg.mimo.api_key), cfg.mimo.base_url, "", language)
    if provider == "openai":
        o = cfg.openai_asr
        return ASRSettings(provider, resolve_secret(o.api_key), o.base_url.strip(), o.model.strip())
    return ASRSettings("qq")


def _find_voice(name: str, voices_dirs: Sequence[Path]) -> Path:
    """按顺序在各个 voices 目录里找参考录音；只取文件名，防止读到 voices 目录以外的文件。"""
    filename = Path(name).name
    for directory in voices_dirs:
        path = directory / filename
        if path.is_file():
            return path
    where = voices_dirs[0] if voices_dirs else "voices"
    raise ConfigError(f"找不到参考录音 {filename!r}，请放到 {where} 目录")


def tts_settings(cfg: QQSuiteConfig, voices_dirs: Sequence[Path]) -> TTSSettings:
    t = cfg.tts
    if not t.enabled:
        return TTSSettings("none")
    mode = TTS_MODES[t.mode]
    clone = None
    if mode == "clone":
        mime = _CLONE_MIME.get(Path(t.clone_file).suffix.lower())
        if mime is None:
            raise ConfigError("参考录音只支持 .wav 或 .mp3")
        clone = AudioClip(_find_voice(t.clone_file, voices_dirs).read_bytes(), mime)
    return TTSSettings(
        TTS_PROVIDERS[t.provider],
        resolve_secret(cfg.mimo.api_key),
        cfg.mimo.base_url,
        mode,
        t.voice,
        t.design_prompt,
        clone,
        TTS_FORMATS[t.audio_format],
    )


def search_settings(cfg: QQSuiteConfig) -> SearchSettings:
    if not cfg.search.enabled:
        return SearchSettings("none")
    provider = SEARCH_PROVIDERS[cfg.search.provider]
    if provider == "searxng":
        return SearchSettings(provider, cfg.searxng.base_url.strip(), "", SEARXNG_LANGUAGES[cfg.searxng.language])
    key = {"tavily": cfg.tavily.api_key, "bocha": cfg.bocha.api_key}.get(provider, "")
    return SearchSettings(provider, "", resolve_secret(key))


def assemble(
    config: QQSuiteConfig,
    *,
    session: aiohttp.ClientSession,
    reader_session: aiohttp.ClientSession,
    voices_dirs: Sequence[Path],
    logger: logging.Logger,
) -> Modules:
    modules = Modules()

    def guarded(name: str, build: Callable[[], T | None]) -> T | None:
        try:
            return build()
        except (ConfigError, OSError) as exc:
            modules.problems.append(f"{name}：{exc}")
            logger.error("%s 未启用：%s", name, exc)
            return None

    qq = config.qq
    secret = resolve_secret(qq.app_secret)
    if qq.enabled and qq.app_id and secret:
        api_base = qq.api_base.strip() or QQSettings.api_base
        modules.qq = QQSettings(
            app_id=qq.app_id.strip(),
            app_secret=secret,
            api_base=api_base,
            markdown=qq.message_format == "Markdown",
            max_message_chars=max(200, min(qq.max_message_chars, QQ_TEXT_LIMIT)),
            typing_indicator=qq.typing_indicator,
        )
    elif qq.enabled:
        modules.problems.append("QQ：没填 AppID 或 AppSecret")

    modules.asr = guarded("语音识别", lambda: build_asr(asr_settings(config), session))
    modules.tts = guarded("语音合成", lambda: build_tts(tts_settings(config, voices_dirs), session))
    if getattr(modules.tts, "audio_format", "") == "mp3" and hasattr(modules.tts, "with_format"):
        modules.tts_fallback = modules.tts.with_format("wav")
    modules.search = guarded("联网搜索", lambda: build_search(search_settings(config), session))
    if config.search.reader_enabled:
        modules.reader = PageReader(reader_session)
    return modules
