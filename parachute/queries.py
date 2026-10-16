"""Cypher the product depends on, plus world_summary().

Each answer is stamped with a short hash of the query text so a result can be tied to the exact query.
Direction conventions: (Commit)-[:PARENT]->(its parent); (Ref)-[:POINTS_TO]->(Commit);
(Copy)-[:COPY_OF]->(Item); (Copy)-[:NEEDS]->(key Item); (Item)-[:TRACKED_AT]->(Commit containing that exact blob).

Domain comparisons are made in Python on values returned by equality-only joins: on the pinned engine a WHERE
comparing properties of two joined patterns produced an unjoined cross product.
"""
from __future__ import annotations

import hashlib

from . import config
from .graphdb import GraphClient, hops

Q_COUNTS_BY_CLASS = "MATCH (i:Item) RETURN i.class AS cls, count(i) AS n ORDER BY cls"
Q_ITEMS = "MATCH (i:Item) RETURN i.id AS id, i.class AS cls ORDER BY id"

Q_ROUTE_RECIPE = "MATCH (i:Item)-[:DERIVED_BY]->(:Recipe) RETURN DISTINCT i.id AS id"

Q_COPY_PLACEMENT = """
MATCH (c:Copy {alive: 1})-[:COPY_OF]->(i:Item)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(d:Domain)
MATCH (c)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(cd:Domain)
RETURN c.id AS copy, i.id AS item, d.id AS item_domain, cd.id AS copy_domain
"""

Q_PHANTOM_SAME_DOMAIN = """
MATCH (c:Copy)-[:COPY_OF]->(i:Item)
WHERE c.alive = 1
MATCH (i)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(d:Domain)
MATCH (c)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(d)
RETURN c.id AS copy, d.id AS domain, collect(i.id) AS items
ORDER BY copy
"""

Q_NEEDS = "MATCH (c:Copy)-[:NEEDS]->(k:Item) RETURN c.id AS copy, k.id AS key ORDER BY copy"
Q_COPY_COVERS = "MATCH (c:Copy)-[:COPY_OF]->(i:Item) RETURN c.id AS copy, collect(i.id) AS items ORDER BY copy"

Q_ROUTE_GIT_DIRECT = """
MATCH (i:Item)-[:TRACKED_AT]->(c:Commit)<-[:POINTS_TO]-(r:Ref)
WHERE r.kind = 'remote_branch'
RETURN DISTINCT i.id AS id
"""
Q_COMMITS_ALL = "MATCH (c:Commit) RETURN c.sha AS sha, c.subject AS subject ORDER BY sha"
Q_COMMITS_REMOTE_DIRECT = """
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit) WHERE r.kind = 'remote_branch' RETURN DISTINCT c.sha AS sha
"""


def q_route_git_via_parents(n: int) -> str:
    # hop bounds cannot be query parameters; hops() validates the int
    return (f"MATCH (r:Ref)-[:POINTS_TO]->(:Commit)-[:PARENT*{hops(n)}]->(c:Commit)<-[:TRACKED_AT]-(i:Item) "
            "WHERE r.kind = 'remote_branch' RETURN DISTINCT i.id AS id")


def q_commits_remote_via_parents(n: int) -> str:
    return (f"MATCH (r:Ref)-[:POINTS_TO]->(:Commit)-[:PARENT*{hops(n)}]->(c:Commit) "
            "WHERE r.kind = 'remote_branch' RETURN DISTINCT c.sha AS sha")


def qhash(q: str) -> str:
    return hashlib.sha256(" ".join(q.split()).encode()).hexdigest()[:8]


def copy_routes(g: GraphClient) -> list[tuple[str, str]]:
    """(copy, item) pairs where a live copy sits in a different failure domain than the item."""
    covers = {r[0]: set(r[1]) for r in g.rows(Q_COPY_COVERS)}
    pairs = []
    for copy, item, item_domain, copy_domain in g.rows(Q_COPY_PLACEMENT):
        if item not in covers.get(copy, ()):
            raise RuntimeError(f"unsound join: {copy} reported for {item} without a COPY_OF edge")
        if item_domain != copy_domain:
            pairs.append((copy, item))
    return pairs


def world_summary(g: GraphClient, max_hops: int | None = None) -> dict:
    n = max_hops or config.MAX_HOPS
    q_git_par, q_com_par = q_route_git_via_parents(n), q_commits_remote_via_parents(n)

    counts = {r[0]: r[1] for r in g.rows(Q_COUNTS_BY_CLASS)}
    items = {r[0]: r[1] for r in g.rows(Q_ITEMS)}
    by_recipe = {r[0] for r in g.rows(Q_ROUTE_RECIPE)}
    by_git = {r[0] for r in g.rows(Q_ROUTE_GIT_DIRECT)} | {r[0] for r in g.rows(q_git_par)}
    copy_pairs = copy_routes(g)

    covers = {r[0]: r[1] for r in g.rows(Q_COPY_COVERS)}
    keys_with_independent_copy = {item for _, item in copy_pairs}
    phantoms = [{"copy": r[0], "reason": "same_domain", "domain": r[1], "items": r[2]}
                for r in g.rows(Q_PHANTOM_SAME_DOMAIN)]
    bad_key_copies = set()
    for copy, key in ((r[0], r[1]) for r in g.rows(Q_NEEDS)):
        if key not in keys_with_independent_copy:
            bad_key_copies.add(copy)
            phantoms.append({"copy": copy, "reason": "key_without_independent_copy", "key": key,
                             "items": covers.get(copy, [])})
    by_copy = {item for copy, item in copy_pairs if copy not in bad_key_copies}

    routed = by_recipe | by_git | by_copy
    no_route = sorted(i for i in items if i not in routed)
    by_class: dict[str, int] = {}
    for i in no_route:
        by_class[items[i]] = by_class.get(items[i], 0) + 1

    remote_commits = {r[0] for r in g.rows(Q_COMMITS_REMOTE_DIRECT)} | {r[0] for r in g.rows(q_com_par)}
    unpushed = [{"sha": r[0], "subject": r[1]} for r in g.rows(Q_COMMITS_ALL) if r[0] not in remote_commits]

    return {
        "graph": g.name,
        "counts_by_class": counts,
        "total_items": len(items),
        "rebuildable_by_recipe": len(by_recipe),
        "items_with_durable_route": len(routed),
        "items_no_durable_route": {"count": len(no_route), "by_class": by_class, "items": no_route},
        "phantom_backups": phantoms,
        "unpushed_commits": unpushed,
        "query_hashes": {"counts_by_class": qhash(Q_COUNTS_BY_CLASS), "route_recipe": qhash(Q_ROUTE_RECIPE),
                         "copy_placement": qhash(Q_COPY_PLACEMENT), "phantom_same_domain": qhash(Q_PHANTOM_SAME_DOMAIN),
                         "needs": qhash(Q_NEEDS), "route_git_direct": qhash(Q_ROUTE_GIT_DIRECT),
                         "route_git_via_parents": qhash(q_git_par),
                         "commits_remote_via_parents": qhash(q_com_par)},
    }
