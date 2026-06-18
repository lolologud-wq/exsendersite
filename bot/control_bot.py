from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time

from group_dialogs import (
    group_chat_title,
    list_broadcast_channels,
    list_group_chats,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from proxy_util import parse_proxy
from telethon_accounts import connect_client_with_fallback, make_telethon_client, session_path
from telethon_client_profile import get_telegram_api_config, request_login_code
from telethon_join import join_chat_or_channel_by_link, resolve_tme_post_for_forward
from spam_scheduler import restart_spam_loop_background
from state import (
    MultiAccountState,
    RuntimeState,
    effective_interval_min,
    effective_jitter,
    effective_send_text,
    enabled_chat_ids,
    save_multi_account_state,
    validate_spam_start,
)

logger = logging.getLogger(__name__)

API_CONFIG = get_telegram_api_config()
PARSE = "HTML"
PER_PAGE = 6


def _persist(context: ContextTypes.DEFAULT_TYPE) -> None:
    save_multi_account_state(context.bot_data["multi"])


def _chats_intro_long(context: ContextTypes.DEFAULT_TYPE) -> str:
    s = _esc(context.bot_data["multi"].active_account_id)
    return (
        f"<b>Чаты</b> · слот <code>{s}</code>\n"
        "Диалоги <b>этого</b> userbot; включение и тексты — свои для каждого слота.\n"
        "Обновите 🔄 при новых диалогах.\n"
        "<b>🔗 Вступить по ссылке</b> — invite или публичный t.me/username.\n"
        "Строка: ✅/❌ — вкл/выкл рассылку · <code>⚙</code> — настройки чата."
    )


def _chats_intro_short(context: ContextTypes.DEFAULT_TYPE) -> str:
    s = _esc(context.bot_data["multi"].active_account_id)
    return (
        f"<b>Чаты</b> · <code>{s}</code>\n"
        "Строка: ✅/❌ — вкл/выкл · ⚙ — настройки."
    )


async def _home_text_kb(
    state: RuntimeState, online: bool, context: ContextTypes.DEFAULT_TYPE
) -> tuple[str, InlineKeyboardMarkup]:
    if not online:
        multi = context.bot_data["multi"]
        clients = context.bot_data["telethon_clients"]
        has_ready = False
        for slot_id in multi.account_order:
            if await _telethon_ready(clients.get(slot_id)):
                has_ready = True
                break
        if not has_ready:
            return (
                "<b>Нет активного онлайн-слота</b>\n\n"
                "Сейчас ни один userbot-аккаунт не подключён к Telegram.\n"
                "Откройте список аккаунтов и войдите в любой слот.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("👤 Аккаунты", callback_data="accpick")]]
                ),
            )
    aid = context.bot_data["multi"].active_account_id
    text = main_panel_text(state, online, account_id=aid)
    return text, main_kb(state)


async def _accounts_pick_text_async(
    multi: MultiAccountState,
    account_order: list[str],
    telethon_clients: dict,
) -> str:
    lines: list[str] = []
    for aid in account_order:
        cl = telethon_clients.get(aid)
        ready = await _telethon_ready(cl)
        mark = "✓ " if aid == multi.active_account_id else "· "
        lines.append(
            f"{mark}<code>{_esc(aid)}</code>  {'🟢' if ready else '🔴'}"
        )
    return (
        "<b>Аккаунты</b>\n"
        "Нажмите слот: <b>🔴</b> — вход (номер → код → пароль 2FA), "
        "<b>🟢</b> — переключить на этот слот.\n\n"
        + "\n".join(lines)
    )


def _accounts_pick_kb(account_order: list[str], active_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for aid in account_order:
        prefix = "✓ " if aid == active_id else ""
        label = f"{prefix}{aid}"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"acc:{aid}")])
    rows.append([InlineKeyboardButton("➕ Новый слот", callback_data="accadd")])
    rows.append([InlineKeyboardButton("🗑 Удалить слот", callback_data="accdelmenu")])
    rows.append([InlineKeyboardButton("◀ Меню", callback_data="home")])
    return InlineKeyboardMarkup(rows)


_ACC_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")


def _normalize_phone(raw: str) -> str | None:
    s = re.sub(r"[\s()-]", "", (raw or "").strip())
    if s.startswith("8") and len(s) == 11:
        s = "+7" + s[1:]
    if not s.startswith("+"):
        return None
    if len(s) < 10 or len(s) > 18:
        return None
    return s


async def _telethon_ready(tc) -> bool:
    if tc is None or not tc.is_connected():
        return False
    try:
        return await tc.is_user_authorized()
    except Exception:
        return False


async def _run_until_disconnected_logged(client, aid: str) -> None:
    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Userbot [%s]: run_until_disconnected завершился с ошибкой", aid
        )


async def _after_telethon_sign_in(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    aid: str,
    client,
) -> None:
    multi = context.bot_data["multi"]
    context.bot_data["telethon_clients"][aid] = client
    boot_auth = context.bot_data.get("authorized_at_boot") or frozenset()
    if aid not in boot_auth:
        asyncio.create_task(_run_until_disconnected_logged(client, aid))
    restart_spam_loop_background(
        client,
        multi.accounts[aid],
        persist=lambda: save_multi_account_state(multi),
        account_key=aid,
    )
    me = await client.get_me()
    multi.active_account_id = aid
    context.bot_data["state"] = multi.accounts[aid]
    context.bot_data["telethon_client"] = client
    _persist(context)
    on = await _telethon_ready(client)
    ht, kb = await _home_text_kb(multi.accounts[aid], on, context)
    await update.message.reply_text(
        f"✅ Вход выполнен: <code>{_esc(aid)}</code> "
        f"(@{_esc(me.username or str(me.id))}).\n\n{ht}",
        reply_markup=kb,
        parse_mode=PARSE,
    )


def _parse_new_account_id(text: str) -> str | None:
    t = (text or "").strip()
    if not t or not _ACC_ID_RE.match(t):
        return None
    return t


def _accounts_delete_kb(account_order: list[str], active_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for aid in account_order:
        mark = "✓ " if aid == active_id else ""
        label = f"🗑 {mark}{aid}"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"accdelask:{aid}")])
    rows.append([InlineKeyboardButton("◀ К аккаунтам", callback_data="accpick")])
    return InlineKeyboardMarkup(rows)


def _delete_session_files(account_id: str) -> None:
    base = session_path(account_id)
    for p in (
        base + ".session",
        base + ".session-journal",
        base + ".session-wal",
        base + ".session-shm",
    ):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            logger.warning("Не удалось удалить файл сессии %s", p, exc_info=True)
CHATS_CACHE_TTL_SEC = 3600
# Кэш на клиенте Telethon: переживает пересоздание PTB Application (таймауты / reconnect).
_TG_GROUP_CHATS_CACHE = "_usbot_group_chats_cache"
_TG_GROUP_CHATS_LOCK = "_usbot_group_chats_lock"

# Bot API: дефолты PTB/httpx слишком жёсткие (connect/read 5 с, pool 1 с) — на слабых каналах → TimedOut.
_BOT_CONNECT_TIMEOUT = float(os.getenv("BOT_API_CONNECT_TIMEOUT", "40"))
_BOT_READ_TIMEOUT = float(os.getenv("BOT_API_READ_TIMEOUT", "30"))
_BOT_WRITE_TIMEOUT = float(os.getenv("BOT_API_WRITE_TIMEOUT", "40"))
_BOT_POOL_TIMEOUT = float(os.getenv("BOT_API_POOL_TIMEOUT", "30"))


def _parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_USER_IDS", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


def _is_admin_m(update: Update, admin_ids: set[int]) -> bool:
    u = update.effective_user.id if update.effective_user else None
    return u is not None and u in admin_ids


def _is_admin_q(q, admin_ids: set[int]) -> bool:
    u = q.from_user.id if q.from_user else None
    return u is not None and u in admin_ids


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _mask_proxy_display(proxy: str | None) -> str:
    if not proxy or not str(proxy).strip():
        return "не задан"
    s = str(proxy).strip()
    if "@" in s:
        left, _, right = s.rpartition("@")
        if ":" in left:
            user, _, _ = left.partition(":")
            return f"{user}:***@{right}"
    return s


def _parse_jitter_user(text: str) -> float:
    t = text.strip().replace(",", ".")
    if "." in t:
        v = float(t)
        return max(0.0, min(0.95, v))
    v = float(t)
    return max(0.0, min(0.95, v / 100.0))


