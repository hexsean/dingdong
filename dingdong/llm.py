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


def _is_xiaomi_mimo_base_url(base_url: str) -> bool:
    return "xiaomimimo.com" in base_url.lower()


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


def _with_last_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在最后一条消息打 cache_control 断点，缓存到当前轮为止的对话前缀（断点随对话自动前移）。

    只浅拷贝、不改动入参；Anthropic 对过短前缀会忽略 cache_control（无副作用）。
    """
    if not messages:
        return messages
    content = messages[-1].get("content")
    if isinstance(content, str):
        if not content:
            return messages
        new_content: Any = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        new_content = [dict(b) if isinstance(b, dict) else b for b in content]
        new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
    else:
        return messages
    out = list(messages)
    out[-1] = {**out[-1], "content": new_content}
    return out


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
            # system 作为可缓存块 → 缓存 tools+system 这段又大又稳定（日级）的前缀；
            # 最后一条消息再打一个断点 → 缓存到当前轮为止的对话历史（断点随对话前移）。
            "system": ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                       if system else system),
            "messages": _with_last_cache_breakpoint(messages),
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
        self._is_xiaomi_mimo = _is_xiaomi_mimo_base_url(base_url)
        client_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        if self._is_xiaomi_mimo:
            client_kwargs["default_headers"] = {"api-key": api_key}
        self._client = OpenAI(**client_kwargs)
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
            text_parts: list[str] = []
            image_parts: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for part in content:
                if part["type"] == "text":
                    text_parts.append(part["text"])
                elif part["type"] == "image":
                    src = part.get("source", {})
                    media_type = src.get("media_type", "image/jpeg")
                    data = src.get("data", "")
                    image_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{data}"},
                    })
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
                if image_parts:
                    multimodal: list[dict[str, Any]] = []
                    if text_parts:
                        multimodal.append({"type": "text", "text": "\n".join(text_parts)})
                    multimodal.extend(image_parts)
                    out.append({"role": role, "content": multimodal})
                elif text_parts:
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
        }
        if self._is_xiaomi_mimo:
            kwargs["extra_body"] = {"max_completion_tokens": max_tokens}
        else:
            kwargs["max_tokens"] = max_tokens
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
            kwargs.setdefault("extra_body", {})["thinking"] = {"type": "enabled"}
            kwargs["reasoning_effort"] = "low"
        elif self._is_xiaomi_mimo:
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


def build_vision_provider(cfg) -> LLMProvider | None:
    """从 VISION_* 配置构建独立视觉模型 provider，未配置返回 None。"""
    if not cfg.vision_provider or not cfg.vision_model or not cfg.vision_api_key:
        return None
    if cfg.vision_provider == "anthropic":
        return AnthropicProvider(cfg.vision_api_key, cfg.vision_model)
    if cfg.vision_provider == "openai":
        base_url = cfg.vision_base_url or "https://api.openai.com/v1"
        return OpenAIProvider(cfg.vision_api_key, cfg.vision_model, base_url)
    raise ValueError(f"unknown VISION_PROVIDER {cfg.vision_provider!r}")
