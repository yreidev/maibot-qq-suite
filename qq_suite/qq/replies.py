"""被动回复名额：记录每个用户最近的消息，给出回复时该带的 msg_id 和 msg_seq。

单聊被动回复：收到消息后 60 分钟内、每条最多回复 4 次；同一 msg_id + msg_seq 重复会被去重（40054005）。
用户最近一小时发的每条消息都各有 4 个名额，这里放在一起用：优先用最新那条，用完再用更早的，
都用完才不带 msg_id，按主动消息发（多半没有主动消息权限而失败，由调用方处理）。
文档：https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

PASSIVE_WINDOW_SECONDS = 60 * 60
PASSIVE_MAX_REPLIES = 4
_MAX_INBOUND_PER_USER = 20


@dataclass
class _Inbound:
    msg_id: str
    received_at: float
    replies: int = 0


@dataclass(frozen=True)
class ReplyTarget:
    msg_id: str | None  # None 表示主动消息
    msg_seq: int


class ReplyTracker:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_users: int = 1000) -> None:
        self._clock = clock
        self._max_users = max_users
        self._inbound: dict[str, deque[_Inbound]] = {}  # 每个用户最近的消息，新的在右边
        self._active_seq = 0

    def record_inbound(self, openid: str, msg_id: str) -> None:
        recent = self._inbound.pop(openid, None) or deque(maxlen=_MAX_INBOUND_PER_USER)
        recent.append(_Inbound(msg_id, self._clock()))
        self._inbound[openid] = recent  # 重新插入：dict 保持插入顺序，最久没说话的在最前
        while len(self._inbound) > self._max_users:
            self._inbound.pop(next(iter(self._inbound)))

    def _usable(self, openid: str) -> list[_Inbound]:
        """窗口内还有名额的消息，最新的在前。"""
        now = self._clock()
        recent = self._inbound.get(openid) or ()
        return [
            m
            for m in reversed(recent)
            if now - m.received_at < PASSIVE_WINDOW_SECONDS and m.replies < PASSIVE_MAX_REPLIES
        ]

    def available(self, openid: str) -> int:
        """还能发几条被动回复。"""
        return sum(PASSIVE_MAX_REPLIES - m.replies for m in self._usable(openid))

    def take_passive(self, openid: str) -> ReplyTarget | None:
        """占用一次被动回复名额；没有名额时返回 None，不占用。"""
        usable = self._usable(openid)
        if not usable:
            return None
        inbound = usable[0]
        inbound.replies += 1
        return ReplyTarget(inbound.msg_id, inbound.replies)

    def next_target(self, openid: str) -> ReplyTarget:
        """占用一次回复名额并返回目标；被动名额用完时返回主动消息目标。"""
        target = self.take_passive(openid)
        if target is not None:
            return target
        self._active_seq += 1
        return ReplyTarget(None, self._active_seq)
