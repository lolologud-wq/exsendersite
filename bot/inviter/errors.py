from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    UserAlreadyParticipantError,
    UserChannelsTooMuchError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import InputPeerChannel, InputPeerChat

logger = logging.getLogger(__name__)

INVITE_RPC_TIMEOUT_SEC = max(10.0, float(os.getenv("INVITE_RPC_TIMEOUT_SEC", "45")))
MAX_FLOODWAIT_SEC = max(30, int(os.getenv("INVITE_MAX_FLOODWAIT_SEC", "120")))


async def _invite_rpc(coro: Any) -> Any:
    return await asyncio.wait_for(coro, timeout=INVITE_RPC_TIMEOUT_SEC)


async def _sleep_floodwait(seconds: int) -> bool:
    """Sleep for FloodWait; return True if wait is too long (abort invite)."""
    need = int(seconds) + 1
    if need > MAX_FLOODWAIT_SEC:
        return True
    await asyncio.sleep(need)
    return False


def _input_user_id(user: Any) -> int | None:
    uid = getattr(user, "user_id", None)
    if uid:
        return int(uid)
    uid = getattr(user, "id", None)
    if uid:
        return int(uid)
    return None


def _results_from_invited_users(result: Any, users: list[Any]) -> list[str]:
    """Telegram returns per-user failures in missing_invitees — not only exceptions."""
    missing = getattr(result, "missing_invitees", None) or []
    missing_by_id: dict[int, str] = {}
    for item in missing:
        uid = int(getattr(item, "user_id", 0) or 0)
        if not uid:
            continue
        if getattr(item, "premium_would_allow_invite", False) or getattr(
            item, "premium_required_for_pm", False
        ):
            missing_by_id[uid] = "premium_required"
        else:
            missing_by_id[uid] = "privacy_restricted"
    out: list[str] = []
    for user in users:
        uid = _input_user_id(user)
        if uid and uid in missing_by_id:
            out.append(missing_by_id[uid])
        else:
            out.append("invited")
    return out


def get_antiflood_timing(base_delay: float) -> tuple[float, float, int, float, float]:
    base = max(0.0, float(base_delay))
    if base > 0:
        jitter = float(os.getenv("INVITE_DELAY_JITTER", "0.15"))
        per_invite_min = max(0.05, base * (1.0 - jitter))
        per_invite_max = max(per_invite_min, base * (1.0 + jitter))
    else:
        per_invite_min = float(os.getenv("INVITE_DELAY_MIN_SEC", "0.5"))
        per_invite_max = float(os.getenv("INVITE_DELAY_MAX_SEC", "2.0"))
        if per_invite_max < per_invite_min:
            per_invite_max = per_invite_min
    batch_size = int(os.getenv("INVITE_BATCH_SIZE", "0"))
    batch_pause_min = float(os.getenv("INVITE_BATCH_PAUSE_MIN_SEC", "0"))
    batch_pause_max = float(os.getenv("INVITE_BATCH_PAUSE_MAX_SEC", "0"))
    if batch_pause_max < batch_pause_min:
        batch_pause_max = batch_pause_min
    return per_invite_min, per_invite_max, max(0, batch_size), batch_pause_min, batch_pause_max


_INVITE_COOLDOWN_RESULTS = frozenset({"invited", "invited_after_wait"})


def needs_invite_cooldown(result: str) -> bool:
    return result in _INVITE_COOLDOWN_RESULTS


def invite_api_batch_size() -> int:
    return max(1, min(int(os.getenv("INVITE_API_BATCH", "1")), 20))


async def invite_many(client: TelegramClient, target_input, users: list[Any]) -> list[str]:
    if not users:
        return []
    if len(users) == 1:
        return [await invite_one(client, target_input, users[0])]
    if not isinstance(target_input, InputPeerChannel):
        out: list[str] = []
        for user in users:
            out.append(await invite_one(client, target_input, user))
        return out
    try:
        result = await _invite_rpc(
            client(InviteToChannelRequest(channel=target_input, users=users))
        )
        return _results_from_invited_users(result, users)
    except asyncio.TimeoutError:
        logger.warning("InviteToChannel batch timeout (%s users)", len(users))
        return ["invite_timeout"] * len(users)
    except (
        UserAlreadyParticipantError,
        UserPrivacyRestrictedError,
        UserChannelsTooMuchError,
        PeerFloodError,
        ChatAdminRequiredError,
        ChatWriteForbiddenError,
        ChannelPrivateError,
    ):
        out = []
        for user in users:
            out.append(await invite_one(client, target_input, user))
        return out
    except FloodWaitError as error:
        if await _sleep_floodwait(error.seconds):
            return ["peer_flood"] * len(users)
        try:
            result = await _invite_rpc(
                client(InviteToChannelRequest(channel=target_input, users=users))
            )
            return [
                "invited_after_wait" if r == "invited" else r
                for r in _results_from_invited_users(result, users)
            ]
        except asyncio.TimeoutError:
            return ["invite_timeout"] * len(users)
        except RPCError:
            out = []
            for user in users:
                out.append(await invite_one(client, target_input, user))
            return out
    except RPCError:
        out = []
        for user in users:
            out.append(await invite_one(client, target_input, user))
        return out


