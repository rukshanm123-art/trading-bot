#!/usr/bin/env python3
"""Local secret pattern scan with narrow allowlists for redaction fixtures."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "var", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
PATTERNS = [
    re.compile(r"BEGIN (RSA|OPENSSH|PRIVATE)"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    re.compile(r"signature=[0-9a-fA-F]{32,}"),
    re.compile(r"api[_-]?secret\s*=\s*['\"][^'\"]{12,}", re.I),
]


def allowed(path: Path, line: str) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if rel == "tests/unit/test_redaction.py" and "signature=" in line:
        return True
    if rel == "tests/unit/test_binance_adapter_errors.py" and "signature=" in line:
        return True
    if rel == "src/trading_bot/security/redaction.py" and "signature=" in line:
        return True
    if rel == "docs/SECURITY.md" and "signature=" in line:
        return True
    if rel == "scripts/secret_scan.py":
        return True
    return False


def main() -> int:
    findings: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if any(p.search(line) for p in PATTERNS) and not allowed(path, line):
                findings.append(f"{rel}:{lineno}: {line.strip()[:160]}")
    if findings:
        print("secret scan FAILED")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print("secret scan OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
