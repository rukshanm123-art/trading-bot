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
        now = self.clock.now()
        row = self.db.query_one("SELECT * FROM instance_lock WHERE name = ?", (self.name,))
        if row is None:
            self.db.execute(
                "INSERT INTO instance_lock (name, instance_id, heartbeat_at) VALUES (?, ?, ?)",
                (self.name, self.instance_id, iso(now)),
            )
            self.acquired = True
            return True
        age = (now - parse_iso(row["heartbeat_at"])).total_seconds()
        if row["instance_id"] == self.instance_id or age > STALE_AFTER_S:
            self.db.execute(
                "UPDATE instance_lock SET instance_id = ?, heartbeat_at = ? WHERE name = ?",
                (self.instance_id, iso(now), self.name),
            )
            if age > STALE_AFTER_S:
                log.warning("stole stale instance lock (previous heartbeat %ss ago)", int(age))
            self.acquired = True
            return True
        log.error(
            "another engine instance (%s) holds the lock (heartbeat %ss ago); refusing to start",
            row["instance_id"],
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
