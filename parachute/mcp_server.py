"""Thin MCP adapter over the shared Parachute service."""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from .graphdb import GraphUnavailable
from .service import (
    ParachuteError, apply_fixes, audit_backups, audit_history,
    explain, preflight, replay_verdict, restore_test, run_command,
    world_summary,
)

mcp = FastMCP("Parachute")


def _result(fn, *args, **kwargs) -> str:
    try:
        return json.dumps(fn(*args, **kwargs), indent=2, default=str)
    except GraphUnavailable:
        return json.dumps({
            "error": "FalkorDB unavailable",
            "verdict": "STOP_AND_ASK",
            "execution_allowed": False,
        })
    except (ValueError, ParachuteError, OSError, KeyError) as exc:
        return json.dumps({"error": str(exc), "execution_allowed": False})


@mcp.tool()
def parachute_world_summary(root: str) -> str:
    """Summarize recoverability and phantom backups from FalkorDB."""
    return _result(world_summary, root)


@mcp.tool()
def parachute_preflight(
    root: str, command: str, cwd: str = "",
    env: dict[str, str] | None = None,
) -> str:
    """Analyze a command without executing it."""
    return _result(preflight, root, command, cwd or None, env)


@mcp.tool()
def parachute_explain(root: str, verdict_id: str) -> str:
    """Explain a stored verdict and return supporting graph evidence."""
    return _result(explain, root, verdict_id)


@mcp.tool()
def parachute_apply_fixes(root: str, verdict_id: str) -> str:
    """Apply only the verified fixes associated with a stored verdict."""
    return _result(apply_fixes, root, verdict_id)


@mcp.tool()
def parachute_restore_test(root: str, verdict_id: str) -> str:
    """Run the independent restore oracle for a saved execution snapshot."""
    return _result(restore_test, root, verdict_id)


@mcp.tool()
def parachute_audit_backups(path: str) -> str:
    """Inspect backup copies and phantom-backup findings for a sandbox path."""
    return _result(audit_backups, path)


@mcp.tool()
def parachute_run(
    root: str, command: str, cwd: str = "",
    timeout: int = 60, env: dict[str, str] | None = None,
) -> str:
    """Request preflight-gated execution; use only with a disposable local sandbox."""
    return _result(run_command, root, command, cwd or None, env, timeout)


@mcp.tool()
def parachute_audit(root: str) -> str:
    """List stored verdicts and their execution statuses."""
    return _result(audit_history, root)


@mcp.tool()
def parachute_replay(root: str, verdict_id: str) -> str:
    """Re-evaluate a saved verdict against the current graph."""
    return _result(replay_verdict, root, verdict_id)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
