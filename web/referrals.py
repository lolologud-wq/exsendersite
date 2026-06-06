"""Referral commissions and ledger."""

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

LEDGER_FILE = data_file("referral_ledger.json")

REFERRAL_COMMISSION_PCT = float(os.getenv("REFERRAL_COMMISSION_PCT", "15") or 15)
REFERRAL_BONUS_DAYS = int(os.getenv("REFERRAL_BONUS_DAYS", "3") or 3)


@dataclass
class ReferralLedgerEntry:
    id: str
    referrer_id: str
    buyer_id: str
    invoice_id: str
    payment_usd: float
    commission_usd: float
    kind: str = "commission"  # commission | bonus_days
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "paymentUsd": self.payment_usd,
            "commissionUsd": self.commission_usd,
            "kind": self.kind,
            "createdAt": self.created_at,
        }


class ReferralLedger:
    def __init__(self, path: Path = LEDGER_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: list[ReferralLedgerEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logger.exception("ReferralLedger load failed")
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                self._items.append(
                    ReferralLedgerEntry(
                        id=str(row["id"]),
                        referrer_id=str(row["referrer_id"]),
                        buyer_id=str(row["buyer_id"]),
                        invoice_id=str(row.get("invoice_id", "")),
                        payment_usd=float(row.get("payment_usd", 0) or 0),
                        commission_usd=float(row.get("commission_usd", 0) or 0),
                        kind=str(row.get("kind", "commission")),
                        created_at=float(row.get("created_at", 0) or time.time()),
                    )
                )
            except (KeyError, ValueError):
                continue

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = [asdict(x) for x in self._items]
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("ReferralLedger save failed")
            try:
                tmp.unlink()
            except OSError:
                pass

    def add(self, entry: ReferralLedgerEntry) -> None:
        with self._lock:
            self._items.append(entry)
            if len(self._items) > 5000:
                self._items = self._items[-5000:]
            self._save_locked()

    def for_referrer(self, referrer_id: str, *, limit: int = 30) -> list[ReferralLedgerEntry]:
        rows = [x for x in self._items if x.referrer_id == referrer_id]
        rows.sort(key=lambda x: x.created_at, reverse=True)
        return rows[:limit]


def referral_stats(users: Any, referrer_id: str, *, paid_user_ids: set[str]) -> dict[str, int]:
    invited = [u for u in users.list_users() if u.referred_by == referrer_id]
    paid = sum(1 for u in invited if u.id in paid_user_ids)
    return {"invited": len(invited), "paid": paid}


def paid_user_ids_from_invoices(invoice_store) -> set[str]:
    return {
        i.user_id
        for i in invoice_store.list_all()
        if i.status == "paid"
    }


def apply_referral_rewards(
    inv: Any,
    users: Any,
    ledger: ReferralLedger,
    *,
    is_first_payment: bool,
) -> None:
    """Credit referrer commission on each referred payment; bonus days on first."""
    buyer = users.get(inv.user_id)
    if buyer is None or not buyer.referred_by:
        return
    referrer = users.get(buyer.referred_by)
    if referrer is None:
        return

    pct = max(0.0, min(100.0, REFERRAL_COMMISSION_PCT))
    commission = round(float(inv.amount_usd) * pct / 100.0, 2)
    if commission > 0:
        users.add_referral_credit(referrer.id, commission)
        ledger.add(
            ReferralLedgerEntry(
                id=secrets.token_urlsafe(8),
                referrer_id=referrer.id,
                buyer_id=buyer.id,
                invoice_id=inv.invoice_id,
                payment_usd=float(inv.amount_usd),
                commission_usd=commission,
                kind="commission",
            )
        )

    if is_first_payment and REFERRAL_BONUS_DAYS > 0:
        users.extend_plan_days(
            referrer.id,
            referrer.plan or "week",
            REFERRAL_BONUS_DAYS,
            invoice_id=f"ref-bonus-{buyer.id}",
        )
        ledger.add(
            ReferralLedgerEntry(
                id=secrets.token_urlsafe(8),
                referrer_id=referrer.id,
                buyer_id=buyer.id,
                invoice_id=inv.invoice_id,
                payment_usd=float(inv.amount_usd),
                commission_usd=0.0,
                kind="bonus_days",
            )
        )


def admin_grant_referral_credit(
    user_id: str,
    amount_usd: float,
    users: Any,
    ledger: ReferralLedger,
    *,
    admin_login: str = "",
    note: str = "",
) -> Any:
    """Credit referral balance manually from admin panel."""
    amount = round(float(amount_usd), 2)
    if amount <= 0:
        raise ValueError("Сумма должна быть больше 0")
    if amount > 10_000:
        raise ValueError("Слишком большая сумма")
    rec = users.add_referral_credit(user_id, amount)
    if rec is None:
        raise ValueError("Пользователь не найден")
    inv_ref = f"admin-{admin_login or 'panel'}"[:48]
    if note:
        inv_ref = f"{inv_ref}:{note[:32]}"[:64]
    ledger.add(
        ReferralLedgerEntry(
            id=secrets.token_urlsafe(8),
            referrer_id=user_id,
            buyer_id="admin",
            invoice_id=inv_ref,
            payment_usd=0.0,
            commission_usd=amount,
            kind="admin_grant",
        )
    )
    return rec
