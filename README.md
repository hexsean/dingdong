# dingdong

微信定时任务助手。接入个人微信后，用自然语言创建、修改和触发定时任务。

## 安装

```bash
git clone https://github.com/hexsean/dingdong.git && cd dingdong
docker compose run --rm dingdong setup
docker compose up -d
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
| `检查更新` | 查看当前版本和最新版本 |
| `更新叮咚` | 在微信里更新到最新版本 |
| `更新状态` | 查看更新结果 |

## 命令行

| 命令 | 说明 |
|------|------|
| `setup` | 配置向导 + 微信登录 |
| `start` | 启动服务 |
| `status` | 查看配置和任务 |
| `logout` | 清除微信登录态 |

Docker：

```bash
docker compose run --rm dingdong <命令>
```

本地：

```bash
python main.py <命令>
```

## 更新

微信里更新：

```
检查更新
更新叮咚
确认更新
更新状态
```

旧版本升级到支持微信内更新：

```bash
git pull
docker compose pull
docker compose run --rm dingdong setup
docker compose up -d
```

命令行更新：

```bash
docker compose pull
docker compose up -d
```

## License

MIT
