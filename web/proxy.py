"""HTTP client to talk to remote bots' API.

When deploy-key exists, uses a reused SSH tunnel (VDS API port is usually
firewall-closed externally). Direct HTTP is tried only without SSH key.
"""

from __future__ import annotations

import asyncio
import logging
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
HEALTH_CACHE_TTL = 12.0

_health_cache: dict[str, tuple[float, dict[str, Any]]] = {}


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
            return not self.conn.is_closed()
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
            connect_timeout=20,
            keepalive_interval=30,
            keepalive_count_max=4,
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
    for attempt in range(2):
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
        except (asyncssh.Error, OSError, httpx.HTTPError) as e:
            last_err = e
            logger.warning("SSH tunnel attempt %s for %s failed: %s", attempt + 1, rec.host, e)
            await _pool.invalidate(rec.id)
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
    now = time.time()
    cached = _health_cache.get(rec.id)
    if cached and now - cached[0] < HEALTH_CACHE_TTL:
        return cached[1]

    try:
        r = await bot_request(rec, "GET", "/health", timeout=HEALTH_TIMEOUT)
        result = {"reachable": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        result = {"reachable": False, "error": str(e)}

    _health_cache[rec.id] = (now, result)
    return result
