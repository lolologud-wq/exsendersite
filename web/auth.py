"""Cookie-based login for the site.

Single operator account, credentials live in env / defaults.
Signed cookie via itsdangerous; no DB, no user mgmt.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Cookie, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

LOGIN = os.getenv("SITE_LOGIN", "favory")
PASSWORD = os.getenv("SITE_PASSWORD", "gubkina2868")
SECRET = os.getenv("SITE_SECRET", "change-me-please-this-is-not-secret")
COOKIE_NAME = "site_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _cookie_secure() -> bool:
    explicit = os.getenv("SITE_COOKIE_SECURE", "").strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return True
    if explicit in ("0", "false", "no", "off"):
        return False
    pub = os.getenv("SITE_PUBLIC_URL", "").strip()
    return pub.startswith("https://")

_serializer = URLSafeSerializer(SECRET, salt="site-login")


def check_credentials(login: str, password: str) -> bool:
    return (login or "") == LOGIN and (password or "") == PASSWORD


def issue_cookie(response: Response) -> None:
    token = _serializer.dumps({"u": LOGIN})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, secure=_cookie_secure())


def _current_user_from_cookie(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        data = _serializer.loads(token)
    except BadSignature:
        return None
    return (data or {}).get("u")


def require_login(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    user = _current_user_from_cookie(token)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def current_user(request: Request) -> Optional[str]:
    return _current_user_from_cookie(request.cookies.get(COOKIE_NAME))
