"""Sandbox layout as declared by the world manifest: root, home, mount roots, stores."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..guard import OutsideSandbox


@dataclass
class Layout:
    root: str
    home: str
    stores: list
    mounts: list

    @classmethod
    def load(cls, root) -> "Layout":
        root = os.path.realpath(root)
        marker = os.path.join(root, ".parachute", "world.json")
        if not os.path.isfile(marker):
            raise OutsideSandbox(f"{root} is not a Parachute sandbox (missing .parachute/world.json)")
        with open(marker) as fh:
            stores = json.load(fh)["stores"]
        mounts = sorted({os.path.join(root, s["path"].split("/")[0]) for s in stores})
        tree = next((s for s in stores if s["kind"] == "tree"), None)
        home = os.path.join(root, tree["path"].split("/")[0]) if tree else root
        return cls(root, home, stores, mounts)

    @property
    def protected(self) -> set:
        return {self.root, self.home, *self.mounts}

    def inside(self, path: str) -> bool:
        return path == self.root or path.startswith(self.root + os.sep)

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def store_of(self, rel: str):
        for s in sorted(self.stores, key=lambda s: -len(s["path"])):
            if rel == s["path"] or rel.startswith(s["path"] + "/"):
                return s
        return None

    def domain_of(self, rel: str):
        s = self.store_of(rel)
        return s["domain"] if s else None

    def locate(self, text: str, cwd: str) -> str:
        """Absolute path of `text` as the shell would act on it: parents resolved, last component untouched."""
        absolute = os.path.normpath(os.path.join(cwd, text))
        parent, name = os.path.split(absolute)
        if not name:
            return absolute
        return os.path.join(os.path.realpath(parent), name)

    def covers_protected(self, path: str) -> bool:
        prefix = path.rstrip(os.sep) + os.sep
        return any(p == path or p.startswith(prefix) for p in self.protected)

    def walk_files(self, path: str) -> list:
        if os.path.islink(path) or os.path.isfile(path):
            return [path]
        out = []
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            out.extend(os.path.join(dirpath, f) for f in filenames)
            out.extend(os.path.join(dirpath, d) for d in dirnames if os.path.islink(os.path.join(dirpath, d)))
        return out

    def files_under(self, path: str) -> list:
        return [self.rel(p) for p in self.walk_files(path)]

    def git_dirs(self) -> list:
        """(holder path, the file whose destruction destroys it) for repositories and bundle files."""
        out = []
        for s in self.stores:
            base = os.path.join(self.root, s["path"])
            if s["kind"] == "tree":
                for dirpath, dirnames, _ in os.walk(base):
                    if ".git" in dirnames:
                        dirnames.remove(".git")
                        rel = self.rel(dirpath)
                        out.append((rel, f"{rel}/.git/HEAD"))
            elif s["kind"] == "remote" and os.path.isdir(base):
                for name in sorted(os.listdir(base)):
                    candidate = os.path.join(base, name)
                    if os.path.isfile(os.path.join(candidate, "HEAD")) and os.path.isdir(os.path.join(candidate, "objects")):
                        rel = self.rel(candidate)
                        out.append((rel, f"{rel}/HEAD"))
            elif s["kind"] == "backup" and os.path.isdir(base):
                for dirpath, _, filenames in os.walk(base):
                    for name in sorted(filenames):
                        if name.endswith(".bundle"):
                            rel = self.rel(os.path.join(dirpath, name))
                            out.append((rel, rel))
        return out
