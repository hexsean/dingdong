from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    llm_provider: str
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    data_dir: Path
    scheduler_tz: str
    long_poll_timeout_ms: int
    exa_api_key: str
    history_limit: int
    allowed_user_ids: frozenset[str]
    vision_enabled: bool | None  # True/False=手动覆盖, None=自动检测

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def qrcode_png_path(self) -> Path:
        return self.data_dir / "qrcode.png"


def load_config() -> Config:
    load_dotenv(Path("data/.env"))
    load_dotenv()

    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    if provider not in {"anthropic", "openai"}:
        raise ValueError(f"LLM_PROVIDER must be 'anthropic' or 'openai', got {provider!r}")

    data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
    allowed = frozenset(x.strip() for x in raw_allowed.split(",") if x.strip())

    raw_vision = os.getenv("VISION_ENABLED", "").strip().lower()
    vision_enabled: bool | None = None
    if raw_vision in ("true", "1", "yes"):
        vision_enabled = True
    elif raw_vision in ("false", "0", "no"):
        vision_enabled = False

    return Config(
        llm_provider=provider,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        data_dir=data_dir,
        scheduler_tz=os.getenv("SCHEDULER_TZ", "Asia/Shanghai"),
        long_poll_timeout_ms=int(os.getenv("LONG_POLL_TIMEOUT_MS", "35000")),
        exa_api_key=os.getenv("EXA_API_KEY", ""),
        history_limit=int(os.getenv("HISTORY_LIMIT", "20")),
        allowed_user_ids=allowed,
        vision_enabled=vision_enabled,
    )
