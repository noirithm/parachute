"""git subcommands for the resolver. Facts come from read-only plumbing on the real repository."""
from __future__ import annotations

import os
import re

from ..scan.gitscan import git
from .model import Destroyed

READ_ONLY = frozenset({"status", "log", "diff", "show", "ls-files", "ls-tree", "rev-parse", "rev-list", "cat-file",
                       "blame", "describe", "shortlog", "grep", "merge-base", "diff-tree", "show-ref"})
CLEAN_SHORT = {"f": "f", "d": "d", "x": "x", "X": "X", "n": "n", "q": "q"}
CLEAN_LONG = {"--force": "f", "--dry-run": "n", "--quiet": "q"}
RESET_MODES = ("--hard", "--soft", "--mixed")


class GitVerbs:
    def _git(self, argv: list, cwd: str | None = None, git_dir: str | None = None) -> str:
        if git_dir is not None:
            return git(argv, git_dir=git_dir, check=False).strip()
        return git(argv, cwd=cwd or self.cwd, check=False).strip()

    def _context(self, cwd: str):
        top = self._git(["rev-parse", "--show-toplevel"], cwd)
        if not top:
            return None
        common = self._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
        main = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        top, main = os.path.realpath(top), os.path.realpath(main)
        if not (self.layout.inside(top) and self.layout.inside(main)):
            return None
        return top, main

    def do_git(self, args: list, d: Destroyed) -> str:
        cwd = self.cwd
        while args and args[0].text == "-C":
            if len(args) < 2:
                return self.unknown("git: -C needs a directory")
            cwd = self.layout.locate(args[1].text, cwd)
            args = args[2:]
        if not args or args[0].text.startswith("-"):
            return self.unknown("git: unsupported global option")
        sub, rest = args[0].text, args[1:]
        if sub in READ_ONLY:
            return "read"
        handlers = {"reset": self.git_reset, "clean": self.git_clean, "branch": self.git_branch,
                    "stash": self.git_stash, "worktree": self.git_worktree, "push": self.git_push,
                    "remote": self.git_remote, "config": self.git_config, "reflog": self.git_reflog,
                    "tag": self.git_tag, "add": self.git_add, "commit": self.git_commit}
        handler = handlers.get(sub)
        if handler is None:
            return self.unknown(f"git {sub}: not an allowlisted subcommand", "UNKNOWN_VERB")
        ctx = self._context(cwd)
        if ctx is None:
            self.block("NOT_A_REPO", f"git {sub}: {cwd} is not inside a repository in the sandbox")
            return "unknown"
        return handler(rest, d, cwd, *ctx)

    def git_remote(self, args, d, cwd, top, main) -> str:
        if not args or args[0].text in ("-v", "--verbose", "show", "get-url"):
            return "read"
        return self.unknown("git remote: only read-only forms are modelled")

    def git_config(self, args, d, cwd, top, main) -> str:
        if args and args[0].text in ("--get", "--get-all", "--get-regexp", "--list", "-l"):
            return "read"
        return self.unknown("git config: only read-only forms are modelled")

    def git_reflog(self, args, d, cwd, top, main) -> str:
        if not args or args[0].text == "show":
            return "read"
        return self.unknown("git reflog: expire and delete are not modelled")

    def git_tag(self, args, d, cwd, top, main) -> str:
        if not args or args[0].text in ("-l", "--list"):
            return "read"
        if len(args) == 1 and not args[0].text.startswith("-"):
            return "write"
        return self.unknown("git tag: only listing and plain creation are modelled")

    def git_add(self, args, d, cwd, top, main) -> str:
        return "write"

    def git_commit(self, args, d, cwd, top, main) -> str:
        if any(a.text.startswith(("--amend", "--fixup", "--squash")) for a in args):
            return self.unknown("git commit: rewriting commits is not modelled")
        return "write"

    def git_reset(self, args, d, cwd, top, main) -> str:
        mode, extra = "--mixed", []
        for a in args:
            if a.text in RESET_MODES:
                mode = a.text
            elif a.text in ("-q", "--quiet"):
                continue
            elif a.text.startswith("-"):
                return self.unknown(f"git reset {a.text}: not modelled")
            else:
                extra.append(a.text)
        if len(extra) > 1:
            return self.unknown("git reset: pathspec resets are not modelled")
        target = extra[0] if extra else "HEAD"
        new = self._git(["rev-parse", "--verify", "-q", f"{target}^{{commit}}"], cwd)
        head = self._git(["rev-parse", "--verify", "-q", "HEAD"], cwd)
        if not new or not head:
            self.warn("BAD_REVISION", f"git reset: {target!r} is not a commit here; git changes nothing")
            return "destructive"
        repo, wrel = self.layout.rel(main), self.layout.rel(top)
        branch = self._git(["symbolic-ref", "-q", "HEAD"], cwd)
        ref_id = f"ref:{repo}:{branch}" if branch else f"ref:{repo}:worktree:{wrel}"
        if new != head:
            d.repoints[ref_id] = new
        if mode == "--hard":
            names = set(self._git(["diff", "--name-only", "-z", "HEAD"], cwd).split("\0"))
            if new != head:
                names |= set(self._git(["diff", "--name-only", "-z", head, new], cwd).split("\0"))
            for name in names - {""}:
                path = os.path.join(top, name)
                if os.path.isfile(path):
                    d.paths.add(self.layout.rel(path))
        return "destructive"

    def git_clean(self, args, d, cwd, top, main) -> str:
        flags, pathspecs = [], []
        for a in args:
            t = a.text
            if t.startswith("--"):
                if t not in CLEAN_LONG:
                    return self.unknown(f"git clean {t}: not modelled")
                flags.append(CLEAN_LONG[t])
            elif t.startswith("-") and len(t) > 1:
                if any(ch not in CLEAN_SHORT for ch in t[1:]):
                    return self.unknown(f"git clean {t}: not modelled")
                flags.extend(t[1:])
            else:
                pathspecs.append(t)
        if "n" in flags:
            return "read"
        if "f" not in flags:
            self.warn("CLEAN_NEEDS_FORCE", "git clean without -f removes nothing")
            return "noop"
        probe = ["clean", "-n"] + [f"-{ch}" for ch in "dxX" if ch in flags] + ["-f"] * flags.count("f")
        out = git(probe + ["--"] + pathspecs, cwd=cwd, check=False)
        for line in out.splitlines():
            if line.startswith("Would remove "):
                target = os.path.join(cwd, line[len("Would remove "):].rstrip("/"))
                d.paths.update(self.layout.files_under(target))
        return "destructive"

    def git_branch(self, args, d, cwd, top, main) -> str:
        flags = [a.text for a in args if a.text.startswith("-")]
        names = [a.text for a in args if not a.text.startswith("-")]
        if any(f in ("-m", "-M", "-c", "-C", "-f", "--force", "--move", "--copy", "-u", "--set-upstream-to",
                     "--unset-upstream", "--edit-description") for f in flags):
            return self.unknown("git branch: rename, copy, force and upstream edits are not modelled")
        delete = any(f in ("-d", "-D", "--delete", "-df", "-Df") for f in flags)
        if not delete:
            listing = "--list" in flags or "-l" in flags
            return "write" if names and not listing else "read"
        repo = self.layout.rel(main)
        checked_out = {line.split(" ", 1)[1] for line in self._git(["worktree", "list", "--porcelain"], cwd).splitlines()
                       if line.startswith("branch ")}
        for name in names:
            ref = f"refs/heads/{name}"
            if not self._git(["rev-parse", "--verify", "-q", ref], cwd):
                self.warn("NO_SUCH_BRANCH", f"branch {name!r} does not exist; git deletes nothing")
            elif ref in checked_out:
                self.warn("BRANCH_CHECKED_OUT", f"branch {name!r} is checked out; git refuses to delete it")
            else:
                d.refs.add(f"ref:{repo}:{ref}")
                d.ref_prefixes.update({f"ref:{repo}:reflog:{repo}:{ref}@{{", f"ref:{repo}:reflog:{repo}:{name}@{{"})
        return "destructive"

    def git_stash(self, args, d, cwd, top, main) -> str:
        repo = self.layout.rel(main)
        sub = args[0].text if args else "push"
        if sub in ("list", "show"):
            return "read"
        if sub in ("push", "save"):
            return "write"
        if sub == "clear":
            d.ref_prefixes.add(f"ref:{repo}:stash@{{")
            return "destructive"
        if sub == "drop":
            index = 0
            if len(args) > 1:
                m = re.fullmatch(r"(?:refs/)?stash@\{(\d+)\}", args[1].text)
                if not m:
                    return self.unknown("git stash drop: only stash@{n} is understood")
                index = int(m.group(1))
            d.refs.add(f"ref:{repo}:stash@{{{index}}}")
            return "destructive"
        return self.unknown(f"git stash {sub}: not modelled")

    def git_worktree(self, args, d, cwd, top, main) -> str:
        sub = args[0].text if args else ""
        if sub == "list":
            return "read"
        if sub != "remove":
            return self.unknown(f"git worktree {sub or '(none)'}: not modelled")
        paths = [a for a in args[1:] if not a.text.startswith("-")]
        if len(paths) != 1:
            return self.unknown("git worktree remove: expected exactly one path")
        target = self.operand(paths[0])
        if target is None:
            return "destructive"
        if os.path.realpath(target) == main:
            self.warn("MAIN_WORKTREE", "git refuses to remove the main working tree")
            return "destructive"
        if not os.path.isfile(os.path.join(target, ".git")):
            self.warn("NOT_A_WORKTREE", f"{target} is not a linked worktree; git removes nothing")
            return "destructive"
        repo, wrel = self.layout.rel(main), self.layout.rel(target)
        d.paths.update(self.layout.files_under(target))
        d.refs.add(f"ref:{repo}:worktree:{wrel}")
        d.ref_prefixes.add(f"ref:{repo}:reflog:{wrel}:")
        return "destructive"

    def git_push(self, args, d, cwd, top, main) -> str:
        force, delete, positional = False, False, []
        for a in args:
            t = a.text
            if t in ("-f", "--force") or t.startswith("--force-with-lease"):
                force = True
            elif t in ("-d", "--delete"):
                delete = True
            elif t in ("-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--no-verify"):
                continue
            elif t.startswith("-"):
                return self.unknown(f"git push {t}: not modelled")
            else:
                positional.append(t)
        if not (force or delete or any(p.startswith(("+", ":")) for p in positional)):
            return "write"
        remote = positional[0] if positional else "origin"
        specs = positional[1:] or [self._git(["symbolic-ref", "--short", "-q", "HEAD"], cwd)]
        url = self._git(["config", "--get", f"remote.{remote}.url"], cwd)
        if not url or "://" in url or ("@" in url and ":" in url):
            return self.unknown(f"git push: remote {remote!r} is not a local bare repository")
        bare = os.path.realpath(url if os.path.isabs(url) else os.path.join(top, url))
        if not self.layout.inside(bare):
            self.block("OUTSIDE_SANDBOX", f"remote {remote!r} points to {bare}, outside the sandbox")
            return "destructive"
        brel = self.layout.rel(bare)
        for spec in specs:
            plus = spec.startswith("+")
            src, _, dst = spec.lstrip("+").partition(":")
            if delete:
                src, dst = "", src
            dst = dst or src
            if not dst:
                return self.unknown("git push: could not work out the destination ref")
            dst_ref = dst if dst.startswith("refs/") else f"refs/heads/{dst}"
            ref_id = f"ref:{brel}:{dst_ref}"
            old = self._git(["rev-parse", "--verify", "-q", dst_ref], git_dir=bare)
            if not src:
                if old:
                    d.refs.add(ref_id)
                continue
            new = self._git(["rev-parse", "--verify", "-q", f"{src}^{{commit}}"], cwd)
            if not new:
                self.warn("BAD_REVISION", f"git push: {src!r} is not a commit here; git pushes nothing")
                continue
            if old and old != new and (force or plus):
                if self._git(["rev-list", "--count", f"{new}..{old}"], cwd) != "0":
                    d.repoints[ref_id] = new
        return "destructive"
