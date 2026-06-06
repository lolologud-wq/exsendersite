from __future__ import annotations

import asyncio
import logging
import time
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
_spam_loop_tasks: dict[str, asyncio.Task] = {}

_GAP = 1.5
_FORBIDDEN_BACKOFF_SEC = 3600.0

# Minimum time between ANY two sends on one account (keyed by account_key).
_account_last_send: dict[str, float] = {}


def _account_gap_sec(state: RuntimeState) -> float:
    base_min = max(0.1, float(state.default_interval_min or 5))
    for cid in enabled_chat_ids(state):
        c = state.cfg(cid)
        if c.custom_interval_min is not None and c.custom_interval_min > 0:
            base_min = max(base_min, float(c.custom_interval_min))
    return base_min * 60.0


async def _wait_for_account_gap(account_key: Optional[str], state: RuntimeState) -> bool:
    """Sleep until global inter-message gap elapsed. False = spam stopped."""
    if not account_key:
        return True
    gap = _account_gap_sec(state)
    last = _account_last_send.get(account_key, 0.0)
    if state.last_send_at > last:
        last = state.last_send_at
    wait = last + gap - time.time()
    while wait > 0:
        if not state.spam_running:
            return False
        chunk = min(wait, 2.0)
        await asyncio.sleep(chunk)
        wait = last + gap - time.time()
    return True


def _mark_account_send(
    account_key: Optional[str],
    state: RuntimeState,
    persist: Callable[[], None],
) -> None:
    if account_key:
        ts = time.time()
        _account_last_send[account_key] = ts
        state.last_send_at = ts
        persist()


def _schedule_next_fire(
    state: RuntimeState, chat_id: int, *, now: Optional[float] = None
) -> float:
    """Next send time for chat_id (wall clock), respecting last_sent_at."""
    now = now if now is not None else time.time()
    c0 = state.cfg(chat_id)
    delay_sec = max(0.0, float(c0.start_delay_min or 0)) * 60.0
    last = float(c0.last_sent_at or 0)
    if last > 0:
        interval = random_interval_seconds(state, chat_id)
        return max(now, last + interval)
    if delay_sec > 0:
        return now + delay_sec
    # First send for a chat: fire on next scheduler tick, not after full interval.
    return now


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
            _spam_loop_tasks.pop(account_key, None)

    loop = asyncio.get_running_loop()
    _spam_loop_tasks[account_key] = loop.create_task(_runner())
    return True


def restart_spam_loop_background(
    client: TelegramClient,
    state: RuntimeState,
    persist: Callable[[], None],
    account_key: str,
) -> bool:
    """Перезапускает spam_loop (нужно после смены Telethon-клиента или старта спама)."""
    old = _spam_loop_tasks.pop(account_key, None)
    if old and not old.done():
        old.cancel()
    _spam_loop_started.discard(account_key)
    return start_spam_loop_background(client, state, persist, account_key)


def _record_send_error(
    account_key: Optional[str],
    state: RuntimeState,
    persist: Callable[[], None],
    err: BaseException,
    *,
    chat_id: Optional[int] = None,
) -> None:
    if account_key:
        health.note_error(account_key, err, chat_id=chat_id)
    state.errors_total = int(state.errors_total or 0) + 1
    state.last_error = str(err)[:200]
    state.last_error_kind = type(err).__name__
    state.last_error_at = time.time()
    state.last_error_chat_id = chat_id
    persist()


async def spam_loop(
    client: TelegramClient,
    state: RuntimeState,
    *,
    persist: Callable[[], None],
    account_key: Optional[str] = None,
) -> None:
    next_fire: dict[int, float] = {}
    seen_interval_seq = -1

    if account_key and state.last_send_at > 0:
        _account_last_send[account_key] = state.last_send_at

    while True:
        await asyncio.sleep(2)
        try:
            if state.interval_seq != seen_interval_seq:
                next_fire.clear()
                seen_interval_seq = state.interval_seq
            if not client.is_connected():
                try:
                    await client.connect()
                except Exception:
                    next_fire.clear()
                    await asyncio.sleep(5)
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

            now = time.time()
            ids = enabled_chat_ids(state)
            for cid in list(next_fire.keys()):
                if cid not in ids:
                    del next_fire[cid]

            if not ids:
                continue

            for cid in ids:
                if cid not in next_fire:
                    next_fire[cid] = _schedule_next_fire(state, cid, now=now)

            due = [cid for cid in ids if next_fire.get(cid, 0) <= now]
            if not due:
                continue
            due.sort(key=lambda x: next_fire.get(x, 0))
            cid = due[0]

            if not await _wait_for_account_gap(account_key, state):
                continue
            if not state.spam_running:
                continue
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
                        next_fire[cid] = _schedule_next_fire(state, cid)
                        continue
                    await client.forward_messages(
                        cid,
                        list(plan.forward_msg_ids),
                        from_peer=plan.forward_from,
                    )
                else:
                    text, entities = plan.text, plan.entities
                    if not text:
                        msg = f"Пустой текст для чата {cid} — задайте сообщение или источник в Настройках"
                        logger.warning("%s", msg)
                        if account_key:
                            _record_send_error(
                                account_key,
                                state,
                                persist,
                                RuntimeError(msg),
                                chat_id=cid,
                            )
                        next_fire[cid] = _schedule_next_fire(state, cid)
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
                sent_at = time.time()
                _mark_account_send(account_key, state, persist)
                c3 = state.cfg(cid)
                c3.messages_sent = int(c3.messages_sent or 0) + 1
                c3.last_sent_at = sent_at
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
                    "Flood/SlowMode %ss для %s (%s) — чат отложен",
                    wait,
                    cid,
                    type(e).__name__,
                )
                if account_key:
                    _record_send_error(account_key, state, persist, e, chat_id=cid)
                next_fire[cid] = time.time() + float(max(wait, 1))
                continue
            except Exception as e:
                if _is_write_forbidden(e):
                    logger.warning("Нет доступа к отправке в %s: %s", cid, e)
                    if account_key:
                        _record_send_error(account_key, state, persist, e, chat_id=cid)
                    next_fire[cid] = time.time() + _FORBIDDEN_BACKOFF_SEC
                    await asyncio.sleep(_GAP)
                    continue
                if _is_bad_peer(e):
                    logger.warning("Некорректный peer %s, чат отключён: %s", cid, e)
                    if account_key:
                        _record_send_error(account_key, state, persist, e, chat_id=cid)
                    c4 = state.cfg(cid)
                    c4.enabled = False
                    state.set_cfg(cid, c4)
                    persist()
                    del next_fire[cid]
                    await asyncio.sleep(_GAP)
                    continue
                logger.warning("Отправка в %s: %s", cid, e)
                if account_key:
                    _record_send_error(account_key, state, persist, e, chat_id=cid)
            next_fire[cid] = _schedule_next_fire(state, cid)
            await asyncio.sleep(_GAP)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("spam_loop: %s", e)
