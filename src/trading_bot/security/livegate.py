"""Live-mode gate. LIVE stays locked until every prerequisite passes AND the
operator completes an interactive unlock ceremony AND the environment sets
LIVE_TRADING_ENABLED=true. There is no code path that promotes PAPER or
TESTNET to LIVE automatically.
"""

from __future__ import annotations

import hashlib
import secrets as sysrand
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from trading_bot.config import constants as C
from trading_bot.config.models import AppConfig
from trading_bot.core.enums import Mode
from trading_bot.core.models import parse_iso, utcnow
from trading_bot.security.qualification import QualificationEvidenceStore
from trading_bot.security.quality import verify_quality_record
from trading_bot.security.secrets import SecretProvider
from trading_bot.storage.repositories import Repositories

_WORDS = (
    "amber basalt canyon delta ember fjord garnet harbor indigo juniper kestrel lagoon "
    "marble nectar obsidian prairie quartz reef summit tundra umber violet willow zenith "
    "anchor beacon cedar dune estuary falcon glacier heron island jade krill lantern "
    "meadow nimbus orchid pebble quill raven sparrow thicket upland vertex wharf yarrow"
).split()


@dataclass(frozen=True)
class Prerequisite:
    name: str
    ok: bool
    detail: str


class LiveGate:
    def __init__(
        self,
        repos: Repositories,
        cfg: AppConfig,
        secret_provider: SecretProvider,
        config_path: str | None = None,
        project_root: str | Path = ".",
        external_alert_probe: Callable[[], dict[str, bool]] | None = None,
    ) -> None:
        self.repos = repos
        self.cfg = cfg
        self.secrets = secret_provider
        self.config_path = config_path
        self.root = Path(project_root)
        self.external_alert_probe = external_alert_probe

    # ------------------------------------------------------------------
    def prerequisites(self) -> list[Prerequisite]:
        checks: list[Prerequisite] = []
        now = utcnow()

        from trading_bot.security.qualification import get_or_create_evidence_key

        evidence = QualificationEvidenceStore(
            self.root, key=get_or_create_evidence_key(self.repos.flags)
        ).summary(decision_days=self.repos.decisions.days_histogram(Mode.PAPER))
        paper_days = evidence.paper_days
        checks.append(
            Prerequisite(
                "paper_days",
                paper_days >= C.LIVE_MIN_PAPER_DAYS,
                f"{paper_days}/{C.LIVE_MIN_PAPER_DAYS} calendar days of paper trading",
            )
        )

        n_decisions = evidence.paper_decisions
        checks.append(
            Prerequisite(
                "paper_decisions",
                n_decisions >= C.LIVE_MIN_PAPER_DECISIONS,
                f"{n_decisions}/{C.LIVE_MIN_PAPER_DECISIONS} recorded paper decisions",
            )
        )
        if not evidence.ok:
            checks.append(
                Prerequisite(
                    "qualification_evidence",
                    False,
                    "; ".join(evidence.failures),
                )
            )

        n_daily = self.repos.reports.count("daily", Mode.PAPER)
        checks.append(
            Prerequisite("paper_reports", n_daily >= 1, f"{n_daily} paper daily report(s)")
        )

        n_backtest = self.repos.reports.count("backtest", Mode.PAPER)
        backtest_files = (
            list(Path("var/reports").glob("backtest-*.json"))
            if Path("var/reports").exists()
            else []
        )
        checks.append(
            Prerequisite(
                "backtest_report",
                n_backtest >= 1 or len(backtest_files) >= 1,
                f"{n_backtest + len(backtest_files)} backtest report(s)",
            )
        )

        checks.append(self._quality_gate(now))
        checks.append(self._explicit_risk_config())
        checks.append(self._production_database())
        checks.append(self._out_of_band_alerting())

        enabled = (self.secrets.get(C.ENV_LIVE_ENABLED) or "").lower() == "true"
        checks.append(
            Prerequisite(
                "env_live_enabled",
                enabled,
                f"{C.ENV_LIVE_ENABLED}={'true' if enabled else 'NOT true'}",
            )
        )

        has_keys = bool(self.secrets.get(C.ENV_LIVE_KEY)) and bool(
            self.secrets.get(C.ENV_LIVE_SECRET)
        )
        checks.append(
            Prerequisite(
                "live_credentials",
                has_keys,
                "live API key/secret present" if has_keys else "live API key/secret missing",
            )
        )
        return checks

    def _quality_gate(self, now) -> Prerequisite:
        """A quality record must prove a REAL, current, sufficient test run.

        Rejected outright: zero/low test counts, failures, low coverage,
        missing named safety tests, stale runs, or a missing results hash.
        A bare {"passed": true} can never satisfy this gate.
        """
        path = self.root / C.QUALITY_GATE_FILE
        if not path.exists():
            return Prerequisite(
                "test_suite",
                False,
                f"{C.QUALITY_GATE_FILE} missing — run `make record-tests`",
            )
        verified = verify_quality_record(self.root, path, require_repo=True)
        if not verified.ok:
            return Prerequisite("test_suite", False, "; ".join(verified.failures))
        data = verified.record
        ran_at = parse_iso(data["ran_at"])

        failures: list[str] = []
        if not data.get("passed"):
            failures.append("suite did not pass")
        collected = int(data.get("tests_collected") or 0)
        passed_n = int(data.get("tests_passed") or 0)
        failed_n = int(data.get("tests_failed") or 0)
        if collected < C.QUALITY_MIN_TESTS:
            failures.append(f"only {collected} tests collected (min {C.QUALITY_MIN_TESTS})")
        if failed_n > 0 or passed_n < C.QUALITY_MIN_TESTS:
            failures.append(f"passed={passed_n} failed={failed_n}")
        coverage = float(data.get("coverage_percent") or 0.0)
        if coverage < C.QUALITY_MIN_COVERAGE_PCT:
            failures.append(f"coverage {coverage:.1f}% < {C.QUALITY_MIN_COVERAGE_PCT}%")
        missing = data.get("required_safety_tests_missing")
        if missing is None or missing:
            failures.append(f"required safety tests missing: {missing or 'unverified'}")
        if not data.get("results_hash"):
            failures.append("no results hash recorded")
        if data.get("git_commit") is None and data.get("git_state") != "no_repo":
            failures.append("git commit not recorded")
        if data.get("git_dirty"):
            failures.append("working tree was dirty during the test run")
        age_h = (now - ran_at).total_seconds() / 3600
        if age_h > C.QUALITY_GATE_MAX_AGE_HOURS:
            failures.append(f"run is {age_h:.1f}h old (max {C.QUALITY_GATE_MAX_AGE_HOURS}h)")
        if failures:
            return Prerequisite("test_suite", False, "; ".join(failures))
        return Prerequisite(
            "test_suite",
            True,
            f"{passed_n}/{collected} tests passed, coverage {coverage:.1f}%, " f"{age_h:.1f}h ago",
        )

    def _explicit_risk_config(self) -> Prerequisite:
        if not self.config_path:
            return Prerequisite("explicit_risk_config", False, "config path unknown")
        try:
            raw = yaml.safe_load(Path(self.config_path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return Prerequisite("explicit_risk_config", False, "config unreadable")
        risk = raw.get("risk") or {}
        required = ("max_daily_loss_pct", "max_drawdown_pct", "max_risk_per_trade_pct")
        missing = [k for k in required if k not in risk]
        return Prerequisite(
            "explicit_risk_config",
            not missing,
            "risk limits explicitly configured" if not missing else f"missing keys: {missing}",
        )

    def _out_of_band_alerting(self) -> Prerequisite:
        telegram_ok = (
            self.cfg.notifications.telegram.enabled
            and bool(self.secrets.get("TELEGRAM_BOT_TOKEN"))
            and bool(self.secrets.get("TELEGRAM_CHAT_ID"))
        )
        email_ok = (
            self.cfg.notifications.email.enabled
            and bool(self.secrets.get("SMTP_HOST"))
            and bool(self.secrets.get("SMTP_FROM"))
            and bool(self.secrets.get("SMTP_TO"))
        )
        configured = bool(telegram_ok or email_ok)
        if not configured:
            return Prerequisite(
                "out_of_band_alerting",
                False,
                "enable Telegram or email and provide its required environment secrets",
            )
        if self.external_alert_probe is None:
            return Prerequisite(
                "out_of_band_alerting",
                False,
                "external alert credentials present but connectivity was not verified",
            )
        try:
            results = self.external_alert_probe()
        except Exception:
            results = {}
        reachable = any(results.values())
        return Prerequisite(
            "out_of_band_alerting",
            reachable,
            "external alert connectivity verified"
            if reachable
            else "configured external alert channel failed its connectivity probe",
        )

    def _production_database(self) -> Prerequisite:
        is_postgres = (
            self.cfg.db.url.startswith("postgresql://") and self.repos.db.backend == "postgres"
        )
        return Prerequisite(
            "production_database",
            is_postgres,
            (
                "PostgreSQL configured"
                if is_postgres
                else "LIVE requires PostgreSQL; set DATABASE_URL outside the repository"
            ),
        )

    # ------------------------------------------------------------------
    def risk_summary(self) -> dict[str, Any]:
        r = self.cfg.risk
        return {
            "symbol": self.cfg.symbol,
            "max_position_allocation_pct": str(r.max_position_allocation_pct),
            "min_cash_reserve_pct": str(r.min_cash_reserve_pct),
            "max_risk_per_trade_pct": str(r.max_risk_per_trade_pct),
            "max_daily_loss_pct": str(r.max_daily_loss_pct),
            "max_7d_loss_pct": str(r.max_7d_loss_pct),
            "max_drawdown_pct": str(r.max_drawdown_pct),
            "max_entries_per_day": r.max_entries_per_day,
            "cooldown_after_loss_hours": r.cooldown_after_loss_hours,
            "pause_after_consecutive_losses": r.pause_after_consecutive_losses,
            "continuation_mode": self.cfg.continuation.mode.value,
        }

    @staticmethod
    def generate_phrase() -> str:
        rng = sysrand.SystemRandom()
        return " ".join(rng.choice(_WORDS) for _ in range(C.LIVE_CONFIRMATION_WORDS))

    @staticmethod
    def _hash_phrase(phrase: str) -> str:
        return hashlib.sha256(phrase.strip().lower().encode("utf-8")).hexdigest()

    def start_unlock(self) -> tuple[str, str]:
        phrase = self.generate_phrase()
        unlock_id = self.repos.live_unlock.create(
            self._hash_phrase(phrase),
            utcnow() + timedelta(hours=C.LIVE_UNLOCK_VALID_HOURS),
            self.risk_summary(),
        )
        return unlock_id, phrase

    def confirm(self, unlock_id: str, typed_phrase: str) -> bool:
        row = self.repos.live_unlock.get(unlock_id)
        if not row:
            return False
        if parse_iso(row["expires_at"]) <= utcnow():
            return False
        if self._hash_phrase(typed_phrase) != row["phrase_hash"]:
            return False
        self.repos.live_unlock.confirm(unlock_id)
        self.repos.events.approval("live_unlock_confirmed", None, "operator")
        return True

    def is_unlocked(self) -> bool:
        return self.repos.live_unlock.has_valid_unlock()

    # ------------------------------------------------------------------
    def assert_live_start_allowed(self) -> None:
        """Called at engine startup in LIVE mode. Raises with details when locked."""
        failures = [p for p in self.prerequisites() if not p.ok]
        if failures:
            details = "; ".join(f"{p.name}: {p.detail}" for p in failures)
            raise PermissionError(f"LIVE mode locked — unmet prerequisites: {details}")
        if not self.is_unlocked():
            raise PermissionError(
                "LIVE mode locked — run `python -m trading_bot live unlock` and complete "
                "the confirmation ceremony (valid for "
                f"{C.LIVE_UNLOCK_VALID_HOURS}h)."
            )
