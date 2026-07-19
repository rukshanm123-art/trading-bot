#!/usr/bin/env python3
"""Verify release/archive hygiene.

Fails if generated caches, runtime databases, reports, logs, local env files or
other machine-specific artifacts are present in the release input.
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

FORBIDDEN_PARTS = {
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "var",
    "htmlcov",
    "build",
    "dist",
}
FORBIDDEN_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".coverage",
    ".log",
}
FORBIDDEN_NAMES = {".env", ".DS_Store"}


def bad_name(name: str) -> bool:
    path = Path(name)
    parts = set(path.parts)
    if parts & FORBIDDEN_PARTS:
        return True
    if path.name in FORBIDDEN_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)


def names_from_tree(root: Path) -> list[str]:
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


def names_from_tar(path: Path) -> list[str]:
    with tarfile.open(path, "r:*") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def main(argv: list[str] | None = None) -> int:
    target = Path((argv or sys.argv[1:] or ["."])[0])
    names = names_from_tar(target) if target.is_file() else names_from_tree(target)
    bad = sorted(name for name in names if bad_name(name))
    if bad:
        print("release hygiene FAILED")
        for name in bad[:200]:
            print(f"  - {name}")
        if len(bad) > 200:
            print(f"  ... {len(bad) - 200} more")
        return 1
    print(f"release hygiene OK ({len(names)} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
