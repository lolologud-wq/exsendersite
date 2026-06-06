"""Trial plan quotas (servers + accounts)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from fastapi import HTTPException

from proxy import get_json
from registry import BotRecord, BotRegistry
from users import UserRecord

logger = logging.getLogger(__name__)

TRIAL_MAX_BOTS = max(0, int(os.getenv("TRIAL_MAX_BOTS", "1") or 1))
TRIAL_MAX_ACCOUNTS = max(0, int(os.getenv("TRIAL_MAX_ACCOUNTS", "2") or 2))


def trial_limits_meta() -> dict[str, int]:
    return {"maxBots": TRIAL_MAX_BOTS, "maxAccounts": TRIAL_MAX_ACCOUNTS}


def user_on_trial(rec: Optional[UserRecord]) -> bool:
    if rec is None or rec.blocked:
        return False
    now = time.time()
    return rec.plan == "trial" and rec.plan_expires_at > now


def enforce_trial_bot_limit(rec: Optional[UserRecord], registry: BotRegistry) -> None:
    if not user_on_trial(rec):
        return
    used = len(registry.list_for(rec.id))
    if used >= TRIAL_MAX_BOTS:
        raise HTTPException(
            status_code=403,
            detail=(
                f"На триале доступен максимум {TRIAL_MAX_BOTS} сервер. "
                "Оформи тариф в профиле."
            ),
        )


async def account_ids_on_bot(bot: BotRecord) -> set[str]:
    if not bot.api_token:
        return set()
    try:
        status, data = await get_json(bot, "accounts")
        if status != 200 or not isinstance(data, list):
            return set()
        return {str(a.get("id", "")).strip() for a in data if a.get("id")}
    except Exception:
        logger.debug("account_ids_on_bot failed for %s", bot.id, exc_info=True)
        return set()


async def count_owner_accounts(registry: BotRegistry, owner_id: str) -> int:
    total = 0
    for bot in registry.list_for(owner_id):
        total += len(await account_ids_on_bot(bot))
    return total


async def owner_usage_stats(registry: BotRegistry, owner_id: str) -> dict[str, int]:
    """VDS (bots) and Telegram slots for a panel user."""
    import overview_cache

    bots = registry.list_for(owner_id)
    bots_used = len(bots)
    accounts_used = 0
    for bot in bots:
        ov = overview_cache.get(bot.id)
        if ov is not None:
            totals = ov.get("totals") if isinstance(ov.get("totals"), dict) else {}
            n = int(totals.get("accounts") or 0)
            if n <= 0 and isinstance(ov.get("accounts"), list):
                n = len(ov["accounts"])
            accounts_used += n
            continue
        if bot.api_token:
            accounts_used += len(await account_ids_on_bot(bot))
    return {"botsUsed": bots_used, "accountsUsed": accounts_used}


async def enforce_trial_account_limit(
    rec: Optional[UserRecord],
    registry: BotRegistry,
    *,
    bot: Optional[BotRecord] = None,
    slot_id: Optional[str] = None,
) -> None:
    """Block creating a new slot when trial account quota is reached."""
    if not user_on_trial(rec):
        return
    if bot is not None and slot_id:
        existing = await account_ids_on_bot(bot)
        if slot_id in existing:
            return
    total = await count_owner_accounts(registry, rec.id)
    if total >= TRIAL_MAX_ACCOUNTS:
        raise HTTPException(
            status_code=403,
            detail=(
                f"На триале доступно максимум {TRIAL_MAX_ACCOUNTS} аккаунта. "
                "Оформи тариф в профиле."
            ),
        )


async def trial_usage(
    rec: Optional[UserRecord], registry: BotRegistry
) -> Optional[dict[str, Any]]:
    if not user_on_trial(rec):
        return None
    bots_used = len(registry.list_for(rec.id))
    accounts_used = await count_owner_accounts(registry, rec.id)
    meta = trial_limits_meta()
    return {
        **meta,
        "botsUsed": bots_used,
        "accountsUsed": accounts_used,
    }
