<!-- Logo placeholder: replace with actual logo once designed -->
<!-- <p align="center"><img src="assets/logo.svg" width="80" alt="dingdong"></p> -->

<h1 align="center">dingdong</h1>

<p align="center">
WeChat scheduled-task assistant. Create, modify and trigger timed tasks with natural language.
</p>

<p align="center">
<a href="README_zh.md">🇨🇳 中文</a> &nbsp;|&nbsp; 🇺🇸 English
</p>

<p align="center">
<img src="https://img.shields.io/badge/license-MIT-blue" alt="License">
<img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
<img src="https://img.shields.io/badge/docker-ready-blue" alt="Docker">
</p>

---

## Features

- Natural language task management via WeChat — "remind me to drink water at 9am every day"
- Supports cron, interval and one-time schedules
- Multi-provider LLM: Anthropic, OpenAI, DeepSeek, MiMo, OpenRouter, SiliconFlow, Zhipu, Moonshot, or any OpenAI-compatible endpoint
- Web search (Exa) and URL reading
- Image understanding (auto-detect or separate vision model)
- Token-aware context management — adapts to your model's context window
- WeChat in-chat update — reply "confirm update" to upgrade without SSH

## Quick Start

```bash
git clone https://github.com/hexsean/dingdong.git
cd dingdong
docker compose run --rm dingdong setup
docker compose up -d
```

Need public access? Add `--profile tunnel` to enable Cloudflare Tunnel:
```bash
docker compose --profile tunnel up -d
```

`setup` walks you through model, search, timezone, admin password, and public access. Re-run it anytime to change config — the service restarts automatically.

**Upgrading from an older version?** Run `git pull` first to update `docker-compose.yml`, then re-run `setup`.

## Usage

Talk to it in WeChat:

```
> Remind me to drink water at 9am every day
Created "daily water reminder", next trigger: tomorrow 09:00.

> What tasks do I have
[6dcbc56e] daily water reminder · cron 0 9 * * * · remind me to drink water

> Change to every 2 hours / pause / delete / run now
> Search recent AI news
> Clear chat
```

Quick reference:

| Command | Action |
|---------|--------|
| `list my tasks` | Show all tasks |
| `clear chat` | Reset conversation history |
| `check update` | Check for new version |
| `confirm update` | Execute the update |

## CLI

| Command | Description |
|---------|-------------|
| `docker compose run --rm dingdong setup` | Configure |
| `docker compose run --rm dingdong status` | View status |
| `docker compose run --rm dingdong logout` | Clear login session |
| `docker compose up -d` | Start / restart |
| `docker compose logs -f dingdong` | View logs |

Local development (without Docker):

```bash
python main.py setup    # configure
python main.py start    # run
python main.py status   # check status
python main.py logout   # clear session
```

## Model Providers

The setup wizard supports these providers out of the box:

| Provider | Type | Default Model |
|----------|------|---------------|
| Anthropic | Native | claude-sonnet-4-6 |
| OpenAI | Native | gpt-4o-mini |
| MiMo | OpenAI-compatible | mimo-v2.5-pro |
| DeepSeek | OpenAI-compatible | deepseek-chat |
| OpenRouter | OpenAI-compatible | openrouter/auto |
| SiliconFlow | OpenAI-compatible | Qwen/Qwen3-8B |
| Zhipu GLM | OpenAI-compatible | glm-4-flash |
| Moonshot | OpenAI-compatible | moonshot-v1-8k |
| Custom | OpenAI-compatible | (your choice) |

## Admin Panel

After `setup`, the admin panel runs at `http://your-server:8081`. From there you can add assistants, generate QR codes for users, and manage accounts. The admin password is auto-generated during setup.

Multiple users can each scan a QR code to bind their own dingdong assistant. Tasks and conversations are fully isolated; LLM and server resources are shared.

### Public Access

To let users scan QR codes remotely, expose the admin panel to the internet:

**Option A: Cloudflare Tunnel** (quickest, free HTTPS, no domain needed)

```bash
# Install cloudflared, then:
cloudflared tunnel --url http://localhost:8081
```

Gives you a public `https://*.trycloudflare.com` URL instantly.

**Option B: Nginx reverse proxy**

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

## Update

Two ways to update:

**In WeChat** (recommended for daily use):

```
check update
confirm update
```

**Command line** (via SSH):

```bash
docker compose pull && docker compose up -d
```

## License

[MIT](LICENSE)
