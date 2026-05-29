"""SQLite 持久化层：accounts / jobs / chat_history。

Account 表示一个绑定的微信账号（多租户模式下每人一个）。
Job 表示一个定时目标，隶属于某个 account + owner_user_id。
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

# 长期偏好默认值（用户未设定时用这些；设定后用用户的——覆盖关系）。
# "有趣/毒舌打趣"是叮咚的默认风格，用户在 set_profile 里给了 persona 就完全按用户的来。
DEFAULT_BOT_NAME = "叮咚"
DEFAULT_PERSONA = (
    "像个损友：机灵、爱吐槽爱打趣，毒舌但不刻薄；说话简短、口语、接地气，别像机器人念说明书。"
    "记账时尤其爱调侃，遇到大额开销会假装心疼地拷问两句。"
)

# 系统到点自动发出的消息（定时任务触发、账单推送等）写进对话历史时的统一前缀，
# 让 LLM 能把"系统自动发生过的事"与"用户指令 / 自己的回复"区分开，降低重复执行与幻觉。
SYSTEM_EVENT_PREFIX = "（系统自动消息）"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    is_admin   INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account_id);

CREATE TABLE IF NOT EXISTS chat_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_owner ON chat_history(owner_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_account ON chat_history(account_id, owner_user_id, created_at);

CREATE TABLE IF NOT EXISTS user_prefs (
    account_id    TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL,
    bot_name      TEXT NOT NULL DEFAULT '',
    user_title    TEXT NOT NULL DEFAULT '',
    persona       TEXT NOT NULL DEFAULT '',
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (account_id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS expenses (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    item          TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    context_token TEXT NOT NULL DEFAULT '',
    spent_at      INTEGER NOT NULL,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_owner ON expenses(owner_user_id, spent_at);
CREATE INDEX IF NOT EXISTS idx_expenses_account ON expenses(account_id, owner_user_id, spent_at);
"""

VALID_SCHEDULE_KINDS = {"cron", "interval", "date"}


# ── Account ─────────────────────────────────────────────────


@dataclass
class Account:
    id: str
    label: str = ""
    is_admin: bool = False
    status: str = "active"
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_row(self) -> tuple[Any, ...]:
        return (self.id, self.label, 1 if self.is_admin else 0, self.status, self.created_at)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Account:
        return cls(
            id=row["id"], label=row["label"],
            is_admin=bool(row["is_admin"]), status=row["status"],
            created_at=row["created_at"],
        )


def new_account_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Job ─────────────────────────────────────────────────────


@dataclass
class Job:
    id: str
    name: str
    goal: str
    schedule_kind: str
    schedule_value: dict[str, Any]
    owner_user_id: str
    context_token: str
    account_id: str = ""
    enabled: bool = True
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_run_at: int | None = None
    last_result: str | None = None

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.account_id,
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
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            account_id=row["account_id"] if "account_id" in row.keys() else "",
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


# ── Expense ─────────────────────────────────────────────────


@dataclass
class Expense:
    id: str
    owner_user_id: str
    amount_cents: int
    item: str
    category: str = ""
    note: str = ""
    context_token: str = ""
    account_id: str = ""
    spent_at: int = field(default_factory=lambda: int(time.time()))
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.id, self.account_id, self.owner_user_id, self.amount_cents,
            self.item, self.category, self.note, self.context_token,
            self.spent_at, self.created_at,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Expense:
        return cls(
            id=row["id"], account_id=row["account_id"], owner_user_id=row["owner_user_id"],
            amount_cents=row["amount_cents"], item=row["item"], category=row["category"],
            note=row["note"], context_token=row["context_token"],
            spent_at=row["spent_at"], created_at=row["created_at"],
        )


def new_expense_id() -> str:
    return uuid.uuid4().hex


