"""Cookie-based session for the site.

Supports two identity types:
    * admin   — operators from env (SITE_LOGIN/PASSWORD + SITE_ADMINS)
    * user    — registered customer from users.json (UserStore)

Cookie payload:
    {"u": "<login>"}             — admin
    {"uid": "<user_id>"}         — customer
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Optional

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from users import UserRecord, UserStore

LOGIN = os.getenv("SITE_LOGIN", "favory")
PASSWORD = os.getenv("SITE_PASSWORD", "gubkina2868")
SECRET = os.getenv("SITE_SECRET", "change-me-please-this-is-not-secret")
COOKIE_NAME = "site_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _cookie_domain() -> Optional[str]:
    """Shared session across subdomains, e.g. .exsender.top for inviter."""
    dom = os.getenv("SITE_COOKIE_DOMAIN", "").strip()
    return dom or None


def _cookie_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {
        "httponly": True,
        "samesite": "lax",
        "secure": _cookie_secure(),
        "path": "/",
    }
    dom = _cookie_domain()
    if dom:
        kw["domain"] = dom
    return kw


def _cookie_secure() -> bool:
    explicit = os.getenv("SITE_COOKIE_SECURE", "").strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return True
    if explicit in ("0", "false", "no", "off"):
        return False
    pub = os.getenv("SITE_PUBLIC_URL", "").strip()
    return pub.startswith("https://")


_serializer = URLSafeSerializer(SECRET, salt="site-login")


def _admin_accounts() -> dict[str, str]:
    """login -> password for all configured admin operators."""
    admins: dict[str, str] = {}
    if LOGIN and PASSWORD:
        admins[LOGIN.strip()] = PASSWORD
    raw = os.getenv("SITE_ADMINS", "").strip()
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        user, _, pwd = part.partition(":")
        user, pwd = user.strip(), pwd.strip()
        if user and pwd:
            admins[user] = pwd
    return admins


def _secret_compare(a: str, b: str) -> bool:
    """Constant-time compare regardless of string length."""
    return hmac.compare_digest(
        hashlib.sha256(a.encode("utf-8")).digest(),
        hashlib.sha256(b.encode("utf-8")).digest(),
    )


def admin_logins() -> set[str]:
    return set(_admin_accounts().keys())


def verify_admin(login: str, password: str) -> Optional[str]:
    """Return admin login if credentials match any configured operator."""
    login_ = (login or "").strip()
    pwd = password or ""
    expected = _admin_accounts().get(login_)
    if expected is not None and _secret_compare(pwd, expected):
        return login_
    return None


def check_admin_credentials(login: str, password: str) -> bool:
    return verify_admin(login, password) is not None


def is_admin_login(login: str) -> bool:
    return login in _admin_accounts()


# Legacy alias used elsewhere in the codebase
def check_credentials(login: str, password: str) -> bool:
    return check_admin_credentials(login, password)


def issue_admin_cookie(response: Response, login: str, request: Any = None) -> None:
    token = _serializer.dumps({"u": login.strip(), "v": 1})
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, **_cookie_kwargs())


def issue_user_cookie(response: Response, user_id: str, request: Any = None) -> None:
    token = _serializer.dumps({"uid": user_id, "v": 1})
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, **_cookie_kwargs())


def issue_impersonation_cookie(
    response: Response, user_id: str, admin_login: str, request: Any = None
) -> None:
    token = _serializer.dumps(
        {"uid": user_id, "imp": admin_login.strip(), "v": 2}
    )
    response.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, **_cookie_kwargs())


# Backwards-compat name still imported by app.py.
def issue_cookie(response: Response) -> None:
    issue_admin_cookie(response, LOGIN)


def clear_cookie(response: Response) -> None:
    kw = _cookie_kwargs()
    response.delete_cookie(
        COOKIE_NAME,
        path=kw.get("path", "/"),
        secure=kw.get("secure", False),
        domain=kw.get("domain"),
    )


def _decode(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    try:
        return _serializer.loads(token)
    except BadSignature:
        return None


def current_user(request: Request) -> Optional[str]:
    """Return cookie 'u' (admin login) if present — kept for backwards compat."""
    data = _decode(request.cookies.get(COOKIE_NAME))
    if not data:
        return None
    return data.get("u")


def current_user_id(request: Request) -> Optional[str]:
    data = _decode(request.cookies.get(COOKIE_NAME))
    if not data:
        return None
    return data.get("uid")


def current_identity(request: Request, users: UserStore) -> dict[str, Any]:
    """Resolve cookie → identity dict.

    Returns:
        {"kind": "admin", "user": "<login>"}        for the admin operator
        {"kind": "user",  "record": UserRecord}     for a customer
        {"kind": "anon"}                             when not logged in
    """
    data = _decode(request.cookies.get(COOKIE_NAME))
    if not data:
        return {"kind": "anon"}
    admin_login = data.get("u")
    if admin_login and is_admin_login(str(admin_login)):
        return {"kind": "admin", "user": str(admin_login)}
    uid = data.get("uid")
    if uid:
        rec = users.get(uid)
        if rec is not None:
            result: dict[str, Any] = {"kind": "user", "record": rec}
            imp = data.get("imp")
            if imp and is_admin_login(str(imp)):
                result["impersonated_by"] = str(imp)
            return result
    return {"kind": "anon"}


def require_login(request: Request) -> str:
    """Require any logged-in identity (admin OR user)."""
    data = _decode(request.cookies.get(COOKIE_NAME))
    if not data:
        raise HTTPException(status_code=401, detail="not authenticated")
    if data.get("u") and is_admin_login(str(data["u"])):
        return str(data["u"])
    if data.get("uid"):
        return f"user:{data['uid']}"
    raise HTTPException(status_code=401, detail="not authenticated")


def require_admin(request: Request, users: UserStore) -> str:
    ident = current_identity(request, users)
    if ident["kind"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return ident["user"]


def require_subscription(request: Request, users: UserStore) -> dict[str, Any]:
    """Admin always allowed; users need active non-blocked subscription."""
    ident = current_identity(request, users)
    if ident["kind"] == "admin":
        return ident
    if ident["kind"] == "user":
        rec = ident["record"]
        if rec.blocked:
            raise HTTPException(status_code=403, detail="account blocked")
        if rec.plan_expires_at <= time.time():
            raise HTTPException(status_code=403, detail="subscription required")
        return ident
    raise HTTPException(status_code=401, detail="not authenticated")
