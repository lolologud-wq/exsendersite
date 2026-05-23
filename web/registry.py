"""Registry of bots managed by the site.

Each bot record holds enough to (a) SSH-deploy or maintain it and
(b) talk to its HTTP API. Persisted as `web/bots.json`.

Schema (per bot):
  id          uuid
  alias       human label
  host        IP or DNS
  ssh_port    int (default 22)
  ssh_user    str (default 'root')
  install_dir str (default '/opt/userbot')
  api_port    int (default 8080) — port of bot_api on the VDS
  api_token   str — Bearer token to call its API (generated at deploy)
  status      new | deploying | running | stopped | error
  last_deploy_at, last_error, has_ssh_key
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "bots.json")
_lock = threading.Lock()
logger = logging.getLogger(__name__)


@dataclass
class BotRecord:
    id: str
    host: str
    ssh_port: int = 22
    ssh_user: str = "root"
    alias: str = ""
    install_dir: str = "/opt/userbot"
    api_port: int = 8080
    api_token: str = ""
    status: str = "new"
    last_deploy_at: Optional[float] = None
    last_error: str = ""
    has_ssh_key: bool = False

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        # never leak the bot API token over public list responses
        d["hasApiToken"] = bool(self.api_token)
        d.pop("api_token", None)
        # camelCase for the frontend
        return {
            "id": d["id"],
            "alias": d["alias"],
            "host": d["host"],
            "sshPort": d["ssh_port"],
            "sshUser": d["ssh_user"],
            "installDir": d["install_dir"],
            "apiPort": d["api_port"],
            "hasApiToken": d["hasApiToken"],
            "status": d["status"],
            "lastDeployAt": d["last_deploy_at"],
            "lastError": d["last_error"],
            "hasSshKey": d["has_ssh_key"],
        }


def _read_raw() -> dict[str, Any]:
    if not os.path.isfile(REGISTRY_PATH):
        return {"bots": {}}
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f) or {"bots": {}}
    except (json.JSONDecodeError, OSError) as e:
        backup = REGISTRY_PATH + ".broken"
        try:
            if os.path.isfile(REGISTRY_PATH):
                os.replace(REGISTRY_PATH, backup)
        except OSError:
            pass
        logger.warning("bots.json повреждён (%s), создан новый. Бэкап: %s", e, backup)
        return {"bots": {}}


def _write_raw(data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    d = os.path.dirname(REGISTRY_PATH) or "."
    fd, tmp = tempfile.mkstemp(suffix=".json.tmp", prefix="bots_", dir=d, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, REGISTRY_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class BotRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, BotRecord] = {}
        self._load()

    def _load(self) -> None:
        with _lock:
            raw = _read_raw()
            self._cache = {}
            allowed = BotRecord.__dataclass_fields__
            for bid, bd in (raw.get("bots") or {}).items():
                if not isinstance(bd, dict):
                    continue
                cleaned = {k: v for k, v in bd.items() if k in allowed}
                cleaned.setdefault("id", bid)
                try:
                    self._cache[bid] = BotRecord(**cleaned)
                except TypeError as e:
                    logger.warning("Пропущена битая запись бота %s: %s", bid, e)

    def _save(self) -> None:
        with _lock:
            _write_raw({"bots": {bid: asdict(b) for bid, b in self._cache.items()}})

    def list(self) -> list[BotRecord]:
        return list(self._cache.values())

    def get(self, bid: str) -> Optional[BotRecord]:
        return self._cache.get(bid)

    def add(
        self,
        *,
        host: str,
        ssh_port: int = 22,
        ssh_user: str = "root",
        alias: str = "",
        install_dir: str = "/opt/userbot",
        api_port: int = 8080,
        api_token: str = "",
    ) -> BotRecord:
        bid = uuid.uuid4().hex[:12]
        rec = BotRecord(
            id=bid,
            host=host.strip(),
            ssh_port=int(ssh_port),
            ssh_user=ssh_user.strip() or "root",
            alias=alias.strip(),
            install_dir=install_dir.strip() or "/opt/userbot",
            api_port=int(api_port),
            api_token=api_token,
        )
        self._cache[bid] = rec
        self._save()
        return rec

    def update(self, bid: str, **patch: Any) -> Optional[BotRecord]:
        rec = self._cache.get(bid)
        if rec is None:
            return None
        for k, v in patch.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        self._save()
        return rec

    def remove(self, bid: str) -> bool:
        if bid not in self._cache:
            return False
        del self._cache[bid]
        self._save()
        return True
