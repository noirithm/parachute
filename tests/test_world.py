"""Pure tests: generator + scanners (no FalkorDB needed)."""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from parachute.graphdb import GraphUnavailable, connect, hops
from parachute.guard import OutsideSandbox, assert_safe_root, require_inside
from parachute.scan import scan_world

P = "disk_a/projects"
EXPECTED_TRAPS = {"T01_REBUILDABLE_DEPS", "T02_REBUILDABLE_BUILD", "T03_GITIGNORED_SECRET",
                  "T04_UNPUSHED_BRANCH", "T05_STASH", "T06_WORKTREE_ONLY_COMMITS", "T07_SQLITE_NO_BACKUP",
                  "T08_SAME_DISK_BACKUP", "T09_KEY_IN_DELETED_FOLDER", "T10_HEALTHY_BACKUP"}


def item(world, iid):
    return next(i for i in world["items"] if i["id"] == iid)


def tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode())
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def test_generator_plants_every_trap(sandbox):
    assert set(sandbox.traps) == EXPECTED_TRAPS
    for t in sandbox.traps.values():
        for iid in t.get("items", []):
            assert (sandbox.root / iid).is_file(), iid
    assert (sandbox.root / "disk_b/remote/webshop.git").is_dir()


def test_classification(world):
    assert item(world, f"{P}/webshop/.env")["cls"] == "SECRET"
    assert item(world, f"{P}/analytics/.env")["cls"] == "SECRET"
    assert item(world, f"{P}/analytics/keys/backup.key")["cls"] == "SECRET"
    assert item(world, f"{P}/webshop/.env.example")["cls"] == "SOURCE"
    assert item(world, f"{P}/webshop/src/app.py")["cls"] == "SOURCE"
    assert item(world, f"{P}/analytics/data/dev.sqlite3")["cls"] == "DATA"
    assert item(world, f"{P}/analytics/scratch/blob.bin")["cls"] == "UNKNOWN"
    gen = [i for i in world["items"] if i["id"].startswith((f"{P}/webshop/vendor/", f"{P}/webshop/dist/"))]
    assert gen and all(i["cls"] == "GENERATED" and i["recipe"] for i in gen)


def test_recipes_are_deterministic(sandbox, tmp_path):
    ws = sandbox.root / P / "webshop"
    for out_dir, script in (("vendor", "tools/install_deps.py"), ("dist", "build.py")):
        want = tree_hash(ws / out_dir)
        for n in range(2):
            copy = tmp_path / f"{out_dir}{n}"
            shutil.copytree(ws, copy, ignore=shutil.ignore_patterns(".git", "vendor", "dist"))
            subprocess.run([sys.executable, script], cwd=copy, check=True)
            assert tree_hash(copy / out_dir) == want


def test_git_scanner_sees_traps(world, sandbox):
    refs = {r["id"]: r for r in world["refs"]}
    for tid in ("T04_UNPUSHED_BRANCH", "T05_STASH", "T06_WORKTREE_ONLY_COMMITS"):
        t = sandbox.traps[tid]
        assert t["ref"] in refs, t["ref"]
        for sha in t["commits"]:
            assert any(c["sha"] == sha for c in world["commits"])
    assert any(r["kind"] == "remote_branch" for r in world["refs"])


def test_backup_manifests(world):
    copies = {c["id"]: c for c in world["copies"]}
    assert {"bk-reports-local", "bk-notebooks-enc", "bk-runbook"} <= set(copies)
    assert all(c["alive"] == 1 for c in copies.values())
    assert copies["bk-notebooks-enc"]["encrypted"] == 1


def test_guard(tmp_path):
    for bad in ("/", str(Path.home())):
        with pytest.raises(OutsideSandbox):
            assert_safe_root(bad)
    with pytest.raises(OutsideSandbox):
        require_inside(tmp_path, "/etc/passwd")
    assert require_inside(tmp_path, "/etc/passwd", allow_outside=True) == Path("/etc/passwd").resolve()
    with pytest.raises(OutsideSandbox):
        scan_world(tmp_path)  # not a Parachute sandbox


def test_hops_validation():
    assert hops(5) == "1..5"
    for bad in (0, -1, "5; MATCH (n) DELETE n", 10**9, True, 2.5):
        with pytest.raises(ValueError):
            hops(bad)


def test_graph_unreachable_fails_closed():
    with pytest.raises(GraphUnavailable):
        connect(port=1)


def test_worktree_reflog_is_owned_by_its_worktree(world):
    reflogs = [r for r in world["refs"] if r["kind"] == "reflog"]
    assert not any(r["name"].startswith("worktrees/") for r in reflogs)
    assert any(r["id"].startswith(f"ref:{P}/webshop:reflog:{P}/webshop-exp:") for r in reflogs)
