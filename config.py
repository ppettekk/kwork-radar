"""Конфигурация из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    raw = _str(key)
    return int(raw) if raw else default


def _bool(key: str, default: bool) -> bool:
    raw = _str(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _list(key: str) -> list[str]:
    raw = _str(key)
    if not raw:
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _int_list(key: str) -> list[int | str]:
    raw = _str(key)
    if not raw:
        return []
    if raw.lower() == "all":
        return ["all"]
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    # --- Kwork ---
    kwork_login: str = field(default_factory=lambda: _str("KWORK_LOGIN"))
    kwork_password: str = field(default_factory=lambda: _str("KWORK_PASSWORD"))
    kwork_phone_last: str | None = field(
        default_factory=lambda: _str("KWORK_PHONE_LAST") or None
    )
    kwork_proxy: str | None = field(default_factory=lambda: _str("KWORK_PROXY") or None)

    # --- Telegram ---
    tg_token: str = field(default_factory=lambda: _str("TG_BOT_TOKEN"))
    tg_chat_id: int = field(default_factory=lambda: _int("TG_CHAT_ID", 0))

    # --- CloseRouter ---
    llm_api_key: str = field(default_factory=lambda: _str("CLOSEROUTER_API_KEY"))
    llm_base_url: str = field(
        default_factory=lambda: _str("CLOSEROUTER_BASE_URL", "https://api.closerouter.dev/v1")
    )
    llm_model: str = field(default_factory=lambda: _str("LLM_MODEL", "openai/gpt-5.4-mini"))
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))
    repair_dashes: bool = field(default_factory=lambda: _bool("REPAIR_DASHES", True))
    greeting: str = field(default_factory=lambda: _str("GREETING", "Здравствуйте!"))

    # --- Фильтры ---
    categories: list[int | str] = field(default_factory=lambda: _int_list("CATEGORIES"))
    max_offers: int = field(default_factory=lambda: _int("MAX_OFFERS", 5))
    min_price: int = field(default_factory=lambda: _int("MIN_PRICE", 0))
    max_price: int = field(default_factory=lambda: _int("MAX_PRICE", 0))
    hiring_from: int = field(default_factory=lambda: _int("HIRING_FROM", 0))
    include_keywords: list[str] = field(default_factory=lambda: _list("INCLUDE_KEYWORDS"))
    exclude_keywords: list[str] = field(default_factory=lambda: _list("EXCLUDE_KEYWORDS"))

    # --- Поведение ---
    poll_interval: int = field(default_factory=lambda: _int("POLL_INTERVAL", 45))
    notify_on_first_run: bool = field(
        default_factory=lambda: _bool("NOTIFY_ON_FIRST_RUN", False)
    )
    db_path: Path = field(default_factory=lambda: BASE_DIR / _str("DB_PATH", "radar.db"))
    profile_path: Path = field(
        default_factory=lambda: BASE_DIR / _str("PROFILE_PATH", "profile.md")
    )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("KWORK_LOGIN", self.kwork_login),
                ("KWORK_PASSWORD", self.kwork_password),
                ("TG_BOT_TOKEN", self.tg_token),
            )
            if not value
        ]
        if not self.tg_chat_id:
            missing.append("TG_CHAT_ID")
        if missing:
            raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")
        if self.llm_enabled and not self.llm_api_key:
            raise RuntimeError("LLM_ENABLED=true, но CLOSEROUTER_API_KEY пуст")

    def load_profile(self) -> str:
        if self.profile_path.exists():
            return self.profile_path.read_text(encoding="utf-8").strip()
        return ""
