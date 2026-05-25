"""Persistent JSON storage — survives redeploy when SITE_DATA_DIR is set."""

from __future__ import annotations

import os
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent


def data_dir() -> Path:
    raw = os.getenv("SITE_DATA_DIR", "").strip()
    if raw:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return WEB_DIR


def data_file(name: str) -> Path:
    return data_dir() / name
