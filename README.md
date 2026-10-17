# PARACHUTE

Before an AI agent deletes or overwrites anything, a FalkorDB graph answers
"if this goes wrong, can I get it back?". If not, the agent first makes it
recoverable, then proceeds.

Quickstart: `make setup && make up && make smoke && make test && make demo`

All data is synthetic and generated locally.

## Cypher the product depends on

<!-- queries:start -->
All Cypher is constant text plus parameters. The only built string is a variable-length hop bound, produced from a validated integer (`graphdb.hops`). Every answer carries the short hash shown next to its query, so a result can be tied to the exact text that produced it. This section is generated from the code (`make docs`) and a test fails if it drifts.

### World summary

#### counts_by_class

Query hash `0bcc789f`. *Why a graph:* Baseline aggregate over the class stored on every Item.

```cypher
MATCH (i:Item) RETURN i.class AS cls, count(i) AS n ORDER BY cls
```

#### items

Query hash `4bdc36f6`. *Why a graph:* The universe the route queries are subtracted from.

```cypher
MATCH (i:Item) RETURN i.id AS id, i.class AS cls ORDER BY id
```

#### rebuildable_by_recipe

Query hash `ac23601e`. *Why a graph:* Rebuildable is an edge to a Recipe node, not a column on the item.

```cypher
MATCH (i:Item)-[:DERIVED_BY]->(:Recipe) RETURN DISTINCT i.id AS id
```

#### copy_placement

Query hash `a489410d`. *Why a graph:* A copy's placement against its original is two Store-to-Domain paths compared side by side.

```cypher
MATCH (c:Copy {alive: 1})-[:COPY_OF]->(i:Item)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(d:Domain)
MATCH (c)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(cd:Domain)
RETURN c.id AS copy, i.id AS item, d.id AS item_domain, cd.id AS copy_domain
```

#### phantom_same_domain

Query hash `f83b1353`. *Why a graph:* A same-disk backup is two paths that meet at the same Domain node.

```cypher
MATCH (c:Copy)-[:COPY_OF]->(i:Item)
WHERE c.alive = 1
MATCH (i)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(d:Domain)
MATCH (c)-[:STORED_ON]->(:Store)-[:IN_DOMAIN]->(d)
RETURN c.id AS copy, d.id AS domain, collect(i.id) AS items
ORDER BY copy
```

#### needs

Query hash `06cb6ced`. *Why a graph:* A backup depends on other items (keys), which is an edge to follow, not a flag.

```cypher
MATCH (c:Copy)-[:NEEDS]->(k:Item) RETURN c.id AS copy, k.id AS key ORDER BY copy
```

#### copy_covers

Query hash `73568646`. *Why a graph:* What each copy actually covers is read straight from COPY_OF.

```cypher
MATCH (c:Copy)-[:COPY_OF]->(i:Item) RETURN c.id AS copy, collect(i.id) AS items ORDER BY copy
```

#### route_git_direct

Query hash `d2292c5e`. *Why a graph:* Pushed means a remote Ref points at a commit that tracks this exact blob.

```cypher
MATCH (i:Item)-[:TRACKED_AT]->(c:Commit)<-[:POINTS_TO]-(r:Ref)
WHERE r.kind = 'remote_branch'
RETURN DISTINCT i.id AS id
```

#### route_git_via_parents

Query hash `5f912b5f`. *Why a graph:* Reachability from a remote branch is a variable-length path over PARENT.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(:Commit)-[:PARENT*1..200]->(c:Commit)<-[:TRACKED_AT]-(i:Item) WHERE r.kind = 'remote_branch' RETURN DISTINCT i.id AS id
```

#### commits_all

Query hash `7e3df19d`. *Why a graph:* The commit set that the remote-reachable set is subtracted from.

```cypher
MATCH (c:Commit) RETURN c.sha AS sha, c.subject AS subject ORDER BY sha
```

#### commits_remote_direct

Query hash `f35d1420`. *Why a graph:* Commits a remote branch points at directly.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit) WHERE r.kind = 'remote_branch' RETURN DISTINCT c.sha AS sha
```

#### commits_remote_via_parents

Query hash `cef88107`. *Why a graph:* Commits a remote branch reaches by walking PARENT links.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(:Commit)-[:PARENT*1..200]->(c:Commit) WHERE r.kind = 'remote_branch' RETURN DISTINCT c.sha AS sha
```

### Recoverability fixpoint (runs on the fork)

#### init_items

Query hash `799abddb`. *Why a graph:* Rank and route label live on the nodes, so every later round is a plain write query.

```cypher
MATCH (i:Item)
SET i.rank = CASE WHEN i.gone = 0 THEN 2 ELSE 0 END,
    i.via = CASE WHEN i.gone = 0 THEN 'present' ELSE 'none' END
