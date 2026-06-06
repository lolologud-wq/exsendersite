from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from telethon.errors import RPCError
from telethon.tl.types import InputPeerChannel, InputPeerUser, User
from telethon.utils import get_peer_id

from bot_service import BotService, ServiceError
from control_bot import _telethon_ready
from inviter import db as inviter_db
from inviter.errors import (
    format_error_message,
    format_invite_result_error,
    get_antiflood_timing,
    invite_api_batch_size,
    invite_many,
    invite_one,
    needs_invite_cooldown,
    sleep_with_stop,
)

logger = logging.getLogger(__name__)

def _parse_skip_reason(user: User) -> str | None:
    if getattr(user, "bot", False):
        return "bot"
    if getattr(user, "deleted", False):
        return "deleted"
    if getattr(user, "scam", False):
        return "scam"
    if getattr(user, "fake", False):
        return "fake"
    if getattr(user, "restricted", False):
        return "restricted"
    return None


async def _resolve_invite_user(
    client: Any,
    row: dict[str, Any],
) -> tuple[Any | None, str | None]:
    """Resolve user for invite without extra API round-trip when access_hash is known."""
    tg_user_id = int(row["tg_user_id"])
    access_hash = int(row.get("access_hash") or 0)
    if access_hash:
        return InputPeerUser(tg_user_id, access_hash), None

    username = (row.get("username") or "").strip().lstrip("@")
    if username:
        try:
            return await client.get_input_entity(username), None
        except RPCError:
            return None, "resolve_failed"

    try:
        user = await client.get_entity(tg_user_id)
        if isinstance(user, User):
            skip = _parse_skip_reason(user)
            if skip:
                return None, "skipped_profile"
        return user, None
    except RPCError:
        return None, "resolve_failed"


async def _backfill_queue_access_hashes(
    client: Any,
    db_path: Path,
    account_id: str,
    *,
    should_stop: Callable[[], bool],
    on_progress: Callable[[int, str], None] | None = None,
) -> int:
    if not inviter_db.queue_needs_hash_backfill(db_path, account_id):
        return 0
    sources = inviter_db.get_queue_source_chats(db_path, account_id)
    total_updated = 0
    for src in sources:
        if should_stop():
            break
        chat_id = int(src["source_chat_id"])
        source_title = str(src.get("source_chat_title") or chat_id)
        scanned = 0
        try:
            entity = await client.get_entity(chat_id)
            input_entity = await client.get_input_entity(entity)
            mapping: dict[int, int] = {}
            async for participant in client.iter_participants(input_entity):
                if should_stop():
                    break
                scanned += 1
                if on_progress and scanned % 25 == 0:
                    on_progress(scanned, source_title)
                user_id = int(getattr(participant, "id", 0) or 0)
                access_hash = int(getattr(participant, "access_hash", 0) or 0)
                if user_id and access_hash:
                    mapping[user_id] = access_hash
            if on_progress:
                on_progress(scanned, source_title)
            total_updated += inviter_db.bulk_update_access_hashes(db_path, account_id, mapping)
            logger.info(
                "backfill access_hash source=%s account=%s updated=%s",
                chat_id,
                account_id,
                total_updated,
            )
        except RPCError as exc:
            logger.warning("backfill access_hash source=%s failed: %s", chat_id, exc)
    return total_updated


