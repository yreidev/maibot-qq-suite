# 设计说明

## 目标

一个 MaiBot 插件同时提供：QQ 官方机器人私聊适配器、语音识别、语音合成、联网搜索与读网页。
可以代替「QQ 适配器插件 + 语音合成插件 + 搜索 MCP 服务」这类多插件组合。

## 分层

```
plugin.py                     插件入口（宿主把仓库目录当包加载，只有一行转发）
qq_suite/
  host/                       ← 唯一依赖 maibot_sdk 的地方
    plugin.py                 注册网关和工具、管理生命周期，只做转调
    config.py                 配置模型（= config.toml 结构 = WebUI 配置页）
    assembly.py               按配置组装各模块；一个模块出错只关掉它自己
    bridge.py                 QQ 消息 ⇄ MaiBot 消息字典（不依赖 SDK，可单测）
    tools.py                  三个工具的逻辑（不依赖 SDK，可单测）
  qq/                         QQ 官方机器人单聊客户端，只依据腾讯官方文档实现
    auth.py  api.py  gateway.py  events.py  replies.py  text.py  media.py  client.py
  speech/asr/                 语音识别：统一接口 + 硅基流动 / MiMo / 通用 OpenAI 兼容
  speech/tts/                 语音合成：统一接口 + MiMo（预置 / 音色设计 / 音色复刻）
  search/                     搜索：统一接口 + SearXNG / Tavily / 博查；网页读取（防 SSRF）
  common/                     错误类型、HTTP 小工具
```

依赖方向只能向下：`host → qq / speech / search → common`。`qq`、`speech`、`search` 之间互不引用，
也不知道 MaiBot 的存在，可以单独拿去别的项目用。

### 解耦手段
- **统一接口**：每类服务商实现同一个 Protocol（`ASRProvider`、`TTSProvider`、`SearchProvider`），
  新增一家只需写一个实现并在对应 `factory.py` 登记。
- **依赖注入**：HTTP 会话、时钟、sleep、URL 校验器都由外部传入，测试用本地假服务跑真实 HTTP / WebSocket。
- **模块隔离**：`assembly.assemble()` 逐个构造模块，配置错误只记日志并关掉该模块。

## 数据流

**入站**：QQ 网关 `C2C_MESSAGE_CREATE` → `QQBotClient`（去重、记录被动回复 msg_id）→ 白名单过滤
→（可选）发「正在输入」→ `InboundBuilder`：引用消息（message_type 103）的被引用内容写成 `[引用：…]` 放在最前；
图片下载成二进制；语音下载平台转好的 WAV，用配置的识别服务转文字，失败时用 QQ 自带的 `asr_refer_text`
→ `ctx.gateway.route_message`。语音段的 data 写成 `[语音: 文字]`，宿主就不再重复识别。

**出站**：宿主调用网关 `qqsuite_c2c` → `OutboundDispatcher` 合并相邻文字、图片和语音逐条发送
→ `QQBotClient`：普通文字模式下去掉 Markdown 符号；超过单条字数就按段落切分；取被动回复目标 → OpenAPI。

**被动回复名额**（`qq/replies.py`）：每条用户消息 60 分钟内可回复 4 次。用户最近一小时的每条消息各自的名额合在一起用，
先用最新的；切分后条数超过剩余名额时，放宽到单条 4000 字重新切，尽量少占名额；全部用完才改发主动消息并告警。
「正在输入」也占一个名额（与正式回复共用 msg_seq，否则会被平台当成重复消息去重）。

**语音回复**：planner 调 `qqsuite_send_voice` → TTS 合成 → `ctx.send.custom("voice", base64, sync_to_maisaka_history=True)`
→ 宿主入库、写进对话历史并回调本插件网关 → 上传到 QQ。返回 `stop_after_execution`，planner 不再追发文字。

## 关键决定

| 决定 | 原因 |
|---|---|
| `plugin_type: adapter`，工具也注册在同一插件 | 宿主注册组件时不看类型，planner 会遍历全部 Supervisor 取工具 |
| 工具 `visibility="visible"`、名字加 `qqsuite_` 前缀 | 插件工具默认 deferred，模型要先调 tool_search 才看得到；工具名按短名全局解析，加前缀避免撞名 |
| 机器人身份用 READY 事件里的 `user.id`，Resume 后也重报 | 会话 id 含账号 id；身份一变会出现「多个 Bot 账号」，回复发不出去 |
| 适配器自己做语音识别 | 宿主只在语音内容为空时才识别，适配器能拿到 QQ 转好的 WAV，还能用 QQ 转写兜底 |
| 富媒体用 base64 `file_data` 上传、语音直接传 WAV | 官方文档已不列 file_data、语音写着只支持 silk，但实测（2026-10）两者都可用；失效时改分片上传 / 转 silk |
| 读网页只允许公网地址 | 网址由模型给出，必须挡住对集群内服务、云元数据地址的访问（含跳转、DNS 重绑定） |
| 密钥支持 `env:变量名` | config.toml 是明文，且能被声明了 config.get_plugin 的其他插件读取 |
| 语音回复带 `sync_to_maisaka_history=True` | 宿主默认不把插件发的消息写进对话历史，不写的话下一轮不知道自己用语音说过什么 |
| 默认去掉 Markdown 符号，Markdown 消息可选 | 模型常写 `**`、`#`，普通文字消息会原样显示；Markdown 发送失败时本次运行自动改回普通文字 |
| 搜索结果紧凑排版、读网页优先取 `<main>/<article>` | 工具结果会留在后续对话上下文里，越短越省 token、越快 |
| 语音默认 MP3，失败自动改 WAV 重发 | 实测 WAV（24kHz 16bit）约 48KB/秒，海外服务器上传到 QQ 一段 25 秒的语音要 28 秒；MP3 约为 1/6。QQ 文档只写了 silk，WAV 实测可用，MP3 待实测，所以留了自动回退 |
| 复刻录音放插件数据目录 | `data/plugins/<id>/` 不随插件更新被覆盖；仍兼容旧版放在插件目录 `voices/` 的录音 |
| `host_application.max_version` 锁 1.3.99 | 依赖宿主内部行为（入站字典形状、出站目标字段等），新小版本先实测再放宽 |

## 待实测（线上）
- intent `1<<25` 默认是否有权限；主动消息（不带 msg_id）是否可发。
- 被动回复窗口到底是 60 分钟 / 4 次。
- 入站附件 URL 有效期。
- 引用消息的 `msg_elements` 实际内容（文档只写了字段，没给完整示例）。
- 「正在输入」（msg_type 6）是否可用、是否计入被动回复次数。
- 单条文字 / Markdown 的长度上限（文档未写明，暂按 4000 字）。

## 参考
- QQ 机器人开放平台文档：https://bot.q.qq.com/wiki/develop/api-v2/
- 小米 MiMo 语音合成 / 识别：https://mimo.mi.com/docs/
- MaiBot 1.3.1 与 maibot-plugin-sdk 2.8.2 源码
