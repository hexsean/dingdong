"""自然语言意图层。

入站消息 → LLM（带工具）→ 操作任务表 → 回复。
对话历史持久化在 SQLite chat_history 表中。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .expense import (
    LARGE_EXPENSE_CENTS,
    aggregate_expenses as _aggregate_expenses,
    expense_to_brief as _expense_to_brief,
    fmt_yuan as _fmt_yuan,
    parse_day_end as _parse_day_end,
    parse_day_start as _parse_day_start,
    parse_when as _parse_when,
    period_range as _period_range,
    yuan_to_cents as _yuan_to_cents,
)
from .llm import LLMProvider, ToolSpec
from .scheduler import ScheduleSpecError, Scheduler, build_trigger
from .search import is_available as search_available, search as exa_search, read_url as exa_read_url
from .self_update import (
    WECHAT_UPDATE_DISABLED_HINT,
    read_update_result,
    trigger_watchtower_update,
    update_configured,
)
from .storage import (
    Expense, Job, JobStore, VALID_SCHEDULE_KINDS,
    describe_schedule, new_expense_id, new_job_id,
)
from .updater import is_disabled, is_newer_version, local_version, remote_version, set_disabled

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
RESPONSE_TOKEN_BUDGET = 2048
TOOL_OVERHEAD_TOKENS = 200
TOOL_ROUND_RESERVE = 4000
FALLBACK_HISTORY_LIMIT = 20


def _is_cjk(c: str) -> bool:
    cp = ord(c)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
    )


def _estimate_tokens(text: str) -> int:
    """Conservative token count estimate for context budget."""
    if not text:
        return 0
    cjk = sum(1 for c in text if _is_cjk(c))
    other = len(text) - cjk
    return max(1, int(cjk * 1.5 + other * 0.4))


def _estimate_message_tokens(msg: dict) -> int:
    content = msg.get("content", "")
    if isinstance(content, str):
        return _estimate_tokens(content) + 4
    parts_text: list[str] = []
    image_count = 0
    for part in content:
        if isinstance(part, dict):
            t = part.get("type", "")
            if t == "text":
                parts_text.append(part.get("text", ""))
            elif t == "tool_use":
                parts_text.append(json.dumps(part.get("input", {})))
            elif t == "tool_result":
                rc = part.get("content", "")
                parts_text.append(rc if isinstance(rc, str) else json.dumps(rc))
            elif t in ("image", "image_url"):
                image_count += 1
    tokens = _estimate_tokens(" ".join(parts_text)) + 4
    tokens += image_count * 1000
    return tokens


def _estimate_tool_spec_tokens(tools: list[ToolSpec]) -> int:
    total = 0
    for t in tools:
        text = t.name + " " + t.description + " " + json.dumps(t.input_schema)
        total += _estimate_tokens(text) + 10
    return total

CLEAR_KEYWORDS = {"清空对话", "新对话", "重置对话", "清除历史"}
GREETING_KEYWORDS = {"你好", "hi", "hello", "hey", "嗨", "在吗", "在不在", "你在吗"}
GREETING_REPLY = (
    "在的 👋[下一条]"
    "想定点啥提醒？直接跟我说就行，比如：\n"
    "· 每天 9 点提醒我喝水\n"
    "· 每个工作日 18 点提醒我写日报\n"
    "· 明天 8 点提醒我带身份证[下一条]"
    "想看现在有哪些任务，发「我有哪些任务」就行～"
)
UPDATE_KEYWORDS = {"更新叮咚", "升级叮咚", "立即更新", "开始更新"}
UPDATE_STATUS_KEYWORDS = {"更新状态", "查看更新状态"}
UPDATE_CONFIRM_KEYWORDS = {"确认更新", "确认升级"}
CHECK_UPDATE_KEYWORDS = {"检查更新", "检查版本", "版本", "当前版本"}
DELETE_ALL_CONFIRM_KEYWORDS = {"确认删除全部", "确认删除所有任务", "确认全部删除"}
DELETE_ALL_CANCEL_KEYWORDS = {"取消", "取消删除", "不用", "不删了", "no", "n"}

# 三项长期偏好的系统默认值（占位）：用户未设定时用这些，用户设定后用用户的。
DEFAULT_BOT_NAME = "叮咚"
DEFAULT_PERSONA = "像微信里的朋友：自然、口语、简短，别像机器人念说明书。"
# user_title 默认无（空）——系统不预设你该怎么称呼用户。

INTENT_SYSTEM_PROMPT = """\
表达：
- 默认简短，能一句说清就一句，不废话。
- 需要多说几句时（介绍自己、解释你能做啥、给建议），拆成几条短消息，用单独一行 [下一条] 分隔，别堆成一大段。
- 例外：搜索/资料类结果要详细完整，列出要点和来源，整条发出，不要用 [下一条] 拆条。可用 read_url 获取页面详情后再总结。

