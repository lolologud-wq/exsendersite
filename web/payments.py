"""Crypto Bot (Crypto Pay) API client + invoice tracking.

Docs: https://help.crypt.bot/crypto-pay-api

Configure via env:
    CRYPTO_BOT_TOKEN   — token from @CryptoBot (Crypto Pay API)
    CRYPTO_BOT_NETWORK — "mainnet" (default) or "testnet"
    CRYPTO_BOT_ASSET   — invoice asset, "USDT" by default
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional

import httpx

from data_paths import WEB_DIR, data_file

logger = logging.getLogger(__name__)

INVOICES_FILE = data_file("invoices.json")


def _api_base() -> str:
    network = os.getenv("CRYPTO_BOT_NETWORK", "mainnet").strip().lower()
    if network == "testnet":
        return "https://testnet-pay.crypt.bot/api"
    return "https://pay.crypt.bot/api"


def crypto_bot_configured() -> bool:
    return bool(os.getenv("CRYPTO_BOT_TOKEN", "").strip())


def _crypto_bot_token() -> str:
    tok = os.getenv("CRYPTO_BOT_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("CRYPTO_BOT_TOKEN не задан в .env сайта")
    return tok


def _asset() -> str:
    return (os.getenv("CRYPTO_BOT_ASSET", "USDT") or "USDT").strip().upper()


# ============================================================================
# Local invoice tracking (so webhook + polling fallback both work)
# ============================================================================
@dataclass
class InvoiceRecord:
    invoice_id: str
    user_id: str
    plan: str
    amount_usd: float
    status: str = "active"  # active | paid | expired | failed
    asset: str = ""
    pay_url: str = ""
    created_at: float = field(default_factory=time.time)
    paid_at: float = 0.0
    promo_code: str = ""
    base_amount_usd: float = 0.0
    referral_credit_usd: float = 0.0


def is_paid_with_real_money(inv: InvoiceRecord) -> bool:
    """Paid invoice with crypto/fiat charge (excludes 100% referral-balance checkout)."""
    return inv.status == "paid" and float(inv.amount_usd or 0) > 0


class InvoiceStore:
    def __init__(self, path: Path = INVOICES_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._invoices: dict[str, InvoiceRecord] = {}
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
            try:
                rec = InvoiceRecord(
                    invoice_id=str(row["invoice_id"]),
                    user_id=str(row["user_id"]),
                    plan=str(row.get("plan", "")),
                    amount_usd=float(row.get("amount_usd", 0) or 0),
                    status=str(row.get("status", "active")),
                    asset=str(row.get("asset", "")),
                    pay_url=str(row.get("pay_url", "")),
                    created_at=float(row.get("created_at", 0) or time.time()),
                    paid_at=float(row.get("paid_at", 0) or 0),
                    promo_code=str(row.get("promo_code", "")),
                    base_amount_usd=float(row.get("base_amount_usd", 0) or 0),
                    referral_credit_usd=float(row.get("referral_credit_usd", 0) or 0),
                )
            except (KeyError, ValueError):
                continue
            self._invoices[rec.invoice_id] = rec

    def _save_locked(self) -> None:
        payload = [asdict(i) for i in self._invoices.values()]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("InvoiceStore save")
            try:
                tmp.unlink()
            except OSError:
                pass

    def add(self, rec: InvoiceRecord) -> None:
        with self._lock:
            self._invoices[rec.invoice_id] = rec
            self._save_locked()

    def get(self, invoice_id: str) -> Optional[InvoiceRecord]:
        return self._invoices.get(str(invoice_id))

    def mark_paid(self, invoice_id: str) -> Optional[InvoiceRecord]:
        with self._lock:
            rec = self._invoices.get(str(invoice_id))
            if rec is None or rec.status == "paid":
                return rec
            rec.status = "paid"
            rec.paid_at = time.time()
            self._save_locked()
            return rec

    def user_history(self, user_id: str) -> list[InvoiceRecord]:
        return [
            i for i in self._invoices.values() if i.user_id == user_id
        ]

    def paid_for_user(self, user_id: str) -> list[InvoiceRecord]:
        return [
            i for i in self._invoices.values()
            if i.user_id == user_id and i.status == "paid"
        ]

    def list_all(self) -> list[InvoiceRecord]:
        return list(self._invoices.values())


# ============================================================================
# API client
# ============================================================================
class CryptoBotError(RuntimeError):
    pass


async def _request(method: str, params: Optional[dict[str, Any]] = None) -> Any:
    headers = {"Crypto-Pay-API-Token": _crypto_bot_token()}
    url = f"{_api_base()}/{method}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=params or {}, headers=headers)
    try:
        body = r.json()
    except ValueError:
        raise CryptoBotError(f"Crypto Bot вернул не-JSON ({r.status_code})")
    if not body.get("ok"):
        err = body.get("error") or {}
        name = err.get("name") or "unknown"
        code = err.get("code") or r.status_code
        raise CryptoBotError(f"Crypto Bot: {name} ({code})")
    return body.get("result")


async def create_invoice(
    *,
    user_id: str,
    plan: str,
    amount_usd: float,
    description: str = "",
    payload: str = "",
) -> dict[str, Any]:
    """Create an invoice on Crypto Bot and return raw API result."""
    params: dict[str, Any] = {
        "currency_type": "fiat",
        "fiat": "USD",
        "accepted_assets": _asset(),
        "amount": f"{float(amount_usd):.2f}",
        "description": description or f"exsender · {plan}",
        "hidden_message": "Доступ активирован. Открой /app",
        "expires_in": 1800,  # 30 min
    }
    if payload:
        params["payload"] = payload
    try:
        return await _request("createInvoice", params)
    except CryptoBotError:
        # Crypto Bot accounts that don't support fiat invoices fall back to
        # crypto invoice in selected asset (price computed by Crypto Bot's rate).
        params.pop("currency_type", None)
        params.pop("fiat", None)
        params.pop("accepted_assets", None)
        params["asset"] = _asset()
        return await _request("createInvoice", params)


async def get_invoices(invoice_ids: list[str]) -> list[dict[str, Any]]:
    if not invoice_ids:
        return []
    params = {"invoice_ids": ",".join(str(i) for i in invoice_ids)}
    result = await _request("getInvoices", params)
    if isinstance(result, dict):
        return result.get("items") or []
    if isinstance(result, list):
        return result
    return []


# ============================================================================
# Webhook signature verification
# ============================================================================
def verify_webhook_signature(token: str, body: bytes, signature: str) -> bool:
    """Verify Crypto-Pay-API-Signature header.

    Algorithm: sha256(token) is the HMAC key, hex(hmac_sha256(secret, body)).
    """
    if not signature:
        return False
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    mac = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature.strip())
