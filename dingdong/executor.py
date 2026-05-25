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
        if job.schedule_kind == "date":
            self._store.delete(job.id)
            log.info("one-shot job %s (%s) cleaned up", job.id, job.name)

    def _generate(self, job: Job) -> str:
        now = datetime.now(self._tz)
        user_prompt = (
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
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
