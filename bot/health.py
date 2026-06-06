"""In-memory health/telemetry per userbot slot.

The spam scheduler records sends / errors here. The dashboard reads
snapshots and combines them with live Telethon connection state to compute
a high-level status ('работает', 'слетел прокси', 'умер бот' и т.п.).

No persistence — lives only while the bot process is running.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import activity

_state: dict[str, dict[str, Any]] = {}


def _slot(account_key: str) -> dict[str, Any]:
    if account_key not in _state:
        _state[account_key] = {
            "lastSendAt": 0.0,
            "sendsTotal": 0,
            "lastErrorAt": 0.0,
            "lastError": "",
            "lastErrorKind": "",
            "lastErrorChatId": None,
            "errorsTotal": 0,
        }
    return _state[account_key]


def note_send(account_key: str, chat_id: Optional[int] = None) -> None:
    s = _slot(account_key)
    s["lastSendAt"] = time.time()
    s["sendsTotal"] += 1
    try:
        activity.record_send(account_key)
    except Exception:
        pass


def note_error(
    account_key: str,
    err: BaseException,
    *,
    chat_id: Optional[int] = None,
) -> None:
    s = _slot(account_key)
    s["lastErrorAt"] = time.time()
    s["lastError"] = str(err)[:200]
    s["lastErrorKind"] = type(err).__name__
    s["lastErrorChatId"] = chat_id
    s["errorsTotal"] += 1


def snapshot(account_key: str) -> Optional[dict[str, Any]]:
    s = _state.get(account_key)
    return dict(s) if s else None


def drop(account_key: str) -> None:
    _state.pop(account_key, None)


def restore_from_persisted(
    account_key: str,
    *,
    errors_total: int = 0,
    last_error: str = "",
    last_error_kind: str = "",
    last_error_at: float = 0.0,
    last_error_chat_id: Optional[int] = None,
) -> None:
    """Seed in-memory counters after process restart from runtime_state.json."""
    if not errors_total and not last_error:
        return
    s = _slot(account_key)
    s["errorsTotal"] = max(int(s.get("errorsTotal") or 0), int(errors_total or 0))
    persisted_at = float(last_error_at or 0)
    if last_error and persisted_at >= float(s.get("lastErrorAt") or 0):
        s["lastError"] = str(last_error)[:200]
        s["lastErrorKind"] = str(last_error_kind or "")
        s["lastErrorAt"] = persisted_at
        s["lastErrorChatId"] = last_error_chat_id
