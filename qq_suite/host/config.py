"""插件配置（对应插件目录下的 config.toml，也决定 WebUI 插件配置页的样子）。

MaiBot 配置页的限制：字段固定显示，不能随别的字段联动；下拉框直接显示选项值。所以：
- 「服务商账号」和「功能」分开成不同分节：服务商分节放官方接口地址（只读展示）和 API Key，
  功能分节只做选择（下拉框，选项值用中文）。小米 MiMo 的 Key 只填一次，识别和合成共用；
  MiMo 有按量付费和 Token Plan 两个官方地址，做成二选一的下拉框。
- 每个字段都写中文 label（名称）和 hint（说明）。
- 旧的英文选项值（如 siliconflow）读入时自动换成中文。
密钥字段可以直接填值，也可以填 env:变量名，从环境变量读取（例如 k8s Secret 注入）。
"""

from __future__ import annotations

import os
from typing import Any, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_TOKEN_PLAN_URL = "https://token-plan-cn.xiaomimimo.com/v1"
TAVILY_URL = "https://api.tavily.com/search"
BOCHA_URL = "https://api.bochaai.com/v1/web-search"
DEFAULT_DESIGN_PROMPT = "年轻女性，声音清晰自然，语速适中，语气亲切"
DEFAULT_APPEARANCE = "二十多岁的年轻女性，长相清秀，黑色长发"
DEFAULT_PHOTO_STYLE = "真实的手机照片，自然光线，写实质感"

# 下拉选项（中文）→ 内部代号
ASR_PROVIDERS = {"硅基流动": "siliconflow", "小米 MiMo": "mimo", "通用 OpenAI 兼容接口": "openai"}
TTS_PROVIDERS = {"小米 MiMo": "mimo"}
TTS_MODES = {"音色设计": "design", "预置音色": "preset", "音色复刻": "clone"}
TTS_FORMATS = {"MP3": "mp3", "WAV": "wav"}
SEARCH_PROVIDERS = {"SearXNG（自建）": "searxng", "Tavily": "tavily", "博查": "bocha"}
# 旧版本里表示「关闭」的选项值；读入时改成 enabled = false
_OFF_VALUES = {"关闭", "none", "", "qq 自带转写", "qq"}
MIMO_ASR_LANGUAGES = {"自动": "auto", "中文": "zh", "英文": "en"}
SEARXNG_LANGUAGES = {"自动": "auto", "简体中文": "zh-CN", "英文": "en"}
IMAGE_SIZES = {"竖图 1024×1536": "1024x1536", "方图 1024×1024": "1024x1024", "横图 1536×1024": "1536x1024"}
IMAGE_QUALITIES = {"低（快、便宜）": "low", "中": "medium", "高（慢、贵）": "high"}

