"""Immutable provenance for every research result.

research_spec.yaml promises that each ledger entry carries the spec, data,
feature-set and Git hashes. It previously did not, and the ledger itself was
gitignored — so a published conclusion could not be tied to the bytes that
produced it. This module supplies those hashes and the ledger writer.

A result without provenance is an anecdote. Archived negative results matter
as much as positive ones: they are what stops the same dead end being
re-explored, and what lets a reader confirm the holdout discipline actually
held.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404 - fixed argv, no shell
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "research" / "experiments.jsonl"


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git() -> dict:
    def run(*args: str) -> str:
        try:
            cmd = ["git", *args]
            out = subprocess.run(  # noqa: S603  # nosec - fixed argv, no shell
                cmd, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
            )
            return out.stdout.strip()
        except Exception:  # pragma: no cover - provenance must never crash a run
            return ""

    return {
        "git_commit": run("rev-parse", "HEAD"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def data_manifest() -> dict:
    path = ROOT / "research" / "data" / "BTCUSDT-1h.manifest.json"
    if not path.exists():
        return {}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "data_sha256": m.get("data_sha256", ""),
        "data_rows": m.get("rows"),
        "data_range": [m.get("first_open_time"), m.get("last_open_time")],
        "data_gaps": m.get("gap_count"),
    }


def provenance() -> dict:
    """Everything needed to reproduce a result, or to prove it was not edited."""
    return {
        "spec_sha256": _sha256_file(ROOT / "research" / "research_spec.yaml"),
        "events_sha256": _sha256_file(ROOT / "research" / "events.py"),
        "features_sha256": _sha256_file(ROOT / "research" / "features.py"),
        **data_manifest(),
        **_git(),
    }


def append(entry: dict) -> None:
    """Append one provenance-stamped record to the append-only ledger."""
    record = {
        "ran_at": datetime.now(UTC).isoformat(),
        **entry,
        "provenance": provenance(),
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
