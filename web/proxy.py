"""HTTP client to talk to remote bots' API.

When deploy-key exists, uses a reused SSH tunnel (VDS API port is usually
firewall-closed externally). Direct HTTP is tried only without SSH key.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh
import httpx

from registry import BotRecord

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
DEPLOY_KEY = WEB_DIR / "deploy_key"

DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
DIRECT_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
HEALTH_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
OVERVIEW_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HEALTH_OK_CACHE_TTL = 45.0
HEALTH_FAIL_CACHE_TTL = 20.0
HEALTH_STALE_OK_SEC = 120.0
HEALTH_OFFLINE_AFTER_FAILS = 3
OVERVIEW_CACHE_TTL = 6.0
SSH_TUNNEL_RETRIES = 3

_health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_overview_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class _HealthTrack:
    reachable: bool = True
    last_ok_at: float = 0.0
    last_fail_at: float = 0.0
    consecutive_fails: int = 0
    last_error: str = ""


_health_track: dict[str, _HealthTrack] = {}


def _track(rec_id: str) -> _HealthTrack:
    if rec_id not in _health_track:
        _health_track[rec_id] = _HealthTrack()
    return _health_track[rec_id]


def health_cache_ttl(result: dict[str, Any]) -> float:
    return HEALTH_OK_CACHE_TTL if result.get("reachable") else HEALTH_FAIL_CACHE_TTL


def cached_health(rec_id: str) -> dict[str, Any] | None:
    """Return cached health if still fresh, else None."""
    cached = _health_cache.get(rec_id)
    if not cached:
        return None
    ts, result = cached
    if time.time() - ts < health_cache_ttl(result):
        return result
    return None


def _stale_health_view(rec_id: str) -> dict[str, Any] | None:
    """Gracefully serve last-known-good while revalidating."""
    cached = _health_cache.get(rec_id)
    tr = _track(rec_id)
    now = time.time()
    if tr.last_ok_at and now - tr.last_ok_at < HEALTH_STALE_OK_SEC:
        err = tr.last_error if tr.consecutive_fails >= HEALTH_OFFLINE_AFTER_FAILS else ""
        return {"reachable": True, "stale": True, "error": err}
    if cached:
        ts, result = cached
        if result.get("reachable") and now - ts < HEALTH_STALE_OK_SEC:
            return {**result, "stale": True}
    return None


def _record_health_success(rec_id: str) -> dict[str, Any]:
    tr = _track(rec_id)
    now = time.time()
    tr.reachable = True
    tr.last_ok_at = now
    tr.consecutive_fails = 0
    tr.last_error = ""
    result = {"reachable": True}
    _health_cache[rec_id] = (now, result)
    return result


def _record_health_failure(rec_id: str, err: str) -> dict[str, Any]:
    tr = _track(rec_id)
    now = time.time()
    tr.consecutive_fails += 1
    tr.last_fail_at = now
    tr.last_error = err[:200]
    still_ok = (
        tr.last_ok_at > 0
        and now - tr.last_ok_at < HEALTH_STALE_OK_SEC
        and tr.consecutive_fails < HEALTH_OFFLINE_AFTER_FAILS
    )
    if still_ok:
        tr.reachable = True
        result = {"reachable": True, "error": ""}
    else:
        tr.reachable = False
        result = {"reachable": False, "error": tr.last_error}
    _health_cache[rec_id] = (now, result)
    return result


def _ssh_tunnel_broken(exc: BaseException) -> bool:
    if isinstance(exc, asyncssh.Error):
        return True
    if isinstance(exc, OSError):
        return True
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, httpx.ConnectTimeout):
        return True
    return False


def _headers(rec: BotRecord, extra: dict[str, str] | None = None) -> dict[str, str]:
    h: dict[str, str] = {}
    if rec.api_token:
        h["Authorization"] = f"Bearer {rec.api_token}"
    if extra:
        h.update(extra)
    return h


def _api_path(path: str) -> str:
    p = path.lstrip("/")
    if p.startswith("api/local/"):
        return "/" + p
    return "/api/local/" + p


def _can_ssh(rec: BotRecord) -> bool:
    return bool(rec.has_ssh_key and DEPLOY_KEY.exists())


@dataclass
class _TunnelEntry:
    conn: asyncssh.SSHClientConnection
    listener: asyncssh.listener.SSHListener
    port: int
    api_port: int

    def alive(self) -> bool:
        try:
            if self.conn.is_closed():
                return False
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.4):
                pass
            return True
        except Exception:
            return False

    async def close(self) -> None:
        try:
            self.listener.close()
        except Exception:
            pass
        try:
            self.conn.close()
            await self.conn.wait_closed()
        except Exception:
            pass


class _TunnelPool:
    """One persistent SSH forward per bot — avoids SSH rate-limit drops."""

    def __init__(self) -> None:
        self._entries: dict[str, _TunnelEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, bot_id: str) -> asyncio.Lock:
        if bot_id not in self._locks:
            self._locks[bot_id] = asyncio.Lock()
        return self._locks[bot_id]

    async def port_for(self, rec: BotRecord) -> int:
        async with self._lock(rec.id):
            entry = self._entries.get(rec.id)
            if entry and entry.alive() and entry.api_port == rec.api_port:
                return entry.port
            if entry:
                await entry.close()
            entry = await self._open(rec)
            self._entries[rec.id] = entry
            return entry.port

    async def invalidate(self, bot_id: str) -> None:
        async with self._lock(bot_id):
            entry = self._entries.pop(bot_id, None)
            if entry:
                await entry.close()

    async def _open(self, rec: BotRecord) -> _TunnelEntry:
        conn = await asyncssh.connect(
            rec.host,
            port=rec.ssh_port,
            username=rec.ssh_user,
            client_keys=[str(DEPLOY_KEY)],
            known_hosts=None,
            connect_timeout=15,
            keepalive_interval=20,
            keepalive_count_max=6,
        )
        listener = await conn.forward_local_port("", 0, "127.0.0.1", rec.api_port)
        return _TunnelEntry(conn, listener, listener.get_port(), rec.api_port)


_pool = _TunnelPool()


async def _httpx_call(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    json: dict | None = None,
    params: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    url = base_url.rstrip("/") + _api_path(path)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, trust_env=False) as client:
        return await client.request(
            method,
            url,
            json=json,
            params=params,
            files=files,
            data=data,
            content=content,
        )


async def _request_via_ssh(
    rec: BotRecord,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    json: dict | None = None,
    params: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    last_err: Exception | None = None
    for attempt in range(SSH_TUNNEL_RETRIES):
        try:
            local_port = await _pool.port_for(rec)
            return await _httpx_call(
                f"http://127.0.0.1:{local_port}",
                method,
                path,
                headers=headers,
                timeout=timeout,
                json=json,
                params=params,
                files=files,
                data=data,
                content=content,
            )
        except Exception as e:
            last_err = e
            logger.warning(
                "SSH tunnel attempt %s/%s for %s failed: %s",
                attempt + 1,
                SSH_TUNNEL_RETRIES,
                rec.host,
                e,
            )
            if _ssh_tunnel_broken(e):
                await _pool.invalidate(rec.id)
            if attempt + 1 < SSH_TUNNEL_RETRIES:
                await asyncio.sleep(0.35 * (attempt + 1))
    raise RuntimeError(
        f"бот недоступен через SSH ({last_err}). Проверь: systemctl status userbot на {rec.host}"
    ) from last_err


async def bot_request(
    rec: BotRecord,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
    content: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> httpx.Response:
    headers = _headers(rec, extra_headers)

    if _can_ssh(rec):
        return await _request_via_ssh(
            rec, method, path,
            headers=headers, timeout=timeout,
            json=json, params=params, files=files, data=data, content=content,
        )

    direct_base = f"http://{rec.host}:{rec.api_port}"
    try:
        return await _httpx_call(
            direct_base, method, path,
            headers=headers, timeout=DIRECT_TIMEOUT,
            json=json, params=params, files=files, data=data, content=content,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        raise RuntimeError(
            f"бот недоступен по {rec.host}:{rec.api_port} ({e}). "
            "Заверши deploy — нужен SSH-ключ для туннеля."
        ) from e


async def request(
    rec: BotRecord,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> httpx.Response:
    return await bot_request(
        rec, method, path,
        json=json, params=params, files=files, data=data, timeout=timeout,
    )


async def get_json(rec: BotRecord, path: str, **kw: Any) -> tuple[int, dict | list | None]:
    r = await request(rec, "GET", path, **kw)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


async def healthcheck(rec: BotRecord) -> dict:
    fresh = cached_health(rec.id)
    if fresh is not None:
        return fresh

    stale = _stale_health_view(rec.id)
    try:
        r = await bot_request(rec, "GET", "/health", timeout=HEALTH_TIMEOUT)
        if r.status_code == 200:
            return _record_health_success(rec.id)
        err = f"health HTTP {r.status_code}"
        if stale and stale.get("reachable"):
            _record_health_failure(rec.id, err)
            return stale
        return _record_health_failure(rec.id, err)
    except Exception as e:
        err = str(e)
        if stale and stale.get("reachable"):
            _record_health_failure(rec.id, err)
            return stale
        return _record_health_failure(rec.id, err)


async def refresh_health_background(bots: list[BotRecord]) -> None:
    if not bots:
        return
    await asyncio.gather(*(warm_bot(b) for b in bots), return_exceptions=True)


async def warm_bot(rec: BotRecord) -> None:
    await healthcheck(rec)
    try:
        await fetch_overview(rec)
    except Exception as e:
        logger.debug("warm overview %s: %s", rec.id, e)


async def fetch_overview(rec: BotRecord, *, force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force:
        cached = _overview_cache.get(rec.id)
        if cached and now - cached[0] < OVERVIEW_CACHE_TTL:
            return cached[1]
        try:
            from overview_cache import get as get_overview_cache

            disk = get_overview_cache(rec.id, max_age_sec=86400)
            if disk is not None:
                _overview_cache[rec.id] = (now, disk)
                return disk
        except Exception:
            pass

    status, data = await get_json(rec, "overview", timeout=OVERVIEW_TIMEOUT)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"overview HTTP {status}")

    _overview_cache[rec.id] = (now, data)
    try:
        from overview_cache import put as put_overview_cache

        put_overview_cache(rec.id, data)
    except Exception:
        pass
    return data


async def fetch_overview_pack(rec: BotRecord) -> dict[str, Any]:
    try:
        data = await fetch_overview(rec)
        return {"botId": rec.id, "ok": True, "overview": data}
    except Exception as e:
        return {"botId": rec.id, "ok": False, "error": str(e)}


async def refresh_overviews_live(bots: list[BotRecord], *, force: bool = True) -> None:
    if not bots:
        return
    await asyncio.gather(
        *(fetch_overview(b, force=force) for b in bots),
        return_exceptions=True,
    )


def invalidate_overview_cache(bot_id: str) -> None:
    _overview_cache.pop(bot_id, None)
    try:
        from overview_cache import invalidate as invalidate_disk_overview_cache

        invalidate_disk_overview_cache(bot_id)
    except Exception:
        pass


def apply_health_to_item(item: dict[str, Any], rec: BotRecord) -> bool:
    """Fill reachable from cache; return True if background refresh is needed."""
    fresh = cached_health(rec.id)
    if fresh is not None:
        item["reachable"] = bool(fresh.get("reachable"))
        if fresh.get("error"):
            item["lastError"] = str(fresh["error"])[:200]
        return False

    stale = _stale_health_view(rec.id)
    if stale is not None:
        item["reachable"] = bool(stale.get("reachable"))
        if stale.get("error") and not stale.get("reachable"):
            item["lastError"] = str(stale["error"])[:200]
        return True

    tr = _track(rec.id)
    item["reachable"] = tr.reachable if tr.last_ok_at or tr.last_fail_at else True
    if not item["reachable"] and tr.last_error:
        item["lastError"] = tr.last_error[:200]
    return True
