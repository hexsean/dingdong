"""版本更新检测。

Bot 启动后注册系统级定时任务（不在用户任务列表中），每 5 分钟优先检查
Docker Hub latest 镜像标签中的版本；仅在旧版本兼容场景下回退到 GitHub VERSION 文件。
"""

from __future__ import annotations

import logging
import platform
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).parent.parent / "VERSION"
REMOTE_URL = "https://raw.githubusercontent.com/hexsean/dingdong/main/VERSION"
IMAGE_REPOSITORY = "hexsean/dingdong"
IMAGE_TAG = "latest"
DOCKER_AUTH_URL = "https://auth.docker.io/token"
DOCKER_REGISTRY_URL = "https://index.docker.io/v2"
IMAGE_VERSION_LABEL = "org.opencontainers.image.version"
CHECK_ID = "__system_update_check__"
DISABLED_FLAG = "update_check_disabled"


def local_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "unknown"


def remote_version() -> str | None:
    version = remote_image_version()
    if version:
        return version
    return remote_file_version()


def remote_file_version() -> str | None:
    try:
        resp = requests.get(REMOTE_URL, timeout=10)
        if resp.status_code == 200:
            return resp.text.strip()
    except Exception as exc:
        log.debug("fetch remote version failed: %s", exc)
    return None



def remote_image_version() -> str | None:
    try:
        token = _docker_token()
        if not token:
            return None
        latest_digest = _watchtower_digest(token)
        if not latest_digest:
            return None
        index = _registry_json(token, f"/{IMAGE_REPOSITORY}/manifests/{latest_digest}")
        manifest = _platform_manifest(token, index)
        if not manifest:
            return None
        config_digest = manifest.get("config", {}).get("digest")
        if not config_digest:
            return None
        config = _registry_json(token, f"/{IMAGE_REPOSITORY}/blobs/{config_digest}")
        labels = config.get("config", {}).get("Labels") or {}
        version = labels.get(IMAGE_VERSION_LABEL)
        return version.strip() if isinstance(version, str) and version.strip() else None
    except Exception as exc:
        log.debug("fetch remote image version failed: %s", exc)
        return None


def _docker_token() -> str | None:
    resp = requests.get(
        DOCKER_AUTH_URL,
        params={"service": "registry.docker.io", "scope": f"repository:{IMAGE_REPOSITORY}:pull"},
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    token = resp.json().get("token")
    return token if isinstance(token, str) and token else None


def _watchtower_digest(token: str) -> str | None:
    resp = requests.head(
        f"{DOCKER_REGISTRY_URL}/{IMAGE_REPOSITORY}/manifests/{IMAGE_TAG}",
        headers=_registry_headers(token),
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    digest = resp.headers.get("Docker-Content-Digest", "").strip()
    return digest if digest.startswith("sha256:") else None


def _registry_json(token: str, path: str) -> dict:
    resp = requests.get(
        f"{DOCKER_REGISTRY_URL}{path}",
        headers=_registry_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _registry_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join((
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.config.v1+json",
            "application/vnd.docker.container.image.v1+json",
        )),
    }


def _platform_manifest(token: str, index_or_manifest: dict) -> dict | None:
    digest = _platform_manifest_digest(index_or_manifest)
    if digest is None:
        return index_or_manifest if index_or_manifest.get("config", {}).get("digest") else None
    return _registry_json(token, f"/{IMAGE_REPOSITORY}/manifests/{digest}")


def _platform_manifest_digest(index: dict) -> str | None:
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        return None

    arch = _docker_arch()
    for manifest in manifests:
        if _matches_platform(manifest, arch):
            return manifest.get("digest")
    for manifest in manifests:
        platform_info = manifest.get("platform", {})
        if platform_info.get("os") == "linux" and platform_info.get("architecture") != "unknown":
            return manifest.get("digest")
    return None


def _matches_platform(manifest: dict, arch: str) -> bool:
    platform_info = manifest.get("platform", {})
    return platform_info.get("os") == "linux" and platform_info.get("architecture") == arch


def _docker_arch() -> str:
    machine = platform.machine().lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine)


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", version.strip())
    if not m:
        return None
    return tuple(int(part) for part in m.groups())


def is_newer_version(remote: str, local: str) -> bool:
    remote_tuple = _version_tuple(remote)
    local_tuple = _version_tuple(local)
    if remote_tuple is not None and local_tuple is not None:
        return remote_tuple > local_tuple
    return remote.strip() != local.strip()


def is_disabled(data_dir: Path) -> bool:
    return (data_dir / DISABLED_FLAG).exists()


def set_disabled(data_dir: Path, disabled: bool) -> None:
    flag = data_dir / DISABLED_FLAG
    if disabled:
        flag.write_text("1")
    else:
        flag.unlink(missing_ok=True)
