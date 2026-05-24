"""Operations layer for the bot HTTP API.

In-process service that mutates the live `MultiAccountState` and
`telethon_clients` dict, so changes apply immediately (spam_scheduler
picks them up on its next tick).

Includes:
  - read: overview / account / chats
  - write: slot CRUD, spam toggle, chat CRUD
  - Telethon auth flow: send_code → sign_in → 2fa
  - upload .session file (operator-supplied)
"""

from __future__ import annotations

import logging
import os
import time as _time
from typing import Any, Callable, Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import health
from control_bot import (
    _ACC_ID_RE,
    _delete_session_files,
    _mask_proxy_display,
    _parse_jitter_user,
    _parse_text_variants,
    _telethon_ready,
)
from proxy_util import parse_proxy
from spam_scheduler import start_spam_loop_background
from state import (
    ChatSpamConfig,
    MultiAccountState,
    RuntimeState,
    save_multi_account_state,
    validate_spam_start,
)
from group_dialogs import list_broadcast_channels, list_group_chats
from telethon_join import resolve_tme_post_for_forward
from telethon_accounts import (
    connect_client_with_fallback,
    make_telethon_client,
    session_path,
)

logger = logging.getLogger(__name__)


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _coerce_int(v: Any, field: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError) as e:
        raise ServiceError(f"{field}: ожидается целое число") from e


def _coerce_float(v: Any, field: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError) as e:
        raise ServiceError(f"{field}: ожидается число") from e