```

#### init_commits

Query hash `dab27175`. *Why a graph:* Commits start with no holder and gain one from refs.

```cypher
MATCH (c:Commit) SET c.rank = 0, c.via = 'none'
```

#### seed_durable

Query hash `eb145a8f`. *Why a graph:* Remote branches and off-domain bundles are the only DURABLE holders of commits.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit)
WHERE r.gone = 0 AND r.kind = $kind AND c.rank < 2
SET c.rank = 2, c.via = $kind
```

#### seed_local

Query hash `2ff556b2`. *Why a graph:* Local refs, stashes and worktree HEADs hold commits only until the ref goes away (WEAK).

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit)
WHERE r.gone = 0 AND r.kind IN $kinds AND r.prunable = $prunable AND c.rank < 1
SET c.rank = 1, c.via = $label
```

#### propagate

Query hash `64869088`. *Why a graph:* Strength flows down PARENT paths from the holder to every ancestor commit.

```cypher
MATCH (c:Commit {via: $label})-[:PARENT*1..200]->(p:Commit) WHERE p.rank < $rank SET p.rank = $rank, p.via = $label
```

#### route_git

Query hash `53847d3f`. *Why a graph:* An item is recoverable if a surviving commit contains its exact blob.

```cypher
MATCH (i:Item)-[:TRACKED_AT]->(c:Commit)
WHERE i.gone = 1 AND i.rank < $rank AND c.rank >= $rank
SET i.rank = $rank, i.via = 'git:' + c.via
```

#### route_copy

Query hash `ce2a60e2`. *Why a graph:* AND over the copy's NEEDS and OR over copies, expressed as one grouped pattern.

```cypher
MATCH (c:Copy)-[:COPY_OF]->(i:Item)
OPTIONAL MATCH (c)-[:NEEDS]->(k:Item)
WITH c, i, count(k) AS needed, sum(CASE WHEN k.rank >= $rank THEN 1 ELSE 0 END) AS ready
WHERE i.gone = 1 AND i.rank < $rank AND c.alive = 1 AND c.gone = 0
  AND (c.domain <> i.domain OR $rank = 1) AND needed = ready
SET i.rank = $rank, i.via = 'copy:' + c.id
```

#### route_recipe

Query hash `b8bdeeee`. *Why a graph:* A rebuild is only as strong as the weakest of its inputs.

```cypher
MATCH (i:Item)-[:DERIVED_BY]->(r:Recipe)
OPTIONAL MATCH (r)-[:READS]->(k:Item)
WITH i, r, count(k) AS needed, sum(CASE WHEN k.rank >= $rank THEN 1 ELSE 0 END) AS ready
WHERE i.gone = 1 AND i.rank < $rank AND r.deterministic = 1 AND r.network = 0 AND needed = ready
SET i.rank = $rank, i.via = 'recipe:' + r.id
```

#### rank_sum

Query hash `0ace4738`. *Why a graph:* Rounds repeat until this total stops changing.

```cypher
MATCH (i:Item) RETURN sum(i.rank)
```

#### destroyed_items

Query hash `1445b546`. *Why a graph:* Reads the outcome from the nodes the rounds wrote.

```cypher
MATCH (i:Item) WHERE i.gone = 1
RETURN i.id AS id, i.class AS cls, i.rank AS rank, i.via AS via ORDER BY id
```

#### affected_commits

Query hash `b4abba14`. *Why a graph:* Commits that lost a holder, plus their ancestors.

```cypher
MATCH (c:Commit) WHERE c.affected = 1
RETURN c.sha AS sha, c.subject AS subject, c.rank AS rank, c.via AS via ORDER BY sha
```

#### justify_state

Query hash `8cfc4a0f`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (i:Item {id: $id}) RETURN i.rank, i.via
```

#### justify_copy

Query hash `bb56819e`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (c:Copy {id: $id})-[:STORED_ON]->(s:Store)-[:IN_DOMAIN]->(d:Domain) RETURN s.id, d.id, c.file
```

#### justify_keys

Query hash `db55b72e`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (c:Copy {id: $id})-[:NEEDS]->(k:Item) RETURN k.id, k.rank, k.via ORDER BY k.id
```

#### justify_tracked

Query hash `fcd6c359`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (i:Item {id: $id})-[:TRACKED_AT]->(c:Commit)
WHERE c.rank >= $rank AND c.via = $label
RETURN c.sha, c.subject ORDER BY c.sha LIMIT 1
```

#### justify_holder

Query hash `f0a75b1c`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit {sha: $sha})
WHERE r.gone = 0 AND r.kind IN $kinds
RETURN r.id ORDER BY r.id LIMIT 1
```

#### justify_holder_parents

