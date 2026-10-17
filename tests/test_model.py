def test_state_properties_are_loaded(graph):
    assert graph.scalar("MATCH (i:Item) WHERE i.domain IS NULL OR i.gone <> 0 RETURN count(i)") == 0
    assert graph.scalar("MATCH (r:Ref) WHERE r.domain IS NULL OR r.gone <> 0 OR r.prunable <> 0 RETURN count(r)") == 0
    assert graph.scalar("MATCH (c:Copy) WHERE c.domain IS NULL OR c.path IS NULL OR c.gone <> 0 RETURN count(c)") == 0


def test_copy_paths_point_at_archives(graph, sandbox):
    paths = {r[0]: r[1] for r in graph.rows("MATCH (c:Copy) RETURN c.id, c.path")}
    assert paths["bk-runbook"] == "disk_b/offsite/runbook.md"
    assert all((sandbox.root / p).is_file() for p in paths.values())
