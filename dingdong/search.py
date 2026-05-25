"""Exa 搜索封装。

提供给 intent 层（用户主动搜索）和 executor（定时任务执行时搜索）复用。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_exa_client = None


def init_exa(api_key: str) -> bool:
    global _exa_client
    if not api_key:
        log.info("EXA_API_KEY not set; search disabled")
        return False
    try:
        from exa_py import Exa
        _exa_client = Exa(api_key=api_key)
        log.info("Exa search initialized")
        return True
    except ImportError:
        log.warning("exa_py not installed; search disabled")
        return False


def is_available() -> bool:
    return _exa_client is not None


def search(query: str, num_results: int = 5) -> str:
    if _exa_client is None:
        return "搜索不可用（EXA_API_KEY 未配置）"
    try:
        results = _exa_client.search(
            query,
            num_results=num_results,
            contents={"text": {"max_characters": 1000}},
        )
        if not results.results:
            return f"没有找到关于「{query}」的结果。"
        parts: list[str] = []
        for i, r in enumerate(results.results, 1):
            title = r.title or "(无标题)"
            url = r.url or ""
            text_preview = (r.text or "")[:300].strip()
            parts.append(f"{i}. {title}\n   {url}\n   {text_preview}")
        return "\n\n".join(parts)
    except Exception as exc:
        log.exception("Exa search failed")
        return f"搜索出错：{exc}"


def read_url(url: str) -> str:
    if _exa_client is None:
        return "搜索不可用（EXA_API_KEY 未配置）"
    try:
        result = _exa_client.get_contents(
            url,
            text={"max_characters": 5000},
        )
        if not result.results:
            return f"无法获取 {url} 的内容。"
        r = result.results[0]
        title = r.title or ""
        text = (r.text or "")[:5000].strip()
        return f"{title}\n{url}\n\n{text}"
    except Exception as exc:
        log.exception("Exa get_contents failed")
        return f"获取页面失败：{exc}"
