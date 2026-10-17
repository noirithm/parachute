"""Messy-devbox generator (all data synthetic, all local, no network).

Two SIMULATED failure domains share one real filesystem under one throwaway root:
  disk-a = the dev laptop disk  (disk_a/projects, disk_a/backups)
  disk-b = another host/account (disk_b/remote bare repos, disk_b/offsite backups)
Traps are recorded in <root>/.parachute/traps.json so tests can reference them by id.
Git identity and dates are fixed, so commit ids are stable between runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..guard import assert_safe_root

EPOCH0 = 1_760_000_000
P = "disk_a/projects"
VERIFIED = "2026-10-10T08:00:00Z"

INSTALL_DEPS = '''"""Offline deterministic 'installer': materialises vendor/ from deps.lock (no network)."""
import hashlib
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
lines = sorted(l.strip() for l in (root / "deps.lock").read_text().splitlines() if l.strip())
for line in lines:
    name, version = line.split("==")
    pkg = root / "vendor" / name
    (pkg / "lib").mkdir(parents=True, exist_ok=True)
    (pkg / "package.json").write_text('{"name": "%s", "version": "%s"}\\n' % (name, version))
    for i in range(4):
        digest = hashlib.sha256(("%s:%d" % (line, i)).encode()).hexdigest()
        (pkg / "lib" / ("part%d.js" % i)).write_text("// %s@%s\\nexports.digest = '%s';\\n" % (name, version, digest))
'''

BUILD_PY = '''"""Deterministic build: bundles src/*.py into dist/ (no network)."""
import hashlib
import pathlib

root = pathlib.Path(__file__).resolve().parent
out = root / "dist"
out.mkdir(exist_ok=True)
bundle = "\\n".join("### %s\\n%s" % (p.name, p.read_text()) for p in sorted((root / "src").glob("*.py")))
(out / "bundle.txt").write_text(bundle)
(out / "manifest.json").write_text('{"sha256": "%s"}\\n' % hashlib.sha256(bundle.encode()).hexdigest())
'''


@dataclass
class Sandbox:
    root: Path
    session_id: str
    traps: dict

    @property
    def graph_name(self) -> str:
        return f"parachute_{self.session_id}"


class _Git:
    """Deterministic git runner: fixed identity, monotonically increasing fixed dates."""

    def __init__(self) -> None:
        self.tick = 0

    def run(self, cwd, *args) -> str:
        self.tick += 60
        stamp = f"{EPOCH0 + self.tick} +0000"
        env = dict(os.environ, GIT_AUTHOR_NAME="Dev Fake", GIT_AUTHOR_EMAIL="dev@example.invalid",
                   GIT_COMMITTER_NAME="Dev Fake", GIT_COMMITTER_EMAIL="dev@example.invalid",
                   GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp, GIT_TERMINAL_PROMPT="0",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        cmd = ["git", "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always", *args]
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {r.stderr.strip()}")
        return r.stdout.strip()


def _w(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tar(src_dir: Path, arc_root: str, out: Path) -> None:
    with tarfile.open(out, "w") as tf:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                ti = tf.gettarinfo(str(f), arcname=f"{arc_root}/{f.relative_to(src_dir).as_posix()}")
                ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
                with open(f, "rb") as fh:
                    tf.addfile(ti, fh)


def _run_recipe(cwd: Path, script: str) -> None:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, script], cwd=cwd, env=env, check=True)


def _webshop(g: _Git, projects: Path, remote: Path) -> dict:
    ws = projects / "webshop"
    exp = projects / "webshop-exp"
    _w(ws / "README.md", "# webshop\n\nSynthetic demo storefront.\n")
    _w(ws / ".gitignore", ".env\nvendor/\ndist/\n")
    _w(ws / ".env.example", "DATABASE_URL=\nSTRIPE_API_KEY=\n")
    _w(ws / "deps.lock", "fake-ui==2.1.0\nleft-pad==1.3.0\ntiny-http==0.9.2\n")
    _w(ws / "tools" / "install_deps.py", INSTALL_DEPS)
    _w(ws / "build.py", BUILD_PY)
    _w(ws / "src" / "app.py", "def main():\n    return 'webshop up'\n")
    _w(ws / "src" / "catalog.py", "ITEMS = ['mug', 'shirt', 'sticker']\n")
    _w(ws / "src" / "cart.py", "def total(prices):\n    return sum(prices)\n")
    _w(ws / "docs" / "handbook.md", "# Handbook\n\nHow we ship the webshop.\n")
    g.run(ws, "init", "-b", "main")
    g.run(ws, "add", "-A")
    g.run(ws, "commit", "-m", "feat: initial storefront")
    _w(ws / "src" / "checkout.py", "def pay(total):\n    return {'paid': total}\n")
    g.run(ws, "add", "-A")
    g.run(ws, "commit", "-m", "feat: checkout skeleton")

    # origin: a bare repo in the OTHER domain (disk-b); main is fully pushed
    bare = remote / "webshop.git"
    g.run(remote, "init", "--bare", "-b", "main", str(bare))
    g.run(ws, "remote", "add", "origin", str(bare))
    g.run(ws, "push", "origin", "main")

    # local-only branch with 2 unpushed commits
    g.run(ws, "checkout", "-b", "feature/cart")
    _w(ws / "src" / "cart.py", "def total(prices, discount=0):\n    return sum(prices) - discount\n")
    g.run(ws, "add", "-A")
    g.run(ws, "commit", "-m", "feat(cart): discounts")
    _w(ws / "src" / "coupons.py", "COUPONS = {'WELCOME': 5}\n")
    g.run(ws, "add", "-A")
    g.run(ws, "commit", "-m", "feat(cart): coupon codes")
    cart = [g.run(ws, "rev-parse", "HEAD~1"), g.run(ws, "rev-parse", "HEAD")]
    g.run(ws, "checkout", "main")

    # detached worktree holding the ONLY reference to 2 commits
    g.run(ws, "worktree", "add", "--detach", str(exp), "main")
    _w(exp / "src" / "experiment.py", "WEIGHTS = {'price': 0.7}\n")
    _w(exp / "src" / "catalog.py", "ITEMS = ['mug', 'shirt', 'sticker', 'poster']\n")
    g.run(exp, "add", "-A")
    g.run(exp, "commit", "-m", "exp: new ranking")
    _w(exp / "src" / "experiment.py", "WEIGHTS = {'price': 0.6, 'rating': 0.4}\n")
    g.run(exp, "add", "-A")
    g.run(exp, "commit", "-m", "exp: tune weights")
    exp_commits = [g.run(exp, "rev-parse", "HEAD~1"), g.run(exp, "rev-parse", "HEAD")]

    # a stash (working tree is clean again afterwards)
    existing = (ws / "src" / "checkout.py").read_text()
    _w(ws / "src" / "checkout.py", existing + "# wip: payment retries\n")
    g.run(ws, "stash", "push", "-m", "wip: payment retries")
    stash = g.run(ws, "rev-parse", "refs/stash")

    # build outputs created by running the REAL recipes (so they are truly rebuildable)
    _run_recipe(ws, "tools/install_deps.py")
    _run_recipe(ws, "build.py")
    # gitignored secret, no backup anywhere
    _w(ws / ".env", "DATABASE_URL=sqlite:///dev.db\nSTRIPE_API_KEY=pk_fake_0000000000000000\n"
                    "SESSION_SECRET=fake-session-secret-0001\n")

    return {
        "T01_REBUILDABLE_DEPS": {
            "description": "vendor/ is GENERATED from deps.lock by a local deterministic recipe",
            "item_prefix": f"{P}/webshop/vendor/"},
        "T02_REBUILDABLE_BUILD": {
            "description": "dist/ is GENERATED by build.py",
            "item_prefix": f"{P}/webshop/dist/"},
        "T04_UNPUSHED_BRANCH": {
            "description": "feature/cart has 2 commits that exist nowhere else",
            "ref": f"ref:{P}/webshop:refs/heads/feature/cart", "commits": cart},
        "T05_STASH": {
            "description": "a stash entry is the only holder of its commit",
            "ref": f"ref:{P}/webshop:stash@{{0}}", "commits": [stash]},
        "T06_WORKTREE_ONLY_COMMITS": {
            "description": "a detached worktree HEAD is the only reference to 2 commits",
            "ref": f"ref:{P}/webshop:worktree:{P}/webshop-exp", "commits": exp_commits,
            "items": [f"{P}/webshop-exp/src/experiment.py", f"{P}/webshop-exp/src/catalog.py"]},
    }


def _analytics(root: Path, projects: Path, backups: Path, offsite: Path) -> dict:
    an = projects / "analytics"
    rng = random.Random(11)
    _w(an / "README.md", "# analytics\n\nSynthetic analytics sandbox (not a git repo).\n")
    _w(an / "docs" / "runbook.md", "# Runbook\n\n1. Export reports\n2. Email finance\n")
    _w(an / "notebooks" / "forecast.py", "def forecast(xs):\n    return sum(xs) / len(xs)\n")
    _w(an / "notebooks" / "eda.py", "def describe(xs):\n    return min(xs), max(xs)\n")
    for q in ("q3", "q4"):
        rows = "\n".join(f"{i},{rng.randint(1, 40)},{round(rng.random() * 100, 2)}" for i in range(50))
        _w(an / "reports" / f"{q}.csv", "id,user,amount\n" + rows + "\n")
    _w(an / ".env", "WAREHOUSE_PASSWORD=fake-warehouse-password-0002\nAPI_TOKEN=fake-token-0000000000\n")
    _w(an / "scratch" / "blob.bin", rng.randbytes(2048))
    key = an / "keys" / "backup.key"
    _w(key, "".join(rng.choice("0123456789abcdef") for _ in range(64)) + "\n")

    # SQLite dev database with no backup
    db = an / "data" / "dev.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, user TEXT, amount REAL)")
    con.executemany("INSERT INTO events (user, amount) VALUES (?, ?)",
                    [(f"user{rng.randint(1, 40)}", round(rng.random() * 100, 2)) for _ in range(300)])
    con.commit()
    con.close()

    # backup of reports/ on the SAME disk as the originals (phantom)
    rep_tar = backups / "reports-2026-10.tar"
    _tar(an / "reports", "reports", rep_tar)
    _w(backups / "MANIFEST.json", json.dumps({"copies": [{
        "id": "bk-reports-local", "kind": "backup", "file": rep_tar.name, "encrypted": 0, "format": "tar",
        "members": {f"{P}/analytics/reports/q3.csv": "reports/q3.csv",
                    f"{P}/analytics/reports/q4.csv": "reports/q4.csv"},
        "sha256": _sha256(rep_tar), "verified_at": VERIFIED,
        "covers": [f"{P}/analytics/reports/q3.csv", f"{P}/analytics/reports/q4.csv"], "needs": []}]}, indent=2))

    # encrypted backup in the OTHER domain whose only key copy lives inside analytics/ (the folder at risk)
    tmp_tar = root / ".parachute" / "notebooks.tar"
    _tar(an / "notebooks", "notebooks", tmp_tar)
    enc = offsite / "notebooks.tar.enc"
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-in", str(tmp_tar),
                    "-out", str(enc), "-pass", f"file:{key}"], check=True, capture_output=True)
    tmp_tar.unlink()

    # healthy control: plain copy in the other domain, no key needed
    shutil.copy2(an / "docs" / "runbook.md", offsite / "runbook.md")
    _w(offsite / "MANIFEST.json", json.dumps({"copies": [
        {"id": "bk-notebooks-enc", "kind": "backup", "file": enc.name, "encrypted": 1, "format": "tar",
         "key": f"{P}/analytics/keys/backup.key",
         "members": {f"{P}/analytics/notebooks/forecast.py": "notebooks/forecast.py",
                     f"{P}/analytics/notebooks/eda.py": "notebooks/eda.py"},
         "sha256": _sha256(enc), "verified_at": VERIFIED,
         "covers": [f"{P}/analytics/notebooks/forecast.py", f"{P}/analytics/notebooks/eda.py"],
         "needs": [f"{P}/analytics/keys/backup.key"]},
        {"id": "bk-runbook", "kind": "backup", "file": "runbook.md", "encrypted": 0, "format": "file",
         "members": {f"{P}/analytics/docs/runbook.md": ""},
         "sha256": _sha256(offsite / "runbook.md"), "verified_at": VERIFIED,
         "covers": [f"{P}/analytics/docs/runbook.md"], "needs": []}]}, indent=2))

    return {
        "T07_SQLITE_NO_BACKUP": {"description": "dev SQLite DB with no copy anywhere",
                                 "items": [f"{P}/analytics/data/dev.sqlite3"]},
        "T08_SAME_DISK_BACKUP": {"description": "backup of reports/ shares the failure domain of its original",
                                 "copy": "bk-reports-local",
                                 "items": [f"{P}/analytics/reports/q3.csv", f"{P}/analytics/reports/q4.csv"]},
        "T09_KEY_IN_DELETED_FOLDER": {"description": "encrypted offsite backup; its only key is inside analytics/",
                                      "copy": "bk-notebooks-enc", "key": f"{P}/analytics/keys/backup.key",
                                      "items": [f"{P}/analytics/notebooks/forecast.py",
                                                f"{P}/analytics/notebooks/eda.py"]},
        "T10_HEALTHY_BACKUP": {"description": "control: independent-domain plain copy (must NOT be phantom)",
                               "copy": "bk-runbook", "items": [f"{P}/analytics/docs/runbook.md"]},
    }


def _notes_cli(g: _Git, projects: Path, remote: Path) -> None:
    nc = projects / "notes-cli"
    _w(nc / "README.md", "# notes-cli\n\nTiny, boring, fully pushed.\n")
    _w(nc / "notes.py", "def add(notes, text):\n    return notes + [text]\n")
    g.run(nc, "init", "-b", "main")
    g.run(nc, "add", "-A")
    g.run(nc, "commit", "-m", "feat: add notes")
    _w(nc / "notes.py", "def add(notes, text):\n    return notes + [text.strip()]\n")
    g.run(nc, "add", "-A")
    g.run(nc, "commit", "-m", "fix: strip input")
    bare = remote / "notes-cli.git"
    g.run(remote, "init", "--bare", "-b", "main", str(bare))
    g.run(nc, "remote", "add", "origin", str(bare))
    g.run(nc, "push", "origin", "main")


def generate_world(root=None) -> Sandbox:
    for tool in ("git", "openssl"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"'{tool}' not found; on Arch: sudo pacman -S --needed git openssl")
    root = Path(root) if root else Path(tempfile.mkdtemp(prefix="parachute-"))
    root.mkdir(parents=True, exist_ok=True)
    root = assert_safe_root(root)
    if any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty; refusing to generate a sandbox into it")
    sid = "s" + secrets.token_hex(4)
    meta = root / ".parachute"
    meta.mkdir()
    projects, backups = root / "disk_a" / "projects", root / "disk_a" / "backups"
    remote, offsite = root / "disk_b" / "remote", root / "disk_b" / "offsite"
    for d in (projects, backups, remote, offsite, root / "disk_c" / "keyring"):
        d.mkdir(parents=True)

    g = _Git()
    traps = _webshop(g, projects, remote)
    traps.update(_analytics(root, projects, backups, offsite))
    _notes_cli(g, projects, remote)
    traps["T03_GITIGNORED_SECRET"] = {
        "description": ".env files with secrets: gitignored, no backup anywhere",
        "items": [f"{P}/webshop/.env", f"{P}/analytics/.env"]}

    (meta / "world.json").write_text(json.dumps({
        "session": sid,
        "domains": [{"id": "disk-a", "label": "dev laptop disk", "kind": "disk"},
                    {"id": "disk-b", "label": "other host / account", "kind": "host"},
                    {"id": "disk-c", "label": "offline keyring", "kind": "account"}],
        "stores": [{"id": "projects", "path": "disk_a/projects", "kind": "tree", "domain": "disk-a"},
                   {"id": "backups-local", "path": "disk_a/backups", "kind": "backup", "domain": "disk-a"},
                   {"id": "origin", "path": "disk_b/remote", "kind": "remote", "domain": "disk-b"},
                   {"id": "offsite", "path": "disk_b/offsite", "kind": "backup", "domain": "disk-b"},
                   {"id": "keyring", "path": "disk_c/keyring", "kind": "tree", "domain": "disk-c"}],
    }, indent=2))
    (meta / "traps.json").write_text(json.dumps(traps, indent=2))
    return Sandbox(root=root, session_id=sid, traps=traps)
