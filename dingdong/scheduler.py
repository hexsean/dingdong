"""APScheduler 包装层。

Job 的持久化在 :mod:`storage` 里完成；本模块只负责把"已在数据库里的 Job"
翻译成 APScheduler 触发器，并在触发时调用 ``executor``。

执行隔离：每个 job 走 thread pool；执行中再次触发同一 job 会被忽略
（``max_instances=1`` + ``coalesce=True``）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .storage import Job, JobStore

log = logging.getLogger(__name__)


class ScheduleSpecError(ValueError):
    pass


def build_trigger(kind: str, value: dict[str, Any], tz: str):
    if kind == "cron":
        expr = value.get("expression")
        if not expr or not isinstance(expr, str):
            raise ScheduleSpecError("cron schedule needs 'expression' (5-field crontab)")
        try:
            return CronTrigger.from_crontab(expr, timezone=tz)
        except ValueError as exc:
            raise ScheduleSpecError(f"invalid cron expression {expr!r}: {exc}") from exc

    if kind == "interval":
        kwargs: dict[str, Any] = {}
        for unit in ("seconds", "minutes", "hours", "days", "weeks"):
            if unit in value:
                kwargs[unit] = int(value[unit])
        if not kwargs:
            raise ScheduleSpecError("interval schedule needs at least one of seconds/minutes/hours/days/weeks")
        return IntervalTrigger(timezone=tz, **kwargs)

    if kind == "date":
        run_at = value.get("run_at")
        if not run_at:
            raise ScheduleSpecError("date schedule needs 'run_at' (YYYY-MM-DD HH:MM:SS)")
        try:
            when = datetime.fromisoformat(run_at)
        except ValueError as exc:
            raise ScheduleSpecError(f"invalid run_at {run_at!r}: {exc}") from exc
        return DateTrigger(run_date=when, timezone=tz)

    raise ScheduleSpecError(f"unknown schedule kind {kind!r}")


class Scheduler:
    def __init__(
        self,
        store: JobStore,
        runner: Callable[[Job], None],
        tz: str,
    ) -> None:
        self._store = store
        self._runner = runner
        self.tz = tz
        self._scheduler = BackgroundScheduler(
            timezone=tz,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        )

    def start(self) -> None:
        for job in self._store.list_jobs():
            if job.enabled:
                try:
                    self._register(job)
                except ScheduleSpecError as exc:
                    log.error("skipping job %s on startup: %s", job.id, exc)
        self._scheduler.start()
        log.info("scheduler started with %d job(s)", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    def sync_job(self, job: Job) -> None:
        """添加或刷新一个 Job 的调度。"""
        self._remove_silent(job.id)
        if job.enabled:
            self._register(job)

    def remove_job(self, job_id: str) -> None:
        self._remove_silent(job_id)

    def get_next_run(self, job_id: str) -> datetime | None:
        try:
            sj = self._scheduler.get_job(job_id)
        except JobLookupError:
            return None
        if sj is None:
            return None
        return sj.next_run_time

    def trigger_now(self, job: Job) -> None:
        """同步触发一次执行（用 scheduler 的 executor 调度，立即运行）。"""
        self._scheduler.add_job(
            self._dispatch,
            args=[job.id],
            id=f"{job.id}__manual_{int(datetime.now().timestamp() * 1000)}",
            misfire_grace_time=30,
        )

    # ---------- internal ----------

    def _remove_silent(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    def _register(self, job: Job) -> None:
        trigger = build_trigger(job.schedule_kind, job.schedule_value, self.tz)
        self._scheduler.add_job(
            self._dispatch,
            trigger=trigger,
            id=job.id,
            args=[job.id],
            replace_existing=True,
            name=job.name,
        )

    def _dispatch(self, job_id: str) -> None:
        """由 APScheduler 在 worker 线程里调用。"""
        job = self._store.get(job_id)
        if job is None:
            log.warning("scheduled job %s no longer exists; removing", job_id)
            self._remove_silent(job_id)
            return
        if not job.enabled:
            log.info("job %s disabled at fire time; skipping", job_id)
            return
        try:
            self._runner(job)
        except Exception as exc:
            log.exception("job %s runner crashed", job_id)
            self._store.record_run(job_id, f"crash: {exc}")
