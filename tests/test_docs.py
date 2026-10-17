from parachute import config
from parachute.engine.fixpoint import query_hashes
from parachute.query_docs import END, README, START, entries, render
from parachute.queries import world_summary


def readme_text():
    return README.read_text(encoding="utf-8")


def test_readme_section_matches_generated_text():
    assert f"{START}\n{render()}\n{END}" in readme_text()


def test_every_entry_has_a_unique_name_and_a_one_line_reason():
    names = [e.name for e in entries()]
    assert len(names) == len(set(names))
    assert all(e.why and "\n" not in e.why for e in entries())


def test_answer_hashes_point_at_documented_queries(graph):
    text = readme_text()
    stamps = set(world_summary(graph)["query_hashes"].values()) | set(query_hashes(config.MAX_HOPS).values())
    assert stamps and all(f"`{h}`" in text for h in stamps)
