"""What-if fork: copy the live graph, apply the destroyed set to the copy, compute, report, delete the copy."""
from __future__ import annotations

import hashlib
import secrets

from .. import config
from ..graphdb import GraphClient, hops
from ..loader import graph_stats
from .effects import apply_effects
from .fixpoint import justify, query_hashes, read_outcome, run_fixpoint
from .layout import Layout
from .model import Destroyed
from .resolver import resolve

CHUNK = 200

Q_RESET_AFFECTED = "MATCH (c:Commit) SET c.affected = 0"
M_ITEMS = "UNWIND $values AS v MATCH (i:Item {id: v}) SET i.gone = 1"
M_COPIES = "UNWIND $values AS v MATCH (c:Copy {path: v}) SET c.gone = 1"
M_REFS = "UNWIND $values AS v MATCH (r:Ref {id: v}) SET r.gone = 1"
M_REF_PREFIXES = "UNWIND $values AS v MATCH (r:Ref) WHERE r.id STARTS WITH v SET r.gone = 1"
M_REPOS = "UNWIND $values AS v MATCH (r:Ref {repo: v}) SET r.gone = 1"
M_PRUNABLE = "UNWIND $values AS v MATCH (r:Ref {kind: 'worktree_head', name: v}) SET r.prunable = 1"
M_MARK_HIT = """
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit)
WHERE r.gone = 1 OR r.prunable = 1
SET c.affected = 1
"""
M_MARK_REPOINTED = "MATCH (r:Ref {id: $rid})-[:POINTS_TO]->(c:Commit) SET c.affected = 1"
M_REPOINT_DROP = "MATCH (r:Ref {id: $rid})-[e:POINTS_TO]->(:Commit) DELETE e"
M_REPOINT_ADD = "MATCH (r:Ref {id: $rid}) MATCH (c:Commit {sha: $sha}) CREATE (r)-[:POINTS_TO]->(c)"

Q_COUNT_ITEMS_GONE = "MATCH (i:Item) WHERE i.gone = 1 RETURN count(i)"
Q_COUNT_COPIES_GONE = "MATCH (c:Copy) WHERE c.gone = 1 RETURN count(c)"
Q_COUNT_REFS_GONE = "MATCH (r:Ref) WHERE r.gone = 1 RETURN count(r)"
Q_COUNT_REFS_PRUNABLE = "MATCH (r:Ref) WHERE r.prunable = 1 RETURN count(r)"
Q_LIVE_FLAGS = """
MATCH (n) WHERE n.gone = 1 OR n.prunable = 1 OR n.affected = 1 OR n.rank IS NOT NULL
RETURN count(n)
"""


def q_spread_affected(n: int) -> str:
    return (f"MATCH (c:Commit)-[:PARENT*{hops(n)}]->(p:Commit) "
            "WHERE c.affected = 1 AND p.affected = 0 SET p.affected = 1")


def fork_name(live: GraphClient, label: str) -> str:
    return f"{live.name}_wi_{hashlib.sha256(label.encode()).hexdigest()[:8]}{secrets.token_hex(3)}"


def _each(fork: GraphClient, cypher: str, values) -> None:
    values = sorted(values)
    for i in range(0, len(values), CHUNK):
        fork.query(cypher, {"values": values[i:i + CHUNK]})


def apply_destroyed(fork: GraphClient, d: Destroyed, max_hops: int) -> None:
    fork.query(Q_RESET_AFFECTED)
    _each(fork, M_ITEMS, d.paths)
    _each(fork, M_COPIES, d.paths)
    _each(fork, M_REFS, d.refs)
    _each(fork, M_REF_PREFIXES, d.ref_prefixes)
    _each(fork, M_REPOS, d.repos)
    _each(fork, M_PRUNABLE, d.prunable)
    fork.query(M_MARK_HIT)
    for rid, sha in sorted(d.repoints.items()):
        fork.query(M_MARK_REPOINTED, {"rid": rid})
        fork.query(M_REPOINT_DROP, {"rid": rid})
        fork.query(M_REPOINT_ADD, {"rid": rid, "sha": sha})
    fork.query(q_spread_affected(max_hops))


def whatif(live: GraphClient, destroyed: Destroyed, label: str, max_hops: int | None = None,
           effects: dict | None = None) -> dict:
    n = max_hops or config.MAX_HOPS
    before = graph_stats(live)
    fork = live.copy_to(fork_name(live, label))
    try:
        apply_destroyed(fork, destroyed, n)
        if effects:
            apply_effects(fork, effects)
        fixpoint = run_fixpoint(fork, n)
        outcome = read_outcome(fork)
        exemplars = {}
        for entry in outcome["fragile"] + outcome["recoverable"]:
            exemplars.setdefault(entry["via"], entry["id"])
        outcome["justifications"] = {via: justify(fork, iid, n) for via, iid in sorted(exemplars.items())}
        diff = {"items_destroyed": fork.scalar(Q_COUNT_ITEMS_GONE), "copies_destroyed": fork.scalar(Q_COUNT_COPIES_GONE),
                "refs_destroyed": fork.scalar(Q_COUNT_REFS_GONE), "refs_prunable": fork.scalar(Q_COUNT_REFS_PRUNABLE),
                "refs_repointed": len(destroyed.repoints)}
        name = fork.name
    finally:
        fork.delete()
    if graph_stats(live) != before or live.scalar(Q_LIVE_FLAGS) != 0:
        raise RuntimeError("what-if mutated the live graph")
    diff["live_unchanged"] = True
    return {"fork": name, "passes": fixpoint["passes"], "outcome": outcome, "diff": diff,
            "query_hashes": query_hashes(n)}


def analyze(live: GraphClient, layout: Layout, command: str, cwd: str, env: dict | None = None,
            max_hops: int | None = None) -> dict:
    res = resolve(command, cwd, layout, env)
    result = None
    if not res.destroyed.is_empty():
        result = whatif(live, res.destroyed, res.command, max_hops)
    return {"resolution": res.to_dict(), "whatif": result}
