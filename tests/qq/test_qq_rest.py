from __future__ import annotations

import base64

import pytest
from aiohttp import web

from qq_suite.common.errors import ProviderError
from qq_suite.qq.api import FileType, QQAPIError, QQOpenAPI
from qq_suite.qq.auth import TokenProvider
from qq_suite.qq.events import MESSAGE_TYPE_QUOTE, C2CMessage
from qq_suite.qq.media import is_trusted_media_url
from qq_suite.qq.replies import ReplyTarget, ReplyTracker


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _token_service(fake_service, extra_routes=None, *, tokens=("t1", "t2", "t3")):
    issued = iter(tokens)

    async def token(request: web.Request) -> web.Response:
        body = await request.json()
        assert body == {"appId": "app", "clientSecret": "sec"}
        return web.json_response({"access_token": next(issued), "expires_in": "7200"})

    routes = {("POST", "/app/getAppAccessToken"): token, **(extra_routes or {})}
    base, session, calls = await fake_service(routes)
    return base, session, calls


async def test_token_cached_and_refreshed_near_expiry(fake_service):
    base, session, calls = await _token_service(fake_service)
    clock = Clock()
    tp = TokenProvider(session, app_id="app", app_secret="sec", token_url=base + "/app/getAppAccessToken", clock=clock)
    assert await tp.get() == "t1"
    clock.now += 7000
    assert await tp.get() == "t1"  # 还剩 200 秒，不刷新
    clock.now += 160
    assert await tp.get() == "t2"  # 剩 40 秒，刷新
    tp.invalidate()
    assert await tp.get() == "t3"
    assert tp.authorization == "QQBot t3"
    assert len(calls) == 3


async def test_token_error_body_with_http_200(fake_service):
    async def token(request: web.Request) -> web.Response:
        return web.json_response({"code": 100016, "message": "appid or secret invalid"})

    base, session, _ = await fake_service({("POST", "/tok"): token})
    tp = TokenProvider(session, app_id="a", app_secret="b", token_url=base + "/tok")
    with pytest.raises(ProviderError, match="100016"):
        await tp.get()


async def test_api_send_text_retry_401_and_dedup(fake_service):
    sent: list[dict] = []
    state = {"first": True}

    async def messages(request: web.Request) -> web.Response:
        assert request.match_info["openid"] == "U1"
        if state["first"]:
            state["first"] = False
            return web.json_response({"code": 11201, "err_code": 40012001, "message": "鉴权失败"}, status=401)
        assert request.headers["Authorization"] == "QQBot t2"
        sent.append(await request.json())
        if len(sent) == 2:
            return web.json_response({"err_code": 40054005, "message": "消息被去重"}, status=400)
        return web.json_response({"id": "ROBOT1.0_x", "timestamp": "2026-01-01T12:00:01+08:00"})

    base, session, _ = await _token_service(fake_service, {("POST", "/v2/users/{openid}/messages"): messages})
    api = QQOpenAPI(
        session,
        TokenProvider(session, app_id="app", app_secret="sec", token_url=base + "/app/getAppAccessToken"),
        base_url=base,
    )
    assert (await api.send_text("U1", "你好", msg_id="M1", msg_seq=1))["id"] == "ROBOT1.0_x"
    assert sent[0] == {"msg_type": 0, "content": "你好", "msg_seq": 1, "msg_id": "M1"}
    assert await api.send_text("U1", "你好", msg_id="M1", msg_seq=1) == {}  # 去重视为成功


