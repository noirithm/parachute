"""Independent restore oracle. It never consults the graph.

Before an action it records content hashes of every file (the scanner reads the filesystem and git directly).
After the action it tries to restore each one using only what physically survives: files still on disk, backup
archives (decrypting with keys that are themselves restored), git objects reachable from surviving refs,
bundles and reflogs, and recipe re-runs on restored inputs. It then compares the outcome with the verdict's
claims. Dangling git objects that no ref or reflog entry points at are deliberately not counted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ..scan import scan_world
from ..scan.gitscan import git
from .util import is_sqlite, sha256_file, sqlite_logical_hash

ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0")


def _short(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:8]


def _decrypt(src: Path, dst: Path, key: Path) -> bool:
    cmd = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-in", str(src), "-out", str(dst), "-pass", f"file:{key}"]
    return subprocess.run(cmd, capture_output=True).returncode == 0


class _Restore:
    def __init__(self, root: Path, snap: dict, tmp: Path):
        self.root, self.snap, self.tmp = root, snap, tmp
        self.cache: dict = {}
        self.busy: set = set()
        self.recipe_out: dict = {}
        self.manifest = self._load_manifests()
        self.scratch = tmp / "scratch"
        self._build_scratch()

    def _load_manifests(self) -> dict:
        found: dict = {}
        for s in self.snap["stores"]:
            sdir = self.root / s["path"]
            manifest = sdir / "MANIFEST.json"
            if s["kind"] != "backup" or not manifest.is_file():
                continue
            try:
                entries = json.loads(manifest.read_text())["copies"]
            except (ValueError, KeyError):
                continue
            for entry in entries:
                for iid in entry.get("members", {}):
                    found.setdefault(iid, []).append((entry, sdir))
        return found

    def _repos(self) -> list:
        out = []
        for s in self.snap["stores"]:
            base = self.root / s["path"]
            if not base.is_dir():
                continue
            if s["kind"] == "tree":
                for dirpath, dirnames, _ in os.walk(base):
                    if ".git" in dirnames:
                        dirnames.remove(".git")
                        out.append(Path(dirpath))
            elif s["kind"] == "remote":
                out += [p for p in sorted(base.iterdir()) if (p / "HEAD").is_file() and (p / "objects").is_dir()]
        return out

    def _bundles(self) -> list:
        out = []
        for s in self.snap["stores"]:
            base = self.root / s["path"]
            if s["kind"] == "backup" and base.is_dir():
                out += sorted(base.rglob("*.bundle"))
        return out

    def _fetch(self, source: Path, refspec: str) -> None:
        git(["fetch", "-q", str(source), refspec], cwd=self.scratch, check=False)

    def _linked_worktrees(self, repo: Path) -> list:
        found = []
        for line in git(["worktree", "list", "--porcelain"], cwd=repo, check=False).splitlines():
            if line.startswith("worktree ") and Path(line[9:]) != repo and Path(line[9:]).is_dir():
                found.append(Path(line[9:]))
        return found

    def _build_scratch(self) -> None:
        self.scratch.mkdir()
        git(["init", "-q"], cwd=self.scratch)
        for n, repo in enumerate(self._repos(), 1):
            self._fetch(repo, f"+refs/*:refs/surv/{n}/*")
            if (repo / ".git").is_dir():
                worktrees = self._linked_worktrees(repo)
                for i, wt in enumerate(worktrees, 1):
                    self._fetch(wt, f"HEAD:refs/surv/{n}/wt{i}")
                self._fetch_reflog(repo, worktrees, n)
        for i, bundle in enumerate(self._bundles()):
            self._fetch(bundle, f"+refs/*:refs/bundle/{i}/*")

    def _fetch_reflog(self, repo: Path, worktrees: list, n: int) -> None:
        shas = set(git(["reflog", "show", "--all", "--format=%H"], cwd=repo, check=False).split())
        for where in [repo, *worktrees]:
            shas |= set(git(["reflog", "show", "HEAD", "--format=%H"], cwd=where, check=False).split())
        if not shas:
            return
        copy = self.tmp / f"gitcopy{n}"
        shutil.copytree(repo / ".git", copy, symlinks=True)
        for i, sha in enumerate(sorted(shas)):
            git(["update-ref", f"refs/weak/{i}", sha], git_dir=copy, check=False)
        self._fetch(copy, f"+refs/weak/*:refs/weak/{n}/*")

    def _blob(self, sha: str):
        r = subprocess.run(["git", "-C", str(self.scratch), "cat-file", "blob", sha], capture_output=True, env=ENV)
        return r.stdout if r.returncode == 0 else None

    def commit(self, sha: str) -> dict:
        r = subprocess.run(["git", "-C", str(self.scratch), "cat-file", "-e", f"{sha}^{{commit}}"],
                           capture_output=True, env=ENV)
        return {"ok": r.returncode == 0, "route": "survivor" if r.returncode == 0 else "none"}

    def _matches(self, data: bytes, want: dict) -> bool:
        if hashlib.sha256(data).hexdigest() == want["sha256"]:
            return True
        if not want.get("logical"):
            return False
        probe = self.tmp / f"probe-{_short(want['sha256'])}.sqlite3"
        probe.write_bytes(data)
        try:
            return sqlite_logical_hash(probe) == want["logical"]
        except sqlite3.Error:
            return False

    def _from_copy(self, iid: str, entry: dict, sdir: Path, depth: int):
        archive = sdir / entry["file"]
        if not archive.is_file() or sha256_file(archive) != entry.get("sha256"):
            return None
        src = archive
        if entry.get("encrypted"):
            key = self.item(entry["key"], depth + 1)
            if not key["ok"]:
                return None
            keyfile, plain = self.tmp / f"key-{_short(entry['id'])}", self.tmp / f"plain-{_short(entry['id'])}"
            keyfile.write_bytes(key["data"])
            if not _decrypt(archive, plain, keyfile):
                return None
            src = plain
        if entry.get("format") != "tar":
            return src.read_bytes()
        try:
            with tarfile.open(src) as tf:
                member = tf.extractfile(entry["members"][iid])
                return member.read() if member else None
        except (tarfile.TarError, KeyError, OSError):
            return None

    def _run_recipe(self, recipe: dict, depth: int):
        cwd, work = recipe["cwd"], self.tmp / f"recipe-{_short(recipe['id'])}"
        work.mkdir(parents=True)
        for inp in recipe["reads"]:
            res = self.item(inp, depth + 1)
            if not res["ok"]:
                return None
            dst = work / inp[len(cwd) + 1:]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(res["data"])
        r = subprocess.run(["bash", "--noprofile", "--norc", "-c", recipe["command"]], cwd=work,
                           capture_output=True, timeout=60, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if r.returncode:
            return None
        out = {}
        for prefix in recipe["outputs"]:
            base = work / prefix[len(cwd) + 1:]
            for f in base.rglob("*") if base.is_dir() else []:
                if f.is_file():
                    out[f"{cwd}/{f.relative_to(work).as_posix()}"] = f.read_bytes()
        return out

    def _rebuild(self, iid: str, depth: int):
        recipe = next((r for r in self.snap["recipes"] if any(iid.startswith(o) for o in r["outputs"])), None)
        if recipe is None:
            return None
        if recipe["id"] not in self.recipe_out:
            self.recipe_out[recipe["id"]] = self._run_recipe(recipe, depth)
        built = self.recipe_out[recipe["id"]]
        return None if built is None else built.get(iid)

    def _candidates(self, iid: str, want: dict, depth: int):
        path = self.root / iid
        if path.is_file():
            yield "present", path.read_bytes()
        for entry, sdir in self.manifest.get(iid, []):
            data = self._from_copy(iid, entry, sdir, depth)
            if data is not None:
                yield f"copy:{entry['id']}", data
        data = self._blob(want["blob"]) if want["blob"] else None
        if data is not None:
            yield "git", data
        if want["cls"] == "GENERATED":
            data = self._rebuild(iid, depth)
            if data is not None:
                yield "recipe", data

    def item(self, iid: str, depth: int = 0) -> dict:
        if iid in self.cache:
            return self.cache[iid]
        if iid in self.busy or depth > 5:
            return {"ok": False, "route": "cycle", "data": None}
        self.busy.add(iid)
        result = {"ok": False, "route": "none", "data": None}
        try:
            want = self.snap["items"][iid]
            for route, data in self._candidates(iid, want, depth):
                if self._matches(data, want):
                    result = {"ok": True, "route": route, "data": data}
                    break
        finally:
            self.busy.discard(iid)
        self.cache[iid] = result
        return result


def _report(items: dict, commits: dict, claims) -> dict:
    claims = claims or {}

    def claimed(*keys) -> set:
        return set().union(*(set(claims.get(k, [])) for k in keys))

    def failed(ids, results) -> list:
        return sorted(i for i in ids if i in results and not results[i]["ok"])

    def restored(ids, results) -> list:
        return sorted(i for i in ids if i in results and results[i]["ok"])

    false_safe = (failed(claimed("recoverable", "fragile"), items)
                  + failed(claimed("recoverable_commits", "fragile_commits"), commits))
    false_alarm = restored(claimed("lost"), items) + restored(claimed("lost_commits"), commits)
    bad_items = sorted(i for i, r in items.items() if not r["ok"])
    bad_commits = sorted(c for c, r in commits.items() if not r["ok"])
    routes: dict = {}
    for r in [*items.values(), *commits.values()]:
        if r["ok"]:
            routes[r["route"].split(":")[0]] = routes.get(r["route"].split(":")[0], 0) + 1
    return {"all_green": not bad_items and not bad_commits,
            "items": {"total": len(items), "restored": len(items) - len(bad_items)},
            "commits": {"total": len(commits), "restored": len(commits) - len(bad_commits)},
            "routes": routes, "unrestorable_items": bad_items, "unrestorable_commits": bad_commits,
            "counters": {"FALSE_SAFE": len(false_safe), "FALSE_ALARM": len(false_alarm)},
            "false_safe": false_safe, "false_alarm": false_alarm}


class RestoreOracle:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.meta = self.root / ".parachute"

    def snapshot(self) -> dict:
        world = scan_world(self.root)
        items = {}
        for i in world["items"]:
            logical = None
            if is_sqlite(i["id"]):
                try:
                    logical = sqlite_logical_hash(self.root / i["id"])
                except sqlite3.Error:
                    pass
            items[i["id"]] = {"sha256": i["content_hash"], "cls": i["cls"], "blob": i["blob_sha1"],
                              "logical": logical}
        recipes = [{k: r[k] for k in ("id", "command", "cwd", "outputs", "reads")} for r in world["recipes"]]
        return {"items": items, "recipes": recipes, "stores": world["stores"],
                "commits": sorted(c["sha"] for c in world["commits"])}

    def save(self, verdict_id: str, snapshot: dict, claims: dict) -> None:
        folder = self.meta / "oracle"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{verdict_id}.json").write_text(json.dumps({"snapshot": snapshot, "claims": claims}))

    def run_saved(self, verdict_id: str) -> dict:
        data = json.loads((self.meta / "oracle" / f"{verdict_id}.json").read_text())
        return self.verify(data["snapshot"], data["claims"])

    def verify(self, snapshot: dict, claims: dict | None = None) -> dict:
        self.meta.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.meta) as tmp:
            restore = _Restore(self.root, snapshot, Path(tmp))
            items = {iid: restore.item(iid) for iid in sorted(snapshot["items"])}
            commits = {sha: restore.commit(sha) for sha in snapshot["commits"]}
        return _report(items, commits, claims)
