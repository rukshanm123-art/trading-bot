#!/usr/bin/env python3
"""Run the full test suite and record a verifiable quality-gate record.

The live gate (src/trading_bot/security/livegate.py) refuses any record that
does not prove a real run: minimum collected tests, zero failures, coverage
threshold, the named safety tests present, a results hash, and git state.
A hand-written {"passed": true} can never pass.

Usage: python scripts/record_test_run.py
Writes: var/quality/latest_test_run.json
Exit code: 0 only if the gate-worthy record says passed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec - dev tooling, fixed argv, no shell
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.config import constants as C  # noqa: E402
from trading_bot.security.quality import expected_hashes  # noqa: E402

QUALITY_DIR = ROOT / "var" / "quality"
JUNIT = QUALITY_DIR / "junit.xml"
COV_JSON = QUALITY_DIR / "coverage.json"
BIN_DIRS = [Path(sys.executable).parent, Path(sys.prefix) / "bin", ROOT / ".venv" / "bin"]
QUALITY_DEADLINE_S = 13 * 60
_deadline: float | None = None


def tool(name: str) -> str:
    for directory in BIN_DIRS:
        candidate = directory / name
        if candidate.exists():
            return str(candidate)
    return name


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run(cmd: list[str], timeout_s: float = 60) -> subprocess.CompletedProcess:
    """Run one quality phase with both per-command and whole-run deadlines."""
    remaining = timeout_s
    if _deadline is not None:
        remaining = min(timeout_s, _deadline - time.monotonic())
    if remaining <= 0:
        return subprocess.CompletedProcess(
            cmd, 124, "", f"quality deadline exceeded before: {' '.join(cmd)}\n"
        )
    print(f"quality phase: {' '.join(cmd)} (timeout {remaining:.0f}s)", flush=True)
    try:
        return subprocess.run(  # nosec - fixed argv, no shell
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            124,
            _as_text(exc.stdout),
            _as_text(exc.stderr) + f"quality phase timed out after {remaining:.0f}s\n",
        )


def collect_test_ids() -> list[str]:
    # -o addopts= neutralises the project-level "-q" so node ids are printed
    proc = run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", "tests"])
    ids = [
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and not line.startswith(("=", "warning"))
    ]
    return ids


def git_info() -> tuple[str | None, bool, str]:
    inside = run(["git", "rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None, False, "no_repo"
    commit = run(["git", "rev-parse", "HEAD"]).stdout.strip() or None
    dirty = bool(run(["git", "status", "--porcelain"]).stdout.strip())
    return commit, dirty, "repo"


def main() -> int:
    global _deadline
    _deadline = time.monotonic() + QUALITY_DEADLINE_S
    QUALITY_DIR.mkdir(parents=True, exist_ok=True)

    collected_ids = collect_test_ids()
    missing_safety = [
        req
        for req in C.REQUIRED_SAFETY_TESTS
        if not any(req in tid or tid in req for tid in collected_ids)
    ]

    format_proc = run([tool("ruff"), "format", "--check", "src", "tests", "scripts"])
    lint_proc = run([tool("ruff"), "check", "src", "tests", "scripts"])
    type_proc = run([tool("mypy")])
    security_proc = run([tool("bandit"), "-c", "pyproject.toml", "-r", "src", "-q"])

    proc = run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            f"--junitxml={JUNIT}",
            "--cov=src/trading_bot",
            f"--cov-report=json:{COV_JSON}",
            "--cov-report=term",
        ],
        timeout_s=8 * 60,
    )
    sys.stdout.write(proc.stdout[-3000:])
    sys.stderr.write(proc.stderr[-2000:])

    tests_run = tests_failed = tests_errors = tests_skipped = 0
    if JUNIT.exists():
        suite = ET.parse(JUNIT).getroot()  # nosec - file we just generated
        node = suite if suite.tag == "testsuite" else suite.find("testsuite")
        if node is not None:
            tests_run = int(node.get("tests", 0))
            tests_failed = int(node.get("failures", 0))
            tests_errors = int(node.get("errors", 0))
            tests_skipped = int(node.get("skipped", 0))

    coverage_percent = 0.0
    if COV_JSON.exists():
        cov = json.loads(COV_JSON.read_text(encoding="utf-8"))
        coverage_percent = round(float(cov["totals"]["percent_covered"]), 2)

    results_hash = hashlib.sha256(JUNIT.read_bytes()).hexdigest() if JUNIT.exists() else ""
    git_commit, git_dirty, git_state = git_info()
    hashes = expected_hashes(ROOT)

    tests_passed = tests_run - tests_failed - tests_errors - tests_skipped
    record = {
        "passed": proc.returncode == 0 and tests_failed == 0 and tests_errors == 0,
        "tests_collected": len(collected_ids),
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed + tests_errors,
        "tests_skipped": tests_skipped,
        "coverage_percent": coverage_percent,
        "required_safety_tests": list(C.REQUIRED_SAFETY_TESTS),
        "required_safety_tests_missing": missing_safety,
        "results_hash": results_hash,
        **hashes,
        "formatter": {
            "command": "ruff format --check src tests scripts",
            "rc": format_proc.returncode,
        },
        "linter": {"command": "ruff check src tests scripts", "rc": lint_proc.returncode},
        "type_check": {"command": "mypy", "rc": type_proc.returncode},
        "security_scan": {
            "command": "bandit -c pyproject.toml -r src -q",
            "rc": security_proc.returncode,
        },
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "git_state": git_state,
        "python": sys.version.split()[0],
        "tool_versions": {
            "pytest": run([sys.executable, "-m", "pytest", "--version"], 10).stdout.strip(),
            "ruff": run([tool("ruff"), "--version"], 10).stdout.strip(),
            "mypy": run([tool("mypy"), "--version"], 10).stdout.strip(),
            "bandit": run([tool("bandit"), "--version"], 10).stdout.strip().splitlines()[0],
        },
        "ran_at": datetime.now(UTC).isoformat(),
    }
    out = ROOT / C.QUALITY_GATE_FILE
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"\nquality record -> {out}")
    print(json.dumps({k: v for k, v in record.items() if k != "required_safety_tests"}, indent=2))

    gate_ok = (
        record["passed"]
        and record["tests_collected"] >= C.QUALITY_MIN_TESTS
        and record["coverage_percent"] >= C.QUALITY_MIN_COVERAGE_PCT
        and not missing_safety
        and format_proc.returncode == 0
        and lint_proc.returncode == 0
        and type_proc.returncode == 0
        and security_proc.returncode == 0
    )
    print(f"\nquality gate: {'PASS' if gate_ok else 'FAIL'}")
    return 0 if gate_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
