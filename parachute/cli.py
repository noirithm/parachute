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
from .scan import scan_world
from .world.generator import generate_world


def _graph_for(root: str) -> GraphClient:
    sid = json.loads((Path(root) / ".parachute" / "world.json").read_text())["session"]
    return GraphClient(f"parachute_{sid}")


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
