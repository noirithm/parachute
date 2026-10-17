"""Fix catalog: sandbox-safe actions that make items or commits recoverable before a command runs.

A Fix id is derived from its kind and parameters, so the effects predicted for the what-if fork and the
artifacts a real run creates carry the same names, and the rescanned world matches the prediction.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..scan.gitscan import git
from .layout import Layout
from .model import Destroyed
from .util import append_manifest, is_sqlite, now_iso, sha256_file, sqlite_logical_hash

KIND_ORDER = {"git_push": 0, "secrets_vault": 1, "copy_backup": 2, "sqlite_backup": 3, "git_bundle": 4}
COMMIT_COST = 4096
TAR_OVERHEAD = 1024


class FixFailed(RuntimeError):
    pass


@dataclass
class Fix:
    kind: str
    params: dict
    cost: int

    @property
    def id(self) -> str:
        blob = json.dumps([self.kind, self.params], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "params": self.params, "cost": self.cost}

    @classmethod
    def from_dict(cls, d: dict) -> "Fix":
        return cls(d["kind"], d["params"], d["cost"])


def store_by_id(layout: Layout, store_id: str) -> dict:
    return next(s for s in layout.stores if s["id"] == store_id)


def _usable(store: dict, destroyed: Destroyed) -> bool:
    prefix = store["path"] + "/"
    return store["id"] not in destroyed.stores and not any(p.startswith(prefix) for p in destroyed.paths)


def _pick_store(layout: Layout, destroyed: Destroyed, kind: str, avoid_domains: set):
    for s in layout.stores:
        if s["kind"] == kind and s["domain"] not in avoid_domains and _usable(s, destroyed):
            return s
    return None


def _size(layout: Layout, item_id: str) -> int:
    path = os.path.join(layout.root, item_id)
    return os.path.getsize(path) if os.path.isfile(path) else 0


def _key_id(layout: Layout, fix: Fix) -> str:
    return f"{store_by_id(layout, fix.params['keyring'])['path']}/parachute-{fix.id}.key"


def _repos(layout: Layout) -> list:
    return [rel for rel, head in layout.git_dirs() if head.endswith("/.git/HEAD")]


def _git_ok(layout: Layout, repo: str, args: list) -> bool:
    try:
        git(args, cwd=os.path.join(layout.root, repo))
        return True
    except RuntimeError:
        return False


def _remotes(layout: Layout, repo: str) -> list:
    cwd = os.path.join(layout.root, repo)
    found = []
    for line in git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd=cwd, check=False).splitlines():
        key, _, url = line.partition(" ")
        url = url.strip()
        if "://" in url or ("@" in url and ":" in url):
            continue
        path = os.path.realpath(url if os.path.isabs(url) else os.path.join(cwd, url))
        if layout.inside(path) and os.path.isdir(path):
            found.append((key[len("remote."):-len(".url")], layout.rel(path)))
    return found


def _commit_fixes(layout: Layout, destroyed: Destroyed, shas: list) -> list:
    by_repo: dict = {}
    for sha in sorted(set(shas)):
        repo = next((r for r in _repos(layout) if _git_ok(layout, r, ["cat-file", "-e", f"{sha}^{{commit}}"])), None)
        if repo:
            by_repo.setdefault(repo, []).append(sha)
    found = []
    for repo, group in sorted(by_repo.items()):
        tips = sorted(s for s in group
                      if not any(o != s and _git_ok(layout, repo, ["merge-base", "--is-ancestor", s, o]) for o in group))
        domain = layout.domain_of(repo)
        for name, bare in _remotes(layout, repo):
            store = layout.store_of(bare)
            if store and store["domain"] != domain and _usable(store, destroyed):
                found.append(Fix("git_push", {"repo": repo, "remote": name, "remote_repo": bare, "tips": tips},
                                 COMMIT_COST * len(group)))
        vault = _pick_store(layout, destroyed, "backup", {domain})
        if vault:
            found.append(Fix("git_bundle", {"repo": repo, "store": vault["id"], "tips": tips},
                             COMMIT_COST * len(group) + 2 * TAR_OVERHEAD))
    return found


def candidates(layout: Layout, destroyed: Destroyed, outcome: dict) -> list:
    entries = outcome["lost"] + outcome["fragile"]
    secret = sorted(e["id"] for e in entries if e["class"] == "SECRET")
    plain = sorted(e["id"] for e in entries if e["class"] != "SECRET")
    dbs = [i for i in plain if is_sqlite(i)]
    files = [i for i in plain if not is_sqlite(i)]

    def domains(ids):
        return {layout.domain_of(i) for i in ids}

    found = []
    if files:
        store = _pick_store(layout, destroyed, "backup", domains(files))
        if store:
            found.append(Fix("copy_backup", {"store": store["id"], "items": files},
                             sum(_size(layout, i) + TAR_OVERHEAD for i in files)))
    for db in dbs:
        store = _pick_store(layout, destroyed, "backup", domains([db]))
        if store:
            found.append(Fix("sqlite_backup", {"store": store["id"], "item": db}, _size(layout, db) + COMMIT_COST))
    if secret:
        vault = _pick_store(layout, destroyed, "backup", domains(secret))
        ring = _pick_store(layout, destroyed, "tree", domains(secret) | {vault["domain"]}) if vault else None
        if vault and ring:
            found.append(Fix("secrets_vault", {"store": vault["id"], "keyring": ring["id"], "items": secret},
                             sum(_size(layout, i) + TAR_OVERHEAD for i in secret) + 64))
    shas = [c["sha"] for c in outcome["lost_commits"] + outcome["fragile_commits"]]
    return found + _commit_fixes(layout, destroyed, shas)


def effects(fix: Fix, layout: Layout) -> dict:
    out = {"items": [], "copies": [], "copy_of": [], "needs": [], "refs": [], "points": []}
    p, fid = fix.params, fix.id
    if fix.kind in ("copy_backup", "sqlite_backup", "secrets_vault"):
        store = store_by_id(layout, p["store"])
        ids = p["items"] if "items" in p else [p["item"]]
        kind = "snapshot" if fix.kind == "sqlite_backup" else "backup"
        suffix = {"copy_backup": ".tar", "sqlite_backup": ".sqlite3", "secrets_vault": ".tar.enc"}[fix.kind]
        out["copies"].append({"id": f"pc-{fid}", "kind": kind, "path": f"{store['path']}/parachute-{fid}{suffix}",
                              "domain": store["domain"]})
        out["copy_of"] += [{"copy": f"pc-{fid}", "item": i, "kind": kind} for i in ids]
        if fix.kind == "secrets_vault":
            ring = store_by_id(layout, p["keyring"])
            out["items"].append({"id": _key_id(layout, fix), "cls": "SECRET", "domain": ring["domain"]})
            out["needs"].append({"copy": f"pc-{fid}", "item": _key_id(layout, fix)})
    elif fix.kind == "git_push":
        domain = layout.domain_of(p["remote_repo"])
        for sha in p["tips"]:
            name = f"refs/heads/parachute/rescue-{sha[:8]}"
            rid = f"ref:{p['remote_repo']}:{name}"
            out["refs"].append({"id": rid, "kind": "remote_branch", "name": name, "repo": p["remote_repo"],
                                "domain": domain})
            out["points"].append({"ref": rid, "sha": sha})
    elif fix.kind == "git_bundle":
        store = store_by_id(layout, p["store"])
        bundle = f"{store['path']}/bundles/parachute-{fid}.bundle"
        for i, sha in enumerate(p["tips"]):
            name = f"refs/parachute/{fid}/{i}"
            rid = f"ref:{bundle}:{name}"
            out["refs"].append({"id": rid, "kind": "bundle", "name": name, "repo": bundle, "domain": store["domain"]})
            out["points"].append({"ref": rid, "sha": sha})
    return out


def _write_tar(root: Path, ids, out: Path) -> None:
    with tarfile.open(out, "w") as tf:
        for iid in sorted(ids):
            src = root / iid
            if not src.is_file():
                raise FixFailed(f"{iid} is not a file")
            info = tf.gettarinfo(str(src), arcname=iid)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            with open(src, "rb") as fh:
                tf.addfile(info, fh)


def _check_tar(root: Path, ids, archive: Path) -> None:
    with tarfile.open(archive) as tf:
        for iid in ids:
            member = tf.extractfile(iid)
            if member is None or hashlib.sha256(member.read()).hexdigest() != sha256_file(root / iid):
                raise FixFailed(f"{iid} did not read back from {archive.name} unchanged")


def _openssl(mode: str, src: Path, dst: Path, key: Path) -> None:
    cmd = ["openssl", "enc", "-d" if mode == "dec" else "-salt", "-aes-256-cbc", "-pbkdf2",
           "-in", str(src), "-out", str(dst), "-pass", f"file:{key}"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        raise FixFailed(f"openssl {mode} failed: {r.stderr.decode(errors='replace').strip()}")


def _entry(fid: str, kind: str, out: Path, ids, members: dict, fmt: str, key: str | None = None) -> dict:
    entry = {"id": f"pc-{fid}", "kind": kind, "file": out.name, "encrypted": int(key is not None), "format": fmt,
             "sha256": sha256_file(out), "verified_at": now_iso(), "covers": sorted(ids),
             "needs": [key] if key else [], "members": members}
    if key:
        entry["key"] = key
    return entry


def _run_copy(fix: Fix, layout: Layout) -> dict:
    root, p = Path(layout.root), fix.params
    out = root / store_by_id(layout, p["store"])["path"] / f"parachute-{fix.id}.tar"
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_tar(root, p["items"], out)
    _check_tar(root, p["items"], out)
    append_manifest(out.parent, _entry(fix.id, "backup", out, p["items"], {i: i for i in p["items"]}, "tar"))
    return {"artifact": out.relative_to(root).as_posix(), "items": len(p["items"])}


def _run_vault(fix: Fix, layout: Layout) -> dict:
    root, p = Path(layout.root), fix.params
    key_id = _key_id(layout, fix)
    key = root / key_id
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(secrets.token_hex(32) + "\n")
    os.chmod(key, 0o600)
    out = root / store_by_id(layout, p["store"])["path"] / f"parachute-{fix.id}.tar.enc"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root / ".parachute") as tmp:
        plain, check = Path(tmp) / "secrets.tar", Path(tmp) / "check.tar"
        _write_tar(root, p["items"], plain)
        _openssl("enc", plain, out, key)
        _openssl("dec", out, check, key)
        _check_tar(root, p["items"], check)
    append_manifest(out.parent, _entry(fix.id, "backup", out, p["items"], {i: i for i in p["items"]}, "tar", key_id))
    return {"artifact": out.relative_to(root).as_posix(), "key": key_id, "items": len(p["items"])}


def _run_sqlite(fix: Fix, layout: Layout) -> dict:
    root, p = Path(layout.root), fix.params
    src_path = root / p["item"]
    out = root / store_by_id(layout, p["store"])["path"] / f"parachute-{fix.id}.sqlite3"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    src = sqlite3.connect(src_path.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    if sqlite_logical_hash(out) != sqlite_logical_hash(src_path):
        raise FixFailed(f"{out.name} does not match {p['item']}")
    append_manifest(out.parent, _entry(fix.id, "snapshot", out, [p["item"]], {p["item"]: ""}, "file"))
    return {"artifact": out.relative_to(root).as_posix()}


def _run_push(fix: Fix, layout: Layout) -> dict:
    root, p = Path(layout.root), fix.params
    pushed = []
    for sha in p["tips"]:
        ref = f"refs/heads/parachute/rescue-{sha[:8]}"
        git(["push", p["remote"], f"{sha}:{ref}"], cwd=root / p["repo"])
        if git(["rev-parse", "--verify", "-q", ref], git_dir=root / p["remote_repo"]).strip() != sha:
            raise FixFailed(f"{ref} on the remote does not point at {sha}")
        pushed.append(ref)
    return {"remote": p["remote_repo"], "refs": pushed}


def _run_bundle(fix: Fix, layout: Layout) -> dict:
    root, p = Path(layout.root), fix.params
    repo = root / p["repo"]
    out = root / store_by_id(layout, p["store"])["path"] / "bundles" / f"parachute-{fix.id}.bundle"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    refs = [f"refs/parachute/{fix.id}/{i}" for i in range(len(p["tips"]))]
    for ref, sha in zip(refs, p["tips"]):
        git(["update-ref", ref, sha], cwd=repo)
    try:
        git(["bundle", "create", str(out), *refs], cwd=repo)
        git(["bundle", "verify", str(out)], cwd=repo)
    finally:
        for ref in refs:
            git(["update-ref", "-d", ref], cwd=repo, check=False)
    listed = {line.split(" ")[0] for line in git(["bundle", "list-heads", str(out)]).splitlines()}
    if listed != set(p["tips"]):
        raise FixFailed(f"{out.name} does not hold the expected commits")
    return {"artifact": out.relative_to(root).as_posix(), "refs": refs}


RUNNERS = {"copy_backup": _run_copy, "sqlite_backup": _run_sqlite, "secrets_vault": _run_vault,
           "git_push": _run_push, "git_bundle": _run_bundle}


def run_fix(fix: Fix, layout: Layout) -> dict:
    try:
        return RUNNERS[fix.kind](fix, layout)
    except FixFailed:
        raise
    except (OSError, RuntimeError, tarfile.TarError, sqlite3.Error) as e:
        raise FixFailed(f"{fix.kind} {fix.id}: {e}") from e
