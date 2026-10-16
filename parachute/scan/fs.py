"""Filesystem scanner: walk tree stores, hash, classify. Never follows symlinks, never touches .git."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

SOURCE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".rst", ".toml", ".sh", ".html", ".css",
              ".txt", ".lock", ".yaml", ".yml", ".cfg", ".ini", ".rs", ".go", ".c", ".h", ".cpp", ".java"}
DATA_EXT = {".csv", ".tsv", ".sqlite", ".sqlite3", ".db", ".parquet", ".jsonl", ".json", ".xlsx"}
SOURCE_NAMES = {"Makefile", ".gitignore", "LICENSE", "Dockerfile"}
CONFIG_EXT = {"", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".toml", ".env"}
SECRET_EXCLUDE = {".env.example", ".env.sample", ".env.template"}
SECRET_NAME = [re.compile(p) for p in (r"^\.env(\..+)?$", r".*\.(key|pem|p12|pfx)$",
                                       r"^id_(rsa|ed25519|ecdsa)$", r"^credentials(\..+)?$")]
PEM = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_CONTENT = re.compile(rb"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[=:]\s*[\"']?[A-Za-z0-9_\-/+=.]{12,}")


def store_for(rel: str, stores: list[dict]) -> str | None:
    """Longest-prefix match of a root-relative path to a Store id."""
    for s in sorted(stores, key=lambda s: -len(s["path"])):
        if rel == s["path"] or rel.startswith(s["path"] + "/"):
            return s["id"]
    return None


def _hash_file(path: Path, size: int):
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1(usedforsecurity=False)  # git blob id, not a security hash
    sha1.update(b"blob %d\0" % size)
    sample = b""
    with open(path, "rb") as fh:
        first = True
        while chunk := fh.read(1 << 20):
            if first:
                sample, first = chunk[:65536], False
            sha256.update(chunk)
            sha1.update(chunk)
    return sha256.hexdigest(), sha1.hexdigest(), sample


def classify(name: str, ext: str, size: int, sample: bytes) -> str:
    if name in SECRET_EXCLUDE:
        return "SOURCE"
    if any(p.match(name) for p in SECRET_NAME):
        return "SECRET"
    text = size <= 65536 and b"\0" not in sample[:1024]
    if text and PEM.search(sample):
        return "SECRET"
    if text and ext in CONFIG_EXT and SECRET_CONTENT.search(sample):
        return "SECRET"
    if ext in DATA_EXT:
        return "DATA"
    if ext in SOURCE_EXT or name in SOURCE_NAMES:
        return "SOURCE"
    return "UNKNOWN"


def scan_trees(root: Path, stores: list[dict], recipes: list[dict]) -> list[dict]:
    prefixes = [(p, r["id"]) for r in recipes for p in r["outputs"]]
    items = []
    for s in stores:
        if s["kind"] != "tree":
            continue
        sdir = root / s["path"]
        if not sdir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(sdir, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if d != ".git" and not os.path.islink(os.path.join(dirpath, d)))
            for fn in sorted(filenames):
                if fn == ".git":  # worktree pointer file
                    continue
                fp = Path(dirpath) / fn
                st = os.lstat(fp)
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = fp.relative_to(root).as_posix()
                sha256, sha1, sample = _hash_file(fp, st.st_size)
                recipe = next((rid for pre, rid in prefixes if rel.startswith(pre)), None)
                cls = "GENERATED" if recipe else classify(fn, fp.suffix.lower(), st.st_size, sample)
                items.append({"id": rel, "path": rel, "kind": "file", "cls": cls, "content_hash": sha256,
                              "size": st.st_size, "blob_sha1": sha1, "store": s["id"], "recipe": recipe})
    return items
