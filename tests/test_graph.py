"""Tests that need FalkorDB (make up)."""
from parachute.loader import graph_stats, load_world
from parachute.queries import world_summary

P = "disk_a/projects"


def test_loader_is_idempotent(graph, world):
    before = graph_stats(graph)
    assert before["nodes"] > 0 and before["edges"] > 0
    load_world(graph, world)
    load_world(graph, world)
    assert graph_stats(graph) == before


def test_t01_t02_rebuildable_are_derived(graph, sandbox, world):
    for tid in ("T01_REBUILDABLE_DEPS", "T02_REBUILDABLE_BUILD"):
        prefix = sandbox.traps[tid]["item_prefix"]
        want = sum(1 for i in world["items"] if i["id"].startswith(prefix))
        got = graph.scalar("MATCH (i:Item)-[:DERIVED_BY]->(:Recipe) WHERE i.id STARTS WITH $p RETURN count(i)",
                           {"p": prefix})
        assert want > 0 and got == want


def test_t03_secrets_have_no_copy(graph, sandbox):
    for iid in sandbox.traps["T03_GITIGNORED_SECRET"]["items"]:
        assert graph.scalar("MATCH (i:Item {id: $id}) RETURN i.class", {"id": iid}) == "SECRET"
        assert graph.scalar("MATCH (c:Copy)-[:COPY_OF]->(i:Item {id: $id}) RETURN count(c)", {"id": iid}) == 0


def _points_to(graph, ref_id):
    return [r[0] for r in graph.rows("MATCH (r:Ref {id: $id})-[:POINTS_TO]->(c:Commit) RETURN c.sha", {"id": ref_id})]


def test_t04_t05_t06_refs_and_unpushed(graph, sandbox):
    summary = world_summary(graph)
    unpushed = {c["sha"] for c in summary["unpushed_commits"]}
    for tid in ("T04_UNPUSHED_BRANCH", "T05_STASH", "T06_WORKTREE_ONLY_COMMITS"):
        t = sandbox.traps[tid]
        assert _points_to(graph, t["ref"]) == [t["commits"][-1] if tid != "T05_STASH" else t["commits"][0]]
        assert set(t["commits"]) <= unpushed, tid
    tip = sandbox.traps["T06_WORKTREE_ONLY_COMMITS"]["commits"][-1]
    kinds = {r[0] for r in graph.rows("MATCH (r:Ref)-[:POINTS_TO]->(c:Commit {sha: $s}) RETURN r.kind", {"s": tip})}
    assert "worktree_head" in kinds
    assert not (kinds & {"branch", "tag", "remote_branch"}), "worktree HEAD must be the ONLY strong holder"


def test_t07_sqlite_has_no_route(graph, sandbox):
    iid = sandbox.traps["T07_SQLITE_NO_BACKUP"]["items"][0]
    assert iid in world_summary(graph)["items_no_durable_route"]["items"]


def test_phantom_backups_found(graph, sandbox):
    s = world_summary(graph)
    by_copy = {(p["copy"], p["reason"]): p for p in s["phantom_backups"]}
    assert ("bk-reports-local", "same_domain") in by_copy
    key = sandbox.traps["T09_KEY_IN_DELETED_FOLDER"]["key"]
    assert by_copy[("bk-notebooks-enc", "key_without_independent_copy")]["key"] == key
    assert not any(p["copy"] == "bk-runbook" for p in s["phantom_backups"])  # healthy control


def test_summary_routes(graph, sandbox):
    s = world_summary(graph)
    no_route = set(s["items_no_durable_route"]["items"])
    must_be_exposed = {f"{P}/webshop/.env", f"{P}/analytics/.env", f"{P}/analytics/keys/backup.key",
                       f"{P}/analytics/data/dev.sqlite3", f"{P}/analytics/reports/q3.csv",
                       f"{P}/analytics/notebooks/forecast.py", f"{P}/analytics/scratch/blob.bin"}
    must_be_exposed |= set(sandbox.traps["T06_WORKTREE_ONLY_COMMITS"]["items"])
    assert must_be_exposed <= no_route
    must_be_safe = {f"{P}/webshop/src/app.py", f"{P}/analytics/docs/runbook.md",
                    f"{P}/webshop-exp/src/app.py", f"{P}/notes-cli/notes.py"}
    assert not (must_be_safe & no_route)
    assert not any(i.startswith((f"{P}/webshop/vendor/", f"{P}/webshop/dist/")) for i in no_route)
    assert s["rebuildable_by_recipe"] > 0 and s["counts_by_class"]["SECRET"] >= 3
