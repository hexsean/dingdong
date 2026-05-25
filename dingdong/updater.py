"""版本更新检测。

启动时注册系统级定时任务（不在用户任务列表中），每 24 小时检查一次 GitHub 上的 VERSION 文件。
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).parent.parent / "VERSION"
REMOTE_URL = "https://raw.githubusercontent.com/hexsean/dingdong/main/VERSION"
CHECK_ID = "__system_update_check__"
DISABLED_FLAG = "update_check_disabled"


def local_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "unknown"


def remote_version() -> str | None:
    try:
        resp = requests.get(REMOTE_URL, timeout=10)
        if resp.status_code == 200:
            return resp.text.strip()
    except Exception as exc:
        log.debug("fetch remote version failed: %s", exc)
    return None


def is_disabled(data_dir: Path) -> bool:
    return (data_dir / DISABLED_FLAG).exists()


def set_disabled(data_dir: Path, disabled: bool) -> None:
    flag = data_dir / DISABLED_FLAG
    if disabled:
        flag.write_text("1")
    else:
        flag.unlink(missing_ok=True)
