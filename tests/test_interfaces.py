from fastapi.testclient import TestClient

from parachute import service
from parachute.agent import SCRIPTED_PLANS
from parachute.api import app
from parachute.mcp_server import (
    parachute_apply_fixes,
    parachute_audit_backups,
    parachute_explain,
    parachute_preflight,
    parachute_restore_test,
    parachute_world_summary,
)


def test_shared_service_exposes_all_six_required_operations():
    for name in (
        "preflight",
        "explain",
        "apply_fixes",
        "restore_test",
        "audit_backups",
        "world_summary",
    ):
        assert callable(getattr(service, name))


def test_mcp_exposes_required_service_tools():
    for tool in (
        parachute_preflight,
        parachute_explain,
        parachute_apply_fixes,
        parachute_restore_test,
        parachute_audit_backups,
        parachute_world_summary,
    ):
        assert callable(tool)


def test_deterministic_cleanup_plan_is_fixed_and_nonempty():
    commands = SCRIPTED_PLANS["cleanup_project"]
    assert commands == [
        "rm -rf analytics",
        "rm -rf webshop/vendor",
    ]


def test_required_http_routes_exist():
    paths = {route.path for route in app.routes}
    assert {
        "/preflight",
        "/explain",
        "/apply-fixes",
        "/restore-test",
        "/audit-backups",
        "/world-summary",
        "/run",
        "/ready",
    } <= paths


def test_execution_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PARACHUTE_ALLOW_EXECUTION", raising=False)
    monkeypatch.delenv("PARACHUTE_PUBLIC_READ_ONLY", raising=False)
    client = TestClient(app)

    response = client.post(
        "/run",
        json={"root": "/tmp/demo", "command": "rm -rf analytics"},
    )
    assert response.status_code == 403
    assert "Execution disabled" in response.json()["detail"]


def test_public_read_only_mode_always_blocks_execution(monkeypatch):
    monkeypatch.setenv("PARACHUTE_ALLOW_EXECUTION", "1")
    monkeypatch.setenv("PARACHUTE_PUBLIC_READ_ONLY", "1")
    client = TestClient(app)

    response = client.post(
        "/run",
        json={"root": "/tmp/demo", "command": "rm -rf analytics"},
    )
    assert response.status_code == 403
    assert "read-only" in response.json()["detail"].lower()


def test_request_body_limit():
    client = TestClient(app)
    response = client.post(
        "/preflight",
        content=b"x" * (64 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