用工具管理任务。有 search_web 时可搜索。

严格规则：
- 用户要你提醒、定时或到点做某事，必须真的调用 create_job 把任务建出来。只回"好的""已设置"之类却没调用工具，是严重错误，绝对禁止。
- 一条消息里说了多个任务（多个时间点、多件事、或用顿号/分号/换行列出），要给每个任务各调一次 create_job，在同一条回复里一次性全部建好，别只建第一个。
- 时间或关键信息不全、没法建任务时，简短追问一句补全，别用"好的"敷衍带过。
- 用户说"删除"就只调 delete_job，不要先 list 再删，直接按名称或 id 删
- 用户要删多个任务，用 job_ids 数组一次删完，或用 job_id="all" 全删
- 用户问任务列表，只调 list_jobs，不要创建任何任务
- 不要自作主张创建用户没要求的任务
- 创建/修改/删除任务，确认一句话就够
- 展示任务列表时必须完整显示每个任务的全部信息（名称、目标、计划、状态、下次触发），不要省略任何任务或字段
- list_jobs 只返回正在生效的任务；一次性(date)任务执行完后不在其中。要核对某个一次性提醒是否已触发过，用 list_done_jobs 查最近完成记录，或看对话历史里「定时任务…已于…触发」的记录。已执行过就别说没找到或提议重建

记账（趣味优先，可靠次之，实用垫底）：
- 用户说买了啥 / 花了多少钱，必须调 add_expense 真的记下来；和建任务一样严格，别只嘴上说"记好了"却没调用。一条消息说了多笔就多次调用，各记一笔。
- 金额默认单位元。只有用户指明了别的时间（昨天、中午…）才传 spent_at，否则记现在。category 自己归类（餐饮/交通/购物/娱乐/日用/医疗/其他等）。
- 记完别干巴巴回"已记录"：用一句打趣 / 调侃 / 假装责怪的话回应，顺手把记下的金额和东西复述一遍，方便用户发现记错。可以拿 add_expense 返回的 today_total / today_count 抖机灵（如"今天第 3 杯奶茶了"）。
- add_expense 返回 large=true（大额）时，追加一句"拷问"——这钱花得值不值之类，增加戏剧性；但记录已经存下了，别因为要拷问就不记、也别要求用户再确认。
- 查明细用 list_expenses；要日/周/月总结或分析用 summarize_expenses，基于它返回的数字写一段带吐槽的回顾（占比、最能花的那笔、哪天花得最猛），数字一律以工具返回为准、绝不编造。
- 记错了用 update_expense 改、delete_expense 删。
- 吐槽和打趣的火力跟随你的人设（persona），默认机灵、损得友善，别刻薄、别说教。

长期偏好（称呼与风格）：当用户表达"想怎么称呼你 / 给你起个名"、"希望你怎么称呼TA"、"希望你是什么性格/风格/语气"时，调用 set_profile 记住。只传发生变化的项（会整项覆盖），其余不传保持不变；要恢复默认就把该项设为空字符串。除非用户提起，别主动反复追问这些。

用户问你能做什么，可以热情点、分几条说（用 [下一条]）：你能帮他定各种定时提醒和任务，到点用微信戳他；也能帮他记账——说一句"买了啥、花了多少"就记下，还能出带吐槽的日/周/月账单；顺带提一句发「我有哪些任务」看列表、「清空对话」重置记录。

任务字段：name(名称) goal(目标描述) schedule_kind(cron/interval/date)
- cron: cron_expression 5字段
- interval: seconds/minutes/hours/days/weeks
- date: run_at "YYYY-MM-DD HH:MM:SS"

