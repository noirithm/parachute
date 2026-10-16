"""Recipe detection from a small rule table (local, deterministic, no network)."""
from __future__ import annotations

from pathlib import Path

RULES = [
    {"id": "lockfile-install", "trigger": "deps.lock", "command": "python3 tools/install_deps.py",
     "outputs": ["vendor"], "reads": ["deps.lock", "tools/install_deps.py"]},
    {"id": "build-script", "trigger": "build.py", "command": "python3 build.py",
     "outputs": ["dist"], "reads": ["build.py", "src/**/*"]},
]


def detect_recipes(root: Path, tree_dirs: list[Path]) -> list[dict]:
    out = []
    for tdir in tree_dirs:
        if not tdir.is_dir():
            continue
        for proj in sorted(p for p in tdir.iterdir() if p.is_dir() and not p.is_symlink()):
            prel = proj.relative_to(root).as_posix()
            for rule in RULES:
                if not (proj / rule["trigger"]).is_file():
                    continue
                reads: list[str] = []
                for pat in rule["reads"]:
                    reads += [p.relative_to(root).as_posix() for p in sorted(proj.glob(pat)) if p.is_file()]
                out.append({"id": f"recipe:{prel}:{rule['id']}", "rule": rule["id"],
                            "command": rule["command"], "cwd": prel,
                            "outputs": [f"{prel}/{o}/" for o in rule["outputs"]],
                            "reads": reads, "deterministic": 1, "network": 0})
    return out
