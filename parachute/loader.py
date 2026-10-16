"""Idempotent MERGE loader: world dict -> FalkorDB (one graph per sandbox session).

Every statement is a hand-written constant; data only ever travels as parameters ($rows).
Running load_world twice yields the same graph (MERGE on a stable key + SET of properties).
"""
from __future__ import annotations

import redis

from .graphdb import GraphClient

BATCH = 200
INDEXES = [("Item", "id"), ("Item", "path"), ("Store", "id"), ("Domain", "id"), ("Commit", "sha"),
           ("Ref", "id"), ("Copy", "id"), ("Recipe", "id")]
LABELS = ("Item", "Store", "Domain", "Commit", "Ref", "Copy", "Recipe")
RELS = ("STORED_ON", "IN_DOMAIN", "COPY_OF", "NEEDS", "POINTS_TO", "PARENT", "DERIVED_BY", "READS", "TRACKED_AT")

Q_RESET = "MATCH (n) DETACH DELETE n"
L_DOMAINS = "UNWIND $rows AS row MERGE (d:Domain {id: row.id}) SET d.label = row.label, d.kind = row.kind"
L_STORES = "UNWIND $rows AS row MERGE (s:Store {id: row.id}) SET s.path = row.path, s.kind = row.kind"
L_STORE_DOMAIN = ("UNWIND $rows AS row MATCH (s:Store {id: row.store}) MATCH (d:Domain {id: row.domain}) "
                  "MERGE (s)-[:IN_DOMAIN]->(d)")
L_ITEMS = ("UNWIND $rows AS row MERGE (i:Item {id: row.id}) SET i.path = row.path, i.kind = row.kind, "
           "i.class = row.cls, i.content_hash = row.content_hash, i.size = row.size, i.blob_sha1 = row.blob_sha1")
L_ITEM_STORE = ("UNWIND $rows AS row MATCH (i:Item {id: row.item}) MATCH (s:Store {id: row.store}) "
                "MERGE (i)-[:STORED_ON]->(s)")
L_RECIPES = ("UNWIND $rows AS row MERGE (r:Recipe {id: row.id}) SET r.rule = row.rule, r.command = row.command, "
             "r.cwd = row.cwd, r.outputs = row.outputs, r.deterministic = row.deterministic, r.network = row.network")
L_DERIVED = ("UNWIND $rows AS row MATCH (i:Item {id: row.item}) MATCH (r:Recipe {id: row.recipe}) "
             "MERGE (i)-[:DERIVED_BY]->(r)")
L_READS = ("UNWIND $rows AS row MATCH (r:Recipe {id: row.recipe}) MATCH (i:Item {id: row.item}) "
           "MERGE (r)-[:READS]->(i)")
L_COPIES = ("UNWIND $rows AS row MERGE (c:Copy {id: row.id}) SET c.kind = row.kind, c.file = row.file, "
            "c.alive = row.alive, c.encrypted = row.encrypted, c.sha256 = row.sha256, c.verified_at = row.verified_at")
L_COPY_STORE = ("UNWIND $rows AS row MATCH (c:Copy {id: row.copy}) MATCH (s:Store {id: row.store}) "
                "MERGE (c)-[:STORED_ON]->(s)")
L_COPY_OF = ("UNWIND $rows AS row MATCH (c:Copy {id: row.copy}) MATCH (i:Item {id: row.item}) "
             "MERGE (c)-[x:COPY_OF]->(i) SET x.kind = row.kind, x.verified_at = row.verified_at")
L_NEEDS = ("UNWIND $rows AS row MATCH (c:Copy {id: row.copy}) MATCH (k:Item {id: row.item}) "
           "MERGE (c)-[:NEEDS]->(k)")
L_COMMITS = "UNWIND $rows AS row MERGE (c:Commit {sha: row.sha}) SET c.subject = row.subject"
L_PARENTS = ("UNWIND $rows AS row MATCH (a:Commit {sha: row.child}) MATCH (b:Commit {sha: row.parent}) "
             "MERGE (a)-[:PARENT]->(b)")
