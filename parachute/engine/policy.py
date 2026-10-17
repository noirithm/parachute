"""Deterministic verdict policy. The LLM never decides safety: everything here is resolver plus graph.

GO            nothing destroyed is lost or only weakly held
FIX_THEN_GO   every lost or fragile target has a fix, and the plan was re-verified on a fresh fork
STOP_AND_ASK  a hard guard fired, the command is unresolvable, some target has no fix, or the graph is down
"""
from __future__ import annotations

import os
import uuid

from ..graphdb import GraphClient, GraphUnavailable
from . import audit
from .audit import digest
from .layout import Layout
from .planner import plan as make_plan, targets
from .resolver import resolve
from .util import now_iso
from .whatif import whatif

GO, FIX_THEN_GO, STOP_AND_ASK = "GO", "FIX_THEN_GO", "STOP_AND_ASK"
Q_WORLD_SIZE = "MATCH (i:Item) RETURN count(i)"


def _entities(entries) -> list:
    return sorted((e["id"], e["strength"], e["via"]) for e in entries)


def _commits(entries) -> list:
    return sorted((c["sha"], c["strength"], c["via"]) for c in entries)


def outcome_signature(o: dict) -> dict:
    return {"lost": _entities(o["lost"]), "fragile": _entities(o["fragile"]),
            "recoverable": _entities(o["recoverable"]), "lost_commits": _commits(o["lost_commits"]),
            "fragile_commits": _commits(o["fragile_commits"]),
            "recoverable_commits": sorted(o["recoverable_commit_shas"])}


def _unfixable(plan: dict) -> list:
    left = plan["unfixable"]
    shown = ", ".join(f"{u['kind']} {u['id'][:70]}" for u in left[:4])
    more = f" (+{len(left) - 4} more)" if len(left) > 4 else ""
    return [f"no verified fix for {len(left)} lost or fragile target(s): {shown}{more}"]


def _conclude(live: GraphClient, rec: dict, verdict: str, reasons: list, record: bool) -> dict:
    rec["verdict"], rec["reasons"] = verdict, reasons
    if record:
        audit.record(live, rec)
    return rec


def preflight(live: GraphClient, layout: Layout, command: str, cwd: str, env: dict | None = None,
              max_hops: int | None = None, record: bool = True) -> dict:
    cwd_abs = os.path.realpath(cwd)
    rec = {"id": "v-" + uuid.uuid4().hex[:12], "command": command, "cwd": layout.rel(cwd_abs), "env": dict(env or {}),
           "created": now_iso(), "degraded": 0, "resolution": None, "outcome": None, "whatif": None, "plan": None,
           "hashes": {"destroyed": None, "outcome": None, "plan": None, "world": None}}
    try:
        if live.scalar(Q_WORLD_SIZE) == 0:
            return _conclude(live, rec, STOP_AND_ASK, ["the world graph is empty; scan and load the sandbox first"],
                             record)
        rec["hashes"]["world"] = audit.fingerprint(live)
        if not os.path.isdir(cwd_abs):
            return _conclude(live, rec, STOP_AND_ASK, [f"working directory {cwd_abs} does not exist"], record)
        res = resolve(command, cwd_abs, layout, env)
        rec["resolution"] = res.to_dict()
        rec["hashes"]["destroyed"] = digest(res.destroyed.to_dict())
        if res.forced_verdict:
            return _conclude(live, rec, res.forced_verdict, res.reasons, record)
        if res.destroyed.is_empty():
            return _conclude(live, rec, GO, ["the command destroys nothing"], record)
        base = whatif(live, res.destroyed, command, max_hops)
        rec["outcome"] = base["outcome"]
        rec["whatif"] = {"passes": base["passes"], "diff": base["diff"], "query_hashes": base["query_hashes"]}
        rec["hashes"]["outcome"] = digest(outcome_signature(base["outcome"]))
        if not targets(base["outcome"]):
            return _conclude(live, rec, GO, ["everything destroyed keeps a durable route back"], record)
        plan = make_plan(live, layout, res.destroyed, base, command, max_hops)
        rec["plan"] = plan
        rec["hashes"]["plan"] = digest([f["id"] for f in plan["fixes"]])
        if plan["verified"]:
            kinds = ", ".join(sorted({f["kind"] for f in plan["fixes"]}))
            reasons = [f"{plan['needed']} lost or fragile target(s) can be made recoverable first: "
                       f"{len(plan['fixes'])} fix(es) planned ({kinds}), estimated cost {plan['cost']} bytes"]
            return _conclude(live, rec, FIX_THEN_GO, reasons, record)
        return _conclude(live, rec, STOP_AND_ASK, _unfixable(plan), record)
    except GraphUnavailable as e:
        rec.update(degraded=1, verdict=STOP_AND_ASK, reasons=[f"DEGRADED: {e}; failing closed"])
        return rec


def replay(live: GraphClient, layout: Layout, verdict_id: str, max_hops: int | None = None) -> dict:
    """Recompute a stored verdict against the current graph and compare hashes."""
    stored = audit.load(live, verdict_id)
    if stored is None:
        raise KeyError(verdict_id)
    cwd = os.path.join(layout.root, stored["cwd"])
    again = preflight(live, layout, stored["command"], cwd, stored["env"], max_hops, record=False)
    same_world = again["hashes"]["world"] == stored["hashes"]["world"]
    same = again["verdict"] == stored["verdict"] and all(
        again["hashes"][k] == stored["hashes"][k] for k in ("destroyed", "outcome", "plan"))
    status = "identical" if same else ("world_changed" if not same_world else "diverged")
    return {"verdict_id": verdict_id, "status": status, "stored": {"verdict": stored["verdict"],
            "hashes": stored["hashes"]}, "replayed": {"verdict": again["verdict"], "hashes": again["hashes"]}}
