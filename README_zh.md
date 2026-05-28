<!-- Logo placeholder: replace with actual logo once designed -->
<!-- <p align="center"><img src="assets/logo.svg" width="80" alt="叮咚"></p> -->

<h1 align="center">叮咚</h1>

<p align="center">
微信定时任务助手。接入个人微信后，用自然语言创建、修改和触发定时任务。
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
- 微信内更新——回复「确认更新」即可升级，无需 SSH

## 快速开始

```bash
git clone https://github.com/hexsean/dingdong.git
cd dingdong
docker compose run --rm dingdong setup
docker compose up -d
```

需要公网访问管理台？加 `--profile tunnel` 开启 Cloudflare Tunnel：
```bash
docker compose --profile tunnel up -d
```

`setup` 会引导配置模型、搜索、时区、管理密码和公网访问方式。以后修改配置，再次运行 `setup` 即可，主服务会自动重启。

**从旧版本升级？** 先 `git pull` 更新 `docker-compose.yml`，再重新运行 `setup`。

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
python main.py start    # 启动
python main.py status   # 查看状态
python main.py logout   # 清除登录态
```

## 模型渠道

配置向导内置以下渠道：

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

## 管理台

`setup` 完成后，管理台运行在 `http://你的服务器:8081`。管理密码在 setup 时自动生成。

在管理台可以添加助手、生成二维码发给用户扫码绑定。每个用户的任务和对话完全隔离，LLM 和服务器资源共享。

### 从公网访问

要让用户远程扫码，需要把管理台暴露到公网：

**方案一：Cloudflare Tunnel**（最快，免费 HTTPS，无需域名）

```bash
# 安装 cloudflared 后：
cloudflared tunnel --url http://localhost:8081
```

立即获得一个公网 `https://*.trycloudflare.com` 地址。

**方案二：Nginx 反向代理**

```nginx
server {
    listen 443 ssl;
    server_name dingdong.example.com;
    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
    }
}
```

## 更新

两种更新方式：

**微信内更新**（日常推荐）：

```
检查更新
确认更新
```

**命令行更新**（SSH 到服务器）：

```bash
docker compose pull && docker compose up -d
```

## License

[MIT](LICENSE)
