"""Backup/manifest detection: every backup store may carry a MANIFEST.json describing its copies.

A copy is `alive` only if its archive exists AND its sha256 matches the manifest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..guard import OutsideSandbox, require_inside


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_backups(root: Path, stores: list[dict], items_by_id: dict, allow_outside: bool = False) -> dict:
    copies, copy_of, needs, warnings = [], [], [], []
    for s in stores:
        if s["kind"] != "backup":
            continue
        sdir = root / s["path"]
        man = sdir / "MANIFEST.json"
        if not man.is_file():
            continue
        try:
            entries = json.loads(man.read_text())["copies"]
        except (ValueError, KeyError) as e:
            warnings.append(f"{s['id']}: unreadable manifest ({e})")
            continue
        for e in entries:
            alive = 0
            try:
                f = require_inside(root, sdir / e["file"], allow_outside)
                alive = int(f.is_file() and _sha256(f) == e["sha256"])
            except (OutsideSandbox, KeyError, OSError) as ex:
                warnings.append(f"{s['id']}/{e.get('id')}: {ex}")
            copies.append({"id": e["id"], "kind": e.get("kind", "backup"), "file": e.get("file"),
                           "store": s["id"], "alive": alive, "encrypted": int(bool(e.get("encrypted"))),
                           "sha256": e.get("sha256"), "verified_at": e.get("verified_at")})
            for iid in e.get("covers", []):
                if iid in items_by_id:
                    copy_of.append({"copy": e["id"], "item": iid, "kind": e.get("kind", "backup"),
                                    "verified_at": e.get("verified_at")})
                else:
                    warnings.append(f"{e['id']}: covers unknown item {iid}")
            for iid in e.get("needs", []):
                if iid in items_by_id:
                    needs.append({"copy": e["id"], "item": iid})
                else:
                    warnings.append(f"{e['id']}: needs unknown item {iid}")
    return {"copies": copies, "copy_of": copy_of, "needs": needs, "warnings": warnings}
