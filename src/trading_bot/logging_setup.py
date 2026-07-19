"""Structured JSON logging with mandatory secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from trading_bot.correlation import get_correlation_id
from trading_bot.security.redaction import GLOBAL_REDACTOR, RedactionFilter

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return GLOBAL_REDACTOR.redact(json.dumps(payload, default=str))


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        base = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}"
        if record.exc_info and record.exc_info[0] is not None:
            base += "\n" + self.formatException(record.exc_info)
        return GLOBAL_REDACTOR.redact(base)


def setup_logging(
    log_dir: str | None = "var/logs",
    level: int = logging.INFO,
    json_console: bool = False,
) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    redaction = RedactionFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(JsonFormatter() if json_console else HumanFormatter())
    console.addFilter(redaction)
    root.addHandler(console)

    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path / "bot.jsonl", encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    # Quiet noisy libraries; they may include URLs with query strings.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
