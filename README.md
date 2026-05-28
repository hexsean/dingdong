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
- Multi-account — one server, multiple users, fully isolated
- Admin panel with Cloudflare Tunnel auto-setup
- WeChat in-chat update — reply "confirm update" to upgrade without SSH

## Quick Start

```bash
git clone https://github.com/hexsean/dingdong.git
cd dingdong
docker compose run --rm dingdong setup
docker compose up -d
```

`setup` walks you through model, search, timezone, admin password, and public access. The admin password is auto-generated and printed — save it.

If you chose Cloudflare Tunnel during setup, the public URL appears in the admin panel automatically after startup. No domain or certificate needed.

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

## Admin Panel

After startup, open `http://your-server:8081` and log in with the admin password from setup.

From the panel you can:
- Add assistants — generate QR codes and send to users
- View account status (online / offline / pending)
- Remove or re-login accounts

Each user's tasks and conversations are fully isolated. LLM and server resources are shared.

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
python main.py start    # run (single-account)
python main.py serve    # run (multi-account server)
python main.py status   # check status
```

## Model Providers

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

## Update

**In WeChat** (recommended):

```
check update
confirm update
```

**Command line** (via SSH):

```bash
docker compose pull && docker compose up -d
```

**Upgrading from v0.7.x or earlier?**

v0.8.0 changes `docker-compose.yml` (default command switched from `start` to `serve`). Existing users:

```bash
cd dingdong
git pull                                  # get new docker-compose.yml
docker compose run --rm dingdong setup    # configure admin password + tunnel
docker compose up -d                      # restart with new config
```

Your existing tasks and login session are automatically migrated. No data loss.

## License

[MIT](LICENSE)
