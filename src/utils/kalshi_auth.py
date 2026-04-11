"""
Helpers for resolving Kalshi authentication assets on disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def resolve_private_key_path(explicit_path: Optional[str] = None) -> str:
    """
    Return the most likely Kalshi private-key path for the current workspace.

    Resolution order:
    1. Explicit caller-provided path, if it exists.
    2. ``KALSHI_PRIVATE_KEY_PATH`` from the environment, if it exists.
    3. Common repo-local defaults, preferring whichever file actually exists.
    4. The configured/legacy default string so callers can emit a useful error.
    """
    candidates = []

    if explicit_path:
        candidates.append(explicit_path)

    env_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if env_path and env_path not in candidates:
        candidates.append(env_path)

    for default_name in ("kalshi_private_key", "kalshi_private_key.pem"):
        if default_name not in candidates:
            candidates.append(default_name)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate

    if explicit_path:
        return explicit_path
    if env_path:
        return env_path
    return "kalshi_private_key"
