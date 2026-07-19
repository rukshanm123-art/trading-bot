"""Config loading: YAML file -> validated AppConfig + integrity hash.

Optional integrity check: if `<config>.sha256` exists next to the config file,
the file's checksum must match or the loader refuses to start (tamper guard).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from trading_bot.config.models import AppConfig


class ConfigError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def config_hash(cfg: AppConfig) -> str:
    canonical = cfg.model_dump_json()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_config(path: str | Path, env: dict[str, str] | None = None) -> AppConfig:
    env = dict(os.environ if env is None else env)
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")

    sidecar = p.with_suffix(p.suffix + ".sha256")
    if sidecar.exists():
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        actual = _sha256_file(p)
        if expected != actual:
            raise ConfigError(
                f"Config checksum mismatch for {p} (expected {expected[:12]}…, got {actual[:12]}…). "
                "Refusing to start with a modified config; regenerate the .sha256 sidecar "
                "deliberately if the change is intended."
            )

    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Top level of {p} must be a mapping")

    # Environment overrides (secrets and deploy-specific values only).
    db_url = env.get("DATABASE_URL", "").strip()
    if db_url:
        raw.setdefault("db", {})
        if isinstance(raw["db"], dict):
            raw["db"]["url"] = db_url

    try:
        cfg = AppConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"Config validation failed for {p}: {exc}") from exc
    return cfg


def write_sidecar_checksum(path: str | Path) -> str:
    p = Path(path)
    digest = _sha256_file(p)
    p.with_suffix(p.suffix + ".sha256").write_text(digest + "\n", encoding="utf-8")
    return digest
