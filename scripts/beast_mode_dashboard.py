#!/usr/bin/env python3
"""Compatibility wrapper for the repo-root beast mode dashboard entrypoint."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Awaitable, Callable


def _load_repo_dashboard_main() -> Callable[[], Awaitable[None]]:
    # Kept for legacy `scripts/beast_mode_dashboard.py` callers.
    repo_root = Path(__file__).resolve().parent.parent
    dashboard_path = repo_root / "beast_mode_dashboard.py"
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    spec = importlib.util.spec_from_file_location(
        "repo_beast_mode_dashboard",
        dashboard_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load dashboard module from {dashboard_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)

    main = getattr(module, "main", None)
    if not callable(main):
        raise RuntimeError(f"{dashboard_path} does not expose an async main()")
    return main


if __name__ == "__main__":
    asyncio.run(_load_repo_dashboard_main()())
