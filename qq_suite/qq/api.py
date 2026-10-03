"""QQ 机器人 OpenAPI（只封装单聊用到的接口）。

文档：
- 发送单聊消息 POST /v2/users/{openid}/messages
  https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html
- 富媒体上传 POST /v2/users/{openid}/files
  https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_files.post.html
- 网关地址 GET /gateway
单聊可用的 msg_type：0 文字、2 Markdown（content 须为空）、6 输入中状态（input_notify）、7 富媒体。
错误体里 err_code 和 code 可能同时出现，以 err_code 为准；201/202 也可能带错误体。
"""

from __future__ import annotations

import base64
from enum import IntEnum
from typing import Any

import aiohttp

from ..common.errors import ProviderError
from .auth import TokenProvider

API_BASE = "https://api.bot.qq.com"
_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
DEDUPLICATED = 40054005  # 同一 msg_id + msg_seq 已发过：重试时视为成功


class FileType(IntEnum):
    IMAGE = 1
    VIDEO = 2
    VOICE = 3
    FILE = 4


class QQAPIError(ProviderError):
    def __init__(self, message: str, *, status: int, err_code: int | None, trace_id: str = "") -> None:
        self.err_code = err_code
        self.trace_id = trace_id
        detail = f"err_code={err_code} {message}" + (f" trace_id={trace_id}" if trace_id else "")
        super().__init__("qq_api", detail, status=status)


def _error_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("err_code", "code"):
        value = payload.get(key)
        if isinstance(value, int) and value != 0:
            return value
    return None


class QQOpenAPI:
    def __init__(self, session: aiohttp.ClientSession, tokens: TokenProvider, *, base_url: str = API_BASE) -> None:
        self._session = session
        self._tokens = tokens
        self._base = base_url.rstrip("/")

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in (1, 2):
            await self._tokens.get()
            headers = {"Authorization": self._tokens.authorization}
            async with self._session.request(
                method, self._base + path, json=body, headers=headers, timeout=_TIMEOUT
            ) as resp:
                status = resp.status
                trace_id = resp.headers.get("X-Tps-trace-ID", "")
                try:
                    payload = await resp.json(content_type=None) if status != 204 else {}
                except ValueError:
                    payload = {"message": (await resp.text(errors="replace"))[:300]}
            if status == 401 and attempt == 1:
                self._tokens.invalidate()  # token 失效：刷新后重试一次
                continue
            err_code = _error_code(payload)
            if status >= 400 or err_code is not None:
                message = str(payload.get("message", "")) if isinstance(payload, dict) else str(payload)
                trace_id = trace_id or (str(payload.get("trace_id", "")) if isinstance(payload, dict) else "")
                raise QQAPIError(message, status=status, err_code=err_code, trace_id=trace_id)
            return payload if isinstance(payload, dict) else {}
        raise AssertionError("unreachable")

    async def gateway_url(self) -> str:
        payload = await self._request("GET", "/gateway")
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise ProviderError("qq_api", f"网关地址不对：{url!r}")
        return url

    async def send_text(self, openid: str, content: str, *, msg_id: str | None, msg_seq: int) -> dict[str, Any]:
        return await self._send(openid, {"msg_type": 0, "content": content}, msg_id, msg_seq)

    async def send_markdown(self, openid: str, markdown: str, *, msg_id: str | None, msg_seq: int) -> dict[str, Any]:
        return await self._send(openid, {"msg_type": 2, "markdown": {"content": markdown}}, msg_id, msg_seq)

    async def send_media(self, openid: str, file_info: str, *, msg_id: str | None, msg_seq: int) -> dict[str, Any]:
        return await self._send(openid, {"msg_type": 7, "media": {"file_info": file_info}}, msg_id, msg_seq)

    async def send_input_notify(self, openid: str, *, msg_id: str, msg_seq: int, seconds: int = 60) -> dict[str, Any]:
        """让对方看到「正在输入」，最长 60 秒。只能作为被动回复发送。"""
        body = {"msg_type": 6, "input_notify": {"input_type": 1, "input_second": max(1, min(seconds, 60))}}
        return await self._send(openid, body, msg_id, msg_seq)

    async def _send(self, openid: str, body: dict[str, Any], msg_id: str | None, msg_seq: int) -> dict[str, Any]:
        body = {**body, "msg_seq": msg_seq}
        if msg_id:
            body["msg_id"] = msg_id
        try:
            return await self._request("POST", f"/v2/users/{openid}/messages", body)
        except QQAPIError as exc:
            if exc.err_code == DEDUPLICATED:
                return {}
            raise

    async def upload_media(self, openid: str, file_type: FileType, data: bytes) -> str:
        """上传富媒体，返回 file_info。

        官方文档已不列 base64 的 file_data 字段，但实测（2026-10）单聊上传 base64 WAV 语音可用；
        若以后失效，需改为分片上传（upload_prepare → PUT → upload_part_finish）。
        """
        body = {
            "file_type": int(file_type),
            "file_data": base64.b64encode(data).decode("ascii"),
            "srv_send_msg": False,
        }
        payload = await self._request("POST", f"/v2/users/{openid}/files", body)
        file_info = payload.get("file_info")
        if not isinstance(file_info, str) or not file_info:
            raise ProviderError("qq_api", "上传成功但没有返回 file_info")
        return file_info
