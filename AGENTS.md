# 叮咚 (dingdong) — Agent 开发规范

## ⚠️ 版本号（必读）

每次修改代码后**必须**更新 `VERSION` 文件：

```
bug 修复    → 第三位 +1（0.2.0 → 0.2.1）
新功能      → 第二位 +1（0.2.1 → 0.3.0）
不兼容改动  → 第一位 +1（0.3.0 → 1.0.0）
```

忘记更新版本号会导致用户收不到更新提醒。

## 项目概述

微信定时任务助手，通过 OpenClaw (ilink) 协议接入个人微信。
用户通过自然语言管理定时任务，任务到点时 LLM 按目标生成内容推送。

## 技术栈

- Python 3.9+
- LLM: Anthropic SDK + OpenAI SDK（双 provider）
- 调度: APScheduler
- 存储: SQLite（任务 + 对话历史）
- 搜索: Exa
- 部署: Docker

## 开发注意事项

1. **时区**: 所有时间代码用 `datetime.now(ZoneInfo(tz))`，禁止 `datetime.now().astimezone()`
2. **安全**: .env 和 data/ 不能进 git 或 Docker 镜像
3. **shortcut**: 只做精确字符串匹配，不做模糊/关键词匹配
4. **工具调用**: quick_reply 只拦截 create_job 和 update_job，其余交 LLM
5. **DeepSeek**: 自动检测并开启 thinking mode（llm.py）
6. **错误处理**: 不向用户暴露原始异常信息
