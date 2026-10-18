"""Executor: run the planned fixes for real, rescan, reload, re-verify, then run the original command."""
from __future__ import annotations

import json
import os
import subprocess

from ..graphdb import GraphClient
from ..loader import load_world
from ..scan import scan_world
from . import audit
from .fixes import Fix, FixFailed, run_fix
from .layout import Layout
from .policy import FIX_THEN_GO, GO, preflight

ALLOWED = (GO, FIX_THEN_GO)
OUTPUT_LIMIT = 4000


def reload_world(live: GraphClient, layout: Layout) -> None:
    load_world(live, scan_world(layout.root), reset=True)
    audit.relink(live)


def claims_from(outcome) -> dict:
    keys = ("lost", "fragile", "recoverable", "lost_commits", "fragile_commits", "recoverable_commits")
    if not outcome:
        return {k: [] for k in keys}
    return {"lost": sorted(e["id"] for e in outcome["lost"]),
            "fragile": sorted(e["id"] for e in outcome["fragile"]),
            "recoverable": sorted(e["id"] for e in outcome["recoverable"]),
            "lost_commits": sorted(c["sha"] for c in outcome["lost_commits"]),
            "fragile_commits": sorted(c["sha"] for c in outcome["fragile_commits"]),
            "recoverable_commits": sorted(outcome["recoverable_commit_shas"])}


def run_shell(layout: Layout, cwd: str, command: str, env: dict, timeout: int) -> dict:
    environ = {"HOME": layout.home, "USER": "dev", "LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               **env}
    try:
        r = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], cwd=cwd, env=environ,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"returncode": None, "timeout": True, "stdout": "", "stderr": ""}
    return {"returncode": r.returncode, "stdout": r.stdout[-OUTPUT_LIMIT:], "stderr": r.stderr[-OUTPUT_LIMIT:]}


def execute(live: GraphClient, layout: Layout, verdict: dict, oracle=None, timeout: int = 60) -> dict:
    vid = verdict["id"]
    result = {"verdict_id": vid, "status": None, "detail": "", "fixes": [], "command": None, "oracle": None}

    def finish(status: str, detail: str = "", audited: bool = True) -> dict:
        result.update(status=status, detail=detail)
        if audited:
            audit.set_status(live, vid, status, detail)
        return result

    if verdict["verdict"] not in ALLOWED or verdict.get("degraded"):
        return finish("refused", f"verdict is {verdict['verdict']}", audited=False)
    # Refresh from the actual sandbox before trusting the graph snapshot.
    # This catches filesystem changes that occurred after preflight.
    try:
        reload_world(live, layout)
    except Exception as exc:
        return finish(
            "verification_failed",
            f"could not refresh the sandbox before execution: {type(exc).__name__}: {exc}",
            audited=False,
        )

    if audit.fingerprint(live) != verdict["hashes"]["world"]:
        return finish("stale", "the world changed since this verdict was issued; run preflight again")

    cwd, env, outcome = os.path.join(layout.root, verdict["cwd"]), verdict["env"], verdict["outcome"]
    fixes = (verdict["plan"] or {}).get("fixes", [])
    execution_world_fp = verdict["hashes"]["world"]
    for d in fixes:
        step = f"{vid}:{d['id']}"
        try:
            info = run_fix(Fix.from_dict(d), layout)
        except FixFailed as e:
            audit.fix_status(live, step, "failed", str(e))
            result["fixes"].append({"id": d["id"], "kind": d["kind"], "status": "failed", "error": str(e)})
            return finish("fix_failed", str(e))
        audit.fix_status(live, step, "ok", json.dumps(info))
        result["fixes"].append({"id": d["id"], "kind": d["kind"], "status": "ok", "result": info})
    if fixes:
        reload_world(live, layout)
        recheck = preflight(live, layout, verdict["command"], cwd, env, record=False)
        if recheck["verdict"] != GO:
            return finish("verification_failed", "; ".join(recheck["reasons"]) or recheck["verdict"])
        outcome = recheck["outcome"]
        execution_world_fp = recheck["hashes"]["world"]

    # Refresh once more immediately before the original command. A changed
    # world invalidates the authorization; never execute against a stale plan.
    try:
        reload_world(live, layout)
    except Exception as exc:
        return finish(
            "verification_failed",
            f"could not refresh the sandbox before the command: {type(exc).__name__}: {exc}",
            audited=False,
        )

    if audit.fingerprint(live) != execution_world_fp:
        return finish("stale", "the world changed after verification; run preflight again")

    snapshot = claims = None
    if oracle is not None:
        snapshot, claims = oracle.snapshot(), claims_from(outcome)
        oracle.save(vid, snapshot, claims)
    result["command"] = run_shell(layout, cwd, verdict["command"], env, timeout)

    try:
        reload_world(live, layout)
    except Exception as exc:
        return finish(
            "post_execution_refresh_failed",
            f"command was attempted, but the resulting world could not be refreshed: "
            f"{type(exc).__name__}: {exc}",
            audited=False,
        )

    if oracle is not None:
        result["oracle"] = oracle.verify(snapshot, claims)

    command_result = result["command"]
    if command_result.get("timeout"):
        return finish("timed_out", f"command exceeded the {timeout}s timeout")
    if command_result.get("returncode") != 0:
        return finish(
            "command_failed",
            json.dumps({
                "returncode": command_result.get("returncode"),
                "stderr": command_result.get("stderr", ""),
            }),
        )

    return finish("executed", json.dumps({"returncode": 0}))
