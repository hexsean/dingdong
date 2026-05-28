"""单账号运行器。

每个 AccountRunner 管理一个微信账号的生命周期：
ILinkClient 连接、消息轮询、IntentRouter 路由、打字状态。
LLM / JobStore / Scheduler 由外部传入（多账号共享）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .ilink import ILinkClient, ILinkError
from .intent import IntentRouter, split_bubbles
from .llm import LLMProvider
from .login import ensure_login, load_session, save_session
from .models import ModelInfo
from .scheduler import Scheduler
from .storage import Job, JobStore

log = logging.getLogger(__name__)

EXECUTOR_SYSTEM_PROMPT = """\
你是叮咚。根据任务目标生成要发给用户的微信消息。
直接输出消息内容，不要前缀。简洁，中文，注意当前时间。
"""

# 仅当首条消息带图片时，短暂等待合并同一用户紧随其后的消息
# （微信常把"图片"和"配文"拆成两条消息发出）。纯文字消息不受影响、立即处理。
IMAGE_COALESCE_SECONDS = 2.0
# 多条消息之间的间隔，模拟真人逐条发送。
BUBBLE_GAP_SECONDS = 0.5


class AccountRunner:
    """Per-account runtime: owns an ILinkClient and a poll thread."""

    def __init__(
        self,
        account_id: str,
        store: JobStore,
        scheduler: Scheduler,
        llm: LLMProvider,
        cfg: Config,
        *,
        vision_llm: LLMProvider | None = None,
        model_info: ModelInfo | None = None,
        context_length: int = 0,
        is_admin: bool = False,
    ) -> None:
        self.account_id = account_id
        self.is_admin = is_admin
        self._store = store
        self._scheduler = scheduler
        self._llm = llm
        self._cfg = cfg
        self._stop = threading.Event()
        self._typing_tickets: dict[str, str] = {}
        self._poll_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._inbox: "queue.Queue[Any]" = queue.Queue()

        self._session_dir = cfg.data_dir / "sessions" / account_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._session_path = self._session_dir / "session.json"
        self._qrcode_path = self._session_dir / "qrcode.png"

        self._client: ILinkClient | None = None
        self._updates_buf = ""

        self._intent = IntentRouter(
            llm, store, scheduler,
            account_id=account_id,
            is_admin=is_admin,
            history_limit=cfg.history_limit,
            model_info=model_info,
            vision_llm=vision_llm,
            context_length=context_length,
            wechat_update_enabled=cfg.wechat_update_enabled and is_admin,
            watchtower_url=cfg.watchtower_url,
            watchtower_token=cfg.watchtower_token,
        )
        self._intent.set_callbacks(
            on_status=self._send_status,
            on_update_progress=self._send_update_progress,
        )

    # ── lifecycle ──

    def login_blocking(self) -> bool:
        """Blocking login (for CLI / startup with cached session)."""
        def factory(token: str | None) -> ILinkClient:
            return ILinkClient(bot_token=token, long_poll_timeout_ms=self._cfg.long_poll_timeout_ms)

        try:
            self._client = ensure_login(
                session_path=self._session_path,
                qrcode_png_path=self._qrcode_path,
                client_factory=factory,
            )
            session = load_session(self._session_path) or {}
            self._updates_buf = session.get("updates_buf", "")
            return True
        except Exception as exc:
            log.error("account %s login failed: %s", self.account_id, exc)
            return False

    def restore_session(self) -> bool:
        """Non-blocking: restore from cached session.json. Returns False if no cached session."""
        cached = load_session(self._session_path)
        if not cached or not cached.get("bot_token"):
            return False
        self._client = ILinkClient(
            bot_token=cached["bot_token"],
            long_poll_timeout_ms=self._cfg.long_poll_timeout_ms,
        )
        self._updates_buf = cached.get("updates_buf", "")
        return True

    def set_client(self, client: ILinkClient) -> None:
        """Set client after external login flow (Admin API)."""
        self._client = client

    def start(self) -> None:
        if self._client is None:
            raise RuntimeError(f"account {self.account_id}: no client; login first")
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop.clear()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name=f"dispatch-{self.account_id}"
        )
        self._dispatch_thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name=f"poll-{self.account_id}"
        )
        self._poll_thread.start()
        log.info("account %s poll started", self.account_id)

    def stop(self) -> None:
        self._stop.set()
        self._inbox.put(None)  # 唤醒可能阻塞在取消息的分发线程
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
        if self._dispatch_thread:
            self._dispatch_thread.join(timeout=10)
        log.info("account %s stopped", self.account_id)

    def is_running(self) -> bool:
        return self._poll_thread is not None and self._poll_thread.is_alive()

    # ── job execution (called by scheduler dispatch) ──

    def execute_job(self, job: Job) -> None:
        if self._client is None:
            log.warning("account %s: no client, skipping job %s", self.account_id, job.id)
            return
        log.info("executing job %s (%s) for account %s", job.id, job.name, self.account_id)
        try:
            content = self._generate_job_content(job)
        except Exception as exc:
            log.exception("LLM generation failed for job %s", job.id)
            content = f"[任务 {job.name} 生成失败] {exc}"
        ok = self._client.safe_send_text(
            to_user_id=job.owner_user_id,
            text=content,
            context_token=job.context_token,
        )
        status = "delivered" if ok else "send-failed"
        self._store.record_run(job.id, f"{status}: {content[:400]}")
        if job.schedule_kind == "date":
            if ok:
                self._store.delete(job.id)
            else:
                log.error("one-shot job %s send failed; kept for retry", job.id)

    def _generate_job_content(self, job: Job) -> str:
        tz = ZoneInfo(self._cfg.scheduler_tz)
        now = datetime.now(tz)
        weekday = "星期" + "一二三四五六日"[now.weekday()]
        user_prompt = (
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')} {weekday}\n"
            f"任务名称：{job.name}\n"
            f"任务目标描述：{job.goal}\n\n"
            "请根据该目标，生成本次应该发给用户的微信消息内容。"
        )
        resp = self._llm.chat(
            system=EXECUTOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1024,
        )
        return resp.text() or f"[任务 {job.name}] 本次未生成内容"

    # ── poll loop ──

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
                    self._inbox.put(m)  # 交给分发线程串行处理，poll 不阻塞，才能及时收到后续消息
                backoff = 1.0
            except ILinkError as exc:
                if self._stop.is_set():
                    break
                log.warning("account %s poll error: %s; sleeping %.1fs",
                            self.account_id, exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("account %s unexpected poll error", self.account_id)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    # ── dispatch (per-account serial; coalesce image+caption) ──

    def _dispatch_loop(self) -> None:
        """串行消费 inbox：纯文字立即处理；首条带图片则短暂等待合并同一用户的后续消息。"""
        while not self._stop.is_set():
            try:
                first = self._inbox.get(timeout=1.0)
            except queue.Empty:
                continue
            if first is None:  # stop 唤醒哨兵
                continue

            owner = first.from_user_id
            ctx = first.context_token
            texts = [first.text] if first.text else []
            images = list(getattr(first, "images", []) or [])

            # 仅图片场景才等待合并：微信会把图片与配文拆成两条紧邻的消息
            if images:
                deadline = time.monotonic() + IMAGE_COALESCE_SECONDS
                while not self._stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        nxt = self._inbox.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if nxt is None:
                        break
                    if nxt.from_user_id != owner:
                        self._inbox.put(nxt)  # 别的用户，放回去稍后处理，不并入本轮
                        break
                    if nxt.context_token:
                        ctx = nxt.context_token
                    if nxt.text:
                        texts.append(nxt.text)
                    images.extend(getattr(nxt, "images", []) or [])
                    if nxt.text:
                        break  # 等到配文，合并完成，立即回复

            try:
                self._process(owner, ctx, "\n".join(t for t in texts if t), images)
            except Exception:
                log.exception("account %s dispatch error", self.account_id)

    def _process(self, owner: str, ctx: str, text: str, images: list) -> None:
        text = (text or "").strip()
        if not text and not images:
            return

        if self._cfg.allowed_user_ids and owner not in self._cfg.allowed_user_ids:
            self._client.safe_send_text(owner, "你不在该 bot 的允许列表中。", ctx)
            return

        image_bytes_list: list[bytes] = []
        failed_images = 0
        for img in images:
            try:
                image_bytes_list.append(self._client.download_image(img))
            except Exception:
                failed_images += 1
                log.exception("image download failed")

        if images and not image_bytes_list:
            self._client.safe_send_text(owner, "图片好像没收到，再发一次试试？", ctx)
            return
        image_note = ""
        if failed_images:
            image_note = f"有 {failed_images} 张图片没读到，我先看已收到的。\n"

        log.info("account %s inbound from %s: %s (images: %d)",
                 self.account_id, owner, text[:120], len(image_bytes_list))
        typing_stop = self._start_typing_loop(owner, ctx)
        try:
            reply = self._intent.handle(
                owner_user_id=owner,
                context_token=ctx,
                text=text,
                image_bytes_list=image_bytes_list,
            )
        except Exception:
            log.exception("intent handling failed")
            reply = "处理失败，我没动你的任何任务。再发一次试试；要是一直失败，发「清空对话」重置一下。"
        typing_stop.set()
        if image_note and reply:
            reply = image_note + reply
        self._send_reply(owner, ctx, reply)
        self._cancel_typing(owner, ctx)

    def _send_reply(self, owner: str, ctx: str, reply: str) -> None:
        """把回复按 BUBBLE_SEP 拆成多条短消息逐条发出，条间带打字状态。"""
        parts = split_bubbles(reply)
        for i, part in enumerate(parts):
            if i > 0:
                self._show_typing(owner, ctx)
                self._stop.wait(BUBBLE_GAP_SECONDS)
            self._client.safe_send_text(owner, part, ctx)

    # ── typing ──

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
        stop = threading.Event()

        def _loop():
            while not stop.wait(3.0):
                self._show_typing(user_id, context_token)

        self._show_typing(user_id, context_token)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        return stop

    # ── feedback ──

    def _send_status(self, user_id: str, context_token: str, text: str) -> None:
        if self._client is None:
            return
        try:
            self._client.send_text_partial(user_id, text, context_token)
        except Exception:
            self._client.safe_send_text(user_id, text, context_token)

    def _send_update_progress(self, user_id: str, context_token: str, text: str) -> None:
        if self._client is None:
            return
        self._client.safe_send_text(user_id, text, context_token)

    def send_to_user(self, user_id: str, text: str, context_token: str) -> bool:
        if self._client is None:
            return False
        return self._client.safe_send_text(user_id, text, context_token)

    # ── persistence ──

    def _persist_updates_buf(self, updates_buf: str) -> None:
        session = load_session(self._session_path) or {}
        if session.get("updates_buf") == updates_buf:
            return
        session["updates_buf"] = updates_buf
        save_session(self._session_path, session)
