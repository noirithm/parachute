"""Renders the Cypher reference in README.md straight from the query constants, so the docs cannot drift."""
from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from . import config
from . import queries as q
from .engine import fixpoint as fx
from .engine import audit as au
from .engine import effects as ef
from .engine import whatif as wi

README = Path(__file__).resolve().parent.parent / "README.md"
START, END = "<!-- queries:start -->", "<!-- queries:end -->"
FENCE = "`" * 3
INTRO = ("All Cypher is constant text plus parameters. The only built string is a variable-length hop bound, "
         "produced from a validated integer (`graphdb.hops`). Every answer carries the short hash shown next to "
         "its query, so a result can be tied to the exact text that produced it. This section is generated from "
         "the code (`make docs`) and a test fails if it drifts.")

SUMMARY = "World summary"
FIXPOINT = "Recoverability fixpoint (runs on the fork)"
FORK = "What-if fork"
AUDIT = "Audit trail (live graph)"
EFFECTS = "Fix effects (applied to the fork)"
JUSTIFY = ("Justification is read back from the nodes the rounds wrote, "
           "so the explanation cannot disagree with the verdict.")
FORK_ONLY = "Applied to the fork only; the live graph is never written."

Entry = namedtuple("Entry", "group name text why")


def entries(max_hops: int | None = None) -> list:
    n = max_hops or config.MAX_HOPS
    s, f, w = SUMMARY, FIXPOINT, FORK
    return [
        Entry(s, "counts_by_class", q.Q_COUNTS_BY_CLASS, "Baseline aggregate over the class stored on every Item."),
        Entry(s, "items", q.Q_ITEMS, "The universe the route queries are subtracted from."),
        Entry(s, "rebuildable_by_recipe", q.Q_ROUTE_RECIPE, "Rebuildable is an edge to a Recipe node, not a column on the item."),
        Entry(s, "copy_placement", q.Q_COPY_PLACEMENT,
              "A copy's placement against its original is two Store-to-Domain paths compared side by side."),
        Entry(s, "phantom_same_domain", q.Q_PHANTOM_SAME_DOMAIN,
              "A same-disk backup is two paths that meet at the same Domain node."),
        Entry(s, "needs", q.Q_NEEDS, "A backup depends on other items (keys), which is an edge to follow, not a flag."),
        Entry(s, "copy_covers", q.Q_COPY_COVERS, "What each copy actually covers is read straight from COPY_OF."),
        Entry(s, "route_git_direct", q.Q_ROUTE_GIT_DIRECT,
              "Pushed means a remote Ref points at a commit that tracks this exact blob."),
        Entry(s, "route_git_via_parents", q.q_route_git_via_parents(n),
              "Reachability from a remote branch is a variable-length path over PARENT."),
        Entry(s, "commits_all", q.Q_COMMITS_ALL, "The commit set that the remote-reachable set is subtracted from."),
        Entry(s, "commits_remote_direct", q.Q_COMMITS_REMOTE_DIRECT, "Commits a remote branch points at directly."),
        Entry(s, "commits_remote_via_parents", q.q_commits_remote_via_parents(n),
              "Commits a remote branch reaches by walking PARENT links."),
        Entry(f, "init_items", fx.Q_INIT_ITEMS,
              "Rank and route label live on the nodes, so every later round is a plain write query."),
        Entry(f, "init_commits", fx.Q_INIT_COMMITS, "Commits start with no holder and gain one from refs."),
        Entry(f, "seed_durable", fx.Q_SEED_DURABLE, "Remote branches and off-domain bundles are the only DURABLE holders of commits."),
        Entry(f, "seed_local", fx.Q_SEED_LOCAL,
              "Local refs, stashes and worktree HEADs hold commits only until the ref goes away (WEAK)."),
        Entry(f, "propagate", fx.q_propagate(n),
              "Strength flows down PARENT paths from the holder to every ancestor commit."),
        Entry(f, "route_git", fx.Q_ROUTE_GIT, "An item is recoverable if a surviving commit contains its exact blob."),
        Entry(f, "route_copy", fx.Q_ROUTE_COPY,
              "AND over the copy's NEEDS and OR over copies, expressed as one grouped pattern."),
        Entry(f, "route_recipe", fx.Q_ROUTE_RECIPE, "A rebuild is only as strong as the weakest of its inputs."),
        Entry(f, "rank_sum", fx.Q_RANK_SUM, "Rounds repeat until this total stops changing."),
        Entry(f, "destroyed_items", fx.Q_DESTROYED_ITEMS, "Reads the outcome from the nodes the rounds wrote."),
        Entry(f, "affected_commits", fx.Q_AFFECTED_COMMITS, "Commits that lost a holder, plus their ancestors."),
        Entry(f, "justify_state", fx.Q_JUSTIFY_STATE, JUSTIFY),
        Entry(f, "justify_copy", fx.Q_JUSTIFY_COPY, JUSTIFY),
        Entry(f, "justify_keys", fx.Q_JUSTIFY_KEYS, JUSTIFY),
        Entry(f, "justify_tracked", fx.Q_JUSTIFY_TRACKED, JUSTIFY),
        Entry(f, "justify_holder", fx.Q_JUSTIFY_HOLDER, JUSTIFY),
        Entry(f, "justify_holder_parents", fx.q_justify_holder_parents(n), JUSTIFY),
        Entry(f, "justify_recipe", fx.Q_JUSTIFY_RECIPE, JUSTIFY),
        Entry(f, "justify_inputs", fx.Q_JUSTIFY_INPUTS, JUSTIFY),
        Entry(w, "reset_affected", wi.Q_RESET_AFFECTED, "Clears the marker on the fork's copy of the graph."),
        Entry(w, "mark_items_gone", wi.M_ITEMS, FORK_ONLY),
        Entry(w, "mark_copies_gone", wi.M_COPIES, "A destroyed archive kills the Copy that lives in it."),
        Entry(w, "mark_refs_gone", wi.M_REFS, FORK_ONLY),
        Entry(w, "mark_ref_prefixes_gone", wi.M_REF_PREFIXES, "Reflogs and stashes are removed by id prefix."),
        Entry(w, "mark_repos_gone", wi.M_REPOS, "Deleting a repository takes every ref it holds."),
        Entry(w, "mark_prunable", wi.M_PRUNABLE, "A removed worktree leaves its HEAD prunable, so it can expire."),
        Entry(w, "mark_hit", wi.M_MARK_HIT, "Marks the commits whose holders were hit."),
        Entry(w, "mark_repointed", wi.M_MARK_REPOINTED, "A rewritten ref puts its old commit at risk."),
        Entry(w, "repoint_drop", wi.M_REPOINT_DROP, FORK_ONLY),
        Entry(w, "repoint_add", wi.M_REPOINT_ADD, FORK_ONLY),
        Entry(w, "spread_affected", wi.q_spread_affected(n), "Ancestors of an affected commit are affected too."),
        Entry(w, "count_items_gone", wi.Q_COUNT_ITEMS_GONE, "Diff against the live graph: items destroyed."),
        Entry(w, "count_copies_gone", wi.Q_COUNT_COPIES_GONE, "Diff against the live graph: copies destroyed."),
        Entry(w, "count_refs_gone", wi.Q_COUNT_REFS_GONE, "Diff against the live graph: refs destroyed."),
        Entry(w, "count_refs_prunable", wi.Q_COUNT_REFS_PRUNABLE, "Diff against the live graph: refs prunable."),
        Entry(w, "live_flags", wi.Q_LIVE_FLAGS, "Run on the live graph after every what-if; it must return zero."),
        Entry(AUDIT, "action", au.A_ACTION, "A proposed command is a node, so verdicts about the same command share it."),
        Entry(AUDIT, "verdict", au.A_VERDICT, "A verdict carries the hashes needed to replay it later."),
        Entry(AUDIT, "about", au.A_ABOUT, "Verdict to Action, so the history of one command is a one-hop read."),
        Entry(AUDIT, "would_lose", au.A_WOULD_LOSE, "Verdict to the Items it warned about; relinked after each reload."),
        Entry(AUDIT, "fix_step", au.A_FIXSTEP, "Each planned fix is a FixStep node with its own execution status."),
        Entry(AUDIT, "set_status", au.A_STATUS, "The verdict status moves from planned to executed or refused."),
        Entry(AUDIT, "set_fix_status", au.A_FIX_STATUS, "Per-step result, kept next to the verdict that planned it."),
        Entry(AUDIT, "fingerprint_nodes", au.Q_FP_NODES,
              "All modeled world-node properties contribute to the verdict fingerprint."),
        Entry(AUDIT, "fingerprint_edges", au.Q_FP_EDGES,
              "Relationships between modeled world nodes contribute to the verdict fingerprint."),
        Entry(EFFECTS, "effect_items", ef.E_ITEMS, "Fix effects are plain writes on the fork; the live graph never sees them."),
        Entry(EFFECTS, "effect_copies", ef.E_COPIES, "A planned backup appears on the fork as a Copy in its target domain."),
        Entry(EFFECTS, "effect_copy_of", ef.E_COPY_OF, "COPY_OF edges say which items the planned backup protects."),
        Entry(EFFECTS, "effect_needs", ef.E_NEEDS, "NEEDS edges carry the independently stored key of a vault."),
        Entry(EFFECTS, "effect_refs", ef.E_REFS, "A planned push or bundle appears as a durable ref."),
        Entry(EFFECTS, "effect_points_to", ef.E_POINTS, "The new ref points at the commits it will hold."),
    ]


def render(max_hops: int | None = None) -> str:
    lines = [INTRO, ""]
    group = None
    for e in entries(max_hops):
        if e.group != group:
            group = e.group
            lines += [f"### {group}", ""]
        lines += [f"#### {e.name}", "", f"Query hash `{q.qhash(e.text)}`. *Why a graph:* {e.why}", "",
                  f"{FENCE}cypher", e.text.strip(), FENCE, ""]
    return "\n".join(lines).rstrip("\n")


def write(readme: Path = README) -> None:
    text = readme.read_text(encoding="utf-8")
    block = f"{START}\n{render()}\n{END}"
    if START in text and END in text:
        head, _, rest = text.partition(START)
        _, _, tail = rest.partition(END)
        text = head + block + tail
    else:
        text = text.rstrip("\n") + "\n\n## Cypher the product depends on\n\n" + block + "\n"
    readme.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    write()
    print(f"updated {README}")
