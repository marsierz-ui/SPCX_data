"""Lightweight .env file loader."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | str | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into the environment.

    Skips blank lines and comments (lines starting with #).
    Does NOT override variables that are already set in the environment.

    Parameters
    ----------
    path : Path, str, or None
        Path to the .env file. If None, does nothing.
    """
    if path is None:
        return
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)
