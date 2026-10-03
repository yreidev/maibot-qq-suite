"""各模块共用的异常类型。"""

from __future__ import annotations


class QQSuiteError(Exception):
    """插件内所有可预期错误的基类。"""


class ProviderError(QQSuiteError):
    """调用外部服务商（语音识别、语音合成、搜索等）失败。"""

    def __init__(self, provider: str, message: str, *, status: int | None = None) -> None:
        self.provider = provider
        self.status = status
        prefix = f"[{provider}]" if status is None else f"[{provider} HTTP {status}]"
        super().__init__(f"{prefix} {message}")


class ConfigError(QQSuiteError):
    """配置不完整或取值非法。"""
