from __future__ import annotations

import time
from pathlib import Path

UPDATE_RESULT = ".update-result"
DEFAULT_WATCHTOWER_URL = "http://watchtower:8080/v1/update"
WATCHTOWER_RETRY_DELAYS_SECONDS = (20, 40, 60)


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
    from .updater import local_version

    write_update_result(data_dir, status="running", target_version=target_version, message="正在更新")
    try:
        import requests

        delays = (0, *WATCHTOWER_RETRY_DELAYS_SECONDS)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                write_update_result(data_dir, status="running", target_version=target_version, message="等待更新生效")
                time.sleep(delay)

            resp = requests.get(
                url or DEFAULT_WATCHTOWER_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=(5, 600),
            )
            if 200 <= resp.status_code < 300:
                if local_version() == target_version:
                    write_update_result(data_dir, status="done", target_version=target_version, message="更新完成")
                    return
                if attempt < len(delays):
                    continue
                write_update_result(data_dir, status="pending", target_version=target_version, message="暂未生效")
                return
            if resp.status_code in (401, 403):
                write_update_result(data_dir, status="failed", target_version=target_version, message="更新令牌无效")
                return
            if resp.status_code == 409 and attempt < len(delays):
                write_update_result(data_dir, status="running", target_version=target_version, message="更新仍在执行")
                continue
            write_update_result(data_dir, status="failed", target_version=target_version, message=f"更新服务返回 {resp.status_code}")
            return
    except requests.Timeout:
        write_update_result(data_dir, status="running", target_version=target_version, message="更新仍在执行")
    except Exception:
        write_update_result(data_dir, status="failed", target_version=target_version, message="无法连接更新服务")
