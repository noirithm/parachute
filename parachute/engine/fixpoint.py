"""Recoverability fixpoint, computed inside FalkorDB on a fork of the world graph.

Every Item and Commit gets a rank (0 = no route, 1 = WEAK, 2 = DURABLE) and a `via` label naming the route
that justified it. Ranks only ever go up, so repeating the route queries until the total rank stops
changing terminates. Routes are applied strongest tier first and never overwrite a stronger result.

Routes:
  copy    a live Copy of the item whose NEEDS (e.g. an encryption key) are all recoverable at that rank;
          DURABLE only when the copy lives in another failure domain than the item
  git     the item's exact blob is tracked at a Commit reachable (by PARENT paths) from a surviving Ref;
          DURABLE from a remote branch, WEAK from local refs, reflog entries and prunable worktrees
  recipe  a deterministic, offline Recipe whose READS inputs are all recoverable at that rank
"""
from __future__ import annotations

from .. import config
from ..graphdb import GraphClient, hops
from ..queries import qhash

STRENGTH = {0: "NONE", 1: "WEAK", 2: "DURABLE"}
MAX_PASSES = 50

LOCAL_TIERS = (
    (["branch", "tag", "stash", "worktree_head"], 0, "local_ref"),
    (["worktree_head"], 1, "worktree_prunable"),
    (["reflog"], 0, "reflog"),
)
PROPAGATION = (("remote_branch", 2), ("bundle", 2), ("local_ref", 1), ("worktree_prunable", 1), ("reflog", 1))
LABEL_KINDS = {"remote_branch": ["remote_branch"], "bundle": ["bundle"], "local_ref": ["branch", "tag", "stash", "worktree_head"],
               "worktree_prunable": ["worktree_head"], "reflog": ["reflog"]}

Q_INIT_ITEMS = """
MATCH (i:Item)
SET i.rank = CASE WHEN i.gone = 0 THEN 2 ELSE 0 END,
    i.via = CASE WHEN i.gone = 0 THEN 'present' ELSE 'none' END
"""
Q_INIT_COMMITS = "MATCH (c:Commit) SET c.rank = 0, c.via = 'none'"
Q_SEED_DURABLE = """
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit)
WHERE r.gone = 0 AND r.kind = $kind AND c.rank < 2
SET c.rank = 2, c.via = $kind
"""
Q_SEED_LOCAL = """
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit)
WHERE r.gone = 0 AND r.kind IN $kinds AND r.prunable = $prunable AND c.rank < 1
SET c.rank = 1, c.via = $label
"""
Q_ROUTE_GIT = """
MATCH (i:Item)-[:TRACKED_AT]->(c:Commit)
WHERE i.gone = 1 AND i.rank < $rank AND c.rank >= $rank
SET i.rank = $rank, i.via = 'git:' + c.via
"""
Q_ROUTE_COPY = """
MATCH (c:Copy)-[:COPY_OF]->(i:Item)
OPTIONAL MATCH (c)-[:NEEDS]->(k:Item)
WITH c, i, count(k) AS needed, sum(CASE WHEN k.rank >= $rank THEN 1 ELSE 0 END) AS ready
WHERE i.gone = 1 AND i.rank < $rank AND c.alive = 1 AND c.gone = 0
  AND (c.domain <> i.domain OR $rank = 1) AND needed = ready
SET i.rank = $rank, i.via = 'copy:' + c.id
"""
Q_ROUTE_RECIPE = """
MATCH (i:Item)-[:DERIVED_BY]->(r:Recipe)
OPTIONAL MATCH (r)-[:READS]->(k:Item)
WITH i, r, count(k) AS needed, sum(CASE WHEN k.rank >= $rank THEN 1 ELSE 0 END) AS ready
WHERE i.gone = 1 AND i.rank < $rank AND r.deterministic = 1 AND r.network = 0 AND needed = ready
SET i.rank = $rank, i.via = 'recipe:' + r.id
"""
Q_RANK_SUM = "MATCH (i:Item) RETURN sum(i.rank)"

Q_DESTROYED_ITEMS = """
MATCH (i:Item) WHERE i.gone = 1
RETURN i.id AS id, i.class AS cls, i.rank AS rank, i.via AS via ORDER BY id
"""
Q_AFFECTED_COMMITS = """
MATCH (c:Commit) WHERE c.affected = 1
RETURN c.sha AS sha, c.subject AS subject, c.rank AS rank, c.via AS via ORDER BY sha
"""

Q_JUSTIFY_STATE = "MATCH (i:Item {id: $id}) RETURN i.rank, i.via"
Q_JUSTIFY_COPY = "MATCH (c:Copy {id: $id})-[:STORED_ON]->(s:Store)-[:IN_DOMAIN]->(d:Domain) RETURN s.id, d.id, c.file"
Q_JUSTIFY_KEYS = "MATCH (c:Copy {id: $id})-[:NEEDS]->(k:Item) RETURN k.id, k.rank, k.via ORDER BY k.id"
Q_JUSTIFY_TRACKED = """
MATCH (i:Item {id: $id})-[:TRACKED_AT]->(c:Commit)
WHERE c.rank >= $rank AND c.via = $label
RETURN c.sha, c.subject ORDER BY c.sha LIMIT 1
"""
Q_JUSTIFY_HOLDER = """
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit {sha: $sha})
WHERE r.gone = 0 AND r.kind IN $kinds
RETURN r.id ORDER BY r.id LIMIT 1
"""
Q_JUSTIFY_RECIPE = "MATCH (r:Recipe {id: $id}) RETURN r.command"
Q_JUSTIFY_INPUTS = "MATCH (r:Recipe {id: $id})-[:READS]->(k:Item) RETURN k.id, k.rank, k.via ORDER BY k.id LIMIT 4"