def _jitter_caption(state: RuntimeState, cid: int) -> str:
    c = state.cfg(cid)
    j = c.custom_interval_jitter
    if j is None:
        g = effective_jitter(state, cid)
        return f"как в настройках (±{int(round(g * 100))}%)"
    if j <= 0:
        return "выкл (строго по минутам)"
    return f"±{int(round(j * 100))}%"


def _parse_text_variants(raw: str) -> list[str]:
    if raw.strip() == "-":
        return []
    parts = re.split(r"(?m)^---\s*$", raw)
    return [p.strip() for p in parts if p.strip()]


def _plain_alert(s: str) -> str:
    return (
        s.replace("<code>", "")
        .replace("</code>", "")
        .replace("&gt;", ">")
        .replace("&lt;", "<")
    )


async def _notify_cb_fail(q, text: str, *, emphasis: bool = False) -> None:
    """Сообщение пользователю, если answerCallbackQuery уже вызван (второй answer нельзя)."""
    msg = q.message
    if msg is None:
        return
    t = f"⚠️ {text}" if emphasis else text
    try:
        await msg.reply_text(t)
    except Exception:
        logger.debug("_notify_cb_fail", exc_info=True)


_NET_ALERT_RU = (
    "⚠️ <b>Не достучаться до Telegram Bot API</b> (сеть / блокировка).\n"
    "Проверьте интернет, DNS и доступ с сервера к <code>api.telegram.org</code>."
)


async def _retry_net(coro_factory, *, attempts: int = 4, label: str = "request") -> None:
    delay = 1.0
    last: Exception | None = None
    for i in range(attempts):
        try:
            await coro_factory()
            return
        except (NetworkError, TimedOut) as e:
            last = e
            logger.warning("%s сеть Telegram (попытка %s/%s): %s", label, i + 1, attempts, e)
            if i + 1 < attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 1.6, 6.0)
    if last:
        raise last


async def _answer_callback_resilient(q, **kw) -> bool:
    """answerCallbackQuery с повторами. False — не удалось (кнопка может «крутиться» до таймаута TG)."""
    try:
        # Для callback-ответа не делаем ретраи: при "query is too old" они только зашумляют лог.
        await q.answer(**kw)
        return True
    except BadRequest as e:
        err = str(e).lower()
        if "query is too old" in err or "response timeout expired" in err:
            logger.debug("callback query устарел во время ответа")
            return False
        raise
    except (NetworkError, TimedOut) as e:
        logger.error("answer_callback окончательно не удался: %s", e)
        msg = q.message
        if msg is not None:
            try:
                await msg.reply_text(_NET_ALERT_RU, parse_mode=PARSE)
            except Exception:
                logger.debug("reply после сетевой ошибки callback", exc_info=True)
        return False


async def _edit_cb_message(q, **kwargs) -> None:
    """Только правка сообщения (ответ на callback должен быть уже отправлен)."""
    async def _edit() -> None:
        await q.edit_message_text(**kwargs)

    try:
        await _retry_net(_edit, label="edit_message")
    except BadRequest as e:
        err = str(e).lower()
        if "not modified" in err:
            return
        if "query is too old" in err or "response timeout expired" in err:
            await _notify_cb_fail(q, "Кнопка устарела. Откройте /start", emphasis=True)
            return
        raise
    except (NetworkError, TimedOut) as e:
        logger.error("edit_message_text после callback: %s", e)
        await _notify_cb_fail(
            q,
            "Нет связи с Bot API. Проверьте сеть с этого хоста.",
            emphasis=True,
        )
        raise


async def _ea(q, **kwargs) -> None:
    """Снять «часики» на кнопке, затем обновить текст. Устойчиво к кратковременным сбоям сети."""
    ok = await _answer_callback_resilient(q)
    if not ok:
        return
    try:
        await _edit_cb_message(q, **kwargs)
    except (NetworkError, TimedOut):
        pass


def _sync_chats_cache_to_bot_data(
    context: ContextTypes.DEFAULT_TYPE, rows: list[tuple[int, str]]
) -> None:
    aid = context.bot_data["multi"].active_account_id
    by_acc: dict[str, list[tuple[int, str]]] = context.bot_data.setdefault(
        "group_chats_rows_by_account", {}
    )
    by_acc[aid] = rows
    at_by_acc: dict[str, float] = context.bot_data.setdefault(
        "group_chats_cached_at_by_account", {}
    )
    at_by_acc[aid] = time.time()


async def _get_group_chats_cached(
    context: ContextTypes.DEFAULT_TYPE, force: bool = False
) -> list[tuple[int, str]]:
    tc = context.bot_data.get("telethon_client")
    if tc is None or not tc.is_connected():
        return []
    try:
        if not await tc.is_user_authorized():
            return []
    except Exception:
        return []
    lock = getattr(tc, _TG_GROUP_CHATS_LOCK, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(tc, _TG_GROUP_CHATS_LOCK, lock)
    async with lock:
        now_m = time.monotonic()
        pack = getattr(tc, _TG_GROUP_CHATS_CACHE, None)
        if (
            not force
            and pack is not None
            and isinstance(pack[1], list)
            and now_m - float(pack[0]) < CHATS_CACHE_TTL_SEC
        ):
            rows = pack[1]
            _sync_chats_cache_to_bot_data(context, rows)
            return rows
        rows = await list_group_chats(tc)
        setattr(tc, _TG_GROUP_CHATS_CACHE, (now_m, rows))
        _sync_chats_cache_to_bot_data(context, rows)
        return rows


def main_panel_text(
    state: RuntimeState, online: bool, *, account_id: str | None = None
) -> str:
    sp = "🟢 запущен" if state.spam_running else "⏹ остановлен"
    line = "✅ Userbot онлайн" if online else "❌ Userbot офлайн / нет сессии"
    n = len(enabled_chat_ids(state))
    acc = (
        f"Аккаунт: <code>{_esc(account_id)}</code>\n"
        f"<i>Чаты, текст и настройки ниже — для этого слота.</i>\n"
        if account_id
        else ""
    )
    return (
        "<b>Главное меню</b>\n\n"
        f"{acc}"
        f"{line}\n"
        f"Спам: {sp}\n"
        f"Чатов в рассылке (✅): {n}"
    )


def main_kb(state: RuntimeState) -> InlineKeyboardMarkup:
    label = "⏹ Остановить спам" if state.spam_running else "▶ Запустить спам"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data="spam")],
            [InlineKeyboardButton("👤 Аккаунты", callback_data="accpick")],
            [InlineKeyboardButton("Чаты", callback_data="chats0")],
            [InlineKeyboardButton("Настройки", callback_data="settings")],
        ]
    )


