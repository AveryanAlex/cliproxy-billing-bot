from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_telegram_ids: frozenset[int]
    keeper_base_url: str
    keeper_login_password: str
    database_url: str
    time_zone: ZoneInfo

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        password = os.environ.get("KEEPER_LOGIN_PASSWORD", "")
        admin_text = os.environ.get("ADMIN_TELEGRAM_IDS", "")
        if not token or not password or not admin_text.strip():
            raise ValueError(
                "TELEGRAM_BOT_TOKEN, KEEPER_LOGIN_PASSWORD and ADMIN_TELEGRAM_IDS are required"
            )
        try:
            admins = frozenset(int(item.strip()) for item in admin_text.split(",") if item.strip())
        except ValueError as error:
            raise ValueError("ADMIN_TELEGRAM_IDS must contain comma-separated integers") from error
        if not admins or any(admin <= 0 for admin in admins):
            raise ValueError("ADMIN_TELEGRAM_IDS must contain positive Telegram IDs")
        database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./billing.db").strip()
        if not database_url.startswith("sqlite+aiosqlite:///"):
            raise ValueError("Only sqlite+aiosqlite DATABASE_URL values are supported")
        return cls(
            telegram_bot_token=token,
            admin_telegram_ids=admins,
            keeper_base_url=os.environ.get(
                "KEEPER_BASE_URL",
                "http://cliproxyapi-usage-keeper:8080/usage/api/v1",
            ).rstrip("/"),
            keeper_login_password=password,
            database_url=database_url,
            time_zone=ZoneInfo(os.environ.get("TIME_ZONE", "Europe/Moscow")),
        )
