from __future__ import annotations

import base64
import importlib.util
import json
import logging
import sys
import tomllib
from pathlib import Path

import aiohttp
import pytest

from qq_suite.common.errors import ProviderError
from qq_suite.host.assembly import assemble
from qq_suite.host.bridge import InboundBuilder, OutboundDispatcher
from qq_suite.host.config import QQSuiteConfig, parse_nicknames, resolve_secret
from qq_suite.host.tools import ToolBox
from qq_suite.qq.api import FileType
from qq_suite.qq.events import Attachment, C2CMessage, MessageElement
from qq_suite.search import SearchResult
from qq_suite.search.reader import PageContent
from qq_suite.speech import AudioClip

ROOT = Path(__file__).resolve().parents[2]


class FakeFetcher:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def download(self, url: str) -> bytes:
        if url not in self.files:
            raise ProviderError("qq_media", "404", status=404)
        return self.files[url]


class FakeASR:
    name = "fake"

    def __init__(self, text: str | None) -> None:
        self.text = text
        self.clips: list[AudioClip] = []

    async def transcribe(self, clip: AudioClip) -> str:
        self.clips.append(clip)
        if self.text is None:
            raise ProviderError("fake", "boom")
        return self.text


def _msg(content: str = "", *attachments: Attachment) -> C2CMessage:
    return C2CMessage(id="M1", user_openid="OPENID123456", content=content, timestamp="", attachments=attachments)


VOICE = Attachment(
    "voice", "https://qqbot.ugcimg.cn/v.silk", voice_wav_url="https://qqbot.ugcimg.cn/v.wav", asr_refer_text="QQ转写"
)


async def test_inbound_text_and_image():
    builder = InboundBuilder(FakeFetcher({"https://x.qq.com/a.png": b"PNG"}), nickname_of=lambda o: "测试用户")
    payload = await builder.build(_msg("看图", Attachment("image/png", "https://x.qq.com/a.png")), "BOT")
    assert payload["message_id"] == "M1" and payload["platform"] == "qq"
    assert payload["message_info"]["user_info"] == {
        "user_id": "OPENID123456",
        "user_nickname": "测试用户",
        "user_cardname": None,
    }
    assert payload["message_info"]["additional_config"] == {"platform_io_account_id": "BOT"}
    assert payload["raw_message"] == [
        {"type": "text", "data": "看图"},
        {"type": "image", "data": "", "binary_data_base64": base64.b64encode(b"PNG").decode()},
    ]
    assert payload["is_picture"] is False


async def test_inbound_voice_uses_asr_then_falls_back():
    asr = FakeASR("今天天气怎么样？")
    builder = InboundBuilder(FakeFetcher({VOICE.voice_wav_url: b"WAV"}), asr=asr)
    seg = (await builder.build(_msg("", VOICE), "BOT"))["raw_message"][0]
    assert seg == {
        "type": "voice",
        "data": "[语音: 今天天气怎么样？]",
        "binary_data_base64": base64.b64encode(b"WAV").decode(),
    }
    assert asr.clips == [AudioClip(b"WAV", "audio/wav")]

    failing = InboundBuilder(
        FakeFetcher({VOICE.voice_wav_url: b"WAV"}), asr=FakeASR(None), logger=logging.getLogger("t")
    )
    assert (await failing.build(_msg("", VOICE), "BOT"))["raw_message"][0]["data"] == "[语音: QQ转写]"

    no_asr_no_download = InboundBuilder(FakeFetcher({}))
    seg = (await no_asr_no_download.build(_msg("", VOICE), "BOT"))["raw_message"][0]
    assert seg == {"type": "voice", "data": "[语音: QQ转写]"}


