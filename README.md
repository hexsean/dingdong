# dingdong

微信定时任务助手。通过 [OpenClaw](https://github.com/Tencent/openclaw-weixin) 接入个人微信，自然语言管理定时任务。

## 快速开始

```bash
git clone https://github.com/hexsean/dingdong.git && cd dingdong
docker compose run --rm dingdong setup   # 选供应商、填 key、扫码
docker compose up -d
```

`setup` 会写入运行配置，默认开启微信内更新并生成 updater 令牌。

之后再次运行 `setup` 修改配置时，正在运行的主服务会自动重启并读取新配置。

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

### 微信里更新

首次 `setup` 默认开启微信内更新并自动生成 updater 令牌。已有部署升级到本版本后，同步新版 `docker-compose.yml`，再重新运行一次：

```bash
docker compose run --rm dingdong setup
docker compose up -d
```

之后微信里发「检查更新」查看版本，发「更新叮咚」并回复「确认更新」即可。
微信更新适用于默认 `latest` 部署；如果使用 `DINGDONG_VERSION=...` 固定版本，请手动改版本号后更新。

## License

MIT
