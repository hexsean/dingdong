# dingdong

微信定时任务助手。通过 [OpenClaw](https://github.com/Tencent/openclaw-weixin) 协议接入个人微信，用自然语言管理定时任务。

## 功能

- 自然语言创建/修改/删除定时任务（cron、interval、一次性）
- 任务到点时 LLM 按目标生成内容，推送到微信
- 联网搜索（Exa，可选）
- 对话历史持久化，重启不丢
- 支持 Anthropic Claude / OpenAI / 兼容接口（Moonshot、DeepSeek 等）

## 快速开始

### Docker（推荐）

```bash
git clone https://github.com/你的用户名/dingdong.git && cd dingdong
docker compose run --rm dingdong setup
docker compose up -d
```

### 本地运行

```bash
git clone https://github.com/你的用户名/dingdong.git && cd dingdong
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py setup   # 交互式配置 + 微信扫码
python main.py start
```

## 命令

```
python main.py setup    # 交互式配置（LLM、搜索、时区、扫码）
python main.py start    # 启动 bot
python main.py status   # 查看配置和任务
python main.py logout   # 清除微信登录态
```

## 使用示例

在微信里直接对话：

```
你：每天早上 9 点提醒我喝水
叮咚：已创建「每日喝水」，明早 9:00 第一次触发。

你：我有哪些任务
叮咚：[6dcbc56e] 每日喝水 · cron 0 9 * * * · 提醒我喝水

你：改成每 2 小时一次
叮咚：已更新。

你：现在跑一次
叮咚：已触发。
（收到 LLM 生成的提醒）

你：暂停 / 删掉它
叮咚：已暂停。/ 已删除。

你：清空对话
叮咚：已清空对话记录。

你：搜一下最近的 AI 新闻
叮咚：（搜索结果摘要）
```

## 配置

`setup` 会自动生成 `.env`，也可手动编辑：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | `anthropic` 或 `openai` | `anthropic` |
| `ANTHROPIC_API_KEY` | Claude API Key | |
| `OPENAI_API_KEY` | OpenAI 兼容 API Key | |
| `OPENAI_BASE_URL` | 自定义 endpoint | `https://api.openai.com/v1` |
| `EXA_API_KEY` | Exa 搜索（可选） | |
| `SCHEDULER_TZ` | 时区 | `Asia/Shanghai` |
| `HISTORY_LIMIT` | 对话历史条数 | `20` |
| `ALLOWED_USER_IDS` | 白名单，逗号分隔 | 空=不限 |

## 项目结构

```
main.py              # CLI 入口
dingdong/
├── bot.py           # 消息循环
├── ilink.py         # 微信 ilink 协议
├── login.py         # 扫码登录
├── intent.py        # 自然语言 → 工具调用
├── executor.py      # 任务触发 → LLM → 推送
├── scheduler.py     # APScheduler 封装
├── storage.py       # SQLite（任务 + 对话历史）
├── llm.py           # LLM 双 provider
├── search.py        # Exa 搜索
├── setup.py         # 交互式引导
└── config.py        # 配置
```

## 注意事项

- 微信 ilink 接口是腾讯公开但未承诺稳定的接口，可能随时变更
- 仅支持文本消息，不支持图片/语音/群聊
- `context_token` 长期不活动会失效，给 bot 发条消息即可刷新

## License

MIT
