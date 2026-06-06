#!/usr/bin/env python3
import json
import os
import sys

path = "/opt/exsender/data/changelog.json"
seed_path = "/opt/exsender/web/content/changelog_seed.json"
entry_id = "seed-20260528b"

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
        sys.exit(0)
    items.insert(0, new)
    print("added")

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
