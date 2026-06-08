"""Pending password resets confirmed via Telegram bot."""

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

RESET_FILE = data_file("password_reset_pending.json")
RESET_TTL_SEC = int(os.getenv("PASSWORD_RESET_TTL_SEC", "900") or 900)


@dataclass
class PendingPasswordReset:
    token: str
    user_id: str
    email: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    verified_at: float = 0.0
    completed: bool = False

    def is_expired(self) -> bool:
        return time.time() > float(self.expires_at or 0)

    def is_verified(self) -> bool:
        return self.verified_at > 0 and not self.completed

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
        }


class PasswordResetStore:
    def __init__(self, path: Path = RESET_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._by_token: dict[str, PendingPasswordReset] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logger.exception("PasswordResetStore: load failed")
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                rec = PendingPasswordReset(
                    token=str(row["token"]),
                    user_id=str(row["user_id"]),
                    email=str(row["email"]).strip().lower(),
                    created_at=float(row.get("created_at", 0) or time.time()),
                    expires_at=float(row.get("expires_at", 0) or 0),
                    verified_at=float(row.get("verified_at", 0) or 0),
                    completed=bool(row.get("completed", False)),
                )
            except (KeyError, ValueError, TypeError):
                continue
            if not rec.completed and not rec.is_expired():
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
            logger.exception("PasswordResetStore: save failed")
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

    def create(self, *, user_id: str, email: str) -> PendingPasswordReset:
        token = secrets.token_urlsafe(18)
        now = time.time()
        rec = PendingPasswordReset(
            token=token,
            user_id=user_id.strip(),
            email=email.strip().lower(),
            created_at=now,
            expires_at=now + float(RESET_TTL_SEC),
        )
        with self._lock:
            self._prune_locked()
            self._by_token[token] = rec
            self._save_locked()
        return rec

    def get(self, token: str) -> Optional[PendingPasswordReset]:
        tok = (token or "").strip()
        if not tok:
            return None
        with self._lock:
            return self._by_token.get(tok)

    def mark_verified(self, token: str, *, telegram_user_id: int) -> tuple[bool, str]:
        with self._lock:
            rec = self._by_token.get(token)
            if rec is None:
                return False, "Ссылка недействительна или устарела. Запросите сброс пароля заново."
            if rec.completed:
                return False, "Пароль уже изменён. Войдите на сайте."
            if rec.is_expired():
                return False, "Время подтверждения истекло. Запросите сброс пароля заново."
            if rec.is_verified():
                return True, "Telegram уже подтверждён. Вернитесь на сайт и задайте новый пароль."
            rec.verified_at = time.time()
            self._save_locked()
            return True, (
                "✅ Telegram подтверждён!\n\n"
                "Вернитесь на сайт и задайте новый пароль."
            )

    def verify_telegram_match(
        self, token: str, telegram_user_id: int, expected_user_id: str
    ) -> tuple[bool, str]:
        rec = self.get(token)
        if rec is None:
            return False, "Ссылка недействительна или устарела."
        if rec.user_id != expected_user_id:
            return False, "Ошибка заявки на сброс."
        return self.mark_verified(token, telegram_user_id=telegram_user_id)

    def mark_completed(self, token: str) -> Optional[PendingPasswordReset]:
        with self._lock:
            rec = self._by_token.get(token)
            if rec is None:
                return None
            rec.completed = True
            self._prune_locked()
            self._save_locked()
            return rec