async def invite_one(client: TelegramClient, target_input, user) -> str:
    try:
        if isinstance(target_input, InputPeerChannel):
            result = await _invite_rpc(
                client(InviteToChannelRequest(channel=target_input, users=[user]))
            )
            return _results_from_invited_users(result, [user])[0]
        elif isinstance(target_input, InputPeerChat):
            await _invite_rpc(
                client(
                    AddChatUserRequest(
                        chat_id=target_input.chat_id, user_id=user, fwd_limit=10
                    )
                )
            )
            return "invited"
        else:
            return "unsupported_target_type"
    except asyncio.TimeoutError:
        return "invite_timeout"
    except UserAlreadyParticipantError:
        return "already_in_chat"
    except UserPrivacyRestrictedError:
        return "privacy_restricted"
    except UserChannelsTooMuchError:
        return "user_too_many_channels"
    except ChatAdminRequiredError:
        return "no_admin_rights"
    except ChatWriteForbiddenError:
        return "chat_write_forbidden"
    except ChannelPrivateError:
        return "target_chat_private"
    except PeerFloodError:
        return "peer_flood"
    except FloodWaitError as error:
        if await _sleep_floodwait(error.seconds):
            return "peer_flood"
        try:
            if isinstance(target_input, InputPeerChannel):
                result = await _invite_rpc(
                    client(InviteToChannelRequest(channel=target_input, users=[user]))
                )
                status = _results_from_invited_users(result, [user])[0]
            elif isinstance(target_input, InputPeerChat):
                await _invite_rpc(
                    client(
                        AddChatUserRequest(
                            chat_id=target_input.chat_id, user_id=user, fwd_limit=10
                        )
                    )
                )
                status = "invited"
            else:
                return "unsupported_target_type"
            return "invited_after_wait" if status == "invited" else status
        except asyncio.TimeoutError:
            return "invite_timeout"
        except RPCError:
            return "failed_after_wait"
    except RPCError as error:
        return f"rpc_error:{error.__class__.__name__}"


def format_error_message(exc: Exception) -> str:
    name = exc.__class__.__name__
    detail = str(exc).strip()
    lines = [f"{name}: {detail}" if detail else name]
    hint = error_hint_for_exception(exc)
    if hint:
        lines.append("")
        lines.append(hint)
    return "\n".join(lines)


def error_hint_for_exception(exc: Exception) -> str:
    name = exc.__class__.__name__
    detail = str(exc).lower()

    if isinstance(exc, ValueError):
        if "could not find" in detail and ("entity" in detail or "peer" in detail):
            if "peerchannel" in detail or "channel" in detail:
                return (
                    "Решение:\n"
                    "1. Выбери target заново (ссылка или @username).\n"
                    "2. Убедись, что аккаунт состоит в target-чате.\n"
                    "3. Обнови список чатов и выбери цель из списка."
                )
            return (
                "Решение:\n"
                "1. Проверь ссылку или @username чата.\n"
                "2. Убедись, что аккаунт имеет доступ к этому чату."
            )
        return ""

    if isinstance(exc, RPCError):
        return rpc_error_hint(name)

    if name in {"ConnectionError", "TimeoutError", "OSError", "ServerError"}:
        return "Решение: проверь интернет и повтори позже."
    if "timeout" in detail or "timed out" in detail:
        return "Решение: проверь интернет и повтори позже."
    return ""


def rpc_error_hint(error_name: str) -> str:
    hints = {
        "ChannelPrivateError": (
            "Решение:\n"
            "1. Аккаунт не состоит в этом чате или чат приватный.\n"
            "2. Вступи в target-чат этим аккаунтом и выбери цель заново."
        ),
        "ChatAdminRequiredError": (
            "Решение:\n"
            "1. Нужны права администратора в target-чате.\n"
            "2. Выдай аккаунту право приглашать пользователей."
        ),
        "ChatWriteForbiddenError": (
            "Решение:\n"
            "1. У аккаунта нет прав на приглашение в target-чат.\n"
            "2. Проверь права администратора или выбери другой target."
        ),
        "UsernameNotOccupiedError": "Решение: проверь @username или ссылку — такого чата/канала нет.",
        "UsernameInvalidError": "Решение: проверь формат @username или ссылки (https://t.me/...).",
        "InviteHashExpiredError": "Решение: ссылка-приглашение устарела. Получи новую ссылку на чат.",
        "PeerFloodError": (
            "Решение:\n"
            "1. Telegram ограничил инвайты на этом аккаунте.\n"
            "2. Подожди несколько часов или используй другой аккаунт.\n"
            "3. Увеличь паузы между инвайтами в .env."
        ),
    }
    return hints.get(error_name, "")


def format_invite_result_error(result: str) -> str:
    hints = {
        "chat_write_forbidden": (
            "chat_write_forbidden\n\n"
            "Решение:\n"
            "1. У аккаунта нет прав на приглашение в target-чат.\n"
            "2. Проверь права администратора или выбери другой target."
        ),
        "no_admin_rights": (
            "no_admin_rights\n\n"
            "Решение:\n"
            "1. Нужны права администратора в target-чате.\n"
            "2. Выдай аккаунту право приглашать пользователей."
        ),
        "target_chat_private": (
            "target_chat_private\n\n"
            "Решение:\n"
            "1. Аккаунт не состоит в target-чате или чат приватный.\n"
            "2. Вступи в чат этим аккаунтом и выбери цель заново."
        ),
        "peer_flood": (
            "PeerFloodError\n\n"
            "Решение:\n"
            "1. Telegram ограничил инвайты на этом аккаунте.\n"
            "2. Подожди несколько часов или используй другой аккаунт.\n"
            "3. Увеличь паузы между инвайтами в .env."
        ),
    }
    if result in hints:
        return hints[result]
    if result.startswith("rpc_error:"):
        rpc_name = result.split(":", 1)[1]
        hint = rpc_error_hint(rpc_name)
        text = result
        if hint:
            text += f"\n\n{hint}"
        return text
    return result


async def sleep_with_stop(stop_flag: Callable[[], bool], seconds: float) -> bool:
    remaining = max(0.0, seconds)
    while remaining > 0:
        if stop_flag():
            return True
        step = min(1.0, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return stop_flag()
