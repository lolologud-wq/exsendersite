"""Security layer: rate limits, IP bans, CSRF, headers, input validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from data_paths import data_file

logger = logging.getLogger(__name__)

STATE_FILE = data_file("security_state.json")

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

# --- tunables (env) ---
TRUST_PROXY = os.getenv("SITE_TRUST_PROXY", "1").strip().lower() in ("1", "true", "yes")
MAX_BODY_BYTES = int(os.getenv("SITE_MAX_BODY_BYTES", "1048576") or 1048576)  # 1 MiB
GENERAL_RPM = int(os.getenv("SITE_RATE_LIMIT_RPM", "120") or 120)
AUTH_RPM = int(os.getenv("SITE_AUTH_RATE_LIMIT_RPM", "15") or 15)
ADMIN_RPM = int(os.getenv("SITE_ADMIN_RATE_LIMIT_RPM", "60") or 60)
WEBHOOK_RPM = int(os.getenv("SITE_WEBHOOK_RATE_LIMIT_RPM", "120") or 120)
AUTH_MAX_FAILS = int(os.getenv("SITE_AUTH_MAX_FAILS", "8") or 8)
AUTH_BAN_MINUTES = int(os.getenv("SITE_AUTH_BAN_MINUTES", "30") or 30)
AUTH_FAIL_WINDOW = int(os.getenv("SITE_AUTH_FAIL_WINDOW_SEC", "900") or 900)
CSRF_ENABLED = os.getenv("SITE_CSRF", "1").strip().lower() not in ("0", "false", "no")
RATE_LIMIT_ENABLED = os.getenv("SITE_RATE_LIMIT", "0").strip().lower() not in ("0", "false", "no")
IP_BAN_ENABLED = os.getenv("SITE_IP_BAN", "0").strip().lower() not in ("0", "false", "no")
MIN_PASSWORD_LEN = int(os.getenv("SITE_MIN_PASSWORD_LEN", "8") or 8)
MAX_PASSWORD_LEN = int(os.getenv("SITE_MAX_PASSWORD_LEN", "128") or 128)

WEAK_SECRETS = {
    "",
    "change-me-please-this-is-not-secret",
    "please-generate-a-long-random-string-here",
}

_PROBE_RE = re.compile(
    r"(^/\.env)|(/wp-admin)|(/wp-login)|(/phpmyadmin)|(/\.git)|"
    r"(/admin\.php)|(/xmlrpc\.php)|(/vendor/phpunit)|(/actuator)|"
    r"(/\.aws/)|(/config\.json)|(/server-status)",
    re.I,
)

_HONEYPOT_KEYS = frozenset({"_website", "website", "url", "company", "fax"})

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


class _SecurityState:
    """In-memory limits + optional persisted IP bans."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._auth_fails: dict[str, deque[float]] = defaultdict(deque)
        self._bans: dict[str, float] = {}
        self._load_bans()

    def _load_bans(self) -> None:
        if not STATE_FILE.is_file():
            return
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                for ip, exp in data.get("bans", {}).items():
                    if float(exp) > now:
                        self._bans[str(ip)] = float(exp)
        except (OSError, ValueError, TypeError):
            logger.warning("security: could not load %s", STATE_FILE)

    def _persist_bans_locked(self) -> None:
        now = time.time()
        payload = {"bans": {ip: exp for ip, exp in self._bans.items() if exp > now}}
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, STATE_FILE)
        except OSError:
            logger.exception("security: save bans failed")

    def is_banned(self, ip: str) -> bool:
        with self._lock:
            exp = self._bans.get(ip)
            if not exp:
                return False
            if exp <= time.time():
                del self._bans[ip]
                return False
            return True

    def ban(self, ip: str, minutes: int) -> None:
        with self._lock:
            self._bans[ip] = time.time() + minutes * 60
            self._persist_bans_locked()
        logger.warning("security: banned IP %s for %s min", ip, minutes)

    def _rate_check(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.time()
        q = self._hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def check_rate(self, ip: str, category: str, limit: int) -> None:
        if not RATE_LIMIT_ENABLED:
            return
        key = f"{category}:{ip}"
        with self._lock:
            ok = self._rate_check(key, limit)
        if not ok:
            raise HTTPException(status_code=429, detail="too many requests")

    def record_auth_failure(self, ip: str) -> None:
        if not IP_BAN_ENABLED:
            return
        now = time.time()
        with self._lock:
            q = self._auth_fails[ip]
            while q and q[0] <= now - AUTH_FAIL_WINDOW:
                q.popleft()
            q.append(now)
            if len(q) >= AUTH_MAX_FAILS:
                self._bans[ip] = now + AUTH_BAN_MINUTES * 60
                self._auth_fails.pop(ip, None)
                self._persist_bans_locked()
                logger.warning("security: auth lockout IP %s", ip)

    def record_auth_success(self, ip: str) -> None:
        with self._lock:
            self._auth_fails.pop(ip, None)


_state = _SecurityState()


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()[:64] or "unknown"
        xri = request.headers.get("x-real-ip", "")
        if xri:
            return xri.strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


def is_probe_path(path: str) -> bool:
    return bool(_PROBE_RE.search(path))


def rate_category(path: str, method: str) -> tuple[str, int]:
    if path.startswith("/api/payments/webhook"):
        return "webhook", WEBHOOK_RPM
    if path.startswith("/api/admin") or path == "/admin":
        return "admin", ADMIN_RPM
    if path.startswith("/api/auth/"):
        return "auth", AUTH_RPM
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return "write", max(30, GENERAL_RPM // 2)
    return "general", GENERAL_RPM


def auth_delay(failures: int) -> float:
    """Progressive delay after failed logins (cap 3s)."""
    return min(3.0, 0.15 * max(0, failures))


def count_recent_auth_fails(ip: str) -> int:
    with _state._lock:
        q = _state._auth_fails.get(ip)
        if not q:
            return 0
        now = time.time()
        return sum(1 for t in q if t > now - AUTH_FAIL_WINDOW)


def record_auth_failure(ip: str, *, skip_ban: bool = False) -> None:
    if skip_ban:
        return
    _state.record_auth_failure(ip)


def record_auth_success(ip: str) -> None:
    _state.record_auth_success(ip)


def ban_ip(ip: str, minutes: int) -> None:
    _state.ban(ip, minutes)


def check_not_banned(request: Request) -> str:
    ip = client_ip(request)
    if IP_BAN_ENABLED and _state.is_banned(ip):
        raise HTTPException(status_code=403, detail="access denied")
    return ip


def check_rate_limit(request: Request) -> str:
    ip = check_not_banned(request)
    cat, limit = rate_category(request.url.path, request.method)
    _state.check_rate(ip, cat, limit)
    return ip


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()[:320]
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Неверный email")
    return email


def validate_password(password: str) -> str:
    pwd = password or ""
    if len(pwd) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Пароль минимум {MIN_PASSWORD_LEN} символов",
        )
    if len(pwd) > MAX_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail="Пароль слишком длинный")
    if not re.search(r"[A-Za-z]", pwd) or not re.search(r"\d", pwd):
        raise HTTPException(
            status_code=400,
            detail="Пароль: буквы и цифры",
        )
    return pwd


