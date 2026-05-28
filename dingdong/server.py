"""多账号服务器。

管理多个 AccountRunner 实例，共享 LLM / JobStore / Scheduler。
提供 Admin API 供管理账号生命周期。
"""

from __future__ import annotations

import logging
import shutil
import signal
import threading
from pathlib import Path
from typing import Any

from .account_runner import AccountRunner
from .config import Config
from .ilink import ILinkClient
from .llm import build_provider, build_vision_provider
from .login import start_qr_login, poll_login_status
from .models import fetch_model_info
from .scheduler import Scheduler
from .search import init_exa
from .self_update import (
    WECHAT_UPDATE_DISABLED_HINT,
    read_update_result,
    update_configured,
    write_update_result,
)
from .storage import Account, JobStore, new_account_id
from .updater import CHECK_ID, is_disabled, is_newer_version, local_version, remote_version

log = logging.getLogger(__name__)


class Server:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._store = JobStore(cfg.db_path)
        self._llm = build_provider(cfg)
        self._vision_llm = build_vision_provider(cfg)
        init_exa(cfg.exa_api_key)

        main_model = cfg.anthropic_model if cfg.llm_provider == "anthropic" else cfg.openai_model
        vision_model_name = cfg.vision_model if self._vision_llm else main_model
        vision_override = True if self._vision_llm else cfg.vision_enabled
        self._model_info = fetch_model_info(
            vision_model_name, data_dir=cfg.data_dir, vision_override=vision_override,
        )
        if self._vision_llm:
            main_info = fetch_model_info(main_model, data_dir=cfg.data_dir)
            self._context_length = cfg.context_length or (main_info.context_length if main_info else 0) or 0
        else:
            self._context_length = cfg.context_length or (
                self._model_info.context_length if self._model_info else 0
            ) or 0

        self._scheduler = Scheduler(self._store, self._dispatch_job, cfg.scheduler_tz)
        self._runners: dict[str, AccountRunner] = {}
        self._runners_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_update_notice_version: str | None = None

        self._login_sessions: dict[str, dict[str, Any]] = {}
        self._login_lock = threading.Lock()

    # ── main ──

    def run(self) -> None:
        self._install_signal_handlers()
        self._maybe_migrate_single_tenant()
        self._start_active_accounts()
        self._scheduler.start()
        self._register_update_check()
        self._start_restart_watcher()
        self._notify_update_result_on_startup()
        with self._runners_lock:
            n_runners = len(self._runners)
        log.info("server v%s online; %d account(s)", local_version(), n_runners)

        from .admin_api import AdminAPI
        api = AdminAPI(self, port=self._cfg.admin_api_port, password=self._cfg.admin_password)
        api.start()

        self._stop.wait()
        api.stop()
        self._shutdown()

    # ── account management (public, for AdminAPI) ──

    def create_account(self, label: str = "") -> Account:
        account = Account(id=new_account_id(), label=label, status="pending_login")
        has_admin = self._store.get_admin_account() is not None
        if not has_admin:
            account.is_admin = True
        self._store.insert_account(account)
        log.info("created account %s (admin=%s)", account.id, account.is_admin)
        return account

    def remove_account(self, account_id: str) -> bool:
        with self._runners_lock:
            runner = self._runners.pop(account_id, None)
        if runner:
            runner.stop()
        ok = self._store.delete_account(account_id)
        self._login_sessions.pop(account_id, None)
        session_dir = self._cfg.data_dir / "sessions" / account_id
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
        log.info("removed account %s (found=%s)", account_id, ok)
        return ok

    def list_accounts(self) -> list[dict[str, Any]]:
        accounts = self._store.list_accounts()
        result = []
        for a in accounts:
            with self._runners_lock:
                runner = self._runners.get(a.id)
            result.append({
                "id": a.id,
                "label": a.label,
                "is_admin": a.is_admin,
                "status": a.status,
                "online": runner.is_running() if runner else False,
                "created_at": a.created_at,
            })
        return result

    def get_account(self, account_id: str) -> Account | None:
        return self._store.get_account(account_id)

    def start_login(self, account_id: str) -> dict[str, Any]:
        account = self._store.get_account(account_id)
        if not account:
            return {"error": "account not found"}

        def factory(token: str | None) -> ILinkClient:
            return ILinkClient(bot_token=token, long_poll_timeout_ms=self._cfg.long_poll_timeout_ms)

        session_dir = self._cfg.data_dir / "sessions" / account_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / "session.json"
        qrcode_path = session_dir / "qrcode.png"

        result = start_qr_login(session_path, qrcode_path, factory)
        if result is None:
            self._activate_account(account_id)
            return {"status": "already_logged_in"}

        client, qr_url, poll_token = result
        with self._login_lock:
            self._login_sessions[account_id] = {
                "client": client,
                "poll_token": poll_token,
                "session_path": session_path,
                "qrcode_path": qrcode_path,
                "factory": factory,
            }
        return {"status": "waiting", "qr_url": qr_url, "qr_png": str(qrcode_path)}

    def poll_login(self, account_id: str) -> dict[str, Any]:
        with self._login_lock:
            ls = self._login_sessions.get(account_id)
        if not ls:
            return {"status": "no_login_session"}

        result = poll_login_status(
            ls["client"], ls["poll_token"], ls["session_path"], ls["factory"]
        )

        if result.get("status") == "confirmed":
            with self._login_lock:
                self._login_sessions.pop(account_id, None)
            self._store.update_account(account_id, status="active")
            self._activate_account(account_id)
            return {"status": "confirmed"}

        if result.get("status") == "expired":
            with self._login_lock:
                self._login_sessions.pop(account_id, None)
            return {"status": "expired"}

        return result

    # ── internal ──

    def _activate_account(self, account_id: str) -> None:
        account = self._store.get_account(account_id)
        if not account:
            return
        runner = self._make_runner(account)
        if runner.restore_session():
            runner.start()
            with self._runners_lock:
                self._runners[account_id] = runner
            log.info("account %s activated", account_id)
        else:
            log.warning("account %s has no session; needs login", account_id)

    def _make_runner(self, account: Account) -> AccountRunner:
        return AccountRunner(
            account_id=account.id,
            store=self._store,
            scheduler=self._scheduler,
            llm=self._llm,
            cfg=self._cfg,
            vision_llm=self._vision_llm,
            model_info=self._model_info,
            context_length=self._context_length,
            is_admin=account.is_admin,
        )

    def _start_active_accounts(self) -> None:
        for account in self._store.list_accounts(status="active"):
            runner = self._make_runner(account)
            if runner.restore_session():
                runner.start()
                with self._runners_lock:
                    self._runners[account.id] = runner
            else:
                log.warning("account %s: no session, skipping", account.id)
                self._store.update_account(account.id, status="pending_login")

    def _dispatch_job(self, job: Any) -> None:
        with self._runners_lock:
            runner = self._runners.get(job.account_id)
            if runner is None and not job.account_id:
                runners = list(self._runners.values())
                if runners:
                    runner = runners[0]
        if runner is None:
            log.warning("no runner for account %s; skipping job %s", job.account_id, job.id)
            return
        runner.execute_job(job)

    def _maybe_migrate_single_tenant(self) -> None:
        old_session = self._cfg.data_dir / "session.json"
        if not old_session.exists():
            return
        if self._store.list_accounts():
            return
        log.info("migrating single-tenant session to multi-tenant")
        account = Account(id=new_account_id(), label="admin", is_admin=True, status="active")
        self._store.insert_account(account)
        session_dir = self._cfg.data_dir / "sessions" / account.id
        session_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_session, session_dir / "session.json")
        n = self._store.migrate_account_id(account.id)
        old_session.rename(old_session.with_suffix(".json.migrated"))
        log.info("migrated %d rows to account %s", n, account.id)

    # ── update check (admin only) ──

    def _register_update_check(self) -> None:
        if is_disabled(self._cfg.data_dir):
            return
        from apscheduler.triggers.interval import IntervalTrigger
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self._cfg.scheduler_tz)
        from datetime import datetime, timedelta
        first_run = datetime.now(tz) + timedelta(seconds=30)
        self._scheduler._scheduler.add_job(
            self._check_update,
            trigger=IntervalTrigger(minutes=5, timezone=tz),
            id=CHECK_ID,
            replace_existing=True,
            next_run_time=first_run,
        )

    def _check_update(self) -> None:
        if is_disabled(self._cfg.data_dir):
            return
        rv = remote_version()
        lv = local_version()
        if rv and is_newer_version(rv, lv):
            if rv == self._last_update_notice_version:
                return
            if self._notify_update(lv, rv):
                self._last_update_notice_version = rv

    def _notify_update(self, current: str, latest: str) -> bool:
        update_action = "回复「确认更新」执行，期间会短暂重启。"
        if not update_configured(self._cfg.wechat_update_enabled, self._cfg.watchtower_token):
            update_action = WECHAT_UPDATE_DISABLED_HINT
        msg = (
            f"🔔 叮咚有新版本 v{latest}（当前 v{current}）\n"
            f"{update_action}\n"
            "关闭提醒：发「关闭更新提醒」"
        )
        admin = self._store.get_admin_account()
        if not admin:
            return False
        with self._runners_lock:
            runner = self._runners.get(admin.id)
        if not runner:
            return False
        jobs = self._store.list_jobs(account_id=admin.id)
        notified: set[str] = set()
        sent = False
        for job in jobs:
            if job.owner_user_id not in notified:
                if runner.send_to_user(job.owner_user_id, msg, job.context_token):
                    sent = True
                notified.add(job.owner_user_id)
        return sent

    def _notify_update_result_on_startup(self) -> None:
        result = read_update_result(self._cfg.data_dir)
        if not result or result.get("notified") == "1":
            return
        user_id = result.get("owner_user_id", "")
        context_token = result.get("context_token", "")
        target = result.get("target_version", "")
        if not user_id or not context_token or not target:
            return
        admin = self._store.get_admin_account()
        if not admin:
            return
        with self._runners_lock:
            runner = self._runners.get(admin.id)
        if not runner:
            return
        lv = local_version()
        if not is_newer_version(target, lv):
            if runner.send_to_user(user_id, f"更新完成：v{lv}。", context_token):
                write_update_result(self._cfg.data_dir, status="done",
                                    target_version=target, message="更新完成", notified="1")
            return
        if result.get("status") == "failed":
            message = result.get("message") or "请稍后再试"
            if runner.send_to_user(user_id, f"更新失败：{message}。", context_token):
                write_update_result(self._cfg.data_dir, status="failed",
                                    target_version=target, message=message, notified="1")

    # ── shutdown ──

    def _install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            log.info("received signal %s; shutting down", signum)
            self._stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except ValueError:
                pass

    def _start_restart_watcher(self) -> None:
        from .restart import RESTART_CHECK_INTERVAL_SECONDS, restart_marker_signature
        seen = restart_marker_signature(self._cfg.data_dir)

        def _watch() -> None:
            nonlocal seen
            while not self._stop.wait(RESTART_CHECK_INTERVAL_SECONDS):
                current = restart_marker_signature(self._cfg.data_dir)
                if current is None or current == seen:
                    continue
                log.info("configuration updated; restarting server")
                self._stop.set()
                break

        t = threading.Thread(target=_watch, daemon=True, name="restart-watch")
        t.start()

    def _shutdown(self) -> None:
        log.info("stopping all account runners...")
        with self._runners_lock:
            runners = list(self._runners.values())
        for r in runners:
            r.stop()
        self._scheduler.stop()
        self._store.close()
        log.info("server shutdown complete")
