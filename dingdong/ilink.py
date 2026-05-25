"""微信 iLink Bot 协议客户端。

封装腾讯官方 OpenClaw 微信通道使用的 https://ilinkai.weixin.qq.com 接口。
仅支持文本消息收发；登录通过扫码完成。
"""

from __future__ import annotations

import base64
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"


class ILinkError(RuntimeError):
    """非 0 返回码或网络错误。"""


@dataclass
class InboundMessage:
    """从 getupdates 接收到的一条入站消息。"""

    from_user_id: str
    to_user_id: str
    text: str
    context_token: str
    message_type: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_user_message(self) -> bool:
        return self.message_type == 1


def _random_uin_header() -> str:
    return base64.b64encode(str(random.randint(0, 2**32 - 1)).encode()).decode()


def _extract_text(item_list: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in item_list or []:
        item_type = item.get("type")
        if item_type == 1:
            text = (item.get("text_item") or {}).get("text")
            if text:
                parts.append(text)
    return "\n".join(parts)


class ILinkClient:
    def __init__(
        self,
        bot_token: str | None = None,
        base_url: str = BASE_URL,
        long_poll_timeout_ms: int = 35000,
        request_timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bot_token = bot_token
        self.long_poll_timeout_ms = long_poll_timeout_ms
        self.request_timeout = request_timeout
        self._session = requests.Session()
        self._send_lock = threading.Lock()

    # ---------- low level ----------

    def _headers(self, *, require_token: bool = True) -> dict[str, str]:
        if require_token and not self.bot_token:
            raise ILinkError("missing bot_token; login first")
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_uin_header(),
            "User-Agent": "dingdong/0.1 (+https://github.com/)",
        }
        if self.bot_token:
            h["Authorization"] = f"Bearer {self.bot_token}"
        return h

    def _post(self, path: str, body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.post(
                url,
                json=body,
                headers=self._headers(),
                timeout=timeout or self.request_timeout,
            )
        except requests.RequestException as exc:
            raise ILinkError(f"POST {path} network error: {exc}") from exc
        return self._handle_response(path, resp)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, require_token: bool = False) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(
                url,
                params=params,
                headers=self._headers(require_token=require_token),
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise ILinkError(f"GET {path} network error: {exc}") from exc
        return self._handle_response(path, resp)

    @staticmethod
    def _handle_response(path: str, resp: requests.Response) -> dict[str, Any]:
        if resp.status_code >= 500:
            raise ILinkError(f"{path} server error {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ILinkError(f"{path} non-json response (status {resp.status_code}): {resp.text[:200]}") from exc
        if resp.status_code >= 400:
            raise ILinkError(f"{path} http {resp.status_code}: {data}")
        ret = data.get("ret")
        if ret not in (None, 0):
            err = data.get("err_msg") or data.get("errmsg") or data
            raise ILinkError(f"{path} ret={ret}: {err}")
        return data

    # ---------- login ----------

    def get_qrcode(self) -> dict[str, Any]:
        """返回 {qrcode, qrcode_img_content?, expire_seconds?}。无需 token。"""
        return self._get("/ilink/bot/get_bot_qrcode", params={"bot_type": 3})

    def poll_qrcode_status(self, qrcode: str) -> dict[str, Any]:
        """轮询扫码状态。状态值常见：waiting / scanned / confirmed / expired。"""
        return self._get("/ilink/bot/get_qrcode_status", params={"qrcode": qrcode})

    def set_token(self, bot_token: str) -> None:
        self.bot_token = bot_token

    # ---------- messaging ----------

    def get_updates(self, updates_buf: str = "") -> tuple[list[InboundMessage], str]:
        """长轮询接收消息。返回 (messages, new_updates_buf)。"""
        body = {
            "get_updates_buf": updates_buf or "",
            "base_info": {"channel_version": CHANNEL_VERSION},
            "longpolling_timeout_ms": self.long_poll_timeout_ms,
        }
        data = self._post(
            "/ilink/bot/getupdates",
            body,
            timeout=(self.long_poll_timeout_ms / 1000.0) + 10.0,
        )
        new_buf = data.get("get_updates_buf", updates_buf)
        messages: list[InboundMessage] = []
        for raw in data.get("msgs", []) or []:
            messages.append(
                InboundMessage(
                    from_user_id=raw.get("from_user_id", ""),
                    to_user_id=raw.get("to_user_id", ""),
                    text=_extract_text(raw.get("item_list", [])),
                    context_token=raw.get("context_token", ""),
                    message_type=raw.get("message_type", 1),
                    raw=raw,
                )
            )
        return messages, new_buf

    def send_text(self, to_user_id: str, text: str, context_token: str) -> dict[str, Any]:
        """发送一条文本消息。回复时 context_token 必须与入站消息一致。"""
        if not text:
            raise ValueError("text must be non-empty")
        client_id = f"dd-{uuid.uuid4().hex[:16]}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        with self._send_lock:
            log.debug("sendmessage to=%s ctx=%s cid=%s text=%s", to_user_id, context_token[:20], client_id, text[:80])
            result = self._post("/ilink/bot/sendmessage", body)
            log.debug("sendmessage response: %s", result)
            return result

    # ---------- typing indicator ----------

    def get_typing_ticket(self, user_id: str, context_token: str) -> str | None:
        """获取 typing_ticket（可缓存 24h）。失败返回 None。"""
        body = {
            "ilink_user_id": user_id,
            "context_token": context_token,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        try:
            data = self._post("/ilink/bot/getconfig", body)
            ticket = data.get("typing_ticket")
            if ticket:
                log.debug("got typing_ticket for %s", user_id)
            return ticket
        except ILinkError as exc:
            log.warning("getconfig failed: %s", exc)
            return None

    def send_typing(self, user_id: str, typing_ticket: str, *, typing: bool = True) -> bool:
        """发送"正在输入"状态。typing=False 可主动取消。"""
        body = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": 1 if typing else 2,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        try:
            self._post("/ilink/bot/sendtyping", body)
            return True
        except ILinkError as exc:
            log.warning("sendtyping failed: %s", exc)
            return False

    # ---------- helpers ----------

    def safe_send_text(self, to_user_id: str, text: str, context_token: str, *, retries: int = 2) -> bool:
        """带重试的发送；失败仅记日志，不抛异常。返回是否成功。"""
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self.send_text(to_user_id, text, context_token)
                return True
            except (ILinkError, ValueError) as exc:
                last_err = exc
                log.warning("send_text failed (attempt %d/%d): %s", attempt + 1, retries + 1, exc)
                time.sleep(1.5 * (attempt + 1))
        log.error("send_text gave up: %s", last_err)
        return False
