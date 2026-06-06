"""FastAPI app for the site (control plane)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
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
import referrals
import changelog as changelog_mod
import overview_cache
import security
import trial_limits
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
from proxy import (
    OVERVIEW_TIMEOUT,
    apply_health_to_item,
    bot_request,
    fetch_overview,
    fetch_overview_pack,
    get_json,
    refresh_health_background,
    refresh_overviews_live,
    request,
)
from registry import BotRecord, BotRegistry
from register_verify import PendingRegistrationStore
from users import PLANS, TRIAL_DAYS, UserStore, hash_password, plan_duration_seconds, plan_info
from verify_bot import (
    VERIFY_BOT_TOKEN,
    bot_deep_link,
    resolve_bot_username,
    start_verify_bot_background,
)

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
referral_ledger = referrals.ReferralLedger()
changelog_store = changelog_mod.ChangelogStore()
pending_regs = PendingRegistrationStore()

INVITER_HOSTS = frozenset(
    h.strip().lower()
    for h in os.getenv("INVITER_HOSTS", "inviter.exsender.top").split(",")
    if h.strip()
)
SITE_PUBLIC_BASE = os.getenv("SITE_PUBLIC_URL", "https://exsender.top").rstrip("/")


def _request_host(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].lower()


def _is_inviter_host(request: Request) -> bool:
    return _request_host(request) in INVITER_HOSTS


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


def _bot_suite(rec: BotRecord) -> str:
    return (getattr(rec, "suite", None) or "sender").strip().lower() or "sender"


def _bots_sender(ident: dict[str, Any]) -> list:
    return [b for b in _bots_visible(ident) if _bot_suite(b) != "inviter"]


def _bots_inviter(ident: dict[str, Any]) -> list:
    return _bots_visible(ident)


def _require_bot(request: Request, bid: str):
    ident = _panel_access(request)
    bid = security.validate_safe_id(bid, name="bot_id")
    if _panel_is_admin(ident):
        rec = registry.get(bid)
    else:
        rec = registry.get_for(bid, _panel_user_id(ident))
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    if _bot_suite(rec) == "inviter":
        raise HTTPException(status_code=404, detail="not found")
    return ident, rec


def _require_inviter_bot(bid: str) -> BotRecord:
    """Any registered bot — inviter may proxy exsender VDS and accounts."""
    bid = security.validate_safe_id(bid, name="bot_id")
    rec = registry.get(bid)
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    return rec


def _require_inviter_owned_bot(bid: str) -> BotRecord:
    """Inviter-suite bots only — CRUD, deploy, uninstall."""
    rec = _require_inviter_bot(bid)
    if _bot_suite(rec) != "inviter":
        raise HTTPException(status_code=404, detail="not found")
    return rec


def _inviter_access(request: Request) -> dict[str, Any]:
    """Logged-in customer with active plan, or admin."""
    return _panel_access(request)


def _require_inviter_bot_access(request: Request, bid: str) -> tuple[dict[str, Any], BotRecord]:
    ident = _inviter_access(request)
    bid = security.validate_safe_id(bid, name="bot_id")
    if _panel_is_admin(ident):
        rec = registry.get(bid)
    else:
        rec = registry.get_for(bid, _panel_user_id(ident))
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    return ident, rec


def _require_inviter_owned_bot_access(request: Request, bid: str) -> tuple[dict[str, Any], BotRecord]:
    ident, rec = _require_inviter_bot_access(request, bid)
    if _bot_suite(rec) != "inviter":
        raise HTTPException(status_code=404, detail="not found")
    return ident, rec


def _login_redirect(request: Request, kind: str) -> str:
    if _is_inviter_host(request):
        return "/inviter"
    return "/admin" if kind == "admin" else "/app"


def create_app() -> FastAPI:
    app = FastAPI(title="exsender", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    @app.middleware("http")
    async def inviter_subdomain_redirect(request: Request, call_next):
        if not _is_inviter_host(request):
            return await call_next(request)
        path = request.url.path
        if path == "/":
            return RedirectResponse("/inviter", status_code=302)
        static_ok = path.startswith(("/api/", "/css/", "/js/", "/img/", "/og/"))
        inviter_ok = path in ("/inviter", "/login")
        if static_ok or inviter_ok:
            return await call_next(request)
        if path in ("/app", "/admin", "/profile", "/register", "/changelog"):
            return RedirectResponse(f"{SITE_PUBLIC_BASE}{path}", status_code=302)
        return await call_next(request)

    app.add_middleware(security.SecurityMiddleware)

    @app.on_event("startup")
    async def _security_startup() -> None:
        security.startup_security_check()

    @app.on_event("startup")
    async def _start_verify_bot() -> None:
        start_verify_bot_background(pending_regs, users.telegram_taken)

    @app.on_event("startup")
    async def _warm_overview_cache() -> None:
        async def _run() -> None:
            bots = [
                b for b in registry.list()
                if b.api_token and b.status not in ("new",)
            ]
            if bots:
                await refresh_overviews_live(bots, force=False)

        asyncio.create_task(_run())

    def _admin_guard(request: Request) -> str:
        return require_admin(request, users)

    # ===================================================== auth pages
    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        ident = current_identity(request, users)
        if ident["kind"] == "admin":
            return RedirectResponse(_login_redirect(request, "admin"), status_code=303)
        if ident["kind"] == "user":
            return RedirectResponse(_login_redirect(request, "user"), status_code=303)
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

    @app.get("/changelog", include_in_schema=False)
    async def changelog_page(request: Request):
        return FileResponse(FRONTEND_DIR / "changelog.html")

    @app.get("/api/changelog", include_in_schema=False)
    async def api_changelog():
        items = [e.public() for e in changelog_store.list_public()]
        return {"items": items}

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
            return {"ok": True, "kind": "admin", "redirect": _login_redirect(request, "admin")}

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
        return {"ok": True, "kind": "user", "redirect": _login_redirect(request, "user")}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        clear_cookie(response)
        security.issue_csrf_token(response, request)
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request, response: Response):
        csrf = security.issue_csrf_token(response, request)
        ident = current_identity(request, users)
        if ident["kind"] == "admin":
            return {"user": ident["user"], "kind": "admin", "csrf": csrf}
        if ident["kind"] == "user":
            rec = ident["record"]
            return {"user": rec.email, "kind": "user", "profile": rec.public(), "csrf": csrf}
        return {"user": None, "csrf": csrf}

    def _resolve_referrer_id(body: dict[str, Any]) -> str:
        ref_code = str(body.get("ref", body.get("referral", ""))).strip().upper()[:32]
        if not ref_code:
            return ""
        referrer = users.by_referral_code(ref_code)
        return referrer.id if referrer is not None else ""

    @app.post("/api/auth/register")
    async def register_legacy():
        raise HTTPException(
            status_code=400,
            detail="Подтвердите аккаунт через Telegram. Обновите страницу регистрации.",
        )

    @app.post("/api/auth/register/start")
    async def register_start(payload: dict[str, Any], request: Request):
        if not VERIFY_BOT_TOKEN:
            raise HTTPException(
                status_code=503,
                detail="Подтверждение через Telegram временно недоступно",
            )
        body = payload or {}
        ip = security.client_ip(request)
        if security.honeypot_triggered(body):
            security.record_auth_failure(ip)
            raise HTTPException(status_code=400, detail="registration failed")

        email = security.validate_email(str(body.get("email", body.get("contact", ""))))
        password = security.validate_password(str(body.get("password", "")))
        if users.by_email(email):
            raise HTTPException(
                status_code=409,
                detail="Аккаунт с такой почтой уже зарегистрирован",
            )
        name = str(body.get("name", "")).strip()[:80]
        referred_by = _resolve_referrer_id(body)
        pending = pending_regs.create(
            email=email,
            password_hash=hash_password(password),
            name=name,
            referred_by=referred_by,
            ip=ip,
        )
        bot_username = ""
        async with httpx.AsyncClient() as client:
            bot_username = await resolve_bot_username(client)
        bot_url = bot_deep_link(pending.token, bot_username)
        if not bot_url:
            raise HTTPException(
                status_code=503,
                detail="Бот подтверждения не настроен (VERIFY_BOT_USERNAME)",
            )
        return {
            "ok": True,
            "token": pending.token,
            "botUrl": bot_url,
            "expiresAt": pending.expires_at,
        }

    @app.get("/api/auth/register/status")
    async def register_status(token: str = ""):
        rec = pending_regs.get(token)
        if rec is None:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        return {"ok": True, **rec.public_status()}

    @app.post("/api/auth/register/complete")
    async def register_complete(
        payload: dict[str, Any], request: Request, response: Response
    ):
        body = payload or {}
        token = str(body.get("token", "")).strip()
        rec = pending_regs.get(token)
        if rec is None:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        if rec.completed:
            raise HTTPException(status_code=409, detail="Регистрация уже завершена")
        if rec.is_expired():
            raise HTTPException(
                status_code=410,
                detail="Время подтверждения истекло. Начните регистрацию заново.",
            )
        if not rec.is_verified():
            raise HTTPException(
                status_code=400,
                detail="Сначала подтвердите аккаунт в Telegram",
            )
        if users.by_email(rec.email):
            pending_regs.mark_completed(token)
            raise HTTPException(
                status_code=409,
                detail="Аккаунт с такой почтой уже зарегистрирован",
            )
        if users.telegram_taken(rec.telegram_user_id):
            raise HTTPException(
                status_code=409,
                detail="Этот Telegram уже привязан к другому аккаунту",
            )
        try:
            user_rec = users.create_with_hash(
                rec.email,
                rec.password_hash,
                rec.name,
                referred_by=rec.referred_by,
                telegram_user_id=rec.telegram_user_id,
                telegram_username=rec.telegram_username,
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        pending_regs.mark_completed(token)
        issue_user_cookie(response, user_rec.id, request)
        security.issue_csrf_token(response, request)
        return {"ok": True, "redirect": "/app", "user": user_rec.public()}

    # ===================================================== profile / billing
    @app.get("/api/users/me", dependencies=[Depends(require_login)])
    async def get_me(request: Request):
        ident = current_identity(request, users)
        plans_list = [
            {"id": pid, **p, "priceUsd": p["price_usd"]}
            for pid, p in PLANS.items()
            if pid != "trial"
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
                    "referralCreditUsd": i.referral_credit_usd,
                }
                for i in invoices.user_history(rec.id)
            ]
            history.sort(key=lambda x: x["createdAt"], reverse=True)
            paid_ids = referrals.paid_user_ids_from_invoices(invoices)
            ref_stats = referrals.referral_stats(
                users, rec.id, paid_user_ids=paid_ids
            )
            ref_history = [
                e.public() for e in referral_ledger.for_referrer(rec.id)
            ]
            usage = await trial_limits.trial_usage(rec, registry)
            profile = rec.public()
            if usage:
                profile = {**profile, "trialLimits": usage}
            return {
                "kind": "user",
                "profile": profile,
                "plans": plans_list,
                "history": history[:20],
                "cryptoBotConfigured": payments.crypto_bot_configured(),
                "notifications": notifications.for_user(
                    rec.id, user_created_at=rec.created_at
                ),
                "referralLink": f"/register?ref={rec.referral_code}",
                "referral": {
                    "commissionPct": referrals.REFERRAL_COMMISSION_PCT,
                    "bonusDaysFirstPay": referrals.REFERRAL_BONUS_DAYS,
                    "invited": ref_stats["invited"],
                    "paid": ref_stats["paid"],
                    "history": ref_history,
                },
                "trialDays": TRIAL_DAYS,
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
            "recentUsers": [],
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

    async def _enrich_admin_users(user_records: list) -> list[dict[str, Any]]:
        async def _one(rec) -> dict[str, Any]:
            view = rec.admin_view()
            usage = await trial_limits.owner_usage_stats(registry, rec.id)
            view["botsUsed"] = usage["botsUsed"]
            view["accountsUsed"] = usage["accountsUsed"]
            return view

        if not user_records:
            return []
        return list(await asyncio.gather(*[_one(u) for u in user_records]))

    @app.get("/api/admin/stats")
    async def admin_stats(request: Request):
        _admin_guard(request)
        payload = _admin_stats_payload()
        recent_users = sorted(users.list_users(), key=lambda u: u.created_at, reverse=True)[:25]
        payload["recentUsers"] = await _enrich_admin_users(recent_users)
        return payload

    @app.get("/api/admin/users")
    async def admin_list_users(request: Request):
        _admin_guard(request)
        all_users = sorted(users.list_users(), key=lambda u: u.created_at, reverse=True)
        return {"users": await _enrich_admin_users(all_users)}

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

    @app.delete("/api/admin/users/{user_id}")
    async def admin_delete_user(user_id: str, request: Request):
        user_id = security.validate_safe_id(user_id, name="user_id")
        admin_login = _admin_guard(request)
        rec = users.get(user_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="user not found")
        email = rec.email
        bots_removed = registry.remove_for_owner(user_id)
        notifications.remove_for_user(user_id)
        users.delete(user_id)
        audit.add(
            admin_login,
            "delete_user",
            target=email,
            details={"botsRemoved": len(bots_removed)},
        )
        return {"ok": True, "email": email, "botsRemoved": bots_removed}

    @app.post("/api/admin/users/{user_id}/referral-balance")
    async def admin_grant_referral_balance(
        user_id: str, payload: dict[str, Any], request: Request
    ):
        user_id = security.validate_safe_id(user_id, name="user_id")
        admin_login = _admin_guard(request)
        body = payload or {}
        try:
            amount = float(body.get("amountUsd", body.get("amount_usd", 0)) or 0)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="Некорректная сумма") from e
        note = str(body.get("note", "")).strip()[:200]
        rec = users.get(user_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            referrals.admin_grant_referral_credit(
                user_id,
                amount,
                users,
                referral_ledger,
                admin_login=admin_login,
                note=note,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        rec = users.get(user_id)
        audit.add(
            admin_login,
            "grant_referral_balance",
            target=rec.email,
            details={
                "amountUsd": round(amount, 2),
                "note": note,
                "balanceUsd": round(float(rec.referral_balance_usd or 0), 2),
            },
        )
        return {"ok": True, "profile": rec.admin_view()}

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
        broadcast_user_ids: list[str] | None = None
        if raw_ids and raw_ids != "all":
            user_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
        else:
            broadcast_user_ids = [u.id for u in users.list_users()]
        try:
            count = notifications.send(
                message=message,
                title=title,
                user_ids=user_ids,
                broadcast_user_ids=broadcast_user_ids,
                admin=admin_login,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        audit.add(
            admin_login,
            "notify",
            target="all" if broadcast_user_ids is not None else f"{len(user_ids or [])} users",
            details={"title": title, "message": message[:200]},
        )
        return {"ok": True, "sent": count}

    @app.delete("/api/admin/notify")
    async def admin_notify_clear(request: Request):
        admin_login = _admin_guard(request)
        removed = notifications.clear_all()
        audit.add(admin_login, "notify_clear", target="all", details={"removed": removed})
        return {"ok": True, "removed": removed}

    @app.get("/api/admin/changelog")
    async def admin_changelog_list(request: Request):
        _admin_guard(request)
        return {
            "items": [e.public() for e in changelog_store.list_all()],
        }

    @app.post("/api/admin/changelog")
    async def admin_changelog_create(payload: dict[str, Any], request: Request):
        admin_login = _admin_guard(request)
        body = payload or {}
        tags_raw = body.get("tags", [])
        tags: list[str] = []
        if isinstance(tags_raw, str):
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        try:
            entry = changelog_store.create(
                version=str(body.get("version", "")).strip(),
                title=str(body.get("title", "")).strip(),
                date=str(body.get("date", "")).strip(),
                body=str(body.get("body", "")).strip(),
                tags=tags,
                published=bool(body.get("published", True)),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        audit.add(
            admin_login,
            "changelog_create",
            target=entry.version,
            details={"title": entry.title},
        )
        return {"ok": True, "entry": entry.public()}

    @app.get("/api/users/notifications", dependencies=[Depends(require_login)])
    async def user_notifications(request: Request):
        uid = current_user_id(request)
        if not uid:
            return {"notifications": []}
        rec = users.get(uid)
        created_at = float(rec.created_at) if rec else 0.0
        return {
            "notifications": notifications.for_user(
                uid, user_created_at=created_at
            )
        }

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
        if not info or plan_id == "trial":
            raise HTTPException(status_code=400, detail="Неизвестный тариф")
        base_usd = float(info["price_usd"])
        amount_usd = base_usd
        if promo_code:
            promo_rec, err = promos.validate(promo_code)
            if promo_rec is None:
                raise HTTPException(status_code=400, detail=err)
            amount_usd = admin_ops.apply_promo_price(base_usd, promo_rec)
        referral_credit = 0.0
        use_ref_balance = bool(
            (payload or {}).get("useReferralBalance", (payload or {}).get("use_referral_balance"))
        )
        if use_ref_balance and rec.referral_balance_usd > 0:
            referral_credit = round(
                min(float(rec.referral_balance_usd), amount_usd),
                2,
            )
            amount_usd = round(amount_usd - referral_credit, 2)

        if amount_usd <= 0 and referral_credit > 0:
            inv_id = f"refbal-{secrets.token_hex(8)}"
            inv = payments.InvoiceRecord(
                invoice_id=inv_id,
                user_id=rec.id,
                plan=plan_id,
                amount_usd=0.0,
                base_amount_usd=base_usd,
                promo_code=promo_code,
                referral_credit_usd=referral_credit,
            )
            invoices.add(inv)
            _activate_paid_invoice(inv)
            rec = users.get(uid)
            return {
                "ok": True,
                "paid": True,
                "paidByReferral": True,
                "invoiceId": inv_id,
                "payUrl": None,
                "amountUsd": 0.0,
                "baseUsd": base_usd,
                "promo": promo_code or None,
                "plan": plan_id,
                "referralCreditUsd": referral_credit,
                "referralBalanceUsd": round(float(rec.referral_balance_usd or 0), 2) if rec else 0.0,
                "profile": rec.public() if rec else None,
            }

        if not payments.crypto_bot_configured():
            raise HTTPException(
                status_code=503,
                detail="Платежи временно недоступны (CRYPTO_BOT_TOKEN не задан)",
            )
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
            referral_credit_usd=referral_credit,
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
            "referralCreditUsd": referral_credit,
            "referralBalanceUsd": round(float(rec.referral_balance_usd or 0), 2),
        }

    @app.get("/api/payments/check/{invoice_id}", dependencies=[Depends(require_login)])
    async def check_invoice(invoice_id: str, request: Request):
        invoice_id = security.validate_safe_id(invoice_id, name="invoice_id")
        uid = current_user_id(request)
        local = invoices.get(invoice_id)
        if local is None or (uid and local.user_id != uid):
            raise HTTPException(status_code=404, detail="invoice not found")
        if local.status != "paid":
            if not str(invoice_id).startswith("refbal-"):
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
        if inv.referral_credit_usd > 0:
            try:
                users.spend_referral_credit(inv.user_id, inv.referral_credit_usd)
            except ValueError:
                logger.warning(
                    "referral credit spend failed user=%s inv=%s",
                    inv.user_id,
                    inv.invoice_id,
                )
        referrals.apply_referral_rewards(
            inv,
            users,
            referral_ledger,
            is_first_payment=(paid_before == 0),
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
        bots = _bots_sender(ident)
        items = [b.public() for b in bots]
        stale: list[BotRecord] = []
        for it, b in zip(items, bots):
            if apply_health_to_item(it, b):
                stale.append(b)
        if stale:
            asyncio.create_task(refresh_health_background(stale))
        return {"bots": items}

    @app.get("/api/dashboard", dependencies=[Depends(require_login)])
    async def dashboard(request: Request, bot: str = ""):
        """Instant dashboard from disk cache; live refresh runs in background."""
        ident = _panel_access(request)
        bots = _bots_sender(ident)
        items = [b.public() for b in bots]
        stale: list[BotRecord] = []
        for it, b in zip(items, bots):
            if apply_health_to_item(it, b):
                stale.append(b)
        if stale:
            asyncio.create_task(refresh_health_background(stale))

        active = [b for b in bots if b.api_token and b.status != "new"]
        if bot.strip():
            active = [b for b in active if b.id == bot.strip()]

        overviews: list[dict[str, Any]] = []
        pending: list[BotRecord] = []
        is_stale = False
        for b in active:
            snap = overview_cache.get(b.id)
            if snap is not None:
                age = overview_cache.age_sec(b.id)
                if age is None or age > 25:
                    is_stale = True
                overviews.append({
                    "botId": b.id,
                    "botLabel": b.alias or b.host,
                    "overview": snap,
                })
            else:
                pending.append(b)
                is_stale = True

        # Instant response from disk cache; refresh stale/missing in background.
        to_refresh: list[BotRecord] = []
        for b in active:
            snap = overview_cache.get(b.id)
            if snap is None:
                to_refresh.append(b)
            else:
                age = overview_cache.age_sec(b.id)
                if age is None or age > 25:
                    to_refresh.append(b)
        if to_refresh:
            asyncio.create_task(refresh_overviews_live(to_refresh, force=bool(pending)))

        return {"bots": items, "overviews": overviews, "stale": is_stale}

    @app.post("/api/bots", status_code=201)
    async def add_bot(payload: dict[str, Any], request: Request):
        ident = _panel_access(request)
        body = payload or {}
        host = str(body.get("host", "")).strip()
        if not host:
            raise HTTPException(status_code=400, detail="host обязателен")
        owner_id = "" if _panel_is_admin(ident) else _panel_user_id(ident)
        if owner_id:
            user_rec = users.get(owner_id)
            trial_limits.enforce_trial_bot_limit(user_rec, registry)
        suite_raw = str(body.get("suite", "sender")).strip().lower() or "sender"
        if suite_raw == "inviter":
            raise HTTPException(status_code=400, detail="используй /api/inviter/bots для inviter VDS")
        rec = registry.add(
            host=host,
            ssh_port=int(body.get("sshPort", body.get("ssh_port", 22))),
            ssh_user=str(body.get("sshUser", body.get("ssh_user", "root"))),
            alias=str(body.get("alias", "")),
            install_dir=str(body.get("installDir", body.get("install_dir", "/opt/userbot"))),
            api_port=int(body.get("apiPort", body.get("api_port", 8080))),
            api_token=str(body.get("apiToken", body.get("api_token", ""))),
            restart_interval_hours=max(
                0,
                min(
                    int(
                        body["restartIntervalHours"]
                        if "restartIntervalHours" in body
                        else body.get("restart_interval_hours", 12)
                    ),
                    168,
                ),
            ),
            owner_id=owner_id,
            suite="sender",
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
            "restartIntervalHours": "restart_interval_hours",
        }
        patch = {allowed[k]: v for k, v in (payload or {}).items() if k in allowed}
        if "restart_interval_hours" in patch:
            try:
                h = int(patch["restart_interval_hours"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="restartIntervalHours должно быть числом")
            patch["restart_interval_hours"] = max(0, min(h, 168))
        prev_hours = rec.restart_interval_hours
        rec = registry.update(bid, **patch)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        if (
            "restart_interval_hours" in patch
            and patch["restart_interval_hours"] != prev_hours
            and rec.has_ssh_key
            and rec.status not in ("new",)
        ):
            _start_bg(deployer.sync_restart_timer_remote(bid, registry))
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
        profile = str(
            body.get("telegramClientProfile")
            or body.get("TELEGRAM_CLIENT_PROFILE")
            or "tdesktop"
        ).strip().lower()
        if profile not in ("tdesktop", "android", "custom"):
            profile = "tdesktop"
        env = {
            "TELEGRAM_CLIENT_PROFILE": profile,
            "TELEGRAM_DEVICE_PROFILE": str(
                body.get("telegramDeviceProfile")
                or body.get("TELEGRAM_DEVICE_PROFILE")
                or "tdesktop"
            ).strip().lower(),
            "API_ID": str(body.get("apiId", body.get("API_ID", ""))).strip(),
            "API_HASH": str(body.get("apiHash", body.get("API_HASH", ""))).strip(),
            "TG_BOT_TOKEN": str(body.get("tgBotToken", body.get("TG_BOT_TOKEN", ""))).strip(),
            "ADMIN_USER_IDS": str(body.get("adminUserIds", body.get("ADMIN_USER_IDS", ""))).strip(),
        }
        if rec.has_ssh_key and rec.status != "new":
            try:
                remote = await deployer.read_remote_env(rec, password=password)
            except Exception:
                remote = {}
            env = deployer._merge_remote_env(env, remote)
            if not env["API_ID"] or not env["API_HASH"]:
                env["API_ID"] = env["API_ID"] or remote.get("API_ID", "")
                env["API_HASH"] = env["API_HASH"] or remote.get("API_HASH", "")
        if env["API_ID"] and env["API_HASH"]:
            env["TELEGRAM_CLIENT_PROFILE"] = "custom"
            profile = "custom"
        elif profile == "custom" and (not env["API_ID"] or not env["API_HASH"]):
            raise HTTPException(
                status_code=400,
                detail="API_ID и API_HASH обязательны при профиле custom (my.telegram.org)",
            )
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
    PROXY_DIALOGS_TIMEOUT = httpx.Timeout(180.0, connect=15.0)
    PROXY_AUTH_TIMEOUT = httpx.Timeout(90.0, connect=15.0)
    PROXY_CHATS_TIMEOUT = httpx.Timeout(90.0, connect=15.0)

    def _proxy_timeout_for(sub: str) -> httpx.Timeout:
        norm = sub.strip("/").lower()
        if "/dialogs" in norm or norm.endswith("/dialogs") or "/channels" in norm:
            return PROXY_DIALOGS_TIMEOUT
        if "/auth/" in norm:
            return PROXY_AUTH_TIMEOUT
        if "/chats" in norm or "/spam" in norm:
            return PROXY_CHATS_TIMEOUT
        if "/resolve_post" in norm:
            return httpx.Timeout(55.0, connect=12.0)
        return PROXY_TIMEOUT

    async def _proxy(bid: str, method: str, sub: str, request: Request) -> Response:
        ident, rec = _require_bot(request, bid)
        bid = rec.id
        if ident.get("kind") == "user" and method.upper() == "POST":
            sub_norm = sub.strip("/")
            user_rec = ident["record"]
            if sub_norm == "accounts":
                await trial_limits.enforce_trial_account_limit(
                    user_rec, registry, bot=rec
                )
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
                timeout=_proxy_timeout_for(sub),
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
            data = await fetch_overview(rec)
        except Exception as e:
            return {"reachable": False, "error": str(e)}
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
        ident, rec = _require_bot(request, bid)
        bid = rec.id
        slot_id = security.validate_safe_id(slot_id, name="slot_id")
        if ident.get("kind") == "user":
            await trial_limits.enforce_trial_account_limit(
                ident["record"],
                registry,
                bot=rec,
                slot_id=slot_id,
            )
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

    @app.get("/inviter", include_in_schema=False)
    async def inviter_page(request: Request):
        if not _is_inviter_host(request):
            return RedirectResponse("https://inviter.exsender.top", status_code=302)
        ident = current_identity(request, users)
        if ident["kind"] == "anon":
            return RedirectResponse("/login?next=/inviter", status_code=303)
        if ident["kind"] == "user":
            rec = ident["record"]
            if rec.blocked:
                return RedirectResponse(f"{SITE_PUBLIC_BASE}/login", status_code=303)
            if rec.plan_expires_at <= time.time():
                return RedirectResponse(f"{SITE_PUBLIC_BASE}/profile", status_code=303)
        return FileResponse(FRONTEND_DIR / "inviter.html")

    INVITER_PROXY_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

    def _inviter_deploy_env(body: dict[str, Any]) -> dict[str, str]:
        profile = str(
            body.get("telegramClientProfile")
            or body.get("TELEGRAM_CLIENT_PROFILE")
            or "tdesktop"
        ).strip().lower()
        if profile not in ("tdesktop", "android", "custom"):
            profile = "tdesktop"
        env = {
            "TELEGRAM_CLIENT_PROFILE": profile,
            "TELEGRAM_DEVICE_PROFILE": str(
                body.get("telegramDeviceProfile")
                or body.get("TELEGRAM_DEVICE_PROFILE")
                or "tdesktop"
            ).strip().lower(),
            "API_ID": str(body.get("apiId", body.get("API_ID", ""))).strip(),
            "API_HASH": str(body.get("apiHash", body.get("API_HASH", ""))).strip(),
            "TG_BOT_TOKEN": str(body.get("tgBotToken", body.get("TG_BOT_TOKEN", ""))).strip(),
            "ADMIN_USER_IDS": str(body.get("adminUserIds", body.get("ADMIN_USER_IDS", ""))).strip(),
        }
        if env["API_ID"] and env["API_HASH"]:
            env["TELEGRAM_CLIENT_PROFILE"] = "custom"
        return env

    @app.get("/api/inviter/bots")
    async def inviter_list_bots(request: Request):
        ident = _inviter_access(request)
        bots = _bots_inviter(ident)
        items = [b.public() for b in bots]
        stale: list[BotRecord] = []
        for it, b in zip(items, bots):
            if apply_health_to_item(it, b):
                stale.append(b)
        if stale:
            asyncio.create_task(refresh_health_background(stale))
        return {"bots": items}

    @app.post("/api/inviter/bots", status_code=201)
    async def inviter_add_bot(payload: dict[str, Any], request: Request):
        ident = _inviter_access(request)
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
            restart_interval_hours=max(
                0,
                min(
                    int(
                        body["restartIntervalHours"]
                        if "restartIntervalHours" in body
                        else body.get("restart_interval_hours", 12)
                    ),
                    168,
                ),
            ),
            owner_id=owner_id,
            suite="inviter",
        )
        return rec.public()

    @app.patch("/api/inviter/bots/{bid}")
    async def inviter_patch_bot(bid: str, payload: dict[str, Any], request: Request):
        _, rec = _require_inviter_owned_bot_access(request, bid)
        allowed = {
            "alias": "alias",
            "host": "host",
            "sshPort": "ssh_port",
            "sshUser": "ssh_user",
            "installDir": "install_dir",
            "apiPort": "api_port",
            "apiToken": "api_token",
            "status": "status",
            "restartIntervalHours": "restart_interval_hours",
        }
        patch = {allowed[k]: v for k, v in (payload or {}).items() if k in allowed}
        if "restart_interval_hours" in patch:
            try:
                h = int(patch["restart_interval_hours"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="restartIntervalHours должно быть числом")
            patch["restart_interval_hours"] = max(0, min(h, 168))
        prev_hours = rec.restart_interval_hours
        rec = registry.update(rec.id, **patch)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        if (
            "restart_interval_hours" in patch
            and patch["restart_interval_hours"] != prev_hours
            and rec.has_ssh_key
            and rec.status not in ("new",)
        ):
            _start_bg(deployer.sync_restart_timer_remote(rec.id, registry))
        return rec.public()

    @app.delete("/api/inviter/bots/{bid}")
    async def inviter_delete_bot(bid: str, request: Request):
        _, rec = _require_inviter_owned_bot_access(request, bid)
        if not registry.remove(rec.id):
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    @app.post("/api/inviter/bots/{bid}/deploy")
    async def inviter_deploy_bot(bid: str, payload: dict[str, Any], request: Request):
        _, rec = _require_inviter_owned_bot_access(request, bid)
        body = payload or {}
        password = body.get("sshPassword") or body.get("password") or None
        env = _inviter_deploy_env(body)
        if rec.has_ssh_key and rec.status != "new":
            try:
                remote = await deployer.read_remote_env(rec, password=password)
            except Exception:
                remote = {}
            env = deployer._merge_remote_env(env, remote)
            if not env["API_ID"] or not env["API_HASH"]:
                env["API_ID"] = env["API_ID"] or remote.get("API_ID", "")
                env["API_HASH"] = env["API_HASH"] or remote.get("API_HASH", "")
        if env["TELEGRAM_CLIENT_PROFILE"] == "custom" and (not env["API_ID"] or not env["API_HASH"]):
            raise HTTPException(
                status_code=400,
                detail="API_ID и API_HASH обязательны при профиле custom",
            )
        _start_bg(deployer.deploy(rec.id, registry, password=password, env=env))
        return {"ok": True, "operation": "deploy"}

    @app.post("/api/inviter/bots/{bid}/restart")
    async def inviter_restart_bot(bid: str, payload: dict[str, Any] | None, request: Request):
        _, rec = _require_inviter_owned_bot_access(request, bid)
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.restart_remote(rec.id, registry, password=password))
        return {"ok": True, "operation": "restart"}

    @app.post("/api/inviter/bots/{bid}/stop")
    async def inviter_stop_bot(bid: str, payload: dict[str, Any] | None, request: Request):
        _, rec = _require_inviter_owned_bot_access(request, bid)
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.stop_remote(rec.id, registry, password=password))
        return {"ok": True, "operation": "stop"}

    @app.post("/api/inviter/bots/{bid}/uninstall")
    async def inviter_uninstall_bot(bid: str, payload: dict[str, Any] | None, request: Request):
        _, rec = _require_inviter_owned_bot_access(request, bid)
        password = (payload or {}).get("sshPassword") or (payload or {}).get("password")
        _start_bg(deployer.uninstall_remote(rec.id, registry, password=password))
        return {"ok": True, "operation": "uninstall"}

    @app.get("/api/inviter/bots/{bid}/deploy/log")
    async def inviter_deploy_log(bid: str, request: Request):
        _, rec = _require_inviter_owned_bot_access(request, bid)
        return deployer.get_state(rec.id).snapshot()

    async def _proxy_inviter_bot(
        rec: BotRecord, method: str, sub: str, request: Request, *, inviter_api: bool
    ) -> Response:
        if not rec.api_token:
            raise HTTPException(status_code=400, detail="у бота нет API токена")
        body = await request.body()
        ct = request.headers.get("content-type", "")
        extra: dict[str, str] = {}
        if ct:
            extra["Content-Type"] = ct
        path = sub.lstrip("/")
        if inviter_api:
            if not path.startswith("inviter/"):
                path = "inviter/" + path
            timeout = INVITER_PROXY_TIMEOUT
        else:
            timeout = _proxy_timeout_for(path)
        try:
            r = await bot_request(
                rec,
                method,
                path,
                content=body if body else None,
                extra_headers=extra or None,
                params=dict(request.query_params),
                timeout=timeout,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        resp_headers = {
            k: v for k, v in r.headers.items()
            if k.lower() not in ("content-encoding", "transfer-encoding", "connection")
        }
        return Response(content=r.content, status_code=r.status_code, headers=resp_headers)

    @app.api_route(
        "/api/inviter/bots/{bid}/proxy/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
    )
    async def inviter_panel_proxy(bid: str, path: str, request: Request):
        _, rec = _require_inviter_bot_access(request, bid)
        return await _proxy_inviter_bot(rec, request.method, path, request, inviter_api=False)

    @app.get("/api/inviter/bots/{bid}/accounts")
    async def inviter_bot_accounts(bid: str, request: Request):
        _, rec = _require_inviter_bot_access(request, bid)
        if not rec.api_token:
            raise HTTPException(status_code=400, detail="у бота нет API токена")
        try:
            status, data = await get_json(rec, "overview")
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        if status != 200 or not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="bot overview failed")
        return {"accounts": data.get("accounts") or []}

    @app.api_route(
        "/api/inviter/bots/{bid}/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
    )
    async def inviter_proxy(bid: str, path: str, request: Request):
        _, rec = _require_inviter_bot_access(request, bid)
        return await _proxy_inviter_bot(rec, request.method, path, request, inviter_api=True)

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