class InviterService:
    """Invite workflow scoped by account_id (Telegram slot)."""

    def __init__(self, bot_service: BotService, db_path: Path | None = None) -> None:
        self.bot = bot_service
        self.db_path = inviter_db.init_db(db_path)
        self._job_task: asyncio.Task | None = None
        self._stop_flag = False
        self._job_state: dict[str, Any] = {
            "running": False,
            "accountId": "",
            "progress": 0,
            "total": 0,
            "stats": {},
            "lastError": "",
            "lastResult": "",
            "lastInviteSec": 0.0,
            "phase": "",
            "backfillScanned": 0,
            "backfillSource": "",
            "startedAt": "",
            "finishedAt": "",
        }
        self._parse_task: asyncio.Task | None = None
        self._parse_state: dict[str, Any] = {
            "running": False,
            "accountId": "",
            "sourceRef": "",
            "sourceTitle": "",
            "progress": 0,
            "phase": "",
            "lastError": "",
            "result": {},
            "startedAt": "",
            "finishedAt": "",
        }

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def overview(self, account_id: str) -> dict[str, Any]:
        account_id = (account_id or "").strip()
        if not account_id:
            raise ServiceError("accountId обязателен")
        self.bot._require_account(account_id)
        queue_count, parsed_count = inviter_db.get_counts(self.db_path, account_id)
        target = inviter_db.get_target(self.db_path, account_id)
        client = self.bot.clients.get(account_id)
        authorized = await _telethon_ready(client)
        return {
            "accountId": account_id,
            "queueCount": queue_count,
            "parsedChatsCount": parsed_count,
            "target": target,
            "job": dict(self._job_state),
            "parse": dict(self._parse_state),
            "authorized": authorized,
            "connected": bool(client and client.is_connected()),
        }

    async def list_dialogs(self, account_id: str) -> list[dict[str, Any]]:
        account_id = (account_id or "").strip()
        if not account_id:
            raise ServiceError("accountId обязателен")
        client = await self.bot._ensure_client(account_id)
        if not await _telethon_ready(client):
            raise ServiceError("Аккаунт не авторизован в Telegram", status=400)
        rows: list[dict[str, Any]] = []
        async for dlg in client.iter_dialogs(limit=200):
            if not dlg.is_group and not dlg.is_channel:
                continue
            rows.append({
                "peerId": get_peer_id(dlg.entity),
                "title": dlg.name or str(get_peer_id(dlg.entity)),
            })
            if len(rows) >= 60:
                break
        rows.sort(key=lambda x: (x.get("title") or "").casefold())
        return rows

    def get_parse(self) -> dict[str, Any]:
        return dict(self._parse_state)

    async def parse(
        self,
        account_id: str,
        source_ref: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        account_id = (account_id or "").strip()
        source_ref = (source_ref or "").strip()
        if not account_id:
            raise ServiceError("accountId обязателен")
        if not source_ref:
            raise ServiceError("sourceRef обязателен")
        if self._parse_task is not None and not self._parse_task.done():
            raise ServiceError("Парс уже выполняется", status=409)

        client = await self.bot._ensure_client(account_id)
        if not await _telethon_ready(client):
            raise ServiceError("Аккаунт не авторизован в Telegram", status=400)

        self._parse_state = {
            "running": True,
            "accountId": account_id,
            "sourceRef": source_ref,
            "sourceTitle": "",
            "progress": 0,
            "phase": "starting",
            "lastError": "",
            "result": {},
            "startedAt": self._utc_now(),
            "finishedAt": "",
        }
        self._parse_task = asyncio.create_task(
            self._run_parse(account_id, source_ref, force=force)
        )
        return {"status": "started", "parse": dict(self._parse_state)}

    async def _run_parse(
        self,
        account_id: str,
        source_ref: str,
        *,
        force: bool,
    ) -> None:
        try:
            client = await self.bot._ensure_client(account_id)
            self._parse_state["phase"] = "resolving"
            source_entity = await client.get_entity(source_ref)
            source_input = await client.get_input_entity(source_entity)
            source_chat_id = str(get_peer_id(source_entity))
            source_chat_title = getattr(source_entity, "title", "") or source_ref
            self._parse_state["sourceTitle"] = source_chat_title

            if not force and inviter_db.is_chat_parsed(
                self.db_path, account_id, source_chat_id
            ):
                self._parse_state["result"] = {
                    "status": "already_parsed",
                    "sourceChatId": source_chat_id,
                    "sourceChatTitle": source_chat_title,
                }
                self._parse_state["phase"] = "done"
                return

            users_payload: list[tuple[int, str, str, int]] = []
            skipped = 0
            blocked_ids = inviter_db.get_blocked_user_ids(self.db_path, account_id)
            self._parse_state["phase"] = "scanning"
            async for participant in client.iter_participants(source_input):
                skip = _parse_skip_reason(participant)
                if skip:
                    skipped += 1
                    continue
                if int(participant.id) in blocked_ids:
                    skipped += 1
                    continue
                display_name = (
                    f"{(participant.first_name or '').strip()} {(participant.last_name or '').strip()}".strip()
                )
                users_payload.append(
                    (
                        participant.id,
                        participant.username or "",
                        display_name,
                        int(getattr(participant, "access_hash", 0) or 0),
                    )
                )
                scanned = len(users_payload) + skipped
                if scanned % 25 == 0:
                    self._parse_state["progress"] = len(users_payload)

            self._parse_state["progress"] = len(users_payload)
            self._parse_state["phase"] = "saving"
            added, duplicated, blocked = inviter_db.add_users_to_queue(
                self.db_path,
                account_id,
                source_chat_id,
                source_chat_title,
                users_payload,
            )
            inviter_db.mark_chat_parsed(
                self.db_path, account_id, source_chat_id, source_chat_title
            )
            self._parse_state["result"] = {
                "status": "ok",
                "sourceChatId": source_chat_id,
                "sourceChatTitle": source_chat_title,
                "added": added,
                "duplicated": duplicated,
                "blocked": blocked,
                "skipped": skipped,
                "totalFound": len(users_payload),
            }
            self._parse_state["phase"] = "done"
        except RPCError as exc:
            logger.exception("Parse RPC error account=%s", account_id)
            self._parse_state["lastError"] = format_error_message(exc)
            self._parse_state["phase"] = "error"
        except Exception as exc:
            logger.exception("Parse error account=%s", account_id)
            self._parse_state["lastError"] = format_error_message(exc)
            self._parse_state["phase"] = "error"
        finally:
            self._parse_state["running"] = False
            self._parse_state["finishedAt"] = self._utc_now()
            self._parse_task = None

    async def set_target(self, account_id: str, target_ref: str) -> dict[str, Any]:
        account_id = (account_id or "").strip()
        target_ref = (target_ref or "").strip()
        if not account_id:
            raise ServiceError("accountId обязателен")
        if not target_ref:
            raise ServiceError("targetRef обязателен")

        client = await self.bot._ensure_client(account_id)
        if not await _telethon_ready(client):
            raise ServiceError("Аккаунт не авторизован в Telegram", status=400)

        try:
            target_entity = await client.get_entity(target_ref)
            target_peer_id = get_peer_id(target_entity)
            target_title = getattr(target_entity, "title", "") or target_ref
            inviter_db.set_target(
                self.db_path,
                account_id,
                target_ref,
                target_peer_id,
                target_title,
            )
            return {
                "ok": True,
                "ref": target_ref,
                "peerId": target_peer_id,
                "title": target_title,
            }
        except RPCError as exc:
            raise ServiceError(format_error_message(exc), status=400) from exc
        except Exception as exc:
            raise ServiceError(format_error_message(exc), status=400) from exc

    def get_queue(self, account_id: str, limit: int = 0) -> dict[str, Any]:
        account_id = (account_id or "").strip()
        if not account_id:
            raise ServiceError("accountId обязателен")
        self.bot._require_account(account_id)
        if limit < 0:
            limit = 0
        items = inviter_db.get_queue(self.db_path, account_id, limit=limit)
        queue_count, _ = inviter_db.get_counts(self.db_path, account_id)
        return {"total": queue_count, "items": items}

    def get_job(self) -> dict[str, Any]:
        return dict(self._job_state)

    async def start_job(
        self,
        account_id: str,
        *,
        limit: int = 0,
        delay: float = 3.0,
    ) -> dict[str, Any]:
        account_id = (account_id or "").strip()
        if not account_id:
            raise ServiceError("accountId обязателен")
        if self._job_task is not None and not self._job_task.done():
            raise ServiceError("Инвайт уже запущен", status=409)

        target = inviter_db.get_target(self.db_path, account_id)
        if not target or not (target.get("ref") or "").strip():
            raise ServiceError("Сначала задай target (ссылка или чат)")

        self._stop_flag = False
        self._job_state = {
            "running": True,
            "accountId": account_id,
            "progress": 0,
            "total": 0,
            "stats": {},
            "lastError": "",
            "lastResult": "",
            "lastInviteSec": 0.0,
            "phase": "",
            "backfillScanned": 0,
            "backfillSource": "",
            "startedAt": self._utc_now(),
            "finishedAt": "",
        }
        self._job_task = asyncio.create_task(
            self._run_invite_job(account_id, target["ref"], limit=limit, delay=delay)
        )
        return {"ok": True, "job": dict(self._job_state)}

    def stop_job(self) -> dict[str, Any]:
        if self._job_task is None or self._job_task.done():
            return {"ok": False, "message": "Нет активного инвайта"}
        self._stop_flag = True
        return {"ok": True, "message": "Запрос на остановку отправлен"}

    async def _run_invite_job(
        self,
        account_id: str,
        target_ref: str,
        *,
        limit: int,
        delay: float,
    ) -> None:
        stats: dict[str, int] = {}
        try:
            queue_rows = inviter_db.get_queue(
                self.db_path, account_id, limit=limit if limit > 0 else 0
            )
            total = len(queue_rows)
            self._job_state["total"] = total
            if not queue_rows:
                self._job_state["lastError"] = "Очередь пустая"
                return

            client = await self.bot._ensure_client(account_id)
            if not await _telethon_ready(client):
                self._job_state["lastError"] = "Аккаунт не авторизован"
                return

            target_entity = await client.get_entity(target_ref)
            target_input = await client.get_input_entity(target_entity)

            self._job_state["phase"] = "backfill"
            backfilled = await _backfill_queue_access_hashes(
                client,
                self.db_path,
                account_id,
                should_stop=lambda: self._stop_flag,
                on_progress=lambda scanned, title: self._job_state.update(
                    {
                        "backfillScanned": scanned,
                        "backfillSource": title,
                    }
                ),
            )
            if backfilled:
                queue_rows = inviter_db.get_queue(
                    self.db_path, account_id, limit=limit if limit > 0 else 0
                )
                total = len(queue_rows)
                self._job_state["total"] = total

            self._job_state["phase"] = "inviting"
            per_invite_min, per_invite_max, batch_size, batch_pause_min, batch_pause_max = (
                get_antiflood_timing(delay)
            )
            api_batch = invite_api_batch_size()
            fatal_target_results = {
                "chat_write_forbidden",
                "no_admin_rights",
                "target_chat_private",
                "rpc_error:ChatWriteForbiddenError",
                "rpc_error:ChatAdminRequiredError",
                "rpc_error:ChannelPrivateError",
            }
            blocked_ids = inviter_db.get_blocked_user_ids(self.db_path, account_id)
            processed = 0
            row_idx = 0

            while row_idx < len(queue_rows):
                if self._stop_flag:
                    break

                batch_rows: list[dict[str, Any]] = []
                batch_users: list[Any] = []

                while row_idx < len(queue_rows) and len(batch_rows) < api_batch:
                    row = queue_rows[row_idx]
                    row_idx += 1
                    processed += 1
                    self._job_state["progress"] = processed
                    tg_user_id = int(row["tg_user_id"])

                    if tg_user_id in blocked_ids:
                        result = "blocked_list"
                        stats[result] = stats.get(result, 0) + 1
                        inviter_db.remove_queue_item(self.db_path, int(row["id"]))
                        self._job_state["stats"] = dict(stats)
                        continue

                    user_input, pre_result = await _resolve_invite_user(client, row)
                    if pre_result:
                        result = pre_result
                        stats[result] = stats.get(result, 0) + 1
                        self._job_state["lastResult"] = result
                        if result == "privacy_restricted":
                            inviter_db.mark_blocked_user(
                                self.db_path, account_id, tg_user_id, "privacy_restricted"
                            )
                        if result not in fatal_target_results:
                            inviter_db.remove_queue_item(self.db_path, int(row["id"]))
                        self._job_state["stats"] = dict(stats)
                        if result in fatal_target_results:
                            self._job_state["lastError"] = format_invite_result_error(result)
                            row_idx = len(queue_rows)
                            break
                        continue

                    batch_rows.append(row)
                    batch_users.append(user_input)

                if not batch_rows:
                    continue

                t0 = time.perf_counter()
                if len(batch_users) == 1:
                    results = [await invite_one(client, target_input, batch_users[0])]
                else:
                    results = await invite_many(client, target_input, batch_users)
                elapsed = round(time.perf_counter() - t0, 2)
                per_user_sec = round(elapsed / len(batch_users), 2)
                self._job_state["lastInviteSec"] = per_user_sec

                batch_cooldown = False
                for row, result in zip(batch_rows, results):
                    tg_user_id = int(row["tg_user_id"])
                    stats[result] = stats.get(result, 0) + 1
                    self._job_state["lastResult"] = result
                    if result == "privacy_restricted":
                        inviter_db.mark_blocked_user(
                            self.db_path, account_id, tg_user_id, "privacy_restricted"
                        )
                    if result not in fatal_target_results:
                        inviter_db.remove_queue_item(self.db_path, int(row["id"]))
                    if result in fatal_target_results:
                        self._job_state["lastError"] = format_invite_result_error(result)
                        row_idx = len(queue_rows)
                        break
                    if result == "peer_flood":
                        self._job_state["lastError"] = format_invite_result_error("peer_flood")
                        row_idx = len(queue_rows)
                        break
                    if needs_invite_cooldown(result):
                        batch_cooldown = True

                self._job_state["stats"] = dict(stats)
                if self._job_state.get("lastError"):
                    break

                if batch_cooldown:
                    wait_s = random.uniform(per_invite_min, per_invite_max)
                    if await sleep_with_stop(lambda: self._stop_flag, wait_s):
                        break

                if batch_size > 0 and processed % batch_size == 0 and processed < total:
                    batch_wait = random.uniform(batch_pause_min, batch_pause_max)
                    if await sleep_with_stop(lambda: self._stop_flag, batch_wait):
                        break
        except RPCError as exc:
            logger.exception("Invite job RPC error account=%s", account_id)
            self._job_state["lastError"] = format_error_message(exc)
        except Exception as exc:
            logger.exception("Invite job error account=%s", account_id)
            self._job_state["lastError"] = format_error_message(exc)
        finally:
            self._job_state["running"] = False
            self._job_state["finishedAt"] = self._utc_now()
            self._stop_flag = False
            self._job_task = None
