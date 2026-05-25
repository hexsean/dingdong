"""LLM 提供商抽象。

把 anthropic 与 openai 两个 SDK 收敛到一个 ``LLMProvider.chat`` 接口。
内部使用 Anthropic 风格的 content-part 列表作为统一格式：

    content = [
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
        {"type": "tool_result", "tool_use_id": "...", "content": "..."},
    ]

所有 provider 在内部把上述格式翻译成自家 API。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ChatResponse:
    """LLM 一次返回。``content`` 同样使用统一格式。"""

    content: list[dict[str, Any]]
    stop_reason: str  # "end_turn" | "tool_use" | ...
    reasoning_content: str | None = None
    raw: Any = None

    def text(self) -> str:
        return "\n".join(p["text"] for p in self.content if p.get("type") == "text").strip()

    def tool_uses(self) -> list[dict[str, Any]]:
        return [p for p in self.content if p.get("type") == "tool_use"]


class LLMProvider(Protocol):
    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse: ...


# ---------------- anthropic ----------------


class AnthropicProvider:
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # type: ignore
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is empty")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        resp = self._client.messages.create(**kwargs)
        content: list[dict[str, Any]] = []
        for block in resp.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                content.append({"type": "text", "text": block.text})
            elif kind == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.input or {}),
                    }
                )
        return ChatResponse(content=content, stop_reason=resp.stop_reason or "", raw=resp)


# ---------------- openai-compatible ----------------


class OpenAIProvider:
    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        from openai import OpenAI  # type: ignore
        if not api_key:
            raise ValueError("OPENAI_API_KEY is empty")
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._is_deepseek = "deepseek" in base_url.lower()

    def _to_openai_messages(
        self, system: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            # assistant turn containing text and/or tool_use
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for part in content:
                if part["type"] == "text":
                    text_parts.append(part["text"])
                elif part["type"] == "tool_use":
                    tool_calls.append(
                        {
                            "id": part["id"],
                            "type": "function",
                            "function": {
                                "name": part["name"],
                                "arguments": json.dumps(part.get("input") or {}),
                            },
                        }
                    )
                elif part["type"] == "tool_result":
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": part["tool_use_id"],
                            "content": part["content"],
                        }
                    )
            if role == "assistant":
                msg_out: dict[str, Any] = {"role": "assistant"}
                text_content = "\n".join(text_parts) if text_parts else None
                if tool_calls and not text_content:
                    text_content = ""
                msg_out["content"] = text_content
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                rc = msg.get("reasoning_content")
                if rc is not None:
                    msg_out["reasoning_content"] = rc
                out.append(msg_out)
            else:
                if text_parts:
                    out.append({"role": role, "content": "\n".join(text_parts)})
                out.extend(tool_results)
        return out

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_openai_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        if self._is_deepseek:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = "low"
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        content: list[dict[str, Any]] = []
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            content.append(
                {"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args}
            )
        stop_reason = "tool_use" if any(p["type"] == "tool_use" for p in content) else "end_turn"
        if choice.finish_reason and not content:
            content.append({"type": "text", "text": ""})
        rc = getattr(msg, "reasoning_content", None)
        return ChatResponse(content=content, stop_reason=stop_reason, reasoning_content=rc, raw=resp)


# ---------------- factory ----------------


def build_provider(cfg) -> LLMProvider:  # cfg: Config
    if cfg.llm_provider == "anthropic":
        return AnthropicProvider(cfg.anthropic_api_key, cfg.anthropic_model)
    if cfg.llm_provider == "openai":
        return OpenAIProvider(cfg.openai_api_key, cfg.openai_model, cfg.openai_base_url)
    raise ValueError(f"unknown LLM provider {cfg.llm_provider!r}")
