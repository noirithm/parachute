import json

from parachute.engine.layout import Layout
from parachute.scan import scan_world
from parachute.scan.gitscan import git
from parachute.world.generator import generate_world

P = "disk_a/projects"


def test_manifest_members_describe_every_covered_item(sandbox):
    for store in ("disk_a/backups", "disk_b/offsite"):
        entries = json.loads((sandbox.root / store / "MANIFEST.json").read_text())["copies"]
        assert entries
        for e in entries:
            assert set(e["members"]) == set(e["covers"])
            assert e["format"] in ("tar", "file")
            assert bool(e["encrypted"]) == ("key" in e)


def test_keyring_is_a_third_failure_domain(world):
    domains = {s["id"]: s["domain"] for s in world["stores"]}
    assert domains["keyring"] == "disk-c"
    assert len({domains["projects"], domains["offsite"], domains["keyring"]}) == 3


def test_layout_resolves_domains(sandbox):
    layout = Layout.load(sandbox.root)
    assert layout.domain_of(f"{P}/webshop/.env") == "disk-a"
    assert layout.domain_of("disk_b/offsite/runbook.md") == "disk-b"
    assert layout.domain_of("disk_c/keyring/anything.key") == "disk-c"
    assert layout.domain_of(".parachute/world.json") is None


def test_bundles_are_scanned_as_durable_holders(tmp_path):
    sb = generate_world(tmp_path / "box")
    ws = sb.root / P / "webshop"
    bundles = sb.root / "disk_b" / "offsite" / "bundles"
    bundles.mkdir()
    tip = git(["rev-parse", "main"], cwd=ws).strip()
    git(["bundle", "create", str(bundles / "main.bundle"), "main"], cwd=ws)
    world = scan_world(sb.root)
    refs = [r for r in world["refs"] if r["kind"] == "bundle"]
    assert len(refs) == 1 and refs[0]["domain"] == "disk-b" and refs[0]["store"] == "offsite"
    assert {"ref": refs[0]["id"], "sha": tip} in world["points_to"]
    assert any(holder.endswith("main.bundle") for holder, _ in Layout.load(sb.root).git_dirs())
