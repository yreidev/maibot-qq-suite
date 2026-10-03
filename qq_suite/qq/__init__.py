"""QQ 官方机器人单聊（C2C）客户端，只依据腾讯官方文档实现，与 MaiBot 无关。"""

from .api import FileType, QQAPIError, QQOpenAPI
from .client import QQBotClient, QQSettings
from .events import Attachment, C2CMessage

__all__ = ["Attachment", "C2CMessage", "FileType", "QQAPIError", "QQBotClient", "QQOpenAPI", "QQSettings"]
