"""Pending registrations until Telegram bot confirms the user."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from data_paths import data_file

logger = logging.getLogger(__name__)

PENDING_FILE = data_file("register_pending.json")
PENDING_TTL_SEC = int(os.getenv("REGISTER_VERIFY_TTL_SEC", "900") or 900)


@dataclass
class PendingRegistration:
    token: str
    email: str
    password_hash: str
    name: str = ""
    referred_by: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    ip: str = ""
    telegram_user_id: int = 0
    telegram_username: str = ""
    verified_at: float = 0.0
    completed: bool = False

    def is_expired(self) -> bool:
        return time.time() > float(self.expires_at or 0)

    def is_verified(self) -> bool:
        return self.telegram_user_id > 0 and self.verified_at > 0 and not self.completed

    def public_status(self) -> dict[str, Any]:
        if self.completed:
            status = "completed"
        elif self.is_expired():
            status = "expired"
        elif self.is_verified():
            status = "verified"
        else:
            status = "pending"
        return {
            "status": status,
            "verified": status == "verified",
            "expiresAt": self.expires_at,
            "email": self.email,
        }


class PendingRegistrationStore:
    def __init__(self, path: Path = PENDING_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._by_token: dict[str, PendingRegistration] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logger.exception("PendingRegistrationStore: load failed")
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                rec = PendingRegistration(
                    token=str(row["token"]),
                    email=str(row["email"]).strip().lower(),
                    password_hash=str(row["password_hash"]),
                    name=str(row.get("name", "")),
                    referred_by=str(row.get("referred_by", "")),
                    created_at=float(row.get("created_at", 0) or time.time()),
                    expires_at=float(row.get("expires_at", 0) or 0),
                    ip=str(row.get("ip", "")),
                    telegram_user_id=int(row.get("telegram_user_id", 0) or 0),
                    telegram_username=str(row.get("telegram_username", "")),
                    verified_at=float(row.get("verified_at", 0) or 0),
                    completed=bool(row.get("completed", False)),
                )
            except (KeyError, ValueError, TypeError):
                continue
            if not rec.completed and rec.is_expired():
                continue
            self._by_token[rec.token] = rec

    def _save_locked(self) -> None:
        now = time.time()
        payload = [
            asdict(rec)
            for rec in self._by_token.values()
            if not rec.completed and not rec.is_expired() and now - rec.created_at < 86400
        ]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("PendingRegistrationStore: save failed")
            try:
                tmp.unlink()
            except OSError:
                pass

    def _prune_locked(self) -> None:
        drop = [
            tok
            for tok, rec in self._by_token.items()
            if rec.completed or rec.is_expired()
        ]
        for tok in drop:
            self._by_token.pop(tok, None)

    def create(
        self,
        *,
        email: str,
        password_hash: str,
        name: str = "",
        referred_by: str = "",
        ip: str = "",
    ) -> PendingRegistration:
        email_norm = email.strip().lower()
        token = secrets.token_urlsafe(18)
        now = time.time()
        rec = PendingRegistration(
            token=token,
            email=email_norm,
            password_hash=password_hash,
            name=name.strip()[:80],
            referred_by=referred_by.strip(),
            created_at=now,
            expires_at=now + float(PENDING_TTL_SEC),
            ip=ip[:64],
        )
        with self._lock:
            self._prune_locked()
            self._by_token[token] = rec
            self._save_locked()
        return rec

    def get(self, token: str) -> Optional[PendingRegistration]:
        tok = (token or "").strip()
        if not tok:
            return None
        with self._lock:
            rec = self._by_token.get(tok)
            if rec is None:
                return None
            if rec.completed or rec.is_expired():
                return rec
            return rec

    def mark_verified(
        self,
        token: str,
        *,
        telegram_user_id: int,
        telegram_username: str = "",
    ) -> tuple[bool, str]:
        with self._lock:
            rec = self._by_token.get(token)
            if rec is None:
                return False, "Ссылка недействительна или устарела. Начните регистрацию на сайте заново."
            if rec.completed:
                return False, "Регистрация уже завершена. Войдите на сайте."
            if rec.is_expired():
                return False, "Время подтверждения истекло. Начните регистрацию на сайте заново."
            if rec.is_verified():
                if rec.telegram_user_id == telegram_user_id:
                    return True, "Telegram уже подтверждён. Вернитесь на сайт."
                return False, "Эта заявка уже подтверждена другим Telegram."
            rec.telegram_user_id = int(telegram_user_id)
            rec.telegram_username = (telegram_username or "").strip().lstrip("@")[:64]
            rec.verified_at = time.time()
            self._save_locked()
            return True, (
                "✅ Telegram подтверждён!\n\n"
                "Вернитесь на сайт — регистрация завершится автоматически."
            )

    def mark_completed(self, token: str) -> Optional[PendingRegistration]:
        with self._lock:
            rec = self._by_token.get(token)
            if rec is None:
                return None
            rec.completed = True
            self._prune_locked()
            self._save_locked()
            return rec

    def pending_for_telegram(self, telegram_user_id: int) -> Optional[PendingRegistration]:
        with self._lock:
            for rec in self._by_token.values():
                if rec.telegram_user_id == int(telegram_user_id) and rec.is_verified():
                    return rec
        return None