def q_propagate(n: int) -> str:
    return (f"MATCH (c:Commit {{via: $label}})-[:PARENT*{hops(n)}]->(p:Commit) "
            "WHERE p.rank < $rank SET p.rank = $rank, p.via = $label")


def q_justify_holder_parents(n: int) -> str:
    return (f"MATCH (r:Ref)-[:POINTS_TO]->(t:Commit)-[:PARENT*{hops(n)}]->(c:Commit {{sha: $sha}}) "
            "WHERE r.gone = 0 AND r.kind IN $kinds RETURN r.id, t.sha ORDER BY r.id LIMIT 1")


def query_hashes(n: int) -> dict:
    named = {"init_items": Q_INIT_ITEMS, "init_commits": Q_INIT_COMMITS, "seed_durable": Q_SEED_DURABLE,
             "seed_local": Q_SEED_LOCAL, "propagate": q_propagate(n), "route_git": Q_ROUTE_GIT,
             "route_copy": Q_ROUTE_COPY, "route_recipe": Q_ROUTE_RECIPE, "destroyed_items": Q_DESTROYED_ITEMS,
             "affected_commits": Q_AFFECTED_COMMITS}
    return {name: qhash(text) for name, text in named.items()}


def run_fixpoint(fork: GraphClient, max_hops: int | None = None) -> dict:
    n = max_hops or config.MAX_HOPS
    fork.query(Q_INIT_ITEMS)
    fork.query(Q_INIT_COMMITS)
    for kind in ("remote_branch", "bundle"):
        fork.query(Q_SEED_DURABLE, {"kind": kind})
    for kinds, prunable, label in LOCAL_TIERS:
        fork.query(Q_SEED_LOCAL, {"kinds": kinds, "prunable": prunable, "label": label})
    for label, rank in PROPAGATION:
        fork.query(q_propagate(n), {"label": label, "rank": rank})

    passes = 0
    while True:
        before = fork.scalar(Q_RANK_SUM)
        for rank in (2, 1):
            fork.query(Q_ROUTE_GIT, {"rank": rank})
            fork.query(Q_ROUTE_COPY, {"rank": rank})
            fork.query(Q_ROUTE_RECIPE, {"rank": rank})
        passes += 1
        if fork.scalar(Q_RANK_SUM) == before:
            return {"passes": passes}
        if passes >= MAX_PASSES:
            raise RuntimeError(f"fixpoint did not settle within {MAX_PASSES} passes")


def read_outcome(fork: GraphClient) -> dict:
    lost, fragile, recoverable = [], [], []
    for iid, cls, rank, via in fork.rows(Q_DESTROYED_ITEMS):
        entry = {"id": iid, "class": cls, "strength": STRENGTH[rank], "via": via}
        if rank == 0:
            lost.append(entry)
        elif rank == 1 and cls != "GENERATED":
            fragile.append(entry)
        else:
            recoverable.append(entry)
    lost_commits, fragile_commits, safe_commits = [], [], []
    for sha, subject, rank, via in fork.rows(Q_AFFECTED_COMMITS):
        entry = {"sha": sha, "subject": subject, "strength": STRENGTH[rank], "via": via}
        if rank == 0:
            lost_commits.append(entry)
        elif rank == 1:
            fragile_commits.append(entry)
        else:
            safe_commits.append(sha)
    return {"destroyed_items": len(lost) + len(fragile) + len(recoverable), "lost": lost, "fragile": fragile,
            "recoverable": recoverable, "lost_commits": lost_commits, "fragile_commits": fragile_commits,
            "recoverable_commits": len(safe_commits), "recoverable_commit_shas": safe_commits}


def justify(fork: GraphClient, item_id: str, max_hops: int | None = None) -> dict:
    n = max_hops or config.MAX_HOPS
    rank, via = fork.rows(Q_JUSTIFY_STATE, {"id": item_id})[0]
    kind, _, ref = via.partition(":")
    path: list = []
    if kind == "present":
        path.append("untouched by this action")
    elif kind == "copy":
        for store, domain, file in fork.rows(Q_JUSTIFY_COPY, {"id": ref}):
            path.append(f"copy {ref} ({file}) in store {store}, failure domain {domain}")
        for key, krank, kvia in fork.rows(Q_JUSTIFY_KEYS, {"id": ref}):
            path.append(f"the copy needs {key}: {STRENGTH[krank]} via {kvia}")
    elif kind == "git":
        tracked = fork.rows(Q_JUSTIFY_TRACKED, {"id": item_id, "rank": rank, "label": ref})
        if tracked:
            sha, subject = tracked[0]
            path.append(f"content is tracked at commit {sha[:10]} ({subject})")
            args = {"sha": sha, "kinds": LABEL_KINDS[ref]}
            direct = fork.rows(Q_JUSTIFY_HOLDER, args)
            if direct:
                path.append(f"the commit is held by {direct[0][0]}")
            else:
                parents = fork.rows(q_justify_holder_parents(n), args)
                if parents:
                    path.append(f"reachable by PARENT links from {parents[0][0]} at commit {parents[0][1][:10]}")
    elif kind == "recipe":
        for command in fork.rows(Q_JUSTIFY_RECIPE, {"id": ref}):
            path.append(f"recipe {ref} re-runs `{command[0]}`")
        for key, krank, kvia in fork.rows(Q_JUSTIFY_INPUTS, {"id": ref}):
            path.append(f"input {key}: {STRENGTH[krank]} via {kvia}")
    return {"item": item_id, "strength": STRENGTH[rank], "via": via, "path": path}
