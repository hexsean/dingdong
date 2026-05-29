"""任务执行器：把 ``Job.goal`` 翻译成消息内容并推送回原会话。

最小实现：调用 LLM 一次，让它按 goal 产出"这次该发什么"，
然后通过 ILinkClient.send_text 把结果发回 owner_user_id + context_token。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .ilink import ILinkClient
from .llm import LLMProvider
from .storage import Job, JobStore

log = logging.getLogger(__name__)


EXECUTOR_SYSTEM_PROMPT = """\
你是叮咚。根据任务目标生成要发给用户的微信消息。
直接输出消息内容，不要前缀。简洁，中文，注意当前时间。
"""

# 已执行完的一次性任务的留存上限（每个用户）：超出按 last_run_at 倒序裁掉，避免历史无限膨胀。
DONE_RETENTION = 20


class JobExecutor:
    def __init__(
        self,
        llm: LLMProvider,
        client: ILinkClient,
        store: JobStore,
        tz: str = "Asia/Shanghai",
    ) -> None:
        self._llm = llm
        self._client = client
        self._store = store
        self._tz = ZoneInfo(tz)

    def run(self, job: Job) -> None:
        log.info("executing job %s (%s)", job.id, job.name)
        try:
            content = self._generate(job)
        except Exception as exc:
            log.exception("LLM generation failed for job %s", job.id)
            content = f"[任务 {job.name} 生成失败] {exc}"
        ok = self._client.safe_send_text(
            to_user_id=job.owner_user_id,
            text=content,
            context_token=job.context_token,
        )
        status = "delivered" if ok else "send-failed"
        self._store.record_run(job.id, f"{status}: {content[:400]}")
        if ok and job.schedule_kind in ("date", "cron"):
            self._record_push(job, content)
        if job.schedule_kind == "date":
            if ok:
                self._store.update_fields(job.id, enabled=False)
                self._store.prune_done_jobs(job.owner_user_id, job.account_id, DONE_RETENTION)
                log.info("one-shot job %s (%s) marked done", job.id, job.name)
            else:
                log.error("one-shot job %s (%s) send failed; kept for manual retry", job.id, job.name)

    def _record_push(self, job: Job, content: str) -> None:
        """把主动推送写进对话历史，让意图层 LLM 知道这条提醒已经发过。

        否则一次性(date)任务触发成功后会被删库，LLM 在历史里找不到它，
        会误判成"没建成功"并提议重建。cron 同理记录；interval 跳过(可能高频刷历史)。
        """
        now = datetime.now(self._tz)
        marker = f"[定时任务「{job.name}」已于 {now:%m-%d %H:%M} 触发]"
        try:
            self._store.append_message(
                job.owner_user_id, "assistant", f"{marker}\n{content}",
                account_id=job.account_id,
            )
        except Exception:
            log.exception("failed to record push to history for job %s", job.id)

    def _generate(self, job: Job) -> str:
        now = datetime.now(self._tz)
        weekday = "星期" + "一二三四五六日"[now.weekday()]
        user_prompt = (
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')} {weekday}\n"
            f"任务名称：{job.name}\n"
            f"任务目标描述：{job.goal}\n\n"
            "请根据该目标，生成本次应该发给用户的微信消息内容。"
        )
        resp = self._llm.chat(
            system=EXECUTOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1024,
        )
        text = resp.text()
        if not text:
            text = f"[任务 {job.name}] 本次未生成内容（goal: {job.goal})"
        return text
