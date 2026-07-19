"""Quality evidence generation and verification.

The verifier recomputes hashes and rejects records that cannot be tied back to
the current source/dependency/config/test artifacts. It is a safety gate, not a
profitability claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.config import constants as C
from trading_bot.core.models import parse_iso

SKIP_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "var",
    "build",
    "dist",
}


@dataclass(frozen=True)
class QualityVerification:
    ok: bool
    failures: tuple[str, ...]
    record: dict[str, Any]


def sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def source_tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if any(part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.name == ".DS_Store" or path.suffix in {".pyc", ".db"}:
            continue
        if path.name == ".coverage":
            continue
        h.update(str(rel).encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def git_info(root: Path) -> tuple[str | None, bool, str]:
    git_dir = root / ".git"
    if not git_dir.exists():
        return None, False, "no_repo"
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ").strip()
        ref_path = git_dir / ref
        commit = ref_path.read_text(encoding="utf-8").strip() if ref_path.exists() else None
    else:
        commit = head
    # Dirty-state proof is supplied by source_tree_hash. Runtime code does not
    # shell out to Git; scripts/record_test_run.py records exact porcelain state.
    return commit or None, False, "repo"


def expected_hashes(root: Path) -> dict[str, str]:
    return {
        "dependency_lock_hash": sha256_file(root / "requirements.txt")
        + ":"
        + sha256_file(root / "requirements-dev.txt"),
        "configuration_schema_hash": sha256_file(root / "src/trading_bot/config/models.py")
        + ":"
        + sha256_file(root / "src/trading_bot/config/constants.py"),
        "test_results_hash": sha256_file(root / "var/quality/junit.xml"),
        "coverage_report_hash": sha256_file(root / "var/quality/coverage.json"),
        "source_tree_hash": source_tree_hash(root),
    }


def verify_quality_record(
    root: Path,
    path: Path | None = None,
    *,
    require_repo: bool = False,
) -> QualityVerification:
    path = path or root / C.QUALITY_GATE_FILE
    failures: list[str] = []
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return QualityVerification(False, (f"quality record unreadable: {exc}",), {})

    try:
        ran_at = parse_iso(record["ran_at"])
    except (KeyError, ValueError):
        failures.append("ran_at missing or invalid")
        ran_at = datetime.fromtimestamp(0, tz=UTC)
    age_h = (datetime.now(UTC) - ran_at).total_seconds() / 3600
    if age_h > C.QUALITY_GATE_MAX_AGE_HOURS:
        failures.append(f"quality record stale: {age_h:.1f}h old")

    collected = int(record.get("tests_collected") or 0)
    passed = int(record.get("tests_passed") or 0)
    failed = int(record.get("tests_failed") or 0)
    if collected <= 0:
        failures.append("zero tests collected")
    if collected < C.QUALITY_MIN_TESTS:
        failures.append(f"only {collected} tests collected")
    if failed:
        failures.append(f"{failed} tests failed")
    if passed + failed + int(record.get("tests_skipped") or 0) > collected:
        failures.append("test counts are inconsistent")
    if float(record.get("coverage_percent") or 0.0) < C.QUALITY_MIN_COVERAGE_PCT:
        failures.append("coverage below configured threshold")
    if record.get("required_safety_tests_missing"):
        failures.append("required safety tests missing")
    if not record.get("passed"):
        failures.append("record says test run did not pass")
    if not record.get("results_hash"):
        failures.append("results hash missing")

    for key, expected in expected_hashes(root).items():
        if record.get(key) != expected:
            failures.append(f"{key} mismatch")

    commit, dirty, git_state = git_info(root)
    if record.get("git_state") != git_state:
        failures.append("git_state mismatch")
    if git_state == "repo" and record.get("git_commit") != commit:
        failures.append("git commit mismatch")
    if bool(record.get("git_dirty")) != dirty:
        failures.append("git dirty state mismatch")
    if require_repo and git_state != "repo":
        failures.append("a real git repository is required")

    return QualityVerification(not failures, tuple(failures), record)
