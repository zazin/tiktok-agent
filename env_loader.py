#!/usr/bin/env python3
"""
Tiny zero-dependency .env loader.

Every CLI in this project calls load_env() at startup so credentials in a local
.env file are available without manually `source`-ing it first. Real environment
variables always win — values from .env are only applied when the variable is
not already set.

Looks for a .env file next to this module (the project root) and, as a fallback,
in the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # tolerate `export KEY=VALUE`
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # Strip matching surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_env(path: Optional[str | os.PathLike] = None, *, override: bool = False) -> bool:
    """
    Load KEY=VALUE pairs from a .env file into os.environ.

    Args:
        path: Explicit .env path. If None, tries `<this module dir>/.env`, then
            `./.env`.
        override: If True, .env values replace existing environment variables.
            Default False — existing env vars take precedence.

    Returns:
        True if a .env file was found and read, else False.
    """
    if path is not None:
        candidates = [Path(path)]
    else:
        candidates = [Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"]

    for candidate in candidates:
        if candidate.is_file():
            for key, val in _parse_env_file(candidate).items():
                if override or key not in os.environ:
                    os.environ[key] = val
            return True
    return False
