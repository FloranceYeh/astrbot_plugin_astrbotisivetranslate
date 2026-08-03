<p align="center">
  <img src="logo.png" alt="AstrBotisive Translate Logo" width="180" />
</p>

<h1 align="center">AstrBot式翻译（AstrBotisive Translate）</h1>

<p align="center">把 AstrBot 人格带进双语网页，让 Bot 陪你翻译、批注、共读并沉淀阅读上下文。</p>

> 喜欢本插件的朋友可以点一个 `Star⭐`，也欢迎在 Issues 里提出建议或反馈问题。欢迎提交 PR，帮助完善功能或修复问题。

> 欢迎加入群聊（1094990582）讨论插件使用、功能建议和问题反馈，也欢迎交流 LLM、Prompt 与 AstrBot 开发。

AstrBotisive Translate 是一款对接 Immersive Translate 的 AstrBot 插件。它把 AstrBot 中已经配置的 LLM 作为 OpenAI 兼容翻译服务，并在单用户模式下保存阅读记录、生成批注与摘要。

## 功能

- `astrbot-translate`：只返回译文。
- `astrbot-annotate`：返回译文和简短的 `〔批注〕`。
- `astrbot-deep-read`：返回译文、`〔解读〕` 和必要的 `〔提示〕`。
- 翻译、批注、深读和摘要统一使用配置中指定的 AstrBot 人格；留空时跟随 `admin_umo` 当前会话人格，再回退到默认人格。
- 支持 `/v1/chat/completions` 非流式与 SSE 流式响应。
- 标准 Immersive Translate 请求可按空闲窗口自动归为一次阅读。
- SQLite 保存原文、译文、批注、滚动摘要与最终摘要，默认保留 30 天。
- 阅读结束后可向 `admin_umo` 主动发送摘要，并写入当前 AstrBot 对话上下文。
- Markdown 阅读笔记导出。

插件面向 AstrBot `4.26.x`，不直接保存任何上游模型密钥；模型密钥继续由 AstrBot Provider 管理。

## 配置

先在 AstrBot 插件配置中设置：

1. 在希望继续讨论文章的聊天中发送 `/sid`，把得到的完整 UMO 填入 `admin_umo`。
2. 在“翻译人格”中选择一个 AstrBot 人格。留空时跟随 `admin_umo` 当前会话人格，再回退到 AstrBot 默认人格。
3. 为三个翻译模式和摘要分别选择 AstrBot Provider。留空时使用 `admin_umo` 当前 Provider，再回退到默认 Provider。
4. Windows 本机使用 `server.host = 127.0.0.1`。Docker 使用 `0.0.0.0` 并映射 `server.port`。
5. 只要监听地址不是本机回环地址，`server.api_key` 就必须设置，否则 HTTP 服务拒绝启动。

`admin_umo` 留空时，纯翻译接口仍能工作，但阅读保存、摘要、主动消息、上下文注入和聊天命令全部关闭。

主动摘要通过 AstrBot 的统一 UMO 发送接口投递，不绑定 QQ、Telegram、飞书、Discord 等具体平台。最终可用性取决于平台适配器是否支持主动消息；AstrBot 4.26.5 的 QQ 官方 API 适配器不支持该接口，失败信息会保留在阅读记录中。

## Immersive Translate

在 Immersive Translate 中添加 OpenAI 兼容的自定义 AI 服务：

```text
API Base URL: http://127.0.0.1:8756/v1
Chat Completions URL: http://127.0.0.1:8756/v1/chat/completions
API Key: 与插件 server.api_key 相同；本机未配置 Key 时可填任意非空占位值
Model: astrbot-translate
```

不同版本的 Immersive Translate 可能要求填写 Base URL 或完整 Chat Completions URL，按其字段选择上面对应的值。需要切换模式时，把 Model 改成：

```text
astrbot-translate
astrbot-annotate
astrbot-deep-read
```

也可以在 Immersive Translate 中复制三份自定义服务配置，每份使用一个模型名。

验证服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8756/health
Invoke-RestMethod http://127.0.0.1:8756/v1/models -Headers @{ Authorization = "Bearer YOUR_KEY" }
```

## Docker 与公网

Docker Compose 需要给 AstrBot 服务增加端口映射：

```yaml
services:
  astrbot:
    ports:
      - "8756:8756"
```

插件配置使用：

```text
server.host = 0.0.0.0
server.port = 8756
server.api_key = 一段足够长的随机值
```

公网部署应在 Caddy、Nginx 等反向代理后提供 HTTPS。不要把无 TLS 的 `8756` 端口直接暴露到公网；API Key 会随每次请求发送。插件自带单 Key 鉴权、请求大小限制、并发限制和每分钟限流，但不代替防火墙与 HTTPS。

## 阅读命令

只有配置的 `admin_umo` 会话可以执行：

```text
/ait status
/ait articles
/ait summary [article_id]
/ait finish [article_id]
/ait export [article_id]
/ait forget [article_id]
```

省略 `article_id` 时操作最近一篇阅读。`/ait finish` 可在 30 分钟空闲超时前手动结束自动阅读。

## 数据

SQLite 文件和导出的 Markdown 位于 AstrBot 的插件数据目录：

```text
data/plugin_data/astrbot_plugin_astrbotisivetranslate/
```

`/ait forget` 会删除指定文章及所有片段。保留天数设为 `0` 时不会自动清理。
