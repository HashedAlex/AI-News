# AI 报童

一个面向 Telegram 的 AI 资讯推送机器人。

它会定时抓取配置好的 XCancel RSS 源，识别每个账号的最新推文，通过 OpenRouter 调用大模型将内容翻译成中文，再用更适合阅读的 HTML 格式推送给订阅用户。

项目的设计目标很明确：

- 低功耗：平时只休眠，按整点 `05` 分唤醒执行一次扫描
- 可持续部署：数据落在本地 SQLite，适合 Railway Volume 或其他持久化磁盘
- 低维护：订阅、退订、失效用户清理都自动完成
- 可读性强：推送消息包含作者、时间、中文内容和原始链接

## 功能亮点

- 多订阅用户支持
  用户发送 `/start` 即可订阅，发送 `/stop` 即可取消订阅。

- 按账号增量抓取
  每个 RSS 源都会在 SQLite 中保存自己的 `last_id`，避免重复推送。

- LLM 中文翻译
  通过 OpenRouter 调用模型，把原始推文翻译成自然、流畅、保留技术术语的中文。

- 翻译失败自动降级
  即使 OpenRouter 超时、Key 缺失或模型异常，消息仍会继续发送，只是回退为英文原文。

- Telegram 友好广播
  发送时自动做短暂延迟，降低限流风险；遇到封禁或失效用户会自动清理订阅记录。

- Railway 友好
  支持 `DATA_DIR`，数据库默认写入 `./data/bot_data.db`，方便挂载持久卷。

## 消息效果

机器人发送的消息格式如下：

```html
👤 OpenAI

📅 2026-03-26 16:05

中文内容：
这里是翻译后的中文内容。

🔗 原始链接
```

其中时间会统一转换为新加坡时间 `Asia/Singapore`。

## 项目结构

```text
.
├── config.py          # RSS 源配置
├── main.py            # Bot 主逻辑
├── pyproject.toml     # Python 依赖
├── .env.example       # 环境变量示例
└── data/
    └── bot_data.db    # SQLite 数据库，运行后自动生成
```

## 工作流程

每轮任务的执行顺序如下：

1. 到达每小时的 `05` 分
2. 抓取所有 RSS 源
3. 读取每个账号在 SQLite 中保存的 `last_id`
4. 判断是否出现新推文
5. 对新推文调用 OpenRouter 进行中文翻译
6. 组装 HTML 消息
7. 广播给所有订阅者
8. 更新数据库中的最新 `tweet_id`

这样做的好处是：

- 不会重复推送旧内容
- 新订阅用户不会收到大量历史消息
- 空闲时几乎不耗 CPU

## 环境要求

- Python `3.11+`
- 一个 Telegram Bot Token
- 一个 OpenRouter API Key

## 安装

如果你使用 `uv`：

```bash
uv sync
```

如果你使用 `pip`：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 配置

先复制环境变量模板：

```bash
cp .env.example .env
```

然后填写 `.env`：

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
DATA_DIR=./data
OPENROUTER_API_KEY=your_openrouter_api_key_here
OPENROUTER_MODEL=google/gemini-2.0-flash-lite-preview-01
```

### 环境变量说明

- `TELEGRAM_BOT_TOKEN`
  Telegram 机器人令牌，必填。

- `DATA_DIR`
  数据目录，默认是 `./data`。数据库文件会写入 `DATA_DIR/bot_data.db`。

- `OPENROUTER_API_KEY`
  OpenRouter Key。若不填写，机器人仍可运行，但翻译会失败并自动回退到英文原文。

- `OPENROUTER_MODEL`
  使用的翻译模型，默认值为 `google/gemini-2.0-flash-lite-preview-01`。

## 配置监控源

你可以在 [config.py](/Users/kaximoduoduo/Desktop/Coding/AI-news/config.py) 中维护需要监控的 AI 账号：

```python
RSS_FEEDS: dict[str, str] = {
    "OpenAI": "https://xcancel.com/OpenAI/rss",
    "Anthropic (Claude)": "https://xcancel.com/AnthropicAI/rss",
    "Google DeepMind": "https://xcancel.com/GoogleDeepMind/rss",
}
```

键名会被当作消息中的作者名显示，也会出现在 `/list` 命令结果里。

## 启动

```bash
.venv/bin/python main.py
```

或：

```bash
python main.py
```

启动后，机器人会：

- 初始化 SQLite 数据库
- 连接 Telegram 长轮询
- 启动后台调度循环
- 等待下一个整点 `05` 分执行抓取

## Telegram 命令

- `/start`
  订阅机器人。欢迎语为：`欢迎订阅 AI 报童！`

- `/stop`
  取消订阅。

- `/list`
  查看当前监控的 AI 账号列表。

## 数据存储

SQLite 数据库包含两张表：

- `subscribers`
  保存所有订阅用户的 `chat_id`

- `seen_posts`
  保存每个账号最后一次处理过的 `tweet_id`

这意味着：

- 不依赖大型 JSON 文件
- 适合 Railway Persistent Volume
- 重启后仍能延续增量抓取状态

## 广播与容错策略

为了让机器人在长期运行中更稳定，项目做了几层保护：

- 每条 Telegram 消息之间等待 `0.05s`
- `send_message` 使用 `try...except` 包裹
- 如果用户屏蔽 bot 或账号失效，会自动从订阅表中删除
- 如果翻译失败，会打印日志并直接发送英文原文
- 如果某个 RSS 源抓取失败，不会阻塞其他源继续处理

## 适合部署到 Railway

推荐配置：

- Service：Python
- Start Command：

```bash
python main.py
```

- 挂载 Volume 到：

```text
/app/data
```

- 环境变量示例：

```env
TELEGRAM_BOT_TOKEN=...
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=google/gemini-2.0-flash-lite-preview-01
DATA_DIR=/app/data
```

这样部署后，即使服务重启，订阅用户和已处理推文记录也不会丢失。

## 常见问题

### 1. 为什么机器人启动后没有立刻抓取？

这是预期行为。项目采用低功耗调度逻辑，只会在每小时的 `05` 分启动一次扫描，例如：

- `14:05`
- `15:05`
- `16:05`

### 2. 没有配置 OpenRouter Key 会怎样？

机器人仍能运行，但翻译步骤会失败，然后自动回退为原始英文内容，不会阻塞推送。

### 3. 为什么新订阅后没有收到历史消息？

因为项目按账号保存 `last_id`，只处理增量更新，避免用户被历史内容刷屏。

### 4. 可以加更多 RSS 源吗？

可以，直接修改 [config.py](/Users/kaximoduoduo/Desktop/Coding/AI-news/config.py) 中的 `RSS_FEEDS` 即可。

## 开发建议

如果你准备继续扩展这个项目，优先建议做这些事情：

- 增加测试，覆盖 RSS 解析、调度时间计算和消息格式化
- 增加日志分级，而不是直接使用 `print`
- 在数据库中保存更多元数据，例如 `published_at`、`link`
- 支持摘要、标签分类、重要程度排序
- 支持按用户维度自定义订阅源

## License

