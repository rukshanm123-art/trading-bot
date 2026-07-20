"""Scheduling helpers: single-instance lock + interval tasks.

The instance lock (DB row + heartbeat) prevents two engine processes from
trading the same database concurrently — the second process refuses to start
unless the first one's heartbeat is stale.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from trading_bot.core.models import iso, parse_iso
from trading_bot.exchange.interface import Clock
from trading_bot.storage.db import Database

log = logging.getLogger(__name__)

STALE_AFTER_S = 120


class InstanceLock:
    def __init__(self, db: Database, clock: Clock, name: str = "engine") -> None:
        self.db = db
        self.clock = clock
        self.name = name
        self.instance_id = uuid.uuid4().hex[:12]
        self.acquired = False

    def acquire(self) -> bool:
        """Atomic acquisition: success is decided ONLY by affected-row counts,
        so two processes racing for a stale lock cannot both win (the loser's
        conditional UPDATE matches zero rows)."""
        now = self.clock.now()
        import sqlite3

        try:
            inserted = self.db.execute_rowcount(
                "INSERT INTO instance_lock (name, instance_id, heartbeat_at) "
                "VALUES (?, ?, ?) ON CONFLICT(name) DO NOTHING",
                (self.name, self.instance_id, iso(now)),
            )
        except sqlite3.IntegrityError:
            inserted = 0
        if inserted == 1:
            self.acquired = True
            return True

        stale_cutoff = iso(now - timedelta(seconds=STALE_AFTER_S))
        updated = self.db.execute_rowcount(
            "UPDATE instance_lock SET instance_id = ?, heartbeat_at = ? "
            "WHERE name = ? AND (instance_id = ? OR heartbeat_at < ?)",
            (self.instance_id, iso(now), self.name, self.instance_id, stale_cutoff),
        )
        if updated == 1:
            self.acquired = True
            row = self.db.query_one(
                "SELECT heartbeat_at FROM instance_lock WHERE name = ?", (self.name,)
            )
            log.info("instance lock acquired (%s)", self.instance_id)
            _ = row
            return True

        row = self.db.query_one("SELECT * FROM instance_lock WHERE name = ?", (self.name,))
        holder = row["instance_id"] if row else "unknown"
        age = (now - parse_iso(row["heartbeat_at"])).total_seconds() if row else 0
        log.error(
            "another engine instance (%s) holds the lock (heartbeat %ss ago); refusing to start",
            holder,
            int(age),
        )
        return False

    def heartbeat(self) -> None:
        if not self.acquired:
            return
        self.db.execute(
            "UPDATE instance_lock SET heartbeat_at = ? WHERE name = ? AND instance_id = ?",
            (iso(self.clock.now()), self.name, self.instance_id),
        )

    def release(self) -> None:
        if not self.acquired:
            return
        self.db.execute(
            "DELETE FROM instance_lock WHERE name = ? AND instance_id = ?",
            (self.name, self.instance_id),
        )
        self.acquired = False


class IntervalTask:
    """Run a callable at most once per interval, driven by the engine loop."""

    def __init__(self, interval_s: int, fn: Callable[[], Any], clock: Clock, name: str) -> None:
        self.interval = timedelta(seconds=interval_s)
        self.fn = fn
        self.clock = clock
        self.name = name
        self.last_run: datetime | None = None

    def maybe_run(self) -> bool:
        now = self.clock.now()
        if self.last_run is not None and now - self.last_run < self.interval:
            return False
        self.last_run = now
        try:
            self.fn()
        except Exception:
            log.exception("scheduled task %s failed", self.name)
        return True