相对时间转绝对值，本地时区。
"""

# 回复分条标记：模型用它把一段回复拆成多条短消息，发送端按此拆开逐条发出。
BUBBLE_SEP = "[下一条]"


def split_bubbles(reply: str) -> list[str]:
    """把一段回复按 BUBBLE_SEP 拆成多条短消息（去空、去首尾空白）。"""
    if not reply:
        return []
    return [p for p in (s.strip() for s in reply.split(BUBBLE_SEP)) if p]


# 一条消息里出现 ≥2 个时间点（"9点""12:30""18时"）时，粗判为多任务。
_TIME_MENTION_RE = re.compile(r"\d+\s*[:：点時时]")


def _looks_multi_task(text: str) -> bool:
    """粗判用户是否在一条消息里塞了多个任务。

    仅用来决定单个 create_job/update_job 是否走快捷回复（省一次 LLM）：判错只影响快慢、
    不影响正确性——判多了最多多花一次 LLM 总结，判少了退回原来的行为。
    顺序型模型（一次只发一个工具调用，如部分国产模型）靠这个才能把多任务建全。
    """
    s = (text or "").strip()
    if not s:
        return False
    if "\n" in s or "；" in s or ";" in s:
        return True
    if s.count("提醒") >= 2:
        return True
    return len(_TIME_MENTION_RE.findall(s)) >= 2


def _job_to_brief(job: Job) -> dict[str, Any]:
    return {
        "id": job.id, "name": job.name, "goal": job.goal,
        "schedule_kind": job.schedule_kind, "schedule_value": job.schedule_value,
        "enabled": job.enabled, "last_run_at": job.last_run_at, "last_result": job.last_result,
    }


def _is_done_oneshot(job: Job) -> bool:
    """一次性任务是否已成功执行完：date 任务触发成功后被标记 enabled=0 且有 last_run_at。

    这类任务默认不出现在 list_jobs（正在生效的任务）里，只能用 list_done_jobs 核对。
    """
    return job.schedule_kind == "date" and not job.enabled and job.last_run_at is not None


def _merge_adjacent(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """合并相邻同角色消息。

    主动推送（executor 写入的"已触发"记录）会在两条 assistant 之间多插一条，
    形成连续同角色；部分 provider 不接受连续同角色消息，这里合并成一条。
    此处历史内容全是纯文本字符串，合并安全。
    """
    merged: list[dict[str, str]] = []
    for m in history:
        if merged and merged[-1].get("role") == m.get("role"):
            merged[-1]["content"] = f"{merged[-1]['content']}\n{m['content']}"
        else:
            merged.append(dict(m))
    return merged


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="get_current_time",
        description="返回服务器当前日期、时间和星期。用于计算相对时间。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="list_jobs",
        description="返回该用户【正在生效】的定时任务列表（不含已执行完的一次性任务，后者用 list_done_jobs 查）。"
                    "必须完整展示每个任务的全部字段：名称、目标、计划类型、计划详情、启用状态、下次触发时间。不得省略任何任务或字段。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="list_done_jobs",
        description="查询最近【已执行完成】的一次性任务（仅返回最近若干条，用于核对某个提醒是否已经触发过）。"
                    "不含正在生效的任务；日常任务列表请用 list_jobs。",
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
    ToolSpec(
        name="set_profile",
        description="记住用户的长期偏好（称呼与风格）。当用户表达想怎么称呼你/给你起名、希望你怎么称呼他、"
                    "或希望你是什么性格/风格/语气时调用。只传发生变化的字段，会整项覆盖；其余不传保持不变。"
                    "用户要求恢复默认时，把对应字段传空字符串。",
        input_schema={
            "type": "object",
            "properties": {
                "bot_name": {"type": "string", "description": "用户希望怎么称呼你/给你起的名字"},
                "user_title": {"type": "string", "description": "你应当怎么称呼用户"},
                "persona": {"type": "string", "description": "用户希望你的性格/风格/语气"},
            },
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

EXPENSE_TOOL_SPECS = [
    ToolSpec(
        name="add_expense",
        description="记一笔开销。用户说买了什么/花了多少钱时调用，真的把它记下来。"
                    "amount 单位元；一条消息说了多笔就多次调用，各记一笔。"
                    "category 自己归类（如 餐饮/交通/购物/娱乐/日用/医疗/其他）。"
                    "spent_at 仅当用户指明了别的时间（如昨天、中午）才传，格式 'YYYY-MM-DD HH:MM:SS'，否则不传=现在。",
        input_schema={
            "type": "object", "required": ["amount", "item"],
            "properties": {
                "amount": {"type": "number", "description": "金额（元）"},
                "item": {"type": "string", "description": "买了啥/花在哪"},
                "category": {"type": "string", "description": "分类"},
                "note": {"type": "string"},
                "spent_at": {"type": "string", "description": "YYYY-MM-DD HH:MM:SS"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="list_expenses",
        description="查询开销明细。period 取 today/yesterday/week/last_week/month/last_month，"
                    "或用 since/until 指定范围（YYYY-MM-DD）；可按 category 过滤。",
        input_schema={
            "type": "object",
            "properties": {
                "period": {"type": "string",
                           "enum": ["today", "yesterday", "week", "last_week", "month", "last_month"]},
                "since": {"type": "string", "description": "YYYY-MM-DD"},
                "until": {"type": "string", "description": "YYYY-MM-DD"},
                "category": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="summarize_expenses",
        description="汇总某段时间的开销，返回总额、按分类金额、最大几笔、每日分布等数字，用于生成日/周/月总结。"
                    "period 取 today/yesterday/week/last_week/month/last_month。数字以返回为准，不要自己编。",
        input_schema={
            "type": "object", "required": ["period"],
            "properties": {"period": {"type": "string",
                                      "enum": ["today", "yesterday", "week", "last_week", "month", "last_month"]}},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="update_expense",
        description="修改某笔开销（记错时）。expense_id 支持完整 id 或前缀。只传需要改的字段。",
        input_schema={
            "type": "object", "required": ["expense_id"],
            "properties": {
                "expense_id": {"type": "string", "description": "开销 id 或前缀"},
                "amount": {"type": "number"},
                "item": {"type": "string"},
                "category": {"type": "string"},
                "note": {"type": "string"},
                "spent_at": {"type": "string", "description": "YYYY-MM-DD HH:MM:SS"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="delete_expense",
        description="删除某笔开销。expense_id 支持完整 id 或前缀。",
        input_schema={
            "type": "object", "required": ["expense_id"],
            "properties": {"expense_id": {"type": "string", "description": "开销 id 或前缀"}},
            "additionalProperties": False,
        },
    ),
]


def _tool_status(name: str, args: dict[str, Any]) -> str | None:
    if name == "search_web":
        return f"搜索「{args.get('query', '')}」..."
    if name == "create_job":
        return f"创建「{args.get('name', '任务')}」..."
    if name == "add_expense":
        return f"记一笔「{args.get('item', '')}」..."
    if name == "summarize_expenses":
        return "算账中..."
    return None


CONFIRM_KEYWORDS = {"确认", "确定", "是", "yes", "y"}
CANCEL_KEYWORDS = {"取消", "取消更新", "不用", "不更新", "先不更新", "no", "n"}


class IntentRouter:
    def __init__(self, llm: LLMProvider, store: JobStore, scheduler: Scheduler, *,
                 account_id: str = "",
                 is_admin: bool = True,
                 history_limit: int = 200, model_info: "ModelInfo | None" = None,
                 vision_llm: LLMProvider | None = None,
                 context_length: int = 0,
                 wechat_update_enabled: bool = False,
                 watchtower_url: str = "http://watchtower:8080/v1/update",
                 watchtower_token: str = "") -> None:
        from .models import ModelInfo
        self._llm = llm
        self._vision_llm = vision_llm
        self._store = store
        self._scheduler = scheduler
        self._account_id = account_id
        self._is_admin = is_admin
        self._history_limit = history_limit
        self._context_length = context_length
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
        # 是否首次对话（用于初次顺带问一下称呼，之后静默）。须在写入本条消息前判断。
        is_first = not self._store.get_history(
            owner_user_id, limit=1, account_id=self._account_id or None)

        if not has_images:
            if owner_user_id in self._pending_update:
                if stripped in UPDATE_CONFIRM_KEYWORDS:
                    latest = self._pending_update.pop(owner_user_id)
                    return self._start_update(owner_user_id, context_token, latest)
                if stripped in CANCEL_KEYWORDS or stripped.lower() in CANCEL_KEYWORDS:
                    self._pending_update.pop(owner_user_id, None)
                    return "好，先不更新。"
                if stripped.lower() in CONFIRM_KEYWORDS:
                    return "稳一点哈，回「确认更新」我就开始，回「取消更新」就先放着。"
                if stripped not in CHECK_UPDATE_KEYWORDS and stripped not in UPDATE_KEYWORDS and stripped not in UPDATE_STATUS_KEYWORDS:
                    self._pending_update.pop(owner_user_id, None)
            if owner_user_id in self._pending_delete_all:
                self._pending_delete_all.discard(owner_user_id)
                if stripped in DELETE_ALL_CONFIRM_KEYWORDS:
                    result = self._tool_delete_job({"job_id": "all", "confirm": "yes"}, owner_user_id)
                    return _quick_reply("delete_job", result) or "好，所有任务都清掉了。"
                if stripped in DELETE_ALL_CANCEL_KEYWORDS or stripped.lower() in DELETE_ALL_CANCEL_KEYWORDS:
                    return "好，任务都留着，没删。"
                return "好，任务都留着，没删。"
            if owner_user_id in self._pending_clear:
                self._pending_clear.discard(owner_user_id)
                if stripped.lower() in CONFIRM_KEYWORDS:
                    n = self._store.clear_history(owner_user_id, account_id=self._account_id or None)
                    return f"清好啦，删了 {n} 条记录，咱们重新开始～"
                return "好，那就不动它。"
            if stripped in CLEAR_KEYWORDS:
                self._pending_clear.add(owner_user_id)
                return "要把咱俩的聊天记录都清掉吗？回个「确认」我就清。"
            if stripped.lower() in GREETING_KEYWORDS:
                reply = GREETING_REPLY
                if is_first:
                    prefs = self._store.get_prefs(owner_user_id, account_id=self._account_id)
                    if not prefs["bot_name"] and not prefs["user_title"]:
                        reply += BUBBLE_SEP + "对了，想让我怎么称呼你？也可以给我起个名字～"
                self._store.append_message(owner_user_id, "user", text, account_id=self._account_id)
                self._store.append_message(owner_user_id, "assistant", reply, account_id=self._account_id)
                return reply
            if stripped == "关闭更新提醒":
                if not self._is_admin:
                    return "这个操作只有管理员能用哦。"
                set_disabled(self._store.db_path.parent, True)
                return "已关闭更新提醒。发「开启更新提醒」可恢复。"
            if stripped == "开启更新提醒":
                if not self._is_admin:
                    return "这个操作只有管理员能用哦。"
                set_disabled(self._store.db_path.parent, False)
                return "已开启更新提醒。"
            if stripped in CHECK_UPDATE_KEYWORDS:
                if not self._is_admin:
                    return "这个操作只有管理员能用哦。"
                return self._check_update(owner_user_id)
            if stripped in UPDATE_CONFIRM_KEYWORDS:
                if not self._is_admin:
                    return "这个操作只有管理员能用哦。"
                return self._confirm_update(owner_user_id, context_token)
            if stripped in UPDATE_STATUS_KEYWORDS:
                if not self._is_admin:
                    return "这个操作只有管理员能用哦。"
                return self._update_status()
            if stripped in UPDATE_KEYWORDS:
                if not self._is_admin:
                    return "这个操作只有管理员能用哦。"
                return self._prepare_update(owner_user_id)

            shortcut = self._try_shortcut(stripped, owner_user_id)
            if shortcut is not None:
                self._store.append_message(owner_user_id, "user", text, account_id=self._account_id)
                self._store.append_message(owner_user_id, "assistant", shortcut, account_id=self._account_id)
                return shortcut

        if has_images and not self._vision_supported:
            return ("当前配置的模型不支持图片理解。\n"
                    "方案一：主模型换为 Claude Sonnet、GPT-4o 等视觉模型\n"
                    "方案二：在 .env 中单独配置视觉模型（VISION_PROVIDER / VISION_MODEL / VISION_API_KEY）")

        use_direct_vision = has_images and not self._vision_llm
        if has_images and self._vision_llm:
            image_desc = self._describe_images(image_bytes_list, text)
            user_text = f"{text}\n\n[图片内容：{image_desc}]" if text else f"[图片内容：{image_desc}]"
            self._store.append_message(owner_user_id, "user", user_text, account_id=self._account_id)
        else:
            self._store.append_message(owner_user_id, "user", text or "[图片]", account_id=self._account_id)

        messages: list[dict[str, Any]] = list(self._build_context(owner_user_id))

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
        wants_multi = _looks_multi_task(text)
        for round_idx in range(MAX_TOOL_ROUNDS):
            tools = list(TOOL_SPECS) + list(EXPENSE_TOOL_SPECS)
            if search_available():
                tools.extend(SEARCH_TOOL_SPECS)
            resp = self._llm.chat(
                system=self._system_prompt(owner_user_id, is_first),
                messages=messages,
                tools=tools,
                max_tokens=RESPONSE_TOKEN_BUDGET,
            )
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
            if resp.reasoning_content is not None:
                assistant_msg["reasoning_content"] = resp.reasoning_content
            messages.append(assistant_msg)

            tool_uses = resp.tool_uses()
            if not tool_uses:
                reply = resp.text() or "好的。"
                self._store.append_message(owner_user_id, "assistant", reply, account_id=self._account_id)
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
            # 顺序型模型一次只建一个；若像多任务，跳过短路让它把剩下的接着建完。
            if (not needs_llm_summary and len(tool_uses) == 1
                    and tool_uses[0]["name"] in QUICK_REPLY_TOOLS and not wants_multi):
                quick = _quick_reply(tool_uses[0]["name"], tool_results[0]["content"])
                if quick:
                    self._store.append_message(owner_user_id, "assistant", quick, account_id=self._account_id)
                    return quick

            messages.append({"role": "user", "content": tool_results})

        fallback = "这条有点绕，我没一次搞定，能拆开分两次说说吗？"
        self._store.append_message(owner_user_id, "assistant", fallback, account_id=self._account_id)
        return fallback

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self._scheduler.tz)

    def _build_context(self, owner_user_id: str) -> list[dict[str, str]]:
        """Build conversation history within token budget."""
        if not self._context_length:
            return _merge_adjacent(self._store.get_history(
                owner_user_id, limit=FALLBACK_HISTORY_LIMIT,
                account_id=self._account_id or None))

        history = self._store.get_history(owner_user_id, limit=self._history_limit,
                                          account_id=self._account_id or None)
        if not history:
            return history

        tools = list(TOOL_SPECS) + list(EXPENSE_TOOL_SPECS)
        if search_available():
            tools.extend(SEARCH_TOOL_SPECS)

        system_tokens = _estimate_tokens(self._system_prompt(owner_user_id))
        tool_tokens = _estimate_tool_spec_tokens(tools) + TOOL_OVERHEAD_TOKENS
        overhead = system_tokens + tool_tokens + RESPONSE_TOKEN_BUDGET + TOOL_ROUND_RESERVE
        available = max(0, self._context_length - overhead)

        costs = [_estimate_message_tokens(m) for m in history]
        total = sum(costs)
        idx = 0
        while idx < len(history) and total > available:
            total -= costs[idx]
            idx += 1
        history = history[idx:]

        while history and history[0].get("role") != "user":
            history = history[1:]

        return _merge_adjacent(history)

    def _system_prompt(self, owner_user_id: str, is_first: bool = False) -> str:
        prefs = self._store.get_prefs(owner_user_id, account_id=self._account_id)
        bot_name = prefs["bot_name"] or DEFAULT_BOT_NAME
        persona = prefs["persona"] or DEFAULT_PERSONA
        user_title = prefs["user_title"]

        header = [f"你是{bot_name}，微信定时任务助手。", f"性格与风格：{persona}"]
        if user_title:
            header.append(f"称呼用户时用「{user_title}」。")
        if is_first and not prefs["bot_name"] and not prefs["user_title"]:
            header.append(
                "这看起来是你们第一次聊：可以自然地顺带问一句对方想怎么称呼你、希望你怎么称呼 TA。"
                "问过一次就好，之后别再追问。"
            )

        now = datetime.now(self._tz())
        weekday = "星期" + "一二三四五六日"[now.weekday()]
        return ("\n".join(header) + "\n\n" + INTENT_SYSTEM_PROMPT
                + f"\n当前版本：v{local_version()}\n当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')} {weekday}\n")

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
            lines.append("稳一点哈，回「确认更新」我就开始，回「取消更新」就先放着。")
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
        return f"将从 v{lv} 更新到 v{rv}，期间会短暂重启。\n稳一点哈，回「确认更新」我就开始，回「取消更新」就先放着。"

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
            if name == "list_done_jobs":
                return self._tool_list_done_jobs(owner_user_id)
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
            if name == "set_profile":
                return self._tool_set_profile(args, owner_user_id)
            if name == "add_expense":
                return self._tool_add_expense(args, owner_user_id, context_token)
            if name == "list_expenses":
                return self._tool_list_expenses(args, owner_user_id)
            if name == "summarize_expenses":
                return self._tool_summarize_expenses(args, owner_user_id)
            if name == "update_expense":
                return self._tool_update_expense(args, owner_user_id)
            if name == "delete_expense":
                return self._tool_delete_expense(args, owner_user_id)
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
        jobs = self._store.list_jobs(owner_user_id=owner_user_id, account_id=self._account_id or None)
        active = [j for j in jobs if not _is_done_oneshot(j)]
        done_count = len(jobs) - len(active)
        if not active:
            note = "还没有任务呢，试试发「每天 9 点提醒我喝水」～"
            if done_count:
                note = f"当前没有正在生效的任务（另有 {done_count} 条已执行完的一次性任务，可让我帮你核对）。"
            return _ok({"jobs": []}, note=note)
        briefs = []
        for j in active:
            b = _job_to_brief(j)
            nr = self._scheduler.get_next_run(j.id)
            b["next_run_at"] = nr.isoformat() if nr else None
            briefs.append(b)
        payload: dict[str, Any] = {"jobs": briefs}
        if done_count:
            payload["done_count"] = done_count
        return _ok(payload)

    def _tool_list_done_jobs(self, owner_user_id: str) -> str:
        jobs = self._store.list_jobs(owner_user_id=owner_user_id, account_id=self._account_id or None)
        done = [j for j in jobs if _is_done_oneshot(j)]
        done.sort(key=lambda j: j.last_run_at or 0, reverse=True)
        if not done:
            return _ok({"done_jobs": []}, note="最近没有已执行完成的一次性任务。")
        briefs = [{
            "id": j.id, "name": j.name, "goal": j.goal,
            "last_run_at": j.last_run_at, "last_result": j.last_result,
        } for j in done]
        return _ok({"done_jobs": briefs})

    def _tool_create_job(self, args: dict[str, Any], owner_user_id: str, context_token: str) -> str:
        kind = args.get("schedule_kind")
        if kind not in VALID_SCHEDULE_KINDS:
            return _err(f"schedule_kind 须为 {sorted(VALID_SCHEDULE_KINDS)}")
        schedule_value = _build_schedule_value(kind, args)
        job = Job(
            id=new_job_id(), name=str(args["name"]).strip(), goal=str(args["goal"]).strip(),
            schedule_kind=kind, schedule_value=schedule_value,
            owner_user_id=owner_user_id, context_token=context_token,
            account_id=self._account_id, enabled=True,
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
            return _err("没找到这个任务诶，发「我有哪些任务」看看？")
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
            jobs = self._store.list_jobs(owner_user_id=owner_user_id, account_id=self._account_id or None)
            if not jobs:
                return _ok({"deleted_count": 0}, note="没有任务可删")
            self._pending_delete_all.add(owner_user_id)
            return _err(f"将删除全部 {len(jobs)} 个任务。请回复「确认删除全部」执行，或回复「取消」放弃。")
        if single:
            ids = [single] if single != "all" else []

        if single == "all" or not ids:
            jobs = self._store.list_jobs(owner_user_id=owner_user_id, account_id=self._account_id or None)
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
            return _err("没找到这个任务诶，发「我有哪些任务」看看？")
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
            return _err("没找到这个任务诶，发「我有哪些任务」看看？")
        self._scheduler.trigger_now(job)
        return _ok({"queued_id": job.id, "queued_name": job.name})

    def _tool_set_profile(self, args: dict[str, Any], owner_user_id: str) -> str:
        fields = {k: args[k] for k in ("bot_name", "user_title", "persona") if k in args}
        if not fields:
            return _err("没有要更新的偏好")
        prefs = self._store.set_prefs(owner_user_id, account_id=self._account_id, **fields)
        return _ok({"profile": prefs, "changed": list(fields.keys())})

    # ---------- expenses (记账) ----------

    def _tool_add_expense(self, args: dict[str, Any], owner_user_id: str, context_token: str) -> str:
        amount_cents = _yuan_to_cents(args.get("amount"))
        item = str(args.get("item", "")).strip()
        if not item:
            return _err("买了啥 / 花在哪不能为空")
        tz = self._tz()
        e = Expense(
            id=new_expense_id(), owner_user_id=owner_user_id, amount_cents=amount_cents,
            item=item, category=str(args.get("category", "")).strip(),
            note=str(args.get("note", "")).strip(), context_token=context_token,
            account_id=self._account_id, spent_at=_parse_when(args.get("spent_at"), tz),
        )
        self._store.insert_expense(e)
        since, until, _ = _period_range("today", tz)
        today = self._store.list_expenses(owner_user_id=owner_user_id, account_id=self._account_id or None,
                                          since=since, until=until)
        today_total = sum(r.amount_cents for r in today)
        return _ok({
            "recorded": _expense_to_brief(e, tz),
            "large": amount_cents >= LARGE_EXPENSE_CENTS,
            "today_total": _fmt_yuan(today_total),
            "today_count": len(today),
        })

    def _tool_list_expenses(self, args: dict[str, Any], owner_user_id: str) -> str:
        tz = self._tz()
        cat = (args.get("category") or "").strip() or None
        period = args.get("period")
        if period:
            since, until, _ = _period_range(period, tz)
        else:
            since = _parse_day_start(args.get("since"), tz)
            until = _parse_day_end(args.get("until"), tz)
        rows = self._store.list_expenses(owner_user_id=owner_user_id, account_id=self._account_id or None,
                                         since=since, until=until, category=cat, limit=100)
        if not rows:
            return _ok({"expenses": []}, note="这段时间还没有记账记录。")
        total = sum(e.amount_cents for e in rows)
        return _ok({"expenses": [_expense_to_brief(e, tz) for e in rows],
                    "count": len(rows), "total": _fmt_yuan(total)})

    def _tool_summarize_expenses(self, args: dict[str, Any], owner_user_id: str) -> str:
        tz = self._tz()
        try:
            since, until, label = _period_range(args.get("period", ""), tz)
        except ValueError as exc:
            return _err(str(exc))
        rows = self._store.list_expenses(owner_user_id=owner_user_id, account_id=self._account_id or None,
                                         since=since, until=until)
        if not rows:
            return _ok({"period": label, "count": 0}, note=f"{label}还没有开销记录，钱包很安全。")
        agg = _aggregate_expenses(rows, tz)
        agg["period"] = label
        return _ok(agg)

    def _tool_update_expense(self, args: dict[str, Any], owner_user_id: str) -> str:
        e = self._resolve_expense(args.get("expense_id", ""), owner_user_id)
        if e is None:
            return _err("没找到这笔记录")
        fields: dict[str, Any] = {}
        if "amount" in args:
            fields["amount_cents"] = _yuan_to_cents(args["amount"])
        for k in ("item", "category", "note"):
            if k in args:
                fields[k] = str(args[k]).strip()
        if "spent_at" in args:
            fields["spent_at"] = _parse_when(args["spent_at"], self._tz())
        if not fields:
            return _err("没有要修改的字段")
        updated = self._store.update_expense_fields(e.id, **fields)
        if updated is None:
            return _err("修改失败")
        return _ok({"updated": _expense_to_brief(updated, self._tz())})

    def _tool_delete_expense(self, args: dict[str, Any], owner_user_id: str) -> str:
        e = self._resolve_expense(args.get("expense_id", ""), owner_user_id)
        if e is None:
            return _err("没找到这笔记录")
        self._store.delete_expense(e.id)
        return _ok({"deleted": _expense_to_brief(e, self._tz())})

    def _resolve_expense(self, raw_id: str, owner_user_id: str) -> Expense | None:
        raw_id = (raw_id or "").strip()
        if not raw_id:
            return None
        aid = self._account_id or None
        e = self._store.get_expense(raw_id)
        if e is None:
            e = self._store.get_expense_by_prefix(raw_id, account_id=aid)
        if e is None or e.owner_user_id != owner_user_id:
            return None
        if self._account_id and e.account_id != self._account_id:
            return None
        return e

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
        aid = self._account_id or None
        job = self._store.get(raw_id)
        if job is None:
            job = self._store.get_by_prefix(raw_id, account_id=aid)
        if job is None:
            job = self._store.find_by_name(raw_id, owner_user_id=owner_user_id, account_id=aid)
        if job is None or job.owner_user_id != owner_user_id:
            return None
        if self._account_id and job.account_id != self._account_id:
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
        return f"搞定，已设好「{c.get('name', '')}」，{r.get('schedule_human', '')}，下次 {_fmt_time(r.get('next_run_at'))} 提醒你。"

    if tool_name == "update_job":
        u = r.get("updated", {})
        return f"好的，「{u.get('name', '')}」改好了，{r.get('schedule_human', '')}。"

    if tool_name == "delete_job":
        names = r.get("deleted_names", [])
        count = r.get("deleted_count", len(names))
        if count == 1 and names:
            return f"好，「{names[0]}」删掉了。"
        return f"好，删掉了 {count} 个任务。"

    if tool_name == "set_enabled":
        status = "启用" if r.get("enabled") else "暂停"
        return f"好，已{status}。"

    if tool_name == "run_now":
        return f"好，「{r.get('queued_name', '')}」这就跑一次。"

    if tool_name == "list_jobs" and not r.get("jobs"):
        return r.get("note") or "还没有任务呢，试试发「每天 9 点提醒我喝水」～"

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
