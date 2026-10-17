import pytest

from parachute.engine.layout import Layout
from parachute.engine.whatif import analyze
from parachute.loader import graph_stats
from parachute.scan.gitscan import git

P = "disk_a/projects"


@pytest.fixture(scope="module")
def layout(sandbox):
    return Layout.load(sandbox.root)


@pytest.fixture
def check(graph, layout, sandbox):
    def _check(command, sub=f"{P}/webshop"):
        return analyze(graph, layout, command, str(sandbox.root / sub))
    return _check


def ids(entries):
    return {e["id"] for e in entries}


def shas(entries):
    return {e["sha"] for e in entries}


def outcome(result):
    return result["whatif"]["outcome"]


def test_rebuildable_deps_are_safe(check, sandbox, world):
    out = outcome(check("rm -rf vendor"))
    prefix = sandbox.traps["T01_REBUILDABLE_DEPS"]["item_prefix"]
    want = {i["id"] for i in world["items"] if i["id"].startswith(prefix)}
    assert out["lost"] == [] and out["fragile"] == []
    assert ids(out["recoverable"]) == want
    assert {e["strength"] for e in out["recoverable"]} == {"DURABLE"}
    assert all(e["via"].startswith("recipe:") for e in out["recoverable"])


def test_rebuildable_build_output_is_safe(check, sandbox, world):
    out = outcome(check("rm -rf dist"))
    prefix = sandbox.traps["T02_REBUILDABLE_BUILD"]["item_prefix"]
    want = {i["id"] for i in world["items"] if i["id"].startswith(prefix)}
    assert out["lost"] == [] and out["fragile"] == []
    assert ids(out["recoverable"]) == want


def test_gitignored_secret_is_lost(check):
    out = outcome(check("rm .env"))
    assert ids(out["lost"]) == {f"{P}/webshop/.env"}


def test_sqlite_without_backup_is_lost(check, sandbox):
    out = outcome(check("rm data/dev.sqlite3", sub=f"{P}/analytics"))
    assert ids(out["lost"]) == set(sandbox.traps["T07_SQLITE_NO_BACKUP"]["items"])


def test_same_disk_backup_is_only_fragile(check, sandbox):
    out = outcome(check("rm -rf reports", sub=f"{P}/analytics"))
    assert out["lost"] == []
    assert ids(out["fragile"]) == set(sandbox.traps["T08_SAME_DISK_BACKUP"]["items"])
    assert {(e["strength"], e["via"]) for e in out["fragile"]} == {("WEAK", "copy:bk-reports-local")}
    path = out["justifications"]["copy:bk-reports-local"]["path"]
    assert any("failure domain disk-a" in line for line in path)


def test_encrypted_backup_survives_while_key_survives(check, sandbox):
    out = outcome(check("rm -rf notebooks", sub=f"{P}/analytics"))
    trap = sandbox.traps["T09_KEY_IN_DELETED_FOLDER"]
    assert out["lost"] == [] and out["fragile"] == []
    assert ids(out["recoverable"]) == set(trap["items"])
    assert {(e["strength"], e["via"]) for e in out["recoverable"]} == {("DURABLE", "copy:bk-notebooks-enc")}
    path = out["justifications"]["copy:bk-notebooks-enc"]["path"]
    assert any(trap["key"] in line and "DURABLE" in line for line in path)


def test_encrypted_backup_dies_with_its_key(check, sandbox):
    out = outcome(check("rm -rf keys notebooks", sub=f"{P}/analytics"))
    trap = sandbox.traps["T09_KEY_IN_DELETED_FOLDER"]
    assert ids(out["lost"]) == set(trap["items"]) | {trap["key"]}
    assert out["fragile"] == [] and out["recoverable"] == []


def test_unpushed_branch_delete_leaves_only_reflog(check, sandbox):
    out = outcome(check("git branch -D feature/cart"))
    assert out["lost_commits"] == []
    assert shas(out["fragile_commits"]) == set(sandbox.traps["T04_UNPUSHED_BRANCH"]["commits"])
    assert {c["via"] for c in out["fragile_commits"]} == {"reflog"}


def test_stash_drop_loses_the_stash_commit(check, sandbox):
    out = outcome(check("git stash drop"))
    assert sandbox.traps["T05_STASH"]["commits"][0] in shas(out["lost_commits"])
    assert out["fragile_commits"] == []


def test_worktree_remove_loses_worktree_only_commits(check, sandbox):
    out = outcome(check("git worktree remove --force ../webshop-exp"))
    trap = sandbox.traps["T06_WORKTREE_ONLY_COMMITS"]
    assert ids(out["lost"]) == set(trap["items"])
    assert shas(out["lost_commits"]) == set(trap["commits"])
    assert out["fragile"] == [] and out["fragile_commits"] == []
    assert {(e["strength"], e["via"]) for e in out["recoverable"]} == {("DURABLE", "git:remote_branch")}
    assert out["destroyed_items"] == 12


def test_force_push_demotes_pushed_commit_to_local_only(check, sandbox):
    tip = git(["rev-parse", "HEAD"], cwd=sandbox.root / P / "webshop").strip()
    out = outcome(check("git push --force origin HEAD~1:main"))
    assert out["lost_commits"] == []
    assert shas(out["fragile_commits"]) == {tip}
    assert {c["via"] for c in out["fragile_commits"]} == {"local_ref"}


def test_reset_hard_is_recoverable_from_remote(check):
    out = outcome(check("git reset --hard HEAD~1"))
    assert out["lost"] == [] and out["fragile"] == []
    entry = next(e for e in out["recoverable"] if e["id"] == f"{P}/webshop/src/checkout.py")
    assert (entry["strength"], entry["via"]) == ("DURABLE", "git:remote_branch")


def test_empty_variable_never_reaches_the_graph(check):
    for command in ('rm -rf "$BUILD_DIR"/', "rm -rf ~", "rm -rf $(pwd)"):
        res = check(command)
        assert res["resolution"]["forced_verdict"] == "STOP_AND_ASK", command
        assert res["whatif"] is None, command


def test_whatif_leaves_live_graph_and_no_forks_behind(check, graph):
    before = graph_stats(graph)
    check("rm -rf vendor")
    check("git worktree remove --force ../webshop-exp")
    check("git push --force origin HEAD~1:main")
    assert graph_stats(graph) == before
    assert not [n for n in graph.db.list_graphs() if n.startswith(graph.name + "_wi_")]