Query hash `a8f4bcf2`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(t:Commit)-[:PARENT*1..200]->(c:Commit {sha: $sha}) WHERE r.gone = 0 AND r.kind IN $kinds RETURN r.id, t.sha ORDER BY r.id LIMIT 1
```

#### justify_recipe

Query hash `696e5318`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (r:Recipe {id: $id}) RETURN r.command
```

#### justify_inputs

Query hash `0139511a`. *Why a graph:* Justification is read back from the nodes the rounds wrote, so the explanation cannot disagree with the verdict.

```cypher
MATCH (r:Recipe {id: $id})-[:READS]->(k:Item) RETURN k.id, k.rank, k.via ORDER BY k.id LIMIT 4
```

### What-if fork

#### reset_affected

Query hash `b417a17c`. *Why a graph:* Clears the marker on the fork's copy of the graph.

```cypher
MATCH (c:Commit) SET c.affected = 0
```

#### mark_items_gone

Query hash `3a5e5f56`. *Why a graph:* Applied to the fork only; the live graph is never written.

```cypher
UNWIND $values AS v MATCH (i:Item {id: v}) SET i.gone = 1
```

#### mark_copies_gone

Query hash `e1e248ce`. *Why a graph:* A destroyed archive kills the Copy that lives in it.

```cypher
UNWIND $values AS v MATCH (c:Copy {path: v}) SET c.gone = 1
```

#### mark_refs_gone

Query hash `92c8d85e`. *Why a graph:* Applied to the fork only; the live graph is never written.

```cypher
UNWIND $values AS v MATCH (r:Ref {id: v}) SET r.gone = 1
```

#### mark_ref_prefixes_gone

Query hash `14338bcd`. *Why a graph:* Reflogs and stashes are removed by id prefix.

```cypher
UNWIND $values AS v MATCH (r:Ref) WHERE r.id STARTS WITH v SET r.gone = 1
```

#### mark_repos_gone

Query hash `040d5f3c`. *Why a graph:* Deleting a repository takes every ref it holds.

```cypher
UNWIND $values AS v MATCH (r:Ref {repo: v}) SET r.gone = 1
```

#### mark_prunable

Query hash `cb9f7a91`. *Why a graph:* A removed worktree leaves its HEAD prunable, so it can expire.

```cypher
UNWIND $values AS v MATCH (r:Ref {kind: 'worktree_head', name: v}) SET r.prunable = 1
```

#### mark_hit

