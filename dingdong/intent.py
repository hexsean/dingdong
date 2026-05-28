"""自然语言意图层。

入站消息 → LLM（带工具）→ 操作任务表 → 回复。
对话历史持久化在 SQLite chat_history 表中。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .llm import LLMProvider, ToolSpec
from .scheduler import ScheduleSpecError, Scheduler, build_trigger
from .search import is_available as search_available, search as exa_search, read_url as exa_read_url
from .self_update import (
    WECHAT_UPDATE_DISABLED_HINT,
    read_update_result,
    trigger_watchtower_update,
    update_configured,
)
from .storage import Job, JobStore, VALID_SCHEDULE_KINDS, describe_schedule, new_job_id
from .updater import is_disabled, is_newer_version, local_version, remote_version, set_disabled

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

CLEAR_KEYWORDS = {"清空对话", "新对话", "重置对话", "清除历史"}
GREETING_KEYWORDS = {"你好", "hi", "hello", "hey", "嗨", "在吗", "在不在", "你在吗"}
GREETING_REPLY = "在的。可以直接说：「每天 9 点提醒我喝水」「我有哪些任务」「检查更新」。"
UPDATE_KEYWORDS = {"更新叮咚", "升级叮咚", "立即更新", "开始更新"}
UPDATE_STATUS_KEYWORDS = {"更新状态", "查看更新状态"}
UPDATE_CONFIRM_KEYWORDS = {"确认更新", "确认升级"}
CHECK_UPDATE_KEYWORDS = {"检查更新", "检查版本", "版本", "当前版本"}
DELETE_ALL_CONFIRM_KEYWORDS = {"确认删除全部", "确认删除所有任务", "确认全部删除"}
DELETE_ALL_CANCEL_KEYWORDS = {"取消", "取消删除", "不用", "不删了", "no", "n"}

INTENT_SYSTEM_PROMPT = """\
你是叮咚，微信定时任务助手。极简回复，不废话。

用工具管理任务。有 search_web 时可搜索。日常对话简短回。

严格规则：
- 用户说"删除"就只调 delete_job，不要先 list 再删，直接按名称或 id 删
- 用户要删多个任务，用 job_ids 数组一次删完，或用 job_id="all" 全删
- 用户问任务列表，只调 list_jobs，不要创建任何任务
- 不要自作主张创建用户没要求的任务
- 创建/修改/删除任务回复一句话说完
- 展示任务列表时必须完整显示每个任务的全部信息（名称、目标、计划、状态、下次触发），不要省略任何任务或字段
- 搜索结果要详细展示：列出要点、来源，不要过度压缩。可用 read_url 获取页面详情后再总结

用户问功能时告知：发"清空对话"重置记录；"我有哪些任务"查看列表；发"检查更新"查看版本；换绑需在服务器操作。

任务字段：name(名称) goal(目标描述) schedule_kind(cron/interval/date)
- cron: cron_expression 5字段
- interval: seconds/minutes/hours/days/weeks
- date: run_at "YYYY-MM-DD HH:MM:SS"

