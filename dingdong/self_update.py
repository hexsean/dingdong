from __future__ import annotations

import logging
import time
from pathlib import Path

UPDATE_RESULT = ".update-result"
DEFAULT_WATCHTOWER_URL = "http://watchtower:8080/v1/update"

log = logging.getLogger(__name__)


def manual_update_command() -> str:
    return "docker compose pull && docker compose up -d --force-recreate"


def pinned_version_note() -> str:
    return "微信更新适用于默认 latest 部署；固定 DINGDONG_VERSION 时请手动改版本号。"


def update_configured(enabled: bool, token: str) -> bool:
    return enabled and bool(token.strip())


def write_update_result(data_dir: Path, *, status: str, target_version: str, message: str = "") -> None:
    path = data_dir / UPDATE_RESULT
    body = "\n".join([
        f"status={status}",
        f"target_version={target_version}",
        f"message={message}",
        f"updated_at={int(time.time())}",
        "",
    ])
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


def trigger_watchtower_update(data_dir: Path, *, url: str, token: str, target_version: str) -> None:
    write_update_result(data_dir, status="running", target_version=target_version, message="triggered")
    try:
        import requests

        resp = requests.get(
            url or DEFAULT_WATCHTOWER_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=(5, 600),
        )
        if 200 <= resp.status_code < 300:
            write_update_result(data_dir, status="done", target_version=target_version, message="update requested")
        elif resp.status_code in (401, 403):
            write_update_result(data_dir, status="failed", target_version=target_version, message="updater token invalid")
        else:
            write_update_result(data_dir, status="failed", target_version=target_version, message=f"updater returned {resp.status_code}")
    except requests.Timeout:
        write_update_result(data_dir, status="running", target_version=target_version, message="updater is still working")
    except Exception as exc:
        log.debug("watchtower update request failed: %s", exc)
        write_update_result(data_dir, status="failed", target_version=target_version, message="cannot reach updater service")
