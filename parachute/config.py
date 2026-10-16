"""Configuration from environment / .env. No secrets, no network."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(PROJECT_DIR / ".env")

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6390"))
FALKORDB_TAG = os.environ.get("FALKORDB_TAG", "6.0.0")
# Upper bound for variable-length PARENT walks (commit chains in a sandbox are short).
MAX_HOPS = int(os.environ.get("PARACHUTE_MAX_HOPS", "200"))