相对时间转绝对值，本地时区。
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
        description="返回服务器当前日期、时间和星期。用于计算相对时间。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="list_jobs",
        description="返回该用户的所有定时任务列表。必须完整展示每个任务的全部字段：名称、目标、计划类型、计划详情、启用状态、下次触发时间。不得省略任何任务或字段。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="create_job",
        description="创建定时任务。必填 name + goal + schedule_kind。"
                    "cron: 提供 cron_expression（5字段，如 '0 9 * * *'）。"
                    "interval: 提供 seconds/minutes/hours/days/weeks 至少一个。"
                    "date: 提供 run_at（'YYYY-MM-DD HH:MM:SS'，本地时区）。",
        input_schema={
            "type": "object",
            "required": ["name", "goal", "schedule_kind"],
            "properties": {
                "name": {"type": "string", "description": "任务名称"},
                "goal": {"type": "string", "description": "触发时要做什么的自然语言描述"},
                "schedule_kind": {"type": "string", "enum": ["cron", "interval", "date"]},
                "cron_expression": {"type": "string", "description": "5字段 crontab"},
                "seconds": {"type": "integer", "minimum": 1},
                "minutes": {"type": "integer", "minimum": 1},
                "hours": {"type": "integer", "minimum": 1},
                "days": {"type": "integer", "minimum": 1},
                "weeks": {"type": "integer", "minimum": 1},
                "run_at": {"type": "string", "description": "YYYY-MM-DD HH:MM:SS"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="update_job",
        description="修改已有任务。job_id 支持完整 id 或前缀（至少4位）。只传需要改的字段，其余不变。",
        input_schema={
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {"type": "string", "description": "任务 id 或前缀"},
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
        description="删除任务。传 job_id 删单个，传 job_ids 数组批量删，传 job_id='all' 删除该用户全部任务。",
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "任务 id/前缀，或 'all' 删全部"},
                "job_ids": {"type": "array", "items": {"type": "string"}, "description": "批量删除的 id 列表"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="set_enabled",
        description="启用(true)或暂停(false)一个任务。",
        input_schema={
            "type": "object", "required": ["job_id", "enabled"],
            "properties": {
                "job_id": {"type": "string", "description": "任务 id 或前缀"},
                "enabled": {"type": "boolean", "description": "true=启用 false=暂停"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="run_now",
        description="立即触发一次任务执行，不影响后续定时计划。",
        input_schema={
            "type": "object", "required": ["job_id"],
            "properties": {"job_id": {"type": "string", "description": "任务 id 或前缀"}},
            "additionalProperties": False,
        },
    ),
]

SEARCH_TOOL_SPECS = [
    ToolSpec(
        name="search_web",
        description="搜索互联网，返回多条结果摘要（标题+URL+摘要）。用于发现信息。",
        input_schema={
            "type": "object", "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="read_url",
        description="读取指定 URL 的页面正文（最多 5000 字符）。用于获取搜索结果中某个页面的详细内容。",
        input_schema={
            "type": "object", "required": ["url"],
            "properties": {"url": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
]


def _tool_status(name: str, args: dict[str, Any]) -> str | None:
    if name == "search_web":
        return f"搜索「{args.get('query', '')}」..."
    if name == "create_job":
        return f"创建「{args.get('name', '任务')}」..."
    return None


CONFIRM_KEYWORDS = {"确认", "确定", "是", "yes", "y"}
CANCEL_KEYWORDS = {"取消", "取消更新", "不用", "不更新", "先不更新", "no", "n"}


class IntentRouter:
    def __init__(self, llm: LLMProvider, store: JobStore, scheduler: Scheduler, *,
                 history_limit: int = 20, model_info: "ModelInfo | None" = None,
                 vision_llm: LLMProvider | None = None,
                 wechat_update_enabled: bool = False,
                 watchtower_url: str = "http://watchtower:8080/v1/update",
                 watchtower_token: str = "") -> None:
        from .models import ModelInfo
        self._llm = llm
        self._vision_llm = vision_llm
        self._store = store
        self._scheduler = scheduler
        self._history_limit = history_limit
        self._on_status = None
        self._on_update_progress = None
        self._pending_clear: set[str] = set()
        self._pending_update: dict[str, str] = {}
        self._pending_delete_all: set[str] = set()
        self._model_info = model_info or ModelInfo(model_id="")
        self._vision_supported = self._model_info.supports_vision
        self._wechat_update_enabled = wechat_update_enabled
        self._watchtower_url = watchtower_url
        self._watchtower_token = watchtower_token

    def set_callbacks(self, *, on_status=None, on_update_progress=None) -> None:
        self._on_status = on_status
        self._on_update_progress = on_update_progress

    def handle(self, *, owner_user_id: str, context_token: str, text: str,
               image_bytes_list: list[bytes] | None = None) -> str:
        stripped = text.strip()
        has_images = bool(image_bytes_list)

        if not has_images:
            if owner_user_id in self._pending_update:
                if stripped in UPDATE_CONFIRM_KEYWORDS:
                    latest = self._pending_update.pop(owner_user_id)
                    return self._start_update(owner_user_id, context_token, latest)
                if stripped in CANCEL_KEYWORDS or stripped.lower() in CANCEL_KEYWORDS:
                    self._pending_update.pop(owner_user_id, None)
                    return "已取消更新。"
                if stripped.lower() in CONFIRM_KEYWORDS:
                    return "为避免误操作，请回复「确认更新」开始；回复「取消更新」放弃。"
                if stripped not in CHECK_UPDATE_KEYWORDS and stripped not in UPDATE_KEYWORDS and stripped not in UPDATE_STATUS_KEYWORDS:
                    self._pending_update.pop(owner_user_id, None)
            if owner_user_id in self._pending_delete_all:
                self._pending_delete_all.discard(owner_user_id)
                if stripped in DELETE_ALL_CONFIRM_KEYWORDS:
                    result = self._tool_delete_job({"job_id": "all", "confirm": "yes"}, owner_user_id)
                    return _quick_reply("delete_job", result) or "已删除全部任务。"
                if stripped in DELETE_ALL_CANCEL_KEYWORDS or stripped.lower() in DELETE_ALL_CANCEL_KEYWORDS:
                    return "已取消删除全部任务。"
                return "已取消删除全部任务。"
            if owner_user_id in self._pending_clear:
                self._pending_clear.discard(owner_user_id)
                if stripped.lower() in CONFIRM_KEYWORDS:
                    n = self._store.clear_history(owner_user_id)
                    return f"已清空对话记录（{n} 条）。"
                return "已取消。"
            if stripped in CLEAR_KEYWORDS:
                self._pending_clear.add(owner_user_id)
                return "确认清空所有对话记录？回复「确认」执行。"
            if stripped.lower() in GREETING_KEYWORDS:
                self._store.append_message(owner_user_id, "user", text)
                self._store.append_message(owner_user_id, "assistant", GREETING_REPLY)
                return GREETING_REPLY
            if stripped == "关闭更新提醒":
                set_disabled(self._store.db_path.parent, True)
                return "已关闭更新提醒。发「开启更新提醒」可恢复。"
            if stripped == "开启更新提醒":
                set_disabled(self._store.db_path.parent, False)
                return "已开启更新提醒。"
            if stripped in CHECK_UPDATE_KEYWORDS:
                return self._check_update(owner_user_id)
            if stripped in UPDATE_CONFIRM_KEYWORDS:
                return self._confirm_update(owner_user_id, context_token)
            if stripped in UPDATE_STATUS_KEYWORDS:
                return self._update_status()
            if stripped in UPDATE_KEYWORDS:
                return self._prepare_update(owner_user_id)

            shortcut = self._try_shortcut(stripped, owner_user_id)
            if shortcut is not None:
                self._store.append_message(owner_user_id, "user", text)
                self._store.append_message(owner_user_id, "assistant", shortcut)
                return shortcut

        if has_images and not self._vision_supported:
            return ("当前配置的模型不支持图片理解。\n"
                    "方案一：主模型换为 Claude Sonnet、GPT-4o 等视觉模型\n"
                    "方案二：在 .env 中单独配置视觉模型（VISION_PROVIDER / VISION_MODEL / VISION_API_KEY）")

        use_direct_vision = has_images and not self._vision_llm
        if has_images and self._vision_llm:
            image_desc = self._describe_images(image_bytes_list, text)
            user_text = f"{text}\n\n[图片内容：{image_desc}]" if text else f"[图片内容：{image_desc}]"
            self._store.append_message(owner_user_id, "user", user_text)
        else:
            self._store.append_message(owner_user_id, "user", text or "[图片]")

        history = self._store.get_history(owner_user_id, limit=self._history_limit)
        messages: list[dict[str, Any]] = list(history)

        if use_direct_vision:
            last_user = messages[-1] if messages and messages[-1]["role"] == "user" else None
            if last_user:
                content_parts: list[dict[str, Any]] = []
                content_parts.append({"type": "text", "text": text or "请描述这张图片。"})
                for img_data in image_bytes_list:
                    content_parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(img_data).decode(),
                        },
                    })
                last_user["content"] = content_parts
        for round_idx in range(MAX_TOOL_ROUNDS):
            tools = list(TOOL_SPECS)
            if search_available():
                tools.extend(SEARCH_TOOL_SPECS)
            resp = self._llm.chat(
                system=self._system_prompt(),
                messages=messages,
                tools=tools,
                max_tokens=2048,
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
            needs_llm_summary = False
            for tu in tool_uses:
                result_text = self._execute_tool(
                    name=tu["name"],
                    args=tu.get("input") or {},
                    owner_user_id=owner_user_id,
                    context_token=context_token,
                )
                tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": result_text})
                if tu["name"] in ("search_web", "read_url"):
                    needs_llm_summary = True

            QUICK_REPLY_TOOLS = {"create_job", "update_job"}
            if not needs_llm_summary and len(tool_uses) == 1 and tool_uses[0]["name"] in QUICK_REPLY_TOOLS:
                quick = _quick_reply(tool_uses[0]["name"], tool_results[0]["content"])
                if quick:
                    self._store.append_message(owner_user_id, "assistant", quick)
                    return quick

            messages.append({"role": "user", "content": tool_results})

        fallback = "（已达到工具调用上限，请把指令拆开说）"
        self._store.append_message(owner_user_id, "assistant", fallback)
        return fallback

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self._scheduler.tz)

    def _system_prompt(self) -> str:
        now = datetime.now(self._tz())
        weekday = "星期" + "一二三四五六日"[now.weekday()]
        return INTENT_SYSTEM_PROMPT + f"\n当前版本：v{local_version()}\n当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')} {weekday}\n"

    # ---------- vision ----------

    _VISION_SYSTEM = "描述图片内容，简洁准确，中文。如果用户附带了问题就直接回答。"

    def _describe_images(self, image_bytes_list: list[bytes], user_text: str) -> str:
        """用视觉模型将图片转为文字描述。"""
        llm = self._vision_llm or self._llm
        content_parts: list[dict[str, Any]] = []
        if user_text:
            content_parts.append({"type": "text", "text": user_text})
        else:
            content_parts.append({"type": "text", "text": "请描述这张图片。"})
        for img_data in image_bytes_list:
            content_parts.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(img_data).decode(),
                },
            })
        resp = llm.chat(
            system=self._VISION_SYSTEM,
            messages=[{"role": "user", "content": content_parts}],
            max_tokens=1024,
        )
        return resp.text() or "[图片描述失败]"

    # ---------- shortcuts (skip LLM entirely,仅精确匹配) ----------

    _LIST_SHORTCUTS = {"任务列表", "查看任务", "我有哪些任务"}

    def _try_shortcut(self, text: str, owner_user_id: str) -> str | None:
        if text in self._LIST_SHORTCUTS:
            result = self._tool_list_jobs(owner_user_id)
            return _quick_reply("list_jobs", result)
        return None

    # ---------- self update ----------

    def _check_update(self, owner_user_id: str) -> str:
        lv = local_version()
        rv = remote_version()
        if rv is None:
            return f"当前版本 v{lv}，无法连接更新服务器。"
        if not is_newer_version(rv, lv):
            return f"当前版本 v{lv}，已是最新。"
        lines = [f"当前版本 v{lv}，最新版本 v{rv}。"]
        if self._can_wechat_update():
            self._pending_update[owner_user_id] = rv
            lines.append("为避免误操作，请回复「确认更新」开始；回复「取消更新」放弃。")
        else:
            lines.append(WECHAT_UPDATE_DISABLED_HINT)
        return "\n".join(lines)

    def _confirm_update(self, owner_user_id: str, context_token: str) -> str:
        lv = local_version()
        rv = remote_version()
        if rv is None:
            return f"当前版本 v{lv}，无法连接更新服务器。"
        if not is_newer_version(rv, lv):
            return f"当前版本 v{lv}，已是最新。"
        return self._start_update(owner_user_id, context_token, rv)

    def _prepare_update(self, owner_user_id: str) -> str:
        if not self._can_wechat_update():
            return WECHAT_UPDATE_DISABLED_HINT
        lv = local_version()
        rv = remote_version()
        if rv is None:
            return f"当前版本 v{lv}，无法连接更新服务器。"
        if not is_newer_version(rv, lv):
            return f"当前版本 v{lv}，已是最新。"
        self._pending_update[owner_user_id] = rv
        return f"将从 v{lv} 更新到 v{rv}，期间会短暂重启。\n为避免误操作，请回复「确认更新」开始；回复「取消更新」放弃。"

    def _start_update(self, owner_user_id: str, context_token: str, latest: str) -> str:
        if not self._can_wechat_update():
            return WECHAT_UPDATE_DISABLED_HINT

        def notify(text: str) -> None:
            if self._on_update_progress:
                self._on_update_progress(owner_user_id, context_token, text)

        t = threading.Thread(
            target=trigger_watchtower_update,
            kwargs={
                "data_dir": self._store.db_path.parent,
                "url": self._watchtower_url,
                "token": self._watchtower_token,
                "target_version": latest,
                "owner_user_id": owner_user_id,
                "context_token": context_token,
                "notify": notify,
            },
            daemon=True,
            name="watchtower-update",
        )
        t.start()
        return f"开始更新到 v{latest}，稍后会短暂重启。"

    def _update_status(self) -> str:
        result = read_update_result(self._store.db_path.parent)
        if result is None:
            return "暂无更新任务。"
        status = result.get("status", "unknown")
        version = result.get("target_version", "")
        message = result.get("message", "")
        legacy_messages = {
            "cannot reach updater service": "无法连接更新服务",
            "无法连接 updater": "无法连接更新服务",
            "updater token invalid": "更新令牌无效",
            "updater 令牌无效": "更新令牌无效",
            "updater is still working": "更新仍在执行",
            "updater 仍在执行": "更新仍在执行",
            "triggered": "正在更新",
        }
        message = legacy_messages.get(message, message)
        if message.startswith("updater returned "):
            message = "更新服务返回 " + message.removeprefix("updater returned ")
        elif message.startswith("updater 返回 "):
            message = "更新服务返回 " + message.removeprefix("updater 返回 ")
        lv = local_version()
        if version and not is_newer_version(version, lv):
            return f"更新完成：v{lv}。"
        if status == "running":
            return f"正在更新到 v{version}..."
        if status == "done":
            return f"正在更新到 v{version}..."
        if status == "pending":
            return f"更新仍在等待生效：v{version}。请稍后再发「更新状态」查看；如果 10 分钟后仍未完成，再发「确认更新」重试。"
        if status == "failed":
            return f"更新失败：{message or '请稍后再试'}。"
        return f"更新状态：{status} {message}".strip()

    def _can_wechat_update(self) -> bool:
        return update_configured(self._wechat_update_enabled, self._watchtower_token)

    # ---------- tools ----------

    def _execute_tool(self, *, name: str, args: dict[str, Any], owner_user_id: str, context_token: str) -> str:
        try:
            if name == "get_current_time":
                now = datetime.now(self._tz())
                weekday = "星期" + "一二三四五六日"[now.weekday()]
                return f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')} {weekday}"
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
            if name == "read_url":
                return self._tool_read_url(args)
            return _err(f"未知工具 {name}")
        except ValueError as exc:
            log.warning("tool %s validation: %s", name, exc)
            return _err(str(exc))
        except Exception as exc:
            log.exception("tool %s failed", name)
            return _err(str(exc))

    def _tool_list_jobs(self, owner_user_id: str) -> str:
        jobs = self._store.list_jobs(owner_user_id=owner_user_id)
        if not jobs:
            return _ok({"jobs": []}, note="还没有任务。试试：「每天 9 点提醒我喝水」。")
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
        kind = args.get("schedule_kind")
        has_schedule_params = any(k in args for k in ("cron_expression", "seconds", "minutes", "hours", "days", "weeks", "run_at"))
        if not kind and has_schedule_params:
            if "cron_expression" in args:
                kind = "cron"
            elif "run_at" in args:
                kind = "date"
            else:
                kind = "interval"
        if kind:
            if kind not in VALID_SCHEDULE_KINDS:
                return _err(f"schedule_kind 须为 {sorted(VALID_SCHEDULE_KINDS)}")
            fields["schedule_kind"] = kind
            fields["schedule_value"] = _build_schedule_value(kind, args)
        old_kind, old_val = job.schedule_kind, job.schedule_value
        updated = self._store.update_fields(job.id, **fields)
        if updated is None:
            return _err("更新失败")
        try:
            self._scheduler.sync_job(updated)
        except Exception as exc:
            self._store.update_fields(job.id, schedule_kind=old_kind, schedule_value=old_val)
            self._scheduler.sync_job(job)
            return _err(f"schedule invalid: {exc}")
        nr = self._scheduler.get_next_run(updated.id)
        return _ok({"updated": _job_to_brief(updated), "next_run_at": nr.isoformat() if nr else None,
                     "schedule_human": describe_schedule(updated.schedule_kind, updated.schedule_value)})

    def _tool_delete_job(self, args: dict[str, Any], owner_user_id: str) -> str:
        ids = args.get("job_ids") or []
        single = args.get("job_id", "")
        if (single == "all" or not ids) and args.get("confirm") != "yes":
            jobs = self._store.list_jobs(owner_user_id=owner_user_id)
            if not jobs:
                return _ok({"deleted_count": 0}, note="没有任务可删")
            self._pending_delete_all.add(owner_user_id)
            return _err(f"将删除全部 {len(jobs)} 个任务。请回复「确认删除全部」执行，或回复「取消」放弃。")
        if single:
            ids = [single] if single != "all" else []

        if single == "all" or not ids:
            jobs = self._store.list_jobs(owner_user_id=owner_user_id)
            if not jobs:
                return _ok({"deleted_count": 0}, note="没有任务可删")
            ids = [j.id for j in jobs]

        deleted = []
        for raw_id in ids:
            job = self._resolve_job(raw_id, owner_user_id)
            if job is None:
                continue
            self._scheduler.remove_job(job.id)
            self._store.delete(job.id)
            deleted.append(job.name)

        if not deleted:
            return _err("没有找到可删除的任务")
        return _ok({"deleted_count": len(deleted), "deleted_names": deleted})

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

    def _tool_read_url(self, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return _err("url is required")
        return exa_read_url(url)

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


def _quick_reply(tool_name: str, result_json: str) -> str | None:
    """从工具结果直接生成用户可读的回复，省掉第二轮 LLM 调用。返回 None 表示需要 LLM 总结。"""
    try:
        r = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not r.get("ok"):
        return r.get("error") or "操作失败。"

    if tool_name == "create_job":
        c = r.get("created", {})
        return f"已创建「{c.get('name', '')}」，{r.get('schedule_human', '')}，下次触发：{_fmt_time(r.get('next_run_at'))}。"

    if tool_name == "update_job":
        u = r.get("updated", {})
        return f"已更新「{u.get('name', '')}」，{r.get('schedule_human', '')}。"

    if tool_name == "delete_job":
        names = r.get("deleted_names", [])
        count = r.get("deleted_count", len(names))
        if count == 1 and names:
            return f"已删除「{names[0]}」。"
        return f"已删除 {count} 个任务。"

    if tool_name == "set_enabled":
        status = "启用" if r.get("enabled") else "暂停"
        return f"已{status}。"

    if tool_name == "run_now":
        return f"已触发「{r.get('queued_name', '')}」。"

    if tool_name == "list_jobs" and not r.get("jobs"):
        return r.get("note") or "还没有任务。试试：「每天 9 点提醒我喝水」。"

    # 非空 list_jobs / get_current_time 是只读查询，可能是多步操作的前置步骤，不拦截

    return None


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "待定"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


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
