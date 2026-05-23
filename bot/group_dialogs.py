from __future__ import annotations

from telethon import TelegramClient
from typing import Optional

from telethon.tl.types import Channel, Chat, User


async def list_group_chats(client: TelegramClient) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    async for dialog in client.iter_dialogs():
        e = dialog.entity
        if isinstance(e, User):
            continue
        if isinstance(e, Channel):
            if getattr(e, "broadcast", False):
                continue
            title = (e.title or "").strip() or str(e.id)
        elif isinstance(e, Chat):
            title = (e.title or "").strip() or str(e.id)
        else:
            continue
        rows.append((dialog.id, title))
    rows.sort(key=lambda x: x[1].casefold())
    return rows


async def group_chat_title(
    client: TelegramClient, peer_id: int
) -> Optional[str]:
    """Название группы / супергруппы по id диалога, иначе None."""
    try:
        ent = await client.get_entity(peer_id)
    except Exception:
        return None
    if isinstance(ent, Channel) and not getattr(ent, "broadcast", False):
        return (ent.title or "").strip() or None
    if isinstance(ent, Chat):
        return (ent.title or "").strip() or None
    return None


async def list_broadcast_channels(client: TelegramClient) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    async for dialog in client.iter_dialogs():
        e = dialog.entity
        if isinstance(e, Channel) and getattr(e, "broadcast", False):
            title = (e.title or "").strip() or str(e.id)
            rows.append((dialog.id, title))
    rows.sort(key=lambda x: x[1].casefold())
    return rows
