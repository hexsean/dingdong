"""叮咚 — 微信定时任务助手。

用法：
    python main.py setup    # 交互式配置 + 扫码（单账号）
    python main.py start    # 启动 bot（单账号）
    python main.py serve    # 多账号服务器模式
    python main.py logout   # 清除登录态
    python main.py status   # 查看配置和任务数

    python main.py account list             # 列出所有账号
    python main.py account add [--label X]  # 添加账号（交互式扫码）
    python main.py account remove <id>      # 删除账号
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


def cmd_serve(args: argparse.Namespace) -> int:
    setup_logging(args.debug)
    from dingdong.config import load_config
    from dingdong.server import Server
    cfg = load_config()
    server = Server(cfg)
    server.run()
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
    from dingdong.self_update import update_configured
    from dingdong.storage import JobStore
    from dingdong.updater import local_version
    cfg = load_config()
    print(f"版本:     v{local_version()}")
    print(f"LLM:      {cfg.llm_provider}")
    print(f"数据目录: {cfg.data_dir}")
    print(f"时区:     {cfg.scheduler_tz}")
    print(f"微信更新: {'✓' if update_configured(cfg.wechat_update_enabled, cfg.watchtower_token) else '✗'}")
    print(f"登录态:   {'✓' if cfg.session_path.exists() else '✗'}")
    if cfg.db_path.exists():
        store = JobStore(cfg.db_path)
        accounts = store.list_accounts()
        if accounts:
            print(f"账号数:   {len(accounts)}")
            for a in accounts:
                role = " (admin)" if a.is_admin else ""
                jobs = store.list_jobs(account_id=a.id)
                print(f"  [{a.id}] {a.label or '未命名'}{role} · {a.status} · {len(jobs)} 个任务")
        else:
            jobs = store.list_jobs()
            print(f"任务数:   {len(jobs)}")
            for j in jobs:
                print(f"  {'✓' if j.enabled else '⏸'} {j.name} · {j.schedule_kind}")
        store.close()
    else:
        print("任务数:   0（数据库不存在）")
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    from dingdong.config import load_config
    from dingdong.storage import JobStore, Account, new_account_id
    cfg = load_config()
    store = JobStore(cfg.db_path)

    action = args.account_action

    if action == "list":
        accounts = store.list_accounts()
        if not accounts:
            print("暂无账号。使用 `account add` 添加。")
        for a in accounts:
            role = " (admin)" if a.is_admin else ""
            jobs = store.list_jobs(account_id=a.id)
            print(f"  [{a.id}] {a.label or '未命名'}{role} · {a.status} · {len(jobs)} 个任务")
        store.close()
        return 0

    if action == "add":
        label = args.label or ""
        has_admin = store.get_admin_account() is not None
        account = Account(id=new_account_id(), label=label, is_admin=not has_admin, status="active")
        store.insert_account(account)
        print(f"  已创建账号 [{account.id}]{' (admin)' if account.is_admin else ''}")

        from dingdong.account_runner import AccountRunner
        from dingdong.llm import build_provider, build_vision_provider
        from dingdong.models import fetch_model_info
        from dingdong.scheduler import Scheduler
        from dingdong.search import init_exa

        llm = build_provider(cfg)
        init_exa(cfg.exa_api_key)
        scheduler = Scheduler(store, lambda job: None, cfg.scheduler_tz)

        runner = AccountRunner(
            account_id=account.id,
            store=store,
            scheduler=scheduler,
            llm=llm,
            cfg=cfg,
            is_admin=account.is_admin,
        )
        if not runner.login_blocking():
            print("  登录失败。账号已创建，稍后用 `account add` 重试登录。")
            store.close()
            return 1
        print(f"  账号 [{account.id}] 登录成功。")
        store.close()
        return 0

    if action == "remove":
        account_id = args.account_id
        account = store.get_account(account_id)
        if not account:
            print(f"  账号 {account_id} 不存在。")
            store.close()
            return 1
        ok = store.delete_account(account_id)
        if ok:
            import shutil
            session_dir = cfg.data_dir / "sessions" / account_id
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            print(f"  已删除账号 [{account_id}] 及其所有任务和对话记录。")
        store.close()
        return 0

    print("未知操作。使用 list / add / remove。")
    store.close()
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dingdong",
        description="叮咚 — 微信定时任务助手",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="交互式配置 + 微信扫码登录")

    sp_start = sub.add_parser("start", help="启动 bot（单账号模式）")
    sp_start.add_argument("--debug", action="store_true", help="DEBUG 日志")

    sp_serve = sub.add_parser("serve", help="多账号服务器模式")
    sp_serve.add_argument("--debug", action="store_true", help="DEBUG 日志")

    sub.add_parser("logout", help="清除微信登录态")
    sub.add_parser("status", help="查看当前配置和任务")

    sp_account = sub.add_parser("account", help="账号管理")
    account_sub = sp_account.add_subparsers(dest="account_action")
    account_sub.add_parser("list", help="列出所有账号")
    sp_add = account_sub.add_parser("add", help="添加账号（交互式扫码）")
    sp_add.add_argument("--label", default="", help="账号标签")
    sp_rm = account_sub.add_parser("remove", help="删除账号")
    sp_rm.add_argument("account_id", help="账号 ID")

    args = parser.parse_args(argv)

    handlers = {
        "setup": cmd_setup,
        "start": cmd_start,
        "serve": cmd_serve,
        "logout": cmd_logout,
        "status": cmd_status,
        "account": cmd_account,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