async def test_inbound_placeholders():
    builder = InboundBuilder(FakeFetcher({}))
    payload = await builder.build(
        _msg("", Attachment("image/png", "https://x.qq.com/missing.png"), Attachment("file", "u", filename="a.pdf")),
        "B",
    )
    assert payload["raw_message"] == [{"type": "text", "data": "[图片]"}, {"type": "text", "data": "[文件：a.pdf]"}]
    assert (await builder.build(_msg(""), "B"))["raw_message"] == [{"type": "text", "data": "[空消息]"}]
    assert payload["message_info"]["user_info"]["user_nickname"] == "QQ用户OPENID"


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_text(self, openid: str, text: str) -> None:
        self.calls.append(("text", openid, text))

    async def send_media(self, openid: str, file_type: FileType, data: bytes) -> str:
        self.calls.append(("media", openid, file_type, data))
        return f"REFIDX_{len(self.calls)}"


def _out(segments, target="U1", **extra):
    return {
        "message_info": {"additional_config": {"platform_io_target_user_id": target, **extra}},
        "raw_message": segments,
    }


async def test_outbound_plan_order_and_merge():
    sender = FakeSender()
    b64 = base64.b64encode(b"V").decode()
    result = await OutboundDispatcher(sender).dispatch(
        _out([
            {"type": "reply", "data": {"target_message_id": "x"}},
            {"type": "text", "data": "你好"},
            {"type": "at", "data": {"target_user_id": "U1"}},
            {"type": "text", "data": "呀"},
            {"type": "voice", "data": "", "binary_data_base64": b64},
            {"type": "image", "data": "", "binary_data_base64": b64},
            {"type": "text", "data": "  "},
        ])
    )  # fmt: skip
    assert result == {"success": True}
    assert sender.calls == [
        ("text", "U1", "你好呀"),
        ("media", "U1", FileType.VOICE, b"V"),
        ("media", "U1", FileType.IMAGE, b"V"),
    ]


async def test_outbound_rejections():
    sender = FakeSender()
    d = OutboundDispatcher(sender, is_allowed=lambda o: o == "U1")
    assert (await d.dispatch(_out([{"type": "text", "data": "x"}], target="", platform_io_target_group_id="G")))[
        "success"
    ] is False
    assert (await d.dispatch(_out([{"type": "text", "data": "x"}], target="U2")))["error"] == "目标用户不在白名单内"
    assert (await d.dispatch(_out([{"type": "text", "data": " "}])))["error"] == "没有可发送的内容"
    assert sender.calls == []


class FakeSearch:
    name = "fake"

    async def search(self, query, *, max_results=8, time_range=""):
        assert (query, max_results, time_range) == ("天气", 3, "day")
        return [SearchResult("北京天气", "https://w", "晴")]


class FakeReader:
    async def read(self, url, *, max_chars=6000):
        if "bad" in url:
            raise ProviderError("reader", "不能读取内网地址")
        return PageContent(url=url, title="T", text="正文", truncated=True)


class FakeTTS:
    name = "tts"

    async def synthesize(self, text, *, style=""):
        return AudioClip(b"WAV")


async def test_toolbox():
    sent: list[tuple] = []

    async def send_voice(b64, stream_id, plain):
        sent.append((base64.b64decode(b64), stream_id, plain))
        return True

    box = ToolBox(search=FakeSearch(), reader=FakeReader(), tts=FakeTTS(), send_voice=send_voice, max_results=3)
    r = await box.web_search(" 天气 ", "day")
    assert r["success"] and "北京天气" in r["content"]
    assert (await box.web_search("  "))["success"] is False
    r = await box.read_url("https://ok")
    assert r["content"] == "标题：T\n网址：https://ok\n\n正文\n\n（正文较长，已截断）"
    assert "内网" in (await box.read_url("http://bad"))["error"]
    r = await box.speak("晚安", "S1", "温柔")
    assert r["success"] and r["stop_after_execution"] is True
    assert sent == [(b"WAV", "S1", "[语音] 晚安")]
    assert (await ToolBox().speak("x", "S1"))["success"] is False
    assert (await box.speak("x", ""))["error"] == "找不到当前聊天"


