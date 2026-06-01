<!-- Logo placeholder: replace with actual logo once designed -->
<!-- <p align="center"><img src="assets/logo.svg" width="80" alt="叮咚"></p> -->

<h1 align="center">叮咚</h1>

<p align="center">
1微信定时任务助手。接入个人微信后，用自然语言创建、修改和触发定时任务。
</p>

<p align="center">
🇨🇳 中文 &nbsp;|&nbsp; <a href="README.md">🇺🇸 English</a>
</p>

<p align="center">
<img src="https://img.shields.io/badge/license-MIT-blue" alt="License">
<img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
<img src="https://img.shields.io/badge/docker-ready-blue" alt="Docker">
</p>

---

## 特性

- 微信内自然语言管理任务——"每天 9 点提醒我喝水"
- 支持 cron、间隔、一次性三种调度
- 多模型渠道：Anthropic、OpenAI、DeepSeek、MiMo、OpenRouter、硅基流动、智谱、Moonshot，或任意 OpenAI 兼容接口
- 联网搜索（Exa）和网页读取
- 图片理解（自动检测或独立视觉模型）
- 基于 token 的上下文管理——自动适配模型上下文窗口
- 多账号——一台服务器，多人使用，完全隔离
- Web 管理台，多账号管理
- 微信内更新——回复「确认更新」即可升级，无需 SSH

## 快速开始

```bash
git clone https://github.com/hexsean/dingdong.git
cd dingdong
docker compose run --rm dingdong setup
docker compose up -d
```

`setup` 会引导配置模型、搜索、时区和管理密码。管理密码自动生成并打印，请妥善保存。

## 使用

微信里直接对话：

```
> 每天 9 点提醒我喝水
已创建「每日喝水」，明早 9:00 触发。

> 我有哪些任务
[6dcbc56e] 每日喝水 · cron 0 9 * * * · 提醒我喝水

> 改成每 2 小时 / 暂停 / 删掉 / 现在跑一次
> 搜一下最近的 AI 新闻
> 清空对话
```

常用指令：

| 命令 | 说明 |
|------|------|
| `我有哪些任务` | 查看任务列表 |
| `清空对话` | 重置对话历史 |
| `检查更新` | 查看版本并准备更新 |
| `确认更新` | 执行更新 |

## 管理台

启动后打开 `http://你的服务器:8081`，用 setup 时生成的密码登录。

在管理台可以：
- 添加助手——生成二维码发给用户扫码绑定
- 查看账号状态（在线 / 离线 / 待扫码）
- 删除或重新登录账号

每个用户的任务和对话完全隔离，LLM 和服务器资源共享。

如需公网访问，配置 Nginx 反向代理指向 `http://127.0.0.1:8081` 即可。

## 命令行

| 命令 | 说明 |
|------|------|
| `docker compose run --rm dingdong setup` | 配置 |
| `docker compose run --rm dingdong status` | 查看状态 |
| `docker compose run --rm dingdong logout` | 清除登录态 |
| `docker compose up -d` | 启动 / 重启 |
| `docker compose logs -f dingdong` | 查看日志 |

本地开发（不使用 Docker）：

```bash
python main.py setup    # 配置
python main.py start    # 启动（单账号）
python main.py serve    # 启动（多账号服务器）
python main.py status   # 查看状态
```

## 模型渠道

| 渠道 | 类型 | 默认模型 |
|------|------|----------|
| Anthropic | 原生 | claude-sonnet-4-6 |
| OpenAI | 原生 | gpt-4o-mini |
| MiMo | OpenAI 兼容 | mimo-v2.5-pro |
| DeepSeek | OpenAI 兼容 | deepseek-chat |
| OpenRouter | OpenAI 兼容 | openrouter/auto |
| 硅基流动 | OpenAI 兼容 | Qwen/Qwen3-8B |
| 智谱 GLM | OpenAI 兼容 | glm-4-flash |
| Moonshot | OpenAI 兼容 | moonshot-v1-8k |
| 自定义 | OpenAI 兼容 | 自行填写 |

## 更新

**微信内更新**（推荐）：

```
检查更新
确认更新
```

**命令行更新**（SSH 到服务器）：

```bash
docker compose pull && docker compose up -d
```

**从 v0.7.x 或更早版本升级？**

v0.8.0 更改了 `docker-compose.yml`（默认命令从 `start` 改为 `serve`）。老用户升级：

```bash
cd dingdong
git pull                                  # 拉取新的 docker-compose.yml
docker compose run --rm dingdong setup    # 配置管理密码
docker compose up -d                      # 用新配置重启
```

已有的任务和登录态会自动迁移，不会丢失数据。

## License

[MIT](LICENSE)
