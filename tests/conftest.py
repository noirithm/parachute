import shutil

import pytest

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
