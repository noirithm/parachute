"""Sandbox guard: Parachute never scans or acts outside its sandbox root unless explicitly told to."""
from __future__ import annotations

import os
from pathlib import Path


class OutsideSandbox(PermissionError):
    """A path (or a proposed sandbox root) is not allowed."""


def assert_safe_root(root) -> Path:
    """A sandbox root may not be '/', a mount root, $HOME, or anything above $HOME."""
    r = Path(root).resolve()
    home = Path.home().resolve()
    if (r == Path(r.anchor) or r == home or home.is_relative_to(r)
            or os.path.ismount(r) or len(r.parts) < 3):
        raise OutsideSandbox(f"refusing unsafe sandbox root: {r}")
    return r


def require_inside(root, p, allow_outside: bool = False) -> Path:
    """Resolve p (symlink-safe) and require it to be inside root, unless allow_outside is set."""
    root = Path(root).resolve()
    rp = Path(p).resolve()
    if rp == root or root in rp.parents:
        return rp
    if allow_outside:
        return rp
    raise OutsideSandbox(f"{rp} is outside the sandbox {root}")
