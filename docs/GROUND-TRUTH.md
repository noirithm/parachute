# GROUND-TRUTH: what the pinned FalkorDB image actually does

- Run at: 2026-10-16T18:35:05+00:00
- Image tag (pinned): `falkordb/falkordb:6.0.0`
- Server version reported: `8.10.2`
- Modules: `[['name', 'graph', 'ver', 60000, 'path', '/var/lib/falkordb/bin/falkordb.so', 'args', ['MAX_QUEUED_QUERIES', '25', 'TIMEOUT', '1000', 'RESULTSET_SIZE', '10000']], ['name', 'vectorset', 'ver', 1, 'path', '', 'args', []]]`
- Host port: `127.0.0.1:6390`; persistence via `REDIS_ARGS=--save 60 1 --appendonly yes` (restart persistence is not exercised by this script)

| Status | Check | Detail |
|---|---|---|
| PASS | parameterised queries | hostile string round-trips as data via parameters |
| PASS | UNWIND/MERGE loader pattern + `class` property | UNWIND+MERGE with list-of-maps params is idempotent; property named `class` works |
| PASS | index creation | index created; plan mentions an index scan: True |
| PASS | variable-length paths (literal bounds) | literal hop bounds from validated ints OK: *1..10=10, *1..50=29 |
| PASS | zero-length lower bound (*0..N) | *0..10 returned 11 (expected 11); loader avoids *0.. anyway |
| PASS | hop bound as parameter | parameter hop bound REJECTED (ResponseError); hops() literals are required |
| PASS | GRAPH.COPY fork isolation | GRAPH.COPY works; mutating the fork left live untouched; indexes visible on fork: 1 |
| PASS | cross-pattern inequality join | inequality across joined patterns is sound on this tag |
| PASS | fail-closed when DB unreachable | connect() to a dead port raises GraphUnavailable (callers fail closed) |

## Rules we derive from this
- All Cypher is a constant plus parameters; hop bounds come from `graphdb.hops()` (validated ints).
- Loader writes are `UNWIND $rows ... MERGE` in batches of 200 (idempotent).
- What-if forks use `GRAPH.COPY` and are deleted after use; the live graph is never mutated.
- Joins are equality-only in Cypher; any comparison between two joined patterns is made in Python on returned values.
