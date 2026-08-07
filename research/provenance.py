"""Tamper-EVIDENT provenance for every research result.

research_spec.yaml promises that each ledger entry carries the spec, data,
feature-set and Git hashes. It previously did not, and the ledger itself was
gitignored — so a published conclusion could not be tied to the bytes that
produced it. This module supplies those hashes and the ledger writer.

TAMPER-EVIDENT, NOT IMMUTABLE. Hashes let a reader DETECT that a record or its
inputs were altered; they do not prevent it. Anyone who can write to the repo
can rewrite both a record and the file it attests. Genuine immutability needs
signed tags, branch protection, or write-once external storage — none of which
is in place here. This mirrors the wording already used for the trading audit
log in docs/SECURITY.md.

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

    # Only the LEDGER is excluded: it is the file this very call is appending
    # to, so counting it would make a clean regeneration permanently
    # un-provable. Nothing else gets a pass. In particular the tracked evidence
    # under research/data/ (the manifest and step0_events.json) MUST make a run
    # dirty if it has been modified — excluding the whole directory, as an
    # earlier version did, would have hidden exactly the tampering these hashes
    # exist to expose. The bulk CSV is gitignored and so never appears here.
    changed = [
        line
        for line in run("status", "--porcelain").splitlines()
        if line.strip() and "research/experiments.jsonl" not in line
    ]
    return {
        "git_commit": run("rev-parse", "HEAD"),
        "git_dirty": bool(changed),
        "git_dirty_paths": [c[3:] for c in changed[:10]],
    }


class DataIntegrityError(RuntimeError):
    """The dataset on disk is not the one the manifest attests to."""


def data_manifest() -> dict:
    """Manifest facts, with the dataset hash RECOMPUTED and cross-checked.

    Copying ``data_sha256`` out of the manifest only proves the manifest is
    self-consistent: swap the CSV and the manifest still cheerfully asserts the
    old hash, so every ledger entry would inherit a false attestation. The hash
    is therefore computed from the CSV bytes here and compared, and a mismatch
    is FATAL — a result must not be recorded against data that is not the data
    it claims.
    """
    path = ROOT / "research" / "data" / "BTCUSDT-1h.manifest.json"
    csv_path = ROOT / "research" / "data" / "BTCUSDT-1h.csv"
    if not path.exists():
        return {}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    recorded = str(m.get("data_sha256", ""))
    if csv_path.exists():
        actual = _sha256_file(csv_path)
        if recorded and actual != recorded:
            raise DataIntegrityError(
                f"{csv_path.name} does not match its manifest.\n"
                f"  manifest: {recorded}\n"
                f"  on disk : {actual}\n"
                "Re-run research/import_binance.py, or restore the archive. "
                "Refusing to stamp a result against unverified data."
            )
        verified = True
    else:
        # A fresh clone has the manifest (tracked) but not the bulk CSV.
        actual, verified = recorded, False

    return {
        "data_sha256": actual,
        "data_sha256_verified": verified,
        "data_rows": m.get("rows"),
        "data_range": [m.get("first_open_time"), m.get("last_open_time")],
        "data_gaps": m.get("gap_count"),
    }


def provenance() -> dict:
    """Everything needed to reproduce a result, or to prove it was not edited.

    ``script_sha256`` hashes the entry point that actually ran, so a clean Git
    SHA is not the only evidence of which bytes executed.
    """
    import __main__

    script = getattr(__main__, "__file__", None)
    return {
        "script": Path(script).name if script else "",
        "script_sha256": _sha256_file(Path(script)) if script else "",
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
