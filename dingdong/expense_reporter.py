"""记账主动推送：日/周/月总结与当天高消费提醒。

由 :mod:`bot`（单账号）和 :mod:`server`（多账号）在启动时通过 :func:`register_reports`
把三个 cron 任务挂到 APScheduler，到点对每个有开销记录的用户生成一段带吐槽的小结并推送。

发送动作通过 ``send`` 回调注入（单账号=ILinkClient.safe_send_text，多账号=AccountRunner.send_to_user），
因此本模块不依赖 ilink / apscheduler，可独立单测。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .expense import DAILY_NUDGE_CENTS, aggregate_expenses, fmt_yuan, period_range
from .llm import LLMProvider
from .storage import DEFAULT_BOT_NAME, DEFAULT_PERSONA, SYSTEM_EVENT_PREFIX, JobStore

log = logging.getLogger(__name__)

# 三个推送的触发时间（cron）。集中在此，bot 与 server 共用。
DAILY_HOUR = 21              # 每天 21:00 检查当天，高消费才提醒
WEEKLY_DOW, WEEKLY_HOUR = "sun", 20   # 周日 20:00 出本周小结
MONTHLY_DAY, MONTHLY_HOUR = 1, 20     # 每月 1 号 20:00 出上个月小结

# 语气/风格交给 persona（用户设了用用户的，否则用默认的"损友"人设），这里只规定内容与结构。
REPORTER_SYSTEM = """\
你是{bot_name}，用户的记账搭子。根据给定的开销数据，写一段简短的回顾小结。
要求：中文、口语、像微信聊天；点出花得最多的类目、最大的一笔、值不值得；别长篇大论、别说教，控制在 3 句以内。
直接输出消息内容，不要任何前缀。
"""

# send(owner_user_id, text, context_token) -> bool
Sender = Callable[[str, str, str], bool]


class ExpenseReporter:
    """对单个账号（account_id）范围内的用户做一次记账总结推送。"""

    def __init__(self, llm: LLMProvider, store: JobStore, tz: str = "Asia/Shanghai",
                 *, account_id: str = "", send: Sender) -> None:
        self._llm = llm
        self._store = store
        self._tz = ZoneInfo(tz)
        self._account_id = account_id
        self._send = send

    # cron 入口（供单账号 bot 直接注册）
    def run_daily(self) -> None:
        self.run("today", nudge_only=True)

    def run_weekly(self) -> None:
        self.run("week")

    def run_monthly(self) -> None:
        self.run("last_month")

    def run(self, period: str, nudge_only: bool = False) -> None:
        try:
            since, until, label = period_range(period, self._tz)
        except ValueError:
            log.warning("expense report: bad period %r", period)
            return
        rows = self._store.list_expenses(account_id=self._account_id or None, since=since, until=until)
        if not rows:
            return
        by_owner: dict[str, list] = defaultdict(list)
        for e in rows:
            by_owner[e.owner_user_id].append(e)

        for owner, owner_rows in by_owner.items():
            total = sum(e.amount_cents for e in owner_rows)
            if nudge_only and total < DAILY_NUDGE_CENTS:
                continue
            ctx = max(owner_rows, key=lambda e: e.spent_at).context_token
            if not ctx:
                log.info("expense report: owner %s has no context_token; skip", owner)
                continue
            try:
                text = self._generate(owner, label, owner_rows)
            except Exception:
                log.exception("expense summary generation failed for %s", owner)
                continue
            if not text:
                continue
            ok = self._send(owner, text, ctx)
            log.info("expense %s report -> %s: %s", label, owner, "ok" if ok else "FAILED")
            if ok:
                try:
                    self._store.append_message(
                        owner, "assistant",
                        f"{SYSTEM_EVENT_PREFIX}已给用户推送了{label}开销小结（共 {fmt_yuan(total)}）",
                        account_id=self._account_id,
                    )
                except Exception:
                    log.exception("failed to record expense report to history for %s", owner)

    def _generate(self, owner: str, label: str, rows: list) -> str:
        agg = aggregate_expenses(rows, self._tz)
        prefs = self._store.get_prefs(owner, account_id=self._account_id)
        bot_name = (prefs.get("bot_name") or "").strip() or DEFAULT_BOT_NAME
        persona = (prefs.get("persona") or "").strip() or DEFAULT_PERSONA
        title = (prefs.get("user_title") or "").strip()
        system = REPORTER_SYSTEM.format(bot_name=bot_name) + f"\n语气/风格：{persona}"
        if title:
            system += f"\n称呼用户用「{title}」。"
        now = datetime.now(self._tz)
        user_prompt = (
            f"现在：{now:%Y-%m-%d %H:%M}\n"
            f"统计周期：{label}\n"
            f"总额：{agg['total']}，共 {agg['count']} 笔\n"
            f"分类金额：{json.dumps(agg['by_category'], ensure_ascii=False)}\n"
            f"最大的几笔：{json.dumps(agg['top_items'], ensure_ascii=False)}\n"
            f"每天花了多少：{json.dumps(agg['by_day'], ensure_ascii=False)}\n"
            f"各时段花了多少：{json.dumps(agg['by_slot'], ensure_ascii=False)}\n\n"
            f"给「{label}」写一段带点吐槽的开销小结。"
        )
        resp = self._llm.chat(
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=512,
        )
        return resp.text()


def register_reports(apscheduler, tz: ZoneInfo, daily: Callable, weekly: Callable,
                     monthly: Callable) -> None:
    """把日/周/月三个 cron 任务挂到 APScheduler。daily/weekly/monthly 为无参回调。

    apscheduler 在函数内 import，使本模块本身不依赖 apscheduler（便于单测）。
    """
    from apscheduler.triggers.cron import CronTrigger
    apscheduler.add_job(
        daily, CronTrigger(hour=DAILY_HOUR, minute=0, timezone=tz),
        id="expense_daily", replace_existing=True,
    )
    apscheduler.add_job(
        weekly, CronTrigger(day_of_week=WEEKLY_DOW, hour=WEEKLY_HOUR, minute=0, timezone=tz),
        id="expense_weekly", replace_existing=True,
    )
    apscheduler.add_job(
        monthly, CronTrigger(day=MONTHLY_DAY, hour=MONTHLY_HOUR, minute=0, timezone=tz),
        id="expense_monthly", replace_existing=True,
    )
