"""Secret redaction for logs and exceptions.

Two layers:
1. Value-based: every secret value registered at startup is replaced anywhere
   it appears in any log line or formatted exception.
2. Pattern-based: strings that *look* like credentials (api keys, signatures,
   auth headers) are masked even if they were never registered.
"""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "***REDACTED***"

_PATTERNS = [
    # key=value / "key": "value" forms for sensitive key names
    re.compile(
        r"(?i)((?:api[_-]?key|api[_-]?secret|secret[_-]?key|signature|password|passwd|"
        r"authorization|x-mbx-apikey|bot[_-]?token|access[_-]?token)"
        r"\s*[=:]\s*[\"']?)([A-Za-z0-9+/=_\-\.]{8,})",
    ),
    # long base64/hex blobs following "signature=" in query strings
    re.compile(r"(signature=)([0-9a-fA-F]{32,})"),
]


class Redactor:
    """Process-wide registry of secret values + scrubbing helpers."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str | None) -> None:
        if value and len(value) >= 6:
            with self._lock:
                self._secrets.add(value)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            secrets = list(self._secrets)
        for s in secrets:
            if s in text:
                text = text.replace(s, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(rf"\g<1>{REDACTED}", text)
        return text

    def redact_mapping(self, data: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in data.items():
            if isinstance(v, str):
                out[k] = self.redact(v)
            elif isinstance(v, dict):
                out[k] = self.redact_mapping(v)  # type: ignore[arg-type]
            else:
                out[k] = v
        return out


GLOBAL_REDACTOR = Redactor()


class RedactionFilter(logging.Filter):
    """Attach to every handler: scrubs message, args and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = GLOBAL_REDACTOR.redact(msg)
        if clean != msg:
            record.msg = clean
            record.args = ()
        if record.exc_text:
            record.exc_text = GLOBAL_REDACTOR.redact(record.exc_text)
        return True


def redact_exception_message(exc: BaseException) -> str:
    return GLOBAL_REDACTOR.redact(f"{type(exc).__name__}: {exc}")
