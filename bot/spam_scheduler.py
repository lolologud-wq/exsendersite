from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import NamedTuple, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SlowModeWaitError

import health
from state import (
    RuntimeState,
    effective_send_text,
    enabled_chat_ids,
    random_interval_seconds,
)

logger = logging.getLogger(__name__)

# Один spam_loop на слот (аккаунт), чтобы не дублировать при входе из бота и т.п.
_spam_loop_started: set[str] = set()

_GAP = 1.5
_FIRST_FIRE_SEC = 3.0
_FORBIDDEN_BACKOFF_SEC = 3600.0


def _is_write_forbidden(e: BaseException) -> bool:
    n = type(e).__name__
    return n in (
        "ChatWriteForbiddenError",
        "UserBannedInChannelError",
        "ChannelPrivateError",
        "ChatGuestSendForbiddenError",
    )


def _is_bad_peer(e: BaseException) -> bool:
    return type(e).__name__ in ("PeerIdInvalidError", "InputUserDeactivatedError")


class _SendPlan(NamedTuple):
    is_forward: bool
    forward_from: Optional[int]
    forward_msg_ids: tuple[int, ...]
    text: Optional[str]
    entities: Optional[list]


async def _resolve_send_plan(
    client: TelegramClient, state: RuntimeState, chat_id: int
) -> _SendPlan:
    c = state.cfg(chat_id)
    peer: Optional[int] = None
    mid: Optional[int] = None
    use_forward: bool = False
    extra: str = ""
    if c.source_channel_id is not None:
        peer = c.source_channel_id
        mid = c.source_message_id
        use_forward = c.source_forward
        extra = c.extra_text or ""
    elif state.global_source_channel_id is not None:
        peer = state.global_source_channel_id
        mid = state.global_source_message_id
        use_forward = state.default_source_forward
        extra = c.extra_text or ""

    if peer is None:
        t = effective_send_text(state, chat_id, pick_random_variant=True)
        return _SendPlan(False, None, (), t, None)

    try:
        if mid is not None:
            fetched = await client.get_messages(peer, ids=mid)
            if isinstance(fetched, (list, tuple)):
                m = fetched[0] if fetched else None
            else:
                m = fetched
        else:
            msg = await client.get_messages(peer, limit=1)
            if isinstance(msg, (list, tuple)):
                m = msg[0] if msg else None
            else:
                m = msg
        if m:
            if use_forward:
                return _SendPlan(
                    True,
                    peer,
                    (m.id,),
                    None,
                    None,
                )
            body = (m.message or "").strip()
            ex = (extra or "").strip()
            if ex and body:
                text = body + "\n" + ex
            elif ex:
                text = ex
            else:
                text = body
            return _SendPlan(False, None, (), text, m.entities)
    except Exception as e:
        logger.warning("Источник поста %s: %s", peer, e)
    t = effective_send_text(state, chat_id, pick_random_variant=True)
    return _SendPlan(False, None, (), t, None)


def start_spam_loop_background(
    client: TelegramClient,
    state: RuntimeState,
    persist: Callable[[], None],
    account_key: str,
) -> bool:
    """Запускает spam_loop в фоне для account_key, если ещё нет. False — уже был запуск."""
    if account_key in _spam_loop_started:
        logger.debug("spam_loop уже запущен для слота %s", account_key)
        return False
    _spam_loop_started.add(account_key)

    async def _runner() -> None:
        try:
            await spam_loop(client, state, persist=persist, account_key=account_key)
        finally:
            _spam_loop_started.discard(account_key)

    loop = asyncio.get_running_loop()
    loop.create_task(_runner())
    return True


