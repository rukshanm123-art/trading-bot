"""Component-level health. A healthy HTTP process is NOT proof trading is safe —
each concern is reported separately and 'trading_permitted' is its own field."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trading_bot.core.enums import ComponentHealth


@dataclass
class HealthState:
    application: ComponentHealth = ComponentHealth.OK
    exchange: ComponentHealth = ComponentHealth.DEGRADED
    market_data: ComponentHealth = ComponentHealth.DEGRADED
    database: ComponentHealth = ComponentHealth.DEGRADED
    risk_engine: ComponentHealth = ComponentHealth.OK
    trading_permitted: bool = False
    kill_switch_active: bool = False
    last_cycle_at: str | None = None
    last_reconciliation_at: str | None = None
    notes: dict[str, str] = field(default_factory=dict)


class HealthRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = HealthState()

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)

    def note(self, key: str, value: str) -> None:
        with self._lock:
            self._state.notes[key] = value

    def heartbeat(self) -> None:
        with self._lock:
            self._state.last_cycle_at = datetime.now(UTC).isoformat()

    def reset(self) -> None:
        with self._lock:
            self._state = HealthState()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self._state
            return {
                "application": s.application.value,
                "exchange": s.exchange.value,
                "market_data": s.market_data.value,
                "database": s.database.value,
                "risk_engine": s.risk_engine.value,
                "trading_permitted": s.trading_permitted,
                "kill_switch_active": s.kill_switch_active,
                "last_cycle_at": s.last_cycle_at,
                "last_reconciliation_at": s.last_reconciliation_at,
                "notes": dict(s.notes),
            }

    def live(self) -> bool:
        """Liveness: the process/loop is running."""
        return self._state.application != ComponentHealth.FAILED

    def ready(self) -> bool:
        """Readiness: safe to evaluate the pipeline (NOT the same as trading permitted)."""
        s = self._state
        return s.application == ComponentHealth.OK and s.database == ComponentHealth.OK


HEALTH = HealthRegistry()
