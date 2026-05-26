# dingdong

微信定时任务助手。通过 [OpenClaw](https://github.com/Tencent/openclaw-weixin) 接入个人微信，自然语言管理定时任务。

## 安装

```bash
git clone https://github.com/hexsean/dingdong.git && cd dingdong
docker compose run --rm dingdong setup
docker compose up -d
```

`setup` 会完成模型、搜索、时区、微信登录和微信内更新配置。

再次运行 `setup` 修改配置时，主服务会自动重启并读取新配置。

## 微信使用

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
| `检查更新` | 查看当前版本和新版本 |
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

### 微信内更新

默认 `latest` 部署可在微信里更新：

```
检查更新
更新叮咚
确认更新
```

已有部署升级到支持微信内更新的版本后，同步新版 `docker-compose.yml`，执行一次：

```bash
docker compose run --rm dingdong setup
docker compose up -d
```

之后再修改配置只需要运行 `setup`。

### 手动更新

```bash
docker compose pull
docker compose up -d
```

固定版本部署：

```bash
DINGDONG_VERSION=0.5.1 docker compose pull
DINGDONG_VERSION=0.5.1 docker compose up -d
```

## License

MIT
