"""叮咚 — 微信定时任务助手。

用法：
    python main.py setup    # 交互式配置 + 扫码
    python main.py start    # 启动 bot
    python main.py logout   # 清除登录态
    python main.py status   # 查看配置和任务数
"""

from __future__ import annotations

import argparse
import logging
import sys


def setup_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    if debug:
        logging.getLogger("dingdong").setLevel(logging.DEBUG)


def cmd_setup(_args: argparse.Namespace) -> int:
    from dingdong.setup import run_setup
    run_setup()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    setup_logging(args.debug)
    from dingdong.bot import Bot
    from dingdong.config import load_config
    cfg = load_config()
    bot = Bot(cfg)
    bot.run()
    return 0


def cmd_logout(_args: argparse.Namespace) -> int:
    from dingdong.config import load_config
    cfg = load_config()
    if cfg.session_path.exists():
        cfg.session_path.unlink()
        print(f"已清除 {cfg.session_path}")
    else:
        print("没有已保存的登录态")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    from dingdong.config import load_config
    from dingdong.storage import JobStore
    from dingdong.updater import local_version
    cfg = load_config()
    print(f"版本:     v{local_version()}")
    print(f"LLM:      {cfg.llm_provider}")
    print(f"数据目录: {cfg.data_dir}")
    print(f"时区:     {cfg.scheduler_tz}")
    print(f"登录态:   {'✓' if cfg.session_path.exists() else '✗'}")
    if cfg.db_path.exists():
        store = JobStore(cfg.db_path)
        jobs = store.list_jobs()
        print(f"任务数:   {len(jobs)}")
        for j in jobs:
            print(f"  {'✓' if j.enabled else '⏸'} {j.name} · {j.schedule_kind}")
        store.close()
    else:
        print("任务数:   0（数据库不存在）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dingdong",
        description="叮咚 — 微信定时任务助手",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="交互式配置 + 微信扫码登录")

    sp_start = sub.add_parser("start", help="启动 bot")
    sp_start.add_argument("--debug", action="store_true", help="DEBUG 日志")

    sub.add_parser("logout", help="清除微信登录态")
    sub.add_parser("status", help="查看当前配置和任务")

    args = parser.parse_args(argv)

    handlers = {
        "setup": cmd_setup,
        "start": cmd_start,
        "logout": cmd_logout,
        "status": cmd_status,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
