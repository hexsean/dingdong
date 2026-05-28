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
from .intent import IntentRouter, split_bubbles
from .llm import build_provider, build_vision_provider
from .login import ensure_login, load_session, save_session
from .models import fetch_model_info
from .restart import RESTART_CHECK_INTERVAL_SECONDS, restart_marker_signature
from .scheduler import Scheduler
from .search import init_exa
from .self_update import (
    WECHAT_UPDATE_DISABLED_HINT,
    read_update_result,
    update_configured,
    write_update_result,
)
from .storage import JobStore
from .updater import CHECK_ID, is_disabled, is_newer_version, local_version, remote_version

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._store = JobStore(cfg.db_path)
        self._llm = build_provider(cfg)
        init_exa(cfg.exa_api_key)
        self._stop = threading.Event()
        self._typing_tickets: dict[str, str] = {}
        self._last_update_notice_version: str | None = None

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

        self._vision_llm = build_vision_provider(cfg)
        vision_model_name = cfg.vision_model if self._vision_llm else (
            cfg.anthropic_model if cfg.llm_provider == "anthropic" else cfg.openai_model
        )
        vision_override = True if self._vision_llm else cfg.vision_enabled
        self._model_info = fetch_model_info(
            vision_model_name, data_dir=cfg.data_dir, vision_override=vision_override,
        )
        main_model = cfg.anthropic_model if cfg.llm_provider == "anthropic" else cfg.openai_model
        if self._vision_llm:
            main_info = fetch_model_info(main_model, data_dir=cfg.data_dir)
            main_ctx = main_info.context_length if main_info else None
        else:
            main_ctx = self._model_info.context_length if self._model_info else None
        effective_ctx = cfg.context_length or main_ctx or 0
        self._intent = IntentRouter(self._llm, self._store, self._scheduler,
                                    history_limit=cfg.history_limit, model_info=self._model_info,
                                    vision_llm=self._vision_llm,
                                    context_length=effective_ctx,
                                    wechat_update_enabled=cfg.wechat_update_enabled,
                                    watchtower_url=cfg.watchtower_url,
                                    watchtower_token=cfg.watchtower_token)
        self._intent.set_callbacks(
            on_status=self._send_status,
            on_update_progress=self._send_update_progress,
        )

    def run(self) -> None:
        self._install_signal_handlers()
        self._scheduler.start()
        self._register_update_check()
        self._start_restart_watcher()
        self._notify_update_result_on_startup()
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
        images = getattr(msg, "images", []) or []
        if not text and not images:
            return
        owner = msg.from_user_id
        ctx = msg.context_token

        if self._cfg.allowed_user_ids and owner not in self._cfg.allowed_user_ids:
            log.info("rejecting message from %s (not in whitelist)", owner)
            self._client.safe_send_text(owner, "你不在该 bot 的允许列表中。", ctx)
            return

        image_bytes_list: list[bytes] = []
        failed_images = 0
        for img in images:
            try:
                data = self._client.download_image(img)
                image_bytes_list.append(data)
                log.info("downloaded image (%d bytes) from %s", len(data), owner)
            except Exception:
                failed_images += 1
                log.exception("image download failed")

        if images and not image_bytes_list:
            self._client.safe_send_text(owner, "图片下载失败了，请重新发送一次。", ctx)
            return
        image_note = ""
        if failed_images:
            image_note = f"有 {failed_images} 张图片没读到，我先处理已收到的图片。\n"

        log.info("inbound from %s: %s (images: %d)", owner, text[:120], len(image_bytes_list))
        typing_stop = self._start_typing_loop(owner, ctx)
        try:
            reply = self._intent.handle(
                owner_user_id=owner,
                context_token=ctx,
                text=text,
                image_bytes_list=image_bytes_list,
            )
        except Exception as exc:
            log.exception("intent handling failed")
            err_msg = str(exc)[:100]
            reply = (
                f"处理失败，我没有执行任何任务变更：{err_msg}"
                if log.isEnabledFor(logging.DEBUG)
                else "处理失败，我没有执行任何任务变更。请重试一次；若连续失败，请发「清空对话」重置上下文，或让管理员查看日志。"
            )
        typing_stop.set()
        if image_note and reply:
            reply = image_note + reply
        parts = split_bubbles(reply)
        if parts:
            for i, part in enumerate(parts):
                if i > 0:
                    self._show_typing(owner, ctx)
                    self._stop.wait(0.5)  # 条间间隔，模拟真人逐条发送
                ok = self._client.safe_send_text(owner, part, ctx)
                log.info("reply bubble %d/%d (%d chars): %s",
                         i + 1, len(parts), len(part), "ok" if ok else "FAILED")
        else:
            log.warning("intent returned empty reply")
        self._cancel_typing(owner, ctx)

    # ---------- config restart ----------

    def _start_restart_watcher(self) -> None:
        seen = restart_marker_signature(self._cfg.data_dir)

        def _watch() -> None:
            nonlocal seen
            while not self._stop.wait(RESTART_CHECK_INTERVAL_SECONDS):
                current = restart_marker_signature(self._cfg.data_dir)
                if current is None or current == seen:
                    continue
                log.info("configuration updated by setup; restarting service")
                self._stop.set()
                break

        t = threading.Thread(target=_watch, daemon=True, name="restart-watch")
        t.start()

    # ---------- feedback ----------

    def _send_status(self, user_id: str, context_token: str, text: str) -> None:
        try:
            self._client.send_text_partial(user_id, text, context_token)
        except Exception:
            log.debug("send_text_partial failed, falling back to send_text")
            self._client.safe_send_text(user_id, text, context_token)

    def _send_update_progress(self, user_id: str, context_token: str, text: str) -> None:
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
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self._cfg.scheduler_tz)
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
            log.info("new version available: %s (current: %s)", rv, lv)
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
        # 通知最近活跃的用户
        jobs = self._store.list_jobs()
        notified: set[str] = set()
        sent = False
        for job in jobs:
            if job.owner_user_id not in notified:
                if self._client.safe_send_text(job.owner_user_id, msg, job.context_token):
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

        lv = local_version()
        if not is_newer_version(target, lv):
            if self._client.safe_send_text(user_id, f"更新完成：v{lv}。", context_token):
                write_update_result(self._cfg.data_dir, status="done", target_version=target, message="更新完成", notified="1")
            return

        if result.get("status") == "failed":
            message = result.get("message") or "请稍后再试"
            if self._client.safe_send_text(user_id, f"更新失败：{message}。", context_token):
                write_update_result(self._cfg.data_dir, status="failed", target_version=target, message=message, notified="1")

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
