# dingdong

微信定时任务助手。接入个人微信后，用自然语言创建、修改和触发定时任务。

## 安装

```bash
git clone https://github.com/hexsean/dingdong.git && cd dingdong && docker compose run --rm dingdong setup && docker compose up -d
```

`setup` 会完成模型、搜索、时区、微信登录和微信内更新配置。以后修改配置，只需要再次运行 `setup`，主服务会自动重启。

## 微信入口

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

配置：

```bash
docker compose run --rm dingdong setup
```

查看状态：

```bash
docker compose run --rm dingdong status
```

清除登录态：

```bash
docker compose run --rm dingdong logout
```

启动或重启服务：

```bash
docker compose up -d
```

查看日志：

```bash
docker compose logs -f dingdong
```

本地开发启动：

```bash
python main.py start
```

本地配置：

```bash
python main.py setup
```

本地查看状态：

```bash
python main.py status
```

本地清除登录态：

```bash
python main.py logout
```

## 更新

叮咚支持两种更新方式：日常使用推荐微信内更新；需要在服务器上操作时，用命令行更新。

微信内更新会自动推送进度和完成消息：

```
检查更新
确认更新
```

命令行更新适合 SSH 到服务器后执行：

```bash
docker compose pull && docker compose up -d
```

## License

MIT