def _normalize_proxy(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s == "-":
        return None
    if parse_proxy(s) is None:
        raise ServiceError(
            "proxy: неверный формат (нужно логин:пароль@host:port, "
            "host:port, socks5://… или http://…)"
        )
    return s


def _compute_health(
    aid: str,
    acc: RuntimeState,
    client,
    *,
    connected: bool,
    authorized: bool,
    enabled_chats: int,
) -> dict[str, Any]:
    snap = health.snapshot(aid) or {}
    last_err_at = snap.get("lastErrorAt") or 0
    last_err = snap.get("lastError") or ""
    last_err_kind = snap.get("lastErrorKind") or ""
    fresh_error = last_err and (_time.time() - last_err_at) < 600

    if client is None:
        code, label, tone = "no_client", "не подключён", "error"
    elif not connected:
        if acc.proxy:
            code, label, tone = "proxy_down", "слетел прокси", "error"
        else:
            code, label, tone = "offline", "нет связи", "error"
    elif not authorized:
        code, label, tone = "no_auth", "умер бот (нет авторизации)", "error"
    elif acc.spam_running and enabled_chats == 0:
        code, label, tone = "no_chats", "нет чатов", "warn"
    elif acc.spam_running:
        code, label, tone = "running", "работает", "ok"
    else:
        code, label, tone = "idle", "остановлен", "warn"

    return {
        "code": code,
        "label": label,
        "tone": tone,
        "lastSendAt": snap.get("lastSendAt") or 0,
        "sendsTotal": snap.get("sendsTotal") or 0,
        "lastErrorAt": last_err_at,
        "lastError": last_err if fresh_error else "",
        "lastErrorKind": last_err_kind if fresh_error else "",
        "errorsTotal": snap.get("errorsTotal") or 0,
    }


class BotService:
    """Service used by the HTTP API. One instance per process."""

    def __init__(
        self,
        multi: MultiAccountState,
        telethon_clients: dict[str, TelegramClient],
        *,
        api_id: int,
        api_hash: str,
        save: Callable[[], None] | None = None,
    ) -> None:
        self.multi = multi
        self.clients = telethon_clients
        self.api_id = api_id
        self.api_hash = api_hash
        self._save = save or (lambda: save_multi_account_state(self.multi))
        # Multi-step Telethon auth state per slot:
        #   {aid: {"phone": str, "hash": str, "client": TelegramClient}}
        self._pending_auth: dict[str, dict[str, Any]] = {}

    # =================================================================== read
    def _summarize_account(self, aid: str, acc: RuntimeState) -> dict[str, Any]:
        chat_configs = acc.chat_configs or {}
        chats = list(chat_configs.values())

        enabled = sum(1 for c in chats if c.get("enabled"))
        messages = sum(int(c.get("messages_sent") or 0) for c in chats)
        messages_quota = 0
        messages_remaining = 0
        for c in chats:
            lim = c.get("message_limit")
            if lim is None:
                continue
            lim_i = int(lim)
            sent_i = int(c.get("messages_sent") or 0)
            messages_quota += lim_i
            messages_remaining += max(0, lim_i - sent_i)
        snap = health.snapshot(aid)
        if snap:
            session_total = int(snap.get("sendsTotal") or 0)
            if session_total > messages:
                messages = session_total
        with_limit = sum(1 for c in chats if c.get("message_limit") is not None)
        with_custom_interval = sum(
            1 for c in chats if c.get("custom_interval_min") is not None
        )
        with_custom_text = sum(
            1
            for c in chats
            if (c.get("custom_message") and str(c.get("custom_message")).strip())
            or (
                isinstance(c.get("text_variants"), list)
                and len(c.get("text_variants") or []) > 0
            )
        )
        with_custom_source = sum(
            1 for c in chats if c.get("source_channel_id") is not None
        )

        client = self.clients.get(aid)
        return {
            "id": aid,
            "spamRunning": bool(acc.spam_running),
            "defaultIntervalMin": float(acc.default_interval_min or 0),
            "defaultIntervalJitter": float(acc.default_interval_jitter or 0),
            "defaultMessage": acc.default_message or "",
            "proxy": _mask_proxy_display(acc.proxy) if acc.proxy else None,
            "hasProxy": bool(acc.proxy),
            "rawProxy": acc.proxy,
            "globalSourceChannelId": acc.global_source_channel_id,
            "globalSourceMessageId": acc.global_source_message_id,
            "defaultSourceForward": bool(acc.default_source_forward),
            "connected": bool(client and client.is_connected()),
            "chats": {
                "total": len(chats),
                "enabled": enabled,
                "disabled": len(chats) - enabled,
                "withLimit": with_limit,
                "withCustomInterval": with_custom_interval,
                "withCustomText": with_custom_text,
                "withCustomSource": with_custom_source,
            },
            "messagesSent": messages,
            "messagesQuota": messages_quota,
            "messagesRemaining": messages_remaining,
        }

    async def overview(self) -> dict[str, Any]:
        order = [aid for aid in self.multi.account_order if aid in self.multi.accounts]
        accounts = [self._summarize_account(aid, self.multi.accounts[aid]) for aid in order]

        for acc, aid in zip(accounts, order):
            client = self.clients.get(aid)
            acc["authorized"] = await _telethon_ready(client)
            acc["health"] = _compute_health(
                aid,
                self.multi.accounts[aid],
                client,
                connected=acc["connected"],
                authorized=acc["authorized"],
                enabled_chats=acc["chats"]["enabled"],
            )

        totals = {
            "accounts": len(accounts),
            "running": sum(1 for a in accounts if a["spamRunning"]),
            "connected": sum(1 for a in accounts if a["connected"]),
            "authorized": sum(1 for a in accounts if a["authorized"]),
            "healthy": sum(1 for a in accounts if a["health"]["code"] == "running"),
            "dead": sum(1 for a in accounts if a["health"]["tone"] == "error"),
            "chats": sum(a["chats"]["total"] for a in accounts),
            "chatsEnabled": sum(a["chats"]["enabled"] for a in accounts),
            "messages": sum(a["messagesSent"] for a in accounts),
            "messagesQuota": sum(a["messagesQuota"] for a in accounts),
            "messagesRemaining": sum(a["messagesRemaining"] for a in accounts),
            "withProxy": sum(1 for a in accounts if a["hasProxy"]),
            "withSource": sum(
                1 for a in accounts if a["globalSourceChannelId"] is not None
            ),
        }
        return {
            "activeAccountId": self.multi.active_account_id,
            "totals": totals,
            "accounts": accounts,
        }

    async def get_account(self, aid: str) -> Optional[dict[str, Any]]:
        acc = self.multi.accounts.get(aid)
        if acc is None:
            return None
        summary = self._summarize_account(aid, acc)
        client = self.clients.get(aid)
        summary["authorized"] = await _telethon_ready(client)
        summary["health"] = _compute_health(
            aid, acc, client,
            connected=summary["connected"],
            authorized=summary["authorized"],
            enabled_chats=summary["chats"]["enabled"],
        )
        chat_list: list[dict[str, Any]] = []
        for cid_raw, c in (acc.chat_configs or {}).items():
            chat_list.append({
                "chatId": str(cid_raw),
                "enabled": bool(c.get("enabled")),
                "customMessage": c.get("custom_message"),
                "textVariants": list(c.get("text_variants") or []),
                "extraText": c.get("extra_text") or "",
                "customIntervalMin": c.get("custom_interval_min"),
                "customIntervalJitter": c.get("custom_interval_jitter"),
                "sourceChannelId": c.get("source_channel_id"),
                "sourceMessageId": c.get("source_message_id"),
                "sourceForward": bool(c.get("source_forward")),
                "messageLimit": c.get("message_limit"),
                "messagesSent": int(c.get("messages_sent") or 0),
                "startDelayMin": c.get("start_delay_min"),
            })
        chat_list.sort(key=lambda c: (not c["enabled"], c["chatId"]))
        summary["chatList"] = chat_list
        return summary

    def _chat_entry_from_cfg(
        self, cid: int, title: str | None, cfg: ChatSpamConfig, *, configured: bool
    ) -> dict[str, Any]:
        return {
            "chatId": str(cid),
            "title": title or str(cid),
            "configured": configured,
            "enabled": bool(cfg.enabled),
            "customMessage": cfg.custom_message,
            "textVariants": list(cfg.text_variants or []),
            "extraText": cfg.extra_text or "",
            "customIntervalMin": cfg.custom_interval_min,
            "customIntervalJitter": cfg.custom_interval_jitter,
            "sourceChannelId": cfg.source_channel_id,
            "sourceMessageId": cfg.source_message_id,
            "sourceForward": bool(cfg.source_forward),
            "messageLimit": cfg.message_limit,
            "messagesSent": int(cfg.messages_sent or 0),
            "startDelayMin": cfg.start_delay_min,
        }

    async def list_account_dialogs(self, slot_id: str) -> list[dict[str, Any]]:
        """Telegram group dialogs merged with slot chat_configs."""
        state = self._require_account(slot_id)
        client = await self._ensure_client(slot_id)
        if not await _telethon_ready(client):
            raise ServiceError("Аккаунт не авторизован в Telegram", status=400)
        try:
            rows = await list_group_chats(client)
        except Exception as e:
            raise ServiceError(f"Не удалось получить диалоги: {e}") from e

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for cid, title in rows:
            sid = str(cid)
            seen.add(sid)
            cfg = state.cfg(cid)
            out.append(
                self._chat_entry_from_cfg(
                    cid, title, cfg, configured=sid in (state.chat_configs or {})
                )
            )

        # Configured chats missing from dialog list (left group, etc.)
        for sid, raw in (state.chat_configs or {}).items():
            if sid in seen:
                continue
            try:
                cid = int(sid)
            except ValueError:
                continue
            cfg = ChatSpamConfig.from_dict(raw)
            out.append(
                self._chat_entry_from_cfg(cid, sid, cfg, configured=True)
            )

        out.sort(key=lambda c: (not c["enabled"], (c.get("title") or c["chatId"]).casefold()))
        return out

    async def list_account_channels(self, slot_id: str) -> list[dict[str, Any]]:
        """Broadcast channels (source pickers) for the account."""
        self._require_account(slot_id)
        client = await self._ensure_client(slot_id)
        if not await _telethon_ready(client):
            raise ServiceError("Аккаунт не авторизован в Telegram", status=400)
        try:
            rows = await list_broadcast_channels(client)
        except Exception as e:
            raise ServiceError(f"Не удалось получить каналы: {e}") from e
        return [{"channelId": str(cid), "title": title or str(cid)} for cid, title in rows]

    async def resolve_post_link(self, slot_id: str, url: str) -> dict[str, Any]:
        self._require_account(slot_id)
        client = await self._ensure_client(slot_id)
        if not await _telethon_ready(client):
            raise ServiceError("Аккаунт не авторизован в Telegram", status=400)
        raw = (url or "").strip()
        if not raw:
            raise ServiceError("url обязателен")
        result = await resolve_tme_post_for_forward(client, raw)
        if not result:
            raise ServiceError("Ссылка недоступна или неверный формат t.me/…", status=400)
        peer_id, message_id = result
        return {"channelId": str(peer_id), "messageId": int(message_id)}

    # ============================================================== slot CRUD
    def _require_account(self, aid: str) -> RuntimeState:
        acc = self.multi.accounts.get(aid)
        if acc is None:
            raise ServiceError(f"Слот '{aid}' не найден", status=404)
        return acc

    def _validate_slot_id(self, slot_id: str) -> None:
        if not _ACC_ID_RE.match(slot_id):
            raise ServiceError(
                "id: разрешены латиница/цифры/«_»/«-», 1–32 символа, первая буква"
            )

    async def add_slot(self, payload: dict[str, Any]) -> dict[str, Any]:
        slot_id = str(payload.get("id") or "").strip()
        self._validate_slot_id(slot_id)
        if slot_id in self.multi.accounts:
            raise ServiceError(f"Слот '{slot_id}' уже существует", status=409)

        state = RuntimeState(proxy=_normalize_proxy(payload.get("proxy")))
        if (iv := payload.get("intervalMin")) is not None:
            iv_f = _coerce_float(iv, "intervalMin")
            if iv_f <= 0:
                raise ServiceError("intervalMin: должен быть > 0")
            state.default_interval_min = iv_f
        if (jt := payload.get("intervalJitter")) is not None:
            try:
                state.default_interval_jitter = _parse_jitter_user(str(jt))
            except Exception as e:
                raise ServiceError(f"intervalJitter: {e}") from e
        if msg := payload.get("defaultMessage"):
            state.default_message = str(msg).strip()

        self.multi.accounts[slot_id] = state
        if slot_id not in self.multi.account_order:
            self.multi.account_order.append(slot_id)
        self._save()

        return {"ok": True, "id": slot_id, "hint": "Слот создан. Авторизуй его (телефон+код или загрузка .session)."}

    async def delete_slot(self, slot_id: str) -> None:
        self._require_account(slot_id)

        client = self.clients.pop(slot_id, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("delete_slot[%s]: disconnect", slot_id, exc_info=True)

        state = self.multi.accounts.pop(slot_id, None)
        if state is not None:
            state.spam_running = False
        self.multi.account_order = [x for x in self.multi.account_order if x != slot_id]
        self._pending_auth.pop(slot_id, None)

        try:
            _delete_session_files(slot_id)
        except Exception:
            logger.warning("delete_slot[%s]: session unlink", slot_id, exc_info=True)

        if self.multi.active_account_id == slot_id:
            self.multi.active_account_id = (
                self.multi.account_order[0] if self.multi.account_order else "default"
            )

        health.drop(slot_id)
        self._save()

    async def activate_slot(self, slot_id: str) -> dict[str, Any]:
        self._require_account(slot_id)
        self.multi.active_account_id = slot_id
        self._save()
        return {"ok": True, "activeAccountId": slot_id}

    async def patch_slot(self, slot_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        state = self._require_account(slot_id)

        if "proxy" in patch:
            state.proxy = _normalize_proxy(patch["proxy"])

        if (v := patch.get("defaultIntervalMin", patch.get("default_interval_min"))) is not None:
            iv = _coerce_float(v, "defaultIntervalMin")
            if iv <= 0:
                raise ServiceError("defaultIntervalMin: должен быть > 0")
            state.default_interval_min = iv

        if "defaultIntervalJitter" in patch or "default_interval_jitter" in patch:
            v = patch.get("defaultIntervalJitter", patch.get("default_interval_jitter"))
            if v in (None, ""):
                state.default_interval_jitter = 0.0
            else:
                try:
                    state.default_interval_jitter = _parse_jitter_user(str(v))
                except Exception as e:
                    raise ServiceError(f"defaultIntervalJitter: {e}") from e

        if "defaultMessage" in patch or "default_message" in patch:
            v = patch.get("defaultMessage", patch.get("default_message"))
            state.default_message = "" if v in (None, "-") else str(v)

        if "defaultSourceForward" in patch or "default_source_forward" in patch:
            state.default_source_forward = bool(
                patch.get("defaultSourceForward", patch.get("default_source_forward"))
            )

        if "globalSourceChannelId" in patch or "global_source_channel_id" in patch:
            v = patch.get("globalSourceChannelId", patch.get("global_source_channel_id"))
            state.global_source_channel_id = (
                None if v in (None, "", "-") else _coerce_int(v, "globalSourceChannelId")
            )
            if state.global_source_channel_id is None:
                state.global_source_message_id = None

        if "globalSourceMessageId" in patch or "global_source_message_id" in patch:
            v = patch.get("globalSourceMessageId", patch.get("global_source_message_id"))
            state.global_source_message_id = (
                None if v in (None, "", "-") else _coerce_int(v, "globalSourceMessageId")
            )

        self._save()
        return self._summarize_account(slot_id, state)

    # =================================================================== spam
    async def set_spam(self, slot_id: str, running: bool) -> dict[str, Any]:
        state = self._require_account(slot_id)
        client = self.clients.get(slot_id)
        if running:
            ok, err = validate_spam_start(state, await _telethon_ready(client))
            if not ok:
                raise ServiceError(err)
            state.spam_running = True
            if client is not None:
                start_spam_loop_background(
                    client, state, persist=self._save, account_key=slot_id
                )
        else:
            state.spam_running = False
        self._save()
        return {"ok": True, "spamRunning": state.spam_running}

    # ================================================================== chats
    def _apply_chat_patch(self, cfg: ChatSpamConfig, patch: dict[str, Any]) -> None:
        if "enabled" in patch:
            cfg.enabled = bool(patch["enabled"])
        if "customMessage" in patch or "custom_message" in patch:
            v = patch.get("customMessage", patch.get("custom_message"))
            cfg.custom_message = None if v in (None, "-", "") else str(v)
        if "textVariants" in patch or "text_variants" in patch:
            v = patch.get("textVariants", patch.get("text_variants"))
            if isinstance(v, str):
                cfg.text_variants = _parse_text_variants(v)
            elif isinstance(v, list):
                cfg.text_variants = [str(x).strip() for x in v if str(x).strip()]
            elif v in (None, "-"):
                cfg.text_variants = []
        if "extraText" in patch or "extra_text" in patch:
            v = patch.get("extraText", patch.get("extra_text"))
            cfg.extra_text = "" if v in (None, "-") else str(v)
        if "customIntervalMin" in patch or "custom_interval_min" in patch:
            v = patch.get("customIntervalMin", patch.get("custom_interval_min"))
            if v in (None, "", "-"):
                cfg.custom_interval_min = None
            else:
                iv = _coerce_float(v, "customIntervalMin")
                if iv <= 0:
                    raise ServiceError("customIntervalMin: должен быть > 0")
                cfg.custom_interval_min = iv
        if "customIntervalJitter" in patch or "custom_interval_jitter" in patch:
            v = patch.get("customIntervalJitter", patch.get("custom_interval_jitter"))
            if v in (None, "", "-"):
                cfg.custom_interval_jitter = None
            else:
                try:
                    cfg.custom_interval_jitter = _parse_jitter_user(str(v))
                except Exception as e:
                    raise ServiceError(f"customIntervalJitter: {e}") from e
        if "sourceChannelId" in patch or "source_channel_id" in patch:
            v = patch.get("sourceChannelId", patch.get("source_channel_id"))
            cfg.source_channel_id = (
                None if v in (None, "", "-") else _coerce_int(v, "sourceChannelId")
            )
            if cfg.source_channel_id is None:
                cfg.source_message_id = None
                cfg.source_forward = False
        if "sourceMessageId" in patch or "source_message_id" in patch:
            v = patch.get("sourceMessageId", patch.get("source_message_id"))
            cfg.source_message_id = (
                None if v in (None, "", "-") else _coerce_int(v, "sourceMessageId")
            )
        if "sourceForward" in patch or "source_forward" in patch:
            cfg.source_forward = bool(
                patch.get("sourceForward", patch.get("source_forward"))
            )
        if "messageLimit" in patch or "message_limit" in patch:
            v = patch.get("messageLimit", patch.get("message_limit"))
            if v in (None, "", "-"):
                cfg.message_limit = None
                cfg.messages_sent = 0
            else:
                cfg.message_limit = _coerce_int(v, "messageLimit")
        if "startDelayMin" in patch or "start_delay_min" in patch:
            v = patch.get("startDelayMin", patch.get("start_delay_min"))
            if v in (None, "", "-"):
                cfg.start_delay_min = None
            else:
                cfg.start_delay_min = _coerce_float(v, "startDelayMin")

    async def upsert_chat(self, slot_id: str, chat_id_raw: Any, patch: dict[str, Any]) -> dict[str, Any]:
        state = self._require_account(slot_id)
        cid = _coerce_int(chat_id_raw, "chatId")
        cfg = state.cfg(cid)
        self._apply_chat_patch(cfg, patch)
        state.set_cfg(cid, cfg)
        self._save()
        return {"ok": True, "chatId": str(cid), "config": cfg.to_dict()}

    async def delete_chat(self, slot_id: str, chat_id_raw: Any) -> None:
        state = self._require_account(slot_id)
        cid = _coerce_int(chat_id_raw, "chatId")
        if str(cid) not in state.chat_configs:
            raise ServiceError(f"Чат {cid} не привязан к слоту '{slot_id}'", status=404)
        state.chat_configs.pop(str(cid), None)
        self._save()

    # ======================================================== Telethon AUTH
    async def _ensure_client(self, slot_id: str) -> TelegramClient:
        state = self._require_account(slot_id)
        client = self.clients.get(slot_id)
        if client is None:
            client = make_telethon_client(
                slot_id, self.api_id, self.api_hash, proxy_raw=state.proxy
            )
            client = await connect_client_with_fallback(
                client,
                account_id=slot_id,
                api_id=self.api_id,
                api_hash=self.api_hash,
                proxy_raw=state.proxy,
                allow_direct_fallback=True,
            )
            self.clients[slot_id] = client
        elif not client.is_connected():
            try:
                await client.connect()
            except Exception as e:
                raise ServiceError(f"Не удалось подключиться: {e}") from e
        return client

    async def auth_send_code(self, slot_id: str, phone: str) -> dict[str, Any]:
        self._require_account(slot_id)
        phone = (phone or "").strip()
        if not phone:
            raise ServiceError("phone обязателен")
        client = await self._ensure_client(slot_id)
        try:
            sent = await client.send_code_request(phone)
        except Exception as e:
            raise ServiceError(f"send_code_request: {e}") from e
        self._pending_auth[slot_id] = {
            "phone": phone,
            "hash": sent.phone_code_hash,
            "client": client,
        }
        return {"ok": True, "needCode": True}

    async def auth_sign_in(
        self,
        slot_id: str,
        code: str,
        password: Optional[str] = None,
    ) -> dict[str, Any]:
        state = self._require_account(slot_id)
        pending = self._pending_auth.get(slot_id)
        if not pending:
            raise ServiceError("Сначала вызови /auth/send_code")
        client: TelegramClient = pending["client"]

        try:
            await client.sign_in(
                pending["phone"],
                (code or "").strip(),
                phone_code_hash=pending["hash"],
            )
        except SessionPasswordNeededError:
            if not password:
                return {"ok": False, "need2FA": True}
            try:
                await client.sign_in(password=password)
            except Exception as e:
                raise ServiceError(f"2FA: {e}") from e
        except Exception as e:
            raise ServiceError(f"sign_in: {e}") from e

        return await self._finalize_auth(slot_id, state, client)

    async def auth_submit_2fa(self, slot_id: str, password: str) -> dict[str, Any]:
        state = self._require_account(slot_id)
        pending = self._pending_auth.get(slot_id)
        if not pending:
            raise ServiceError("Сначала вызови /auth/send_code и /auth/sign_in")
        client: TelegramClient = pending["client"]
        try:
            await client.sign_in(password=(password or "").strip())
        except Exception as e:
            raise ServiceError(f"2FA: {e}") from e
        return await self._finalize_auth(slot_id, state, client)

    async def _finalize_auth(
        self,
        slot_id: str,
        state: RuntimeState,
        client: TelegramClient,
    ) -> dict[str, Any]:
        self._pending_auth.pop(slot_id, None)
        me = await client.get_me()
        start_spam_loop_background(
            client, state, persist=self._save, account_key=slot_id
        )
        self._save()
        return {
            "ok": True,
            "authorized": True,
            "id": slot_id,
            "tgUsername": me.username,
            "tgUserId": me.id,
        }

    # ========================================================== session upload
    async def upload_session(
        self,
        slot_id: str,
        data: bytes,
        proxy: Optional[str] = None,
    ) -> dict[str, Any]:
        self._validate_slot_id(slot_id)
        if slot_id in self.multi.accounts and self.clients.get(slot_id) is not None:
            await self.delete_slot(slot_id)  # clean restart

        proxy_n = _normalize_proxy(proxy)
        if slot_id not in self.multi.accounts:
            self.multi.accounts[slot_id] = RuntimeState(proxy=proxy_n)
            if slot_id not in self.multi.account_order:
                self.multi.account_order.append(slot_id)
        else:
            self.multi.accounts[slot_id].proxy = proxy_n

        base = session_path(slot_id)
        target = base + ".session"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)

        state = self.multi.accounts[slot_id]
        try:
            client = make_telethon_client(
                slot_id, self.api_id, self.api_hash, proxy_raw=state.proxy
            )
            client = await connect_client_with_fallback(
                client,
                account_id=slot_id,
                api_id=self.api_id,
                api_hash=self.api_hash,
                proxy_raw=state.proxy,
                allow_direct_fallback=True,
            )
        except Exception as e:
            raise ServiceError(f"connect: {e}") from e

        self.clients[slot_id] = client
        authorized = await client.is_user_authorized()
        if authorized:
            me = await client.get_me()
            start_spam_loop_background(
                client, state, persist=self._save, account_key=slot_id
            )
            self._save()
            return {
                "ok": True,
                "id": slot_id,
                "authorized": True,
                "tgUsername": me.username,
                "tgUserId": me.id,
            }
        self._save()
        return {
            "ok": False,
            "id": slot_id,
            "authorized": False,
            "hint": ".session принят, но Telethon не подтвердил авторизацию — возможно, файл от другого API_ID.",
        }
