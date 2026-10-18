"""Audit trail in the live graph: Verdict, Action and FixStep nodes.

Reloading the world replaces the scanned labels only, so the trail survives. A Verdict keeps its own copy of
what it would have lost (lost_json), because the Items it pointed at may not exist after the command ran.
"""
from __future__ import annotations

import hashlib
import json

from ..graphdb import GraphClient

A_ACTION = "MERGE (a:Action {id: $id}) SET a.command = $command, a.cwd = $cwd"
A_VERDICT = ("CREATE (v:Verdict {id: $id, verdict: $verdict, status: $status, command: $command, cwd: $cwd, "
             "created: $created, degraded: $degraded, destroyed_hash: $destroyed_hash, outcome_hash: $outcome_hash, "
             "plan_hash: $plan_hash, world_fp: $world_fp, lost_json: $lost_json, payload: $payload})")
A_ABOUT = "MATCH (v:Verdict {id: $vid}) MATCH (a:Action {id: $aid}) CREATE (v)-[:ABOUT]->(a)"
A_WOULD_LOSE = ("UNWIND $ids AS iid MATCH (v:Verdict {id: $vid}) MATCH (i:Item {id: iid}) "
                "MERGE (v)-[:WOULD_LOSE]->(i)")
A_FIXSTEP = ("MATCH (v:Verdict {id: $vid}) CREATE (v)-[:PLANS]->(:FixStep {id: $id, fix: $fix, kind: $kind, "
             "cost: $cost, status: 'planned', params_json: $params, result_json: ''})")
A_STATUS = "MATCH (v:Verdict {id: $id}) SET v.status = $status, v.detail = $detail"
A_FIX_STATUS = "MATCH (f:FixStep {id: $id}) SET f.status = $status, f.result_json = $result"
Q_VERDICT_LOST = "MATCH (v:Verdict) RETURN v.id, v.lost_json"
Q_LOAD = "MATCH (v:Verdict {id: $id}) RETURN v.verdict, v.status, v.command, v.cwd, v.created, v.degraded, v.payload"
Q_LIST = "MATCH (v:Verdict) RETURN v.id, v.verdict, v.status, v.command, v.created ORDER BY v.created"
# Fingerprint only the modeled world. Audit nodes (Verdict, Action, FixStep)
# are deliberately excluded because recording a verdict must not stale itself.
_WORLD_LABELS = (
    "Item", "Copy", "Ref", "Commit", "Recipe", "Store", "Domain",
)
_WORLD_FILTER = " OR ".join(f"n:{label}" for label in _WORLD_LABELS)
_WORLD_EDGE_FILTER_A = " OR ".join(f"a:{label}" for label in _WORLD_LABELS)
_WORLD_EDGE_FILTER_B = " OR ".join(f"b:{label}" for label in _WORLD_LABELS)

Q_FP_NODES = (
    "MATCH (n) WHERE " + _WORLD_FILTER +
    " RETURN labels(n), properties(n)"
)
Q_FP_EDGES = (
    "MATCH (a)-[r]->(b) WHERE (" + _WORLD_EDGE_FILTER_A +
    ") AND (" + _WORLD_EDGE_FILTER_B +
    ") RETURN labels(a), properties(a), type(r), properties(r), "
    "labels(b), properties(b)"
)


def digest(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def fingerprint(live: GraphClient) -> str:
    """Deterministic fingerprint of the modeled world, excluding the audit trail."""
    def canonical_rows(rows):
        encoded = [
            json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
            for row in rows
        ]
        return sorted(encoded)

    return digest([
        canonical_rows(live.rows(Q_FP_NODES)),
        canonical_rows(live.rows(Q_FP_EDGES)),
    ])


def lost_summary(outcome) -> dict:
    if not outcome:
        return {"items": [], "commits": []}
    return {"items": sorted(e["id"] for e in outcome["lost"] + outcome["fragile"]),
            "commits": sorted(c["sha"] for c in outcome["lost_commits"] + outcome["fragile_commits"])}


def record(live: GraphClient, rec: dict) -> None:
    hashes, lost = rec["hashes"], lost_summary(rec["outcome"])
    action = digest([rec["command"], rec["cwd"]])[:12]
    status = {"GO": "approved", "FIX_THEN_GO": "planned"}.get(rec["verdict"], "blocked")
    payload = {k: rec[k] for k in ("env", "reasons", "outcome", "whatif", "plan", "hashes", "resolution")}
    live.query(A_ACTION, {"id": action, "command": rec["command"], "cwd": rec["cwd"]})
    live.query(A_VERDICT, {
        "id": rec["id"], "verdict": rec["verdict"], "status": status, "command": rec["command"], "cwd": rec["cwd"],
        "created": rec["created"], "degraded": rec["degraded"], "destroyed_hash": hashes["destroyed"] or "",
        "outcome_hash": hashes["outcome"] or "", "plan_hash": hashes["plan"] or "", "world_fp": hashes["world"] or "",
        "lost_json": json.dumps(lost), "payload": json.dumps(payload)})
    live.query(A_ABOUT, {"vid": rec["id"], "aid": action})
    if lost["items"]:
        live.query(A_WOULD_LOSE, {"vid": rec["id"], "ids": lost["items"]})
    for fix in (rec["plan"] or {}).get("fixes", []):
        live.query(A_FIXSTEP, {"vid": rec["id"], "id": f"{rec['id']}:{fix['id']}", "fix": fix["id"],
                               "kind": fix["kind"], "cost": fix["cost"], "params": json.dumps(fix["params"])})


def set_status(live: GraphClient, vid: str, status: str, detail: str = "") -> None:
    live.query(A_STATUS, {"id": vid, "status": status, "detail": detail})


def fix_status(live: GraphClient, step_id: str, status: str, result: str = "") -> None:
    live.query(A_FIX_STATUS, {"id": step_id, "status": status, "result": result})


def relink(live: GraphClient) -> None:
    """Re-create WOULD_LOSE edges to the items that still exist after a world reload."""
    for vid, blob in live.rows(Q_VERDICT_LOST):
        ids = json.loads(blob)["items"]
        if ids:
            live.query(A_WOULD_LOSE, {"vid": vid, "ids": ids})


def load(live: GraphClient, vid: str):
    rows = live.rows(Q_LOAD, {"id": vid})
    if not rows:
        return None
    verdict, status, command, cwd, created, degraded, payload = rows[0]
    rec = json.loads(payload)
    rec.update(id=vid, verdict=verdict, status=status, command=command, cwd=cwd, created=created, degraded=degraded)
    return rec


def list_verdicts(live: GraphClient) -> list:
    return [{"id": r[0], "verdict": r[1], "status": r[2], "command": r[3], "created": r[4]}
            for r in live.rows(Q_LIST)]
