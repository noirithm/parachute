"""Applies the predicted graph effects of fixes to a what-if fork. Never touches the live graph."""
from __future__ import annotations

from ..graphdb import GraphClient

CHUNK = 200

E_ITEMS = ("UNWIND $values AS v MERGE (i:Item {id: v.id}) "
           "SET i.path = v.id, i.kind = 'file', i.class = v.cls, i.domain = v.domain, i.gone = 0")
E_COPIES = ("UNWIND $values AS v MERGE (c:Copy {id: v.id}) "
            "SET c.kind = v.kind, c.path = v.path, c.domain = v.domain, c.alive = 1, c.gone = 0")
E_COPY_OF = ("UNWIND $values AS v MATCH (c:Copy {id: v.copy}) MATCH (i:Item {id: v.item}) "
             "MERGE (c)-[x:COPY_OF]->(i) SET x.kind = v.kind")
E_NEEDS = "UNWIND $values AS v MATCH (c:Copy {id: v.copy}) MATCH (k:Item {id: v.item}) MERGE (c)-[:NEEDS]->(k)"
E_REFS = ("UNWIND $values AS v MERGE (r:Ref {id: v.id}) "
          "SET r.kind = v.kind, r.name = v.name, r.repo = v.repo, r.domain = v.domain, r.gone = 0, r.prunable = 0")
E_POINTS = "UNWIND $values AS v MATCH (r:Ref {id: v.ref}) MATCH (c:Commit {sha: v.sha}) MERGE (r)-[:POINTS_TO]->(c)"

ORDERED = (("items", E_ITEMS), ("copies", E_COPIES), ("copy_of", E_COPY_OF), ("needs", E_NEEDS),
           ("refs", E_REFS), ("points", E_POINTS))


def merge(parts: list) -> dict:
    out = {key: [] for key, _ in ORDERED}
    for part in parts:
        for key, _ in ORDERED:
            out[key].extend(part.get(key, []))
    return out


def apply_effects(fork: GraphClient, effects: dict) -> None:
    for key, cypher in ORDERED:
        values = effects.get(key, [])
        for i in range(0, len(values), CHUNK):
            fork.query(cypher, {"values": values[i:i + CHUNK]})