Query hash `701a3b11`. *Why a graph:* Marks the commits whose holders were hit.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit)
WHERE r.gone = 1 OR r.prunable = 1
SET c.affected = 1
```

#### mark_repointed

Query hash `34073727`. *Why a graph:* A rewritten ref puts its old commit at risk.

```cypher
MATCH (r:Ref {id: $rid})-[:POINTS_TO]->(c:Commit) SET c.affected = 1
```

#### repoint_drop

Query hash `3e0d162d`. *Why a graph:* Applied to the fork only; the live graph is never written.

```cypher
MATCH (r:Ref {id: $rid})-[e:POINTS_TO]->(:Commit) DELETE e
```

#### repoint_add

Query hash `01c11b33`. *Why a graph:* Applied to the fork only; the live graph is never written.

```cypher
MATCH (r:Ref {id: $rid}) MATCH (c:Commit {sha: $sha}) CREATE (r)-[:POINTS_TO]->(c)
```

#### spread_affected

Query hash `76a988b9`. *Why a graph:* Ancestors of an affected commit are affected too.

```cypher
MATCH (c:Commit)-[:PARENT*1..200]->(p:Commit) WHERE c.affected = 1 AND p.affected = 0 SET p.affected = 1
```

#### count_items_gone

Query hash `43ef8242`. *Why a graph:* Diff against the live graph: items destroyed.

```cypher
MATCH (i:Item) WHERE i.gone = 1 RETURN count(i)
```

#### count_copies_gone

Query hash `1c538bc9`. *Why a graph:* Diff against the live graph: copies destroyed.

```cypher
MATCH (c:Copy) WHERE c.gone = 1 RETURN count(c)
```

#### count_refs_gone

Query hash `1e1f996b`. *Why a graph:* Diff against the live graph: refs destroyed.

```cypher
MATCH (r:Ref) WHERE r.gone = 1 RETURN count(r)
```

#### count_refs_prunable

Query hash `50df297c`. *Why a graph:* Diff against the live graph: refs prunable.

```cypher
MATCH (r:Ref) WHERE r.prunable = 1 RETURN count(r)
```

#### live_flags

Query hash `5b09f741`. *Why a graph:* Run on the live graph after every what-if; it must return zero.

```cypher
MATCH (n) WHERE n.gone = 1 OR n.prunable = 1 OR n.affected = 1 OR n.rank IS NOT NULL
RETURN count(n)
```

### Audit trail (live graph)

#### action

Query hash `24b7d435`. *Why a graph:* A proposed command is a node, so verdicts about the same command share it.

```cypher
MERGE (a:Action {id: $id}) SET a.command = $command, a.cwd = $cwd
```

#### verdict

Query hash `6733f638`. *Why a graph:* A verdict carries the hashes needed to replay it later.

```cypher
CREATE (v:Verdict {id: $id, verdict: $verdict, status: $status, command: $command, cwd: $cwd, created: $created, degraded: $degraded, destroyed_hash: $destroyed_hash, outcome_hash: $outcome_hash, plan_hash: $plan_hash, world_fp: $world_fp, lost_json: $lost_json, payload: $payload})
```

#### about

Query hash `2335de1d`. *Why a graph:* Verdict to Action, so the history of one command is a one-hop read.

```cypher
MATCH (v:Verdict {id: $vid}) MATCH (a:Action {id: $aid}) CREATE (v)-[:ABOUT]->(a)
```

#### would_lose

Query hash `bad025a9`. *Why a graph:* Verdict to the Items it warned about; relinked after each reload.

```cypher
UNWIND $ids AS iid MATCH (v:Verdict {id: $vid}) MATCH (i:Item {id: iid}) MERGE (v)-[:WOULD_LOSE]->(i)
```

#### fix_step

Query hash `fa93afff`. *Why a graph:* Each planned fix is a FixStep node with its own execution status.

```cypher
MATCH (v:Verdict {id: $vid}) CREATE (v)-[:PLANS]->(:FixStep {id: $id, fix: $fix, kind: $kind, cost: $cost, status: 'planned', params_json: $params, result_json: ''})
```

#### set_status

Query hash `1431f6d4`. *Why a graph:* The verdict status moves from planned to executed or refused.

```cypher
MATCH (v:Verdict {id: $id}) SET v.status = $status, v.detail = $detail
```

#### set_fix_status

Query hash `788cd372`. *Why a graph:* Per-step result, kept next to the verdict that planned it.

```cypher
MATCH (f:FixStep {id: $id}) SET f.status = $status, f.result_json = $result
```

#### fingerprint_items

Query hash `6ece6a4a`. *Why a graph:* Part of the world hash a verdict is bound to.

```cypher
MATCH (i:Item) RETURN i.id AS id, i.content_hash AS hash ORDER BY id
```

#### fingerprint_copies

Query hash `3f5bdc9e`. *Why a graph:* Copy liveness is part of what a verdict depends on.

```cypher
MATCH (c:Copy) RETURN c.id AS id, c.alive AS alive ORDER BY id
```

#### fingerprint_refs

Query hash `5772b22a`. *Why a graph:* What every ref points at is part of the world hash.

```cypher
MATCH (r:Ref)-[:POINTS_TO]->(c:Commit) RETURN r.id AS id, c.sha AS sha ORDER BY id, sha
```

### Fix effects (applied to the fork)

#### effect_items

Query hash `66c8d9d4`. *Why a graph:* Fix effects are plain writes on the fork; the live graph never sees them.

```cypher
UNWIND $values AS v MERGE (i:Item {id: v.id}) SET i.path = v.id, i.kind = 'file', i.class = v.cls, i.domain = v.domain, i.gone = 0
```

#### effect_copies

Query hash `1932b52c`. *Why a graph:* A planned backup appears on the fork as a Copy in its target domain.

```cypher
UNWIND $values AS v MERGE (c:Copy {id: v.id}) SET c.kind = v.kind, c.path = v.path, c.domain = v.domain, c.alive = 1, c.gone = 0
```

#### effect_copy_of

Query hash `e432ca1a`. *Why a graph:* COPY_OF edges say which items the planned backup protects.

```cypher
UNWIND $values AS v MATCH (c:Copy {id: v.copy}) MATCH (i:Item {id: v.item}) MERGE (c)-[x:COPY_OF]->(i) SET x.kind = v.kind
```

#### effect_needs

Query hash `fc9f75ef`. *Why a graph:* NEEDS edges carry the independently stored key of a vault.

```cypher
UNWIND $values AS v MATCH (c:Copy {id: v.copy}) MATCH (k:Item {id: v.item}) MERGE (c)-[:NEEDS]->(k)
```

#### effect_refs

Query hash `0cc09e73`. *Why a graph:* A planned push or bundle appears as a durable ref.

```cypher
UNWIND $values AS v MERGE (r:Ref {id: v.id}) SET r.kind = v.kind, r.name = v.name, r.repo = v.repo, r.domain = v.domain, r.gone = 0, r.prunable = 0
```

#### effect_points_to

Query hash `5679ffa1`. *Why a graph:* The new ref points at the commits it will hold.

```cypher
UNWIND $values AS v MATCH (r:Ref {id: v.ref}) MATCH (c:Commit {sha: v.sha}) MERGE (r)-[:POINTS_TO]->(c)
```
<!-- queries:end -->
