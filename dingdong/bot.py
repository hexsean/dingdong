"""消息分发主循环。

启动顺序：
1. 加载配置 → 初始化 store / LLM。
2. 通过 ``ensure_login`` 拿到带 token 的 ILinkClient。
3. 启动 APScheduler 并加载历史 jobs。
4. 进入长轮询：getupdates → 路由给 IntentRouter → send_text 回复。
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config
from .executor import JobExecutor
from .ilink import ILinkClient, ILinkError
from .intent import IntentRouter
from .llm import build_provider
from .login import ensure_login, load_session, save_session
from .scheduler import Scheduler
from .search import init_exa
from .storage import JobStore
from .updater import CHECK_ID, local_version, remote_version, is_disabled

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._store = JobStore(cfg.db_path)
        self._llm = build_provider(cfg)
        init_exa(cfg.exa_api_key)
        self._stop = threading.Event()
        self._typing_tickets: dict[str, str] = {}

        def factory(token: str | None) -> ILinkClient:
            return ILinkClient(
                bot_token=token,
                long_poll_timeout_ms=cfg.long_poll_timeout_ms,
            )

        self._client = ensure_login(
            session_path=cfg.session_path,
            qrcode_png_path=cfg.qrcode_png_path,
            client_factory=factory,
        )
        self._executor = JobExecutor(self._llm, self._client, self._store, tz=cfg.scheduler_tz)
        self._scheduler = Scheduler(self._store, self._executor.run, cfg.scheduler_tz)
        self._intent = IntentRouter(self._llm, self._store, self._scheduler, history_limit=cfg.history_limit)
        self._intent.set_callbacks(
            on_status=self._send_status,
        )

    def run(self) -> None:
        self._install_signal_handlers()
        self._scheduler.start()
        self._register_update_check()
        log.info("v%s online; entering long-poll loop", local_version())

        session = load_session(self._cfg.session_path) or {}
        self._updates_buf = session.get("updates_buf", "")

        poll = threading.Thread(target=self._poll_loop, daemon=True, name="poll")
        poll.start()
        self._stop.wait()
        self._shutdown()

    # ---------- main loop ----------

    def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                msgs, new_buf = self._client.get_updates(self._updates_buf)
                if new_buf != self._updates_buf:
                    self._updates_buf = new_buf
                    self._persist_updates_buf(new_buf)
                for m in msgs:
                    if not m.is_user_message:
                        continue
                    self._handle_message(m)
                backoff = 1.0
            except ILinkError as exc:
                if self._stop.is_set():
                    break
                log.warning("long-poll error: %s; sleeping %.1fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("unexpected error in main loop")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle_message(self, msg: Any) -> None:
        text = (msg.text or "").strip()
        if not text:
            return
        owner = msg.from_user_id
        ctx = msg.context_token

        if self._cfg.allowed_user_ids and owner not in self._cfg.allowed_user_ids:
            log.info("rejecting message from %s (not in whitelist)", owner)
            self._client.safe_send_text(owner, "你不在该 bot 的允许列表中。", ctx)
            return

        log.info("inbound from %s: %s", owner, text[:120])
        typing_stop = self._start_typing_loop(owner, ctx)
        try:
            reply = self._intent.handle(
                owner_user_id=owner,
                context_token=ctx,
                text=text,
            )
        except Exception as exc:
            log.exception("intent handling failed")
            err_msg = str(exc)[:100]
            reply = f"出错了：{err_msg}" if log.isEnabledFor(logging.DEBUG) else "出错了，请稍后重试。"
        typing_stop.set()
        if reply:
            log.info("reply (%d chars): %s", len(reply), reply[:200])
            ok = self._client.safe_send_text(owner, reply, ctx)
            if ok:
                log.info("reply sent successfully")
            else:
                log.error("reply send FAILED after retries")
        else:
            log.warning("intent returned empty reply")
        self._cancel_typing(owner, ctx)

    # ---------- feedback ----------

    def _send_status(self, user_id: str, context_token: str, text: str) -> None:
        try:
            self._client.send_text_partial(user_id, text, context_token)
        except Exception:
            log.debug("send_text_partial failed, falling back to send_text")
            self._client.safe_send_text(user_id, text, context_token)

    def _show_typing(self, user_id: str, context_token: str) -> None:
        ticket = self._typing_tickets.get(user_id)
        if not ticket:
            ticket = self._client.get_typing_ticket(user_id, context_token)
            if ticket:
                self._typing_tickets[user_id] = ticket
        if ticket:
            ok = self._client.send_typing(user_id, ticket)
            if not ok:
                self._typing_tickets.pop(user_id, None)

    def _cancel_typing(self, user_id: str, context_token: str) -> None:
        ticket = self._typing_tickets.get(user_id)
        if ticket:
            self._client.send_typing(user_id, ticket, typing=False)

    def _start_typing_loop(self, user_id: str, context_token: str) -> threading.Event:
        """启动后台线程每 3 秒刷新 typing 状态，返回 stop event。"""
        stop = threading.Event()

        def _loop():
            while not stop.wait(3.0):
                self._show_typing(user_id, context_token)

        self._show_typing(user_id, context_token)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        return stop

    # ---------- update check ----------

    def _register_update_check(self) -> None:
        if is_disabled(self._cfg.data_dir):
            log.info("update check disabled by user")
            return
        from apscheduler.triggers.interval import IntervalTrigger
        self._scheduler._scheduler.add_job(
            self._check_update,
            trigger=IntervalTrigger(hours=24),
            id=CHECK_ID,
            replace_existing=True,
            next_run_time=None,  # 不立即执行，等 24h
        )
        # 启动 30 秒后做一次首检
        from apscheduler.triggers.date import DateTrigger
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self._cfg.scheduler_tz)
        self._scheduler._scheduler.add_job(
            self._check_update,
            trigger=DateTrigger(run_date=datetime.now(tz) + timedelta(seconds=30)),
            id=f"{CHECK_ID}_init",
        )

    def _check_update(self) -> None:
        if is_disabled(self._cfg.data_dir):
            return
        rv = remote_version()
        lv = local_version()
        if rv and rv != lv:
            log.info("new version available: %s (current: %s)", rv, lv)
            self._notify_update(lv, rv)

    def _notify_update(self, current: str, latest: str) -> None:
        msg = f"🔔 叮咚有新版本 v{latest}（当前 v{current}）\n更新：docker compose pull && docker compose up -d\n关闭提醒：发「关闭更新提醒」"
        session = load_session(self._cfg.session_path) or {}
        # 通知最近活跃的用户
        jobs = self._store.list_jobs()
        notified: set[str] = set()
        for job in jobs:
            if job.owner_user_id not in notified:
                self._client.safe_send_text(job.owner_user_id, msg, job.context_token)
                notified.add(job.owner_user_id)

    # ---------- shutdown ----------

    def _install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            log.info("received signal %s; shutting down", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except ValueError:
                pass

    def _shutdown(self) -> None:
        log.info("scheduler stopping...")
        self._scheduler.stop()
        self._store.close()
        log.info("bye")

    def _persist_updates_buf(self, updates_buf: str) -> None:
        session = load_session(self._cfg.session_path) or {}
        if session.get("updates_buf") == updates_buf:
            return
        session["updates_buf"] = updates_buf
        save_session(self._cfg.session_path, session)
