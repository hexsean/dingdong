# dingdong

微信定时任务助手。通过 [OpenClaw](https://github.com/Tencent/openclaw-weixin) 接入个人微信，自然语言管理定时任务。

## 快速开始

```bash
git clone https://github.com/hexsean/dingdong.git && cd dingdong
docker compose run --rm dingdong setup   # 选供应商、填 key、扫码
docker compose up -d
```

## 用法

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

## 命令

| 命令 | 说明 |
|------|------|
| `setup` | 交互式配置 + 扫码 |
| `start` | 启动 |
| `status` | 查看配置和任务 |
| `logout` | 清除微信登录态 |

Docker: `docker compose run --rm dingdong <命令>`
本地: `python main.py <命令>`

## 更新

```bash
docker compose pull
docker compose up -d --force-recreate
```

如果要固定到某个版本，把版本号传给 Compose：

```bash
DINGDONG_VERSION=0.3.x docker compose pull
DINGDONG_VERSION=0.3.x docker compose up -d --force-recreate
```

## License

MIT
