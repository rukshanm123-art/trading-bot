"""In-process metrics registry with Prometheus text exposition."""

from __future__ import annotations

import threading

Number = int | float


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, value: Number = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + float(value)

    def set_gauge(self, name: str, value: Number) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def get(self, name: str) -> float:
        with self._lock:
            return self._counters.get(name, self._gauges.get(name, 0.0))

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, value in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name} {value}")
            for name, value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()
