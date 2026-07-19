"""Append-only live-qualification evidence ledger.

Backtests and fixture simulations are useful engineering checks, but they
contribute zero live-market paper days. Only records explicitly marked as
live-market paper and protected by the hash chain count toward qualification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.core.models import parse_iso
from trading_bot.core.types import json_dumps

EVIDENCE_FILE = "var/quality/qualification_evidence.jsonl"


@dataclass(frozen=True)
class QualificationSummary:
    ok: bool
    failures: tuple[str, ...]
    paper_days: int
    paper_decisions: int
    records: tuple[dict[str, Any], ...]


def _payload_hash(payload: dict[str, Any], previous_hash: str) -> str:
    material = json_dumps({"previous_hash": previous_hash, "payload": payload})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class QualificationEvidenceStore:
    def __init__(self, root: str | Path = ".", path: str | Path | None = None) -> None:
        self.root = Path(root)
        self.path = Path(path) if path else self.root / EVIDENCE_FILE

    def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        records = self.records(validate=False)
        previous = records[-1]["record_hash"] if records else "GENESIS"
        clean = dict(payload)
        clean.setdefault("recorded_at", datetime.now(UTC).isoformat())
        record = {
            "previous_hash": previous,
            "payload": clean,
            "record_hash": _payload_hash(clean, previous),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json_dumps(record) + "\n")
        return record

    def records(self, validate: bool = True) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [
            json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line
        ]
        if validate:
            previous = "GENESIS"
            for row in rows:
                if row.get("previous_hash") != previous:
                    raise ValueError("qualification evidence chain is broken")
                expected = _payload_hash(row["payload"], previous)
                if row.get("record_hash") != expected:
                    raise ValueError("qualification evidence hash mismatch")
                previous = row["record_hash"]
        return rows

    def summary(self) -> QualificationSummary:
        failures: list[str] = []
        try:
            rows = self.records(validate=True)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            return QualificationSummary(
                False, (f"qualification evidence invalid: {exc}",), 0, 0, ()
            )

        credited_days: set[str] = set()
        decisions = 0
        eligible: list[dict[str, Any]] = []
        for row in rows:
            payload = row["payload"]
            source_mode = payload.get("source_mode")
            data_source = payload.get("data_source_class")
            if source_mode != "paper" or data_source != "live_market":
                continue
            if payload.get("git_state") == "no_repo" or not payload.get("git_commit"):
                failures.append("eligible paper evidence missing git commit")
                continue
            if not payload.get("configuration_hash") or not payload.get("strategy_version"):
                failures.append("eligible paper evidence missing configuration/strategy")
                continue
            try:
                start = parse_iso(payload["wall_clock_start"])
                end = parse_iso(payload["wall_clock_end"])
            except (KeyError, ValueError):
                failures.append("eligible paper evidence missing wall-clock timestamps")
                continue
            if end <= start:
                failures.append("eligible paper evidence has invalid time range")
                continue
            day = start.astimezone(UTC).date().isoformat()
            credited_days.add(day)
            decisions += int(payload.get("eligible_decisions") or 0)
            eligible.append(payload)

        return QualificationSummary(
            ok=not failures,
            failures=tuple(failures),
            paper_days=len(credited_days),
            paper_decisions=decisions,
            records=tuple(eligible),
        )
