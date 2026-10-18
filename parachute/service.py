"""Shared service layer. FalkorDB and deterministic policy remain authoritative."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import audit
from .engine.executor import execute, reload_world
from .engine.fixes import Fix, FixFailed, run_fix
from .engine.layout import Layout
from .engine.oracle import RestoreOracle
from .engine.policy import preflight as engine_preflight
from .engine.policy import replay as engine_replay
from .graphdb import GraphClient
from .guard import assert_safe_root
from .queries import world_summary as graph_world_summary


class ParachuteError(RuntimeError):
    """A request cannot safely be completed."""


def open_sandbox(root: str) -> tuple[GraphClient, Layout]:
    safe_root = assert_safe_root(root)
    layout = Layout.load(safe_root)
    manifest = safe_root / ".parachute" / "world.json"

    try:
        session = json.loads(manifest.read_text(encoding="utf-8"))["session"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ParachuteError("Invalid Parachute world manifest") from exc

    if not isinstance(session, str) or not session.isalnum():
        raise ParachuteError("Invalid session identifier")
    return GraphClient(f"parachute_{session}"), layout


def _cwd(layout: Layout, cwd: str | None) -> Path:
    if cwd:
        value = Path(cwd)
        path = (value if value.is_absolute() else Path(layout.root) / value).resolve()
    else:
        path = (Path(layout.root) / "disk_a" / "projects").resolve()

    if not layout.inside(str(path)) or not path.is_dir():
        raise ValueError("cwd must be an existing directory inside the sandbox")
    return path


def _validate_command(command: str, env: dict[str, str] | None) -> None:
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ValueError("command must be a non-empty string")

    if env is not None:
        if not isinstance(env, dict):
            raise ValueError("env must be a dictionary")
        for key, value in env.items():
            if (not isinstance(key, str) or not key or "=" in key or "\0" in key
                    or not isinstance(value, str) or "\0" in value):
                raise ValueError("env must contain valid string KEY=VALUE pairs")


def preflight(
    root: str,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve and analyze a command; this function never executes it."""
    _validate_command(command, env)
    graph, layout = open_sandbox(root)
    return engine_preflight(graph, layout, command, str(_cwd(layout, cwd)), env=env)


def explain(root: str, verdict_id: str) -> dict[str, Any]:
    """Return the stored verdict, recovery justification and adjacent graph evidence."""
    graph, _ = open_sandbox(root)
    rec = audit.load(graph, verdict_id)
    if rec is None:
        raise KeyError(f"Unknown verdict: {verdict_id}")

    evidence = graph.rows(
        """
        MATCH (v:Verdict {id: $id})-[:WOULD_LOSE]->(i:Item)
        OPTIONAL MATCH (i)-[r]-(n)
        RETURN i.id, type(r), labels(n), coalesce(n.id, n.sha, n.path),
               properties(r)
        ORDER BY i.id
        """,
        {"id": verdict_id},
    )
    outcome = rec.get("outcome") or {}
    return {
        "verdict_id": verdict_id,
        "verdict": rec["verdict"],
        "status": rec.get("status"),
        "command": rec["command"],
        "reasons": rec.get("reasons", []),
        "resolution": rec.get("resolution"),
        "lost": outcome.get("lost", []),
        "fragile": outcome.get("fragile", []),
        "recoverable": outcome.get("recoverable", []),
        "graph_evidence": [
            {
                "item": row[0],
                "relationship": row[1],
                "neighbor_labels": row[2],
                "neighbor": row[3],
                "relationship_properties": row[4],
            }
            for row in evidence
        ],
    }