def chats_kb(
    rows: list[tuple[int, str]], page: int, state: RuntimeState
) -> InlineKeyboardMarkup:
    pages = max(1, (len(rows) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PER_PAGE : page * PER_PAGE + PER_PAGE]
    btns: list[list[InlineKeyboardButton]] = []
    for cid, title in chunk:
        en = state.cfg(cid).enabled
        mark = "✅" if en else "❌"
        t = (title or "")[:28] + ("…" if len(title or "") > 28 else "")
        btns.append(
            [
                InlineKeyboardButton(
                    f"{mark} {t}", callback_data=f"T{page}_{cid}"
                ),
                InlineKeyboardButton("⚙", callback_data=f"B{page}_{cid}"),
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("«", callback_data=f"chats{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("»", callback_data=f"chats{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append(
        [InlineKeyboardButton("🔗 Вступить по ссылке", callback_data=f"jinv{page}")]
    )
    btns.append(
        [
            InlineKeyboardButton("✅ Включить все", callback_data=f"a_on_{page}"),
            InlineKeyboardButton("❌ Выключить все", callback_data=f"a_off_{page}"),
        ]
    )
    btns.append(
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"rchats{page}")]
    )
    btns.append([InlineKeyboardButton("◀ В меню", callback_data="home")])
    return InlineKeyboardMarkup(btns)


def _chat_title_from_cache(context: ContextTypes.DEFAULT_TYPE, cid: int) -> str | None:
    aid = context.bot_data["multi"].active_account_id
    by_acc = context.bot_data.get("group_chats_rows_by_account")
    if not isinstance(by_acc, dict):
        return None
    rows = by_acc.get(aid)
    if not isinstance(rows, list):
        return None
    for r_id, title in rows:
        try:
            if int(r_id) != int(cid):
                continue
        except (TypeError, ValueError):
            continue
        t = (str(title) if title is not None else "").strip()
        return t or None
    return None


async def chat_settings_text_async(
    state: RuntimeState,
    cid: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> str:
    title = _chat_title_from_cache(context, cid)
    tc = context.bot_data.get("telethon_client")
    if not title and tc is not None and tc.is_connected():
        try:
            title = await group_chat_title(tc, cid)
        except Exception:
            logger.debug("group_chat_title(%s)", cid, exc_info=True)
    slot = context.bot_data["multi"].active_account_id
    return chat_settings_text(state, cid, display_title=title, slot_id=slot)


def chat_settings_text(
    state: RuntimeState,
    cid: int,
    display_title: str | None = None,
    *,
    slot_id: str | None = None,
) -> str:
    c = state.cfg(cid)
    nvar = len([x for x in (c.text_variants or []) if str(x).strip()])
    if nvar:
        cm = f"несколько текстов ({nvar} шт.)"
    elif c.custom_message and str(c.custom_message).strip():
        cm = "свой текст"
    else:
        cm = "стандарт"
    body = effective_send_text(state, cid) or "—"
    prev = body if len(body) < 500 else body[:500] + "…"
    iv = effective_interval_min(state, cid)
    ch = c.source_channel_id
    if ch:
        post = (
            f", пост <code>#{c.source_message_id}</code>"
            if c.source_message_id is not None
            else ", <b>последний пост</b>"
        )
        chs = (
            f"<code>{ch}</code>{post} — "
            f"<b>{'пересылка' if c.source_forward else 'копия текста'}</b>"
        )
    else:
        if state.global_source_channel_id is not None:
            gmid = state.global_source_message_id
            post = (
                f", пост <code>#{gmid}</code>"
                if gmid is not None
                else ", <b>последний пост</b>"
            )
            gm = "пересылка" if state.default_source_forward else "копия"
            chs = (
                f"<b>общий (Настройки)</b> <code>{state.global_source_channel_id}</code>"
                f"{post} — {gm}"
            )
        else:
            chs = "нет"
    ci = (
        "свой"
        if c.custom_interval_min is not None
        else "стандарт"
    )
    lim = c.message_limit
    lims = (
        f"{c.messages_sent or 0}/{lim}"
        if lim is not None and int(lim) > 0
        else ("нет" if lim is None else str(lim))
    )
    sd = c.start_delay_min
    sds = f"{_esc(sd)} мин" if sd is not None and float(sd) > 0 else "нет"
    tn = (display_title or "").strip()
    slot_line = (
        f"Слот аккаунта: <code>{_esc(slot_id)}</code>\n\n"
        if slot_id
        else ""
    )
    if tn:
        header = f"{slot_line}<b>{_esc(tn)}</b>\n<code>{_esc(cid)}</code>\n\n"
    else:
        header = f"{slot_line}<b>Чат</b> <code>{_esc(cid)}</code>\n\n"
    return (
        header
        + f"В рассылке: {'да' if c.enabled else 'нет'}\n"
        + f"Текст: {cm}, интервал: {ci}\n"
        + f"Интервал сейчас: <b>{_esc(iv)}</b> мин\n"
        + f"Рандом паузы: {_jitter_caption(state, cid)}\n"
        + f"Задержка старта: {sds}\n"
        + f"Лимит отправок: {lims}\n"
        + f"Канал-источник: {chs}\n"
        + f"Доп. текст (только при <b>копии</b>, не при пересылке): {_esc(c.extra_text or '—')}\n\n"
        + f"<b>Как будет выглядеть (первый текст):</b>\n<code>{_esc(prev)}</code>"
    )


def chat_settings_kb(cid: int, state: RuntimeState, back_page: int) -> InlineKeyboardMarkup:
    en = (
        "⏸ Выключить чат"
        if state.cfg(cid).enabled
        else "▶ Включить чат"
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(en, callback_data=f"ten{cid}")],
            [InlineKeyboardButton("✏️ Кастомное сообщение", callback_data=f"cmsg{cid}")],
            [InlineKeyboardButton("⏱ Свой интервал (мин)", callback_data=f"cint{cid}")],
            [InlineKeyboardButton("➕ Дополнительный текст", callback_data=f"cext{cid}")],
            [
                InlineKeyboardButton(
                    "📢 Канал (список)", callback_data=f"clp0_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    (
                        "📤 Режим: пересылка ✓"
                        if state.cfg(cid).source_forward
                        else "📋 Режим: копия текста ✓"
                    ),
                    callback_data=f"cfwd{cid}",
                )
            ],
            [InlineKeyboardButton("🔢 Id канала вручную", callback_data=f"chid{cid}")],
            [InlineKeyboardButton("🔗 Ссылка на пост", callback_data=f"cspt{cid}")],
            [
                InlineKeyboardButton(
                    "↩ К «последнему посту»", callback_data=f"cclr{cid}"
                )
            ],
            [InlineKeyboardButton("↩ Сброс текста", callback_data=f"cmsgr{cid}")],
            [InlineKeyboardButton("↩ Сброс интервала", callback_data=f"cintr{cid}")],
            [InlineKeyboardButton("🎲 Рандом ±%", callback_data=f"cjit{cid}")],
            [InlineKeyboardButton("↩ Сброс рандома", callback_data=f"cjitr{cid}")],
            [
                InlineKeyboardButton(
                    "📝 Несколько текстов (случайный)", callback_data=f"cvrs{cid}"
                )
            ],
            [InlineKeyboardButton("↩ Убрать все варианты", callback_data=f"cvrsr{cid}")],
            [InlineKeyboardButton("🔢 Лимит сообщений", callback_data=f"clim{cid}")],
            [InlineKeyboardButton("↩ Сброс лимита и счётчика", callback_data=f"climr{cid}")],
            [InlineKeyboardButton("⏰ Задержка старта (мин)", callback_data=f"csdl{cid}")],
            [InlineKeyboardButton("↩ Сброс задержки", callback_data=f"csdlr{cid}")],
            [InlineKeyboardButton("✖ Сброс канала", callback_data=f"cclc{cid}")],
            [InlineKeyboardButton(f"◀ К списку (стр. {back_page + 1})", callback_data=f"chats{back_page}")],
        ]
    )


def settings_text(state: RuntimeState, *, slot_id: str | None = None) -> str:
    dm = state.default_message or "—"
    dmp = dm if len(dm) < 400 else dm[:400] + "…"
    jp = int(round(state.default_interval_jitter * 100))
    jr = f"±{jp}% к паузе" if jp > 0 else "выкл"
    px = _mask_proxy_display(state.proxy)
    slot_line = (
        f"Слот: <code>{_esc(slot_id)}</code> · ниже только для этого аккаунта.\n\n"
        if slot_id
        else ""
    )
    chdef = (
        "<b>пересылка</b> (репост)"
        if state.default_source_forward
        else "<b>копия текста</b>"
    )
    if state.global_source_channel_id is not None:
        gmid = state.global_source_message_id
        gp = (
            f"пост <code>#{gmid}</code>"
            if gmid is not None
            else "<b>последний пост</b>"
        )
        glob_block = (
            f"<b>Ссылка на пост для всех чатов:</b> peer <code>{state.global_source_channel_id}</code>, {gp}.\n"
            "<i>Если у чата в ⚙ не задан свой канал — берётся это. Режим — кнопка «С канала».</i>\n\n"
        )
    else:
        glob_block = (
            "<b>Ссылка на пост для всех чатов:</b> <i>не задана</i> — кнопка "
            "<b>🔗 Ссылка на пост (все чаты)</b>.\n\n"
        )
    return (
        f"<b>Настройки</b>\n{slot_line}"
        "<b>Про текст</b>\n"
        "<b>Стандартный</b> — что шлём в чаты, где не задан свой текст и нет источника с канала.\n"
        "В <b>Чаты → ⚙</b> — «несколько текстов» через строку <code>---</code> (случайный вариант).\n\n"
        f"<b>Пост с канала по умолчанию:</b> {chdef}\n"
        f"{glob_block}"
        f"<b>Стандартный текст</b> ({'задан' if state.default_message.strip() else 'пуст'}):\n"
        f"<code>{_esc(dmp)}</code>\n\n"
        f"<b>Прокси этого аккаунта:</b> <code>{_esc(px)}</code>\n"
        "<i>Применяется при новом подключении аккаунта (обычно после перезапуска скрипта).</i>\n\n"
        f"<b>Стандартный интервал:</b> {_esc(state.default_interval_min)} мин\n"
        f"<b>Рандом интервала:</b> {jr} "
        f"(след. пауза ≈ интервал × случайно от 1−j до 1+j)\n"
    )


def settings_kb(state: RuntimeState) -> InlineKeyboardMarkup:
    fwd_lbl = (
        "📤 С канала: пересылка ✓"
        if state.default_source_forward
        else "📋 С канала: копия текста ✓"
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Стандартный текст", callback_data="dmsg")],
            [InlineKeyboardButton("🌐 Прокси аккаунта", callback_data="dprox")],
            [InlineKeyboardButton(fwd_lbl, callback_data="dsfw")],
            [
                InlineKeyboardButton(
                    "🔗 Ссылка на пост (все чаты)", callback_data="gspst"
                )
            ],
            [InlineKeyboardButton("✖ Сброс общего поста", callback_data="gsclr")],
            [InlineKeyboardButton("⏱ Стандартный интервал", callback_data="dint")],
            [InlineKeyboardButton("🎲 Рандом интервала (±%)", callback_data="djit")],
            [InlineKeyboardButton("↩ Сброс рандома", callback_data="djitr")],
            [InlineKeyboardButton("◀ В меню", callback_data="home")],
        ]
    )


