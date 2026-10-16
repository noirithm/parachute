"""Thin FalkorDB wrapper.

Rules enforced here:
* every query is a hand-written constant plus a params dict (never built from user/model text);
* the ONLY string-built Cypher allowed is a variable-length hop bound from `hops()` (validated int);
* if FalkorDB is unreachable we raise GraphUnavailable and callers must FAIL CLOSED (STOP_AND_ASK).
"""
from __future__ import annotations

import re

import redis
from falkordb import FalkorDB

from . import config

# NOTE: ResponseError (bad Cypher) is deliberately NOT here: a syntax error is a bug, not an outage.
CONN_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class GraphUnavailable(RuntimeError):
    """FalkorDB cannot be reached. There is no in-memory fallback: verdict DEGRADED -> STOP_AND_ASK."""


def hops(n: int) -> str:
    """Variable-length bound as a validated literal, e.g. '1..25' (hop bounds can't be parameters)."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 10_000:
        raise ValueError(f"hop bound must be an int in 1..10000, got {n!r}")
    return f"1..{n}"


def connect(host: str | None = None, port: int | None = None) -> FalkorDB:
    try:
        db = FalkorDB(host=host or config.FALKORDB_HOST, port=port or config.FALKORDB_PORT,
                      socket_connect_timeout=2, socket_timeout=30)
        db.connection.ping()
        return db
    except CONN_ERRORS as e:
        raise GraphUnavailable(f"FalkorDB unreachable: {e}") from e


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid graph name {name!r}")
    return name


class GraphClient:
    def __init__(self, name: str, db: FalkorDB | None = None):
        self.name = _check_name(name)
        self.db = db or connect()
        self._g = self.db.select_graph(self.name)

    def query(self, cypher: str, params: dict | None = None):
        try:
            return self._g.query(cypher, params or {})
        except CONN_ERRORS as e:
            raise GraphUnavailable(f"FalkorDB unreachable: {e}") from e

    def rows(self, cypher: str, params: dict | None = None) -> list:
        return self.query(cypher, params).result_set

    def scalar(self, cypher: str, params: dict | None = None):
        r = self.rows(cypher, params)
        return r[0][0] if r else None

    def copy_to(self, dest: str) -> "GraphClient":
        """Server-side copy of this graph (GRAPH.COPY)."""
        try:
            self.db.connection.execute_command("GRAPH.COPY", self.name, _check_name(dest))
        except CONN_ERRORS as e:
            raise GraphUnavailable(f"FalkorDB unreachable: {e}") from e
        return GraphClient(dest, self.db)

    def exists(self) -> bool:
        try:
            return self.name in self.db.list_graphs()
        except CONN_ERRORS as e:
            raise GraphUnavailable(str(e)) from e

    def delete(self) -> None:
        try:
            self._g.delete()
        except redis.exceptions.ResponseError:
            pass  # graph did not exist
        except CONN_ERRORS as e:
            raise GraphUnavailable(str(e)) from e
