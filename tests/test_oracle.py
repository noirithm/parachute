import pytest

from parachute.engine.executor import claims_from, execute, run_shell
from parachute.engine.oracle import RestoreOracle
from parachute.engine.policy import preflight

P = "disk_a/projects"
SCENARIOS = [
    ("rm -rf analytics", P),
    ("git branch -D feature/cart", f"{P}/webshop"),
    ("git stash drop", f"{P}/webshop"),
    ("git worktree remove --force ../webshop-exp", f"{P}/webshop"),
    ("rm -rf vendor", f"{P}/webshop"),
    ("rm -rf dist", f"{P}/webshop"),
]


@pytest.mark.parametrize("command,sub", SCENARIOS)
def test_fix_then_go_ends_with_everything_restorable(fresh, command, sub):
    sb, g, layout = fresh
    verdict = preflight(g, layout, command, str(sb.root / sub))
    result = execute(g, layout, verdict, oracle=RestoreOracle(sb.root))
    assert result["status"] == "executed", result
    report = result["oracle"]
    assert report["all_green"], report
    assert report["counters"]["FALSE_SAFE"] == 0


@pytest.mark.parametrize("command,sub", SCENARIOS)
def test_graph_and_oracle_agree_when_the_command_runs_unprotected(fresh, command, sub):
    sb, g, layout = fresh
    verdict = preflight(g, layout, command, str(sb.root / sub), record=False)
    oracle = RestoreOracle(sb.root)
    snapshot = oracle.snapshot()
    assert run_shell(layout, str(sb.root / sub), command, {}, 60)["returncode"] == 0
    report = oracle.verify(snapshot, claims_from(verdict["outcome"]))
    assert report["counters"] == {"FALSE_SAFE": 0, "FALSE_ALARM": 0}, report


def test_oracle_finds_exactly_the_planned_losses(fresh):
    sb, g, layout = fresh
    oracle = RestoreOracle(sb.root)
    snapshot = oracle.snapshot()
    run_shell(layout, str(sb.root / P), "rm -rf analytics", {}, 60)
    report = oracle.verify(snapshot)
    assert set(report["unrestorable_items"]) == {
        f"{P}/analytics/{name}" for name in (".env", "README.md", "data/dev.sqlite3", "keys/backup.key",
                                            "notebooks/eda.py", "notebooks/forecast.py", "scratch/blob.bin")}
    assert not report["all_green"]


def test_error_counters_catch_wrong_claims(fresh):
    sb, g, layout = fresh
    oracle = RestoreOracle(sb.root)
    snapshot = oracle.snapshot()
    run_shell(layout, str(sb.root / P), "rm -rf analytics", {}, 60)
    wrong = {"recoverable": [f"{P}/analytics/.env"], "lost": [f"{P}/analytics/reports/q3.csv"]}
    report = oracle.verify(snapshot, wrong)
    assert report["counters"] == {"FALSE_SAFE": 1, "FALSE_ALARM": 1}
    assert report["false_safe"] == [f"{P}/analytics/.env"]
    assert report["false_alarm"] == [f"{P}/analytics/reports/q3.csv"]
