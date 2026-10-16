#!/usr/bin/env python
"""Smoke-test the pinned FalkorDB image and write docs/GROUND-TRUTH.md."""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parachute import config
from parachute.graphdb import GraphClient, GraphUnavailable, connect, hops

GRAPH, FORK = "parachute_smoke", "parachute_smoke_fork"
results: list[tuple[str, str, str]] = []  # (status, check, detail)


def run(name, fn, critical=True):
    try:
        results.append(("PASS", name, str(fn() or "")))
    except Exception as e:
        results.append(("FAIL" if critical else "NOTE", name, f"{type(e).__name__}: {e}"))


def main() -> int:
    db = connect()  # raises GraphUnavailable if docker is not up
    version = db.connection.info().get("redis_version", "?")
    try:
        modules = str(db.connection.execute_command("MODULE", "LIST"))
    except Exception as e:
        modules = f"(unavailable: {e})"
    GraphClient(FORK, db).delete()
    g = GraphClient(GRAPH, db)
    g.delete()

    def c_params():
        payload = 'x"; MATCH (n) DETACH DELETE n // \\ \' end'
        got = g.scalar("RETURN $s", {"s": payload})
        assert got == payload, f"round-trip mismatch: {got!r}"
        return "hostile string round-trips as data via parameters"

    def c_unwind():
        rows = [{"id": f"n{i}", "v": i, "cls": "SECRET" if i % 2 else "DATA"} for i in range(5)]
        q = "UNWIND $rows AS row MERGE (n:SmokeItem {id: row.id}) SET n.v = row.v, n.class = row.cls"
        g.query(q, {"rows": rows})
        g.query(q, {"rows": rows})
        assert g.scalar("MATCH (n:SmokeItem) RETURN count(n)") == 5
        assert g.scalar("MATCH (n:SmokeItem) WHERE n.class = $c RETURN count(n)", {"c": "SECRET"}) == 2
        return "UNWIND+MERGE with list-of-maps params is idempotent; property named `class` works"

    def c_index():
        g.query("CREATE INDEX FOR (n:SmokeItem) ON (n.id)")
        plan = g.db.connection.execute_command(
            "GRAPH.EXPLAIN", GRAPH, "MATCH (n:SmokeItem {id:'n1'}) RETURN n")
        text = " ".join(map(str, plan)) if isinstance(plan, (list, tuple)) else str(plan)
        return f"index created; plan mentions an index scan: {'index' in text.lower()}"

    def c_varlen():
        g.query("UNWIND range(0,29) AS k CREATE (:Chain {i: k})")
        g.query("UNWIND range(0,28) AS k MATCH (a:Chain {i:k}), (b:Chain {i:k+1}) CREATE (a)-[:P]->(b)")
        out = []
        for n, want in ((10, 10), (50, 29)):
            got = g.scalar(f"MATCH (a:Chain {{i:0}})-[:P*{hops(n)}]->(b:Chain) RETURN count(DISTINCT b)")
            assert got == want, f"*{hops(n)} returned {got}, expected {want}"
            out.append(f"*{hops(n)}={got}")
        return "literal hop bounds from validated ints OK: " + ", ".join(out)

    def c_zero_hop():
        got = g.scalar("MATCH (a:Chain {i:0})-[:P*0..10]->(b:Chain) RETURN count(DISTINCT b)")
        return f"*0..10 returned {got} (expected 11); loader avoids *0.. anyway"

    def c_param_bound():
        try:
            got = g.scalar("MATCH (a:Chain {i:0})-[:P*1..$n]->(b:Chain) RETURN count(DISTINCT b)", {"n": 10})
            return f"parameter hop bound ACCEPTED (returned {got}); we still use hops() literals"
        except Exception as e:
            return f"parameter hop bound REJECTED ({type(e).__name__}); hops() literals are required"

    def c_copy():
        fork = g.copy_to(FORK)
        try:
            fork.query("MATCH (n:Chain) DETACH DELETE n")
            live = g.scalar("MATCH (n:Chain) RETURN count(n)")
            forked = fork.scalar("MATCH (n:Chain) RETURN count(n)")
            assert live == 30 and forked == 0, f"live={live} fork={forked}"
            try:
                idx = len(fork.rows("CALL db.indexes()"))
            except Exception as e:
                idx = f"n/a ({type(e).__name__})"
        finally:
            fork.delete()
        return f"GRAPH.COPY works; mutating the fork left live untouched; indexes visible on fork: {idx}"

    def c_cross_join():
        g.query("CREATE (a:Dom {id:'a'}), (b:Dom {id:'b'}), "
                "(s1:Sto {id:'s1'})-[:IN]->(a), (s2:Sto {id:'s2'})-[:IN]->(b)")
        g.query("MATCH (s1:Sto {id:'s1'}), (s2:Sto {id:'s2'}) "
                "CREATE (i1:It {id:'i1'})-[:ON]->(s1), (i2:It {id:'i2'})-[:ON]->(s1), "
                "(c1:Cp {id:'c1'})-[:OF]->(i1), (c1)-[:ON]->(s2), "
                "(c2:Cp {id:'c2'})-[:OF]->(i2), (c2)-[:ON]->(s1)")
        joined = ("MATCH (c:Cp)-[:OF]->(i:It) MATCH (i)-[:ON]->(:Sto)-[:IN]->(d:Dom) "
                  "MATCH (c)-[:ON]->(:Sto)-[:IN]->(cd:Dom) ")
        equality_rows = g.rows(joined + "RETURN c.id, i.id, d.id, cd.id")
        assert len(equality_rows) == 2, f"equality-only join returned {len(equality_rows)} rows, expected 2"
        rows = g.rows(joined + "WHERE cd.id <> d.id RETURN c.id, i.id")
        assert [tuple(r) for r in rows] == [("c1", "i1")], (
            f"inequality across joined patterns returned {len(rows)} rows, expected exactly (c1, i1)")
        return "inequality across joined patterns is sound on this tag"

    def c_failclosed():
        try:
            connect(port=1)
        except GraphUnavailable:
            return "connect() to a dead port raises GraphUnavailable (callers fail closed)"
        raise AssertionError("no exception for a dead port")

    run("parameterised queries", c_params)
    run("UNWIND/MERGE loader pattern + `class` property", c_unwind)
    run("index creation", c_index)
    run("variable-length paths (literal bounds)", c_varlen)
    run("zero-length lower bound (*0..N)", c_zero_hop, critical=False)
    run("hop bound as parameter", c_param_bound, critical=False)
    run("GRAPH.COPY fork isolation", c_copy)
    run("cross-pattern inequality join", c_cross_join, critical=False)
    run("fail-closed when DB unreachable", c_failclosed)
    g.delete()

    lines = [
        "# GROUND-TRUTH: what the pinned FalkorDB image actually does", "",
        f"- Run at: {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}",
        f"- Image tag (pinned): `falkordb/falkordb:{config.FALKORDB_TAG}`",
        f"- Server version reported: `{version}`", f"- Modules: `{modules}`",
        f"- Host port: `127.0.0.1:{config.FALKORDB_PORT}`; persistence via `REDIS_ARGS=--save 60 1 --appendonly yes` "
        "(restart persistence is not exercised by this script)", "",
        "| Status | Check | Detail |", "|---|---|---|",
    ]
    lines += [f"| {s} | {n} | {d.replace('|', '/')} |" for s, n, d in results]
    lines += ["", "## Rules we derive from this",
              "- All Cypher is a constant plus parameters; hop bounds come from `graphdb.hops()` (validated ints).",
              "- Loader writes are `UNWIND $rows ... MERGE` in batches of 200 (idempotent).",
              "- What-if forks use `GRAPH.COPY` and are deleted after use; the live graph is never mutated.",
              "- Joins are equality-only in Cypher; any comparison between two joined patterns is made in Python on returned values."]
    out = Path(__file__).resolve().parent.parent / "docs" / "GROUND-TRUTH.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    for s, n, d in results:
        print(f"[{s}] {n}: {d}")
    print(f"\nwrote {out}")
    return 1 if any(s == "FAIL" for s, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
