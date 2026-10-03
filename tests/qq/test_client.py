from __future__ import annotations

import logging

from aiohttp import web

from qq_suite.qq.api import FileType
from qq_suite.qq.client import QQBotClient, QQSettings


async def _client(fake_service, *, reject: set[int] = frozenset(), **settings):
    """起一个假的 QQ 开放平台；reject 里的 msg_type 一律返回错误。"""
    sent: list[dict] = []

    async def token(request: web.Request) -> web.Response:
        return web.json_response({"access_token": "t", "expires_in": "7200"})

    async def files(request: web.Request) -> web.Response:
        body = await request.json()
        sent.append({"upload": body["file_type"]})
        return web.json_response({"file_info": "FI"})

    async def messages(request: web.Request) -> web.Response:
        body = await request.json()
        sent.append(body)
        if body["msg_type"] in reject:
            return web.json_response({"code": 304003, "message": "没有权限"}, status=400)
        return web.json_response({"id": "ROBOT1.0_x", "ext_info": {"ref_idx": "REFIDX_sent"}})

    routes = {
        ("POST", "/app/getAppAccessToken"): token,
        ("POST", "/v2/users/{openid}/messages"): messages,
        ("POST", "/v2/users/{openid}/files"): files,
    }
    base, session, _ = await fake_service(routes)
    received = []

    async def on_message(message) -> None:
        received.append(message)

    qq = QQSettings("app", "sec", api_base=base, token_url=base + "/app/getAppAccessToken", **settings)
    client = QQBotClient(session, qq, on_message=on_message, logger=logging.getLogger("t"))
    return client, sent, received


def _inbound(msg_id: str) -> dict:
    return {"id": msg_id, "author": {"user_openid": "U"}, "content": "在吗"}


async def test_inbound_dedupe(fake_service):
    client, _, received = await _client(fake_service)
    await client.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    await client.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    await client.on_dispatch("GROUP_AT_MESSAGE_CREATE", _inbound("M2"), "")
    assert [m.id for m in received] == ["M1"]


async def test_send_text_strips_markdown_splits_and_saves_quota(fake_service, caplog):
    client, sent, _ = await _client(fake_service, max_message_chars=200)
    await client.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")

    await client.send_text("U", "**粗体**\n\n" + "长" * 300)
    assert [(b["msg_type"], b["msg_id"], b["msg_seq"], len(b["content"])) for b in sent] == [
        (0, "M1", 1, 2),
        (0, "M1", 2, 200),
        (0, "M1", 3, 100),
    ]
    assert sent[0]["content"] == "粗体"

    sent.clear()
    await client.send_text("U", "短" * 500)  # 要 3 条，但只剩 1 个名额：放宽到 4000 字，一条发完
    assert [(b["msg_seq"], len(b["content"])) for b in sent] == [(4, 500)]

    sent.clear()
    with caplog.at_level(logging.WARNING):
        await client.send_text("U", "还有一句")
    assert "msg_id" not in sent[0]  # 名额用完：主动消息
    assert "名额已用完" in caplog.text


async def test_markdown_mode_and_fallback(fake_service):
    client, sent, _ = await _client(fake_service, markdown=True)
    await client.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    await client.send_text("U", "**重点**")
    assert sent == [{"msg_type": 2, "markdown": {"content": "**重点**"}, "msg_seq": 1, "msg_id": "M1"}]

    failing, sent, _ = await _client(fake_service, markdown=True, reject={2})
    await failing.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    await failing.send_text("U", "**重点**")
    await failing.send_text("U", "## 第二条")
    assert [(b["msg_type"], b.get("content")) for b in sent] == [(2, None), (0, "重点"), (0, "第二条")]


async def test_typing_indicator(fake_service):
    off, sent, _ = await _client(fake_service)
    await off.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    await off.notify_typing("U")
    assert sent == []

    on, sent, _ = await _client(fake_service, typing_indicator=True)
    await on.notify_typing("U")  # 没有可回复的消息：不发
    await on.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    await on.notify_typing("U")
    assert [(b["msg_type"], b["msg_id"], b["msg_seq"]) for b in sent] == [(6, "M1", 1)]
    await on.send_text("U", "好")
    assert sent[-1]["msg_seq"] == 2  # 「正在输入」占了一个序号

    failing, sent, _ = await _client(fake_service, typing_indicator=True, reject={6})
    await failing.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    await failing.notify_typing("U")  # 失败只记日志，不抛异常
    assert len(sent) == 1


async def test_send_media_logs_upload_time(fake_service, caplog):
    client, sent, _ = await _client(fake_service)
    await client.on_dispatch("C2C_MESSAGE_CREATE", _inbound("M1"), "")
    with caplog.at_level(logging.INFO):
        assert await client.send_media("U", FileType.VOICE, b"x" * 4096) == "REFIDX_sent"
    assert sent == [{"upload": 3}, {"msg_type": 7, "media": {"file_info": "FI"}, "msg_seq": 1, "msg_id": "M1"}]
    assert "发语音给" in caplog.text and "4 KB" in caplog.text
