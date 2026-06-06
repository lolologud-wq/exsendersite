#!/usr/bin/env python3
"""Insert a changelog entry from seed into live changelog.json (idempotent)."""
from __future__ import annotations

import json
import os
import sys

path = os.environ.get("CHANGELOG_PATH", "/opt/exsender/data/changelog.json")
seed_path = os.environ.get(
    "CHANGELOG_SEED", "/opt/exsender/web/content/changelog_seed.json"
)
entry_id = sys.argv[1] if len(sys.argv) > 1 else ""

if not entry_id:
    print("usage: patch_changelog_entry.py <entry_id>", file=sys.stderr)
    raise SystemExit(2)

with open(seed_path, encoding="utf-8") as f:
    seed = json.load(f)
new = next(x for x in seed if x.get("id") == entry_id)

if not os.path.isfile(path):
    items = [new]
    print("created")
else:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        items = []
    if any(isinstance(x, dict) and x.get("id") == entry_id for x in items):
        print("exists")
        raise SystemExit(0)
    items.insert(0, new)
    print("added")

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
