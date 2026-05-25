"""Admin operations: audit log, promos, user notifications."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from data_paths import data_file

logger = logging.getLogger(__name__)

AUDIT_FILE = data_file("admin_audit.json")
PROMOS_FILE = data_file("promos.json")
NOTIFY_FILE = data_file("notifications.json")

REFERRAL_BONUS_DAYS = int(os.getenv("REFERRAL_BONUS_DAYS", "3") or 3)


def _save_json(path: Path, payload: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        logger.exception("save %s failed", path)
        try:
            tmp.unlink()
        except OSError:
            pass


@dataclass
class AuditEntry:
    id: str
    admin: str
    action: str
    target: str = ""
    details: str = ""
    created_at: float = field(default_factory=time.time)


class AuditStore:
    def __init__(self, path: Path = AUDIT_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: list[AuditEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                self._items.append(
                    AuditEntry(
                        id=str(row["id"]),
                        admin=str(row["admin"]),
                        action=str(row["action"]),
                        target=str(row.get("target", "")),
                        details=str(row.get("details", "")),
                        created_at=float(row.get("created_at", 0) or time.time()),
                    )
                )
            except (KeyError, ValueError):
                continue

    def add(self, admin: str, action: str, *, target: str = "", details: Any = None) -> AuditEntry:
        detail_str = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)
        entry = AuditEntry(
            id=secrets.token_urlsafe(8),
            admin=admin,
            action=action,
            target=target,
            details=detail_str[:2000],
        )
        with self._lock:
            self._items.append(entry)
            if len(self._items) > 500:
                self._items = self._items[-500:]
            _save_json(self.path, [asdict(x) for x in self._items])
        return entry

    def list_recent(self, limit: int = 50) -> list[AuditEntry]:
        return sorted(self._items, key=lambda x: x.created_at, reverse=True)[:limit]


@dataclass
class PromoRecord:
    code: str
    discount_pct: float = 0.0
    bonus_days: int = 0
    max_uses: int = 0
    uses: int = 0
    expires_at: float = 0.0
    active: bool = True
    owner_user_id: str = ""
    note: str = ""
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "discountPct": self.discount_pct,
            "bonusDays": self.bonus_days,
            "maxUses": self.max_uses,
            "uses": self.uses,
            "expiresAt": self.expires_at,
            "active": self.active,
            "ownerUserId": self.owner_user_id,
            "note": self.note,
            "createdAt": self.created_at,
        }


class PromoStore:
    def __init__(self, path: Path = PROMOS_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._promos: dict[str, PromoRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                rec = PromoRecord(
                    code=str(row["code"]).strip().upper(),
                    discount_pct=float(row.get("discount_pct", 0) or 0),
                    bonus_days=int(row.get("bonus_days", 0) or 0),
                    max_uses=int(row.get("max_uses", 0) or 0),
                    uses=int(row.get("uses", 0) or 0),
                    expires_at=float(row.get("expires_at", 0) or 0),
                    active=bool(row.get("active", True)),
                    owner_user_id=str(row.get("owner_user_id", "")),
                    note=str(row.get("note", ""))[:200],
                    created_at=float(row.get("created_at", 0) or time.time()),
                )
            except (KeyError, ValueError):
                continue
            self._promos[rec.code] = rec

    def _save_locked(self) -> None:
        _save_json(self.path, [asdict(p) for p in self._promos.values()])

    def list_all(self) -> list[PromoRecord]:
        return sorted(self._promos.values(), key=lambda p: p.created_at, reverse=True)

    def get(self, code: str) -> Optional[PromoRecord]:
        return self._promos.get(str(code or "").strip().upper())

    def create(
        self,
        code: str,
        *,
        discount_pct: float = 0,
        bonus_days: int = 0,
        max_uses: int = 0,
        expires_at: float = 0,
        owner_user_id: str = "",
        note: str = "",
    ) -> PromoRecord:
        code_norm = str(code or "").strip().upper()
        if not code_norm or len(code_norm) < 3:
            raise ValueError("Код минимум 3 символа")
        with self._lock:
            if code_norm in self._promos:
                raise ValueError("Такой промокод уже есть")
            rec = PromoRecord(
                code=code_norm,
                discount_pct=max(0.0, min(100.0, float(discount_pct))),
                bonus_days=max(0, int(bonus_days)),
                max_uses=max(0, int(max_uses)),
                expires_at=float(expires_at or 0),
                owner_user_id=owner_user_id,
                note=note[:200],
            )
            self._promos[code_norm] = rec
            self._save_locked()
            return rec

    def set_active(self, code: str, active: bool) -> Optional[PromoRecord]:
        with self._lock:
            rec = self._promos.get(str(code).strip().upper())
            if rec is None:
                return None
            rec.active = active
            self._save_locked()
            return rec

    def validate(self, code: str) -> tuple[Optional[PromoRecord], str]:
        rec = self.get(code)
        if rec is None:
            return None, "Промокод не найден"
        if not rec.active:
            return None, "Промокод отключён"
        if rec.expires_at and rec.expires_at < time.time():
            return None, "Срок промокода истёк"
        if rec.max_uses and rec.uses >= rec.max_uses:
            return None, "Лимит использований исчерпан"
        return rec, ""

    def apply_use(self, code: str) -> None:
        with self._lock:
            rec = self._promos.get(str(code).strip().upper())
            if rec is None:
                return
            rec.uses += 1
            self._save_locked()


@dataclass
class NotificationRecord:
    id: str
    user_id: str  # "*" = broadcast
    title: str
    message: str
    created_at: float = field(default_factory=time.time)
    created_by: str = ""
    read_by: list[str] = field(default_factory=list)


class NotificationStore:
    def __init__(self, path: Path = NOTIFY_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: list[NotificationRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                self._items.append(
                    NotificationRecord(
                        id=str(row["id"]),
                        user_id=str(row.get("user_id", "*")),
                        title=str(row.get("title", "Уведомление"))[:120],
                        message=str(row.get("message", ""))[:2000],
                        created_at=float(row.get("created_at", 0) or time.time()),
                        created_by=str(row.get("created_by", "")),
                        read_by=list(row.get("read_by") or []),
                    )
                )
            except (KeyError, ValueError):
                continue

    def _save_locked(self) -> None:
        payload = []
        for n in self._items:
            d = asdict(n)
            payload.append(d)
        _save_json(self.path, payload)

    def send(
        self,
        *,
        message: str,
        title: str = "exsender",
        user_ids: list[str] | None = None,
        admin: str = "",
    ) -> int:
        """Create notifications. user_ids=None or empty → broadcast to all."""
        msg = message.strip()
        if not msg:
            raise ValueError("Сообщение пустое")
        count = 0
        with self._lock:
            if not user_ids:
                self._items.append(
                    NotificationRecord(
                        id=secrets.token_urlsafe(8),
                        user_id="*",
                        title=title[:120],
                        message=msg[:2000],
                        created_by=admin,
                    )
                )
                count = 1
            else:
                for uid in user_ids:
                    uid = str(uid).strip()
                    if not uid:
                        continue
                    self._items.append(
                        NotificationRecord(
                            id=secrets.token_urlsafe(8),
                            user_id=uid,
                            title=title[:120],
                            message=msg[:2000],
                            created_by=admin,
                        )
                    )
                    count += 1
            if len(self._items) > 1000:
                self._items = self._items[-1000:]
            self._save_locked()
        return count

    def for_user(self, user_id: str, *, include_read: bool = False) -> list[dict[str, Any]]:
        now_items = []
        for n in self._items:
            if n.user_id != "*" and n.user_id != user_id:
                continue
            read = user_id in n.read_by
            if read and not include_read:
                continue
            now_items.append(
                {
                    "id": n.id,
                    "title": n.title,
                    "message": n.message,
                    "createdAt": n.created_at,
                    "read": read,
                    "broadcast": n.user_id == "*",
                }
            )
        return sorted(now_items, key=lambda x: x["createdAt"], reverse=True)[:30]

    def mark_read(self, user_id: str, notification_id: str) -> bool:
        with self._lock:
            for n in self._items:
                if n.id != notification_id:
                    continue
                if n.user_id not in ("*", user_id):
                    continue
                if user_id not in n.read_by:
                    n.read_by.append(user_id)
                    self._save_locked()
                return True
        return False


def revenue_by_day(invoices_paid: list[Any], days: int = 30) -> list[dict[str, Any]]:
    """Aggregate paid invoice amounts by UTC calendar day (includes today)."""
    days = max(1, min(int(days), 90))
    today = datetime.now(timezone.utc).date()
    start_day = today - timedelta(days=days - 1)
    buckets: dict[str, float] = {}
    counts: dict[str, int] = {}
    for i in range(days):
        key = (start_day + timedelta(days=i)).isoformat()
        buckets[key] = 0.0
        counts[key] = 0
    for inv in invoices_paid:
        ts = float(getattr(inv, "paid_at", 0) or getattr(inv, "created_at", 0) or 0)
        if ts <= 0:
            continue
        key = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        if key not in buckets:
            continue
        buckets[key] += float(getattr(inv, "amount_usd", 0) or 0)
        counts[key] += 1
    return [
        {"date": k, "amountUsd": round(buckets[k], 2), "count": counts[k]}
        for k in sorted(buckets.keys())
    ]


def apply_promo_price(base_usd: float, promo: PromoRecord) -> float:
    if promo.discount_pct > 0:
        return round(base_usd * (1 - promo.discount_pct / 100), 2)
    return round(base_usd, 2)
