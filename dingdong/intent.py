"""自然语言意图层。

入站消息 → LLM（带工具）→ 操作任务表 → 回复。
对话历史持久化在 SQLite chat_history 表中。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .llm import LLMProvider, ToolSpec
from .scheduler import ScheduleSpecError, Scheduler, build_trigger
from .search import is_available as search_available, search as exa_search
from .storage import Job, JobStore, VALID_SCHEDULE_KINDS, describe_schedule, new_job_id

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

CLEAR_KEYWORDS = {"清空对话", "新对话", "重置对话", "清除历史"}

INTENT_SYSTEM_PROMPT = """\
你是叮咚，一个微信里的定时任务助手。

核心能力：用工具管理定时任务（增删改查、暂停、立即执行）。
有 search_web 工具时可查实时信息。日常对话正常友好回复。

任务字段：
- name: 任务名
- goal: 触发时要做什么（自然语言描述）
- schedule: cron("0 9 * * *") / interval(seconds/minutes/hours等) / date("YYYY-MM-DD HH:MM:SS")

规则：
- 相对时间结合当前时间转绝对值，本地时区
- 操作后一句话确认
- 列任务格式：[id前8位] name · 计划 · 目标
"""


def _job_to_brief(job: Job) -> dict[str, Any]:
    return {
        "id": job.id, "name": job.name, "goal": job.goal,
        "schedule_kind": job.schedule_kind, "schedule_value": job.schedule_value,
        "enabled": job.enabled, "last_run_at": job.last_run_at, "last_result": job.last_result,
    }


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="get_current_time",
        description="获取当前日期和时间。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="list_jobs",
        description="列出当前用户的所有定时任务。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="create_job",
        description="新建定时任务。cron 需 cron_expression(5字段)；interval 需 seconds/minutes/hours/days/weeks；date 需 run_at。",
        input_schema={
            "type": "object",
            "required": ["name", "goal", "schedule_kind"],
            "properties": {
                "name": {"type": "string"},
                "goal": {"type": "string"},
                "schedule_kind": {"type": "string", "enum": ["cron", "interval", "date"]},
                "cron_expression": {"type": "string"},
                "seconds": {"type": "integer", "minimum": 1},
                "minutes": {"type": "integer", "minimum": 1},
                "hours": {"type": "integer", "minimum": 1},
                "days": {"type": "integer", "minimum": 1},
                "weeks": {"type": "integer", "minimum": 1},
                "run_at": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="update_job",
        description="修改任务。job_id 可用完整 id 或前缀。未提供的字段不变。",
        input_schema={
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {"type": "string"},
                "name": {"type": "string"}, "goal": {"type": "string"},
                "schedule_kind": {"type": "string", "enum": ["cron", "interval", "date"]},
                "cron_expression": {"type": "string"},
                "seconds": {"type": "integer", "minimum": 1},
                "minutes": {"type": "integer", "minimum": 1},
                "hours": {"type": "integer", "minimum": 1},
                "days": {"type": "integer", "minimum": 1},
                "weeks": {"type": "integer", "minimum": 1},
                "run_at": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="delete_job",
        description="删除任务。",
        input_schema={"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}, "additionalProperties": False},
    ),
    ToolSpec(
        name="set_enabled",
        description="启用或暂停任务。",
        input_schema={"type": "object", "required": ["job_id", "enabled"], "properties": {"job_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "additionalProperties": False},
    ),
    ToolSpec(
        name="run_now",
        description="立即触发一次任务。",
        input_schema={"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}, "additionalProperties": False},
    ),
]

SEARCH_TOOL_SPEC = ToolSpec(
    name="search_web",
    description="搜索互联网获取实时信息。",
    input_schema={
        "type": "object", "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "num_results": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "additionalProperties": False,
    },
)


def _tool_status(name: str, args: dict[str, Any]) -> str | None:
    if name == "search_web":
        return f"搜索「{args.get('query', '')}」..."
    if name == "create_job":
        return f"创建「{args.get('name', '任务')}」..."
    if name == "update_job":
        return f"更新「{args.get('job_id', '')[:8]}」..."
    if name == "delete_job":
        return f"删除「{args.get('job_id', '')[:8]}」..."
    if name == "run_now":
        return f"触发「{args.get('job_id', '')[:8]}」..."
    return None


class IntentRouter:
    def __init__(self, llm: LLMProvider, store: JobStore, scheduler: Scheduler, *, history_limit: int = 20) -> None:
        self._llm = llm
        self._store = store
        self._scheduler = scheduler
        self._history_limit = history_limit
        self._on_typing = None
        self._on_status = None

    def set_callbacks(self, *, on_typing=None, on_status=None) -> None:
        """on_typing(owner, ctx): 刷新输入中状态。on_status(owner, ctx, text): 发送中间状态消息。"""
        self._on_typing = on_typing
        self._on_status = on_status

    def handle(self, *, owner_user_id: str, context_token: str, text: str) -> str:
        if text.strip() in CLEAR_KEYWORDS:
            n = self._store.clear_history(owner_user_id)
            return f"已清空对话记录（{n} 条）。"

        self._store.append_message(owner_user_id, "user", text)
        history = self._store.get_history(owner_user_id, limit=self._history_limit)

        messages: list[dict[str, Any]] = list(history)
        for round_idx in range(MAX_TOOL_ROUNDS):
            if self._on_typing:
                self._on_typing(owner_user_id, context_token)

            tools = list(TOOL_SPECS)
            if search_available():
                tools.append(SEARCH_TOOL_SPEC)
            resp = self._llm.chat(
                system=self._system_prompt(),
                messages=messages,
                tools=tools,
                max_tokens=1024,
            )
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
            if resp.reasoning_content is not None:
                assistant_msg["reasoning_content"] = resp.reasoning_content
            messages.append(assistant_msg)

            tool_uses = resp.tool_uses()
            if not tool_uses:
                reply = resp.text() or "好的。"
                self._store.append_message(owner_user_id, "assistant", reply)
                return reply

            if self._on_status:
                parts = [s for tu in tool_uses if (s := _tool_status(tu["name"], tu.get("input") or {}))]
                if parts:
                    self._on_status(owner_user_id, context_token, " ".join(parts))

            tool_results = []
            for tu in tool_uses:
                result_text = self._execute_tool(
                    name=tu["name"],
                    args=tu.get("input") or {},
                    owner_user_id=owner_user_id,
                    context_token=context_token,
                )
                tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": result_text})
            messages.append({"role": "user", "content": tool_results})

        fallback = "（已达到工具调用上限，请把指令拆开说）"
        self._store.append_message(owner_user_id, "assistant", fallback)
        return fallback

    def _system_prompt(self) -> str:
        now = datetime.now().astimezone()
        return INTENT_SYSTEM_PROMPT + f"\n当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"

    # ---------- tools ----------

    def _execute_tool(self, *, name: str, args: dict[str, Any], owner_user_id: str, context_token: str) -> str:
        try:
            if name == "get_current_time":
                now = datetime.now().astimezone()
                return now.strftime("%Y-%m-%d %H:%M:%S %Z (星期%w)").replace("星期0","星期日").replace("星期1","星期一").replace("星期2","星期二").replace("星期3","星期三").replace("星期4","星期四").replace("星期5","星期五").replace("星期6","星期六")
            if name == "list_jobs":
                return self._tool_list_jobs(owner_user_id)
            if name == "create_job":
                return self._tool_create_job(args, owner_user_id, context_token)
            if name == "update_job":
                return self._tool_update_job(args, owner_user_id, context_token)
            if name == "delete_job":
                return self._tool_delete_job(args, owner_user_id)
            if name == "set_enabled":
                return self._tool_set_enabled(args, owner_user_id)
            if name == "run_now":
                return self._tool_run_now(args, owner_user_id)
            if name == "search_web":
                return self._tool_search_web(args)
            return _err(f"未知工具 {name}")
        except Exception as exc:
            log.exception("tool %s failed", name)
            return _err(str(exc))

    def _tool_list_jobs(self, owner_user_id: str) -> str:
        jobs = self._store.list_jobs(owner_user_id=owner_user_id)
        if not jobs:
            return _ok({"jobs": []}, note="没有任务")
        briefs = []
        for j in jobs:
            b = _job_to_brief(j)
            nr = self._scheduler.get_next_run(j.id)
            b["next_run_at"] = nr.isoformat() if nr else None
            briefs.append(b)
        return _ok({"jobs": briefs})

    def _tool_create_job(self, args: dict[str, Any], owner_user_id: str, context_token: str) -> str:
        kind = args.get("schedule_kind")
        if kind not in VALID_SCHEDULE_KINDS:
            return _err(f"schedule_kind 须为 {sorted(VALID_SCHEDULE_KINDS)}")
        schedule_value = _build_schedule_value(kind, args)
        job = Job(
            id=new_job_id(), name=str(args["name"]).strip(), goal=str(args["goal"]).strip(),
            schedule_kind=kind, schedule_value=schedule_value,
            owner_user_id=owner_user_id, context_token=context_token, enabled=True,
        )
        try:
            build_trigger(kind, schedule_value, self._scheduler.tz)
        except ScheduleSpecError as exc:
            return _err(f"schedule invalid: {exc}")
        self._store.insert(job)
        try:
            self._scheduler.sync_job(job)
        except Exception as exc:
            self._store.delete(job.id)
            return _err(f"schedule rejected: {exc}")
        nr = self._scheduler.get_next_run(job.id)
        return _ok({"created": _job_to_brief(job), "next_run_at": nr.isoformat() if nr else None,
                     "schedule_human": describe_schedule(job.schedule_kind, job.schedule_value)})

    def _tool_update_job(self, args: dict[str, Any], owner_user_id: str, context_token: str) -> str:
        job = self._resolve_job(args.get("job_id", ""), owner_user_id)
        if job is None:
            return _err("找不到该任务")
        fields: dict[str, Any] = {"context_token": context_token}
        for k in ("name", "goal"):
            if k in args:
                fields[k] = str(args[k]).strip()
        if "enabled" in args:
            fields["enabled"] = bool(args["enabled"])
        if "schedule_kind" in args:
            kind = args["schedule_kind"]
            if kind not in VALID_SCHEDULE_KINDS:
                return _err(f"schedule_kind 须为 {sorted(VALID_SCHEDULE_KINDS)}")
            fields["schedule_kind"] = kind
            fields["schedule_value"] = _build_schedule_value(kind, args)
        updated = self._store.update_fields(job.id, **fields)
        if updated is None:
            return _err("更新失败")
        try:
            self._scheduler.sync_job(updated)
        except ScheduleSpecError as exc:
            return _err(f"schedule invalid: {exc}")
        nr = self._scheduler.get_next_run(updated.id)
        return _ok({"updated": _job_to_brief(updated), "next_run_at": nr.isoformat() if nr else None,
                     "schedule_human": describe_schedule(updated.schedule_kind, updated.schedule_value)})

    def _tool_delete_job(self, args: dict[str, Any], owner_user_id: str) -> str:
        job = self._resolve_job(args.get("job_id", ""), owner_user_id)
        if job is None:
            return _err("找不到该任务")
        self._scheduler.remove_job(job.id)
        self._store.delete(job.id)
        return _ok({"deleted_id": job.id, "deleted_name": job.name})

    def _tool_set_enabled(self, args: dict[str, Any], owner_user_id: str) -> str:
        job = self._resolve_job(args.get("job_id", ""), owner_user_id)
        if job is None:
            return _err("找不到该任务")
        enabled = bool(args.get("enabled", True))
        updated = self._store.update_fields(job.id, enabled=enabled)
        if updated is None:
            return _err("更新失败")
        self._scheduler.sync_job(updated)
        nr = self._scheduler.get_next_run(updated.id) if enabled else None
        return _ok({"id": updated.id, "enabled": updated.enabled, "next_run_at": nr.isoformat() if nr else None})

    def _tool_run_now(self, args: dict[str, Any], owner_user_id: str) -> str:
        job = self._resolve_job(args.get("job_id", ""), owner_user_id)
        if job is None:
            return _err("找不到该任务")
        self._scheduler.trigger_now(job)
        return _ok({"queued_id": job.id, "queued_name": job.name})

    def _tool_search_web(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return _err("query is required")
        return exa_search(query, num_results=min(int(args.get("num_results", 5)), 10))

    def _resolve_job(self, raw_id: str, owner_user_id: str) -> Job | None:
        raw_id = (raw_id or "").strip()
        if not raw_id:
            return None
        job = self._store.get(raw_id) or self._store.get_by_prefix(raw_id)
        if job is None:
            job = self._store.find_by_name(raw_id, owner_user_id=owner_user_id)
        if job is None or job.owner_user_id != owner_user_id:
            return None
        return job


def _ok(payload: dict[str, Any], note: str | None = None) -> str:
    body: dict[str, Any] = {"ok": True, **payload}
    if note:
        body["note"] = note
    return json.dumps(body, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _build_schedule_value(kind: str, args: dict[str, Any]) -> dict[str, Any]:
    if kind == "cron":
        expr = args.get("cron_expression")
        if not expr:
            raise ValueError("cron needs cron_expression")
        return {"expression": expr}
    if kind == "interval":
        out: dict[str, Any] = {}
        for u in ("seconds", "minutes", "hours", "days", "weeks"):
            if u in args:
                out[u] = int(args[u])
        if not out:
            raise ValueError("interval needs seconds/minutes/hours/days/weeks")
        return out
    if kind == "date":
        if "run_at" not in args:
            raise ValueError("date needs run_at")
        return {"run_at": str(args["run_at"]).strip()}
    raise ValueError(f"unknown kind {kind}")
