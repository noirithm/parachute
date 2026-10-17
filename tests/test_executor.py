import json
import os

from parachute.engine.executor import execute, reload_world
from parachute.engine.policy import preflight, replay

P = "disk_a/projects"


def test_verdict_is_recorded_and_replayable(fresh):
    sb, g, layout = fresh
    v = preflight(g, layout, "rm -rf analytics", str(sb.root / P))
    row = g.rows("MATCH (v:Verdict {id: $id})-[:ABOUT]->(a:Action) RETURN v.verdict, v.status, a.command",
                 {"id": v["id"]})
    assert row == [["FIX_THEN_GO", "planned", "rm -rf analytics"]]
    assert g.scalar("MATCH (:Verdict {id: $id})-[:PLANS]->(f:FixStep) RETURN count(f)", {"id": v["id"]}) == 3
    assert g.scalar("MATCH (:Verdict {id: $id})-[:WOULD_LOSE]->(i:Item) RETURN count(i)", {"id": v["id"]}) == 9
    assert replay(g, layout, v["id"])["status"] == "identical"


def test_fix_then_go_runs_fixes_then_the_command(fresh):
    sb, g, layout = fresh
    v = preflight(g, layout, "rm -rf analytics", str(sb.root / P))
    result = execute(g, layout, v)
    assert result["status"] == "executed" and result["command"]["returncode"] == 0
    assert not (sb.root / P / "analytics").exists()
    offsite = sb.root / "disk_b" / "offsite"
    assert len(list(offsite.glob("parachute-*"))) == 3
    assert len(list((sb.root / "disk_c" / "keyring").glob("parachute-*.key"))) == 1
    manifest = json.loads((offsite / "MANIFEST.json").read_text())["copies"]
    assert {f"pc-{f['id']}" for f in v["plan"]["fixes"]} <= {c["id"] for c in manifest}
    assert g.rows("MATCH (v:Verdict {id: $id}) RETURN v.status", {"id": v["id"]}) == [["executed"]]
    assert g.scalar("MATCH (:Verdict {id: $id})-[:PLANS]->(f:FixStep {status: 'ok'}) RETURN count(f)",
                    {"id": v["id"]}) == 3
    assert g.scalar("MATCH (i:Item) WHERE i.id STARTS WITH $p RETURN count(i)", {"p": f"{P}/analytics/"}) == 0


def test_go_runs_without_fixes(fresh):
    sb, g, layout = fresh
    v = preflight(g, layout, "rm -rf vendor", str(sb.root / P / "webshop"))
    result = execute(g, layout, v)
    assert v["verdict"] == "GO" and result["status"] == "executed" and result["fixes"] == []
    assert not (sb.root / P / "webshop" / "vendor").exists()


def test_stop_and_ask_never_runs(fresh):
    sb, g, layout = fresh
    v = preflight(g, layout, "rm -rf ~", str(sb.root / P))
    assert execute(g, layout, v)["status"] == "refused"
    assert (sb.root / P / "webshop" / "src" / "app.py").exists()


def test_stale_verdict_is_not_executed(fresh):
    sb, g, layout = fresh
    v = preflight(g, layout, "rm -rf analytics", str(sb.root / P))
    (sb.root / P / "analytics" / "notes.txt").write_text("added after the verdict")
    reload_world(g, layout)
    assert execute(g, layout, v)["status"] == "stale"
    assert (sb.root / P / "analytics").exists()


def test_failed_fix_blocks_the_command(fresh):
    sb, g, layout = fresh
    v = preflight(g, layout, "rm -rf analytics", str(sb.root / P))
    offsite = sb.root / "disk_b" / "offsite"
    os.chmod(offsite, 0o500)
    try:
        result = execute(g, layout, v)
    finally:
        os.chmod(offsite, 0o700)
    assert result["status"] == "fix_failed"
    assert (sb.root / P / "analytics").exists()
    assert g.scalar("MATCH (:Verdict {id: $id})-[:PLANS]->(f:FixStep {status: 'failed'}) RETURN count(f)",
                    {"id": v["id"]}) == 1
