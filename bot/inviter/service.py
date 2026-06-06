from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon.errors import RPCError, UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import InputPeerChannel
from telethon.utils import get_peer_id

from bot_service import BotService, ServiceError
from control_bot import _telethon_ready
from inviter import db as inviter_db
from inviter.errors import (
    format_error_message,
    format_invite_result_error,
    get_antiflood_timing,
    invite_one,
    sleep_with_stop,
)

logger = logging.getLogger(__name__)


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

        client = await self.bot._ensure_client(account_id)
        if not await _telethon_ready(client):
            raise ServiceError("Аккаунт не авторизован в Telegram", status=400)

        try:
            source_entity = await client.get_entity(source_ref)
            source_input = await client.get_input_entity(source_entity)
            source_chat_id = str(get_peer_id(source_entity))
            source_chat_title = getattr(source_entity, "title", "") or source_ref

            if not force and inviter_db.is_chat_parsed(
                self.db_path, account_id, source_chat_id
            ):
                return {
                    "status": "already_parsed",
                    "sourceChatId": source_chat_id,
                    "sourceChatTitle": source_chat_title,
                }

            users_payload: list[tuple[int, str, str]] = []
            async for participant in client.iter_participants(source_input):
                if participant.bot or participant.deleted:
                    continue
                display_name = (
                    f"{(participant.first_name or '').strip()} {(participant.last_name or '').strip()}".strip()
                )
                users_payload.append(
                    (participant.id, participant.username or "", display_name)
                )

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
            return {
                "status": "ok",
                "sourceChatId": source_chat_id,
                "sourceChatTitle": source_chat_title,
                "added": added,
                "duplicated": duplicated,
                "blocked": blocked,
                "totalFound": len(users_payload),
            }
        except RPCError as exc:
            raise ServiceError(format_error_message(exc), status=400) from exc
        except Exception as exc:
            raise ServiceError(format_error_message(exc), status=400) from exc

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
            per_invite_min, per_invite_max, batch_size, batch_pause_min, batch_pause_max = (
                get_antiflood_timing(delay)
            )
            fatal_target_results = {
                "chat_write_forbidden",
                "no_admin_rights",
                "target_chat_private",
                "rpc_error:ChatWriteForbiddenError",
                "rpc_error:ChatAdminRequiredError",
                "rpc_error:ChannelPrivateError",
            }

            for idx, row in enumerate(queue_rows, start=1):
                if self._stop_flag:
                    break
                self._job_state["progress"] = idx
                tg_user_id = int(row["tg_user_id"])
                block_reason = inviter_db.get_block_reason(
                    self.db_path, account_id, tg_user_id
                )
                if block_reason:
                    result = f"blocked_{block_reason}"
                    stats[result] = stats.get(result, 0) + 1
                    inviter_db.remove_queue_item(int(row["id"]))
                    continue
                try:
                    user = await client.get_entity(tg_user_id)
                    if getattr(user, "bot", False) or getattr(user, "deleted", False):
                        result = "skipped_profile"
                    else:
                        already_in_chat = False
                        if isinstance(target_input, InputPeerChannel):
                            try:
                                await client(
                                    GetParticipantRequest(channel=target_input, participant=user)
                                )
                                already_in_chat = True
                            except UserNotParticipantError:
                                already_in_chat = False
                            except RPCError:
                                already_in_chat = False
                        if already_in_chat:
                            result = "already_in_chat"
                        else:
                            result = await invite_one(client, target_input, user)
                except RPCError:
                    result = "resolve_failed"

                stats[result] = stats.get(result, 0) + 1
                if result == "privacy_restricted":
                    inviter_db.mark_blocked_user(
                        self.db_path, account_id, tg_user_id, "privacy_restricted"
                    )
                if result not in fatal_target_results:
                    inviter_db.remove_queue_item(int(row["id"]))

                if result in fatal_target_results:
                    self._job_state["lastError"] = format_invite_result_error(result)
                    break
                if result == "peer_flood":
                    self._job_state["lastError"] = format_invite_result_error("peer_flood")
                    break

                wait_s = random.uniform(per_invite_min, per_invite_max)
                if await sleep_with_stop(lambda: self._stop_flag, wait_s):
                    break

                if batch_size > 0 and idx % batch_size == 0 and idx < total:
                    batch_wait = random.uniform(batch_pause_min, batch_pause_max)
                    if await sleep_with_stop(lambda: self._stop_flag, batch_wait):
                        break

            self._job_state["stats"] = stats
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
