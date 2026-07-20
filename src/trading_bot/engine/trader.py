"""Trading engine: wiring (bootstrap) + the main evaluation loop.

Pipeline per closed candle (deterministic, in order):
  1. market-data validation
  2. strategy signal generation
  3. risk-management approval (single-use signed token)
  4. exchange-rule validation (inside sizing + adapter)
  5. order preview (persisted intent + audit record)
  6. execution gateway (the only path to the exchange)
  7. post-trade reconciliation (fills -> position -> snapshots)

Exit monitoring (protective stop) runs every cycle, not just per candle.
No AI/LLM component participates anywhere in this loop.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import signal
import threading
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_bot.ai.analyst import TemplateAnalyst
from trading_bot.config import constants as C
from trading_bot.config.loader import config_hash
from trading_bot.config.models import AppConfig
from trading_bot.control.approval import ApprovalService
from trading_bot.control.circuit import CircuitBreaker, CircuitStatus
from trading_bot.control.killswitch import KillSwitch
from trading_bot.core.enums import (
    ComponentHealth,
    EmergencyPositionPolicy,
    EndpointEnvironment,
    Mode,
    OrderState,
    OrderType,
    ReasonCode,
    SignalAction,
)
from trading_bot.core.models import (
    Candle,
    DecisionRecord,
    ExecutionResult,
    OrderResponse,
    PositionState,
    RiskDecision,
    SignalDecision,
    SymbolRules,
)
from trading_bot.core.types import ZERO, dec
from trading_bot.correlation import new_correlation_id
from trading_bot.engine.scheduler import InstanceLock, IntervalTask
from trading_bot.exchange.binance import BinanceAdapter, BinancePublicData
from trading_bot.exchange.errors import ExchangeUnavailable, OrderNotFoundError, OrderRejectedError
from trading_bot.exchange.interface import Clock, ExchangeAdapter, MarketDataSource, RealClock
from trading_bot.exchange.paper import PaperExchange
from trading_bot.execution.exit_monitor import ExitCheck, check_protective_exit
from trading_bot.execution.gateway import ExecutionGateway
from trading_bot.market_data.fixture import FixtureDataSource
from trading_bot.market_data.service import MarketDataService
from trading_bot.market_data.validation import cross_check_quote, validate_candles
from trading_bot.monitoring.health import HEALTH
from trading_bot.monitoring.metrics import METRICS
from trading_bot.monitoring.server import MonitoringServer
from trading_bot.notifications.adapters import (
    ConsoleNotifier,
    EmailNotifier,
    NotificationHub,
    Notifier,
    TelegramNotifier,
)
from trading_bot.portfolio.accounting import PortfolioService
from trading_bot.portfolio.reconciliation import Reconciler
from trading_bot.reporting.daily import DailyReportBuilder
from trading_bot.risk.engine import GateContext, RiskEngine
from trading_bot.risk.sizing import size_exit_qty
from trading_bot.risk.state import RiskStateService
from trading_bot.security.livegate import LiveGate
from trading_bot.security.secrets import EnvSecretProvider, SecretProvider
from trading_bot.storage.audit import AuditLog
from trading_bot.storage.db import Database
from trading_bot.storage.repositories import Repositories
from trading_bot.strategies.registry import build_strategy

log = logging.getLogger(__name__)


class _FixtureClock:
    """Clock driven by the fixture cursor (paper fixture runs / backtests)."""

    def __init__(self, fixture: FixtureDataSource) -> None:
        self.fixture = fixture

    def now(self) -> datetime:
        return self.fixture.now()


def default_rules(symbol: str) -> SymbolRules:
    """Offline fallback rules matching Binance BTCUSDT filters closely enough
    for fixture-driven paper runs and backtests."""
    quote = "USDT" if symbol.endswith("USDT") else symbol[-3:]
    base = symbol[: -len(quote)]
    return SymbolRules(
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        status="TRADING",
        min_qty=dec("0.00001"),
        step_size=dec("0.00001"),
        tick_size=dec("0.01"),
        min_notional=dec("5"),
    )


class TradingEngine:
    def __init__(
        self,
        cfg: AppConfig,
        config_path: str | None = None,
        secrets: SecretProvider | None = None,
        db: Database | None = None,
        transport=None,
        project_root: str | Path = ".",
        migrations_dir: str | Path | None = None,
        close_db_on_shutdown: bool = True,
    ) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.root = Path(project_root)
        self.close_db_on_shutdown = close_db_on_shutdown
        self.secrets = secrets or EnvSecretProvider()
        self.cfg_hash = config_hash(cfg)

        self.db = db or Database(cfg.db.url)
        mdir = Path(migrations_dir) if migrations_dir else self.root / "migrations"
        self.db.migrate(mdir)
        self.repos = Repositories(self.db)
        self.audit = AuditLog(self.db)
        self.repos.config_versions.record(self.cfg_hash, cfg.model_dump_json())

        # ---- data source, clock, rules -------------------------------
        self.fixture: FixtureDataSource | None = None
        self.data_source: MarketDataSource
        if cfg.data.source == "fixture":
            if cfg.data.fixture_path is None:
                raise ValueError("fixture data source requires a fixture_path")
            self.fixture = FixtureDataSource(
                self.root / cfg.data.fixture_path
                if not Path(cfg.data.fixture_path).is_absolute()
                else cfg.data.fixture_path,
                cfg.symbol,
                cfg.interval,
                spread_bps=str(cfg.paper.spread_bps),
                start_index=None,
            )
            self.clock: Clock = _FixtureClock(self.fixture)
            self.data_source = self.fixture
            self.rules = default_rules(cfg.symbol)
            # ALL persistence timestamps must follow simulated time in replays
            from trading_bot.core.models import set_time_provider

            set_time_provider(self.clock.now)
        else:
            self.clock = RealClock()
            public_env = (
                EndpointEnvironment.LIVE_PUBLIC
                if cfg.mode == Mode.PAPER
                else EndpointEnvironment.TESTNET
                if cfg.mode == Mode.TESTNET
                else EndpointEnvironment.LIVE
            )
            public = BinancePublicData(
                environment=public_env,
                transport=transport,
                on_api_error=self.repos.events.api_error,
            )
            self.data_source = public
            self.rules = (
                public.get_rules(cfg.symbol)
                if cfg.mode == Mode.PAPER
                else default_rules(cfg.symbol)
            )

        # ---- adapter ---------------------------------------------------
        if cfg.mode == Mode.PAPER:
            self.adapter: ExchangeAdapter = PaperExchange(
                self.rules, cfg.paper, self.data_source, self.repos.sim_state, self.clock
            )
            fee_bps = cfg.paper.taker_fee_bps
        else:
            self.adapter = BinanceAdapter(
                cfg.mode,
                self.secrets,
                transport=transport,
                on_api_error=self.repos.events.api_error,
            )
            self.data_source = self.adapter.public
            self.rules = self.adapter.get_rules(cfg.symbol)
            fee_bps = Decimal("10")  # 0.10% taker; refine per-account if needed

        # ---- services ---------------------------------------------------
        self.strategy = build_strategy(cfg.strategy)
        if self.fixture is not None:
            # replay starts once the warmup window is available
            self.fixture.cursor = min(self.strategy.warmup + 1, len(self.fixture.candles) - 1)
        self.market_data = MarketDataService(
            self.data_source, cfg, self.clock, min_candles=self.strategy.warmup
        )
        self.risk_engine = RiskEngine(cfg, fee_bps)
        self.risk_state = RiskStateService(self.repos, cfg, self.clock)
        self.kill_switch = KillSwitch(self.repos, stop_file_dir=self.root)
        self.circuit = CircuitBreaker(cfg, self.kill_switch)
        self.approval = ApprovalService(self.repos, cfg, self.clock)
        self.gateway = ExecutionGateway(
            self.adapter,
            self.repos,
            self.risk_engine,
            cfg.mode,
            self.audit,
            self.clock,
            kill_switch_check=self.kill_switch.check,
        )
        self.portfolio = PortfolioService(self.repos, cfg.mode, self.rules, fee_bps=fee_bps)
        self.reconciler = Reconciler(
            self.adapter, self.repos, cfg, self.rules, self.gateway, self.clock, self.portfolio
        )

        notifiers: list[Notifier] = [ConsoleNotifier()] if cfg.notifications.console else []
        if cfg.notifications.telegram.enabled:
            notifiers.append(TelegramNotifier(self.secrets))
        if cfg.notifications.email.enabled:
            notifiers.append(EmailNotifier(self.secrets, cfg.notifications.email.use_tls))
        self.hub = NotificationHub(notifiers)
        self.reports = DailyReportBuilder(self.repos, cfg, self.rules, self.clock, self.hub)
        self.analyst = TemplateAnalyst()

        self.live_gate = LiveGate(self.repos, cfg, self.secrets, config_path, self.root)
        self.lock = InstanceLock(self.db, self.clock)
        self.monitor: MonitoringServer | None = None

        # runtime state
        self._stop = threading.Event()
        self._last_candle_ts: datetime | None = None
        self._db_ok = True
        self._kill_policy_done = False
        self._shutdown_done = False
        self._native_stops = cfg.execution.use_native_stops
        self._breach_cycles = 0
        self._alerted_codes_day: tuple[str, set[str]] = ("", set())
        self._reconcile_task = IntervalTask(1800, self._reconcile, self.clock, "reconcile")
        self._integrity_task = IntervalTask(3600, self._check_db, self.clock, "db_integrity")

    # ==================================================================
    def request_stop(self) -> None:
        self._stop.set()

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.request_stop())
            except ValueError:
                pass

    # ==================================================================
    def startup_checks(self) -> None:
        mode = self.cfg.mode
        if mode == Mode.LIVE:
            log.critical(
                "\n" + "=" * 70 + "\n  LIVE TRADING MODE — REAL FUNDS AT RISK"
                "\n  Symbol %s | risk/trade %s%% | daily stop %s%% | drawdown stop %s%%"
                "\n  Kill switches: CLI stop | %s=true | DB flag | %s file | circuit breaker"
                "\n" + "=" * 70,
                self.cfg.symbol,
                self.cfg.risk.max_risk_per_trade_pct,
                self.cfg.risk.max_daily_loss_pct,
                self.cfg.risk.max_drawdown_pct,
                C.ENV_KILL_SWITCH,
                C.STOP_FILE_NAME,
            )
            self.live_gate.assert_live_start_allowed()
            if not isinstance(self.adapter, BinanceAdapter):
                raise RuntimeError("LIVE mode requires a signed Binance adapter")
            self.adapter.sync_clock()
            self.adapter.verify_key_permissions()
            self._apply_account_fee()
            self._warn_if_console_only_alerts()
        elif mode == Mode.TESTNET:
            if not isinstance(self.adapter, BinanceAdapter):
                raise RuntimeError("TESTNET mode requires a signed Binance adapter")
            self.adapter.sync_clock()
            self._apply_account_fee()
            self._warn_if_console_only_alerts()
            log.info("TESTNET mode — official Spot Testnet, no real funds")
        else:
            log.info(
                "PAPER mode — simulated balances (start %s %s), %s data",
                self.cfg.paper.starting_quote,
                self.rules.quote_asset,
                self.cfg.data.source,
            )

    def _apply_account_fee(self) -> None:
        """Use the account's REAL taker commission for sizing instead of the
        conservative default, when the exchange reports it."""
        if not isinstance(self.adapter, BinanceAdapter):
            return
        fee = self.adapter.taker_fee_bps(self.cfg.symbol)
        if fee is not None and fee > ZERO:
            self.risk_engine.fee_bps = fee
            self.portfolio.fee_bps = fee
            log.info("account taker commission: %s bps (applied to sizing)", fee)

    def _warn_if_console_only_alerts(self) -> None:
        externals = [n for n in self.hub.notifiers if not isinstance(n, ConsoleNotifier)]
        if not externals:
            log.warning(
                "no external notifier configured — critical alerts (kill switch, "
                "reconciliation, stop failures) will only reach the console/log. "
                "Enable telegram or email in config before unattended operation."
            )
            HEALTH.note("notifications", "console-only: enable telegram/email for alerts")

    # ==================================================================
    def run(self, max_cycles: int | None = None) -> None:
        self._shutdown_done = False
        self._install_signals()
        self.startup_checks()
        if not self.lock.acquire():
            raise RuntimeError("another engine instance is running; refusing to start")
        self.audit.append(
            "engine.start",
            {
                "mode": self.cfg.mode.value,
                "config_hash": self.cfg_hash,
                "strategy": f"{self.strategy.name}@{self.strategy.version}",
            },
        )
        from datetime import UTC as _UTC

        self._session_wall_start = datetime.now(_UTC)
        self._session_decisions_start = self.repos.decisions.count(self.cfg.mode)
        HEALTH.update(application=ComponentHealth.OK, database=ComponentHealth.OK)

        try:
            if self.cfg.monitoring.enabled and self.fixture is None:
                self.monitor = MonitoringServer(
                    self.cfg.monitoring.host,
                    self.cfg.monitoring.port,
                    token=self.secrets.get("MONITORING_TOKEN"),
                )
                try:
                    self.monitor.start()
                except OSError as exc:
                    log.warning("monitoring server not started: %s", exc)
                    self.monitor = None

            initial_reconcile_ok = self._reconcile()
            if not initial_reconcile_ok and self.cfg.mode in (Mode.TESTNET, Mode.LIVE):
                raise RuntimeError(
                    "startup reconciliation failed; trading mode refused fail-closed"
                )

            # cycle cadence = stop-monitor cadence: the protective exit check
            # runs every cycle, so this bounds worst-case stop latency
            sleep_s = max(5, min(self.cfg.execution.stop_monitor_interval_s, 60))
            cycles = 0
            while not self._stop.is_set():
                if max_cycles is not None and cycles >= max_cycles:
                    break
                cycles += 1
                try:
                    self.cycle()
                except ExchangeUnavailable as exc:
                    log.warning("cycle skipped, exchange unavailable: %s", exc)
                    HEALTH.update(exchange=ComponentHealth.DEGRADED)
                except Exception as exc:
                    log.exception("cycle failed")
                    self._fail_runtime_closed(exc)
                if self.fixture is not None:
                    if not self.fixture.advance():
                        log.info("fixture exhausted after %s cycles", cycles)
                        break
                else:
                    self._stop.wait(sleep_s)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self._record_session_evidence()
        except Exception:
            log.exception("failed to record qualification evidence")
        try:
            self.audit.append("engine.stop", {"mode": self.cfg.mode.value})
        except Exception:
            log.exception("failed to append engine stop audit event")
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        self.lock.release()
        HEALTH.update(application=ComponentHealth.DEGRADED, trading_permitted=False)
        if self.close_db_on_shutdown:
            self.db.close()
        log.info("engine stopped cleanly")

    # ==================================================================
    def cycle(self) -> None:
        cid = new_correlation_id()
        METRICS.inc("bot_cycles_total")
        HEALTH.heartbeat()

        kill_active, kill_reason = self.kill_switch.check()
        HEALTH.update(kill_switch_active=kill_active)

        candles, cval = self.market_data.closed_candles(limit=self.strategy.warmup + 60)
        signal_candles = candles
        if self.fixture is not None:
            if len(candles) > 1:
                signal_candles = candles[:-1]
                cval = validate_candles(
                    signal_candles,
                    symbol=self.cfg.symbol,
                    interval_seconds=self.cfg.interval_seconds,
                    now=self.clock.now(),
                    max_gap_pct=self.cfg.risk.max_gap_pct,
                    grace_seconds=self.cfg.risk.candle_grace_s,
                    min_candles=self.strategy.warmup,
                    max_future_seconds=self.cfg.risk.max_clock_skew_s,
                )
            else:
                signal_candles = []
        quote, qval = self.market_data.quote()
        # Second-source sanity: the book ticker must agree with the candle
        # feed within the gap tolerance, or entries pause this cycle.
        if quote is not None and qval.ok and signal_candles:
            cross = cross_check_quote(quote, signal_candles[-1].close, self.cfg.risk.max_gap_pct)
            if not cross.ok:
                log.warning("quote cross-check failed: %s", "; ".join(cross.issues))
                qval = cross
        HEALTH.update(
            market_data=ComponentHealth.OK
            if (cval.ok and qval.ok and self.market_data.required_feeds_healthy())
            else ComponentHealth.DEGRADED
        )

        try:
            balances = self.adapter.get_balances()
            HEALTH.update(exchange=ComponentHealth.OK)
        except ExchangeUnavailable:
            HEALTH.update(exchange=ComponentHealth.FAILED)
            raise

        mark_price = None
        if quote is not None and qval.ok:
            mark_price = quote.mid
        elif signal_candles:
            mark_price = signal_candles[-1].close
        if mark_price is None:
            log.warning("no usable price this cycle; skipping")
            return

        equity = self.portfolio.snapshot(balances, mark_price)
        METRICS.set_gauge("bot_equity_quote", float(equity))
        state = self.risk_state.snapshot(equity)
        METRICS.set_gauge("bot_drawdown_pct", float(state.drawdown_pct))
        METRICS.set_gauge("bot_consecutive_losses", state.consecutive_losses)

        self._integrity_task.maybe_run()
        circuit = self.circuit.evaluate(state, self.market_data.consecutive_failures, self._db_ok)

        health_ok = (
            cval.ok
            and qval.ok
            and not circuit.open
            and self._db_ok
            and not state.reconciliation_blocked
            and state.unknown_orders == 0
            and state.active_entry_orders == 0
            and self.market_data.required_feeds_healthy()
        )
        entries_allowed = self.approval.entries_allowed(health_ok) and not kill_active
        HEALTH.update(trading_permitted=entries_allowed and not circuit.open)

        position = self.portfolio.open_position()
        base_free = balances.get(self.rules.base_asset)
        quote_free = balances.get(self.rules.quote_asset)
        base_free_qty = base_free.free if base_free else ZERO
        quote_free_qty = quote_free.free if quote_free else ZERO

        # ---- exit management (every cycle, kill switch included) --------
        if position is not None and self._native_stops:
            # first, learn whether the resting native stop already acted
            position = self._sync_protective(position)
        if position is not None:
            exit_reason = ""
            if kill_active and not self._kill_policy_done:
                if self.cfg.emergency_position_policy == EmergencyPositionPolicy.CLOSE_AT_MARKET:
                    exit_reason = "kill_switch_close_policy"
                self._kill_policy_done = True
                self._notify_kill(kill_reason, position)
            if not exit_reason:
                exit_reason = check_protective_exit(position, quote)
            if (
                exit_reason == ExitCheck.STOP_BREACH
                and self._native_stops
                and position.protective_order_id
            ):
                # the exchange-native stop should be filling; escalate to a
                # market exit only on gap-through or exhausted patience
                if not self._should_escalate_stop(position, quote):
                    exit_reason = ""
            if exit_reason:
                self._breach_cycles = 0
                self._execute_exit(
                    position,
                    exit_reason,
                    quote,
                    qval,
                    cval,
                    state,
                    equity,
                    base_free_qty,
                    quote_free_qty,
                    kill_active,
                    circuit,
                    cid,
                )
                position = self.portfolio.open_position()
            elif check_protective_exit(position, quote) == ExitCheck.NONE:
                self._breach_cycles = 0
        else:
            self._breach_cycles = 0
        if not kill_active:
            self._kill_policy_done = False

        # ---- strategy pipeline: once per NEW closed candle ---------------
        if signal_candles and cval.ok:
            last = signal_candles[-1]
            already = self.repos.decisions.last_processed_candle(self.cfg.mode, self.strategy.name)
            if (self._last_candle_ts is None or last.open_time > self._last_candle_ts) and (
                already is None or last.open_time > already
            ):
                self._process_candle(
                    signal_candles,
                    quote,
                    qval,
                    cval,
                    state,
                    equity,
                    position,
                    base_free_qty,
                    quote_free_qty,
                    kill_active,
                    kill_reason,
                    circuit,
                    entries_allowed,
                    cid,
                )
                self._last_candle_ts = last.open_time

        # ---- protective stop upkeep (works under kill switch too) ---------
        if self._native_stops:
            current = self.portfolio.open_position()
            if current is not None:
                self._ensure_protective(current, cid)

        # ---- housekeeping -------------------------------------------------
        if self.cfg.execution.limit_timeout_s > 0:
            self.gateway.cancel_stale_entry_orders(self.cfg.execution.limit_timeout_s)
        try:
            self._maybe_sweep_dust(quote if qval.ok else None, cid)
        except Exception:
            log.exception("dust sweep failed; will retry next cycle")
        self._maybe_daily_report(kill_active, kill_reason, entries_allowed)
        self._reconcile_task.maybe_run()
        self.lock.heartbeat()

    def _record_session_evidence(self) -> None:
        """Signed live-qualification evidence: only REAL paper sessions on
        live market data count (fixtures and backtests record nothing)."""
        if self.cfg.mode != Mode.PAPER or self.fixture is not None:
            return
        if self.cfg.data.source != "exchange":
            return
        start = getattr(self, "_session_wall_start", None)
        if start is None:
            return
        from datetime import UTC as _UTC

        from trading_bot.security.qualification import (
            QualificationEvidenceStore,
            get_or_create_evidence_key,
        )
        from trading_bot.security.quality import git_info

        commit, _dirty, git_state = git_info(self.root)
        decisions_now = self.repos.decisions.count(self.cfg.mode)
        store = QualificationEvidenceStore(
            self.root, key=get_or_create_evidence_key(self.repos.flags)
        )
        store.append(
            {
                "source_mode": "paper",
                "data_source_class": "live_market",
                "wall_clock_start": start.isoformat(),
                "wall_clock_end": datetime.now(_UTC).isoformat(),
                "eligible_decisions": decisions_now
                - getattr(self, "_session_decisions_start", decisions_now),
                "configuration_hash": self.cfg_hash,
                "strategy_version": f"{self.strategy.name}@{self.strategy.version}",
                "git_commit": commit,
                "git_state": git_state,
                "symbol": self.cfg.symbol,
            }
        )
        log.info("qualification evidence recorded for this paper session")

    def _maybe_sweep_dust(self, quote, cid: str) -> None:
        """Aggregated exchange dust becomes sellable once it clears the
        exchange minimums; sweep it so value is not silently parked."""
        if quote is None:
            return
        if self.portfolio.open_position() is not None:
            return  # never intermingle a sweep with an open position
        dust_rows = self.repos.positions.dust_positions(self.cfg.mode)
        if not dust_rows:
            return
        total = sum((dec(r["qty"]) for r in dust_rows), ZERO)
        if total <= ZERO or total * quote.bid < self.rules.min_notional * dec("1.02"):
            return
        try:
            balances = self.adapter.get_balances()
        except ExchangeUnavailable:
            return
        bal = balances.get(self.rules.base_asset)
        base_free = bal.free if bal else ZERO
        decision = self.risk_engine.evaluate_dust_sweep(total, quote, self.rules, base_free)
        if not decision.approved or decision.order is None:
            return
        execution = self.gateway.submit(decision.order, decision.approval_token, cid, "exit")
        if execution.response is None:
            return
        final = self.gateway.await_completion(execution.response)
        if final.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            proceeds = self.portfolio.record_dust_sweep(dust_rows, final)
            METRICS.inc("bot_dust_sweeps_total")
            log.info("dust sweep recovered %s %s", proceeds, self.rules.quote_asset)

    # ==================================================================
    def _gate_context(
        self,
        quote,
        qval,
        cval,
        state,
        equity,
        position,
        base_free,
        quote_free,
        kill_active,
        kill_reason,
        circuit: CircuitStatus,
        approval_ok: bool,
        duplicate: bool = False,
    ) -> GateContext:
        return GateContext(
            now=self.clock.now(),
            equity=equity,
            quote_free=quote_free,
            base_free=base_free,
            quote=quote,
            quote_validation=qval,
            candle_validation=cval,
            rules=self.rules,
            state=state,
            open_position=position,
            kill_switch_active=kill_active,
            kill_switch_reason=kill_reason,
            circuit_breaker_open=circuit.open,
            approval_ok=approval_ok,
            continuation_mode=self.cfg.continuation.mode,
            duplicate_signal=duplicate,
        )

    def _process_candle(
        self,
        candles: list[Candle],
        quote,
        qval,
        cval,
        state,
        equity,
        position,
        base_free,
        quote_free,
        kill_active,
        kill_reason,
        circuit,
        entries_allowed,
        cid,
    ) -> None:
        signal = self.strategy.evaluate(candles, has_position=position is not None)
        METRICS.inc(f"bot_signals_{signal.action.value.lower()}_total")

        risk_decision: RiskDecision | None = None
        execution: ExecutionResult | None = None

        if signal.action == SignalAction.ENTER_LONG:
            ctx = self._gate_context(
                quote,
                qval,
                cval,
                state,
                equity,
                position,
                base_free,
                quote_free,
                kill_active,
                kill_reason,
                circuit,
                entries_allowed,
            )
            risk_decision = self.risk_engine.evaluate_entry(ctx)
            self._alert_new_risk_codes(risk_decision, state)
            if risk_decision.approved and risk_decision.order is not None:
                self.audit.append(
                    "pipeline.preview",
                    {"order": risk_decision.order.as_dict(), "correlation_id": cid},
                )
                execution = self.gateway.submit(
                    risk_decision.order, risk_decision.approval_token, cid, "entry"
                )
                if execution.response is not None:
                    final = self.gateway.await_completion(execution.response)
                    if final.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
                        self.portfolio.record_entry(final, risk_decision.order.stop_price)
                        METRICS.inc("bot_entries_total")
        elif signal.action == SignalAction.EXIT_LONG and position is not None:
            self._execute_exit(
                position,
                ExitCheck.STRATEGY_EXIT,
                quote,
                qval,
                cval,
                state,
                equity,
                base_free,
                quote_free,
                kill_active,
                circuit,
                cid,
                signal=signal,
            )
            risk_decision = None  # recorded inside _execute_exit

        record = DecisionRecord(
            decision_id=uuid.uuid4().hex,
            correlation_id=cid,
            ts=self.clock.now(),
            mode=self.cfg.mode,
            strategy=self.strategy.name,
            strategy_version=self.strategy.version,
            config_hash=self.cfg_hash,
            symbol=self.cfg.symbol,
            market_data_ts=candles[-1].close_time if candles else None,
            signal=signal,
            risk=risk_decision,
            execution=execution,
            explanation="",
        )
        explanation = self.analyst.explain_decision(
            {
                "signal_action": signal.action.value,
                "signal_reason": signal.reason,
                "risk_approved": 1 if (risk_decision and risk_decision.approved) else 0,
                "risk_codes_json": None
                if risk_decision is None
                else json.dumps([c.value for c in risk_decision.codes]),
            }
        )
        record = dataclasses.replace(record, explanation=explanation)
        self.repos.decisions.insert(record)
        METRICS.inc("bot_decisions_total")

    # ============================================== native protective stops
    def _sync_protective(self, position: PositionState) -> PositionState | None:
        """Learn the resting native stop's fate. Returns the refreshed
        position (None when the stop closed it)."""
        coid = position.protective_order_id
        if not coid:
            return position
        row = self.repos.orders.get_by_client_id(coid)
        try:
            resp = self.adapter.query_order(self.cfg.symbol, coid)
        except ExchangeUnavailable:
            return position
        if resp is None:
            log.warning("native stop %s vanished from the exchange; will re-place", coid)
            self.repos.positions.set_protective_order(position.position_id, None)
            return self.portfolio.open_position()
        if row is not None and row["state"] != resp.state.value:
            self.repos.orders.update_state(coid, resp.state, resp)
            if resp.fills and resp.state == OrderState.FILLED:
                self.repos.orders.add_fills(row["id"], coid, resp.fills)
        if resp.state == OrderState.FILLED and resp.executed_qty > ZERO:
            log.warning(
                "exchange-native stop FILLED: %s sold %s @ ~%s",
                coid,
                resp.executed_qty,
                resp.avg_fill_price,
            )
            self.repos.positions.set_protective_order(position.position_id, None)
            realized = self.portfolio.record_exit(position, resp, "stop_breach_native")
            METRICS.inc("bot_exits_total")
            METRICS.inc("bot_native_stop_fills_total")
            self._post_exit_bookkeeping(realized)
            return self.portfolio.open_position()
        if resp.state in (OrderState.CANCELLED, OrderState.REJECTED):
            self.repos.positions.set_protective_order(position.position_id, None)
            if resp.executed_qty > ZERO:
                realized = self.portfolio.record_exit(position, resp, "stop_breach_native")
                self._post_exit_bookkeeping(realized)
            return self.portfolio.open_position()
        # PARTIALLY_FILLED stops finalize via escalation/cancel (cumulative
        # fills are only booked once, from a terminal response).
        return position

    def _should_escalate_stop(self, position: PositionState, quote) -> bool:
        """Stop is breached and a native stop is resting. Escalate to a
        market exit when the market gapped through the stop's limit price or
        the stop stayed unfilled too long. Cancels the native stop first."""
        self._breach_cycles += 1
        row = self.repos.orders.get_by_client_id(position.protective_order_id or "")
        limit_price = position.stop_price
        if row is not None and row.get("limit_price"):
            limit_price = dec(row["limit_price"])
        gap_through = quote is not None and quote.bid < limit_price
        if (
            not gap_through
            and self._breach_cycles < self.cfg.execution.protective_escalation_cycles
        ):
            log.warning(
                "stop breached; native stop should fill (cycle %s/%s)",
                self._breach_cycles,
                self.cfg.execution.protective_escalation_cycles,
            )
            return False
        log.warning(
            "escalating to market exit (%s)",
            "gap through stop limit" if gap_through else "native stop unfilled too long",
        )
        if self._cancel_protective(position):
            return False  # the cancel revealed a fill; position already booked
        return True

    def _cancel_protective(self, position: PositionState) -> bool:
        """Cancel the resting native stop before any other exit path sells.
        Returns True when cancelling revealed the stop had already filled
        (the exit is then recorded and no further sell must happen)."""
        coid = position.protective_order_id
        if not coid:
            return False
        row = self.repos.orders.get_by_client_id(coid)
        resp: OrderResponse | None
        try:
            resp = self.adapter.cancel_order(self.cfg.symbol, coid)
        except (OrderRejectedError, OrderNotFoundError):
            try:
                resp = self.adapter.query_order(self.cfg.symbol, coid)
            except ExchangeUnavailable:
                return False
        except ExchangeUnavailable:
            return False
        self.repos.positions.set_protective_order(position.position_id, None)
        if resp is None:
            return False
        if row is not None and row["state"] != resp.state.value:
            self.repos.orders.update_state(coid, resp.state, resp)
            if resp.fills:
                self.repos.orders.add_fills(row["id"], coid, resp.fills)
        if resp.executed_qty > ZERO:
            realized = self.portfolio.record_exit(position, resp, "stop_breach_native")
            METRICS.inc("bot_exits_total")
            METRICS.inc("bot_native_stop_fills_total")
            self._post_exit_bookkeeping(realized)
            return True
        return False

    def _ensure_protective(self, position: PositionState, cid: str) -> None:
        """Place (or refresh) the exchange-native stop for the open position."""
        try:
            balances = self.adapter.get_balances()
        except ExchangeUnavailable:
            return
        bal = balances.get(self.rules.base_asset)
        base_free = bal.free if bal else ZERO

        if position.protective_order_id:
            row = self.repos.orders.get_by_client_id(position.protective_order_id)
            if row is None:
                self.repos.positions.set_protective_order(position.position_id, None)
            elif row["state"] in (
                OrderState.ACKNOWLEDGED.value,
                OrderState.SUBMITTED.value,
            ):
                row_qty = dec(row["qty"])
                row_stop = dec(row["stop_price"]) if row.get("stop_price") else None
                desired = size_exit_qty(
                    position.qty,
                    base_free + row_qty,
                    self.rules,
                    OrderType.STOP_LOSS_LIMIT,
                )
                drifted = (
                    row_stop != position.stop_price
                    or desired is None
                    or abs(desired - row_qty) >= self.rules.step_size
                )
                if not drifted:
                    return
                log.info("protective stop drifted (qty/stop changed); replacing")
                if self._cancel_protective(position):
                    return
                refreshed = self.portfolio.open_position()
                if refreshed is None or refreshed.position_id != position.position_id:
                    return
                position = refreshed
                try:
                    balances = self.adapter.get_balances()
                except ExchangeUnavailable:
                    return
                bal = balances.get(self.rules.base_asset)
                base_free = bal.free if bal else ZERO
            else:
                return  # terminal states are handled by _sync_protective

        decision = self.risk_engine.evaluate_protective_stop(position, self.rules, base_free)
        if not decision.approved or decision.order is None:
            log.debug(
                "native stop not placeable for %s: %s",
                position.position_id,
                decision.primary_code.value,
            )
            return
        execution = self.gateway.submit(decision.order, decision.approval_token, cid, "protective")
        if execution.response is not None and execution.response.state in (
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
        ):
            self.repos.positions.set_protective_order(
                position.position_id, decision.order.client_order_id
            )
            METRICS.inc("bot_native_stops_placed_total")
            log.info(
                "native stop resting: trigger %s / limit %s for %s %s",
                decision.order.stop_price,
                decision.order.limit_price,
                decision.order.qty,
                self.cfg.symbol,
            )
        elif execution.response is not None and execution.response.state == OrderState.REJECTED:
            log.warning(
                "native stop rejected (%s); software monitor remains the backstop",
                execution.error or execution.response.raw_status,
            )

    def _post_exit_bookkeeping(self, realized) -> None:
        if realized < ZERO:
            METRICS.inc("bot_losing_trades_total")
            consecutive = self.repos.positions.consecutive_losses(self.cfg.mode)
            if consecutive >= self.cfg.risk.pause_after_consecutive_losses:
                msg = (
                    f"{consecutive} consecutive losses — entries paused by risk engine "
                    f"(cooldown {self.cfg.risk.cooldown_after_loss_hours}h)"
                )
                delivered = self.hub.send("Consecutive-loss pause", msg, "critical")
                self.repos.events.alert("critical", "consecutive_loss_pause", msg, delivered)

    # ==================================================================
    def _execute_exit(
        self,
        position: PositionState,
        reason: str,
        quote,
        qval,
        cval,
        state,
        equity,
        base_free,
        quote_free,
        kill_active,
        circuit,
        cid,
        signal: SignalDecision | None = None,
    ) -> None:
        # The native stop must come off the book BEFORE any other sell: it
        # holds the base balance locked, and cancelling may reveal it already
        # filled (in which case this exit is already done).
        if self._native_stops and position.protective_order_id:
            if self._cancel_protective(position):
                return
            refreshed = self.portfolio.open_position()
            if refreshed is None:
                return
            position = refreshed
            try:
                balances = self.adapter.get_balances()
            except ExchangeUnavailable:
                log.warning("cannot refresh balances after stop cancel; retrying next cycle")
                return
            base_bal = balances.get(self.rules.base_asset)
            quote_bal = balances.get(self.rules.quote_asset)
            base_free = base_bal.free if base_bal else ZERO
            quote_free = quote_bal.free if quote_bal else ZERO

        ctx = self._gate_context(
            quote,
            qval,
            cval,
            state,
            equity,
            position,
            base_free,
            quote_free,
            kill_active,
            "",
            circuit,
            approval_ok=True,
        )
        decision = self.risk_engine.evaluate_exit(ctx, reason)
        if not decision.approved or decision.order is None:
            code = decision.primary_code.value
            msg = f"exit blocked ({reason}): {code}"
            log.error(msg)
            delivered = self.hub.send("Exit blocked", msg, "critical")
            self.repos.events.alert("critical", "exit_blocked", msg, delivered)
            return
        execution = self.gateway.submit(decision.order, decision.approval_token, cid, "exit")
        if execution.response is None:
            msg = f"exit order failed ({reason}): {execution.error}"
            delivered = self.hub.send("Exit order failed", msg, "critical")
            self.repos.events.alert("critical", "exit_failed", msg, delivered)
            return
        final = self.gateway.await_completion(execution.response)
        if final.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            realized = self.portfolio.record_exit(position, final, reason)
            METRICS.inc("bot_exits_total")
            self._post_exit_bookkeeping(realized)

    # ==================================================================
    def _alert_new_risk_codes(self, decision: RiskDecision, state) -> None:
        """Alert once per day per hard-limit code."""
        interesting = {
            ReasonCode.DAILY_LOSS_LIMIT,
            ReasonCode.WEEKLY_LOSS_LIMIT,
            ReasonCode.MAX_DRAWDOWN,
            ReasonCode.CONSECUTIVE_LOSS_PAUSE,
            ReasonCode.RECONCILIATION_MISMATCH,
            ReasonCode.UNKNOWN_ORDER_PENDING,
            ReasonCode.MIN_NOTIONAL_EXCEEDS_RISK,
        }
        day = state.day
        if self._alerted_codes_day[0] != day:
            self._alerted_codes_day = (day, set())
        for code in decision.codes:
            if code in interesting and code.value not in self._alerted_codes_day[1]:
                self._alerted_codes_day[1].add(code.value)
                msg = f"risk limit event: {code.value}"
                severity = "critical" if code != ReasonCode.MIN_NOTIONAL_EXCEEDS_RISK else "info"
                delivered = self.hub.send(msg, f"entries blocked by {code.value}", severity)
                self.repos.events.alert(severity, code.value, msg, delivered)

    def _notify_kill(self, reason: str, position: PositionState | None) -> None:
        policy = self.cfg.emergency_position_policy.value
        body = (
            f"Kill switch active: {reason}\n"
            f"Open position: {position.as_dict() if position else 'none'}\n"
            f"Emergency position policy: {policy}\n"
            "No new entries will be submitted. Manual reset required (trading-bot resume)."
        )
        delivered = self.hub.send("KILL SWITCH ACTIVE", body, "critical")
        self.repos.events.alert("critical", "kill_switch", body, delivered)

    # ==================================================================
    def _maybe_daily_report(self, kill_active: bool, kill_reason: str, permitted: bool) -> None:
        tz = ZoneInfo(self.cfg.timezone)
        local_now = self.clock.now().astimezone(tz)
        hh, mm = self.cfg.reporting.daily_time_local.split(":")
        due = local_now.hour > int(hh) or (
            local_now.hour == int(hh) and local_now.minute >= int(mm)
        )
        if not due:
            return
        day = local_now.strftime("%Y-%m-%d")
        if self.repos.reports.get(day, "daily", self.cfg.mode) is not None:
            return
        self.reports.generate_and_store(day, HEALTH.snapshot(), kill_active, kill_reason, permitted)
        self.approval.consume_after_daily_report()

    # ==================================================================
    def _reconcile(self) -> bool:
        try:
            result = self.reconciler.run()
        except Exception as exc:
            log.exception("reconciliation crashed")
            result = self.reconciler.fail_closed(exc)
            HEALTH.update(
                exchange=ComponentHealth.DEGRADED,
                risk_engine=ComponentHealth.FAILED,
                trading_permitted=False,
            )
            delivered = self.hub.send(
                "Reconciliation failed closed",
                f"{type(exc).__name__}: {str(exc)[:200]}",
                "critical",
            )
            self.repos.events.alert(
                "critical",
                "reconciliation_exception",
                f"{type(exc).__name__}: {str(exc)[:300]}",
                delivered,
            )
            return False
        HEALTH.update(
            last_reconciliation_at=self.clock.now().isoformat(),
        )
        if not result.ok:
            HEALTH.update(trading_permitted=False, risk_engine=ComponentHealth.DEGRADED)
            delivered = self.hub.send(
                "Reconciliation mismatch",
                str(result.details.get("problems")),
                "critical",
            )
            self.repos.events.alert(
                "critical",
                "reconciliation_mismatch",
                str(result.details.get("problems")),
                delivered,
            )
            return False
        HEALTH.update(risk_engine=ComponentHealth.OK)
        return True

    def _check_db(self) -> None:
        try:
            self._db_ok = self.db.integrity_check()
        except Exception as exc:
            self._fail_runtime_closed(exc)
            return
        HEALTH.update(database=ComponentHealth.OK if self._db_ok else ComponentHealth.FAILED)
        if not self._db_ok:
            self._fail_runtime_closed(RuntimeError("database integrity check failed"))

    def _fail_runtime_closed(self, exc: BaseException) -> None:
        """Persist uncertainty and block entries after any unexpected cycle failure.

        The exception may have happened after the exchange accepted an order
        but before every local accounting write completed.  Continuing to
        enter on an assumed state would risk a duplicate position.
        """
        self._db_ok = False
        METRICS.inc("bot_cycle_errors_total")
        HEALTH.update(
            application=ComponentHealth.DEGRADED,
            database=ComponentHealth.FAILED,
            risk_engine=ComponentHealth.FAILED,
            trading_permitted=False,
        )
        try:
            self.repos.flags.set(self.repos.flags.RECONCILIATION_BLOCK, "true")
        except Exception:
            log.exception("could not persist reconciliation block after runtime failure")
        try:
            delivered = self.hub.send(
                "Runtime uncertainty — entries blocked",
                f"{type(exc).__name__}: {str(exc)[:200]}",
                "critical",
            )
            self.repos.events.alert(
                "critical",
                "runtime_uncertainty",
                f"{type(exc).__name__}: {str(exc)[:300]}",
                delivered,
            )
        except Exception:
            log.exception("could not persist runtime-uncertainty alert")
