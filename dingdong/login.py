"""二维码登录流程。

首次运行时调用 :func:`ensure_login`：若本地 session.json 已有 bot_token 直接返回；
否则向 ilinkai 获取二维码，把它以 ASCII + PNG 两种形式展示出来，
然后轮询扫码状态直到 confirmed，最终把 bot_token 持久化到磁盘。

关键字段区分（来自 get_bot_qrcode 响应）：
- ``qrcode``            → 不透明 session token，**仅用于轮询** get_qrcode_status
- ``qrcode_img_content`` → 一个 **URL 字符串**，这才是要编码进二维码给用户扫的内容
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import qrcode

from .ilink import ILinkClient, ILinkError

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
LOGIN_TIMEOUT_SECONDS = 5 * 60


def load_session(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("session file %s is not valid json; ignoring", path)
        return None


def save_session(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _print_qr_ascii(payload: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)


def _save_qr_png(payload: str, path: Path) -> None:
    img = qrcode.make(payload)
    img.save(path)


def _extract_qr_content(resp: dict[str, Any]) -> str:
    """提取要编码进二维码的 URL（即 qrcode_img_content）。"""
    for key in ("qrcode_img_content", "img_content", "qrcode_url", "url"):
        value = resp.get(key)
        if value and isinstance(value, str):
            return value
    raise ILinkError(
        f"get_bot_qrcode 响应中找不到 qrcode_img_content 字段: {list(resp.keys())}"
    )


def _extract_poll_token(resp: dict[str, Any]) -> str:
    """提取用于轮询状态的 session token（即 qrcode）。"""
    token = resp.get("qrcode")
    if token and isinstance(token, str):
        return token
    raise ILinkError(
        f"get_bot_qrcode 响应中找不到 qrcode (poll token) 字段: {list(resp.keys())}"
    )


def ensure_login(
    session_path: Path,
    qrcode_png_path: Path,
    client_factory,
) -> ILinkClient:
    """
    返回已带有 bot_token 的 ILinkClient。

    ``client_factory(bot_token: str | None) -> ILinkClient`` 由调用方提供，
    以便携带配置（base_url、超时）。
    """
    cached = load_session(session_path)
    if cached and cached.get("bot_token"):
        log.info("using cached bot_token from %s", session_path)
        return client_factory(cached["bot_token"])

    log.info("no cached session; starting QR login flow")
    client = client_factory(None)
    resp = client.get_qrcode()

    qr_url = _extract_qr_content(resp)
    poll_token = _extract_poll_token(resp)

    _save_qr_png(qr_url, qrcode_png_path)

    print()
    print("=" * 60)
    print(" 请用微信扫一扫（首页右上角 + → 扫一扫）扫描以下二维码")
    print(f" PNG 已保存到: {qrcode_png_path}")
    print()
    print(f" 若终端二维码无法扫描，也可用浏览器打开此链接：")
    print(f" {qr_url}")
    print("=" * 60)
    _print_qr_ascii(qr_url)
    print("=" * 60)
    print(" 等待扫码... (5 分钟超时)")
    sys.stdout.flush()

    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    last_status: str | None = None
    while time.time() < deadline:
        try:
            status = client.poll_qrcode_status(poll_token)
        except ILinkError as exc:
            log.warning("poll status error (will retry): %s", exc)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        s = (status.get("status") or status.get("state") or "").lower()
        if s != last_status:
            log.info("qrcode status: %s", s or "(unknown)")
            last_status = s

        if s in {"confirmed", "ok", "success"} and status.get("bot_token"):
            bot_token = status["bot_token"]
            save_session(
                session_path,
                {
                    "bot_token": bot_token,
                    "baseurl": status.get("baseurl"),
                    "bot_id": status.get("bot_id"),
                    "ilink_bot_id": status.get("ilink_bot_id"),
                    "login_at": int(time.time()),
                },
            )
            log.info("login confirmed; bot_token saved to %s", session_path)
            return client_factory(bot_token)

        if s in {"expired", "cancel", "cancelled", "canceled"}:
            raise ILinkError(f"qrcode {s}; please retry")

        time.sleep(POLL_INTERVAL_SECONDS)

    raise ILinkError("qrcode login timed out after 5 minutes")


def start_qr_login(
    session_path: Path,
    qrcode_png_path: Path,
    client_factory,
) -> tuple[ILinkClient, str, str] | None:
    """Non-blocking: return (client, qr_url, poll_token) or None if cached session exists."""
    cached = load_session(session_path)
    if cached and cached.get("bot_token"):
        return None

    client = client_factory(None)
    resp = client.get_qrcode()
    qr_url = _extract_qr_content(resp)
    poll_token = _extract_poll_token(resp)
    _save_qr_png(qr_url, qrcode_png_path)
    return client, qr_url, poll_token


def poll_login_status(
    client: ILinkClient,
    poll_token: str,
    session_path: Path,
    client_factory,
) -> dict[str, Any]:
    """Single poll attempt. Returns {status, bot_token?, client?}."""
    try:
        status = client.poll_qrcode_status(poll_token)
    except ILinkError as exc:
        return {"status": "error", "message": str(exc)}

    s = (status.get("status") or status.get("state") or "").lower()

    if s in {"confirmed", "ok", "success"} and status.get("bot_token"):
        bot_token = status["bot_token"]
        save_session(
            session_path,
            {
                "bot_token": bot_token,
                "baseurl": status.get("baseurl"),
                "bot_id": status.get("bot_id"),
                "ilink_bot_id": status.get("ilink_bot_id"),
                "login_at": int(time.time()),
            },
        )
        return {"status": "confirmed", "bot_token": bot_token,
                "client": client_factory(bot_token)}

    if s in {"expired", "cancel", "cancelled", "canceled"}:
        return {"status": "expired"}

    return {"status": s or "waiting"}