def test_config_helpers(monkeypatch):
    monkeypatch.setenv("XY_KEY", "secret!")
    assert resolve_secret(" env:XY_KEY ") == "secret!"
    assert resolve_secret("env:MISSING") == ""
    assert resolve_secret("plain") == "plain"
    assert parse_nicknames(["A=小红", "bad", " B = 小明 ", "=x"]) == {"A": "小红", "B": "小明"}


async def test_assemble_isolates_failures(tmp_path):
    cfg = QQSuiteConfig.model_validate(
        {
            "qq": {"app_id": "1", "app_secret": "s"},
            "asr": {"enabled": True, "provider": "siliconflow"},  # 硅基流动没填 key：只关掉语音识别
            "tts": {"enabled": True, "mode": "clone", "clone_file": "../../etc/passwd"},  # 不是 wav/mp3
            "mimo": {"api_key": "mk"},
            "search": {"enabled": True, "provider": "searxng", "reader_enabled": True},
            "searxng": {"base_url": "http://searxng:8080"},
        }
    )
    async with aiohttp.ClientSession() as session:
        m = assemble(
            cfg, session=session, reader_session=session, voices_dirs=[tmp_path], logger=logging.getLogger("t")
        )
    assert m.qq is not None and m.qq.app_id == "1"
    assert m.asr is None and m.tts is None
    assert m.search is not None and m.reader is not None
    assert len(m.problems) == 2


def test_provider_sections_feed_feature_settings(monkeypatch):
    from qq_suite.host.assembly import asr_settings, search_settings, tts_settings

    monkeypatch.setenv("SF_KEY", "sf-secret")
    cfg = QQSuiteConfig.model_validate(
        {
            "asr": {"enabled": True, "provider": "硅基流动", "siliconflow_model": "Qwen/Qwen3-ASR-1.7B"},
            # 有人手改了只读的官方地址：程序仍用官方地址
            "siliconflow": {"api_key": "env:SF_KEY", "base_url": "http://evil.example/v1"},
            "mimo": {"api_key": "mk"},
            "tts": {"enabled": True, "mode": "预置音色", "voice": "冰糖"},
            "search": {"enabled": True, "provider": "博查"},
            "bocha": {"api_key": "bk"},
        }
    )
    a = asr_settings(cfg)
    assert (a.provider, a.api_key, a.base_url, a.model) == (
        "siliconflow",
        "sf-secret",
        "https://api.siliconflow.cn/v1",
        "Qwen/Qwen3-ASR-1.7B",
    )
    t = tts_settings(cfg, Path("/nonexistent"))
    assert (t.provider, t.api_key, t.mode, t.voice) == ("mimo", "mk", "preset", "冰糖")
    s = search_settings(cfg)
    assert (s.provider, s.api_key) == ("bocha", "bk")
    mimo_asr = asr_settings(
        QQSuiteConfig.model_validate({"asr": {"provider": "mimo", "mimo_language": "zh"}, "mimo": {"api_key": "k"}})
    )
    assert (mimo_asr.provider, mimo_asr.language) == ("mimo", "zh")


