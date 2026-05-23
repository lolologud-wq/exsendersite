"""In-memory health/telemetry per userbot slot.

The spam scheduler records sends / errors here. The dashboard reads
snapshots and combines them with live Telethon connection state to compute
a high-level status ('работает', 'слетел прокси', 'умер бот' и т.п.).

No persistence — lives only while the bot process is running.
"""

from __future__ import annotations

import time
from typing import Any, Optional

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
