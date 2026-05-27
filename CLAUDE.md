# 叮咚 (dingdong) — 开发规范

## 版本号

版本号在 `VERSION` 文件中，格式 `MAJOR.MINOR.PATCH`。

**每次修改代码后必须更新版本号：**
- bug 修复 / 小调整 → PATCH +1（如 0.2.0 → 0.2.1）
- 新功能 → MINOR +1，PATCH 归零（如 0.2.1 → 0.3.0）
- 不兼容改动 → MAJOR +1（如 0.3.0 → 1.0.0）

涉及前两位版本号变更（MAJOR 或 MINOR）前，必须先询问用户确认是升大版本号还是小版本号；未经确认只能做 PATCH 升级。

## 部署

本地开发用 `./deploy.sh "commit message"` 一键提交+构建+推送。
deploy.sh 自动从 VERSION 读取版本号，打 latest + 版本标签。

## 项目结构

```
VERSION              # 版本号（必须与每次代码变更同步）
dingdong/
├── cli.py           # CLI 入口（setup/start/status/logout）
├── bot.py           # 消息循环 + typing + 更新检测
├── intent.py        # 自然语言 → 工具调用（system prompt 在这里）
├── executor.py      # 定时任务触发 → LLM → 推送
├── ilink.py         # 微信 ilink 协议
├── llm.py           # LLM 双 provider（Anthropic + OpenAI 兼容）
├── scheduler.py     # APScheduler 封装
├── storage.py       # SQLite（jobs + chat_history）
├── search.py        # Exa 搜索 + read_url
├── setup.py         # 交互式引导
├── updater.py       # 版本更新检测
├── login.py         # 扫码登录
└── config.py        # 配置
```

## 关键规则

- Docker 镜像不能包含 .env 和 data/（.dockerignore 控制）
- 用户配置存在 data/.env（挂载卷持久化）
- system prompt 在 intent.py 的 INTENT_SYSTEM_PROMPT
- 时间相关代码必须用 ZoneInfo(scheduler_tz)，不能用 datetime.now().astimezone()
- DeepSeek 自动开启 thinking（llm.py 检测 base_url）
- shortcut 只做精确匹配，其余全交 LLM
