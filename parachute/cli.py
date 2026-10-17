"""python -m parachute.cli {wait|generate|load|summary|drop|demo}"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .graphdb import GraphClient, GraphUnavailable, connect
from .loader import load_world
from .queries import world_summary
from .engine import audit
from .engine.executor import execute
from .engine.layout import Layout
from .engine.oracle import RestoreOracle
from .engine.planner import plan as make_plan
from .engine.policy import preflight, replay
from .engine.resolver import resolve
from .engine.whatif import analyze, whatif as run_whatif
from .scan import scan_world
from .world.generator import generate_world


def _graph_for(root: str) -> GraphClient:
    sid = json.loads((Path(root) / ".parachute" / "world.json").read_text())["session"]
    return GraphClient(f"parachute_{sid}")


def _brief(result: dict) -> dict:
    res = result["resolution"]
    out = {"resolution": {"forced_verdict": res["forced_verdict"], "reasons": res["reasons"],
                          "destroyed": {k: (len(v) if k == "paths" else v) for k, v in res["destroyed"].items() if v}},
           "whatif": None}
    if result["whatif"]:
        outcome = dict(result["whatif"]["outcome"])
        counts: dict = {}
        for r in outcome.pop("recoverable"):
            key = f"{r['strength']} via {r['via']}"
            counts[key] = counts.get(key, 0) + 1
        outcome["recoverable"] = counts
        out["whatif"] = {**result["whatif"], "outcome": outcome}
    return out


def _view(v: dict) -> dict:
    out = {"id": v["id"], "verdict": v["verdict"], "degraded": bool(v["degraded"]), "reasons": v["reasons"]}
    if v["outcome"]:
        o = v["outcome"]
        out["at_risk"] = {"lost": len(o["lost"]), "fragile": len(o["fragile"]), "lost_commits": len(o["lost_commits"]),
                          "fragile_commits": len(o["fragile_commits"]), "recoverable": len(o["recoverable"])}
    if v["plan"] and v["plan"]["fixes"]:
        out["fixes"] = [{"id": f["id"], "kind": f["kind"], "cost": f["cost"]} for f in v["plan"]["fixes"]]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="parachute")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wait").add_argument("--timeout", type=int, default=60)
    sub.add_parser("generate").add_argument("--root")
    p = sub.add_parser("load")
    p.add_argument("root")
    p.add_argument("--allow-outside", action="store_true", help="explicit opt-in to follow paths outside the sandbox")
    sub.add_parser("summary").add_argument("root")
    sub.add_parser("drop").add_argument("root")
    w = sub.add_parser("whatif")
    w.add_argument("root")
    w.add_argument("command")
    w.add_argument("--cwd", help="working directory, relative to the sandbox root")
    pl = sub.add_parser("plan")
    pl.add_argument("root")
    pl.add_argument("command")
    pl.add_argument("--cwd", help="working directory, relative to the sandbox root")
    for name in ("preflight", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("root")
        cmd.add_argument("command")
        cmd.add_argument("--cwd", help="working directory, relative to the sandbox root")
    rp = sub.add_parser("replay")
    rp.add_argument("root")
    rp.add_argument("verdict_id")
    sub.add_parser("audit").add_argument("root")
    sub.add_parser("demo")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "wait":
            deadline, err = time.time() + a.timeout, None
            while time.time() < deadline:
                try:
                    connect()
                    print("FalkorDB is up")
                    return 0
                except GraphUnavailable as e:
                    err = e
                    time.sleep(1)
            print(f"FalkorDB did not come up: {err}", file=sys.stderr)
            return 1
        if a.cmd == "generate":
            sb = generate_world(a.root)
            print(f"sandbox: {sb.root}\nsession: {sb.session_id}")
        elif a.cmd == "load":
            stats = load_world(_graph_for(a.root), scan_world(a.root, a.allow_outside), reset=True)
            print(json.dumps(stats, indent=2))
        elif a.cmd == "summary":
            print(json.dumps(world_summary(_graph_for(a.root)), indent=2))
        elif a.cmd == "drop":
            _graph_for(a.root).delete()
            print("graph deleted")
        elif a.cmd == "whatif":
            cwd = Path(a.root, a.cwd) if a.cwd else Path(a.root, "disk_a", "projects")
            result = analyze(_graph_for(a.root), Layout.load(a.root), a.command, str(cwd))
            print(json.dumps(_brief(result), indent=2))
        elif a.cmd == "plan":
            cwd = Path(a.root, a.cwd) if a.cwd else Path(a.root, "disk_a", "projects")
            graph, layout = _graph_for(a.root), Layout.load(a.root)
            res = resolve(a.command, str(cwd), layout)
            if res.forced_verdict or res.destroyed.is_empty():
                print(json.dumps({"forced_verdict": res.forced_verdict, "reasons": res.reasons}, indent=2))
            else:
                base = run_whatif(graph, res.destroyed, res.command)
                print(json.dumps(make_plan(graph, layout, res.destroyed, base, res.command), indent=2))
        elif a.cmd in ("preflight", "run"):
            cwd = Path(a.root, a.cwd) if a.cwd else Path(a.root, "disk_a", "projects")
            graph, layout = _graph_for(a.root), Layout.load(a.root)
            verdict = preflight(graph, layout, a.command, str(cwd))
            out = {"verdict": _view(verdict)}
            if a.cmd == "run" and verdict["verdict"] in ("GO", "FIX_THEN_GO"):
                out["execution"] = execute(graph, layout, verdict, oracle=RestoreOracle(layout.root))
            print(json.dumps(out, indent=2, default=str))
        elif a.cmd == "replay":
            print(json.dumps(replay(_graph_for(a.root), Layout.load(a.root), a.verdict_id), indent=2))
        elif a.cmd == "audit":
            print(json.dumps(audit.list_verdicts(_graph_for(a.root)), indent=2))
        elif a.cmd == "demo":
            sb = generate_world()
            g = GraphClient(sb.graph_name)
            load_world(g, scan_world(sb.root), reset=True)
            print(f"sandbox: {sb.root}\ngraph:   {sb.graph_name}\n")
            print(json.dumps(world_summary(g), indent=2))
    except GraphUnavailable as e:
        print(f"DEGRADED: {e} (fail closed: every verdict would be STOP_AND_ASK)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
