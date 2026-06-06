"""Cached bot overview snapshots — dashboard loads instantly from disk."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from data_paths import data_dir

logger = logging.getLogger(__name__)

_CACHE_DIR = data_dir() / "overview_cache"
_mem: dict[str, tuple[float, dict[str, Any]]] = {}


def _path(bot_id: str) -> Path:
    return _CACHE_DIR / f"{bot_id}.json"


def get(bot_id: str, *, max_age_sec: float = 86400) -> dict[str, Any] | None:
    now = time.time()
    hit = _mem.get(bot_id)
    if hit and now - hit[0] < max_age_sec:
        return hit[1]

    p = _path(bot_id)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        ts = float(raw.get("_cachedAt") or 0)
        data = raw.get("overview")
        if not isinstance(data, dict) or not ts:
            return None
        if now - ts > max_age_sec:
            return None
        _mem[bot_id] = (ts, data)
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        logger.debug("overview cache read %s: %s", bot_id, e)
        return None


def put(bot_id: str, overview: dict[str, Any]) -> None:
    ts = time.time()
    _mem[bot_id] = (ts, overview)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"_cachedAt": ts, "overview": overview}
    tmp = _path(bot_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_path(bot_id))


def age_sec(bot_id: str) -> float | None:
    hit = _mem.get(bot_id)
    if hit:
        return max(0.0, time.time() - hit[0])
    p = _path(bot_id)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        ts = float(raw.get("_cachedAt") or 0)
        return max(0.0, time.time() - ts) if ts else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
