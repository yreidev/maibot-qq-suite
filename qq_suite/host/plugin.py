"""MaiBot 插件入口：只做注册和转调，具体逻辑在 bridge / tools / assembly 和各功能模块里。"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import aiohttp
from maibot_sdk import MaiBotPlugin, MessageGateway, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from ..image import PhotoAlbum
from ..qq import C2CMessage, FileType, QQBotClient
from ..search import make_reader_session
from .assembly import Modules, assemble
from .bridge import PLATFORM, InboundBuilder, OutboundDispatcher
from .config import QQSuiteConfig, parse_nicknames
from .photos import PhotoStudio
from .portrait import handle_portrait, is_portrait_command
from .tools import ToolBox

GATEWAY = "qqsuite_c2c"
PLUGIN_ID = "yreidev.qq_suite"
PLUGIN_DIR = Path(__file__).resolve().parents[2]


def tools_to_expose(modules: Modules) -> dict[str, bool]:
    """每个工具是否该暴露给模型：对应功能没开、或开了但没配好（缺 Key 等），都不暴露。"""
    return {
        "qqsuite_web_search": modules.search is not None,
        "qqsuite_read_url": modules.reader is not None,
        # 语音要经 QQ 网关发出，QQ 没连也不暴露
        "qqsuite_send_voice": modules.tts is not None and modules.qq is not None,
        "qqsuite_send_photo": modules.image is not None and modules.qq is not None,
    }


class QQSuitePlugin(MaiBotPlugin):
    config_model = QQSuiteConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: aiohttp.ClientSession | None = None
        self._reader_session: aiohttp.ClientSession | None = None
        self._qq: QQBotClient | None = None
        self._builder: InboundBuilder | None = None
        self._dispatcher: OutboundDispatcher | None = None
        self._tools = ToolBox()
        self._album: PhotoAlbum | None = None
        self._photos: PhotoStudio | None = None
        self._allowed: set[str] = set()
        self._applied: dict[str, Any] | None = None  # 上次生效的配置，用来跳过没有实际变化的重载
        self._lock = asyncio.Lock()

    # ---------------- 生命周期 ----------------
    async def on_load(self) -> None:
        await self._start()

    async def on_unload(self) -> None:
        await self._stop()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        if self.config.model_dump() == self._applied:
            return  # 例如配置结构升级后宿主写回文件，内容没变，不必重连 QQ
        self.ctx.logger.info("配置已更新，重新组装各模块")
        await self._stop()
        await self._start()

    async def _start(self) -> None:
        async with self._lock:
            cfg: QQSuiteConfig = self.config
            self._applied = cfg.model_dump()
            log = self.ctx.logger
            self._session = aiohttp.ClientSession()
            self._reader_session = make_reader_session()
            modules = assemble(
                cfg,
                session=self._session,
                reader_session=self._reader_session,
                voices_dirs=self._voices_dirs(),
                logger=log,
            )
            self._tools = ToolBox(
                search=modules.search,
                reader=modules.reader,
                tts=modules.tts,
                tts_fallback=modules.tts_fallback,
                send_voice=self._send_voice,
                max_results=max(1, min(cfg.search.max_results, 20)),
                read_max_chars=max(500, cfg.search.read_max_chars),
                logger=log,
            )
            self._allowed = {x.strip() for x in cfg.qq.allowed_openids if x.strip()}
            self._album = PhotoAlbum(Path(self.ctx.paths.data_dir) / "photos")
            if modules.image is not None:
                self._photos = PhotoStudio(
                    painter=modules.image,
                    album=self._album,
                    send_image=self._send_image,
                    appearance=cfg.image.appearance,
                    style=cfg.image.style,
                    follow_seconds=cfg.image.follow_minutes * 60,
                    daily_limit=cfg.image.daily_limit,
                    logger=log,
                )
            if modules.qq is not None:
                nicknames = parse_nicknames(cfg.qq.nicknames)
                self._qq = QQBotClient(
                    self._session, modules.qq, on_message=self._on_qq_message, on_state=self._on_qq_state, logger=log
                )
                self._builder = InboundBuilder(
                    self._qq,
                    asr=modules.asr,
                    nickname_of=lambda openid: nicknames.get(openid) or f"QQ用户{openid[:6]}",
                    logger=log,
                )
                self._dispatcher = OutboundDispatcher(
                    self._qq, is_allowed=self._is_allowed, on_media_sent=self._on_media_sent
                )
                self._qq.start()  # 后台连接，不阻塞 on_load
            enabled = [
                name
                for name, on in (
                    ("QQ", modules.qq),
                    ("语音识别", modules.asr),
                    ("语音合成", modules.tts),
                    ("搜索", modules.search),
                    ("读网页", modules.reader),
                    ("拍照", modules.image),
                )
                if on is not None
            ]
            log.info("QQ 全家桶插件已启动：%s", "、".join(enabled) or "无可用模块")
            await self._sync_tools(modules)

    async def _sync_tools(self, modules: Modules) -> None:
        """在宿主侧启用 / 禁用工具组件：被禁用的工具不会出现在模型的工具列表里。"""
        exposed = []
        for name, expose in tools_to_expose(modules).items():
            toggle = self.ctx.component.enable_component if expose else self.ctx.component.disable_component
            try:
                result = await toggle(f"{PLUGIN_ID}.{name}", "tool")
            except Exception as exc:  # 宿主不支持或未授权时，工具仍在，但调用时会返回「未启用」
                self.ctx.logger.warning("切换工具 %s 失败：%s", name, exc)
                continue
            if isinstance(result, dict) and not result.get("success", True):
                self.ctx.logger.warning("切换工具 %s 失败：%s", name, result.get("error"))
            elif expose:
                exposed.append(name)
        self.ctx.logger.info("暴露给模型的工具：%s", "、".join(exposed) or "无")

    async def _stop(self) -> None:
        async with self._lock:
            if self._qq is not None:
                await self._qq.stop()
                with contextlib.suppress(Exception):
                    await self.ctx.gateway.update_state(GATEWAY, ready=False)
            self._qq = self._builder = self._dispatcher = None
            self._photos = None
            for session in (self._session, self._reader_session):
                if session is not None:
                    await session.close()
            self._session = self._reader_session = None

    def _voices_dirs(self) -> tuple[Path, ...]:
        """音色复刻的录音目录：优先插件数据目录（更新插件不会被覆盖），兼容旧版放在插件目录里的录音。"""
        data_voices = Path(self.ctx.paths.data_dir) / "voices"
        with contextlib.suppress(OSError):
            data_voices.mkdir(parents=True, exist_ok=True)
        return (data_voices, PLUGIN_DIR / "voices")

    def _is_allowed(self, openid: str) -> bool:
        return not self._allowed or openid in self._allowed

    # ---------------- QQ → MaiBot ----------------
    async def _on_qq_state(self, ready: bool, bot_id: str) -> None:
        if ready:
            await self.ctx.gateway.update_state(GATEWAY, ready=True, platform=PLATFORM, account_id=bot_id)
        else:
            await self.ctx.gateway.update_state(GATEWAY, ready=False)

    async def _on_qq_message(self, message: C2CMessage) -> None:
        if not self._is_allowed(message.user_openid):
            self.ctx.logger.info("忽略白名单外用户的私聊：%s", message.user_openid)
            return
        if self._builder is None or self._qq is None:
            return
        if is_portrait_command(message):
            await self._handle_portrait(message)  # 命令由插件自己处理，不交给 MaiBot
            return
        await self._qq.notify_typing(message.user_openid)
        bot_id = self._qq.bot_id
        payload = await self._builder.build(message, bot_id)
        await self.ctx.gateway.route_message(
            GATEWAY,
            payload,
            route_metadata={"self_id": bot_id},
            external_message_id=message.id,
            dedupe_key=message.id,
        )

    async def _handle_portrait(self, message: C2CMessage) -> None:
        qq, album, openid = self._qq, self._album, message.user_openid
        if qq is None or album is None:
            return
        await handle_portrait(
            message,
            album,
            reply=lambda text: qq.send_text(openid, text),
            send_photo=lambda data: qq.send_media(openid, FileType.IMAGE, data),
        )

    def _on_media_sent(self, file_type: FileType, data: bytes, ref_idx: str) -> None:
        if file_type == FileType.IMAGE and self._album is not None and self._album.remember_sent(data, ref_idx):
            self.ctx.logger.debug("记下照片的引用编号 %s", ref_idx)

    # ---------------- MaiBot → QQ ----------------
    @MessageGateway(
        "duplex",
        name=GATEWAY,
        platform=PLATFORM,
        protocol="qq_official_c2c",
        description="QQ 官方机器人私聊（QQ 官方机器人全家桶）",
        timeout_ms=120000,
    )
    async def qqsuite_c2c(self, message: dict[str, Any], route: Any = None, metadata: Any = None, **kwargs: Any):
        if self._dispatcher is None:
            return {"success": False, "error": "QQ 没有连接"}
        try:
            return await self._dispatcher.dispatch(message)
        except Exception as exc:
            self.ctx.logger.exception("发送到 QQ 失败")
            return {"success": False, "error": str(exc)}

    async def _send_voice(self, audio_b64: str, stream_id: str, plain_text: str) -> bool:
        result = await self.ctx.send.custom(
            "voice",
            audio_b64,
            stream_id,
            processed_plain_text=plain_text,
            # 宿主默认不把插件发的消息写进对话历史；不写的话，下一轮麦麦不知道自己用语音说过什么
            sync_to_maisaka_history=True,
            rpc_timeout_ms=120000,
        )
        return bool(result)

    async def _send_image(self, image_b64: str, stream_id: str) -> bool:
        result = await self.ctx.send.image(image_b64, stream_id, sync_to_maisaka_history=True, rpc_timeout_ms=120000)
        return bool(result)

    # ---------------- 给 planner 的工具 ----------------
    @Tool(
        "qqsuite_web_search",
        description="联网搜索。需要最新信息（新闻、天气、价格、版本、发布时间等）或你不确定的事实时使用，返回标题、链接和摘要。",
        parameters=[
            ToolParameterInfo(name="query", description="搜索词，尽量具体"),
            ToolParameterInfo(
                name="time_range",
                description="只看最近的结果：day / week / month / year；不限就留空",
                required=False,
                enum_values=["", "day", "week", "month", "year"],
            ),
        ],
        visibility="visible",
        timeout_ms=60000,
    )
    async def qqsuite_web_search(self, query: str = "", time_range: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._tools.web_search(query, time_range or "")

    @Tool(
        "qqsuite_read_url",
        description="读取一个网页的正文（只能是公网 http/https 地址）。搜索结果的摘要不够详细时，用它打开具体链接。",
        parameters=[ToolParameterInfo(name="url", description="要读取的网址")],
        visibility="visible",
        timeout_ms=60000,
    )
    async def qqsuite_read_url(self, url: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._tools.read_url(url)

    @Tool(
        "qqsuite_send_voice",
        description=(
            "用语音回复（代替这一轮的文字回复，发出后不要再发文字）。对方要求你用语音说话时必须用；"
            "问候、安慰等口语化、带情绪的短句也适合用。长篇说明、代码、链接不要用语音。"
        ),
        parameters=[
            ToolParameterInfo(name="text", description="要说的话，口语化，建议不超过 100 字"),
            ToolParameterInfo(
                name="style", description="语气，如「温柔地」「开心地」「小声地」；没有就留空", required=False
            ),
        ],
        visibility="visible",
        timeout_ms=120000,
    )
    async def qqsuite_send_voice(
        self, text: str = "", style: str = "", stream_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        return await self._tools.speak(text, stream_id, style or "")

    @Tool(
        "qqsuite_send_photo",
        description=(
            "拍一张照片发给对方。对方想看你在干什么、想要你的照片时使用；"
            "聊到你正在做的事、想分享眼前的画面时也可以主动发，但别太频繁。发完可以再配一句话。"
            "对方不满意要重拍时，拍一张新的，不要接着上一张拍。"
        ),
        parameters=[
            ToolParameterInfo(
                name="scene",
                description="画面描述：在哪、在做什么、穿什么、表情和动作、怎么拍的（自拍 / 别人帮拍 / 只拍景物）",
            ),
            ToolParameterInfo(
                name="selfie",
                param_type=ToolParamType.BOOLEAN,
                description="照片里有没有你自己；只拍景物、食物等就填 false",
                required=False,
            ),
            ToolParameterInfo(
                name="follow_previous",
                param_type=ToolParamType.BOOLEAN,
                description=(
                    "是否接着上一张拍（同一身衣服、同一个场景）。对方说「换个姿势」「再来一张同样的」时填 true；"
                    "对方嫌不好看要重拍、想换衣服或换场景时填 false"
                ),
                required=False,
            ),
        ],
        visibility="visible",
        timeout_ms=360000,
    )
    async def qqsuite_send_photo(
        self, scene: str = "", selfie: Any = True, follow_previous: Any = False, stream_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        if self._photos is None:
            return {"success": False, "content": "", "error": "拍照没有启用"}
        return await self._photos.take(
            scene, stream_id, selfie=_as_bool(selfie, True), follow_previous=_as_bool(follow_previous, False)
        )


def _as_bool(value: Any, default: bool) -> bool:
    """模型有时把布尔参数传成字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "是"}:
            return True
        if text in {"false", "0", "no", "否"}:
            return False
    return default


def create_plugin() -> QQSuitePlugin:
    return QQSuitePlugin()