L_REFS = ("UNWIND $rows AS row MERGE (r:Ref {id: row.id}) SET r.kind = row.kind, r.name = row.name, "
          "r.repo = row.repo")
L_REF_STORE = ("UNWIND $rows AS row MATCH (r:Ref {id: row.ref}) MATCH (s:Store {id: row.store}) "
               "MERGE (r)-[:STORED_ON]->(s)")
L_POINTS_TO = ("UNWIND $rows AS row MATCH (r:Ref {id: row.ref}) MATCH (c:Commit {sha: row.sha}) "
               "MERGE (r)-[:POINTS_TO]->(c)")
L_TRACKED = ("UNWIND $rows AS row MATCH (i:Item {id: row.item}) MATCH (c:Commit {sha: row.sha}) "
             "MERGE (i)-[x:TRACKED_AT]->(c) SET x.blob = row.blob")


def ensure_schema(g: GraphClient) -> None:
    for label, prop in INDEXES:  # constants only, never user data
        try:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except redis.exceptions.ResponseError as e:
            if "already" not in str(e).lower():
                raise


def _run(g: GraphClient, cypher: str, rows: list) -> None:
    for i in range(0, len(rows), BATCH):
        g.query(cypher, {"rows": rows[i:i + BATCH]})


def graph_stats(g: GraphClient) -> dict:
    labels = {lb: g.scalar(f"MATCH (n:{lb}) RETURN count(n)") for lb in LABELS}
    rels = {r: g.scalar(f"MATCH ()-[x:{r}]->() RETURN count(x)") for r in RELS}
    return {"nodes": g.scalar("MATCH (n) RETURN count(n)"), "edges": g.scalar("MATCH ()-[x]->() RETURN count(x)"),
            "labels": labels, "rels": rels}


def load_world(g: GraphClient, world: dict, reset: bool = False) -> dict:
    if reset:
        g.query(Q_RESET)
    ensure_schema(g)
    items = world["items"]
    _run(g, L_DOMAINS, world["domains"])
    _run(g, L_STORES, [{"id": s["id"], "path": s["path"], "kind": s["kind"]} for s in world["stores"]])
    _run(g, L_STORE_DOMAIN, [{"store": s["id"], "domain": s["domain"]} for s in world["stores"]])
    _run(g, L_ITEMS, [{k: i[k] for k in ("id", "path", "kind", "cls", "content_hash", "size", "blob_sha1")}
                      for i in items])
    _run(g, L_ITEM_STORE, [{"item": i["id"], "store": i["store"]} for i in items])
    _run(g, L_RECIPES, [{"id": r["id"], "rule": r["rule"], "command": r["command"], "cwd": r["cwd"],
                         "outputs": ",".join(r["outputs"]), "deterministic": r["deterministic"],
                         "network": r["network"]} for r in world["recipes"]])
    _run(g, L_DERIVED, [{"item": i["id"], "recipe": i["recipe"]} for i in items if i["recipe"]])
    _run(g, L_READS, [{"recipe": r["id"], "item": x} for r in world["recipes"] for x in r["reads"]])
    _run(g, L_COPIES, world["copies"])
    _run(g, L_COPY_STORE, [{"copy": c["id"], "store": c["store"]} for c in world["copies"]])
    _run(g, L_COPY_OF, world["copy_of"])
    _run(g, L_NEEDS, world["needs"])
    _run(g, L_COMMITS, world["commits"])
    _run(g, L_PARENTS, world["parents"])
    _run(g, L_REFS, [{k: r[k] for k in ("id", "kind", "name", "repo")} for r in world["refs"]])
    _run(g, L_REF_STORE, [{"ref": r["id"], "store": r["store"]} for r in world["refs"] if r["store"]])
    _run(g, L_POINTS_TO, world["points_to"])
    _run(g, L_TRACKED, world["tracked"])
    return graph_stats(g)
