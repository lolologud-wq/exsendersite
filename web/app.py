"""FastAPI app for the site (control plane)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
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
import admin_ops
import payments
import security
from auth import (
    admin_logins,
    clear_cookie,
    current_identity,
    current_user_id,
    issue_admin_cookie,
    issue_user_cookie,
    require_admin,
    require_login,
    require_subscription,
    verify_admin,
)
from proxy import bot_request, get_json, healthcheck, request
from registry import BotRegistry
from users import PLANS, UserStore, plan_duration_seconds, plan_info

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
registry = BotRegistry()
users = UserStore()
invoices = payments.InvoiceStore()
audit = admin_ops.AuditStore()
promos = admin_ops.PromoStore()
notifications = admin_ops.NotificationStore()


def _panel_access(request: Request) -> dict[str, Any]:
    """Logged-in panel user with active subscription (admin exempt)."""
    ident = current_identity(request, users)
    if ident["kind"] == "anon":
        raise HTTPException(status_code=401, detail="not authenticated")
    if ident["kind"] == "user":
        rec = ident["record"]
        if rec.blocked:
            raise HTTPException(status_code=403, detail="account blocked")
        if rec.plan_expires_at <= time.time():
            raise HTTPException(status_code=403, detail="subscription required")
    return ident


def _panel_is_admin(ident: dict[str, Any]) -> bool:
    return ident.get("kind") == "admin"


def _panel_user_id(ident: dict[str, Any]) -> str:
    if ident.get("kind") != "user":
        return ""
    return ident["record"].id


def _bots_visible(ident: dict[str, Any]) -> list:
    if _panel_is_admin(ident):
        return registry.list()
    return registry.list_for(_panel_user_id(ident))


def _require_bot(request: Request, bid: str):
    ident = _panel_access(request)
    bid = security.validate_safe_id(bid, name="bot_id")
    if _panel_is_admin(ident):
        rec = registry.get(bid)
    else:
        rec = registry.get_for(bid, _panel_user_id(ident))
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    return ident, rec


def create_app() -> FastAPI:
    app = FastAPI(title="exsender", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(security.SecurityMiddleware)

    @app.on_event("startup")
    async def _security_startup() -> None:
        security.startup_security_check()

    def _admin_guard(request: Request) -> str:
        return require_admin(request, users)

    # ===================================================== auth pages
    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        ident = current_identity(request, users)
        if ident["kind"] == "admin":
            return RedirectResponse("/admin", status_code=303)
        if ident["kind"] == "user":
            return RedirectResponse("/app", status_code=303)
        return FileResponse(FRONTEND_DIR / "login.html")

    @app.get("/register", include_in_schema=False)
    async def register_page(request: Request):
        ident = current_identity(request, users)
        if ident["kind"] != "anon":
            return RedirectResponse("/app", status_code=303)
        return FileResponse(FRONTEND_DIR / "register.html")

    @app.get("/profile", include_in_schema=False)
    async def profile_page(request: Request):
        ident = current_identity(request, users)
        if ident["kind"] == "anon":
            return RedirectResponse("/login", status_code=303)
        return FileResponse(FRONTEND_DIR / "profile.html")

    @app.get("/api/auth/csrf", include_in_schema=False)
    async def auth_csrf(request: Request, response: Response):
        token = security.issue_csrf_token(response, request)
        return {"ok": True, "csrf": token}

    @app.post("/api/auth/login")
    async def login(payload: dict[str, Any], request: Request, response: Response):
        import asyncio

        ip = security.client_ip(request)
        body = payload or {}
        if security.honeypot_triggered(body):
            security.record_auth_failure(ip)
            security.ban_ip(ip, security.AUTH_BAN_MINUTES)
            raise HTTPException(status_code=400, detail="bad credentials")

        login_ = str(body.get("login", "")).strip()[:320]
        pwd = str(body.get("password", ""))[: security.MAX_PASSWORD_LEN]
        admin_attempt = login_ in admin_logins()
        fails = security.count_recent_auth_fails(ip) if not admin_attempt else 0
        if fails:
            await asyncio.sleep(security.auth_delay(fails))

        admin_login = verify_admin(login_, pwd)
        if admin_login:
            security.record_auth_success(ip)
            issue_admin_cookie(response, admin_login, request)
            security.issue_csrf_token(response, request)
            return {"ok": True, "kind": "admin", "redirect": "/admin"}

        blocked_rec = users.by_email(login_)
        if blocked_rec is not None and blocked_rec.blocked:
            security.record_auth_failure(ip, skip_ban=admin_attempt)
            raise HTTPException(status_code=401, detail="bad credentials")

        rec = users.verify(login_, pwd)
        if rec is None:
            security.record_auth_failure(ip, skip_ban=admin_attempt)
            raise HTTPException(status_code=401, detail="bad credentials")

        security.record_auth_success(ip)
        users.touch_login(rec.id)
        issue_user_cookie(response, rec.id, request)
        security.issue_csrf_token(response, request)
        return {"ok": True, "kind": "user", "redirect": "/app"}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        clear_cookie(response)
        security.issue_csrf_token(response, request)
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request, response: Response):
        security.issue_csrf_token(response, request)
        ident = current_identity(request, users)
        if ident["kind"] == "admin":
            return {"user": ident["user"], "kind": "admin"}
        if ident["kind"] == "user":
            rec = ident["record"]
            return {"user": rec.email, "kind": "user", "profile": rec.public()}
        return {"user": None}

    @app.post("/api/auth/register")
    async def register(payload: dict[str, Any], request: Request, response: Response):
        body = payload or {}
        ip = security.client_ip(request)
        if security.honeypot_triggered(body):
            security.record_auth_failure(ip)
            raise HTTPException(status_code=400, detail="registration failed")

        email = security.validate_email(str(body.get("email", body.get("contact", ""))))
        password = security.validate_password(str(body.get("password", "")))
        name = str(body.get("name", "")).strip()[:80]
        referred_by = ""
        ref_code = str(body.get("ref", body.get("referral", ""))).strip().upper()[:32]
        if ref_code:
            referrer = users.by_referral_code(ref_code)
            if referrer is not None:
                referred_by = referrer.id
        try:
            rec = users.create(email=email, password=password, name=name, referred_by=referred_by)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        issue_user_cookie(response, rec.id, request)
        security.issue_csrf_token(response, request)
        return {"ok": True, "redirect": "/profile", "user": rec.public()}

    # ===================================================== profile / billing
    @app.get("/api/users/me", dependencies=[Depends(require_login)])
    async def get_me(request: Request):
        ident = current_identity(request, users)
        plans_list = [
            {"id": pid, **p, "priceUsd": p["price_usd"]}
            for pid, p in PLANS.items()
        ]
        if ident["kind"] == "user":
            rec = ident["record"]
            history = [
                {
                    "invoiceId": i.invoice_id,
                    "plan": i.plan,
                    "amountUsd": i.amount_usd,
                    "status": i.status,
                    "createdAt": i.created_at,
                    "paidAt": i.paid_at,
                }
                for i in invoices.user_history(rec.id)
            ]
            history.sort(key=lambda x: x["createdAt"], reverse=True)
            return {
                "kind": "user",
                "profile": rec.public(),
                "plans": plans_list,
                "history": history[:20],
                "cryptoBotConfigured": payments.crypto_bot_configured(),
                "notifications": notifications.for_user(rec.id),
                "referralLink": f"/register?ref={rec.referral_code}",
            }
        return {
            "kind": "admin",
            "profile": {
                "email": ident["user"],
                "planActive": True,
                "plan": "admin",
                "name": ident["user"],
            },
            "plans": plans_list,
            "cryptoBotConfigured": payments.crypto_bot_configured(),
        }

    def _admin_stats_payload() -> dict[str, Any]:
        now = time.time()
        month_start = now - 30 * 86400
        all_users = users.list_users()
        all_invoices = invoices.list_all()

        paid = [i for i in all_invoices if i.status == "paid"]
        pending = [i for i in all_invoices if i.status in ("active", "pending")]
        revenue_total = sum(i.amount_usd for i in paid)
        revenue_month = sum(i.amount_usd for i in paid if i.paid_at >= month_start)

        by_plan: dict[str, int] = {}
        for inv in paid:
            by_plan[inv.plan] = by_plan.get(inv.plan, 0) + 1

        email_by_id = {u.id: u.email for u in all_users}
        recent_payments = sorted(paid, key=lambda x: x.paid_at or x.created_at, reverse=True)[:25]
        recent_users = sorted(all_users, key=lambda u: u.created_at, reverse=True)[:25]

        return {
            "usersTotal": len(all_users),
            "usersActiveSub": sum(1 for u in all_users if u.plan_expires_at > now),
            "usersRegisteredMonth": sum(1 for u in all_users if u.created_at >= month_start),
            "invoicesPaid": len(paid),
            "invoicesPending": len(pending),
            "revenueTotalUsd": round(revenue_total, 2),
            "revenueMonthUsd": round(revenue_month, 2),
            "byPlan": by_plan,
            "cryptoBotConfigured": payments.crypto_bot_configured(),
            "recentPayments": [
                {
                    "invoiceId": i.invoice_id,
                    "email": email_by_id.get(i.user_id, i.user_id),
                    "plan": i.plan,
                    "amountUsd": i.amount_usd,
                    "paidAt": i.paid_at,
                    "createdAt": i.created_at,
                }
                for i in recent_payments
            ],
            "recentUsers": [u.admin_view() for u in recent_users],
            "revenueChart": admin_ops.revenue_by_day(paid, days=30),
            "promos": [p.public() for p in promos.list_all()],
            "auditLog": [
                {
                    "id": e.id,
                    "admin": e.admin,
                    "action": e.action,
                    "target": e.target,
                    "details": e.details,
                    "createdAt": e.created_at,
                }
                for e in audit.list_recent(40)
            ],
        }

    @app.get("/admin", include_in_schema=False)
    async def admin_page(request: Request):
        ident = current_identity(request, users)
        if ident["kind"] != "admin":
            return RedirectResponse("/login", status_code=303)
        return FileResponse(FRONTEND_DIR / "admin.html")

    @app.get("/api/admin/stats")
    async def admin_stats(request: Request):
        _admin_guard(request)
        return _admin_stats_payload()

    @app.get("/api/admin/users")
    async def admin_list_users(request: Request):
        _admin_guard(request)
        all_users = sorted(users.list_users(), key=lambda u: u.created_at, reverse=True)
        return {"users": [u.admin_view() for u in all_users]}

    @app.post("/api/admin/users/{user_id}/plan")
    async def admin_grant_plan(user_id: str, payload: dict[str, Any], request: Request):
        user_id = security.validate_safe_id(user_id, name="user_id")
        admin_login = _admin_guard(request)
        body = payload or {}
        plan_id = str(body.get("plan", "month")).strip()
        days = int(body.get("days", 0) or 0)
        if plan_id not in PLANS and days <= 0:
            raise HTTPException(status_code=400, detail="Укажи plan или days")
        if days <= 0:
            days = int(PLANS[plan_id]["duration_days"])
        rec = users.get(user_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="user not found")
        users.extend_plan_days(user_id, plan_id or rec.plan or "month", days, invoice_id="admin")
        audit.add(
            admin_login,
            "grant_plan",
            target=rec.email,
            details={"plan": plan_id, "days": days},
        )
        return {"ok": True, "profile": users.get(user_id).public()}

    @app.post("/api/admin/users/{user_id}/block")
    async def admin_block_user(user_id: str, payload: dict[str, Any], request: Request):
        user_id = security.validate_safe_id(user_id, name="user_id")
        admin_login = _admin_guard(request)
        blocked = bool((payload or {}).get("blocked", True))
        rec = users.get(user_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="user not found")
        users.set_blocked(user_id, blocked)
        audit.add(
            admin_login,
            "block" if blocked else "unblock",
            target=rec.email,
        )
        return {"ok": True, "profile": users.get(user_id).admin_view()}

    @app.get("/api/admin/revenue-chart")
    async def admin_revenue_chart(request: Request, days: int = 30):
        _admin_guard(request)
        paid = [i for i in invoices.list_all() if i.status == "paid"]
        return {"days": days, "points": admin_ops.revenue_by_day(paid, days=days)}

    @app.get("/api/admin/promos")
    async def admin_list_promos(request: Request):
        _admin_guard(request)
        return {"promos": [p.public() for p in promos.list_all()]}

    @app.post("/api/admin/promos")
    async def admin_create_promo(payload: dict[str, Any], request: Request):
        admin_login = _admin_guard(request)
        body = payload or {}
        code = str(body.get("code", "")).strip()
        try:
            rec = promos.create(
                code,
                discount_pct=float(body.get("discountPct", body.get("discount_pct", 0)) or 0),
                bonus_days=int(body.get("bonusDays", body.get("bonus_days", 0)) or 0),
                max_uses=int(body.get("maxUses", body.get("max_uses", 0)) or 0),
                expires_at=float(body.get("expiresAt", body.get("expires_at", 0)) or 0),
                owner_user_id=str(body.get("ownerUserId", "")).strip(),
                note=str(body.get("note", "")).strip(),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        audit.add(admin_login, "create_promo", target=rec.code, details=rec.public())
        return {"ok": True, "promo": rec.public()}

    @app.patch("/api/admin/promos/{code}")
    async def admin_toggle_promo(code: str, payload: dict[str, Any], request: Request):
        admin_login = _admin_guard(request)
        active = bool((payload or {}).get("active", True))
        rec = promos.set_active(code, active)
        if rec is None:
            raise HTTPException(status_code=404, detail="promo not found")
        audit.add(admin_login, "toggle_promo", target=rec.code, details={"active": active})
        return {"ok": True, "promo": rec.public()}

    @app.get("/api/admin/audit-log")
    async def admin_audit_log(request: Request, limit: int = 50):
        _admin_guard(request)
        return {
            "items": [
                {
                    "id": e.id,
                    "admin": e.admin,
                    "action": e.action,
                    "target": e.target,
                    "details": e.details,
                    "createdAt": e.created_at,
                }
                for e in audit.list_recent(limit)
            ]
        }

    @app.post("/api/admin/notify")
    async def admin_notify(payload: dict[str, Any], request: Request):
        admin_login = _admin_guard(request)
        body = payload or {}
        message = str(body.get("message", "")).strip()
        title = str(body.get("title", "exsender")).strip() or "exsender"
        raw_ids = body.get("userIds", body.get("user_ids"))
        user_ids: list[str] | None = None
        if raw_ids and raw_ids != "all":
            user_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
        try:
            count = notifications.send(
                message=message,
                title=title,
                user_ids=user_ids,
                admin=admin_login,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        audit.add(
            admin_login,
            "notify",
            target="all" if not user_ids else f"{len(user_ids)} users",
            details={"title": title, "message": message[:200]},
        )
        return {"ok": True, "sent": count}

    @app.get("/api/users/notifications", dependencies=[Depends(require_login)])
    async def user_notifications(request: Request):
        uid = current_user_id(request)
        if not uid:
            return {"notifications": []}
        return {"notifications": notifications.for_user(uid)}

    @app.post("/api/users/notifications/{nid}/read", dependencies=[Depends(require_login)])
    async def user_notification_read(nid: str, request: Request):
        nid = security.validate_safe_id(nid, name="notification_id")
        uid = current_user_id(request)
        if not uid:
            raise HTTPException(status_code=400, detail="users only")
        notifications.mark_read(uid, nid)
        return {"ok": True}

    @app.post("/api/payments/validate-promo", dependencies=[Depends(require_login)])
    async def validate_promo(payload: dict[str, Any], request: Request):
        uid = current_user_id(request)
        if not uid:
            raise HTTPException(status_code=400, detail="users only")
        code = str((payload or {}).get("code", "")).strip()
        plan_id = str((payload or {}).get("plan", "")).strip()
        promo, err = promos.validate(code)
        if promo is None:
            raise HTTPException(status_code=400, detail=err)
        info = plan_info(plan_id) if plan_id else None
        base = float(info["price_usd"]) if info else 0
        final = admin_ops.apply_promo_price(base, promo) if base else 0
        return {
            "ok": True,
            "code": promo.code,
            "discountPct": promo.discount_pct,
            "bonusDays": promo.bonus_days,
            "amountUsd": final,
            "baseUsd": base,
        }

    @app.post("/api/payments/create-invoice", dependencies=[Depends(require_login)])
    async def create_invoice(payload: dict[str, Any], request: Request):
        uid = current_user_id(request)
        if not uid:
            raise HTTPException(status_code=400, detail="Только для пользовательских аккаунтов")
        rec = users.get(uid)
        if rec is None:
            raise HTTPException(status_code=404, detail="user not found")
        plan_id = str((payload or {}).get("plan", "")).strip()
        promo_code = str((payload or {}).get("promo", (payload or {}).get("promoCode", ""))).strip().upper()
        info = plan_info(plan_id)
        if not info:
            raise HTTPException(status_code=400, detail="Неизвестный тариф")
        if not payments.crypto_bot_configured():
            raise HTTPException(
                status_code=503,
                detail="Платежи временно недоступны (CRYPTO_BOT_TOKEN не задан)",
            )
        base_usd = float(info["price_usd"])
        amount_usd = base_usd
        promo_rec = None
        if promo_code:
            promo_rec, err = promos.validate(promo_code)
            if promo_rec is None:
                raise HTTPException(status_code=400, detail=err)
            amount_usd = admin_ops.apply_promo_price(base_usd, promo_rec)
        if amount_usd < 0.01:
            amount_usd = 0.01
        try:
            result = await payments.create_invoice(
                user_id=rec.id,
                plan=plan_id,
                amount_usd=amount_usd,
                description=f"exsender {info['label']} — {info['duration_days']} дн.",
                payload=json.dumps({"uid": rec.id, "plan": plan_id, "promo": promo_code}),
            )
        except payments.CryptoBotError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        inv = payments.InvoiceRecord(
            invoice_id=str(result.get("invoice_id") or result.get("hash") or ""),
            user_id=rec.id,
            plan=plan_id,
            amount_usd=amount_usd,
            base_amount_usd=base_usd,
            promo_code=promo_code,
            asset=str(result.get("asset") or result.get("accepted_assets") or ""),
            pay_url=str(result.get("pay_url") or result.get("bot_invoice_url") or ""),
        )
        if not inv.invoice_id:
            raise HTTPException(status_code=502, detail="Crypto Bot не вернул invoice_id")
        invoices.add(inv)
        return {
            "ok": True,
            "invoiceId": inv.invoice_id,
            "payUrl": inv.pay_url,
            "amountUsd": inv.amount_usd,
            "baseUsd": base_usd,
            "promo": promo_code or None,
            "plan": plan_id,
        }

    @app.get("/api/payments/check/{invoice_id}", dependencies=[Depends(require_login)])
    async def check_invoice(invoice_id: str, request: Request):
        invoice_id = security.validate_safe_id(invoice_id, name="invoice_id")
        uid = current_user_id(request)
        local = invoices.get(invoice_id)
        if local is None or (uid and local.user_id != uid):
            raise HTTPException(status_code=404, detail="invoice not found")
        if local.status != "paid":
            try:
                rows = await payments.get_invoices([invoice_id])
            except payments.CryptoBotError:
                rows = []
            for row in rows:
                if str(row.get("invoice_id")) != invoice_id:
                    continue
                status = str(row.get("status") or "").lower()
                if status == "paid":
                    _activate_paid_invoice(local)
                elif status in ("expired", "active"):
                    local.status = status
        rec = users.get(local.user_id) if local else None
        return {
            "ok": True,
            "status": local.status,
            "profile": rec.public() if rec else None,
        }

    def _activate_paid_invoice(inv: payments.InvoiceRecord) -> None:
        if inv.status == "paid":
            return
        duration = plan_duration_seconds(inv.plan)
        if duration <= 0:
            return
        paid_before = len(invoices.paid_for_user(inv.user_id))
        users.set_plan(
            inv.user_id,
            inv.plan,
            duration_sec=duration,
            invoice_id=inv.invoice_id,
        )
        if inv.promo_code:
            promo_rec = promos.get(inv.promo_code)
            if promo_rec is not None:
                promos.apply_use(inv.promo_code)
                if promo_rec.bonus_days > 0:
                    users.extend_plan_days(
                        inv.user_id,
                        inv.plan,
                        promo_rec.bonus_days,
                        invoice_id=f"promo-{inv.promo_code}",
                    )
        invoices.mark_paid(inv.invoice_id)
        # Referral bonus on first payment
        if paid_before == 0:
            buyer = users.get(inv.user_id)
            if buyer and buyer.referred_by:
                referrer = users.get(buyer.referred_by)
                if referrer is not None:
                    bonus = admin_ops.REFERRAL_BONUS_DAYS
                    users.extend_plan_days(
                        referrer.id,
                        referrer.plan or "week",
                        bonus,
                        invoice_id=f"ref-{buyer.id}",
                    )

    @app.post("/api/payments/webhook")
    async def crypto_bot_webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("crypto-pay-api-signature", "")
        token = os.getenv("CRYPTO_BOT_TOKEN", "").strip()
        if not token:
            raise HTTPException(status_code=503, detail="webhook disabled")
        if not payments.verify_webhook_signature(token, body, signature):
            raise HTTPException(status_code=401, detail="bad signature")
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="bad json") from e
        update_type = str(data.get("update_type") or "")
        payload_obj = data.get("payload") or {}
        if update_type == "invoice_paid":
            inv_id = str(payload_obj.get("invoice_id") or "")
            local = invoices.get(inv_id)
            if local is not None:
                _activate_paid_invoice(local)
        return {"ok": True}

    # ===================================================== bots registry
    @app.get("/api/bots", dependencies=[Depends(require_login)])
    async def list_bots(request: Request):
        ident = _panel_access(request)
        bots = _bots_visible(ident)
        items = [b.public() for b in bots]
        results = await asyncio.gather(
            *(healthcheck(b) for b in bots), return_exceptions=True
        )
        for it, res in zip(items, results):
            if isinstance(res, dict):
                it["reachable"] = bool(res.get("reachable"))
            else:
                it["reachable"] = False
        return {"bots": items}

    @app.post("/api/bots", status_code=201)
    async def add_bot(payload: dict[str, Any], request: Request):
        ident = _panel_access(request)
        body = payload or {}
        host = str(body.get("host", "")).strip()
        if not host:
            raise HTTPException(status_code=400, detail="host обязателен")
        owner_id = "" if _panel_is_admin(ident) else _panel_user_id(ident)
        rec = registry.add(
            host=host,
            ssh_port=int(body.get("sshPort", body.get("ssh_port", 22))),
            ssh_user=str(body.get("sshUser", body.get("ssh_user", "root"))),
            alias=str(body.get("alias", "")),
            install_dir=str(body.get("installDir", body.get("install_dir", "/opt/userbot"))),
            api_port=int(body.get("apiPort", body.get("api_port", 8080))),
            api_token=str(body.get("apiToken", body.get("api_token", ""))),
            owner_id=owner_id,
        )
        return rec.public()

    @app.patch("/api/bots/{bid}")
    async def patch_bot(bid: str, payload: dict[str, Any], request: Request):
        _, rec = _require_bot(request, bid)
        bid = rec.id
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

    @app.delete("/api/bots/{bid}")
    async def delete_bot(bid: str, request: Request):
        _, rec = _require_bot(request, bid)
        if not registry.remove(rec.id):
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    # ===================================================== deploy / ops
    def _start_bg(coro) -> None:
        asyncio.create_task(coro)

    @app.post("/api/bots/{bid}/deploy")
    async def deploy_bot(bid: str, payload: dict[str, Any], request: Request):
        _, rec = _require_bot(request, bid)
        bid = rec.id
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

    @app.post("/api/bots/{bid}/restart")
    async def restart_bot(bid: str, payload: dict[str, Any] | None, request: Request):
        _, rec = _require_bot(request, bid)
        bid = rec.id
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.restart_remote(bid, registry, password=password))
        return {"ok": True, "operation": "restart"}

    @app.post("/api/bots/{bid}/stop")
    async def stop_bot(bid: str, payload: dict[str, Any] | None, request: Request):
        _, rec = _require_bot(request, bid)
        bid = rec.id
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.stop_remote(bid, registry, password=password))
        return {"ok": True, "operation": "stop"}

    @app.post("/api/bots/{bid}/uninstall")
    async def uninstall_bot(bid: str, payload: dict[str, Any] | None, request: Request):
        _, rec = _require_bot(request, bid)
        bid = rec.id
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.uninstall_remote(bid, registry, password=password))
        return {"ok": True, "operation": "uninstall"}

    @app.get("/api/bots/{bid}/deploy/log")
    async def deploy_log(bid: str, request: Request):
        _, rec = _require_bot(request, bid)
        bid = rec.id
        return deployer.get_state(bid).snapshot()

    # ===================================================== proxy to bot API
    PROXY_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

    async def _proxy(bid: str, method: str, sub: str, request: Request) -> Response:
        _, rec = _require_bot(request, bid)
        bid = rec.id
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
    @app.get("/api/bots/{bid}/overview")
    async def bot_overview(bid: str, request: Request):
        _, rec = _require_bot(request, bid)
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
    @app.post("/api/bots/{bid}/accounts/upload_session")
    async def upload_session(
        bid: str,
        request: Request,
        slot_id: str = Form(...),
        proxy_str: str = Form("", alias="proxy"),
        session_file: UploadFile = File(...),
    ):
        _, rec = _require_bot(request, bid)
        bid = rec.id
        slot_id = security.validate_safe_id(slot_id, name="slot_id")
        if not rec.api_token:
            raise HTTPException(status_code=404, detail="bot not found or not deployed")
        data = await session_file.read()
        if len(data) > 512 * 1024:
            raise HTTPException(status_code=413, detail="session file too large")
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
    async def landing(request: Request):
        return FileResponse(FRONTEND_DIR / "landing.html")

    @app.get("/app", include_in_schema=False)
    async def app_root(request: Request):
        ident = current_identity(request, users)
        if ident["kind"] == "anon":
            return RedirectResponse("/login", status_code=303)
        return FileResponse(FRONTEND_DIR / "index.html")

    if FRONTEND_DIR.is_dir():
        # Mount static AFTER all /api routes to avoid eclipsing them.
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")

    return app