def apply_fixes(root: str, verdict_id: str) -> dict[str, Any]:
    """Apply only a stored verdict's planned fixes; never run its original command."""
    graph, layout = open_sandbox(root)
    rec = audit.load(graph, verdict_id)
    if rec is None:
        raise KeyError(f"Unknown verdict: {verdict_id}")
    if rec["verdict"] != "FIX_THEN_GO" or rec.get("degraded"):
        raise ParachuteError("Only a non-degraded FIX_THEN_GO verdict can apply fixes")

    fixes = (rec.get("plan") or {}).get("fixes", [])
    if not fixes:
        raise ParachuteError("Verdict has no planned fixes")

    results = []
    for data in fixes:
        step_id = f"{verdict_id}:{data['id']}"
        try:
            result = run_fix(Fix.from_dict(data), layout)
        except FixFailed as exc:
            audit.fix_status(graph, step_id, "failed", str(exc))
            audit.set_status(graph, verdict_id, "fix_failed", str(exc))
            return {
                "verdict_id": verdict_id,
                "status": "fix_failed",
                "detail": str(exc),
                "fixes": results,
            }

        audit.fix_status(graph, step_id, "ok", json.dumps(result))
        results.append({"id": data["id"], "kind": data["kind"], "result": result})

    reload_world(graph, layout)
    cwd = str(Path(layout.root) / rec["cwd"])
    check = engine_preflight(
        graph, layout, rec["command"], cwd, rec.get("env"), record=False
    )
    status = "fixes_verified" if check["verdict"] == "GO" and not check.get("degraded") else "verification_failed"
    audit.set_status(
        graph, verdict_id, status,
        "; ".join(check.get("reasons", [])) or check["verdict"],
    )
    return {
        "verdict_id": verdict_id,
        "status": status,
        "recheck_verdict": check["verdict"],
        "reasons": check.get("reasons", []),
        "fixes": results,
    }


def restore_test(root: str, verdict_id: str) -> dict[str, Any]:
    """Run the independent restore oracle from the saved pre-action snapshot."""
    _, layout = open_sandbox(root)
    saved = Path(layout.root) / ".parachute" / "oracle" / f"{verdict_id}.json"
    if not saved.is_file():
        raise ValueError("No saved restore-oracle snapshot for this verdict")
    return RestoreOracle(layout.root).run_saved(verdict_id)


def audit_backups(path: str) -> dict[str, Any]:
    """Audit backup copies in a sandbox or a directory inside that sandbox."""
    requested = Path(path).resolve()
    root = next(
        (p for p in (requested, *requested.parents)
         if (p / ".parachute" / "world.json").is_file()),
        None,
    )
    if root is None:
        raise ValueError("path must be a sandbox root or a directory inside one")

    graph, layout = open_sandbox(str(root))
    rel = "." if requested == root else requested.relative_to(root).as_posix()
    rows = graph.rows(
        """
        MATCH (c:Copy)-[:STORED_ON]->(s:Store)
        RETURN c.id, c.kind, c.file, c.alive, s.path, s.domain
        ORDER BY c.id
        """
    )
    copies = []
    for copy_id, kind, file, alive, store_path, domain in rows:
        store = str(store_path or "")
        if rel != "." and store != rel and not store.startswith(rel.rstrip("/") + "/"):
            continue
        copies.append({
            "id": copy_id, "kind": kind, "file": file,
            "alive": bool(alive), "store": store, "domain": domain,
        })

    summary = graph_world_summary(graph)
    return {
        "sandbox": layout.root,
        "path": rel,
        "copies": copies,
        "phantom_backups": summary["phantom_backups"],
    }


def world_summary(root: str) -> dict[str, Any]:
    graph, _ = open_sandbox(root)
    return graph_world_summary(graph)


def check_command(root, command, cwd=None, env=None):
    """Compatibility alias for older API/agent callers."""
    return preflight(root, command, cwd, env)


def run_command(
    root: str,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Preflight and execute only when deterministic policy authorizes it."""
    _validate_command(command, env)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ValueError("timeout must be an integer from 1 to 300")

    graph, layout = open_sandbox(root)
    verdict = engine_preflight(graph, layout, command, str(_cwd(layout, cwd)), env=env)
    result: dict[str, Any] = {"verdict": verdict, "execution": None}

    if verdict["verdict"] not in ("GO", "FIX_THEN_GO") or verdict.get("degraded"):
        result["execution"] = {
            "status": "refused",
            "detail": "Parachute did not authorize execution",
        }
        return result

    result["execution"] = execute(
        graph, layout, verdict, oracle=RestoreOracle(layout.root), timeout=timeout
    )
    return result


def audit_history(root: str) -> list[dict[str, Any]]:
    graph, _ = open_sandbox(root)
    return audit.list_verdicts(graph)


def replay_verdict(root: str, verdict_id: str) -> dict[str, Any]:
    graph, layout = open_sandbox(root)
    return engine_replay(graph, layout, verdict_id)
