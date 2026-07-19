"""Secret access. Environment-based provider; values auto-register with the redactor.

No secret ever lives in a config file, a log line, or the database. The
provider interface exists so a real secret manager (Vault, AWS SM, 1Password
CLI) can be plugged in without touching call sites.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from trading_bot.security.redaction import GLOBAL_REDACTOR


class SecretProvider(ABC):
    @abstractmethod
    def get(self, name: str) -> str | None: ...

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise KeyError(f"Required secret '{name}' is not set")
        return value


class EnvSecretProvider(SecretProvider):
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env if env is not None else dict(os.environ)

    def get(self, name: str) -> str | None:
        value = self._env.get(name)
        if value:
            value = value.strip()
        if value:
            GLOBAL_REDACTOR.register(value)
            return value
        return None


class StaticSecretProvider(SecretProvider):
    """For tests only."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        for v in values.values():
            GLOBAL_REDACTOR.register(v)

    def get(self, name: str) -> str | None:
        return self._values.get(name)
