"""Bring the virtualenv in line with uv.lock before the backend imports from it.

An update checks out new code and restarts the backend process, not the service,
so the environment `uv run` built when the service started can be a release
behind: a new dependency is missing and the backend dies on import. Standard
library only, since it runs before anything else is known to be installed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
STAMP_NAME = ".synced-uv-lock"


def syncEnvironment() -> None:
    uv = os.environ.get("UV")
    venv = os.environ.get("VIRTUAL_ENV")
    if not uv or not venv:
        return  # not started through `uv run`: someone's own interpreter, leave it alone
    digest = hashlib.sha256((BACKEND_DIR / "uv.lock").read_bytes()).hexdigest()
    stamp = Path(venv) / STAMP_NAME
    if stamp.exists() and stamp.read_text().strip() == digest:
        return
    print("[startup] uv.lock differs from the environment's: running uv sync", file=sys.stderr, flush=True)
    subprocess.run([uv, "sync", "--locked"], cwd=BACKEND_DIR, check=True)
    stamp.write_text(digest)
