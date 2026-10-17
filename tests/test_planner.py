import pytest

from parachute.engine.fixes import Fix, effects, run_fix
from parachute.engine.layout import Layout
from parachute.engine.planner import plan
from parachute.engine.resolver import resolve
from parachute.engine.whatif import whatif
from parachute.scan import scan_world
from parachute.world.generator import generate_world

P = "disk_a/projects"


@pytest.fixture(scope="module")
def layout(sandbox):
    return Layout.load(sandbox.root)


@pytest.fixture
def planned(graph, layout, sandbox):
    def _planned(command, sub=f"{P}/webshop"):
        res = resolve(command, str(sandbox.root / sub), layout)
        assert res.forced_verdict is None, res.reasons
        base = whatif(graph, res.destroyed, command)
        return plan(graph, layout, res.destroyed, base, command)
    return _planned


def kinds(result):
    return sorted(f["kind"] for f in result["fixes"])


def test_rebuildable_output_needs_no_fix(planned):
    result = planned("rm -rf vendor")
    assert result["needed"] == 0 and result["fixes"] == [] and result["verified"]


def test_analytics_folder_needs_three_kinds_of_fix(planned):
    result = planned("rm -rf analytics", sub=P)
    assert kinds(result) == ["copy_backup", "secrets_vault", "sqlite_backup"]
    assert result["verified"] and result["unfixable"] == []


def test_worktree_removal_is_fixed_by_one_push(planned, sandbox):
    result = planned("git worktree remove --force ../webshop-exp")
    assert kinds(result) == ["git_push"] and result["verified"]
    assert result["fixes"][0]["params"]["tips"] == [sandbox.traps["T06_WORKTREE_ONLY_COMMITS"]["commits"][-1]]


def test_stash_drop_is_fixed_by_pushing_the_stash_commit(planned, sandbox):
    result = planned("git stash drop")
    assert kinds(result) == ["git_push"] and result["verified"]
    assert result["fixes"][0]["params"]["tips"] == sandbox.traps["T05_STASH"]["commits"]


def test_branch_delete_is_fixed_by_pushing_the_branch_tip(planned, sandbox):
    result = planned("git branch -D feature/cart")
    assert kinds(result) == ["git_push"] and result["verified"]
    assert result["fixes"][0]["params"]["tips"] == [sandbox.traps["T04_UNPUSHED_BRANCH"]["commits"][-1]]


def test_no_destination_means_no_plan(planned, sandbox):
    result = planned("rm -rf disk_b/offsite disk_a/projects/analytics", sub="")
    assert not result["verified"] and result["fixes"] == []
    assert {"kind": "item", "id": f"{P}/analytics/data/dev.sqlite3"} in result["unfixable"]


def test_real_fixes_create_exactly_what_the_effects_predict(tmp_path):
    sb = generate_world(tmp_path / "box")
    layout = Layout.load(sb.root)
    fixes = [
        Fix("copy_backup", {"store": "offsite", "items": [f"{P}/analytics/README.md",
                                                         f"{P}/analytics/scratch/blob.bin"]}, 0),
        Fix("sqlite_backup", {"store": "offsite", "item": f"{P}/analytics/data/dev.sqlite3"}, 0),
        Fix("secrets_vault", {"store": "offsite", "keyring": "keyring",
                              "items": [f"{P}/webshop/.env", f"{P}/analytics/keys/backup.key"]}, 0),
        Fix("git_push", {"repo": f"{P}/webshop", "remote": "origin", "remote_repo": "disk_b/remote/webshop.git",
                         "tips": [sb.traps["T04_UNPUSHED_BRANCH"]["commits"][-1]]}, 0),
        Fix("git_bundle", {"repo": f"{P}/webshop", "store": "offsite",
                           "tips": [sb.traps["T05_STASH"]["commits"][0]]}, 0),
    ]
    for fix in fixes:
        run_fix(fix, layout)
    world = scan_world(sb.root)
    copies = {c["id"]: c for c in world["copies"]}
    copy_of = {(c["copy"], c["item"]) for c in world["copy_of"]}
    needs = {(n["copy"], n["item"]) for n in world["needs"]}
    items = {i["id"]: i for i in world["items"]}
    refs = {r["id"]: r for r in world["refs"]}
    points = {(p["ref"], p["sha"]) for p in world["points_to"]}
    for fix in fixes:
        predicted = effects(fix, layout)
        for c in predicted["copies"]:
            got = copies[c["id"]]
            assert (got["alive"], got["path"], got["domain"], got["kind"]) == (1, c["path"], c["domain"], c["kind"])
        assert {(c["copy"], c["item"]) for c in predicted["copy_of"]} <= copy_of
        assert {(n["copy"], n["item"]) for n in predicted["needs"]} <= needs
        for i in predicted["items"]:
            assert (items[i["id"]]["cls"], items[i["id"]]["domain"]) == (i["cls"], i["domain"])
        for r in predicted["refs"]:
            assert (refs[r["id"]]["kind"], refs[r["id"]]["domain"]) == (r["kind"], r["domain"])
        assert {(p["ref"], p["sha"]) for p in predicted["points"]} <= points
