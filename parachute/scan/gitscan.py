"""Git scanner: plumbing only (refs, stashes, worktrees, reflog, remotes, unpushed commits). Read-only."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..guard import OutsideSandbox, require_inside
from .fs import store_for

REFLOG_LIMIT = 200
_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_TERMINAL_PROMPT="0", LC_ALL="C")
# fsmonitor/hooks in a scanned repo must never get to run commands during a scan
_SAFE = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}"]
_REF_FMT = "--format=%(refname)%1f%(objectname)%1f%(objecttype)%1f%(*objectname)"


def git(args, *, cwd=None, git_dir=None, stdin=None, check=True) -> str:
    cmd = ["git", *_SAFE]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += list(args)
    r = subprocess.run(cmd, env=_ENV, input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        if check:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return ""
    return r.stdout


def _find_repos(tree_dirs: list[Path]) -> list[Path]:
    repos = []
    for tdir in tree_dirs:
        for dirpath, dirnames, _ in os.walk(tdir):
            if ".git" in dirnames:  # a .git *directory* = main repo; linked worktrees have a .git file
                repos.append(Path(dirpath))
                dirnames.remove(".git")
    return sorted(repos)


def _ls_tree(sha: str, **kw):
    out = git(["ls-tree", "-r", "-z", sha], check=False, **kw)
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, typ, blob = meta.split()
        if typ == "blob" and mode in ("100644", "100755"):
            yield blob, path


def _load_commits(facts: dict, tips: list[str], **kw) -> None:
    tips = [t for t in dict.fromkeys(tips) if t]
    if not tips:
        return
    out = git(["log", "--format=%H%x1f%P%x1f%s", "--stdin"], stdin="\n".join(tips) + "\n", check=False, **kw)
    for line in out.splitlines():
        sha, parents, subject = (line.split("\x1f") + ["", ""])[:3]
        facts["commits"][sha] = {"sha": sha, "subject": subject[:120]}
        for p in parents.split():
            facts["parents"].add((sha, p))


def scan_git(root: Path, stores: list[dict], tree_dirs: list[Path], items_by_id: dict,
             allow_outside: bool = False) -> dict:
    facts = {"commits": {}, "parents": set(), "refs": [], "points_to": [], "tracked": set(), "warnings": []}
    remote_cache: dict[str, dict] = {}

    def rel(p) -> str:
        return Path(p).resolve().relative_to(root).as_posix()

    def add_ref(rid, kind, name, repo_rel, store, sha):
        facts["refs"].append({"id": rid, "kind": kind, "name": name, "repo": repo_rel, "store": store})
        if sha:
            facts["points_to"].append({"ref": rid, "sha": sha})

    def scan_remote(bare: Path) -> dict:
        key = rel(bare)
        if key in remote_cache:
            return remote_cache[key]
        blob_tip: dict[str, str] = {}
        store = store_for(key, stores)
        tips = []
        for line in git(["for-each-ref", _REF_FMT, "refs/heads"], git_dir=bare, check=False).splitlines():
            name, sha, typ, peeled = line.split("\x1f")
            add_ref(f"ref:{key}:{name}", "remote_branch", name, key, store, sha)
            tips.append(sha)
        _load_commits(facts, tips, git_dir=bare)
        for tip in dict.fromkeys(tips):
            for blob, _path in _ls_tree(tip, git_dir=bare):
                blob_tip.setdefault(blob, tip)
        remote_cache[key] = blob_tip
        return blob_tip

    for repo in _find_repos(tree_dirs):
        repo_rel = rel(repo)
        store = store_for(repo_rel, stores)

        # --- remotes (local paths only; never any network) ---
        remote_blobs: dict[str, str] = {}
        cfg = git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd=repo, check=False)
        for line in cfg.splitlines():
            url = line.partition(" ")[2].strip()
            if "://" in url or ("@" in url and ":" in url):
                facts["warnings"].append(f"{repo_rel}: non-local remote ignored ({url})")
                continue
            try:
                bare = require_inside(root, Path(url) if os.path.isabs(url) else repo / url, allow_outside)
            except OutsideSandbox as e:
                facts["warnings"].append(f"{repo_rel}: {e}")
                continue
            if bare.is_dir():
                for blob, tip in scan_remote(bare).items():
                    remote_blobs.setdefault(blob, tip)

        tips: list[str] = []
        # --- local branches / tags ---
        for line in git(["for-each-ref", _REF_FMT, "refs/heads", "refs/tags"], cwd=repo, check=False).splitlines():
            name, sha, typ, peeled = line.split("\x1f")
            sha = peeled if typ == "tag" else sha
            add_ref(f"ref:{repo_rel}:{name}", "tag" if name.startswith("refs/tags/") else "branch",
                    name, repo_rel, store, sha)
            tips.append(sha)
        # --- stashes ---
        for i, sha in enumerate(git(["rev-list", "-g", "refs/stash"], cwd=repo, check=False).split()):
            add_ref(f"ref:{repo_rel}:stash@{{{i}}}", "stash", f"stash@{{{i}}}", repo_rel, store, sha)
            tips.append(sha)
        # --- worktrees (+ per-worktree reflog + tracked files) ---
        blocks, cur = [], {}
        for line in git(["worktree", "list", "--porcelain"], cwd=repo, check=False).splitlines() + [""]:
            if not line:
                if cur.get("worktree"):
                    blocks.append(cur)
                cur = {}
                continue
            k, _, v = line.partition(" ")
            cur[k] = v
        for wt in blocks:
            if "bare" in wt or "prunable" in wt:
                continue
            try:
                wpath = require_inside(root, Path(wt["worktree"]), allow_outside)
            except OutsideSandbox as e:
                facts["warnings"].append(f"{repo_rel}: {e}")
                continue
            wrel, head = rel(wpath), wt.get("HEAD", "")
            if not head or set(head) == {"0"}:
                continue
            add_ref(f"ref:{repo_rel}:worktree:{wrel}", "worktree_head", f"HEAD@{wrel}", repo_rel, store, head)
            tips.append(head)
            fmt = "--format=%H%x1f%gd"
            if wpath == repo:
                log = (git(["reflog", "show", "--all", fmt], cwd=wpath, check=False)
                       + git(["reflog", "show", "HEAD", fmt], cwd=wpath, check=False))
            else:
                log = git(["reflog", "show", "HEAD", fmt], cwd=wpath, check=False)
            for line in log.splitlines()[:REFLOG_LIMIT]:
                sha, _, sel = line.partition("\x1f")
                # --all also lists other worktrees' HEAD logs; each one is scanned from inside its own worktree
                if not sha or sel.startswith(("refs/stash", "stash", "worktrees/")):
                    continue
                add_ref(f"ref:{repo_rel}:reflog:{wrel}:{sel}", "reflog", sel, repo_rel, store, sha)
                tips.append(sha)
            for blob, path in _ls_tree(head, cwd=wpath):
                iid = f"{wrel}/{path}"
                it = items_by_id.get(iid)
                if it and it["blob_sha1"] == blob:  # working file unmodified vs this worktree's HEAD
                    facts["tracked"].add((iid, head, blob))
                    if blob in remote_blobs:  # identical content also sits at a pushed remote tip
                        facts["tracked"].add((iid, remote_blobs[blob], blob))
        _load_commits(facts, tips, cwd=repo)

    # drop duplicate refs (same worktree reflog can be listed twice)
    seen, refs = set(), []
    for r in facts["refs"]:
        if r["id"] not in seen:
            seen.add(r["id"])
            refs.append(r)
    return {
        "commits": sorted(facts["commits"].values(), key=lambda c: c["sha"]),
        "parents": [{"child": c, "parent": p} for c, p in sorted(facts["parents"])],
        "refs": refs, "points_to": facts["points_to"],
        "tracked": [{"item": i, "sha": s, "blob": b} for i, s, b in sorted(facts["tracked"])],
        "warnings": facts["warnings"],
    }
