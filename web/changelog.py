"""Changelog / blog posts (file-backed)."""

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

from data_paths import WEB_DIR, data_file

logger = logging.getLogger(__name__)

CHANGELOG_FILE = data_file("changelog.json")
SEED_FILE = WEB_DIR / "content" / "changelog_seed.json"


@dataclass
class ChangelogEntry:
    id: str
    version: str
    title: str
    date: str
    tags: list[str] = field(default_factory=list)
    body: str = ""
    published: bool = True
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "date": self.date,
            "tags": list(self.tags or []),
            "body": self.body,
            "createdAt": self.created_at,
        }


class ChangelogStore:
    def __init__(self, path: Path = CHANGELOG_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: list[ChangelogEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._seed_if_needed()
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logger.exception("ChangelogStore load failed")
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                tags = row.get("tags") or []
                if not isinstance(tags, list):
                    tags = []
                self._items.append(
                    ChangelogEntry(
                        id=str(row["id"]),
                        version=str(row.get("version", "")),
                        title=str(row.get("title", "")),
                        date=str(row.get("date", "")),
                        tags=[str(t) for t in tags],
                        body=str(row.get("body", "")),
                        published=bool(row.get("published", True)),
                        created_at=float(row.get("created_at", 0) or time.time()),
                    )
                )
            except (KeyError, ValueError):
                continue

    def _seed_if_needed(self) -> None:
        if not SEED_FILE.is_file():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            raw = SEED_FILE.read_text(encoding="utf-8")
            with self.path.open("w", encoding="utf-8") as f:
                f.write(raw)
        except OSError:
            logger.exception("Changelog seed failed")

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = [asdict(x) for x in self._items]
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("ChangelogStore save failed")
            try:
                tmp.unlink()
            except OSError:
                pass

    def list_public(self, *, limit: int = 50) -> list[ChangelogEntry]:
        rows = [x for x in self._items if x.published]
        rows.sort(key=lambda x: (x.date, x.created_at), reverse=True)
        return rows[:limit]

    def list_all(self) -> list[ChangelogEntry]:
        return sorted(self._items, key=lambda x: (x.date, x.created_at), reverse=True)

    def create(
        self,
        *,
        version: str,
        title: str,
        date: str,
        body: str,
        tags: Optional[list[str]] = None,
        published: bool = True,
    ) -> ChangelogEntry:
        title = title.strip()
        body = body.strip()
        if not title or not body:
            raise ValueError("Заголовок и текст обязательны")
        entry = ChangelogEntry(
            id=secrets.token_urlsafe(8),
            version=version.strip()[:32],
            title=title[:200],
            date=date.strip()[:16] or time.strftime("%Y-%m-%d"),
            tags=[str(t).strip()[:32] for t in (tags or []) if str(t).strip()],
            body=body[:12000],
            published=bool(published),
        )
        with self._lock:
            self._items.append(entry)
            self._save_locked()
        return entry
