# QQ 官方机器人全家桶（maibot-qq-suite）

[MaiBot](https://github.com/Mai-with-u/MaiBot) 插件，一个插件搞定：

- **QQ 官方机器人私聊适配器**：走 QQ 开放平台官方接口（WebSocket），不需要登录 QQ 号、不需要公网回调；
  支持文字、图片、语音、引用消息，长回复自动分条，可选 Markdown 和「正在输入」提示
- **语音识别**：硅基流动 / 小米 MiMo / 任意 OpenAI 兼容接口，不配置时用 QQ 自带转写
- **语音合成**：小米 MiMo-V2.5，支持预置音色、文字描述设计音色、录音复刻音色；机器人觉得合适时用语音回复
- **联网搜索与读网页**：自建 SearXNG（免费）或 Tavily / 博查；读网页自动拦截内网地址
- **拍照**：用 gpt-image 系列（OpenAI 兼容接口，第三方中转或官方）生成照片发给你，定妆照保持长相，接着上一张拍保持连贯

要求 MaiBot 1.3.x（插件 SDK ≥ 2.8.2）。运行时只用 MaiBot 环境里已有的 `aiohttp`，不额外安装依赖。

## 安装

```bash
cd MaiBot/plugins
git clone https://github.com/yreidev/maibot-qq-suite
```

也可以在 [Releases](https://github.com/yreidev/maibot-qq-suite/releases) 下载压缩包，解压到 `MaiBot/plugins`。

重启 MaiBot，在 WebUI「插件配置」里填写各项后保存即可（改配置会自动重载）。

> 本插件包含 QQ 适配器，不要和其他 QQ 官方机器人适配器（如 qq-official-adapter、Mai-with-u.qq-adapter、
> galeros.qqbot-adapter）同时启用：同一个机器人只能有一个网关连接。

QQ 开放平台侧：事件接入方式选 **WebSocket**；如启用了 IP 白名单，把服务器出口 IP 加进去。

## 配置

配置页分两类分节：**功能**分节做选择（开关和下拉框），**服务商**分节填 Key（官方接口地址只读展示，程序固定使用）。
每个功能都有独立开关，没开或没配好的功能不会出现在模型的工具列表里。

| 分节 | 内容 |
|---|---|
| QQ 官方机器人 | AppID、AppSecret；用户白名单、显示名（QQ 官方接口不提供昵称）；文字消息格式（普通文字 / Markdown）；单条消息最多字数；「正在输入」提示（实验） |
| 语音识别 | 识别服务：硅基流动 / 小米 MiMo / 通用 OpenAI 兼容接口；硅基流动模型；MiMo 识别语言。关闭时用 QQ 自带转写 |
| 语音合成 | 音色来源：音色设计（文字描述）/ 预置音色 / 音色复刻（模仿一段录音）；音频格式 MP3 / WAV |
| 联网搜索 | 搜索服务：SearXNG（自建）/ Tavily / 博查；结果条数；读网页开关和字数上限 |
| 拍照 | 画图模型（gpt-image-2 / 2.5-sunburst / 2.5-flare）、尺寸、质量、画风、人物外貌、连贯时长、每天最多张数 |
| 小米 MiMo | API Key（识别和合成共用）；接口地址：按量付费或 Token Plan（包月套餐） |
| 硅基流动、Tavily、博查 | API Key |
| SearXNG | 实例地址（需在其 settings.yml 开启 json 格式）、搜索语言 |
| 通用 OpenAI 兼容接口 | 地址、Key、模型，可接自建 Whisper 等 |
| 图像接口（OpenAI 兼容） | 地址、Key：任意支持 `/images/generations` 和 `/images/edits` 的接口，第三方中转或官方 `https://api.openai.com/v1` |

- 所有 Key 都可以写成 `env:变量名`，从环境变量读取，避免明文写在 `config.toml` 里。
- 音色复刻的录音放到 `MaiBot/data/plugins/yreidev.qq_suite/voices/`（更新插件不会被覆盖），配置里填文件名。
- 拍过的照片（最近 30 张）和定妆照存在 `MaiBot/data/plugins/yreidev.qq_suite/photos/`。
- 语音识别失败时自动退回 QQ 自带的转写文字。

## 用起来是什么样

- 对它说「用语音跟我说晚安」：它会用语音回复；问候、安慰这类短句它也可能自己决定用语音。
- 直接发语音：自动转成文字给它看。
- 「帮我查一下今天的新闻」：它会联网搜索，需要时再打开具体网页读正文。
- 发一个链接问「这篇讲了什么」：它会读网页（需开启读网页）。
- 引用它之前说的某句话再提问：它知道你指的是哪一句。
- 「你在干嘛？拍张照片看看」：它会拍一张照片发给你；「再来一张」「换个姿势」会接着上一张拍。
- 拍到满意的照片后发 `/定妆照`：以后拍它自己都照这个长相（`/定妆照 看` 查看，`/定妆照 清除` 取消）。
- 回复很长时自动按段落分成几条发出。

## 提供给模型的工具

| 工具 | 作用 |
|---|---|
| `qqsuite_web_search` | 联网搜索 |
| `qqsuite_read_url` | 读取网页正文 |
| `qqsuite_send_voice` | 用语音回复（说的内容会写进对话历史，下一轮它知道自己说过什么） |
| `qqsuite_send_photo` | 拍一张照片发出去（可以带定妆照、接着上一张拍） |

聊天命令：`/定妆照`（只有白名单里的用户能用）。

## 隐私与第三方服务

插件本身不收集、不上报任何数据；Key 只保存在本地 `config.toml` 或环境变量里。启用对应功能后，数据会发给这些服务：

| 功能 | 发出去的数据 | 发给谁 |
|---|---|---|
| QQ 收发消息 | 聊天内容、图片、语音 | QQ 开放平台（腾讯） |
| 语音识别 | 你发的语音音频 | 所选识别服务：硅基流动 / 小米 MiMo / 你填写的接口 |
| 语音合成 | 要说的文字、语气、音色描述或复刻录音 | 小米 MiMo |
| 联网搜索 | 搜索词 | 所选搜索服务：你的 SearXNG 实例 / Tavily / 博查 |
| 拍照 | 画面描述、人物外貌、定妆照和上一张照片 | 你填写的图像接口 |
| 读网页 | 访问网页（不带聊天内容） | 网页所在网站 |

各服务如何处理数据，以它们自己的隐私政策为准。

## 常见问题

**回复变成「懒得说」「不知道」「()」？**
这是 MaiBot 的回复分段设置：回复切出来的句子太多时，会被换成一句默认话术。在 MaiBot 的 `bot_config.toml` 里把
`[response_splitter]` 的 `enable_overflow_return_all` 改成 `true`（超长时整段发出），或调大 `max_sentence_num`。

**日志提示「被动回复名额已用完」？**
QQ 规定机器人只能在用户发消息后 1 小时内回复，每条消息最多回复 4 次；超出就只能发主动消息，而主动消息多半没有权限。
插件会把最近一小时内每条消息的名额合起来用，回复较长时也会尽量少分几条。

**回复里出现 `**`、`#` 这类符号？**
「文字消息格式」选「普通文字」时，插件会去掉这些符号再发；选「Markdown」则按格式显示。

**语音没有识别出来？**
看日志里「语音识别」那一行：Key 是否填对、服务是否可用。识别失败会自动改用 QQ 自带的转写。

**语音回复很慢？**
看日志里「发语音给…上传 X 秒」那一行。WAV 体积大，服务器在海外时上传到 QQ 可能要几十秒；默认的 MP3 小得多。

**「正在输入」提示报错？**
这是实验功能，QQ 对它的说明不多。日志里提示发送失败就在配置里关掉。

## 开发

见 [docs/DEVELOPMENT.md](https://github.com/yreidev/maibot-qq-suite/blob/main/docs/DEVELOPMENT.md)；
架构与设计取舍见 [docs/DESIGN.md](https://github.com/yreidev/maibot-qq-suite/blob/main/docs/DESIGN.md)。

## 许可证

[AGPL-3.0](LICENSE)
