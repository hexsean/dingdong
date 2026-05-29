"""记账领域的纯函数与常量。

金额换算、时间区间、聚合等逻辑集中在这里，供 :mod:`intent`（交互层）与
:mod:`expense_reporter`（主动推送）共用。只依赖标准库 + :class:`storage.Expense`，
不引入任何重依赖，方便单测。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .storage import Expense

# 单笔金额 ≥ ¥300 触发"拷问"互动（趣味用，内置默认值）
LARGE_EXPENSE_CENTS = 300_00
# 当天累计 ≥ ¥500 时，当晚主动推送一条提醒
DAILY_NUDGE_CENTS = 500_00


def yuan_to_cents(amount: Any) -> int:
    try:
        cents = round(float(amount) * 100)
    except (TypeError, ValueError):
        raise ValueError("金额无法识别")
    if cents <= 0:
        raise ValueError("金额需大于 0")
    return int(cents)


def fmt_yuan(cents: int) -> str:
    if cents % 100 == 0:
        return f"¥{cents // 100}"
    return f"¥{cents / 100:.2f}"


def parse_when(s: Any, tz: ZoneInfo) -> int:
    """解析 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD'，返回 epoch 秒；空=现在。"""
    text = s.strip() if isinstance(s, str) else ""
    if not text:
        return int(datetime.now(tz).timestamp())
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"时间格式无法识别: {text}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def parse_day_start(s: Any, tz: ZoneInfo) -> int | None:
    text = s.strip() if isinstance(s, str) else ""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"日期格式无法识别: {text}")
    dt = dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    return int(dt.timestamp())


def parse_day_end(s: Any, tz: ZoneInfo) -> int | None:
    start = parse_day_start(s, tz)
    return None if start is None else start + 86400  # 次日 0 点，作为 exclusive 上界


def period_range(period: str, tz: ZoneInfo) -> tuple[int, int, str]:
    """返回 (since_ts, until_ts, 中文标签)。until 为 exclusive 上界。

    覆盖 today/yesterday/week/last_week/month/last_month。"week"=本周一至今，
    用于周末出"本周"小结；月报在 1 号触发，统计的是 last_month（上个月整月）。
    """
    now = datetime.now(tz)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    until = now + timedelta(seconds=1)  # 默认上界=现在
    this_monday = start_today - timedelta(days=now.weekday())
    first_this_month = start_today.replace(day=1)

    if period == "today":
        since, label = start_today, "今天"
    elif period == "yesterday":
        since, until, label = start_today - timedelta(days=1), start_today, "昨天"
    elif period == "week":
        since, label = this_monday, "本周"
    elif period == "last_week":
        since, until, label = this_monday - timedelta(days=7), this_monday, "上周"
    elif period == "month":
        since, label = first_this_month, "本月"
    elif period == "last_month":
        since = (first_this_month - timedelta(days=1)).replace(day=1)
        until, label = first_this_month, "上个月"
    else:
        raise ValueError("period 须为 today/yesterday/week/last_week/month/last_month")
    return int(since.timestamp()), int(until.timestamp()), label


def expense_to_brief(e: Expense, tz: ZoneInfo) -> dict[str, Any]:
    dt = datetime.fromtimestamp(e.spent_at, tz)
    return {
        "id": e.id[:8], "amount": fmt_yuan(e.amount_cents), "item": e.item,
        "category": e.category, "note": e.note, "spent_at": dt.strftime("%Y-%m-%d %H:%M"),
    }


def aggregate_expenses(expenses: list[Expense], tz: ZoneInfo) -> dict[str, Any]:
    total = sum(e.amount_cents for e in expenses)
    by_cat: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for e in expenses:
        cat = e.category or "其他"
        by_cat[cat] = by_cat.get(cat, 0) + e.amount_cents
        day = datetime.fromtimestamp(e.spent_at, tz).strftime("%m-%d")
        by_day[day] = by_day.get(day, 0) + e.amount_cents
    top = sorted(expenses, key=lambda e: e.amount_cents, reverse=True)[:5]
    return {
        "total": fmt_yuan(total),
        "count": len(expenses),
        "by_category": [{"category": c, "amount": fmt_yuan(v)}
                        for c, v in sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)],
        "top_items": [{"item": e.item, "amount": fmt_yuan(e.amount_cents), "category": e.category}
                      for e in top],
        "by_day": [{"day": d, "amount": fmt_yuan(v)} for d, v in sorted(by_day.items())],
    }
