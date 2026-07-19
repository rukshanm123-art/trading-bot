"""Exchange error taxonomy."""

from __future__ import annotations


class ExchangeError(RuntimeError):
    """Base for all exchange-related failures."""


class ExchangeUnavailable(ExchangeError):
    """Network/5xx/rate-limit failure after retries. Retryable at a later cycle."""


class ExchangeAuthError(ExchangeError):
    """Bad or missing credentials, or permission problems."""


class OrderRejectedError(ExchangeError):
    """Exchange refused the order (filters, balance, symbol status)."""


class OrderNotFoundError(ExchangeError):
    """The exchange explicitly reported that the order id/client id does not exist."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class OrderStateUnknownError(ExchangeError):
    """Submission outcome is uncertain (timeout mid-flight). NEVER blind-retry:
    the order may exist on the exchange. Query by client order id first."""


class EndpointMismatchError(ExchangeError):
    """Mode / base-URL / credential-source mismatch. Always fatal at startup."""


class DataUnavailableError(ExchangeError):
    """Market data could not be fetched or failed validation."""
