"""管理 API + Web UI。

基于 stdlib http.server（ThreadingHTTPServer）。提供：
- /           → 管理页面
- /api/login  → 密码登录
- /api/*      → 账号管理 REST API
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .server import Server

log = logging.getLogger(__name__)

SESSION_TTL = 86400
MAX_SESSIONS = 100
MAX_BODY_SIZE = 65536
ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{1,32}$")


class AdminAPI:
    def __init__(self, server: "Server", port: int = 8081, password: str = "") -> None:
        self._server = server
        self._port = port
        self._password = password
        self._sessions: dict[str, float] = {}
        self._sessions_lock = threading.Lock()
        handler = partial(_Handler, app=server, api=self)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="admin-api"
        )
        self._thread.start()
        auth_hint = "password protected" if self._password else "NO PASSWORD (set ADMIN_PASSWORD)"
        log.info("admin UI at http://0.0.0.0:%d (%s)", self._port, auth_hint)

    def stop(self) -> None:
        self._httpd.shutdown()

    def check_login(self, password: str) -> str | None:
        if not self._password:
            return None
        if not hmac.compare_digest(password.encode(), self._password.encode()):
            return None
        token = secrets.token_urlsafe(32)
        with self._sessions_lock:
            if len(self._sessions) >= MAX_SESSIONS:
                self._purge_expired()
            self._sessions[token] = time.time() + SESSION_TTL
        return token

    def verify_token(self, token: str) -> bool:
        if not self._password:
            return True
        with self._sessions_lock:
            expires = self._sessions.get(token, 0)
            if time.time() > expires:
                self._sessions.pop(token, None)
                return False
        return True

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._sessions.items() if v < now]
        for k in expired:
            del self._sessions[k]

    @property
    def requires_auth(self) -> bool:
        return bool(self._password)


_STATIC_DIR = Path(__file__).parent / "admin_static"


class _Handler(BaseHTTPRequestHandler):
    server_version = "dingdong-admin/1.0"

    def __init__(self, *args, app: "Server", api: AdminAPI, **kwargs):
        self._app = app
        self._api = api
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        log.debug("admin: " + fmt, *args)

    def _check_auth(self) -> bool:
        if not self._api.requires_auth:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and self._api.verify_token(auth[7:]):
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _json(self, code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > MAX_BODY_SIZE:
            self._json(413, {"error": "request too large"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "invalid JSON"})
            return None

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _parts(self) -> tuple[str, ...]:
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            return ("/",)
        return tuple(path.strip("/").split("/"))

    def _valid_account_id(self, aid: str) -> bool:
        return bool(ACCOUNT_ID_RE.match(aid))

    # ── routing ──

    def do_GET(self) -> None:
        parts = self._parts()

        if parts == ("/",):
            self._serve_file(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        if parts == ("api", "auth-status"):
            self._json(200, {"requires_auth": self._api.requires_auth})
            return

        if not self._check_auth():
            return

        if parts == ("api", "accounts"):
            self._json(200, {"accounts": self._app.list_accounts()})
            return

        if len(parts) == 3 and parts[:2] == ("api", "accounts"):
            if not self._valid_account_id(parts[2]):
                self._json(400, {"error": "invalid account id"})
                return
            account = self._app.get_account(parts[2])
            if not account:
                self._json(404, {"error": "not found"})
                return
            self._json(200, {
                "id": account.id, "label": account.label,
                "is_admin": account.is_admin, "status": account.status,
            })
            return

        if len(parts) == 4 and parts[:2] == ("api", "accounts") and parts[3] == "login":
            if not self._valid_account_id(parts[2]):
                self._json(400, {"error": "invalid account id"})
                return
            result = self._app.poll_login(parts[2])
            self._json(200, result)
            return

        if len(parts) == 4 and parts[:2] == ("api", "accounts") and parts[3] == "qrcode.png":
            if not self._valid_account_id(parts[2]):
                self.send_error(400)
                return
            qr_path = self._app._cfg.data_dir / "sessions" / parts[2] / "qrcode.png"
            self._serve_file(qr_path, "image/png")
            return

        if parts == ("api", "status"):
            from .updater import local_version
            accounts = self._app.list_accounts()
            online = sum(1 for a in accounts if a.get("online"))
            self._json(200, {
                "version": local_version(), "accounts": len(accounts), "online": online,
                "tunnel_url": self._app.tunnel_url,
            })
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = self._parts()

        if parts == ("api", "login"):
            body = self._read_json()
            if body is None:
                return
            token = self._api.check_login(body.get("password", ""))
            if token:
                self._json(200, {"token": token})
            else:
                self._json(401, {"error": "密码错误"})
            return

        if not self._check_auth():
            return

        if parts == ("api", "accounts"):
            body = self._read_json()
            if body is None:
                return
            account = self._app.create_account(label=body.get("label", ""))
            self._json(201, {
                "id": account.id, "label": account.label,
                "is_admin": account.is_admin, "status": account.status,
            })
            return

        if len(parts) == 4 and parts[:2] == ("api", "accounts") and parts[3] == "login":
            if not self._valid_account_id(parts[2]):
                self._json(400, {"error": "invalid account id"})
                return
            result = self._app.start_login(parts[2])
            code = 200 if "error" not in result else 400
            self._json(code, result)
            return

        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if not self._check_auth():
            return
        parts = self._parts()

        if len(parts) == 3 and parts[:2] == ("api", "accounts"):
            if not self._valid_account_id(parts[2]):
                self._json(400, {"error": "invalid account id"})
                return
            ok = self._app.remove_account(parts[2])
            self._json(200 if ok else 404, {"deleted": ok})
            return

        self._json(404, {"error": "not found"})
