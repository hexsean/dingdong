from __future__ import annotations

import time
from pathlib import Path

RESTART_MARKER = ".restart-request"
RESTART_CHECK_INTERVAL_SECONDS = 5.0


def request_service_restart(data_dir: Path) -> Path:
    marker = data_dir / RESTART_MARKER
    marker.write_text(f"{time.time()}\n", encoding="utf-8")
    return marker


def restart_marker_signature(data_dir: Path) -> tuple[int, int] | None:
    marker = data_dir / RESTART_MARKER
    try:
        stat = marker.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size