def validate_safe_id(value: str, *, name: str = "id") -> str:
    v = str(value or "").strip()
    if not _SAFE_ID_RE.match(v):
        raise HTTPException(status_code=400, detail=f"invalid {name}")
    return v


def honeypot_triggered(body: dict[str, Any]) -> bool:
    for key in _HONEYPOT_KEYS:
        val = body.get(key)
        if val is not None and str(val).strip():
            return True
    return False


def _csrf_cookie_secure(request: Request) -> bool:
    from auth import _cookie_secure

    if request.url.scheme == "https":
        return True
    return _cookie_secure()


def _csrf_cookie_domain() -> str:
    from auth import _cookie_domain

    return (_cookie_domain() or "").strip()


def _sign_csrf(raw: str) -> str:
    secret = os.getenv("SITE_SECRET", "change-me-please-this-is-not-secret")
    sig = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"{raw}.{sig}"


def _verify_signed_csrf(token: str) -> bool:
    token = (token or "").strip()
    if "." not in token:
        return False
    raw, sig = token.rsplit(".", 1)
    if not raw or not sig:
        return False
    secret = os.getenv("SITE_SECRET", "change-me-please-this-is-not-secret")
    expected = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


def issue_csrf_token(response: Response, request: Request) -> str:
    token = _sign_csrf(secrets.token_urlsafe(24))
    secure = _csrf_cookie_secure(request)
    csrf_kw: dict[str, Any] = {
        "max_age": 86400,
        "httponly": False,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
    }
    cookie_dom = _csrf_cookie_domain()
    response.delete_cookie(CSRF_COOKIE, path="/")
    if cookie_dom:
        response.delete_cookie(CSRF_COOKIE, path="/", domain=cookie_dom)
        csrf_kw["domain"] = cookie_dom
    response.set_cookie(CSRF_COOKIE, token, **csrf_kw)
    return token


def verify_csrf(request: Request) -> None:
    if not CSRF_ENABLED:
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    path = request.url.path
    if path.startswith("/api/payments/webhook"):
        return
    # Bootstrap auth — no session/CSRF yet; JSON body + SameSite cookies are enough here.
    if path in ("/api/auth/login", "/api/auth/register", "/api/auth/logout", "/api/auth/csrf"):
        return
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if header and cookie and hmac.compare_digest(header, cookie):
        return
    if header and _verify_signed_csrf(header):
        return
    raise HTTPException(status_code=403, detail="csrf validation failed")


def apply_security_headers(response: Response, request: Request) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-XSS-Protection"] = "0"
    if request.url.scheme == "https" or os.getenv("SITE_PUBLIC_URL", "").startswith("https://"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # API responses — minimal CSP; static HTML served separately
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"


def startup_security_check() -> None:
    secret = os.getenv("SITE_SECRET", "").strip()
    if secret in WEAK_SECRETS or len(secret) < 32:
        logger.critical(
            "SITE_SECRET слабый или не задан — смените на случайную строку ≥32 символов"
        )
    login = os.getenv("SITE_LOGIN", "")
    pwd = os.getenv("SITE_PASSWORD", "")
    if login == "favory" and pwd == "gubkina2868":
        logger.warning("security: используются дефолтные SITE_LOGIN/SITE_PASSWORD — смените!")


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if is_probe_path(path):
            logger.warning("security: probe %s from %s", path, client_ip(request))
            return Response(status_code=404, content="Not Found")

        ip = client_ip(request)
        if IP_BAN_ENABLED and _state.is_banned(ip):
            return Response(status_code=403, content="Forbidden")

        if RATE_LIMIT_ENABLED:
            try:
                cat, limit = rate_category(path, request.method)
                _state.check_rate(ip, cat, limit)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return Response(status_code=413, content="Payload Too Large")

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            try:
                verify_csrf(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        response = await call_next(request)
        apply_security_headers(response, request)
        return response