ASRProviderName = Literal["硅基流动", "小米 MiMo", "通用 OpenAI 兼容接口"]
TTSProviderName = Literal["小米 MiMo"]
TTSModeName = Literal["音色设计", "预置音色", "音色复刻"]
SearchProviderName = Literal["SearXNG（自建）", "Tavily", "博查"]
SiliconFlowASRModel = Literal[
    "FunAudioLLM/SenseVoiceSmall",
    "Qwen/Qwen3-ASR-1.7B",
    "XingChenAGI/XingChenASR-V3.2",
    "XingChenAGI/XingChenASR-V3.2-Ultra",
]
PresetVoice = Literal["茉莉", "冰糖", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]
MiMoLanguage = Literal["自动", "中文", "英文"]
SearXNGLanguage = Literal["自动", "简体中文", "英文"]
MiMoBaseURL = Literal["https://api.xiaomimimo.com/v1", "https://token-plan-cn.xiaomimimo.com/v1"]
MessageFormat = Literal["普通文字", "Markdown"]
ImageModel = Literal["gpt-image-2", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"]
ImageSize = Literal["竖图 1024×1536", "方图 1024×1024", "横图 1536×1024"]
ImageQuality = Literal["低（快、便宜）", "中", "高（慢、贵）"]
AudioFormatName = Literal["MP3", "WAV"]


def _field(
    default: Any = None,
    *,
    label: str,
    hint: str = "",
    factory: Any = None,
    password: bool = False,
    ge: int | None = None,
    le: int | None = None,
    **extra: Any,
):
    schema: dict[str, Any] = {"label": label, "hint": hint, **extra}
    if password:
        schema["x-widget"] = "password"
        schema.setdefault("placeholder", "也可以填 env:环境变量名")
    bounds = {k: v for k, v in (("ge", ge), ("le", le)) if v is not None}
    if factory is not None:
        return Field(default_factory=factory, description=hint, json_schema_extra=schema, **bounds)
    return Field(default=default, description=hint, json_schema_extra=schema, **bounds)


def _readonly_url(url: str, *, label: str = "接口地址"):
    """官方接口地址：只读展示，程序固定使用这个地址，不读配置里的值。"""
    return _field(url, label=label, hint="官方地址，不可修改", disabled=True)


def _to_label(mapping: dict[str, str]):
    """读配置时把英文代号（旧写法）换成中文选项。"""
    reverse = {code.lower(): label for label, code in mapping.items()}

    def convert(value: Any) -> Any:
        if isinstance(value, str):
            return reverse.get(value.strip().lower(), value.strip())
        return value

    return convert


def _switch_from_legacy(provider_default: str):
    """旧配置用下拉框里的「关闭」表示不启用：转成 enabled = false，下拉框回到默认值。"""

    def convert(cls, data: Any) -> Any:
        if isinstance(data, dict) and "enabled" not in data:
            provider = data.get("provider")
            if isinstance(provider, str) and provider.strip().lower() in _OFF_VALUES:
                data = {**data, "enabled": False, "provider": provider_default}
            elif isinstance(provider, str):
                data = {**data, "enabled": True}  # 旧配置选了具体服务商，说明原本是开着的
        return data

    return model_validator(mode="before")(classmethod(convert))


def resolve_secret(value: str) -> str:
    value = value.strip()
    if value.startswith("env:"):
        return os.environ.get(value[4:].strip(), "")
    return value


# ---------------- 总开关与 QQ ----------------
class PluginSection(PluginConfigBase):
    """插件总开关"""

    __ui_label__ = "插件"
    __ui_order__ = 0
    enabled: bool = _field(True, label="启用插件")
    config_version: str = _field("2.4.0", label="配置版本", hint="由插件自动维护，请勿修改", disabled=True)


class QQSection(PluginConfigBase):
    """QQ 官方机器人，只支持私聊。凭据在 QQ 开放平台「开发管理」页获取；事件接入方式需选 WebSocket。"""

    __ui_label__ = "QQ 官方机器人"
    __ui_order__ = 10
    enabled: bool = _field(True, label="连接 QQ")
    app_id: str = _field("", label="AppID", hint="机器人的 AppID")
    app_secret: str = _field("", label="AppSecret", hint="机器人的 AppSecret", password=True)
    api_base: str = _field("https://api.bot.qq.com", label="接口地址", hint="QQ 开放平台接口地址，一般不用改")
    allowed_openids: list[str] = _field(
        label="用户白名单", hint="只回应这些用户（填 user_openid，可在日志里看到）；留空表示所有人", factory=list
    )
    nicknames: list[str] = _field(
        label="用户显示名",
        hint="每行一个「openid=昵称」。QQ 官方接口不提供昵称，不填就显示为「QQ用户+openid 前几位」",
        factory=list,
    )
    message_format: MessageFormat = _field(
        "普通文字",
        label="文字消息格式",
        hint="普通文字：去掉 **、# 这类符号再发；Markdown：加粗、列表等按格式显示（发送失败会自动改回普通文字）",
    )
    max_message_chars: int = _field(
        2000,
        label="单条消息最多字数",
        hint="回复超过这个长度就按段落切成多条发（200~4000）；回复名额不够时会放宽到 4000 字，少占名额",
        ge=200,
        le=4000,
    )
    typing_indicator: bool = _field(
        False,
        label="显示「正在输入」（实验）",
        hint="收到消息后让对方看到「对方正在输入…」。每次占用一个回复名额；日志里提示发送失败就关掉",
    )


# ---------------- 功能 ----------------
class ASRSection(PluginConfigBase):
    """收到语音时由谁转成文字。不启用就直接用 QQ 自带的转写；启用后识别失败也会退回 QQ 自带转写。"""

    __ui_label__ = "语音识别"
    __ui_order__ = 20
    enabled: bool = _field(False, label="启用", hint="关闭时直接使用 QQ 自带的转写文字")
    provider: ASRProviderName = _field("硅基流动", label="识别服务", hint="所选服务的 Key 在下方对应服务商分节里填")
    siliconflow_model: SiliconFlowASRModel = _field(
        "FunAudioLLM/SenseVoiceSmall", label="硅基流动模型", hint="选「硅基流动」时生效；SenseVoiceSmall 实测最准最快"
    )
    mimo_language: MiMoLanguage = _field("自动", label="MiMo 识别语言", hint="选「小米 MiMo」时生效")

    _legacy_switch = _switch_from_legacy("硅基流动")
    _norm_provider = field_validator("provider", mode="before")(_to_label(ASR_PROVIDERS))
    _norm_language = field_validator("mimo_language", mode="before")(_to_label(MIMO_ASR_LANGUAGES))


class TTSSection(PluginConfigBase):
    """语音回复（小米 MiMo 语音合成，目前限时免费）。觉得合适时会用语音回复，你要求时一定用。"""

    __ui_label__ = "语音合成"
    __ui_order__ = 30
    enabled: bool = _field(False, label="启用", hint="关闭时不提供语音回复工具，模型完全看不到它")
    provider: TTSProviderName = _field("小米 MiMo", label="合成服务", hint="Key 在下方「小米 MiMo」分节里填")
    mode: TTSModeName = _field(
        "音色设计", label="音色来源", hint="音色设计：按文字描述生成；预置音色：用现成音色；音色复刻：模仿一段录音"
    )
    design_prompt: str = _field(
        DEFAULT_DESIGN_PROMPT,
        label="音色描述",
        hint="「音色设计」时生效：说清性别年龄、音色质感、语气、语速这几项，1～4 句就够；"
        "具体比长重要，别堆砌或自相矛盾",
        **{"x-widget": "textarea", "rows": 3},
    )
    voice: PresetVoice = _field(
        "茉莉", label="预置音色", hint="「预置音色」时生效：茉莉、冰糖为中文女声，苏打、白桦为中文男声"
    )
    audio_format: AudioFormatName = _field(
        "MP3",
        label="音频格式",
        hint="MP3 体积约为 WAV 的 1/6，上传到 QQ 快得多；MP3 发送失败时会自动改用 WAV",
    )
    clone_file: str = _field(
        "",
        label="参考录音文件",
        hint="「音色复刻」时生效：录音放到 MaiBot 的 data/plugins/yreidev.qq_suite/voices/ 目录，这里填文件名；"
        "wav 或 mp3，不超过 7MB",
    )

    _legacy_switch = _switch_from_legacy("小米 MiMo")
    _norm_provider = field_validator("provider", mode="before")(_to_label(TTS_PROVIDERS))
    _norm_mode = field_validator("mode", mode="before")(_to_label(TTS_MODES))
    _norm_format = field_validator("audio_format", mode="before")(_to_label(TTS_FORMATS))


class SearchSection(PluginConfigBase):
    """联网搜索与读网页，让机器人能查实时信息。"""

    __ui_label__ = "联网搜索"
    __ui_order__ = 40
    enabled: bool = _field(False, label="启用搜索", hint="关闭时不提供搜索工具，模型完全看不到它")
    provider: SearchProviderName = _field(
        "SearXNG（自建）", label="搜索服务", hint="所选服务的地址或 Key 在下方对应分节里填"
    )
    max_results: int = _field(8, label="结果条数", hint="每次搜索返回几条（1~20）", ge=1, le=20)
    reader_enabled: bool = _field(
        False, label="启用读网页", hint="允许打开网页读取正文（只能访问公网地址）；和搜索各自独立开关"
    )
    read_max_chars: int = _field(6000, label="网页最多字数", hint="读网页时最多返回多少字", ge=500, le=50000)

    _legacy_switch = _switch_from_legacy("SearXNG（自建）")
    _norm_provider = field_validator("provider", mode="before")(_to_label(SEARCH_PROVIDERS))


class ImageSection(PluginConfigBase):
    """拍照：生成照片发给对方（比如想看看她在干什么）。接口地址和 Key 在下方「图像接口」分节。"""

    __ui_label__ = "拍照"
    __ui_order__ = 45
    enabled: bool = _field(False, label="启用", hint="关闭时不提供拍照工具，模型完全看不到它")
    model: ImageModel = _field("gpt-image-2", label="画图模型", hint="2.5 系列更能贴合参考图，人物长相更稳定")
    size: ImageSize = _field("竖图 1024×1536", label="尺寸")
    quality: ImageQuality = _field("中", label="质量", hint="越高越清晰，也越慢、越贵")
    appearance: str = _field(
        DEFAULT_APPEARANCE,
        label="人物外貌",
        hint="画「自己」时用：长相、发型、身材、穿衣风格。设了定妆照（在聊天里发 /定妆照）之后，长相主要按定妆照保持",
        **{"x-widget": "textarea", "rows": 3},
    )
    style: str = _field(DEFAULT_PHOTO_STYLE, label="画风", hint="例如：真实的手机照片 / 日系胶片 / 二次元插画")
    follow_minutes: int = _field(
        180,
        label="连贯时长（分钟）",
        hint="接着上一张拍时，只参考这么多分钟以内的上一张，让服装、场景前后连贯；0 表示从不参考上一张",
        ge=0,
        le=10080,
    )
    daily_limit: int = _field(20, label="每天最多张数", hint="控制花费；0 表示不限", ge=0, le=500)

    _norm_size = field_validator("size", mode="before")(_to_label(IMAGE_SIZES))
    _norm_quality = field_validator("quality", mode="before")(_to_label(IMAGE_QUALITIES))


# ---------------- 服务商账号 ----------------
class MiMoSection(PluginConfigBase):
    """小米 MiMo 开放平台（https://platform.xiaomimimo.com），语音识别和语音合成共用这个 Key。"""

    __ui_label__ = "小米 MiMo"
    __ui_order__ = 50
    base_url: MiMoBaseURL = _field(
        MIMO_BASE_URL,
        label="接口地址",
        hint="按量付费选 api.xiaomimimo.com；Token Plan（包月套餐）选 token-plan-cn.xiaomimimo.com",
    )
    api_key: str = _field("", label="API Key", password=True)

    @field_validator("base_url", mode="before")
    @classmethod
    def _official_only(cls, value: Any) -> Any:
        """只允许两个官方地址；手改成别的值时回到按量付费地址，不让配置整个读不进来。"""
        value = str(value or "").strip().rstrip("/")
        return value if value in (MIMO_BASE_URL, MIMO_TOKEN_PLAN_URL) else MIMO_BASE_URL


class SiliconFlowSection(PluginConfigBase):
    """硅基流动（https://cloud.siliconflow.cn）"""

    __ui_label__ = "硅基流动"
    __ui_order__ = 60
    base_url: str = _readonly_url(SILICONFLOW_BASE_URL)
    api_key: str = _field("", label="API Key", password=True)


class OpenAIASRSection(PluginConfigBase):
    """任意 OpenAI 兼容的语音转写接口（POST {地址}/audio/transcriptions），比如自建 Whisper 服务。"""

    __ui_label__ = "通用 OpenAI 兼容接口（语音识别）"
    __ui_order__ = 70
    base_url: str = _field(
        "", label="接口地址", hint="例如 https://example.com/v1", placeholder="https://example.com/v1"
    )
    api_key: str = _field("", label="API Key", password=True)
    model: str = _field("", label="模型", hint="例如 whisper-1")


class SearXNGSection(PluginConfigBase):
    """自建 SearXNG 元搜索引擎（免费，需在其 settings.yml 里开启 json 格式）。"""

    __ui_label__ = "SearXNG"
    __ui_order__ = 80
    base_url: str = _field("", label="地址", hint="例如 http://searxng:8080", placeholder="http://searxng:8080")
    language: SearXNGLanguage = _field("自动", label="搜索语言")

    _norm_language = field_validator("language", mode="before")(_to_label(SEARXNG_LANGUAGES))


class TavilySection(PluginConfigBase):
    """Tavily 搜索（https://tavily.com）"""

    __ui_label__ = "Tavily"
    __ui_order__ = 90
    base_url: str = _readonly_url(TAVILY_URL)
    api_key: str = _field("", label="API Key", password=True)


class BochaSection(PluginConfigBase):
    """博查搜索（https://open.bochaai.com）"""

    __ui_label__ = "博查"
    __ui_order__ = 100
    base_url: str = _readonly_url(BOCHA_URL)
    api_key: str = _field("", label="API Key", password=True)


class OpenAIImageSection(PluginConfigBase):
    """OpenAI 兼容的图像接口，第三方中转或官方都行，需支持 /images/generations 和 /images/edits。"""

    __ui_label__ = "图像接口（OpenAI 兼容）"
    __ui_order__ = 105
    base_url: str = _field(
        "", label="接口地址", hint="例如 https://api.example.com/v1", placeholder="https://api.example.com/v1"
    )
    api_key: str = _field("", label="API Key", password=True)


class QQSuiteConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    qq: QQSection = Field(default_factory=QQSection)
    asr: ASRSection = Field(default_factory=ASRSection)
    tts: TTSSection = Field(default_factory=TTSSection)
    search: SearchSection = Field(default_factory=SearchSection)
    image: ImageSection = Field(default_factory=ImageSection)
    mimo: MiMoSection = Field(default_factory=MiMoSection)
    siliconflow: SiliconFlowSection = Field(default_factory=SiliconFlowSection)
    openai_asr: OpenAIASRSection = Field(default_factory=OpenAIASRSection)
    searxng: SearXNGSection = Field(default_factory=SearXNGSection)
    tavily: TavilySection = Field(default_factory=TavilySection)
    bocha: BochaSection = Field(default_factory=BochaSection)
    image_api: OpenAIImageSection = Field(default_factory=OpenAIImageSection)


def parse_nicknames(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        openid, sep, name = line.partition("=")
        if sep and openid.strip() and name.strip():
            result[openid.strip()] = name.strip()
    return result
