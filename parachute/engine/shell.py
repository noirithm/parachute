"""Minimal POSIX-shell front end: tokenise, then expand words the way sh would.

Anything it cannot model faithfully (command substitution, subshells, heredocs, brace lists, special
parameters) raises ShellError, and the resolver turns that into STOP_AND_ASK.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_DUP = re.compile(r">&(\d+|-)")
_GLOB = set("*?[")
_SPECIAL_PARAMS = "?!@*#-$0123456789"


class ShellError(ValueError):
    pass


@dataclass
class Redirect:
    op: str
    target: str


@dataclass
class RawCommand:
    words: list = field(default_factory=list)
    redirects: list = field(default_factory=list)
    sep: str = ""


@dataclass
class Arg:
    text: str
    empty: tuple = ()


@dataclass
class Expansion:
    args: list
    empty: list
    special: list


def is_assignment(raw: str) -> bool:
    return bool(_ASSIGN.match(raw))


def _operator(line, i, end_command, set_redirect):
    c = line[i]
    if c == ";":
        end_command(";")
        return i + 1
    if c == "&":
        if line.startswith("&&", i):
            end_command("&&")
            return i + 2
        if line.startswith("&>>", i):
            set_redirect("&>>")
            return i + 3
        if line.startswith("&>", i):
            set_redirect("&>")
            return i + 2
        end_command(";")
        return i + 1
    if c == "|":
        if line.startswith("||", i):
            end_command("||")
            return i + 2
        end_command("|")
        return i + (2 if line.startswith("|&", i) else 1)
    if c == "<":
        if line.startswith("<<", i) or line.startswith("<(", i):
            raise ShellError("heredocs and process substitution are not supported")
        set_redirect("<")
        return i + 1
    if line.startswith(">>", i):
        set_redirect(">>")
        return i + 2
    if line.startswith(">|", i):
        set_redirect(">|")
        return i + 2
    if line.startswith(">&", i):
        dup = _DUP.match(line, i)
        if dup:
            return dup.end()
        set_redirect("&>")
        return i + 2
    set_redirect(">")
    return i + 1


def tokenize(line: str) -> list:
    commands: list = []
    words: list = []
    redirects: list = []
    buf: list = []
    state = {"has": False, "pending": None}

    def end_word():
        if not state["has"]:
            return
        word = "".join(buf)
        buf.clear()
        state["has"] = False
        if state["pending"] is not None:
            redirects.append(Redirect(state["pending"], word))
            state["pending"] = None
        else:
            words.append(word)

    def end_command(sep):
        nonlocal words, redirects
        end_word()
        if state["pending"] is not None:
            raise ShellError("redirection without a target")
        if words or redirects:
            commands.append(RawCommand(words, redirects, sep))
        elif sep in ("&&", "||", "|"):
            raise ShellError(f"syntax error near {sep!r}")
        words, redirects = [], []

    def set_redirect(op):
        if state["pending"] is not None:
            raise ShellError("redirection without a target")
        state["pending"] = op

    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_command(";")
            i += 1
        elif c == "#" and not state["has"]:
            while i < n and line[i] != "\n":
                i += 1
        elif c == "\\":
            if i + 1 >= n:
                raise ShellError("trailing backslash")
            if line[i + 1] == "\n":
                i += 2
            else:
                buf.append(line[i:i + 2])
                state["has"] = True
                i += 2
        elif c == "'":
            j = line.find("'", i + 1)
            if j < 0:
                raise ShellError("unterminated single quote")
            buf.append(line[i:j + 1])
            state["has"] = True
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and line[j] != '"':
                if line[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if line[j] == "`" or line.startswith("$(", j):
                    raise ShellError("command substitution is not supported")
                j += 1
            if j >= n:
                raise ShellError("unterminated double quote")
            buf.append(line[i:j + 1])
            state["has"] = True
            i = j + 1
        elif c == "`" or line.startswith("$(", i):
            raise ShellError("command substitution is not supported")
        elif c in "()":
            raise ShellError(f"unsupported shell syntax: {c}")
        elif c == "{":
            if i + 1 < n and line[i + 1] in " \t\n":
                raise ShellError("command groups are not supported")
            j = line.find("}", i)
            inner = line[i + 1:j] if j > 0 else ""
            if "," in inner or ".." in inner:
                raise ShellError("brace expansion is not supported")
            buf.append(c)
            state["has"] = True
            i += 1
        elif c in ";|&<>":
            if c in "<>" and state["has"] and "".join(buf).isdigit():
                buf.clear()
                state["has"] = False
            end_word()
            i = _operator(line, i, end_command, set_redirect)
        else:
            buf.append(c)
            state["has"] = True
            i += 1
    end_command("")
    if commands and commands[-1].sep in ("&&", "||", "|"):
        raise ShellError(f"syntax error: command ends with {commands[-1].sep!r}")
    return commands


def _parse_var(raw: str, i: int):
    n = len(raw)
    if i + 1 >= n:
        return None, i + 1
    c = raw[i + 1]
    if c == "{":
        j = raw.find("}", i + 2)
        if j < 0:
            raise ShellError("unterminated ${")
        name = raw[i + 2:j]
        if not _NAME.fullmatch(name):
            raise ShellError(f"unsupported parameter expansion ${{{name}}}")
        return name, j + 1
    m = _NAME.match(raw, i + 1)
    if m:
        return m.group(0), m.end()
    if c in _SPECIAL_PARAMS:
        raise ShellError(f"unsupported special parameter ${c}")
    return None, i + 1


def _pieces(raw, env, empty, special):
    """Split a raw word into (text, quoted, from_unquoted_variable) pieces."""
    out: list = []
    lit: list = []
    i, n = 0, len(raw)

    def flush():
        if lit:
            out.append(("".join(lit), False, False))
            lit.clear()

    if raw.startswith("~"):
        if n == 1 or raw[1] == "/":
            out.append((env.get("HOME", ""), True, False))
            i = 1
        else:
            raise ShellError("unsupported tilde expansion")
    while i < n:
        c = raw[i]
        if c == "\\":
            flush()
            out.append((raw[i + 1] if i + 1 < n else "\\", True, False))
            i += 2
        elif c == "'":
            flush()
            j = raw.index("'", i + 1)
            out.append((raw[i + 1:j], True, False))
            i = j + 1
        elif c == '"':
            flush()
            seg = []
            j = i + 1
            while raw[j] != '"':
                ch = raw[j]
                if ch == "\\" and raw[j + 1] in '"\\$`':
                    seg.append(raw[j + 1])
                    j += 2
                elif ch == "$":
                    name, j = _parse_var(raw, j)
                    if name is None:
                        seg.append("$")
                    else:
                        value = env.get(name, "")
                        if value == "":
                            empty.append(name)
                        seg.append(value)
                else:
                    seg.append(ch)
                    j += 1
            out.append(("".join(seg), True, False))
            i = j + 1
        elif c == "$":
            flush()
            name, i = _parse_var(raw, i)
            if name is None:
                lit.append("$")
            else:
                value = env.get(name, "")
                if value == "":
                    empty.append(name)
                elif any(ch.isspace() or ch in _GLOB for ch in value):
                    special.append(name)
                out.append((value, False, True))
        else:
            lit.append(c)
            i += 1
    flush()
    return out


def _assemble(pieces):
    """Word splitting: unquoted variable values split on whitespace, quoted text never does."""
    fields, cur, has = [], [], False
    for text, quoted, from_var in pieces:
        if quoted:
            cur.append((text, True))
            has = True
        elif from_var:
            for part in re.split(r"(\s+)", text):
                if part == "":
                    continue
                if part.isspace():
                    if has:
                        fields.append(cur)
                        cur, has = [], False
                else:
                    cur.append((part, False))
                    has = True
        elif text:
            cur.append((text, False))
            has = True
    if has:
        fields.append(cur)
    return fields


def expand(raw: str, env: dict, cwd: str, may_glob) -> Expansion:
    """Expand one raw word. Globs are only evaluated where may_glob(absolute pattern) is true."""
    empty: list = []
    special: list = []
    out: list = []
    for segs in _assemble(_pieces(raw, env, empty, special)):
        text = "".join(t for t, _ in segs)
        if any(not quoted and any(ch in _GLOB for ch in t) for t, quoted in segs):
            pattern = "".join(glob.escape(t) if quoted else t for t, quoted in segs)
            if may_glob(os.path.normpath(os.path.join(cwd, pattern))):
                hits = sorted(glob.glob(pattern, root_dir=cwd))
                if hits:
                    out.extend(hits)
                    continue
        out.append(text)
    return Expansion(out, empty, special)


def expand_scalar(raw: str, env: dict) -> str:
    """Expansion for assignment values: no word splitting, no globbing."""
    return "".join(text for text, _, _ in _pieces(raw, env, [], []))
