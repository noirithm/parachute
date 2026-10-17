"""Small helpers shared by the fix catalog, the executor and the restore oracle."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SQLITE_EXT = (".sqlite", ".sqlite3", ".db")


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_sqlite(path) -> bool:
    return str(path).lower().endswith(SQLITE_EXT)


def sqlite_logical_hash(path) -> str:
    """Hash of schema and rows. An online backup is logically identical but not always byte-identical."""
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for line in con.iterdump():
            h.update(line.encode() + b"\n")
        return h.hexdigest()
    finally:
        con.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_manifest(store_dir: Path, entry: dict) -> None:
    manifest = store_dir / "MANIFEST.json"
    data = json.loads(manifest.read_text()) if manifest.is_file() else {"copies": []}
    data["copies"] = [c for c in data["copies"] if c.get("id") != entry["id"]] + [entry]
    tmp = manifest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, manifest)
