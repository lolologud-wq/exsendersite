"""HTTP API of a single bot instance.

Listens on its own port (default 8080), protected by Bearer token.
NO UI, NO server registry, NO deployer — those live in the site (web/).

The site (`web/`) talks to this API over the network and aggregates
data from multiple bots.

Environment:
  BOT_API_HOST   default 0.0.0.0
  BOT_API_PORT   default 8080
  BOT_API_TOKEN  if missing, generated and written to bot_api_token.txt
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bot_service import BotService, ServiceError
from inviter.service import InviterService

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(__file__).resolve().parent / "bot_api_token.txt"
MAX_SESSION_BYTES = 5 * 1024 * 1024  # 5 MiB safety cap


def resolve_token() -> str:
    """Pick BOT_API_TOKEN from env, or generate + persist to disk."""
    env_token = (os.getenv("BOT_API_TOKEN") or "").strip()
    if env_token:
        return env_token
    if TOKEN_FILE.exists():
        cached = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    new_token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(new_token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    logger.warning(
        "BOT_API_TOKEN not set in env — generated new one and saved to %s",
        TOKEN_FILE,
    )
    return new_token


def build_app(
    service: BotService,
    token: str,
    inviter_service: InviterService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Userbot — local API",
        version="1.0.0",
        docs_url="/api/local/docs",
        redoc_url=None,
    )
    bearer = HTTPBearer(auto_error=False)

    async def require_token(
        creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if creds is None or not secrets.compare_digest(creds.credentials, token):
            raise HTTPException(status_code=401, detail="bad token")

    @app.exception_handler(ServiceError)
    async def _handle_service_error(_: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})

    # ---------------------------------------------------------------- health
    @app.get("/api/local/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "userbot"}

    @app.get("/api/local/whoami", dependencies=[Depends(require_token)])
    async def whoami() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "userbot",
            "activeAccountId": service.multi.active_account_id,
            "slots": len(service.multi.accounts),
        }

    # ---------------------------------------------------------------- read
    @app.get("/api/local/overview", dependencies=[Depends(require_token)])
    async def overview() -> dict[str, Any]:
        return await service.overview()

    @app.get("/api/local/accounts", dependencies=[Depends(require_token)])
    async def accounts() -> list[dict[str, Any]]:
        ov = await service.overview()
        return ov["accounts"]

    @app.get("/api/local/accounts/{aid}", dependencies=[Depends(require_token)])
    async def account(aid: str) -> dict[str, Any]:
        acc = await service.get_account(aid)
        if acc is None:
            raise HTTPException(status_code=404, detail="not found")
        return acc

    @app.get(
        "/api/local/accounts/{aid}/chats", dependencies=[Depends(require_token)]
    )
    async def account_chats(aid: str) -> list[dict[str, Any]]:
        acc = await service.get_account(aid)
        if acc is None:
            raise HTTPException(status_code=404, detail="not found")
        return acc["chatList"]

    @app.get(
        "/api/local/accounts/{aid}/dialogs", dependencies=[Depends(require_token)]
    )
    async def account_dialogs(aid: str, force: bool = False) -> list[dict[str, Any]]:
        return await service.list_account_dialogs(aid, force=force)

    @app.get(
        "/api/local/accounts/{aid}/channels", dependencies=[Depends(require_token)]
    )
    async def account_channels(aid: str) -> list[dict[str, Any]]:
        return await service.list_account_channels(aid)

    @app.post(
        "/api/local/accounts/{aid}/resolve_post",
        dependencies=[Depends(require_token)],
    )
    async def resolve_post(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await service.resolve_post_link(aid, str((payload or {}).get("url", "")))

    # -------------------------------------------------------------- slot CRUD
    @app.post(
        "/api/local/accounts", status_code=201, dependencies=[Depends(require_token)]
    )
    async def add_account(payload: dict[str, Any]) -> dict[str, Any]:
        return await service.add_slot(payload or {})

    @app.delete(
        "/api/local/accounts/{aid}", dependencies=[Depends(require_token)]
    )
    async def delete_account(aid: str) -> dict[str, Any]:
        await service.delete_slot(aid)
        return {"ok": True}

    @app.post(
        "/api/local/accounts/{aid}/activate",
        dependencies=[Depends(require_token)],
    )
    async def activate_account(aid: str) -> dict[str, Any]:
        return await service.activate_slot(aid)

    @app.patch(
        "/api/local/accounts/{aid}", dependencies=[Depends(require_token)]
    )
    async def patch_account(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await service.patch_slot(aid, payload or {})

    @app.post(
        "/api/local/accounts/{aid}/spam", dependencies=[Depends(require_token)]
    )
    async def set_spam(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await service.set_spam(aid, bool((payload or {}).get("running", False)))

    # ----------------------------------------------------------------- chats
    @app.patch(
        "/api/local/accounts/{aid}/chats/bulk",
        dependencies=[Depends(require_token)],
    )
    async def bulk_chats(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = payload or {}
        chat_ids = body.get("chatIds") or body.get("chat_ids")
        if chat_ids is not None and not isinstance(chat_ids, list):
            raise HTTPException(status_code=400, detail="chatIds must be a list")
        return await service.bulk_set_chats_enabled(
            aid,
            bool(body.get("enabled", False)),
            chat_ids,
            force_dialogs=bool(body.get("forceDialogs") or body.get("force_dialogs")),
        )

    @app.patch(
        "/api/local/accounts/{aid}/chats/{chat_id}",
        dependencies=[Depends(require_token)],
    )
    async def patch_chat(
        aid: str, chat_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await service.upsert_chat(aid, chat_id, payload or {})

    @app.post(
        "/api/local/accounts/{aid}/chats", dependencies=[Depends(require_token)]
    )
    async def add_chat(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload or {})
        chat_id = body.pop("chatId", body.pop("chat_id", None))
        if chat_id is None:
            raise HTTPException(status_code=400, detail="chatId обязателен")
        body.setdefault("enabled", True)
        return await service.upsert_chat(aid, chat_id, body)

    @app.delete(
        "/api/local/accounts/{aid}/chats/{chat_id}",
        dependencies=[Depends(require_token)],
    )
    async def delete_chat(aid: str, chat_id: str) -> dict[str, Any]:
        await service.delete_chat(aid, chat_id)
        return {"ok": True}

    # ----------------------------------------------------- Telethon auth flow
    @app.post(
        "/api/local/accounts/{aid}/auth/send_code",
        dependencies=[Depends(require_token)],
    )
    async def auth_send_code(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await service.auth_send_code(aid, str((payload or {}).get("phone", "")))

    @app.post(
        "/api/local/accounts/{aid}/auth/sign_in",
        dependencies=[Depends(require_token)],
    )
    async def auth_sign_in(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = payload or {}
        return await service.auth_sign_in(
            aid, str(body.get("code", "")), body.get("password")
        )

    @app.post(
        "/api/local/accounts/{aid}/auth/2fa",
        dependencies=[Depends(require_token)],
    )
    async def auth_2fa(aid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await service.auth_submit_2fa(aid, str((payload or {}).get("password", "")))

    # ----------------------------------------------------- session upload
    @app.post(
        "/api/local/accounts/upload_session",
        dependencies=[Depends(require_token)],
    )
    async def upload_session(
        slot_id: str = Form(...),
        proxy: str = Form(""),
        session_file: UploadFile = File(...),
    ) -> dict[str, Any]:
        data = await session_file.read()
        if len(data) == 0:
            raise HTTPException(status_code=400, detail="пустой файл")
        if len(data) > MAX_SESSION_BYTES:
            raise HTTPException(status_code=413, detail="файл слишком большой")
        return await service.upload_session(slot_id, data, proxy=proxy or None)

    @app.get("/api/local/activity", dependencies=[Depends(require_token)])
    async def get_activity(
        days: int = 14,
        account: str | None = None,
    ) -> dict[str, Any]:
        return service.get_activity(days=days, account_key=account or None)

    @app.post("/api/local/proxy/check", dependencies=[Depends(require_token)])
    async def proxy_check(payload: dict[str, Any]) -> dict[str, Any]:
        return await service.check_proxy(str((payload or {}).get("proxy", "")))


    # -------------------------------------------------------------- inviter
    inv = inviter_service

    @app.get("/api/local/inviter/overview", dependencies=[Depends(require_token)])
    async def inviter_overview(accountId: str = "") -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        return await inv.overview(accountId)

    @app.get(
        "/api/local/inviter/accounts/{aid}/dialogs",
        dependencies=[Depends(require_token)],
    )
    async def inviter_dialogs(aid: str) -> list[dict[str, Any]]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        return await inv.list_dialogs(aid)

    @app.post("/api/local/inviter/parse", dependencies=[Depends(require_token)])
    async def inviter_parse(payload: dict[str, Any]) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        body = payload or {}
        return await inv.parse(
            str(body.get("accountId", body.get("account_id", ""))),
            str(body.get("sourceRef", body.get("source_ref", ""))),
            force=bool(body.get("force", False)),
        )

    @app.post("/api/local/inviter/target", dependencies=[Depends(require_token)])
    async def inviter_target(payload: dict[str, Any]) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        body = payload or {}
        return await inv.set_target(
            str(body.get("accountId", body.get("account_id", ""))),
            str(body.get("targetRef", body.get("target_ref", ""))),
        )

    @app.get("/api/local/inviter/queue", dependencies=[Depends(require_token)])
    async def inviter_queue(accountId: str = "", limit: int = 0) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        return inv.get_queue(accountId, limit=limit)

    @app.get("/api/local/inviter/parsed", dependencies=[Depends(require_token)])
    async def inviter_parsed(
        accountId: str = "",
        includeArchived: bool = False,
    ) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        return inv.list_parsed_chats(accountId, include_archived=includeArchived)

    @app.patch(
        "/api/local/inviter/parsed/{source_chat_id}",
        dependencies=[Depends(require_token)],
    )
    async def inviter_parsed_update(
        source_chat_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        body = payload or {}
        return inv.update_parsed_chat(
            str(body.get("accountId", body.get("account_id", ""))),
            source_chat_id,
            traffic_category=str(body.get("trafficCategory", body.get("traffic_category", "other"))),
            note=str(body.get("note", "")),
        )

    @app.post(
        "/api/local/inviter/parsed/{source_chat_id}/archive",
        dependencies=[Depends(require_token)],
    )
    async def inviter_parsed_archive(
        source_chat_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        body = payload or {}
        return inv.archive_parsed_chat(
            str(body.get("accountId", body.get("account_id", ""))),
            source_chat_id,
            archived=bool(body.get("archived", True)),
        )

    @app.post(
        "/api/local/inviter/parsed/{source_chat_id}/clear_queue",
        dependencies=[Depends(require_token)],
    )
    async def inviter_parsed_clear_queue(
        source_chat_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        body = payload or {}
        return inv.clear_parsed_source_queue(
            str(body.get("accountId", body.get("account_id", ""))),
            source_chat_id,
        )

    @app.post("/api/local/inviter/run", dependencies=[Depends(require_token)])
    async def inviter_run(payload: dict[str, Any]) -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        body = payload or {}
        limit = int(body.get("limit", 0) or 0)
        delay = float(body.get("delay", 3.0) or 3.0)
        return await inv.start_job(
            str(body.get("accountId", body.get("account_id", ""))),
            limit=limit,
            delay=delay,
        )

    @app.post("/api/local/inviter/stop", dependencies=[Depends(require_token)])
    async def inviter_stop() -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        return inv.stop_job()

    @app.get("/api/local/inviter/job", dependencies=[Depends(require_token)])
    async def inviter_job() -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        return inv.get_job()

    @app.get("/api/local/inviter/parse", dependencies=[Depends(require_token)])
    async def inviter_parse_status() -> dict[str, Any]:
        if inv is None:
            raise HTTPException(status_code=503, detail="inviter disabled")
        return inv.get_parse()

    return app


async def serve_bot_api(
    service: BotService,
    inviter_service: InviterService | None = None,
) -> None:
    host = os.getenv("BOT_API_HOST", "0.0.0.0")
    port = int(os.getenv("BOT_API_PORT", "8080"))
    token = resolve_token()
    app = build_app(service, token, inviter_service)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=os.getenv("BOT_API_LOG_LEVEL", "warning"),
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    logger.info("Bot API listening on http://%s:%s", host, port)
    await server.serve()
