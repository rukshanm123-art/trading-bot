"""Persistent, manually acknowledged consecutive-loss risk brake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trading_bot.config.models import AppConfig
from trading_bot.core.models import parse_iso
from trading_bot.core.types import ZERO, dec
from trading_bot.exchange.interface import Clock
from trading_bot.storage.repositories import Repositories


class LossPauseStateError(RuntimeError):
    """Stored acknowledgement state cannot be trusted; callers must fail closed."""


class LossPauseAcknowledgementError(PermissionError):
    def __init__(self, blockers: list[str]) -> None:
        self.blockers = tuple(blockers)
        super().__init__("; ".join(blockers))


@dataclass(frozen=True)
class ConsecutiveLossPauseState:
    raw_streak: int
    effective_streak: int
    threshold: int
    active: bool
    active_since: datetime | None
    minimum_ack_at: datetime | None
    latest_acknowledgement: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        ack = self.latest_acknowledgement
        return {
            "raw_streak": self.raw_streak,
            "effective_streak": self.effective_streak,
            "threshold": self.threshold,
            "active": self.active,
            "active_since": self.active_since.isoformat() if self.active_since else None,
            "minimum_ack_at": self.minimum_ack_at.isoformat() if self.minimum_ack_at else None,
            "clears_automatically": False,
            "latest_acknowledgement": (
                {
                    "id": ack["id"],
                    "watermark_position_id": ack["watermark_position_id"],
                    "watermark_closed_at": ack["watermark_closed_at"],
                    "streak_count": int(ack["streak_count"]),
                    "acknowledged_at": ack["acknowledged_at"],
                    "actor": ack["actor"],
                    "note": ack["note"],
                }
                if ack
                else None
            ),
        }


@dataclass(frozen=True)
class LossPauseAcknowledgementResult:
    record: dict[str, Any]
    created: bool
    state: ConsecutiveLossPauseState


class ConsecutiveLossPauseService:
    """Derive the effective loss streak from an append-only review watermark."""

    RECONCILIATION_MAX_AGE = timedelta(hours=1)
    ACK_COMMAND = (
        "python -m trading_bot --config <same-config> risk acknowledge-loss-pause "
        "--note '<review and decision>'"
    )

    def __init__(self, repos: Repositories, cfg: AppConfig, clock: Clock) -> None:
        self.repos = repos
        self.cfg = cfg
        self.clock = clock
        self.mode = cfg.mode

    def _validated_watermark(self, ack: dict[str, Any]) -> tuple[dict[str, Any], datetime]:
        try:
            marker = self.repos.positions.get_closed(self.mode, str(ack["watermark_position_id"]))
            watermark_closed_at = parse_iso(str(ack["watermark_closed_at"]))
            acknowledged_at = parse_iso(str(ack["acknowledged_at"]))
            streak_count = int(ack["streak_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LossPauseStateError("invalid consecutive-loss acknowledgement record") from exc

        if marker is None:
            raise LossPauseStateError("loss-pause watermark position is missing")
        marker_closed_at = parse_iso(str(marker["closed_at"]))
        if marker_closed_at != watermark_closed_at:
            raise LossPauseStateError("loss-pause watermark timestamp does not match its position")
        if marker.get("realized_pnl") is None or dec(marker["realized_pnl"]) >= ZERO:
            raise LossPauseStateError("loss-pause watermark does not reference a losing position")
        if streak_count < self.cfg.risk.pause_after_consecutive_losses:
            raise LossPauseStateError("loss-pause acknowledgement recorded an insufficient streak")
        if acknowledged_at < watermark_closed_at:
            raise LossPauseStateError("loss-pause acknowledgement predates its watermark")
        if not str(ack.get("actor") or "").strip() or not str(ack.get("note") or "").strip():
            raise LossPauseStateError("loss-pause acknowledgement lacks operator evidence")
        return marker, watermark_closed_at

    def status(self) -> ConsecutiveLossPauseState:
        raw_streak = self.repos.positions.consecutive_losses(self.mode)
        ack = self.repos.loss_acknowledgements.latest(self.mode)
        closed_after: datetime | None = None
        if ack is not None:
            _marker, closed_after = self._validated_watermark(ack)

        effective_streak = self.repos.positions.consecutive_losses(self.mode, closed_after)
        active = effective_streak >= self.cfg.risk.pause_after_consecutive_losses
        latest = self.repos.positions.latest_closed(self.mode)
        active_since: datetime | None = None
        minimum_ack_at: datetime | None = None
        if active:
            if latest is None or latest.get("realized_pnl") is None:
                raise LossPauseStateError("active loss-pause has no closing loss")
            if dec(latest["realized_pnl"]) >= ZERO:
                raise LossPauseStateError("active loss-pause does not end in a loss")
            parsed_active_since = parse_iso(str(latest["closed_at"]))
            active_since = parsed_active_since
            minimum_ack_at = parsed_active_since + timedelta(
                hours=self.cfg.risk.cooldown_after_loss_hours
            )

        return ConsecutiveLossPauseState(
            raw_streak=raw_streak,
            effective_streak=effective_streak,
            threshold=self.cfg.risk.pause_after_consecutive_losses,
            active=active,
            active_since=active_since,
            minimum_ack_at=minimum_ack_at,
            latest_acknowledgement=ack,
        )

    def active_alert_message(self, state: ConsecutiveLossPauseState | None = None) -> str:
        state = state or self.status()
        if not state.active:
            raise ValueError("consecutive-loss pause is not active")
        earliest = (
            state.minimum_ack_at.isoformat() if state.minimum_ack_at else "the review interval"
        )
        return (
            f"{state.effective_streak} consecutive losses — entries are latched off by "
            "the risk engine. This pause DOES NOT CLEAR WITH TIME. "
            f"Operator review may be acknowledged after {earliest} with: {self.ACK_COMMAND}"
        )

    def acknowledge(self, actor: str, note: str) -> LossPauseAcknowledgementResult:
        actor = actor.strip()
        note = note.strip()
        blockers: list[str] = []
        if not actor:
            blockers.append("operator identity is required")
        if not note:
            blockers.append("a non-empty review note is required")

        state = self.status()
        latest = self.repos.positions.latest_closed(self.mode)
        if not state.active:
            ack = state.latest_acknowledgement
            if (
                ack is not None
                and latest is not None
                and (latest["id"] == ack["watermark_position_id"])
            ):
                if blockers:
                    raise LossPauseAcknowledgementError(blockers)
                return LossPauseAcknowledgementResult(ack, False, state)
            blockers.append("the consecutive-loss pause is not active")

        now = self.clock.now()
        if state.minimum_ack_at is not None and now < state.minimum_ack_at:
            blockers.append(
                "minimum review interval has not elapsed "
                f"(earliest acknowledgement {state.minimum_ack_at.isoformat()})"
            )
        # Deliberately test position status, not wallet balance: exchange-minimum
        # dust is not an open position and must not recreate a permanent deadlock.
        if self.repos.positions.open_position(self.mode) is not None:
            blockers.append("an open position still exists")
        unknown = len(self.repos.orders.unknown_orders(self.mode))
        active_entries = len(self.repos.orders.active_entry_orders(self.mode))
        active_exits = len(self.repos.orders.active_exit_orders(self.mode))
        if unknown:
            blockers.append(f"{unknown} order(s) have unknown execution state")
        if active_entries:
            blockers.append(f"{active_entries} entry order(s) are still active")
        if active_exits:
            blockers.append(f"{active_exits} exit order(s) are still active")
        if self.repos.flags.is_true(self.repos.flags.RECONCILIATION_BLOCK):
            blockers.append("the reconciliation block is active")
        reconciliation = self.repos.events.last_reconciliation(self.mode)
        if reconciliation is None:
            blockers.append("no reconciliation result is recorded")
        elif not bool(reconciliation["ok"]):
            blockers.append("the latest reconciliation did not pass")
        else:
            try:
                reconciliation_at = parse_iso(str(reconciliation["ts"]))
            except (KeyError, TypeError, ValueError):
                blockers.append("the latest reconciliation timestamp is invalid")
            else:
                age = now - reconciliation_at
                if age < timedelta(0):
                    blockers.append("the latest reconciliation timestamp is in the future")
                elif age > self.RECONCILIATION_MAX_AGE:
                    blockers.append(
                        "the latest reconciliation is stale "
                        f"({int(age.total_seconds())}s old; maximum 3600s)"
                    )

        if blockers:
            raise LossPauseAcknowledgementError(blockers)
        if latest is None or state.active_since is None:
            raise LossPauseStateError("cannot resolve the loss-pause watermark")

        record, created = self.repos.loss_acknowledgements.insert(
            mode=self.mode,
            watermark_position_id=str(latest["id"]),
            watermark_closed_at=state.active_since,
            streak_count=state.effective_streak,
            acknowledged_at=now,
            actor=actor,
            note=note,
        )
        return LossPauseAcknowledgementResult(record, created, self.status())