async def test_api_errors_and_upload(fake_service):
    async def messages(request: web.Request) -> web.Response:
        return web.json_response({"err_code": 40054013, "message": "用户拒收"}, status=202)

    async def files(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["file_type"] == 3 and body["srv_send_msg"] is False
        assert base64.b64decode(body["file_data"]) == b"WAV"
        return web.json_response({"file_uuid": "u", "file_info": "FI", "ttl": 300})

    routes = {("POST", "/v2/users/{openid}/messages"): messages, ("POST", "/v2/users/{openid}/files"): files}
    base, session, _ = await _token_service(fake_service, routes)
    api = QQOpenAPI(
        session,
        TokenProvider(session, app_id="app", app_secret="sec", token_url=base + "/app/getAppAccessToken"),
        base_url=base,
    )
    with pytest.raises(QQAPIError) as info:
        await api.send_text("U", "x", msg_id=None, msg_seq=1)
    assert info.value.err_code == 40054013
    assert await api.upload_media("U", FileType.VOICE, b"WAV") == "FI"


def test_reply_tracker_window_and_quota():
    clock = Clock()
    rt = ReplyTracker(clock=clock)
    rt.record_inbound("U", "M1")
    targets = [rt.next_target("U") for _ in range(5)]
    assert [(t.msg_id, t.msg_seq) for t in targets[:4]] == [("M1", 1), ("M1", 2), ("M1", 3), ("M1", 4)]
    assert targets[4].msg_id is None  # 超过 4 次改为主动消息
    rt.record_inbound("U", "M2")
    clock.now += 3601
    assert rt.next_target("U").msg_id is None  # 超过 60 分钟
    assert rt.next_target("nobody").msg_id is None


def test_reply_tracker_pools_recent_messages():
    clock = Clock()
    rt = ReplyTracker(clock=clock)
    rt.record_inbound("U", "M1")
    clock.now += 600
    rt.record_inbound("U", "M2")
    assert rt.available("U") == 8
    ids = [rt.next_target("U").msg_id for _ in range(8)]
    assert ids == ["M2"] * 4 + ["M1"] * 4  # 先用最新的，用完再用更早的
    assert rt.available("U") == 0 and rt.take_passive("U") is None
    rt.record_inbound("U", "M3")
    clock.now += 3000  # M3 还在窗口内（50 分钟），M1 已过期
    assert rt.available("U") == 4
    assert rt.take_passive("U") == ReplyTarget("M3", 1)


def test_reply_tracker_evicts_oldest_user():
    rt = ReplyTracker(max_users=2)
    for user in ("a", "b", "c"):
        rt.record_inbound(user, f"m-{user}")
    assert rt.next_target("a").msg_id is None
    assert rt.next_target("c").msg_id == "m-c"


def test_c2c_message_parse():
    payload = {
        "id": "ROBOT1.0_abc",
        "author": {"id": "OPEN", "user_openid": "OPEN", "union_openid": ""},
        "content": " 你好 ",
        "timestamp": "2026-01-01T12:00:00+08:00",
        "attachments": [
            {"content_type": "voice", "url": "qqbot.ugcimg.cn/v.silk", "voice_wav_url": "https://qqbot.ugcimg.cn/v.wav",
             "asr_refer_text": "你好。", "size": "123"},
            {"content_type": "image/png", "url": "https://multimedia.nt.qq.com.cn/x.png"},
            "garbage",
        ],
    }  # fmt: skip
    msg = C2CMessage.from_payload(payload)
    assert (msg.id, msg.user_openid, msg.content) == ("ROBOT1.0_abc", "OPEN", "你好")
    voice, image = msg.attachments
    assert voice.kind == "voice" and voice.url == "https://qqbot.ugcimg.cn/v.silk" and voice.size == 123
    assert voice.asr_refer_text == "你好。"
    assert image.kind == "image"
    assert msg.message_type == 0 and msg.elements == ()


def test_c2c_quote_message_parse():
    payload = {
        "id": "M", "author": {"user_openid": "OPEN"}, "content": "这个是什么意思", "message_type": 103,
        "msg_elements": [
            {"msg_idx": "REFIDX_1", "message_type": 103, "content": "被引用的话",
             "attachments": [{"content_type": "image/jpeg", "url": "https://x.qq.com/a.jpg"}],
             "msg_elements": [{"content": "更早的一句"}]},
            "garbage",
        ],
    }  # fmt: skip
    msg = C2CMessage.from_payload(payload)
    assert msg.message_type == MESSAGE_TYPE_QUOTE
    assert [e.content for e in msg.elements] == ["被引用的话", "更早的一句"]
    assert msg.elements[0].attachments[0].kind == "image"
    assert C2CMessage.from_payload({"id": "M", "message_type": "bad"}).message_type == 0


async def test_api_input_notify(fake_service):
    sent: list[dict] = []

    async def messages(request: web.Request) -> web.Response:
        sent.append(await request.json())
        return web.json_response({})

    base, session, _ = await _token_service(fake_service, {("POST", "/v2/users/{openid}/messages"): messages})
    api = QQOpenAPI(
        session,
        TokenProvider(session, app_id="app", app_secret="sec", token_url=base + "/app/getAppAccessToken"),
        base_url=base,
    )
    await api.send_input_notify("U", msg_id="M1", msg_seq=2, seconds=999)
    assert sent == [
        {"msg_type": 6, "input_notify": {"input_type": 1, "input_second": 60}, "msg_seq": 2, "msg_id": "M1"}
    ]


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://qqbot.ugcimg.cn/a.wav", True),
        ("https://multimedia.nt.qq.com.cn/x.png", True),
        ("https://gchat.qpic.cn/x", True),
        ("http://qqbot.ugcimg.cn/a.wav", False),
        ("https://evil.com/qq.com", False),
        ("https://qq.com.evil.com/", False),
        ("https://qqbot.ugcimg.cn:8443/a", False),
        ("https://u:p@qqbot.ugcimg.cn/a", False),
    ],
)
def test_trusted_media_url(url, ok):
    assert is_trusted_media_url(url) is ok