def channels_kb(
    channels: list[tuple[int, str]], page: int, dst_cid: int
) -> InlineKeyboardMarkup:
    pp = 8
    pages = max(1, (len(channels) + pp - 1) // pp)
    page = max(0, min(page, pages - 1))
    chunk = channels[page * pp : page * pp + pp]
    rows: list[list[InlineKeyboardButton]] = []
    for ch_id, title in chunk:
        t = (title or "")[:30] + ("…" if len(title or "") > 30 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    t, callback_data=f"cs_{dst_cid}_{ch_id}"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("«", callback_data=f"clp{page - 1}_{dst_cid}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("»", callback_data=f"clp{page + 1}_{dst_cid}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("◀ Назад к чату", callback_data=f"opn{dst_cid}")])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: RuntimeState = context.bot_data["state"]
    admins: set[int] = context.bot_data["admin_ids"]
    if not _is_admin_m(update, admins):
        uid = update.effective_user.id if update.effective_user else None
        if not admins and uid is not None:
            await update.message.reply_text(
                "Бот запущен без списка админов. Добавьте в <code>.env</code>:\n"
                f"<code>ADMIN_USER_IDS={uid}</code>\n"
                "и перезапустите скрипт.",
                parse_mode=PARSE,
            )
        else:
            await update.message.reply_text("Нет доступа.")
        return
    context.user_data.clear()
    on = await _telethon_ready(context.bot_data.get("telethon_client"))
    text, kb = await _home_text_kb(state, on, context)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=PARSE)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    state: RuntimeState = context.bot_data["state"]
    admins: set[int] = context.bot_data["admin_ids"]
    tc = context.bot_data.get("telethon_client")

    if not _is_admin_q(q, admins):
        await q.answer("Нет доступа.", show_alert=True)
        return

    data = (q.data or "").strip()
    online = await _telethon_ready(tc)

    if data == "accpick":
        multi = context.bot_data["multi"]
        order = multi.account_order
        clients = context.bot_data["telethon_clients"]
        pick_body = await _accounts_pick_text_async(multi, order, clients)
        await _ea(
            q,
            text=pick_body,
            reply_markup=_accounts_pick_kb(order, multi.active_account_id),
            parse_mode=PARSE,
        )
        return

    if data == "accadd":
        context.user_data["wait"] = ("accadd",)
        await q.answer()
        await q.edit_message_text(
            "Введите <b>id нового аккаунта</b> латиницей: первая буква, далее буквы, цифры, "
            "<code>_</code> и <code>-</code>, до <b>32</b> символов.\n"
            "Пример: <code>shop2</code>\n\n"
            "Сессия: <code>sessions/&lt;id&gt;.session</code> (кроме перенесённого "
            "<code>default</code> с <code>userbot_session.session</code> в корне).\n\n"
            "Дальше откройте <b>Аккаунты</b> и <b>нажмите этот слот</b> — запросит номер, код и пароль.\n"
            "<i>Перезапуск не нужен.</i>",
            parse_mode=PARSE,
        )
        return

    if data == "accdelmenu":
        multi = context.bot_data["multi"]
        if not multi.account_order:
            await q.answer("Слотов нет.", show_alert=True)
            return
        await _ea(
            q,
            text=(
                "<b>Удаление слота</b>\n"
                "Выберите слот для удаления.\n"
                "Будут удалены: настройки слота, подключение и файл сессии Telethon."
            ),
            reply_markup=_accounts_delete_kb(multi.account_order, multi.active_account_id),
            parse_mode=PARSE,
        )
        return

    m_acc_del_ask = re.match(r"^accdelask:(.+)$", data)
    if m_acc_del_ask:
        aid = m_acc_del_ask.group(1).strip()
        multi = context.bot_data["multi"]
        if aid not in multi.accounts:
            await q.answer("Нет такого слота.", show_alert=True)
            return
        if len(multi.accounts) <= 1:
            await q.answer("Нельзя удалить последний слот.", show_alert=True)
            return
        await _ea(
            q,
            text=(
                f"Удалить слот <code>{_esc(aid)}</code>?\n"
                "Действие необратимо: удалим его настройки и сессию."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Да, удалить", callback_data=f"accdelok:{aid}")],
                    [InlineKeyboardButton("◀ Назад", callback_data="accdelmenu")],
                ]
            ),
            parse_mode=PARSE,
        )
        return

    m_acc_del_ok = re.match(r"^accdelok:(.+)$", data)
    if m_acc_del_ok:
        aid = m_acc_del_ok.group(1).strip()
        multi = context.bot_data["multi"]
        if aid not in multi.accounts:
            await q.answer("Слот уже удалён.", show_alert=True)
            return
        if len(multi.accounts) <= 1:
            await q.answer("Нельзя удалить последний слот.", show_alert=True)
            return

        clients: dict = context.bot_data["telethon_clients"]
        client = clients.pop(aid, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("Ошибка при disconnect слота %s", aid, exc_info=True)

        st_del = multi.accounts.get(aid)
        if st_del is not None:
            st_del.spam_running = False
        multi.accounts.pop(aid, None)
        multi.account_order = [x for x in multi.account_order if x != aid]
        _delete_session_files(aid)

        if multi.active_account_id == aid:
            for cand in multi.account_order:
                if cand in multi.accounts:
                    multi.active_account_id = cand
                    break
        if multi.active_account_id not in multi.accounts:
            await q.answer("Нет доступного активного слота.", show_alert=True)
            return

        new_active = multi.active_account_id
        context.bot_data["state"] = multi.accounts[new_active]
        context.bot_data["telethon_client"] = clients.get(new_active)

        w = context.user_data.get("wait")
        if isinstance(w, tuple) and w and w[0].startswith("tauth") and len(w) > 1:
            if str(w[1]) == aid:
                context.user_data.pop("wait", None)

        _persist(context)
        on_new = await _telethon_ready(context.bot_data.get("telethon_client"))
        text_home, kb_home = await _home_text_kb(context.bot_data["state"], on_new, context)
        await q.answer(f"Слот {aid} удалён.")
        await _edit_cb_message(
            q,
            text=text_home,
            reply_markup=kb_home,
            parse_mode=PARSE,
        )
        return

    m_acc = re.match(r"^acc:(.+)$", data)
    if m_acc:
        new_id = m_acc.group(1).strip()
        multi = context.bot_data["multi"]
        if new_id not in multi.accounts:
            await q.answer("Нет такого слота.", show_alert=True)
            return
        clients: dict = context.bot_data["telethon_clients"]
        api_id = API_CONFIG.api_id
        api_hash = API_CONFIG.api_hash
        client = clients.get(new_id)
        created = client is None
        if created:
            client = make_telethon_client(
                new_id,
                api_id,
                api_hash,
                proxy_raw=multi.accounts[new_id].proxy,
                profile=API_CONFIG,
            )
        try:
            client = await connect_client_with_fallback(
                client,
                account_id=new_id,
                api_id=api_id,
                api_hash=api_hash,
                proxy_raw=multi.accounts[new_id].proxy,
                allow_direct_fallback=False,
                profile=API_CONFIG,
            )
        except Exception as e:
            ok = await _answer_callback_resilient(
                q,
                text=f"Не удалось подключить слот {new_id}",
                show_alert=True,
            )
            if not ok:
                await _notify_cb_fail(
                    q,
                    f"Не удалось подключить слот {new_id}: {_esc(e)}",
                    emphasis=True,
                )
            return
        clients[new_id] = client
        restart_spam_loop_background(
            client,
            multi.accounts[new_id],
            persist=lambda: save_multi_account_state(multi),
            account_key=new_id,
        )
        if not await client.is_user_authorized():
            context.user_data["wait"] = ("tauth_phone", new_id)
            await q.answer()
            await q.edit_message_text(
                f"<b>Вход</b> · слот <code>{_esc(new_id)}</code>\n\n"
                "Шаг 1: пришлите <b>номер</b> (<code>+7900…</code> или <code>8900…</code>).\n"
                "Потом — <b>код</b> из Telegram; если спросит — <b>пароль 2FA</b>.",
                parse_mode=PARSE,
            )
            return
        multi.active_account_id = new_id
        context.bot_data["state"] = multi.accounts[new_id]
        context.bot_data["telethon_client"] = client
        _persist(context)
        context.user_data.clear()
        state = context.bot_data["state"]
        online = await _telethon_ready(client)
        t, kb = await _home_text_kb(state, online, context)
        await q.answer(f"Слот: {new_id}")
        await _edit_cb_message(q, text=t, reply_markup=kb, parse_mode=PARSE)
        return

    if data == "home":
        context.user_data.clear()
        t, kb = await _home_text_kb(state, online, context)
        await _ea(
            q,
            text=t,
            reply_markup=kb,
            parse_mode=PARSE,
        )
        return

    if data == "spam":
        if state.spam_running:
            state.spam_running = False
            _persist(context)
            await q.answer("Спам остановлен.")
            t0, kb0 = await _home_text_kb(state, online, context)
            await _edit_cb_message(
                q,
                text=t0,
                reply_markup=kb0,
                parse_mode=PARSE,
            )
            return
        ok, err = validate_spam_start(state, online)
        if not ok:
            await q.answer(_plain_alert(err), show_alert=True)
            return
        state.spam_running = True
        _persist(context)
        multi = context.bot_data["multi"]
        aid = multi.active_account_id
        spam_client = context.bot_data["telethon_clients"].get(aid) or tc
        if spam_client is not None and aid:
            restart_spam_loop_background(
                spam_client,
                state,
                persist=lambda: save_multi_account_state(multi),
                account_key=aid,
            )
        await q.answer("Спам запущен.")
        t1, kb1 = await _home_text_kb(state, online, context)
        await _edit_cb_message(
            q,
            text=t1,
            reply_markup=kb1,
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^jinv(\d+)$", data)
    if m:
        if not online or tc is None:
            await q.answer("Userbot офлайн.", show_alert=True)
            return
        page = int(m.group(1))
        context.user_data["wait"] = ("jinv", page)
        await q.answer()
        await q.edit_message_text(
            "<b>Вступить по ссылке</b> (текущий слот userbot).\n"
            "Пришлите одним сообщением:\n"
            "• приватное приглашение <code>https://t.me/+…</code>\n"
            "• или <code>https://t.me/joinchat/…</code>\n"
            "• или публичный канал/группа <code>https://t.me/username</code>\n\n"
            "Потом откройте <b>Чаты</b> и при необходимости 🔄.",
            parse_mode=PARSE,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К списку чатов", callback_data=f"jinvc{page}")]]
            ),
        )
        return

    m = re.match(r"^jinvc(\d+)$", data)
    if m:
        page = int(m.group(1))
        w = context.user_data.get("wait")
        if isinstance(w, tuple) and len(w) >= 1 and w[0] == "jinv":
            context.user_data.pop("wait", None)
        if not online or tc is None:
            await q.answer("Userbot офлайн.", show_alert=True)
            return
        await q.answer()
        try:
            rows = await _get_group_chats_cached(context, force=False)
        except Exception:
            logger.exception("list_group_chats")
            await _notify_cb_fail(
                q, "Не удалось загрузить диалоги.", emphasis=True
            )
            return
        await _edit_cb_message(
            q,
            text=f"{_chats_intro_long(context)}\n\nВсего: {len(rows)}.",
            reply_markup=chats_kb(rows, page, state),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^r?chats(\d+)$", data)
    if m:
        if not online or tc is None:
            await q.answer("Userbot офлайн — список недоступен.", show_alert=True)
            return
        page = int(m.group(1))
        await q.answer()
        try:
            rows = await _get_group_chats_cached(
                context, force=data.startswith("rchats")
            )
        except Exception:
            logger.exception("list_group_chats")
            await _notify_cb_fail(
                q, "Не удалось загрузить диалоги (сеть / Flood).", emphasis=True
            )
            return
        await _edit_cb_message(
            q,
            text=f"{_chats_intro_long(context)}\n\nВсего: {len(rows)}.",
            reply_markup=chats_kb(rows, page, state),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^a_(on|off)_(\d+)$", data)
    if m:
        if not online or tc is None:
            await q.answer("Userbot офлайн — список недоступен.", show_alert=True)
            return
        page = int(m.group(2))
        want_on = m.group(1) == "on"
        await q.answer()
        try:
            rows = await _get_group_chats_cached(context)
        except Exception:
            logger.exception("list_group_chats (all toggle)")
            await _notify_cb_fail(
                q, "Не удалось загрузить диалоги (сеть / Flood).", emphasis=True
            )
            return
        for cid, _title in rows:
            c = state.cfg(cid)
            c.enabled = want_on
            state.set_cfg(cid, c)
        _persist(context)
        await _edit_cb_message(
            q,
            text=f"{_chats_intro_long(context)}\n\nВсего: {len(rows)}.",
            reply_markup=chats_kb(rows, page, state),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^T(\d+)_(-?\d+)$", data)
    if m:
        page, cid = int(m.group(1)), int(m.group(2))
        c = state.cfg(cid)
        c.enabled = not c.enabled
        state.set_cfg(cid, c)
        _persist(context)
        await q.answer()
        rows: list[tuple[int, str]] = []
        if online and tc is not None:
            try:
                rows = await _get_group_chats_cached(context)
            except Exception:
                logger.exception("list_group_chats (toggle)")
                await _notify_cb_fail(q, "Не удалось обновить список.", emphasis=True)
                return
        await _edit_cb_message(
            q,
            text=f"{_chats_intro_short(context)}\n\nВсего: {len(rows)}.",
            reply_markup=chats_kb(rows, page, state),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^B(\d+)_(-?\d+)$", data)
    if m:
        page, cid = int(m.group(1)), int(m.group(2))
        context.user_data["chats_back"] = page
        await q.answer()
        txt_b = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_b,
            reply_markup=chat_settings_kb(cid, state, page),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^opn(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_opn = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_opn,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^ten(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.enabled = not c.enabled
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_ten = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_ten,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cmsg(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("cmsg", cid)
        await q.answer()
        await q.edit_message_text(
            f"Пришлите <b>кастомный текст</b> для чата <code>{cid}</code> одним сообщением "
            f"(или «-» чтобы очистить).",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cint(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("cint", cid)
        await q.answer()
        await q.edit_message_text(
            f"Пришлите интервал в <b>минутах</b> для чата <code>{cid}</code> (число, или «-» для стандарта).",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cext(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("cext", cid)
        await q.answer()
        await q.edit_message_text(
            f"Пришлите <b>дополнительный текст</b> (суффикс) для <code>{cid}</code> или «-» чтобы убрать.",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cmsgr(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.custom_message = None
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_cmsgr = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_cmsgr,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cintr(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.custom_interval_min = None
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_cintr = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_cintr,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cspt(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("cspt", cid)
        await q.answer()
        await q.edit_message_text(
            f"Чат <code>{cid}</code>: пришлите <b>ссылку на сообщение</b> (копировать из Telegram):\n\n"
            "<code>https://t.me/username/123</code>\n"
            "<code>https://t.me/c/1234567890/123</code>\n\n"
            "Будет использоваться <b>этот</b> пост, а не последний. Режим пересылка/копия — кнопкой «Режим».",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cclr(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        if c.source_message_id is None:
            await q.answer("Уже выбран последний пост канала.", show_alert=True)
            return
        c.source_message_id = None
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer("Теперь источник — последний пост.")
        txt_cclr = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_cclr,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cfwd(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.source_forward = not c.source_forward
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer("Режим сохранён.")
        txt_cfwd = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_cfwd,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cclc(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.source_channel_id = None
        c.source_message_id = None
        c.source_forward = False
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_cclc = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_cclc,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^chid(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("chid", cid)
        await q.answer()
        await q.edit_message_text(
            f"Пришлите числовой <code>id</code> канала (например <code>-100...</code>) для чата <code>{cid}</code>.",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^clp(\d+)_(-?\d+)$", data)
    if m:
        page, dst = int(m.group(1)), int(m.group(2))
        if not online or tc is None:
            await q.answer("Офлайн.", show_alert=True)
            return
        await q.answer()
        try:
            chs = await list_broadcast_channels(tc)
        except Exception:
            logger.exception("list_broadcast_channels")
            await _notify_cb_fail(q, "Не удалось загрузить каналы.", emphasis=True)
            return
        if not chs:
            await _notify_cb_fail(
                q, "Каналы не найдены (нужна подписка userbot).", emphasis=True
            )
            return
        await _edit_cb_message(
            q,
            text=f"<b>Выбор канала</b> для чата <code>{dst}</code>:\n"
            "По умолчанию — <b>последний пост</b>. Конкретное сообщение — кнопка "
            "<b>«Ссылка на пост»</b>. Режим пересылка/копия — «Режим».",
            reply_markup=channels_kb(chs, page, dst),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cs_(-?\d+)_(-?\d+)$", data)
    if m:
        dst, src = int(m.group(1)), int(m.group(2))
        c = state.cfg(dst)
        c.source_channel_id = src
        c.source_message_id = None
        c.source_forward = state.default_source_forward
        state.set_cfg(dst, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer("Канал привязан.")
        txt_cs = await chat_settings_text_async(state, dst, context)
        await _edit_cb_message(
            q,
            text=txt_cs,
            reply_markup=chat_settings_kb(dst, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cjit(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("cjit", cid)
        await q.answer()
        await q.edit_message_text(
            f"Чат <code>{cid}</code>: введите разброс <b>в процентах</b> (например <code>40</code> = ±40% к паузе) "
            "или дробь <code>0.35</code>.\n<code>0</code> — без рандома для этого чата.\n"
            "«-» — брать как в «Настройки».",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cjitr(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.custom_interval_jitter = None
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_cjitr = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_cjitr,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cvrs(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("cvrs", cid)
        await q.answer()
        await q.edit_message_text(
            f"Чат <code>{cid}</code>: несколько <b>разных текстов</b> одним сообщением — "
            "при рассылке каждый раз случайный.\n"
            "Разделитель — строка только из трёх дефисов:\n<code>---</code>\n\n"
            "«-» одним символом — очистить.",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^cvrsr(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.text_variants = []
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_cvrsr = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_cvrsr,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^clim(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("clim", cid)
        await q.answer()
        await q.edit_message_text(
            f"Чат <code>{cid}</code>: лимит <b>числа отправок</b> (целое &gt; 0).\n"
            "После достижения чат сам выключится из рассылки.\n"
            "«-» — без лимита.",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^climr(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.message_limit = None
        c.messages_sent = 0
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_climr = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_climr,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^csdl(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        context.user_data["wait"] = ("csdl", cid)
        await q.answer()
        await q.edit_message_text(
            f"Чат <code>{cid}</code>: <b>задержка первой отправки</b> после «Запустить спам» (минуты).\n"
            "<code>0</code> или «-» — без задержки.",
            parse_mode=PARSE,
        )
        return

    m = re.match(r"^csdlr(-?\d+)$", data)
    if m:
        cid = int(m.group(1))
        c = state.cfg(cid)
        c.start_delay_min = None
        state.set_cfg(cid, c)
        _persist(context)
        bp = int(context.user_data.get("chats_back", 0))
        await q.answer()
        txt_csdlr = await chat_settings_text_async(state, cid, context)
        await _edit_cb_message(
            q,
            text=txt_csdlr,
            reply_markup=chat_settings_kb(cid, state, bp),
            parse_mode=PARSE,
        )
        return

    if data == "settings":
        sid = context.bot_data["multi"].active_account_id
        await _ea(
            q,
            text=settings_text(state, slot_id=sid),
            reply_markup=settings_kb(state),
            parse_mode=PARSE,
        )
        return

    if data == "djitr":
        state.default_interval_jitter = 0.0
        _persist(context)
        sid = context.bot_data["multi"].active_account_id
        await _ea(
            q,
            text=settings_text(state, slot_id=sid),
            reply_markup=settings_kb(state),
            parse_mode=PARSE,
        )
        return

    if data == "dsfw":
        state.default_source_forward = not state.default_source_forward
        _persist(context)
        sid = context.bot_data["multi"].active_account_id
        await _ea(
            q,
            text=settings_text(state, slot_id=sid),
            reply_markup=settings_kb(state),
            parse_mode=PARSE,
        )
        return

    if data == "gspst":
        if not online or tc is None:
            await q.answer("Userbot офлайн.", show_alert=True)
            return
        context.user_data["wait"] = ("gspst",)
        await q.answer()
        await q.edit_message_text(
            "<b>Ссылка на пост для всех чатов</b> (текущий слот).\n"
            "Чаты, у которых в <b>Чаты → ⚙</b> <b>не</b> задан свой канал-источник, "
            "будут использовать этот пост.\n\n"
            "Пришлите ссылку на сообщение:\n"
            "<code>https://t.me/username/123</code>\n"
            "<code>https://t.me/c/1234567890/123</code>\n\n"
            "<i>Пересылка или копия — в Настройках, кнопка «С канала».</i>",
            parse_mode=PARSE,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ Настройки", callback_data="settings")]]
            ),
        )
        return

    if data == "gsclr":
        if state.global_source_channel_id is None:
            await q.answer("Общий пост не задан.", show_alert=True)
            return
        state.global_source_channel_id = None
        state.global_source_message_id = None
        _persist(context)
        sid = context.bot_data["multi"].active_account_id
        await _ea(
            q,
            text=settings_text(state, slot_id=sid),
            reply_markup=settings_kb(state),
            parse_mode=PARSE,
        )
        return

    if data == "djit":
        context.user_data["wait"] = ("djit",)
        await q.answer()
        await q.edit_message_text(
            "Введите <b>±%</b> к длительности паузы (целое 0–95, напр. <code>30</code> = ±30%).\n"
            "Или дробь <code>0.2</code> = ±20%.\n<code>0</code> — выключить рандом.",
            parse_mode=PARSE,
        )
        return

    if data == "dmsg":
        context.user_data["wait"] = ("dmsg",)
        await q.answer()
        await q.edit_message_text(
            "Пришлите <b>стандартный текст</b> для всех чатов одним сообщением.",
            parse_mode=PARSE,
        )
        return

    if data == "dprox":
        context.user_data["wait"] = ("dprox",)
        await q.answer()
        await q.edit_message_text(
            "Пришлите прокси для <b>этого аккаунта</b>:\n"
            "<code>логин:пароль@ip:порт</code> или <code>ip:порт</code>\n"
            "Можно также URL: <code>socks5://...</code> / <code>http://...</code>\n\n"
            "Отправьте <code>-</code>, чтобы убрать прокси.",
            parse_mode=PARSE,
        )
        return

    if data == "dint":
        context.user_data["wait"] = ("dint",)
        await q.answer()
        await q.edit_message_text(
            "Пришлите <b>стандартный интервал</b> в минутах (число, можно с точкой).",
            parse_mode=PARSE,
        )
        return

    await q.answer()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admins: set[int] = context.bot_data["admin_ids"]
    if not _is_admin_m(update, admins):
        return
    w = context.user_data.get("wait")
    if not w:
        return
    state: RuntimeState = context.bot_data["state"]
    text = (update.message.text or "").strip()
    kind = w[0]

    if kind == "tauth_phone":
        from telethon.errors import (
            FloodWaitError,
            PhoneNumberInvalidError,
            SendCodeUnavailableError,
        )

        aid = str(w[1])
        phone = _normalize_phone(text)
        if not phone:
            await update.message.reply_text(
                "Нужен номер с кодом страны, например <code>+79001234567</code> или 8900...",
                parse_mode=PARSE,
            )
            return
        multi = context.bot_data["multi"]
        clients: dict = context.bot_data["telethon_clients"]
        client = clients.get(aid)
        api_id = API_CONFIG.api_id
        api_hash = API_CONFIG.api_hash
        if client is None:
            client = make_telethon_client(
                aid,
                api_id,
                api_hash,
                proxy_raw=multi.accounts[aid].proxy,
                profile=API_CONFIG,
            )
        try:
            client = await connect_client_with_fallback(
                client,
                account_id=aid,
                api_id=api_id,
                api_hash=api_hash,
                proxy_raw=multi.accounts[aid].proxy,
                allow_direct_fallback=False,
                profile=API_CONFIG,
            )
        except Exception as e:
            await update.message.reply_text(
                f"Не удалось подключить слот <code>{_esc(aid)}</code>: {_esc(e)}.\n"
                "Проверьте прокси этого слота в Настройки.",
                parse_mode=PARSE,
            )
            return
        clients[aid] = client
        try:
            sent = await request_login_code(client, phone)
        except PhoneNumberInvalidError:
            await update.message.reply_text("Неверный номер телефона.")
            return
        except SendCodeUnavailableError:
            logger.warning("send_code_request: SendCodeUnavailable для %s", aid)
            await update.message.reply_text(
                "Telegram <b>не может выслать код</b> сейчас: исчерпаны способы доставки "
                "(SMS / звонок и т.п.) или нужна пауза.\n\n"
                "Что сделать: подождите <b>15–60 минут</b>, откройте официальный Telegram на телефоне "
                "(убедитесь, что номер активен), затем снова: <b>Аккаунты</b> → этот слот.\n"
                "Если только что запрашивали код в другом месте — ждите следующей попытки.",
                parse_mode=PARSE,
            )
            return
        except FloodWaitError as e:
            await update.message.reply_text(
                f"Слишком частые запросы. Подождите {getattr(e, 'seconds', 0)} сек."
            )
            return
        except Exception as e:
            logger.exception("send_code_request")
            await update.message.reply_text(f"Ошибка отправки кода: {e}")
            return
        pch = getattr(sent, "phone_code_hash", None) or ""
        context.user_data["wait"] = ("tauth_code", aid, phone, pch)
        await update.message.reply_text(
            "Код отправлен в Telegram. Введите <b>код</b> (цифры из SMS или приложения).",
            parse_mode=PARSE,
        )
        return

    if kind == "tauth_code":
        from telethon.errors import (
            FloodWaitError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )

        aid, phone, pch = str(w[1]), str(w[2]), str(w[3])
        clients = context.bot_data["telethon_clients"]
        client = clients.get(aid)
        if client is None:
            context.user_data.pop("wait", None)
            await update.message.reply_text("Сессия потеряна. Начните вход заново.")
            return
        code = re.sub(r"\s+", "", text.strip())
        try:
            await client.sign_in(phone, code, phone_code_hash=pch)
        except PhoneCodeInvalidError:
            await update.message.reply_text("Неверный код. Введите ещё раз.")
            return
        except PhoneCodeExpiredError:
            context.user_data.pop("wait", None)
            await update.message.reply_text(
                "Код истёк. Откройте <b>Аккаунты</b> и снова нажмите этот слот."
            )
            return
        except SessionPasswordNeededError:
            context.user_data["wait"] = ("tauth_pwd", aid)
            await update.message.reply_text(
                "Введите <b>пароль двухэтапной аутентификации</b> Telegram (облачный пароль).",
                parse_mode=PARSE,
            )
            return
        except FloodWaitError as e:
            await update.message.reply_text(
                f"Подождите {getattr(e, 'seconds', 0)} сек и попробуйте снова."
            )
            return
        except Exception as e:
            logger.exception("sign_in code")
            await update.message.reply_text(f"Ошибка входа: {e}")
            return
        context.user_data.pop("wait", None)
        await _after_telethon_sign_in(update, context, aid, client)
        return

    if kind == "tauth_pwd":
        from telethon.errors import FloodWaitError, PasswordHashInvalidError

        aid = str(w[1])
        clients = context.bot_data["telethon_clients"]
        client = clients.get(aid)
        if client is None:
            context.user_data.pop("wait", None)
            await update.message.reply_text("Сессия потеряна. Начните вход заново.")
            return
        try:
            await client.sign_in(password=text)
        except PasswordHashInvalidError:
            await update.message.reply_text("Неверный пароль. Введите снова.")
            return
        except FloodWaitError as e:
            await update.message.reply_text(
                f"Подождите {getattr(e, 'seconds', 0)} сек."
            )
            return
        except Exception as e:
            logger.exception("sign_in password")
            await update.message.reply_text(f"Ошибка: {e}")
            return
        context.user_data.pop("wait", None)
        await _after_telethon_sign_in(update, context, aid, client)
        return

    if kind == "accadd":
        new_id = _parse_new_account_id(text)
        if not new_id:
            await update.message.reply_text(
                "Неверный id: латиница, первая — буква, далее буквы/цифры/_/-, до 32 символов."
            )
            return
        multi = context.bot_data["multi"]
        if new_id in multi.accounts:
            await update.message.reply_text(
                f"Аккаунт <code>{_esc(new_id)}</code> уже существует.",
                parse_mode=PARSE,
            )
            return
        multi.accounts[new_id] = RuntimeState()
        if new_id not in multi.account_order:
            multi.account_order.append(new_id)
        _persist(context)
        clients: dict = context.bot_data["telethon_clients"]
        if new_id not in clients:
            api_id = API_CONFIG.api_id
            api_hash = API_CONFIG.api_hash
            nc = make_telethon_client(
                new_id,
                api_id,
                api_hash,
                proxy_raw=multi.accounts[new_id].proxy,
                profile=API_CONFIG,
            )
            try:
                nc = await connect_client_with_fallback(
                    nc,
                    account_id=new_id,
                    api_id=api_id,
                    api_hash=api_hash,
                    proxy_raw=multi.accounts[new_id].proxy,
                    allow_direct_fallback=False,
                    profile=API_CONFIG,
                )
            except Exception as e:
                context.user_data.pop("wait", None)
                await update.message.reply_text(
                    f"Слот <code>{_esc(new_id)}</code> добавлен, но подключение не удалось: {_esc(e)}.\n"
                    "Исправьте прокси в Настройки и нажмите слот в Аккаунты ещё раз.",
                    parse_mode=PARSE,
                )
                return
            clients[new_id] = nc
            restart_spam_loop_background(
                nc,
                multi.accounts[new_id],
                persist=lambda: save_multi_account_state(multi),
                account_key=new_id,
            )
            logger.info("Новый слот %s: клиент подключён, spam_loop запланирован.", new_id)
        context.user_data.pop("wait", None)
        st = context.bot_data["state"]
        on = await _telethon_ready(context.bot_data.get("telethon_client"))
        ht, kb = await _home_text_kb(st, on, context)
        await update.message.reply_text(
            f"Слот <code>{_esc(new_id)}</code> добавлен (свои чаты и настройки только в нём).\n"
            "<b>Аккаунты</b> → нажмите слот — введите номер, код, при необходимости пароль 2FA.\n\n"
            f"{ht}",
            reply_markup=kb,
            parse_mode=PARSE,
        )
        return

    async def back_main():
        context.user_data.pop("wait", None)
        on = await _telethon_ready(context.bot_data.get("telethon_client"))
        st = context.bot_data["state"]
        ht, kb = await _home_text_kb(st, on, context)
        await update.message.reply_text(ht, reply_markup=kb, parse_mode=PARSE)

    if kind == "gspst":
        tc = context.bot_data.get("telethon_client")
        try:
            ok_g = (
                tc is not None
                and tc.is_connected()
                and await tc.is_user_authorized()
            )
        except Exception:
            ok_g = False
        if not ok_g:
            context.user_data.pop("wait", None)
            await update.message.reply_text("Userbot офлайн или нет авторизации.")
            return
        pair = await resolve_tme_post_for_forward(tc, text)
        if not pair:
            await update.message.reply_text(
                "Не вышло. Нужна ссылка на сообщение "
                "<code>t.me/канал/123</code> или <code>t.me/c/…/123</code>; "
                "аккаунт должен <b>видеть</b> тот чат.",
                parse_mode=PARSE,
            )
            return
        pid, mid = pair
        state.global_source_channel_id = pid
        state.global_source_message_id = mid
        _persist(context)
        context.user_data.pop("wait", None)
        rep = "пересылка" if state.default_source_forward else "копия текста"
        await update.message.reply_text(
            f"Общий пост для всех чатов сохранён: peer <code>{pid}</code>, id <code>{mid}</code>. "
            f"Режим: <b>{rep}</b> (кнопка «С канала» здесь). "
            "Если у чата в ⚙ задан свой канал — используется он.",
            parse_mode=PARSE,
            reply_markup=settings_kb(state),
        )
        return

    if kind == "dmsg":
        state.default_message = text if text != "-" else ""
        _persist(context)
        await back_main()
        return

    if kind == "dprox":
        if text == "-":
            state.proxy = None
        else:
            px = text.strip()
            if parse_proxy(px) is None:
                await update.message.reply_text(
                    "Неверный формат прокси. Пример: <code>user:pass@1.2.3.4:1080</code> "
                    "или <code>1.2.3.4:1080</code>.",
                    parse_mode=PARSE,
                )
                return
            state.proxy = px
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Прокси сохранён для активного слота. Применится при новом подключении аккаунта.",
            reply_markup=settings_kb(state),
            parse_mode=PARSE,
        )
        return

    if kind == "dint":
        try:
            v = float(text.replace(",", "."))
            if v <= 0:
                raise ValueError
            state.default_interval_min = v
            _persist(context)
        except ValueError:
            await update.message.reply_text("Нужно положительное число (минуты).")
            return
        await back_main()
        return

    if kind == "djit":
        try:
            state.default_interval_jitter = _parse_jitter_user(text)
            _persist(context)
        except ValueError:
            await update.message.reply_text("Нужно число: проценты или дробь 0–1.")
            return
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Сохранено.",
            reply_markup=settings_kb(state),
        )
        return

    if kind == "cjit":
        cid = int(w[1])
        c = state.cfg(cid)
        if text.strip() == "-":
            c.custom_interval_jitter = None
        else:
            try:
                c.custom_interval_jitter = _parse_jitter_user(text)
            except ValueError:
                await update.message.reply_text("Нужно число, «0» или дробь; «-» сброс.")
                return
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Сохранено.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
        )
        return

    if kind == "cmsg":
        cid = int(w[1])
        c = state.cfg(cid)
        c.custom_message = None if text == "-" else text
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Сохранено.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "◀ К настройкам чата",
                            callback_data=f"opn{cid}",
                        )
                    ]
                ]
            ),
        )
        return

    if kind == "cint":
        cid = int(w[1])
        c = state.cfg(cid)
        if text == "-":
            c.custom_interval_min = None
        else:
            try:
                v = float(text.replace(",", "."))
                if v <= 0:
                    raise ValueError
                c.custom_interval_min = v
            except ValueError:
                await update.message.reply_text("Нужно положительное число или «-».")
                return
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Сохранено.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
        )
        return

    if kind == "cext":
        cid = int(w[1])
        c = state.cfg(cid)
        c.extra_text = "" if text == "-" else text
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Сохранено.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
        )
        return

    if kind == "chid":
        cid = int(w[1])
        try:
            chid = int(text.replace(" ", ""))
        except ValueError:
            await update.message.reply_text("Нужно целое число id канала.")
            return
        c = state.cfg(cid)
        c.source_channel_id = chid
        c.source_message_id = None
        c.source_forward = state.default_source_forward
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        rep_mode = "пересылка" if state.default_source_forward else "копия текста"
        await update.message.reply_text(
            f"Канал привязан. Режим из «Настройки»: <b>{rep_mode}</b> последнего поста. "
            "Конкретный пост — «Ссылка на пост» в настройках чата.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
            parse_mode=PARSE,
        )
        return

    if kind == "cspt":
        cid = int(w[1])
        tc = context.bot_data.get("telethon_client")
        try:
            ok_cspt = (
                tc is not None
                and tc.is_connected()
                and await tc.is_user_authorized()
            )
        except Exception:
            ok_cspt = False
        if not ok_cspt:
            context.user_data.pop("wait", None)
            await update.message.reply_text("Userbot офлайн или нет авторизации.")
            return
        pair = await resolve_tme_post_for_forward(tc, text)
        if not pair:
            await update.message.reply_text(
                "Не вышло. Нужна ссылка вида <code>t.me/канал/123</code> или "
                "<code>t.me/c/…/123</code>, и этот аккаунт должен <b>видеть</b> тот чат; "
                "пост не должен быть удалён.",
                parse_mode=PARSE,
            )
            return
        pid, mid = pair
        c = state.cfg(cid)
        c.source_channel_id = pid
        c.source_message_id = mid
        c.source_forward = state.default_source_forward
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        rep_mode = "пересылка" if state.default_source_forward else "копия текста"
        await update.message.reply_text(
            f"Пост <code>{mid}</code> из чата <code>{pid}</code> привязан. "
            f"Режим как в «Настройки»: <b>{rep_mode}</b> (в чате можно переключить).",
            parse_mode=PARSE,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
        )
        return

    if kind == "cvrs":
        cid = int(w[1])
        variants = _parse_text_variants(text)
        c = state.cfg(cid)
        c.text_variants = variants
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            f"Сохранено текстов: {len(variants)}." if variants else "Случайные тексты убраны.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
        )
        return

    if kind == "clim":
        cid = int(w[1])
        c = state.cfg(cid)
        if text == "-":
            c.message_limit = None
        else:
            try:
                v = int(text.replace(" ", ""))
                if v <= 0:
                    raise ValueError
                c.message_limit = v
            except ValueError:
                await update.message.reply_text("Нужно целое число > 0 или «-».")
                return
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Сохранено.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
        )
        return

    if kind == "csdl":
        cid = int(w[1])
        c = state.cfg(cid)
        if text in ("-", ""):
            c.start_delay_min = None
        else:
            try:
                v = float(text.replace(",", "."))
                if v < 0:
                    raise ValueError
                c.start_delay_min = None if v == 0 else v
            except ValueError:
                await update.message.reply_text("Нужно число минут ≥ 0, «-» или 0.")
                return
        state.set_cfg(cid, c)
        _persist(context)
        context.user_data.pop("wait", None)
        await update.message.reply_text(
            "Сохранено.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К чату", callback_data=f"opn{cid}")]]
            ),
        )
        return

    if kind == "jinv":
        page = int(w[1])
        tc = context.bot_data.get("telethon_client")
        try:
            online_j = (
                tc is not None
                and tc.is_connected()
                and await tc.is_user_authorized()
            )
        except Exception:
            online_j = False
        if not online_j:
            context.user_data.pop("wait", None)
            await update.message.reply_text("Userbot офлайн или нет авторизации.")
            return
        _peer_id, msg = await join_chat_or_channel_by_link(tc, text)
        context.user_data.pop("wait", None)
        setattr(tc, _TG_GROUP_CHATS_CACHE, None)
        try:
            rows = await _get_group_chats_cached(context, force=True)
        except Exception:
            logger.exception("list_group_chats после вступления")
            rows = []
        await update.message.reply_text(
            msg,
            parse_mode=PARSE,
            reply_markup=chats_kb(rows, page, state),
        )
        return


def build_application(
    multi: MultiAccountState,
    token: str,
    telethon_clients: dict,
    *,
    boot_account_ids: frozenset[str],
    authorized_at_boot: frozenset[str],
) -> Application:
    state = multi.accounts[multi.active_account_id]
    telethon_client = telethon_clients[multi.active_account_id]
    req = HTTPXRequest(
        connect_timeout=_BOT_CONNECT_TIMEOUT,
        read_timeout=_BOT_READ_TIMEOUT,
        write_timeout=_BOT_WRITE_TIMEOUT,
        pool_timeout=_BOT_POOL_TIMEOUT,
        media_write_timeout=max(60.0, _BOT_WRITE_TIMEOUT),
        httpx_kwargs={"trust_env": False},
    )
    app = Application.builder().token(token).request(req).build()
    app.bot_data["multi"] = multi
    app.bot_data["state"] = state
    app.bot_data["admin_ids"] = _parse_admin_ids()
    app.bot_data["telethon_clients"] = telethon_clients
    app.bot_data["telethon_client"] = telethon_client
    app.bot_data["boot_account_ids"] = boot_account_ids
    app.bot_data["authorized_at_boot"] = authorized_at_boot

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
