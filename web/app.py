"""FastAPI app for the site (control plane)."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import deployer
from auth import (
    check_credentials,
    clear_cookie,
    current_user,
    issue_cookie,
    require_login,
)
from proxy import bot_request, get_json, healthcheck, request
from registry import BotRegistry

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

registry = BotRegistry()


def create_app() -> FastAPI:
    app = FastAPI(title="exsender", version="1.0.0")

    # ===================================================== auth pages
    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(FRONTEND_DIR / "login.html")

    @app.post("/api/auth/login")
    async def login(payload: dict[str, Any], response: Response):
        login_ = str((payload or {}).get("login", "")).strip()
        pwd = str((payload or {}).get("password", ""))
        if not check_credentials(login_, pwd):
            raise HTTPException(status_code=401, detail="bad credentials")
        issue_cookie(response)
        return {"ok": True}

    @app.post("/api/auth/logout")
    async def logout(response: Response):
        clear_cookie(response)
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request):
        return {"user": current_user(request)}

    # ===================================================== bots registry
    @app.get("/api/bots", dependencies=[Depends(require_login)])
    async def list_bots():
        items = [b.public() for b in registry.list()]
        # add quick health
        results = await asyncio.gather(
            *(healthcheck(b) for b in registry.list()), return_exceptions=True
        )
        for it, res in zip(items, results):
            if isinstance(res, dict):
                it["reachable"] = bool(res.get("reachable"))
            else:
                it["reachable"] = False
        return {"bots": items}

    @app.post("/api/bots", status_code=201, dependencies=[Depends(require_login)])
    async def add_bot(payload: dict[str, Any]):
        body = payload or {}
        host = str(body.get("host", "")).strip()
        if not host:
            raise HTTPException(status_code=400, detail="host обязателен")
        rec = registry.add(
            host=host,
            ssh_port=int(body.get("sshPort", body.get("ssh_port", 22))),
            ssh_user=str(body.get("sshUser", body.get("ssh_user", "root"))),
            alias=str(body.get("alias", "")),
            install_dir=str(body.get("installDir", body.get("install_dir", "/opt/userbot"))),
            api_port=int(body.get("apiPort", body.get("api_port", 8080))),
            api_token=str(body.get("apiToken", body.get("api_token", ""))),
        )
        return rec.public()

    @app.patch("/api/bots/{bid}", dependencies=[Depends(require_login)])
    async def patch_bot(bid: str, payload: dict[str, Any]):
        allowed = {
            "alias": "alias",
            "host": "host",
            "sshPort": "ssh_port",
            "sshUser": "ssh_user",
            "installDir": "install_dir",
            "apiPort": "api_port",
            "apiToken": "api_token",
            "status": "status",
        }
        patch = {allowed[k]: v for k, v in (payload or {}).items() if k in allowed}
        rec = registry.update(bid, **patch)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        return rec.public()

    @app.delete("/api/bots/{bid}", dependencies=[Depends(require_login)])
    async def delete_bot(bid: str):
        if not registry.remove(bid):
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    # ===================================================== deploy / ops
    def _start_bg(coro) -> None:
        asyncio.create_task(coro)

    @app.post("/api/bots/{bid}/deploy", dependencies=[Depends(require_login)])
    async def deploy_bot(bid: str, payload: dict[str, Any]):
        rec = registry.get(bid)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        body = payload or {}
        password = body.get("sshPassword") or body.get("password") or None
        env = {
            "API_ID": str(body.get("apiId", body.get("API_ID", ""))).strip(),
            "API_HASH": str(body.get("apiHash", body.get("API_HASH", ""))).strip(),
            "TG_BOT_TOKEN": str(body.get("tgBotToken", body.get("TG_BOT_TOKEN", ""))).strip(),
            "ADMIN_USER_IDS": str(body.get("adminUserIds", body.get("ADMIN_USER_IDS", ""))).strip(),
        }
        if (not env["API_ID"] or not env["API_HASH"]) and rec.has_ssh_key and rec.status != "new":
            try:
                remote = await deployer.read_remote_env(rec, password=password)
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"API_ID/API_HASH не заданы и не удалось прочитать .env с VDS: {e}",
                ) from e
            env["API_ID"] = env["API_ID"] or remote.get("API_ID", "")
            env["API_HASH"] = env["API_HASH"] or remote.get("API_HASH", "")
            env["TG_BOT_TOKEN"] = env["TG_BOT_TOKEN"] or remote.get("BOT_TOKEN", remote.get("TG_BOT_TOKEN", ""))
            env["ADMIN_USER_IDS"] = env["ADMIN_USER_IDS"] or remote.get("ADMIN_USER_IDS", "")
        if not env["API_ID"] or not env["API_HASH"]:
            raise HTTPException(status_code=400, detail="API_ID и API_HASH обязательны")
        _start_bg(deployer.deploy(bid, registry, password=password, env=env))
        return {"ok": True, "operation": "deploy"}

    @app.post("/api/bots/{bid}/restart", dependencies=[Depends(require_login)])
    async def restart_bot(bid: str, payload: dict[str, Any] | None = None):
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.restart_remote(bid, registry, password=password))
        return {"ok": True, "operation": "restart"}

    @app.post("/api/bots/{bid}/stop", dependencies=[Depends(require_login)])
    async def stop_bot(bid: str, payload: dict[str, Any] | None = None):
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.stop_remote(bid, registry, password=password))
        return {"ok": True, "operation": "stop"}

    @app.post("/api/bots/{bid}/uninstall", dependencies=[Depends(require_login)])
    async def uninstall_bot(bid: str, payload: dict[str, Any] | None = None):
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.uninstall_remote(bid, registry, password=password))
        return {"ok": True, "operation": "uninstall"}

    @app.get("/api/bots/{bid}/deploy/log", dependencies=[Depends(require_login)])
    async def deploy_log(bid: str):
        if registry.get(bid) is None:
            raise HTTPException(status_code=404, detail="not found")
        return deployer.get_state(bid).snapshot()

    # ===================================================== proxy to bot API
    PROXY_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

    async def _proxy(bid: str, method: str, sub: str, request: Request) -> Response:
        rec = registry.get(bid)
        if rec is None:
            raise HTTPException(status_code=404, detail="bot not found")
        if not rec.api_token:
            raise HTTPException(status_code=400, detail="у бота нет API токена — заверши deploy")

        body = await request.body()
        ct = request.headers.get("content-type", "")
        extra: dict[str, str] = {}
        if ct:
            extra["Content-Type"] = ct

        try:
            r = await bot_request(
                rec,
                method,
                sub,
                content=body if body else None,
                extra_headers=extra or None,
                params=dict(request.query_params),
                timeout=PROXY_TIMEOUT,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

        resp_headers = {
            k: v for k, v in r.headers.items()
            if k.lower() not in ("content-encoding", "transfer-encoding", "connection")
        }
        return Response(content=r.content, status_code=r.status_code, headers=resp_headers)

    @app.api_route(
        "/api/bots/{bid}/proxy/{sub:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
        dependencies=[Depends(require_login)],
    )
    async def proxy(bid: str, sub: str, request: Request):
        return await _proxy(bid, request.method, sub, request)

    # ===================================================== bot overview convenience
    @app.get("/api/bots/{bid}/overview", dependencies=[Depends(require_login)])
    async def bot_overview(bid: str):
        rec = registry.get(bid)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        if not rec.api_token:
            return {"reachable": False, "error": "no api token"}
        try:
            status, data = await get_json(rec, "overview")
        except Exception as e:
            return {"reachable": False, "error": str(e)}
        if status != 200:
            return {"reachable": False, "status": status, "body": data}
        return {"reachable": True, "data": data}

    # ===================================================== session upload via site
    @app.post(
        "/api/bots/{bid}/accounts/upload_session",
        dependencies=[Depends(require_login)],
    )
    async def upload_session(
        bid: str,
        slot_id: str = Form(...),
        proxy_str: str = Form("", alias="proxy"),
        session_file: UploadFile = File(...),
    ):
        rec = registry.get(bid)
        if rec is None or not rec.api_token:
            raise HTTPException(status_code=404, detail="bot not found or not deployed")
        data = await session_file.read()
        files = {
            "session_file": (session_file.filename or "tg.session", data,
                             session_file.content_type or "application/octet-stream"),
        }
        form = {"slot_id": slot_id, "proxy": proxy_str or ""}
        r = await bot_request(
            rec, "POST", "accounts/upload_session", files=files, data=form
        )
        try:
            return JSONResponse(status_code=r.status_code, content=r.json())
        except ValueError:
            return Response(status_code=r.status_code, content=r.content)

    # ===================================================== static frontend
    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        if not current_user(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(FRONTEND_DIR / "index.html")

    if FRONTEND_DIR.is_dir():
        # Mount static AFTER all /api routes to avoid eclipsing them.
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")

    return app
