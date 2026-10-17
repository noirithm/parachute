"""Deterministic command resolver: shell text -> exact destroyed set. No LLM, no network.

The command is never executed. The real filesystem and read-only git plumbing supply the facts.
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import time

from .gitverbs import GitVerbs
from .layout import Layout
from .model import Destroyed, Finding, Resolution, Step
from .shell import Arg, ShellError, expand, expand_scalar, is_assignment, tokenize

READ_ONLY = frozenset({"ls", "cat", "head", "tail", "wc", "echo", "printf", "pwd", "stat", "file", "du", "df",
                       "grep", "diff", "basename", "dirname", "true", "false", "date", "whoami", "which",
                       "test", "[", ":"})
BIN_DIRS = ("/bin", "/usr/bin", "/usr/local/bin")
NULL_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")


class _Run(GitVerbs):
    def __init__(self, command: str, cwd: str, layout: Layout, env: dict):
        self.command = command
        self.layout = layout
        self.cwd = cwd
        self.env = {"HOME": layout.home, "USER": "dev", "PWD": cwd, **env}
        self.steps: list = []
        self.findings: list = []
        self.destroyed = Destroyed()
        self.idx = 0

    def block(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message, True, self.idx))

    def warn(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message, False, self.idx))

    def unknown(self, message: str, code: str = "UNSUPPORTED") -> str:
        self.block(code, message)
        return "unknown"

    def expand_words(self, raws: list) -> list:
        args = []
        for raw in raws:
            exp = expand(raw, self.env, self.cwd, self.layout.inside)
            if exp.special:
                self.block("UNQUOTED_EXPANSION",
                           f"unquoted ${', $'.join(exp.special)} holds whitespace or glob characters in {raw!r}")
            if exp.empty and not exp.args:
                self.warn("EMPTY_VARIABLE", f"{raw!r} expands to nothing (unset or empty: {', '.join(exp.empty)})")
            args.extend(Arg(text, tuple(exp.empty)) for text in exp.args)
        return args

    def operand(self, arg: Arg, cwd: str | None = None, destructive: bool = True) -> str | None:
        if arg.text == "":
            return None
        path = self.layout.locate(arg.text, cwd or self.cwd)
        if not self.layout.inside(path):
            if arg.empty:
                self.block("EMPTY_VARIABLE", f"empty variable ({', '.join(arg.empty)}) turns an operand into {path}")
            else:
                self.block("OUTSIDE_SANDBOX", f"{arg.text!r} resolves to {path}, outside the sandbox")
            return None
        if destructive and self.layout.covers_protected(path):
            self.block("BLAST_COVERS_ROOT", f"{arg.text!r} resolves to {path}, which is or contains a protected root")
            return None
        return path

    def parse_flags(self, args: list, short_ok: str, long_ok: dict):
        flags, operands, done = set(), [], False
        for a in args:
            t = a.text
            if done or not t.startswith("-") or t == "-":
                operands.append(a)
            elif t == "--":
                done = True
            elif t.startswith("--"):
                if t not in long_ok:
                    return None
                flags.add(long_ok[t])
            else:
                for ch in t[1:]:
                    if ch not in short_ok:
                        return None
                    flags.add(ch)
        return flags, operands

    def execute(self) -> None:
        for rc in tokenize(self.command):
            self.idx += 1
            self.run_command(rc)

    def run_command(self, rc) -> None:
        words = list(rc.words)
        assigns = []
        while words and is_assignment(words[0]):
            assigns.append(words.pop(0))
        d = Destroyed()
        argv: list = []
        if not words:
            for a in assigns:
                name, _, raw = a.partition("=")
                self.env[name] = expand_scalar(raw, self.env)
            kind = "noop"
        else:
            argv = self.expand_words(words)
            kind = self.dispatch(argv, d) if argv else "noop"
        self.redirects(rc.redirects, d)
        self.steps.append(Step([a.text for a in argv], argv[0].text if argv else "", kind, d))
        self.destroyed.merge(d)

    def dispatch(self, argv: list, d: Destroyed) -> str:
        verb = argv[0].text
        if "/" in verb:
            if os.path.dirname(verb) not in BIN_DIRS:
                return self.unknown(f"{verb}: executables outside the standard bin dirs are not allowed", "UNKNOWN_VERB")
            verb = os.path.basename(verb)
        args = argv[1:]
        if verb in ("mv", "cp"):
            return self.do_transfer(verb, args, d)
        handlers = {"rm": self.do_rm, "rmdir": self.do_rmdir, "find": self.do_find, "git": self.do_git}
        if verb in handlers:
            return handlers[verb](args, d)
        if verb == "cd":
            return self.do_cd(args)
        if verb == "export":
            return self.do_export(args)
        if verb == "unset":
            for a in args:
                self.env.pop(a.text, None)
            return "noop"
        if verb in READ_ONLY:
            return "read"
        return self.unknown(f"{verb}: not an allowlisted verb", "UNKNOWN_VERB")

    def do_cd(self, args: list) -> str:
        if len(args) > 1 or (args and args[0].text.startswith("-")):
            return self.unknown("cd: unsupported form")
        target = self.layout.locate(args[0].text, self.cwd) if args else self.layout.home
        if os.path.isdir(target):
            self.cwd = target
            self.env["PWD"] = target
        else:
            self.warn("CD_FAILED", f"cd target {target} does not exist; later commands resolve from {self.cwd}")
        return "noop"

    def do_export(self, args: list) -> str:
        for a in args:
            if "=" in a.text:
                name, _, value = a.text.partition("=")
                self.env[name] = value
        return "noop"

    def do_rm(self, args: list, d: Destroyed) -> str:
        if any(a.text == "--no-preserve-root" for a in args):
            self.block("NO_PRESERVE_ROOT", "rm --no-preserve-root is never allowed")
            return "destructive"
        parsed = self.parse_flags(args, "rRfvdiI", {
            "--recursive": "r", "--force": "f", "--verbose": "v", "--dir": "d", "--preserve-root": "p",
            "--one-file-system": "x", "--interactive": "i"})
        if parsed is None:
            return self.unknown("rm: unsupported option")
        flags, operands = parsed
        recursive = bool(flags & {"r", "R"})
        for a in operands:
            path = self.operand(a)
            if path is None:
                continue
            if os.path.islink(path) or os.path.isfile(path):
                d.paths.add(self.layout.rel(path))
            elif os.path.isdir(path) and recursive:
                d.paths.update(self.layout.files_under(path))
        return "destructive"

    def do_rmdir(self, args: list, d: Destroyed) -> str:
        parsed = self.parse_flags(args, "pv", {"--parents": "p", "--verbose": "v", "--ignore-fail-on-non-empty": "i"})
        if parsed is None:
            return self.unknown("rmdir: unsupported option")
        for a in parsed[1]:
            self.operand(a)
        return "destructive"

    def overwrite(self, target: str, d: Destroyed) -> None:
        if os.path.isfile(target) and not os.path.islink(target):
            d.paths.add(self.layout.rel(target))

    def do_transfer(self, verb: str, args: list, d: Destroyed) -> str:
        if verb == "mv":
            parsed = self.parse_flags(args, "fivnT", {"--force": "f", "--verbose": "v", "--no-clobber": "n",
                                                      "--interactive": "i"})
        else:
            parsed = self.parse_flags(args, "rRafvnpTi", {"--recursive": "r", "--archive": "a", "--force": "f",
                                                          "--verbose": "v", "--no-clobber": "n", "--preserve": "p"})
        if parsed is None:
            return self.unknown(f"{verb}: unsupported option")
        flags, operands = parsed
        if len(operands) < 2:
            return "noop"
        dst = self.operand(operands[-1], destructive=False)
        sources = [p for p in (self.operand(a, destructive=False) for a in operands[:-1]) if p is not None]
        if dst is None or "n" in flags:
            return "destructive"
        dst_is_dir = os.path.isdir(dst) and "T" not in flags
        if len(sources) > 1 and not dst_is_dir:
            return "noop"
        recursive = verb == "cp" and bool(flags & {"r", "R", "a"})
        for src in sources:
            if not os.path.lexists(src):
                continue
            base = os.path.join(dst, os.path.basename(src.rstrip(os.sep))) if dst_is_dir else dst
            if os.path.isdir(src) and not os.path.islink(src):
                if verb == "mv" or not recursive:
                    continue
                for file in self.layout.walk_files(src):
                    self.overwrite(os.path.join(base, os.path.relpath(file, src)), d)
            elif os.path.realpath(src) != os.path.realpath(base):
                self.overwrite(base, d)
        return "destructive"

    def do_find(self, args: list, d: Destroyed) -> str:
        i, starts = 0, []
        while i < len(args) and not (args[i].text.startswith("-") or args[i].text in ("!", "(")):
            starts.append(args[i])
            i += 1
        starts = starts or [Arg(".")]
        expr = [a.text for a in args[i:]]
        tests, maxdepth, mindepth, delete_tests, negate, j = [], None, 0, None, False, 0
        while j < len(expr):
            t = expr[j]
            if t in ("-name", "-iname", "-path", "-type", "-mtime", "-maxdepth", "-mindepth"):
                if j + 1 >= len(expr):
                    return self.unknown(f"find: {t} needs a value")
                val, j = expr[j + 1], j + 2
                if t in ("-maxdepth", "-mindepth"):
                    if not val.isdigit():
                        return self.unknown(f"find: {t} needs a number")
                    if t == "-maxdepth":
                        maxdepth = int(val)
                    else:
                        mindepth = int(val)
                    continue
                if (t == "-type" and val not in ("f", "d", "l")) or (t == "-mtime" and not re.fullmatch(r"[+-]?\d+", val)):
                    return self.unknown(f"find: unsupported value for {t}: {val}")
                tests.append((t, val, negate))
                negate = False
            elif t == "-empty":
                tests.append((t, None, negate))
                negate = False
                j += 1
            elif t in ("!", "-not"):
                negate = not negate
                j += 1
            elif t in ("-a", "-and", "-print", "-print0"):
                j += 1
            elif t == "-delete":
                delete_tests = list(tests)
                j += 1
            else:
                return self.unknown(f"find: unsupported expression {t!r}")
        if delete_tests is None:
            return "read"
        now = time.time()
        for s in starts:
            base = self.operand(s, destructive=False)
            if base is not None:
                self.find_walk(base, s.text, delete_tests, maxdepth, mindepth, now, d, 0)
        return "destructive"

    def find_walk(self, path, display, tests, maxdepth, mindepth, now, d, depth) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            return
        is_link, is_dir = stat.S_ISLNK(st.st_mode), stat.S_ISDIR(st.st_mode)
        in_range = depth >= mindepth and (maxdepth is None or depth <= maxdepth)
        if in_range and not is_dir and self.find_match(path, display, st, is_dir, is_link, tests, now):
            d.paths.add(self.layout.rel(path))
        if is_dir and not is_link and (maxdepth is None or depth < maxdepth) and depth < 64:
            try:
                names = sorted(os.listdir(path))
            except OSError:
                return
            for name in names:
                self.find_walk(os.path.join(path, name), display.rstrip("/") + "/" + name, tests, maxdepth,
                               mindepth, now, d, depth + 1)

    @staticmethod
    def find_match(path, display, st, is_dir, is_link, tests, now) -> bool:
        name = os.path.basename(display.rstrip("/")) or display
        for kind, val, negated in tests:
            if kind == "-name":
                ok = fnmatch.fnmatchcase(name, val)
            elif kind == "-iname":
                ok = fnmatch.fnmatchcase(name.lower(), val.lower())
            elif kind == "-path":
                ok = fnmatch.fnmatchcase(display, val)
            elif kind == "-type":
                ok = {"l": is_link, "d": is_dir and not is_link, "f": not is_dir and not is_link}[val]
            elif kind == "-empty":
                ok = (not os.listdir(path)) if is_dir and not is_link else (st.st_size == 0 and not is_dir)
            else:
                days, n = int((now - st.st_mtime) // 86400), int(val.lstrip("+-"))
                ok = days > n if val.startswith("+") else (days < n if val.startswith("-") else days == n)
            if ok == negated:
                return False
        return True

    def redirects(self, redirects: list, d: Destroyed) -> None:
        for rd in redirects:
            if rd.op in ("<", ">>", "&>>"):
                continue
            exp = expand(rd.target, self.env, self.cwd, self.layout.inside)
            if len(exp.args) != 1:
                self.block("AMBIGUOUS_REDIRECT", f"redirect target {rd.target!r} does not expand to one word")
                continue
            if exp.args[0] in NULL_SINKS:
                continue
            path = self.operand(Arg(exp.args[0], tuple(exp.empty)), destructive=False)
            if path is not None and os.path.isfile(path) and not os.path.islink(path):
                d.paths.add(self.layout.rel(path))

    def finish(self) -> Resolution:
        layout, d = self.layout, self.destroyed
        heads = {head: repo for repo, head in layout.git_dirs()}
        d.repos |= {repo for head, repo in heads.items() if head in d.paths}
        for p in d.paths:
            if os.path.basename(p) == ".git" and os.path.isfile(os.path.join(layout.root, p)):
                d.prunable.add(f"HEAD@{os.path.dirname(p)}")
        for s in layout.stores:
            files = layout.files_under(os.path.join(layout.root, s["path"]))
            if files and set(files) <= d.paths:
                d.stores.add(s["id"])
        for protected in sorted(layout.protected):
            files = layout.files_under(protected)
            if files and set(files) <= d.paths:
                self.block("BLAST_COVERS_ROOT", f"the destroyed set covers every file under {protected}")
        if any(p.startswith(".parachute/") for p in d.paths):
            self.block("PROTECTED_METADATA", "the sandbox control files under .parachute/ may not be destroyed")
        blocking = [f for f in self.findings if f.blocking]
        if blocking or any(s.kind == "unknown" for s in self.steps):
            forced = "STOP_AND_ASK"
        elif all(s.kind in ("read", "noop") for s in self.steps) and d.is_empty():
            forced = "GO"
        else:
            forced = None
        reasons = [f"{f.code}: {f.message}" for f in blocking]
        return Resolution(self.command, self.cwd, self.steps, self.findings, d, forced, reasons)


def resolve(command: str, cwd: str, layout: Layout, env: dict | None = None) -> Resolution:
    cwd = os.path.realpath(cwd)
    run = _Run(command, cwd, layout, env or {})
    if not layout.inside(cwd):
        run.block("OUTSIDE_SANDBOX", f"working directory {cwd} is outside the sandbox")
        return run.finish()
    try:
        run.execute()
    except ShellError as e:
        run.block("UNPARSEABLE", str(e))
    return run.finish()