def test_manifest_and_versions_consistent():
    manifest = json.loads((ROOT / "_manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert manifest["version"] == project["version"]
    assert manifest["plugin_type"] == "adapter" and manifest["id"] == "yreidev.qq_suite"
    assert "send.custom" in manifest["capabilities"]


def test_plugin_loads_like_host():
    spec = importlib.util.spec_from_file_location(
        "_maibot_plugin_yreidev_qq_suite", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        plugin = module.create_plugin()
        names = {c["name"]: c for c in plugin.get_components()}
    finally:
        sys.modules.pop(spec.name, None)
    tools = ("qqsuite_web_search", "qqsuite_read_url", "qqsuite_send_voice", "qqsuite_send_photo")
    assert set(names) == {"qqsuite_c2c", *tools}
    for tool in tools:
        assert names[tool]["metadata"]["visibility"] == "visible"
    assert plugin.get_default_config()["plugin"]["config_version"]


@pytest.mark.parametrize("section", ["qq", "asr", "tts", "search"])
def test_default_config_sections_have_ui_labels(section):
    cls = QQSuiteConfig.model_fields[section].annotation
    assert cls.__ui_label__


def test_webui_schema_is_chinese():
    from qq_suite.host.plugin import QQSuitePlugin

    schema = QQSuitePlugin().get_webui_config_schema()
    sections = schema["sections"]
    labels = []
    for section in sections.values() if isinstance(sections, dict) else sections:
        for name, field in (section.get("fields") or {}).items():
            labels.append((name, field["label"]))
    assert labels, schema
    english_named = [n for n, label in labels if label == n]
    assert english_named == [], f"这些字段没写中文名称：{english_named}"


def test_legacy_english_values_are_converted():
    cfg = QQSuiteConfig.model_validate(
        {
            "asr": {"provider": "SiliconFlow"},
            "tts": {"provider": "mimo", "mode": "design"},
            "search": {"provider": "searxng"},
        }
    )
    assert (cfg.asr.provider, cfg.tts.provider, cfg.tts.mode, cfg.search.provider) == (
        "硅基流动",
        "小米 MiMo",
        "音色设计",
        "SearXNG（自建）",
    )


def test_feature_switches_and_legacy_off_values():
    from qq_suite.host.assembly import asr_settings, search_settings, tts_settings

    defaults = QQSuiteConfig()
    assert not (
        defaults.asr.enabled or defaults.tts.enabled or defaults.search.enabled or defaults.search.reader_enabled
    )
    assert asr_settings(defaults).provider == "qq"
    assert tts_settings(defaults, Path("/x")).provider == "none"
    assert search_settings(defaults).provider == "none"

    legacy_off = QQSuiteConfig.model_validate(
        {"asr": {"provider": "QQ 自带转写"}, "tts": {"provider": "关闭"}, "search": {"provider": "none"}}
    )
    assert (legacy_off.asr.enabled, legacy_off.tts.enabled, legacy_off.search.enabled) == (False, False, False)
    assert legacy_off.tts.provider == "小米 MiMo" and legacy_off.search.provider == "SearXNG（自建）"

    legacy_on = QQSuiteConfig.model_validate({"asr": {"provider": "siliconflow"}, "search": {"provider": "searxng"}})
    assert legacy_on.asr.enabled and legacy_on.search.enabled
    explicit = QQSuiteConfig.model_validate({"search": {"provider": "searxng", "enabled": False}})
    assert explicit.search.enabled is False


def test_tools_to_expose():
    from qq_suite.host.assembly import Modules
    from qq_suite.host.plugin import tools_to_expose

    assert tools_to_expose(Modules()) == {
        "qqsuite_web_search": False,
        "qqsuite_read_url": False,
        "qqsuite_send_voice": False,
        "qqsuite_send_photo": False,
    }
    full = Modules(qq=object(), tts=object(), search=object(), reader=object(), image=object())
    assert all(tools_to_expose(full).values())
    no_qq = Modules(tts=object(), search=object())
    assert tools_to_expose(no_qq) == {
        "qqsuite_web_search": True,
        "qqsuite_read_url": False,
        "qqsuite_send_voice": False,
        "qqsuite_send_photo": False,
    }


async def test_inbound_quote_goes_before_content():
    builder = InboundBuilder(FakeFetcher({}))
    quoted = C2CMessage(
        id="M2",
        user_openid="OPENID123456",
        content="这是啥",
        timestamp="",
        message_type=103,
        elements=(
            MessageElement("原话", (Attachment("image/png", "https://x.qq.com/a.png"),)),
            MessageElement("长" * 400),
        ),
    )
    segments = (await builder.build(quoted, "B"))["raw_message"]
    assert segments[1] == {"type": "text", "data": "这是啥"}
    assert segments[0]["data"] == f"[引用：原话 [图片] / {'长' * 300}…]\n"
    only_quote = C2CMessage(id="M3", user_openid="U", content="", timestamp="", message_type=103,
                            elements=(MessageElement("", (VOICE,)),))  # fmt: skip
    assert (await builder.build(only_quote, "B"))["raw_message"] == [
        {"type": "text", "data": "[引用：[语音: QQ转写]]\n"}
    ]


def test_qq_and_mimo_settings_from_config(tmp_path):
    from qq_suite.host.assembly import asr_settings, tts_settings

    cfg = QQSuiteConfig.model_validate(
        {
            "qq": {"app_id": "1", "app_secret": "s", "message_format": "Markdown", "max_message_chars": 4000,
                   "typing_indicator": True},
            "mimo": {"api_key": "k", "base_url": "https://token-plan-cn.xiaomimimo.com/v1/"},
            "asr": {"enabled": True, "provider": "小米 MiMo"},
            "tts": {"enabled": True, "mode": "预置音色"},
        }
    )  # fmt: skip
    m = assemble(cfg, session=None, reader_session=None, voices_dirs=[tmp_path], logger=logging.getLogger("t"))
    assert (m.qq.markdown, m.qq.max_message_chars, m.qq.typing_indicator) == (True, 4000, True)
    token_plan = "https://token-plan-cn.xiaomimimo.com/v1"
    assert asr_settings(cfg).base_url == token_plan and tts_settings(cfg, []).base_url == token_plan
    hacked = QQSuiteConfig.model_validate({"mimo": {"base_url": "http://evil.example/v1"}})
    assert hacked.mimo.base_url == "https://api.xiaomimimo.com/v1"
    assert QQSuiteConfig().qq.message_format == "普通文字" and QQSuiteConfig().qq.typing_indicator is False


def test_clone_voice_searches_data_dir_then_legacy_dir(tmp_path):
    from qq_suite.common.errors import ConfigError
    from qq_suite.host.assembly import tts_settings

    data_dir, legacy_dir = tmp_path / "data", tmp_path / "legacy"
    data_dir.mkdir()
    legacy_dir.mkdir()
    (legacy_dir / "a.wav").write_bytes(b"OLD")
    cfg = QQSuiteConfig.model_validate(
        {"tts": {"enabled": True, "mode": "音色复刻", "clone_file": "a.wav"}, "mimo": {"api_key": "k"}}
    )
    assert tts_settings(cfg, [data_dir, legacy_dir]).clone_sample.data == b"OLD"
    (data_dir / "a.wav").write_bytes(b"NEW")
    assert tts_settings(cfg, [data_dir, legacy_dir]).clone_sample.data == b"NEW"
    missing = cfg.model_copy(update={"tts": cfg.tts.model_copy(update={"clone_file": "b.mp3"})})
    with pytest.raises(ConfigError, match=r"b\.mp3"):
        tts_settings(missing, [data_dir, legacy_dir])


async def test_voice_reply_is_written_to_history():
    from maibot_sdk.context import PluginContext

    from qq_suite.host.plugin import QQSuitePlugin

    plugin = QQSuitePlugin()
    plugin._set_context(PluginContext("yreidev.qq_suite"))
    calls = []

    async def custom(custom_type, data, stream_id, **kwargs):
        calls.append((custom_type, data, stream_id, kwargs))
        return True

    plugin.ctx.send.custom = custom
    assert await plugin._send_voice("QUJD", "S1", "[语音] 晚安") is True
    (custom_type, data, stream_id, kwargs) = calls[0]
    assert (custom_type, data, stream_id) == ("voice", "QUJD", "S1")
    assert kwargs["sync_to_maisaka_history"] is True and kwargs["processed_plain_text"] == "[语音] 晚安"


def test_manifest_host_range_and_capabilities():
    manifest = json.loads((ROOT / "_manifest.json").read_text())
    assert manifest["host_application"]["max_version"] == "1.3.99"
    assert sorted(manifest["capabilities"]) == [
        "component.disable",
        "component.enable",
        "send.custom",
        "send.image",
    ]


async def test_voice_falls_back_to_wav_when_mp3_fails():
    class FormatTTS:
        name = "tts"

        def __init__(self, fmt):
            self.fmt = fmt

        async def synthesize(self, text, *, style=""):
            return AudioClip(self.fmt.encode(), f"audio/{self.fmt}")

    sent: list[bytes] = []

    async def wav_only(b64, stream_id, plain):
        data = base64.b64decode(b64)
        sent.append(data)
        return data == b"wav"

    box = ToolBox(tts=FormatTTS("mp3"), tts_fallback=FormatTTS("wav"), send_voice=wav_only)
    assert (await box.speak("晚安", "S1"))["success"] is True
    assert sent == [b"mp3", b"wav"]
    assert box.tts.fmt == "wav" and box.tts_fallback is None  # 之后直接用 WAV
    assert (await box.speak("再见", "S1"))["success"] is True and sent[-1] == b"wav"

    async def always_fail(b64, stream_id, plain):
        return False

    failing = ToolBox(tts=FormatTTS("mp3"), tts_fallback=FormatTTS("wav"), send_voice=always_fail)
    assert (await failing.speak("晚安", "S1"))["success"] is False
    assert failing.tts.fmt == "mp3"  # WAV 也失败：不是格式问题，不切换


def test_audio_format_config_and_fallback_module(tmp_path):
    cfg = QQSuiteConfig.model_validate({"tts": {"enabled": True, "mode": "预置音色"}, "mimo": {"api_key": "k"}})
    assert cfg.tts.audio_format == "MP3"
    m = assemble(cfg, session=None, reader_session=None, voices_dirs=[tmp_path], logger=logging.getLogger("t"))
    assert m.tts.audio_format == "mp3" and m.tts_fallback.audio_format == "wav"
    wav_cfg = QQSuiteConfig.model_validate(
        {"tts": {"enabled": True, "mode": "预置音色", "audio_format": "wav"}, "mimo": {"api_key": "k"}}
    )
    assert wav_cfg.tts.audio_format == "WAV"
    m = assemble(wav_cfg, session=None, reader_session=None, voices_dirs=[tmp_path], logger=logging.getLogger("t"))
    assert m.tts.audio_format == "wav" and m.tts_fallback is None


def test_image_settings_from_config(monkeypatch):
    from qq_suite.host.assembly import image_settings

    assert image_settings(QQSuiteConfig()).provider == "none"
    monkeypatch.setenv("IMG_KEY", "ik")
    cfg = QQSuiteConfig.model_validate(
        {
            "image": {"enabled": True, "model": "gpt-image-2.5-sunburst", "size": "1536x1024", "quality": "high"},
            "image_api": {"base_url": " https://relay.example/v1 ", "api_key": "env:IMG_KEY"},
        }
    )
    assert (cfg.image.size, cfg.image.quality) == ("横图 1536×1024", "高（慢、贵）")  # 英文写法读入后换成中文选项
    i = image_settings(cfg)
    assert (i.provider, i.base_url, i.api_key, i.model, i.size, i.quality) == (
        "openai",
        "https://relay.example/v1",
        "ik",
        "gpt-image-2.5-sunburst",
        "1536x1024",
        "high",
    )


async def test_outbound_reports_media_ref_idx():
    seen = []
    sender = FakeSender()
    b64 = base64.b64encode(b"IMG").decode()
    d = OutboundDispatcher(sender, on_media_sent=lambda ft, data, ref: seen.append((ft, data, ref)))
    await d.dispatch(_out([{"type": "text", "data": "看"}, {"type": "image", "data": "", "binary_data_base64": b64}]))
    assert seen == [(FileType.IMAGE, b"IMG", "REFIDX_2")]  # 文字不报，图片报出 QQ 给的引用编号
