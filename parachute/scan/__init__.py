"""scan_world(root) -> a plain dict describing the whole world. Pure read; the loader writes it to FalkorDB."""
from __future__ import annotations

import json
from pathlib import Path

from ..guard import OutsideSandbox, require_inside
from .backups import scan_backups
from .fs import scan_trees
from .gitscan import scan_git
from .recipes import detect_recipes


def scan_world(root, allow_outside: bool = False) -> dict:
    root = Path(root).resolve()
    marker = root / ".parachute" / "world.json"
    if not marker.is_file():
        raise OutsideSandbox(f"{root} is not a Parachute sandbox (missing .parachute/world.json)")
    meta = json.loads(marker.read_text())
    stores = [dict(s) for s in meta["stores"]]
    for s in stores:
        require_inside(root, root / s["path"], allow_outside)  # a hostile world.json cannot point outside
    tree_dirs = [root / s["path"] for s in stores if s["kind"] == "tree"]

    recipes = detect_recipes(root, tree_dirs)
    items = scan_trees(root, stores, recipes)
    by_id = {i["id"]: i for i in items}
    for r in recipes:
        r["reads"] = [x for x in r["reads"] if x in by_id]
    g = scan_git(root, stores, tree_dirs, by_id, allow_outside)
    b = scan_backups(root, stores, by_id, allow_outside)
    domain_of = {s["id"]: s["domain"] for s in stores}
    for it in items:
        it["domain"] = domain_of[it["store"]]
    for c in b["copies"]:
        c["domain"] = domain_of[c["store"]]
    for r in g["refs"]:
        r["domain"] = domain_of.get(r["store"])
    return {
        "session": meta["session"], "domains": meta["domains"], "stores": stores,
        "items": items, "recipes": recipes,
        "copies": b["copies"], "copy_of": b["copy_of"], "needs": b["needs"],
        "commits": g["commits"], "parents": g["parents"], "refs": g["refs"],
        "points_to": g["points_to"], "tracked": g["tracked"],
        "warnings": g["warnings"] + b["warnings"],
    }
