"""Kill switches — five independent mechanisms, any one halts new entries.

1. CLI:            ``python -m trading_bot stop`` (sets DB flag + STOP file)
2. Environment:    TRADING_KILL_SWITCH=true
3. Database flag:  control_flags.kill_switch
4. Emergency file: ./STOP_TRADING (works even if DB/CLI are unavailable)
5. Circuit breaker: automatic (see control/circuit.py) — latches into the DB
   flag when it trips hard.

A latched kill switch requires explicit manual reset (``resume`` command).
The env-var switch can only be cleared by fixing the environment.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from trading_bot.config import constants as C
from trading_bot.core.enums import KillSwitchSource
from trading_bot.storage.repositories import Repositories

log = logging.getLogger(__name__)


class KillSwitch:
    def __init__(
        self,
        repos: Repositories,
        stop_file_dir: str | Path = ".",
        env: dict[str, str] | None = None,
    ) -> None:
        self.repos = repos
        self.stop_file = Path(stop_file_dir) / C.STOP_FILE_NAME
        self._env = env  # None -> live os.environ lookup each check

    def _env_value(self) -> str:
        source = self._env if self._env is not None else os.environ
        return source.get(C.ENV_KILL_SWITCH, "").strip().lower()

    # ------------------------------------------------------------------
    def check(self) -> tuple[bool, str]:
        """Returns (active, 'source:reason')."""
        if self._env_value() in ("true", "1", "yes"):
            return True, f"{KillSwitchSource.ENV.value}:{C.ENV_KILL_SWITCH}=true"
        if self.stop_file.exists():
            try:
                reason = self.stop_file.read_text(encoding="utf-8").strip()[:120]
            except OSError:
                reason = ""
            return True, f"{KillSwitchSource.FILE.value}:{reason or 'STOP_TRADING present'}"
        raw = self.repos.flags.get(self.repos.flags.KILL_SWITCH)
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            # A legacy/plain value ("true") parses as a bool or a non-dict;
            # only a dict carries structured source/reason.
            if isinstance(parsed, dict):
                data = parsed
            else:
                data = {"active": raw.strip().lower() in ("true", "1", "yes"), "reason": raw}
            if data.get("active"):
                return True, f"{data.get('source', 'db_flag')}:{data.get('reason', '')}"
        return False, ""

    # ------------------------------------------------------------------
    def activate(self, source: KillSwitchSource, reason: str) -> bool:
        """Halt trading. Returns whether the FILE backstop was also written.

        The DB flag is authoritative and is set first. The file is a secondary,
        independent backstop for when the database or CLI is unavailable — but
        it cannot be written on a read-only rootfs, which is exactly what the
        hardened container images use. Callers must surface that: telling an
        operator a backstop exists when it does not is worse than not having
        it, because it is relied on during an incident.
        """
        payload = json.dumps({"active": True, "source": source.value, "reason": reason})
        self.repos.flags.set(self.repos.flags.KILL_SWITCH, payload)
        file_written = True
        try:
            self.stop_file.write_text(f"{source.value}: {reason}\n", encoding="utf-8")
        except OSError as exc:
            file_written = False
            log.error("could not write %s: %s", self.stop_file, exc)
        self.repos.events.killswitch(source.value, True, reason)
        log.critical("KILL SWITCH ACTIVATED (%s): %s", source.value, reason)
        return file_written

    def reset(self, actor: str, note: str = "") -> list[str]:
        """Manual reset. Returns a list of blockers that could NOT be cleared."""
        blockers: list[str] = []
        self.repos.flags.set(
            self.repos.flags.KILL_SWITCH, json.dumps({"active": False, "reason": note})
        )
        if self.stop_file.exists():
            try:
                self.stop_file.unlink()
            except OSError as exc:
                blockers.append(f"could not remove {self.stop_file}: {exc}")
        if self._env_value() in ("true", "1", "yes"):
            blockers.append(
                f"{C.ENV_KILL_SWITCH} is still 'true' in the environment; unset it and restart"
            )
        self.repos.events.killswitch("manual_reset", False, f"{actor}: {note}")
        log.warning("kill switch reset by %s (%s)", actor, note or "no note")
        return blockers
