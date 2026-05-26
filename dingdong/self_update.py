from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

UPDATE_RESULT = ".update-result"
DEFAULT_WATCHTOWER_URL = "http://watchtower:8080/v1/update"
WATCHTOWER_RETRY_DELAYS_SECONDS = (20, 40, 60, 90, 120, 180, 240, 300)

log = logging.getLogger(__name__)


def update_configured(enabled: bool, token: str) -> bool:
    return enabled and bool(token.strip())


def write_update_result(
    data_dir: Path,
    *,
    status: str,
    target_version: str,
    message: str = "",
    owner_user_id: str | None = None,
    context_token: str | None = None,
    notified: str | None = None,
) -> None:
    path = data_dir / UPDATE_RESULT
    result = read_update_result(data_dir) or {}
    result.update({
        "status": status,
        "target_version": target_version,
        "message": message,
        "updated_at": str(int(time.time())),
    })
    if owner_user_id is not None:
        result["owner_user_id"] = owner_user_id
    if context_token is not None:
        result["context_token"] = context_token
    if notified is not None:
        result["notified"] = notified

    keys = ["status", "target_version", "message", "owner_user_id", "context_token", "notified", "updated_at"]
    lines = [f"{key}={result[key]}" for key in keys if result.get(key)]
    body = "\n".join([*lines, ""])
    path.write_text(body, encoding="utf-8")


def read_update_result(data_dir: Path) -> dict[str, str] | None:
    path = data_dir / UPDATE_RESULT
    try:
        lines = path.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return None
    result: dict[str, str] = {}
    for line in lines:
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result or None


def _notify_progress(notify: Callable[[str], None] | None, text: str) -> None:
    if not notify:
        return
    try:
        notify(text)
    except Exception as exc:
        log.debug("send update progress failed: %s", exc)


def trigger_watchtower_update(
    data_dir: Path,
    *,
    url: str,
    token: str,
    target_version: str,
    owner_user_id: str = "",
    context_token: str = "",
    notify: Callable[[str], None] | None = None,
) -> None:
    from .updater import is_newer_version, local_version

    write_update_result(
        data_dir,
        status="running",
        target_version=target_version,
        message="正在更新",
        owner_user_id=owner_user_id,
        context_token=context_token,
        notified="0",
    )
    try:
        import requests

        delays = (0, *WATCHTOWER_RETRY_DELAYS_SECONDS)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                write_update_result(data_dir, status="running", target_version=target_version, message="等待更新生效")
                _notify_progress(notify, f"正在更新到 v{target_version}，等待生效...")
                time.sleep(delay)

            if attempt == 1:
                _notify_progress(notify, f"正在拉取 v{target_version}...")
            log.info("triggering watchtower update attempt=%s target=%s", attempt, target_version)
            resp = requests.get(
                url or DEFAULT_WATCHTOWER_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=(5, 600),
            )
            if 200 <= resp.status_code < 300:
                current_version = local_version()
                log.info(
                    "watchtower update returned status=%s attempt=%s target=%s current=%s",
                    resp.status_code,
                    attempt,
                    target_version,
                    current_version,
                )
                if not is_newer_version(target_version, current_version):
                    write_update_result(data_dir, status="done", target_version=target_version, message="更新完成", notified="1")
                    _notify_progress(notify, f"更新完成：v{target_version}。")
                    return
                if attempt < len(delays):
                    continue
                write_update_result(data_dir, status="pending", target_version=target_version, message="仍未完成")
                _notify_progress(notify, f"更新仍未完成：v{target_version}。可再发「确认更新」重试。")
                return
            if resp.status_code in (401, 403):
                write_update_result(data_dir, status="failed", target_version=target_version, message="更新令牌无效")
                _notify_progress(notify, "更新失败：更新令牌无效。")
                return
            if resp.status_code == 409 and attempt < len(delays):
                write_update_result(data_dir, status="running", target_version=target_version, message="更新仍在执行")
                _notify_progress(notify, f"正在更新到 v{target_version}，继续等待...")
                continue
            write_update_result(data_dir, status="failed", target_version=target_version, message=f"更新服务返回 {resp.status_code}")
            _notify_progress(notify, f"更新失败：更新服务返回 {resp.status_code}。")
            return
    except requests.Timeout:
        log.info("watchtower update request still running target=%s", target_version)
        write_update_result(data_dir, status="running", target_version=target_version, message="更新仍在执行")
        _notify_progress(notify, f"正在更新到 v{target_version}，仍在执行...")
    except Exception as exc:
        log.warning("watchtower update request failed target=%s: %s", target_version, exc)
        write_update_result(data_dir, status="failed", target_version=target_version, message="无法连接更新服务")
        _notify_progress(notify, "更新失败：无法连接更新服务。")
