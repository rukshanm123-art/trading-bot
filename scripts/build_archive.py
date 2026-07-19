#!/usr/bin/env python3
"""Build a clean source archive.

When run inside Git, only tracked files are included. Outside Git, the script
falls back to a conservative allowlist and still excludes runtime artifacts.
"""

from __future__ import annotations

import subprocess  # nosec - fixed argv, no shell
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "dist" / "trading-bot-source.tar.gz"
ALLOW_PREFIXES = {
    ".github",
    "config",
    "data/fixtures",
    "docs",
    "migrations",
    "scripts",
    "src",
    "tests",
}
ALLOW_FILES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "Dockerfile",
    "Makefile",
    "README.md",
    "docker-compose.yml",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
}


def git_files() -> list[Path] | None:
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return [ROOT / line for line in proc.stdout.splitlines() if line]


def fallback_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOW_FILES or any(rel == p or rel.startswith(p + "/") for p in ALLOW_PREFIXES):
            if "__pycache__" not in rel and not rel.endswith(".pyc"):
                files.append(path)
    return files


def main(argv: list[str] | None = None) -> int:
    out = Path((argv or sys.argv[1:] or [str(DEFAULT_OUT)])[0])
    files = git_files() or fallback_files()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        for path in sorted(files):
            tf.add(path, arcname=f"trading-bot/{path.relative_to(ROOT).as_posix()}")
    print(f"archive written: {out} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