# ── Store ───────────────────────────────────────────────────


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        cols_ch = {r[1] for r in self._conn.execute("PRAGMA table_info(chat_history)").fetchall()}
        needs_jobs = cols and "account_id" not in cols
        needs_chat = cols_ch and "account_id" not in cols_ch
        if not needs_jobs and not needs_chat:
            return
        log.info("migrating: adding account_id columns")
        self._conn.execute("BEGIN")
        try:
            if needs_jobs:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account_id)")
            if needs_chat:
                self._conn.execute("ALTER TABLE chat_history ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_account ON chat_history(account_id, owner_user_id, created_at)"
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── accounts ──

    def insert_account(self, account: Account) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO accounts (id, label, is_admin, status, created_at) VALUES (?,?,?,?,?)",
                account.to_row(),
            )

    def get_account(self, account_id: str) -> Account | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return Account.from_row(row) if row else None

    def list_accounts(self, status: str | None = None) -> list[Account]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM accounts WHERE status = ? ORDER BY created_at", (status,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        return [Account.from_row(r) for r in rows]

    def update_account(self, account_id: str, **fields: Any) -> Account | None:
        if not fields:
            return self.get_account(account_id)
        allowed = {"label", "is_admin", "status"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update account fields: {bad}")
        if "is_admin" in fields:
            fields["is_admin"] = 1 if fields["is_admin"] else 0
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE accounts SET {sets} WHERE id = ?", (*fields.values(), account_id))
        return self.get_account(account_id)

    def delete_account(self, account_id: str) -> bool:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
                self._conn.execute("DELETE FROM jobs WHERE account_id = ?", (account_id,))
                self._conn.execute("DELETE FROM chat_history WHERE account_id = ?", (account_id,))
                self._conn.execute("DELETE FROM user_prefs WHERE account_id = ?", (account_id,))
                self._conn.execute("DELETE FROM expenses WHERE account_id = ?", (account_id,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return cur.rowcount > 0

    def get_admin_account(self) -> Account | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE is_admin = 1 LIMIT 1"
            ).fetchone()
        return Account.from_row(row) if row else None

    # ── jobs ──

    def insert(self, job: Job) -> None:
        if job.schedule_kind not in VALID_SCHEDULE_KINDS:
            raise ValueError(f"invalid schedule_kind: {job.schedule_kind}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id,account_id,name,goal,schedule_kind,schedule_value,"
                "owner_user_id,context_token,enabled,created_at,last_run_at,last_result) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                job.to_row(),
            )

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def get_by_prefix(self, prefix: str, account_id: str | None = None) -> Job | None:
        with self._lock:
            if account_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE id LIKE ? AND account_id = ? LIMIT 2",
                    (f"{prefix}%", account_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE id LIKE ? LIMIT 2", (f"{prefix}%",)
                ).fetchall()
        if len(rows) == 1:
            return Job.from_row(rows[0])
        return None

    def find_by_name(self, name: str, owner_user_id: str | None = None,
                     account_id: str | None = None) -> Job | None:
        with self._lock:
            if owner_user_id and account_id is not None:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE name = ? AND owner_user_id = ? AND account_id = ?",
                    (name, owner_user_id, account_id),
                ).fetchone()
            elif owner_user_id:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE name = ? AND owner_user_id = ?",
                    (name, owner_user_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE name = ?", (name,)
                ).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, owner_user_id: str | None = None,
                  account_id: str | None = None) -> list[Job]:
        with self._lock:
            if owner_user_id and account_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE owner_user_id = ? AND account_id = ? ORDER BY created_at",
                    (owner_user_id, account_id),
                ).fetchall()
            elif owner_user_id:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE owner_user_id = ? ORDER BY created_at",
                    (owner_user_id,),
                ).fetchall()
            elif account_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE account_id = ? ORDER BY created_at",
                    (account_id,),
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

    def prune_done_jobs(self, owner_user_id: str, account_id: str, keep: int) -> int:
        """限制"已执行完的一次性任务"留存数量，避免历史无限膨胀。

        已执行完 = date 任务、enabled=0 且 last_run_at 非空（executor 触发成功后标记）。
        按 last_run_at 倒序保留最近 keep 条，其余删除。返回删除条数。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE id IN ("
                "SELECT id FROM jobs "
                "WHERE owner_user_id = ? AND account_id = ? AND schedule_kind = 'date' "
                "AND enabled = 0 AND last_run_at IS NOT NULL "
                "ORDER BY last_run_at DESC LIMIT -1 OFFSET ?)",
                (owner_user_id, account_id, keep),
            )
        return cur.rowcount

    def set_context_token(self, job_id: str, context_token: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET context_token = ? WHERE id = ?",
                (context_token, job_id),
            )

    def migrate_account_id(self, account_id: str) -> int:
        """Assign account_id to all rows that have empty account_id (single-tenant migration)."""
        with self._lock:
            c1 = self._conn.execute(
                "UPDATE jobs SET account_id = ? WHERE account_id = ''", (account_id,)
            )
            c2 = self._conn.execute(
                "UPDATE chat_history SET account_id = ? WHERE account_id = ''", (account_id,)
            )
            c3 = self._conn.execute(
                "UPDATE expenses SET account_id = ? WHERE account_id = ''", (account_id,)
            )
        return c1.rowcount + c2.rowcount + c3.rowcount

    # ── expenses ──

    def insert_expense(self, e: Expense) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO expenses (id,account_id,owner_user_id,amount_cents,item,"
                "category,note,context_token,spent_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                e.to_row(),
            )

    def get_expense(self, expense_id: str) -> Expense | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        return Expense.from_row(row) if row else None

    def get_expense_by_prefix(self, prefix: str, account_id: str | None = None) -> Expense | None:
        with self._lock:
            if account_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM expenses WHERE id LIKE ? AND account_id = ? LIMIT 2",
                    (f"{prefix}%", account_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM expenses WHERE id LIKE ? LIMIT 2", (f"{prefix}%",)
                ).fetchall()
        if len(rows) == 1:
            return Expense.from_row(rows[0])
        return None

    def list_expenses(self, owner_user_id: str | None = None, account_id: str | None = None,
                      since: int | None = None, until: int | None = None,
                      category: str | None = None, limit: int | None = None) -> list[Expense]:
        clauses: list[str] = []
        params: list[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?"); params.append(owner_user_id)
        if account_id is not None:
            clauses.append("account_id = ?"); params.append(account_id)
        if since is not None:
            clauses.append("spent_at >= ?"); params.append(since)
        if until is not None:
            clauses.append("spent_at < ?"); params.append(until)
        if category:
            clauses.append("category = ?"); params.append(category)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM expenses{where} ORDER BY spent_at DESC"
        if limit is not None:
            sql += " LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Expense.from_row(r) for r in rows]

    def update_expense_fields(self, expense_id: str, **fields: Any) -> Expense | None:
        if not fields:
            return self.get_expense(expense_id)
        allowed = {"amount_cents", "item", "category", "note", "spent_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update expense fields: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE expenses SET {sets} WHERE id = ?",
                (*fields.values(), expense_id),
            )
        return self.get_expense(expense_id)

    def delete_expense(self, expense_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        return cur.rowcount > 0

    # ── chat history ──

    def append_message(self, owner_user_id: str, role: str, content: str,
                       account_id: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_history (account_id, owner_user_id, role, content, created_at) "
                "VALUES (?,?,?,?,?)",
                (account_id, owner_user_id, role, content, int(time.time())),
            )

    def get_history(self, owner_user_id: str, limit: int = 20,
                    account_id: str | None = None) -> list[dict[str, str]]:
        with self._lock:
            if account_id is not None:
                rows = self._conn.execute(
                    "SELECT role, content FROM chat_history "
                    "WHERE account_id = ? AND owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (account_id, owner_user_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT role, content FROM chat_history "
                    "WHERE owner_user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (owner_user_id, limit),
                ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_history(self, owner_user_id: str, account_id: str | None = None) -> int:
        with self._lock:
            if account_id is not None:
                cur = self._conn.execute(
                    "DELETE FROM chat_history WHERE account_id = ? AND owner_user_id = ?",
                    (account_id, owner_user_id),
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM chat_history WHERE owner_user_id = ?", (owner_user_id,)
                )
        return cur.rowcount

    # ── user prefs (long-term: 称呼 / 风格) ──────────────────────

    PREF_FIELDS = ("bot_name", "user_title", "persona")

    def get_prefs(self, owner_user_id: str, account_id: str = "") -> dict[str, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT bot_name, user_title, persona FROM user_prefs "
                "WHERE account_id = ? AND owner_user_id = ?",
                (account_id, owner_user_id),
            ).fetchone()
        if not row:
            return {k: "" for k in self.PREF_FIELDS}
        return {k: row[k] for k in self.PREF_FIELDS}

    def set_prefs(self, owner_user_id: str, account_id: str = "", **fields: str) -> dict[str, str]:
        """全量覆盖传入的字段（空串=恢复默认/清空），未传的保持不变。返回更新后的全部偏好。"""
        clean = {k: ("" if v is None else str(v)).strip()
                 for k, v in fields.items() if k in self.PREF_FIELDS}
        if not clean:
            return self.get_prefs(owner_user_id, account_id)
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_prefs (account_id, owner_user_id, updated_at) "
                "VALUES (?,?,?)",
                (account_id, owner_user_id, now),
            )
            sets = ", ".join(f"{k} = ?" for k in clean)
            self._conn.execute(
                f"UPDATE user_prefs SET {sets}, updated_at = ? "
                "WHERE account_id = ? AND owner_user_id = ?",
                (*clean.values(), now, account_id, owner_user_id),
            )
        return self.get_prefs(owner_user_id, account_id)
