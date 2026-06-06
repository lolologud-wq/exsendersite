"""Telegram bot for registration confirmation (long polling via Bot API)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Optional

import httpx

from register_verify import PendingRegistrationStore

logger = logging.getLogger(__name__)

VERIFY_BOT_TOKEN = os.getenv("VERIFY_BOT_TOKEN", "").strip()
VERIFY_BOT_USERNAME = os.getenv("VERIFY_BOT_USERNAME", "").strip()
POLL_TIMEOUT_SEC = 25


def _api_base() -> str:
    return f"https://api.telegram.org/bot{VERIFY_BOT_TOKEN}"


async def _tg_call(
    client: httpx.AsyncClient,
    method: str,
    *,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    r = await client.post(
        f"{_api_base()}/{method}",
        json=json_body,
        params=params,
        timeout=45.0,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"telegram {method} failed")
    return data


async def send_message(
    client: httpx.AsyncClient,
    chat_id: int,
    text: str,
    *,
    reply_markup: Optional[dict[str, Any]] = None,
) -> None:
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        body["reply_markup"] = reply_markup
    await _tg_call(client, "sendMessage", json_body=body)


def _extract_start_token(text: str) -> str:
    raw = (text or "").strip()
    if not raw.startswith("/start"):
        return ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    arg = parts[1].strip()
    if arg.startswith("verify_"):
        return arg[len("verify_") :]
    return arg


async def resolve_bot_username(client: httpx.AsyncClient) -> str:
    global VERIFY_BOT_USERNAME
    if VERIFY_BOT_USERNAME:
        return VERIFY_BOT_USERNAME.lstrip("@")
    try:
        data = await _tg_call(client, "getMe")
        username = str((data.get("result") or {}).get("username") or "").strip()
        if username:
            VERIFY_BOT_USERNAME = username
        return username
    except Exception:
        logger.warning("verify bot getMe failed", exc_info=True)
        return ""


def bot_deep_link(token: str, username: str = "") -> str:
    uname = (username or VERIFY_BOT_USERNAME).lstrip("@")
    if not uname:
        return ""
    return f"https://t.me/{uname}?start=verify_{token}"


async def handle_update(
    update: dict[str, Any],
    *,
    client: httpx.AsyncClient,
    pending: PendingRegistrationStore,
    telegram_taken: Callable[[int], bool],
) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    from_user = msg.get("from") or {}
    tg_uid = int(from_user.get("id") or 0)
    if tg_uid <= 0:
        return
    text = str(msg.get("text") or "")

    if text.strip() in ("/help", "/start") and not _extract_start_token(text):
        await send_message(
            client,
            int(chat_id),
            "Это бот подтверждения регистрации exsender.\n\n"
            "Откройте ссылку с сайта после заполнения формы — "
            "кнопка «Подтвердить в Telegram».",
        )
        return

    token = _extract_start_token(text)
    if not token:
        return

    if telegram_taken(tg_uid):
        await send_message(
            client,
            int(chat_id),
            "Этот Telegram уже привязан к аккаунту exsender.\n"
            "Войдите на сайте или используйте другой Telegram.",
        )
        return

    ok, reply = pending.mark_verified(
        token,
        telegram_user_id=tg_uid,
        telegram_username=str(from_user.get("username") or ""),
    )
    if not ok:
        await send_message(client, int(chat_id), reply)
        return

    site = os.getenv("SITE_PUBLIC_URL", "https://exsender.top").rstrip("/")
    await send_message(
        client,
        int(chat_id),
        reply,
        reply_markup={
            "inline_keyboard": [[{"text": "Открыть exsender", "url": site + "/register"}]]
        },
    )


async def run_verify_bot(
    pending: PendingRegistrationStore,
    telegram_taken: Callable[[int], bool],
) -> None:
    if not VERIFY_BOT_TOKEN:
        logger.warning(
            "VERIFY_BOT_TOKEN не задан — подтверждение через Telegram отключено"
        )
        return

    offset = 0
    async with httpx.AsyncClient() as client:
        username = await resolve_bot_username(client)
        logger.info(
            "Verify bot polling started%s",
            f" (@{username})" if username else "",
        )
        while True:
            try:
                r = await client.get(
                    f"{_api_base()}/getUpdates",
                    params={"timeout": POLL_TIMEOUT_SEC, "offset": offset},
                    timeout=float(POLL_TIMEOUT_SEC + 20),
                )
                r.raise_for_status()
                data = r.json()
                if not data.get("ok"):
                    raise RuntimeError(data.get("description") or "getUpdates failed")
                for upd in data.get("result") or []:
                    offset = int(upd.get("update_id", 0)) + 1
                    try:
                        await handle_update(
                            upd,
                            client=client,
                            pending=pending,
                            telegram_taken=telegram_taken,
                        )
                    except Exception:
                        logger.exception("verify bot handle_update")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("verify bot poll error", exc_info=True)
                await asyncio.sleep(3.0)


def start_verify_bot_background(
    pending: PendingRegistrationStore,
    telegram_taken: Callable[[int], bool],
) -> Optional[asyncio.Task]:
    if not VERIFY_BOT_TOKEN:
        return None
    return asyncio.create_task(run_verify_bot(pending, telegram_taken))
