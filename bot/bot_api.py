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


def build_app(service: BotService, token: str) -> FastAPI:
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
    async def account_dialogs(aid: str) -> list[dict[str, Any]]:
        return await service.list_account_dialogs(aid)

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

    return app


async def serve_bot_api(service: BotService) -> None:
    host = os.getenv("BOT_API_HOST", "0.0.0.0")
    port = int(os.getenv("BOT_API_PORT", "8080"))
    token = resolve_token()
    app = build_app(service, token)
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
