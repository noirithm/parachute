"""Fix planner: greedy minimum-cost cover.

Coverage is measured, not assumed: each candidate is applied to a fork and the real fixpoint says which lost
or fragile targets it removes. Adding fixes only adds routes, so the union of individual coverages is a lower
bound for the combined plan; the plan is still re-verified on a fresh fork before it counts as verified.
"""
from __future__ import annotations

from .effects import merge
from .fixes import KIND_ORDER, candidates, effects
from .layout import Layout
from .model import Destroyed
from .whatif import whatif


def targets(outcome: dict) -> set:
    found = {("item", e["id"]) for e in outcome["lost"] + outcome["fragile"]}
    return found | {("commit", c["sha"]) for c in outcome["lost_commits"] + outcome["fragile_commits"]}


def _pretty(keys) -> list:
    return [{"kind": kind, "id": ident} for kind, ident in sorted(keys)]


def greedy(pool: list, coverage: dict, uncovered: set):
    chosen, left, rest = [], set(uncovered), list(pool)
    while left:
        best = None
        for fx in rest:
            gain = coverage[fx.id] & left
            if not gain:
                continue
            score = (fx.cost / len(gain), -len(gain), KIND_ORDER[fx.kind], fx.id)
            if best is None or score < best[0]:
                best = (score, fx, gain)
        if best is None:
            break
        _, fx, gain = best
        chosen.append(fx)
        left -= gain
        rest.remove(fx)
    return chosen, left


def prune(chosen: list, coverage: dict) -> list:
    needed = set().union(*(coverage[f.id] for f in chosen)) if chosen else set()
    kept = list(chosen)
    for fx in sorted(chosen, key=lambda f: (-f.cost, f.id)):
        rest = [f for f in kept if f is not fx]
        if rest and needed <= set().union(*(coverage[f.id] for f in rest)):
            kept = rest
    return kept


def plan(live, layout: Layout, destroyed: Destroyed, base: dict, label: str, max_hops: int | None = None) -> dict:
    uncovered = targets(base["outcome"])
    if not uncovered:
        return {"needed": 0, "fixes": [], "cost": 0, "verified": True, "unfixable": [], "candidates": []}
    pool = list({fx.id: fx for fx in candidates(layout, destroyed, base["outcome"])}.values())
    coverage = {}
    for fx in pool:
        after = whatif(live, destroyed, f"{label}#{fx.id}", max_hops, effects=effects(fx, layout))
        coverage[fx.id] = uncovered - targets(after["outcome"])
    chosen, left = greedy(pool, coverage, uncovered)
    fixes = prune(chosen, coverage)
    remaining = set(left)
    if fixes:
        verify = whatif(live, destroyed, f"{label}#plan", max_hops,
                        effects=merge([effects(f, layout) for f in fixes]))
        remaining = targets(verify["outcome"])
    return {
        "needed": len(uncovered),
        "fixes": [f.to_dict() for f in fixes],
        "cost": sum(f.cost for f in fixes),
        "verified": bool(fixes) and not remaining,
        "unfixable": _pretty(remaining),
        "candidates": [{"id": f.id, "kind": f.kind, "cost": f.cost, "covers": len(coverage[f.id])} for f in pool],
    }
