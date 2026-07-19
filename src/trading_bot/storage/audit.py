"""Append-only, hash-chained audit log for critical actions.

Each record's hash covers (seq, prev_hash, kind, payload); any retroactive
edit breaks the chain, detectable via verify_chain(). This is tamper-EVIDENT
storage, not tamper-proof — see docs/THREAT_MODEL.md.
"""

from __future__ import annotations

import hashlib
from typing import Any

from trading_bot.core.models import iso, utcnow
from trading_bot.core.types import json_dumps
from trading_bot.storage.db import Database, uid

GENESIS_HASH = "0" * 64


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _last(self) -> tuple[int, str]:
        row = self.db.query_one("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1")
        if not row:
            return 0, GENESIS_HASH
        return int(row["seq"]), str(row["hash"])

    @staticmethod
    def _digest(seq: int, prev_hash: str, kind: str, payload_json: str) -> str:
        material = f"{seq}|{prev_hash}|{kind}|{payload_json}".encode()
        return hashlib.sha256(material).hexdigest()

    def append(self, kind: str, payload: dict[str, Any]) -> int:
        payload_json = json_dumps(payload)
        last_seq, prev_hash = self._last()
        seq = last_seq + 1
        digest = self._digest(seq, prev_hash, kind, payload_json)
        self.db.execute(
            "INSERT INTO audit_log (id, seq, ts, kind, payload_json, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid(), seq, iso(utcnow()), kind, payload_json, prev_hash, digest),
        )
        return seq

    def verify_chain(self) -> tuple[bool, int | None]:
        """Returns (ok, first_bad_seq)."""
        rows = self.db.query("SELECT * FROM audit_log ORDER BY seq ASC")
        prev_hash = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            seq = int(row["seq"])
            if seq != expected_seq or row["prev_hash"] != prev_hash:
                return False, seq
            digest = self._digest(seq, prev_hash, row["kind"], row["payload_json"])
            if digest != row["hash"]:
                return False, seq
            prev_hash = digest
            expected_seq += 1
        return True, None
