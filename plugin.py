"""QQ 官方机器人全家桶插件入口。宿主把本目录当作包加载，所以这里用相对导入。"""

from .qq_suite.host.plugin import QQSuitePlugin, create_plugin

__all__ = ["QQSuitePlugin", "create_plugin"]