async def spam_loop(
    client: TelegramClient,
    state: RuntimeState,
    *,
    persist: Callable[[], None],
    account_key: Optional[str] = None,
) -> None:
    next_fire: dict[int, float] = {}
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(2)
        try:
            if not client.is_connected():
                next_fire.clear()
                continue
            try:
                if not await client.is_user_authorized():
                    next_fire.clear()
                    continue
            except Exception:
                next_fire.clear()
                continue
            if not state.spam_running:
                next_fire.clear()
                continue

            now = loop.time()
            ids = enabled_chat_ids(state)
            for cid in list(next_fire.keys()):
                if cid not in ids:
                    del next_fire[cid]

            if not ids:
                continue

            for cid in ids:
                if cid not in next_fire:
                    c0 = state.cfg(cid)
                    delay_sec = max(0.0, float(c0.start_delay_min or 0)) * 60.0
                    if delay_sec > 0:
                        next_fire[cid] = now + delay_sec
                    else:
                        # Первое сообщение скоро, дальше — полный интервал (раньше ждали целый интервал сразу).
                        next_fire[cid] = now + _FIRST_FIRE_SEC

            due = [cid for cid in ids if next_fire.get(cid, 0) <= now]
            due.sort(key=lambda x: next_fire.get(x, 0))

            for cid in due:
                if not state.spam_running:
                    break
                if cid not in enabled_chat_ids(state):
                    continue
                try:
                    cfg = state.cfg(cid)
                    lim = cfg.message_limit
                    if lim is not None and int(lim) > 0:
                        if cfg.messages_sent >= int(lim):
                            c2 = state.cfg(cid)
                            c2.enabled = False
                            state.set_cfg(cid, c2)
                            persist()
                            del next_fire[cid]
                            logger.info(
                                "Лимит %s достигнут для %s — чат выключен.",
                                lim,
                                cid,
                            )
                            continue
                    plan = await _resolve_send_plan(client, state, cid)
                    if plan.is_forward:
                        if not plan.forward_msg_ids or plan.forward_from is None:
                            logger.warning(
                                "Пересылка: нет сообщения в канале-источнике, пропуск %s",
                                cid,
                            )
                            next_fire[cid] = loop.time() + random_interval_seconds(
                                state, cid
                            )
                            continue
                        await client.forward_messages(
                            cid,
                            list(plan.forward_msg_ids),
                            from_peer=plan.forward_from,
                        )
                    else:
                        text, entities = plan.text, plan.entities
                        if not text:
                            logger.warning("Пустой текст, пропуск %s", cid)
                            next_fire[cid] = loop.time() + random_interval_seconds(
                                state, cid
                            )
                            continue
                        if entities:
                            try:
                                await client.send_message(
                                    cid, text, formatting_entities=entities
                                )
                            except TypeError:
                                await client.send_message(cid, text)
                        else:
                            await client.send_message(cid, text)
                    logger.info("Отправлено в чат %s", cid)
                    if account_key:
                        health.note_send(account_key, chat_id=cid)
                    c3 = state.cfg(cid)
                    c3.messages_sent = int(c3.messages_sent or 0) + 1
                    lim2 = c3.message_limit
                    hit = (
                        lim2 is not None
                        and int(lim2) > 0
                        and c3.messages_sent >= int(lim2)
                    )
                    if hit:
                        c3.enabled = False
                    state.set_cfg(cid, c3)
                    persist()
                    if hit:
                        del next_fire[cid]
                        logger.info(
                            "Лимит %s достигнут для %s — чат выключен.",
                            lim2,
                            cid,
                        )
                        await asyncio.sleep(_GAP)
                        continue
                except (FloodWaitError, SlowModeWaitError) as e:
                    wait = int(getattr(e, "seconds", 0) or 0) + 2
                    logger.warning(
                        "Flood/SlowMode %ss для %s (%s) — чат отложен, остальные без паузы",
                        wait,
                        cid,
                        type(e).__name__,
                    )
                    if account_key:
                        health.note_error(account_key, e, chat_id=cid)
                    # Не sleep здесь: иначе вся рассылка замирает. Только этот peer ждёт свой срок.
                    next_fire[cid] = loop.time() + float(max(wait, 1))
                    continue
                except Exception as e:
                    if _is_write_forbidden(e):
                        logger.warning("Нет доступа к отправке в %s: %s", cid, e)
                        if account_key:
                            health.note_error(account_key, e, chat_id=cid)
                        next_fire[cid] = loop.time() + _FORBIDDEN_BACKOFF_SEC
                        await asyncio.sleep(_GAP)
                        continue
                    if _is_bad_peer(e):
                        logger.warning("Некорректный peer %s, чат отключён: %s", cid, e)
                        if account_key:
                            health.note_error(account_key, e, chat_id=cid)
                        c4 = state.cfg(cid)
                        c4.enabled = False
                        state.set_cfg(cid, c4)
                        persist()
                        del next_fire[cid]
                        await asyncio.sleep(_GAP)
                        continue
                    logger.warning("Отправка в %s: %s", cid, e)
                    if account_key:
                        health.note_error(account_key, e, chat_id=cid)
                next_fire[cid] = loop.time() + random_interval_seconds(state, cid)
                await asyncio.sleep(_GAP)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("spam_loop: %s", e)
