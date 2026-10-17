import shutil

import pytest

from parachute.engine.layout import Layout
from parachute.graphdb import GraphClient, GraphUnavailable
from parachute.loader import load_world
from parachute.scan import scan_world
from parachute.world.generator import generate_world


@pytest.fixture(scope="session")
def sandbox(tmp_path_factory):
    root = tmp_path_factory.mktemp("parachute-sandbox") / "box"
    sb = generate_world(root)
    yield sb
    shutil.rmtree(sb.root, ignore_errors=True)


@pytest.fixture(scope="session")
def world(sandbox):
    return scan_world(sandbox.root)


@pytest.fixture(scope="session")
def graph(sandbox, world):
    try:
        g = GraphClient(sandbox.graph_name)
    except GraphUnavailable as e:
        pytest.fail(f"FalkorDB not reachable ({e}). Run `make up` first.")
    load_world(g, world, reset=True)
    yield g
    g.delete()


@pytest.fixture
def fresh(tmp_path):
    """A private sandbox and graph for tests that destroy things."""
    sb = generate_world(tmp_path / "box")
    g = GraphClient(sb.graph_name)
    load_world(g, scan_world(sb.root), reset=True)
    yield sb, g, Layout.load(sb.root)
    g.delete()
