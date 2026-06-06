"""Simple file-backed user store with password hashing and subscriptions.

Designed for early-stage SaaS — no DB required, just users.json next to web/.
Concurrent writes are serialized via a lock; this is fine for <100 active users.

Subscription state lives on the user record:
    plan         : "" | "week" | "month" | "quarter"
    plan_expires_at: epoch seconds, 0 if no active plan
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from data_paths import data_file

WEB_DIR = Path(__file__).resolve().parent
USERS_FILE = data_file("users.json")

TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "1") or 1)

PBKDF2_ITERS = 200_000
PBKDF2_DKLEN = 32


def _hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERS, PBKDF2_DKLEN
    )
    return f"pbkdf2$sha256${PBKDF2_ITERS}${salt.hex()}${key.hex()}"


def hash_password(password: str) -> str:
    return _hash_password(password)


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, sub, iters_s, salt_hex, key_hex = stored.split("$")
        if algo != "pbkdf2" or sub != "sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iters_s), len(expected)
        )
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


@dataclass
class UserRecord:
    id: str
    email: str
    password_hash: str
    name: str = ""
    plan: str = ""
    plan_expires_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_login_at: float = 0.0
    last_invoice_id: str = ""
    blocked: bool = False
    referral_code: str = ""
    referred_by: str = ""
    trial_used: bool = False
    referral_balance_usd: float = 0.0
    referral_earned_total: float = 0.0
    telegram_user_id: int = 0
    telegram_username: str = ""

    def public(self) -> dict[str, Any]:
        now = time.time()
        active = self.plan_expires_at > now and not self.blocked
        remaining = max(0.0, self.plan_expires_at - now) if active else 0.0
        d: dict[str, Any] = {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "plan": self.plan,
            "planExpiresAt": self.plan_expires_at,
            "planActive": active,
            "isTrial": self.plan == "trial" and active,
            "planRemainingSec": int(remaining),
            "planDaysLeft": round(remaining / 86400, 2),
            "createdAt": self.created_at,
            "blocked": self.blocked,
            "referralCode": self.referral_code,
            "referralBalanceUsd": round(float(self.referral_balance_usd or 0), 2),
            "referralEarnedTotal": round(float(self.referral_earned_total or 0), 2),
            "telegramLinked": self.telegram_user_id > 0,
        }
        if self.plan == "trial" and active:
            from trial_limits import trial_limits_meta

            d["trialLimits"] = trial_limits_meta()
        return d

    def admin_view(self) -> dict[str, Any]:
        d = self.public()
        d["referredBy"] = self.referred_by
        d["lastLoginAt"] = self.last_login_at
        d["telegramUserId"] = self.telegram_user_id
        d["telegramUsername"] = self.telegram_username
        return d


def _gen_referral_code(existing: set[str]) -> str:
    for _ in range(20):
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
        if code and code not in existing:
            return code
    return secrets.token_hex(4).upper()


class UserStore:
    def __init__(self, path: Path = USERS_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._users: dict[str, UserRecord] = {}
        self._email_idx: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logger.exception("UserStore: failed to load %s", self.path)
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                rec = UserRecord(
                    id=str(row["id"]),
                    email=str(row["email"]).strip().lower(),
                    password_hash=str(row["password_hash"]),
                    name=str(row.get("name", "")),
                    plan=str(row.get("plan", "")),
                    plan_expires_at=float(row.get("plan_expires_at", 0) or 0),
                    created_at=float(row.get("created_at", 0) or time.time()),
                    last_login_at=float(row.get("last_login_at", 0) or 0),
                    last_invoice_id=str(row.get("last_invoice_id", "")),
                    blocked=bool(row.get("blocked", False)),
                    referral_code=str(row.get("referral_code", "")),
                    referred_by=str(row.get("referred_by", "")),
                    trial_used=bool(row.get("trial_used", False)),
                    referral_balance_usd=float(row.get("referral_balance_usd", 0) or 0),
                    referral_earned_total=float(row.get("referral_earned_total", 0) or 0),
                    telegram_user_id=int(row.get("telegram_user_id", 0) or 0),
                    telegram_username=str(row.get("telegram_username", "")),
                )
            except (KeyError, ValueError):
                continue
            self._users[rec.id] = rec
            self._email_idx[rec.email] = rec.id
        # Backfill referral codes for legacy users
        codes = {u.referral_code for u in self._users.values() if u.referral_code}
        changed = False
        for u in self._users.values():
            if not u.referral_code:
                u.referral_code = _gen_referral_code(codes)
                codes.add(u.referral_code)
                changed = True
        if changed:
            self._save_locked()

    def _save_locked(self) -> None:
        payload = [asdict(u) for u in self._users.values()]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("UserStore: save failed")
            try:
                tmp.unlink()
            except OSError:
                pass

    def get(self, user_id: str) -> Optional[UserRecord]:
        return self._users.get(user_id)

    def by_email(self, email: str) -> Optional[UserRecord]:
        uid = self._email_idx.get(email.strip().lower())
        return self._users.get(uid) if uid else None

    def by_telegram_id(self, telegram_user_id: int) -> Optional[UserRecord]:
        tg = int(telegram_user_id or 0)
        if tg <= 0:
            return None
        for u in self._users.values():
            if u.telegram_user_id == tg:
                return u
        return None

    def telegram_taken(self, telegram_user_id: int) -> bool:
        return self.by_telegram_id(telegram_user_id) is not None

    def create(
        self,
        email: str,
        password: str,
        name: str = "",
        *,
        referred_by: str = "",
    ) -> UserRecord:
        email_norm = email.strip().lower()
        with self._lock:
            if email_norm in self._email_idx:
                raise ValueError("Аккаунт с такой почтой уже зарегистрирован")
            uid = secrets.token_urlsafe(9)
            while uid in self._users:
                uid = secrets.token_urlsafe(9)
            codes = {u.referral_code for u in self._users.values() if u.referral_code}
            rec = UserRecord(
                id=uid,
                email=email_norm,
                password_hash=_hash_password(password),
                name=name.strip()[:80],
                referral_code=_gen_referral_code(codes),
                referred_by=referred_by.strip(),
            )
            if TRIAL_DAYS > 0:
                rec.plan = "trial"
                rec.plan_expires_at = time.time() + float(TRIAL_DAYS) * 86400.0
                rec.trial_used = True
            self._users[uid] = rec
            self._email_idx[email_norm] = uid
            self._save_locked()
            return rec

    def create_with_hash(
        self,
        email: str,
        password_hash: str,
        name: str = "",
        *,
        referred_by: str = "",
        telegram_user_id: int = 0,
        telegram_username: str = "",
    ) -> UserRecord:
        email_norm = email.strip().lower()
        tg = int(telegram_user_id or 0)
        with self._lock:
            if email_norm in self._email_idx:
                raise ValueError("Аккаунт с такой почтой уже зарегистрирован")
            if tg > 0:
                for u in self._users.values():
                    if u.telegram_user_id == tg:
                        raise ValueError("Этот Telegram уже привязан к другому аккаунту")
            uid = secrets.token_urlsafe(9)
            while uid in self._users:
                uid = secrets.token_urlsafe(9)
            codes = {u.referral_code for u in self._users.values() if u.referral_code}
            rec = UserRecord(
                id=uid,
                email=email_norm,
                password_hash=password_hash,
                name=name.strip()[:80],
                referral_code=_gen_referral_code(codes),
                referred_by=referred_by.strip(),
                telegram_user_id=tg,
                telegram_username=(telegram_username or "").strip().lstrip("@")[:64],
            )
            if TRIAL_DAYS > 0:
                rec.plan = "trial"
                rec.plan_expires_at = time.time() + float(TRIAL_DAYS) * 86400.0
                rec.trial_used = True
            self._users[uid] = rec
            self._email_idx[email_norm] = uid
            self._save_locked()
            return rec

    def verify(self, email: str, password: str) -> Optional[UserRecord]:
        rec = self.by_email(email)
        if rec is None:
            # Constant-time dummy verify to avoid leaking user existence via timing.
            _verify_password(password, _hash_password("dummy"))
            return None
        if rec.blocked:
            return None
        if not _verify_password(password, rec.password_hash):
            return None
        return rec

    def by_referral_code(self, code: str) -> Optional[UserRecord]:
        code_norm = str(code or "").strip().upper()
        if not code_norm:
            return None
        for u in self._users.values():
            if u.referral_code == code_norm:
                return u
        return None

    def set_blocked(self, user_id: str, blocked: bool) -> Optional[UserRecord]:
        with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return None
            rec.blocked = bool(blocked)
            self._save_locked()
            return rec

    def extend_plan_days(
        self,
        user_id: str,
        plan: str,
        days: int,
        *,
        invoice_id: str = "manual",
    ) -> Optional[UserRecord]:
        if days <= 0:
            raise ValueError("days must be positive")
        return self.set_plan(
            user_id,
            plan,
            duration_sec=int(days) * 86400,
            invoice_id=invoice_id,
        )

    def add_referral_credit(self, user_id: str, amount_usd: float) -> Optional[UserRecord]:
        amount = round(float(amount_usd), 2)
        if amount <= 0:
            return self.get(user_id)
        with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return None
            rec.referral_balance_usd = round(
                float(rec.referral_balance_usd or 0) + amount, 2
            )
            rec.referral_earned_total = round(
                float(rec.referral_earned_total or 0) + amount, 2
            )
            self._save_locked()
            return rec

    def spend_referral_credit(self, user_id: str, amount_usd: float) -> Optional[UserRecord]:
        amount = round(float(amount_usd), 2)
        if amount <= 0:
            return self.get(user_id)
        with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return None
            bal = float(rec.referral_balance_usd or 0)
            if amount > bal:
                raise ValueError("Недостаточно реферального баланса")
            rec.referral_balance_usd = round(bal - amount, 2)
            self._save_locked()
            return rec

    def touch_login(self, user_id: str) -> None:
        with self._lock:
            rec = self._users.get(user_id)
            if not rec:
                return
            rec.last_login_at = time.time()
            self._save_locked()

    def set_plan(
        self,
        user_id: str,
        plan: str,
        *,
        duration_sec: int,
        invoice_id: str = "",
    ) -> Optional[UserRecord]:
        """Add/extend subscription. Stacks on top of any remaining time."""
        with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return None
            now = time.time()
            base = max(rec.plan_expires_at, now)
            rec.plan = plan
            rec.plan_expires_at = base + float(duration_sec)
            if invoice_id:
                rec.last_invoice_id = invoice_id
            self._save_locked()
            return rec

    def list_users(self) -> list[UserRecord]:
        return list(self._users.values())

    def delete(self, user_id: str) -> Optional[UserRecord]:
        with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return None
            email = rec.email
            del self._users[user_id]
            self._email_idx.pop(email, None)
            for u in self._users.values():
                if u.referred_by == user_id:
                    u.referred_by = ""
            self._save_locked()
            return rec


# ============================================================================
# Subscription plans (USD)
# ============================================================================
PLANS: dict[str, dict[str, Any]] = {
    "trial": {
        "label": "Trial",
        "duration_days": TRIAL_DAYS,
        "price_usd": 0.0,
    },
    "week": {
        "label": "Week",
        "duration_days": 7,
        "price_usd": 4.0,
    },
    "month": {
        "label": "Month",
        "duration_days": 30,
        "price_usd": 12.0,
    },
    "quarter": {
        "label": "Quarter",
        "duration_days": 90,
        "price_usd": 30.0,
    },
}


def plan_info(plan_id: str) -> Optional[dict[str, Any]]:
    return PLANS.get(plan_id)


def plan_duration_seconds(plan_id: str) -> int:
    info = PLANS.get(plan_id)
    if not info:
        return 0
    return int(info["duration_days"]) * 24 * 3600
