"""Hourly send-activity buckets (persisted JSON for dashboard heatmap)."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

ACTIVITY_PATH = os.path.join(os.path.dirname(__file__), "activity.json")
_lock = threading.Lock()
MAX_BUCKETS_DAYS = 42


def _hour_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y%m%d%H")


def _load() -> dict[str, Any]:
    if not os.path.isfile(ACTIVITY_PATH):
        return {"global": {}, "accounts": {}}
    try:
        with open(ACTIVITY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("global", {})
            data.setdefault("accounts", {})
            return data
    except Exception:
        pass
    return {"global": {}, "accounts": {}}


def _save(data: dict[str, Any]) -> None:
    tmp = ACTIVITY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, ACTIVITY_PATH)


def _prune(buckets: dict[str, int]) -> dict[str, int]:
    cutoff = (datetime.now() - timedelta(days=MAX_BUCKETS_DAYS)).strftime("%Y%m%d%H")
    return {k: int(v) for k, v in buckets.items() if k >= cutoff_key and int(v) > 0}


def record_send(account_key: str) -> None:
    if not account_key:
        return
    key = _hour_key()
    with _lock:
        data = _load()
        g = data.setdefault("global", {})
        g[key] = int(g.get(key, 0)) + 1
        data["global"] = _prune(g)
        acc_map = data.setdefault("accounts", {})
        ab = acc_map.setdefault(account_key, {})
        ab[key] = int(ab.get(key, 0)) + 1
        acc_map[account_key] = _prune(ab)
        _save(data)


def get_activity(days: int = 14, account_key: Optional[str] = None) -> dict[str, Any]:
    days = max(1, min(int(days or 14), MAX_BUCKETS_DAYS))
    now = datetime.now()
    start = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    with _lock:
        data = _load()
        if account_key:
            buckets = dict(data.get("accounts", {}).get(account_key, {}))
        else:
            buckets = dict(data.get("global", {}))

    rows: list[dict[str, Any]] = []
    total = 0
    max_val = 0
    cur = start
    end_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    while cur <= end_day:
        hours: list[int] = []
        day_total = 0
        for h in range(24):
            hk = cur.replace(hour=h).strftime("%Y%m%d%H")
            c = int(buckets.get(hk, 0))
            hours.append(c)
            day_total += c
            max_val = max(max_val, c)
        rows.append(
            {
                "date": cur.strftime("%Y-%m-%d"),
                "weekday": cur.weekday(),
                "hours": hours,
                "total": day_total,
            }
        )
        total += day_total
        cur += timedelta(days=1)

    return {
        "days": days,
        "account": account_key,
        "total": total,
        "max": max_val,
        "rows": rows,
        "timezone": "local",
    }
