"""Telegram API credentials + device fingerprint (TDesktop / Android)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient
from telethon.tl.functions.auth import SendCodeRequest
from telethon.tl.types import CodeSettings, auth


@dataclass(frozen=True)
class TelegramApiConfig:
    api_id: int
    api_hash: str
    profile: str
    device_model: str
    system_version: str
    app_version: str
    lang_code: str = "en"
    system_lang_code: str = "en-US"


PROFILES: dict[str, dict[str, Any]] = {
    "tdesktop": {
        "api_id": 2040,
        "api_hash": "b18441a1ff607e10a989891a5462e627",
        "device_model": "Desktop",
        "system_version": "Windows 10",
        "app_version": "5.2.3 x64",
    },
    "android": {
        "api_id": 6,
        "api_hash": "eb06d4abfb49dc3eeb1aeb98ae0f581e",
        "device_model": "Samsung SM-G973F",
        "system_version": "SDK 31",
        "app_version": "10.14.5",
    },
}


def _device_fields(profile_name: str) -> dict[str, str]:
    data = PROFILES.get(profile_name) or PROFILES["tdesktop"]
    return {
        "device_model": str(data["device_model"]),
        "system_version": str(data["system_version"]),
        "app_version": str(data["app_version"]),
    }


def get_telegram_api_config() -> TelegramApiConfig:
    """
    If API_ID/API_HASH are in .env — use them (keeps existing .session files).
    Device fingerprint comes from TELEGRAM_DEVICE_PROFILE (default tdesktop).
    """
    client_profile = (os.getenv("TELEGRAM_CLIENT_PROFILE") or "tdesktop").strip().lower()
    device_profile = (
        os.getenv("TELEGRAM_DEVICE_PROFILE") or client_profile or "tdesktop"
    ).strip().lower()
    if device_profile not in PROFILES:
        device_profile = "tdesktop"

    api_id_raw = (os.getenv("API_ID") or "").strip()
    api_hash = (os.getenv("API_HASH") or "").strip()
    device = _device_fields(device_profile)

    if api_id_raw and api_hash:
        return TelegramApiConfig(
            api_id=int(api_id_raw),
            api_hash=api_hash,
            profile="custom",
            lang_code="en",
            system_lang_code="en-US",
            **device,
        )

    if client_profile == "custom":
        raise RuntimeError(
            "TELEGRAM_CLIENT_PROFILE=custom требует API_ID и API_HASH в .env"
        )

    profile = client_profile if client_profile in PROFILES else "tdesktop"
    data = PROFILES[profile]
    return TelegramApiConfig(profile=profile, **data)


def telethon_client_kwargs(cfg: TelegramApiConfig) -> dict[str, Any]:
    return {
        "device_model": cfg.device_model,
        "system_version": cfg.system_version,
        "app_version": cfg.app_version,
        "lang_code": cfg.lang_code,
        "system_lang_code": cfg.system_lang_code,
    }


async def request_login_code(client: TelegramClient, phone: str) -> auth.SentCode:
    """Request auth code with settings that prefer in-app delivery."""
    return await client(
        SendCodeRequest(
            phone_number=phone,
            api_id=client.api_id,
            api_hash=client.api_hash,
            settings=CodeSettings(
                allow_flashcall=True,
                current_number=True,
                allow_app_hash=True,
                allow_missed_call=True,
                allow_firebase=True,
                unknown_number=True,
            ),
        )
    )
