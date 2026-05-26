"""模型能力查询。

每次启动从 OpenRouter 拉取模型列表，匹配当前配置的模型名，
返回视觉支持、上下文长度等元信息。查询成功写入缓存，失败时读缓存兜底。
未匹配到的模型需用户通过 VISION_ENABLED 环境变量手动配置。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

log = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


@dataclass
class ModelInfo:
    model_id: str
    supports_vision: bool = False
    context_length: int | None = None
    modality: str = ""
    source: str = ""


def _cache_path(data_dir: Path) -> Path:
    return data_dir / "model_cache.json"


def _load_cache(data_dir: Path, model_name: str) -> ModelInfo | None:
    path = _cache_path(data_dir)
    if not path.exists():
        return None
    try:
        cache = json.loads(path.read_text("utf-8"))
        if cache.get("model_name") != model_name:
            return None
        return ModelInfo(**cache["info"])
    except Exception:
        return None


def _save_cache(data_dir: Path, model_name: str, info: ModelInfo) -> None:
    path = _cache_path(data_dir)
    try:
        path.write_text(json.dumps({
            "model_name": model_name,
            "info": asdict(info),
        }, ensure_ascii=False), "utf-8")
    except Exception as exc:
        log.debug("缓存写入失败: %s", exc)


def _match_model(model_name: str, models: list[dict]) -> dict | None:
    name = model_name.lower().strip()

    for m in models:
        if m.get("id", "").lower() == name:
            return m

    for m in models:
        mid = m.get("id", "").lower()
        short = mid.split("/", 1)[-1] if "/" in mid else mid
        if short == name:
            return m

    for m in models:
        mid = m.get("id", "").lower()
        short = mid.split("/", 1)[-1] if "/" in mid else mid
        if name in short or short in name:
            return m

    return None


def fetch_model_info(model_name: str, *, data_dir: Path | None = None,
                     vision_override: bool | None = None) -> ModelInfo:
    """查询模型能力。

    每次启动都请求 OpenRouter。成功则更新缓存；失败则读缓存兜底。
    VISION_ENABLED 环境变量优先级最高。
    """
    if not model_name:
        return ModelInfo(model_id="")

    # 1. 查询 OpenRouter
    info = _query_openrouter(model_name)

    # 2. 查询失败，读缓存兜底
    if not info.source and data_dir:
        cached = _load_cache(data_dir, model_name)
        if cached:
            log.info("OpenRouter 不可用，使用缓存: vision=%s, context=%s",
                     cached.supports_vision, cached.context_length)
            info = cached

    # 3. 查询成功，写缓存
    if info.source and data_dir:
        _save_cache(data_dir, model_name, info)

    # 4. VISION_ENABLED 覆盖
    if vision_override is not None:
        info.supports_vision = vision_override
        info.source = info.source or "config"
        log.info("VISION_ENABLED 覆盖: vision=%s", vision_override)

    return info


def _query_openrouter(model_name: str) -> ModelInfo:
    try:
        resp = requests.get(OPENROUTER_MODELS_URL, timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
    except Exception as exc:
        log.warning("OpenRouter 查询失败: %s", exc)
        return ModelInfo(model_id=model_name, source="")

    matched = _match_model(model_name, models)
    if not matched:
        log.warning(
            "OpenRouter 未找到模型「%s」，图片理解默认关闭。"
            "如需开启请在 .env 中设置 VISION_ENABLED=true",
            model_name,
        )
        return ModelInfo(model_id=model_name, supports_vision=False, source="openrouter")

    arch = matched.get("architecture") or {}
    modality = arch.get("modality", "")
    ctx = matched.get("context_length")

    info = ModelInfo(
        model_id=matched.get("id", model_name),
        supports_vision="image" in modality.lower(),
        context_length=ctx,
        modality=modality,
        source="openrouter",
    )
    log.info("模型 %s: vision=%s, context=%s, modality=%s (OpenRouter)",
             info.model_id, info.supports_vision, info.context_length, info.modality)
    return info
