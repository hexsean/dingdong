"""SQLite 持久化层：jobs 表的 CRUD。

Job 表示一个定时目标：
- ``schedule_kind`` 取值 ``"cron"`` / ``"interval"`` / ``"date"``。
- ``schedule_value`` 是该 kind 对应的 JSON 字符串：
  - cron     -> {"expression": "0 9 * * *"}
  - interval -> {"seconds": 3600}
  - date     -> {"run_at": "2026-05-25 09:00:00"}
- ``owner_user_id`` 与 ``context_token`` 用于把执行结果回投到原会话。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    goal            TEXT NOT NULL,
    schedule_kind   TEXT NOT NULL,
    schedule_value  TEXT NOT NULL,
    owner_user_id   TEXT NOT NULL,
    context_token   TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    last_run_at     INTEGER,
    last_result     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_user_id);

CREATE TABLE IF NOT EXISTS chat_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_owner ON chat_history(owner_user_id, created_at);
"""

VALID_SCHEDULE_KINDS = {"cron", "interval", "date"}


@dataclass
class Job:
    id: str
    name: str
    goal: str
    schedule_kind: str
    schedule_value: dict[str, Any]
    owner_user_id: str
    context_token: str
    enabled: bool = True
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_run_at: int | None = None
    last_result: str | None = None

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.name,
            self.goal,
            self.schedule_kind,
            json.dumps(self.schedule_value, ensure_ascii=False),
            self.owner_user_id,
            self.context_token,
            1 if self.enabled else 0,
            self.created_at,
            self.last_run_at,
            self.last_result,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            name=row["name"],
            goal=row["goal"],
            schedule_kind=row["schedule_kind"],
            schedule_value=json.loads(row["schedule_value"]),
            owner_user_id=row["owner_user_id"],
            context_token=row["context_token"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_run_at=row["last_run_at"],
            last_result=row["last_result"],
        )

    def summary_line(self) -> str:
        sched = describe_schedule(self.schedule_kind, self.schedule_value)
        status = "✓" if self.enabled else "⏸"
        return f"{status} [{self.id[:8]}] {self.name} · {sched} · 目标: {self.goal}"


def describe_schedule(kind: str, value: dict[str, Any]) -> str:
    if kind == "cron":
        return f"cron {value.get('expression', '?')}"
    if kind == "interval":
        return f"每 {value.get('seconds', '?')} 秒"
    if kind == "date":
        return f"一次性 @ {value.get('run_at', '?')}"
    return f"{kind} {value}"


def new_job_id() -> str:
    return uuid.uuid4().hex


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- CRUD ----------

    def insert(self, job: Job) -> None:
        if job.schedule_kind not in VALID_SCHEDULE_KINDS:
            raise ValueError(f"invalid schedule_kind: {job.schedule_kind}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id,name,goal,schedule_kind,schedule_value,owner_user_id,"
                "context_token,enabled,created_at,last_run_at,last_result) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                job.to_row(),
            )

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def get_by_prefix(self, prefix: str) -> Job | None:
        """支持按 id 前缀匹配（必须唯一）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE id LIKE ? LIMIT 2", (f"{prefix}%",)
            ).fetchall()
        if len(rows) == 1:
            return Job.from_row(rows[0])
        return None

    def find_by_name(self, name: str, owner_user_id: str | None = None) -> Job | None:
        with self._lock:
            if owner_user_id:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE name = ? AND owner_user_id = ?",
                    (name, owner_user_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE name = ?", (name,)
                ).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, owner_user_id: str | None = None) -> list[Job]:
        with self._lock:
            if owner_user_id:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE owner_user_id = ? ORDER BY created_at",
                    (owner_user_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at"
                ).fetchall()
        return [Job.from_row(r) for r in rows]

    def update_fields(self, job_id: str, **fields: Any) -> Job | None:
        if not fields:
            return self.get(job_id)
        allowed = {
            "name", "goal", "schedule_kind", "schedule_value",
            "enabled", "context_token",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update fields: {bad}")
        if "schedule_value" in fields and isinstance(fields["schedule_value"], dict):
            fields["schedule_value"] = json.dumps(fields["schedule_value"], ensure_ascii=False)
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {sets} WHERE id = ?",
                (*fields.values(), job_id),
            )
        return self.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0

    def record_run(self, job_id: str, result: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET last_run_at = ?, last_result = ? WHERE id = ?",
                (int(time.time()), result[:4000], job_id),
            )

    def set_context_token(self, job_id: str, context_token: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET context_token = ? WHERE id = ?",
                (context_token, job_id),
            )

    # ---------- chat history ----------

    def append_message(self, owner_user_id: str, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_history (owner_user_id, role, content, created_at) VALUES (?,?,?,?)",
                (owner_user_id, role, content, int(time.time())),
            )

    def get_history(self, owner_user_id: str, limit: int = 20) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM chat_history "
                "WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                (owner_user_id, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_history(self, owner_user_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM chat_history WHERE owner_user_id = ?", (owner_user_id,)
            )
        return cur.rowcount
