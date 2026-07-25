"""Append-only live-qualification evidence ledger.

Backtests and fixture simulations are useful engineering checks, but they
contribute zero live-market paper days. Only records explicitly marked as
live-market paper count toward qualification, and only when they survive
every check below:

- **Authenticated chain.** Records are chained with HMAC-SHA256 under a key
  generated once and stored in the operational database (control_flags).
  A fabricated evidence file written without that key fails verification;
  rewriting history requires tampering with BOTH the file and the database
  consistently (and the DB cross-checks below still have to line up).
- **Real runtime, not one-second "days".** A day is credited only when its
  eligible sessions cover at least ``QUALIFICATION_MIN_DAY_COVERAGE_S`` of
  wall-clock operation within that UTC day.
- **Database cross-check.** A credited day must also show at least
  ``QUALIFICATION_MIN_DECISIONS_PER_DAY`` recorded paper decisions in the
  decisions table for that same day. Evidence about days the database never
  saw counts nothing.
- **PostgreSQL provenance.** Records must declare that the qualifying session
  used PostgreSQL. SQLite sessions are deliberately non-qualifying so the
  eventual LIVE database contains the evidence key, decisions and reports.
- **No backdating.** ``recorded_at`` must be at or after the session end and
  non-decreasing along the chain.

This is defence-in-depth against fabricated or sloppy evidence, not
cryptographic proof against a determined admin of the same machine — see
docs/THREAT_MODEL.md.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import secrets as sysrand
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_bot.core.models import parse_iso
from trading_bot.core.types import json_dumps

EVIDENCE_FILE = "var/quality/qualification_evidence.jsonl"
EVIDENCE_KEY_FLAG = "qualification_evidence_key"
QUALIFICATION_MIN_DAY_COVERAGE_S = 12 * 3600  # a credited day ran >= 12h
QUALIFICATION_MIN_DECISIONS_PER_DAY = 12  # and the DB saw real decisions

# A 24/7 engine must not have to stop to prove it ran. The engine flushes an
# evidence record every EVIDENCE_FLUSH_INTERVAL_S and then advances its window
# mark, so consecutive records are CONTIGUOUS AND NON-OVERLAPPING — required,
# because summary() SUMS per-day coverage across records and overlapping
# windows would inflate a day beyond real wall-clock time.
EVIDENCE_FLUSH_INTERVAL_S = 1800  # 30 min: bounds evidence lost to a hard kill
EVIDENCE_MIN_WINDOW_S = 60  # windows shorter than this are not worth a record


def get_or_create_evidence_key(flags) -> str:
    """The signing key lives in control_flags; created once per database."""
    key = flags.get(EVIDENCE_KEY_FLAG)
    if not key:
        key = sysrand.token_hex(32)
        flags.set(EVIDENCE_KEY_FLAG, key)
    return key


@dataclass(frozen=True)
class QualificationSummary:
    ok: bool
    failures: tuple[str, ...]
    paper_days: int
    paper_decisions: int
    records: tuple[dict[str, Any], ...]


class QualificationEvidenceStore:
    def __init__(
        self,
        root: str | Path = ".",
        path: str | Path | None = None,
        key: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.path = Path(path) if path else self.root / EVIDENCE_FILE
        self._key = key

    def _record_hash(self, payload: dict[str, Any], previous_hash: str) -> str:
        material = json_dumps({"previous_hash": previous_hash, "payload": payload})
        if self._key:
            return hmac_mod.new(
                bytes.fromhex(self._key), material.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        records = self.records(validate=False)
        previous = records[-1]["record_hash"] if records else "GENESIS"
        clean = dict(payload)
        clean.setdefault("recorded_at", datetime.now(UTC).isoformat())
        record = {
            "previous_hash": previous,
            "payload": clean,
            "record_hash": self._record_hash(clean, previous),
            "sig_alg": "hmac-sha256" if self._key else "sha256",
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
                if self._key and row.get("sig_alg") != "hmac-sha256":
                    raise ValueError(
                        "qualification evidence is unsigned; only HMAC-signed records count"
                    )
                expected = self._record_hash(row["payload"], previous)
                if not hmac_mod.compare_digest(str(row.get("record_hash", "")), expected):
                    raise ValueError("qualification evidence hash mismatch")
                previous = row["record_hash"]
        return rows

    # ------------------------------------------------------------------
    def summary(
        self,
        decision_days: dict[str, int] | None = None,
        min_day_coverage_s: int = QUALIFICATION_MIN_DAY_COVERAGE_S,
        min_decisions_per_day: int = QUALIFICATION_MIN_DECISIONS_PER_DAY,
    ) -> QualificationSummary:
        """Credit days only for authenticated, sufficiently long, DB-confirmed
        live-market paper operation.

        ``decision_days``: UTC day -> paper decision count from the
        operational database. When None the cross-check cannot run and NO
        day is credited (fail closed).
        """
        failures: list[str] = []
        try:
            rows = self.records(validate=True)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            return QualificationSummary(
                False, (f"qualification evidence invalid: {exc}",), 0, 0, ()
            )

        coverage: dict[str, float] = {}  # UTC day -> covered seconds
        day_decisions: dict[str, int] = {}
        eligible: list[dict[str, Any]] = []
        last_recorded_at: datetime | None = None

        for row in rows:
            payload = row["payload"]
            source_mode = payload.get("source_mode")
            data_source = payload.get("data_source_class")
            if source_mode != "paper" or data_source != "live_market":
                continue
            if payload.get("database_backend") != "postgres":
                failures.append("eligible paper evidence was not recorded on PostgreSQL")
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
                recorded_at = parse_iso(payload["recorded_at"])
            except (KeyError, ValueError):
                failures.append("eligible paper evidence missing wall-clock timestamps")
                continue
            if end <= start:
                failures.append("eligible paper evidence has invalid time range")
                continue
            # no backdating: recorded at/after session end, non-decreasing
            if recorded_at < end:
                failures.append("evidence recorded before its session ended (backdated)")
                continue
            if last_recorded_at is not None and recorded_at < last_recorded_at:
                failures.append("evidence recorded_at not monotonic (backdated insert)")
                continue
            last_recorded_at = recorded_at

            # accumulate per-UTC-day coverage from the session interval
            cursor = start
            while cursor < end:
                day = cursor.astimezone(UTC).date().isoformat()
                day_end = datetime.fromisoformat(f"{day}T00:00:00+00:00") + timedelta(days=1)
                segment_end = min(end, day_end)
                coverage[day] = coverage.get(day, 0.0) + (segment_end - cursor).total_seconds()
                cursor = segment_end
            # attribute the session's decisions to its start day (approximate,
            # conservative: the DB cross-check below is per-day regardless)
            start_day = start.astimezone(UTC).date().isoformat()
            day_decisions[start_day] = day_decisions.get(start_day, 0) + int(
                payload.get("eligible_decisions") or 0
            )
            eligible.append(payload)

        credited_days: set[str] = set()
        decisions = 0
        for day, seconds in coverage.items():
            if seconds < min_day_coverage_s:
                continue
            db_count = (decision_days or {}).get(day, 0)
            if db_count < min_decisions_per_day:
                failures.append(
                    f"day {day} not confirmed by database decisions "
                    f"({db_count} < {min_decisions_per_day})"
                )
                continue
            credited_days.add(day)
            decisions += day_decisions.get(day, 0)

        return QualificationSummary(
            ok=not failures,
            failures=tuple(failures),
            paper_days=len(credited_days),
            paper_decisions=decisions,
            records=tuple(eligible),
        )
