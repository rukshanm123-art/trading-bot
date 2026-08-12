"""Hard-coded safety bounds and endpoint constants.

These are NOT configuration. Config may only tighten risk relative to these
caps; the config loader rejects any file that tries to exceed them. Editing
this module is a code change that must go through review + tests (see
docs/RISK_MODEL.md).
"""

from __future__ import annotations

from decimal import Decimal

# --------------------------------------------------------------------------
# Absolute risk caps — config values may be LOWER, never higher.
# --------------------------------------------------------------------------
HARD_MAX_OPEN_POSITIONS = 1
HARD_MAX_POSITION_ALLOCATION_PCT = Decimal("25")  # of total equity
HARD_MIN_CASH_RESERVE_PCT = Decimal("30")  # config default is 50
HARD_MAX_RISK_PER_TRADE_PCT = Decimal("1.0")  # config default is 0.5
HARD_MAX_DAILY_LOSS_PCT = Decimal("3")  # config default is 2
HARD_MAX_7D_LOSS_PCT = Decimal("6")  # config default is 5
HARD_MAX_DRAWDOWN_PCT = Decimal("10")  # config default is 8
HARD_MAX_ENTRIES_PER_DAY = 4  # config default is 2
HARD_MIN_COOLDOWN_AFTER_LOSS_HOURS = 1
HARD_MAX_SLIPPAGE_BPS = Decimal("100")
HARD_MAX_SPREAD_BPS = Decimal("100")
HARD_MAX_PROTECTIVE_EXIT_BUFFER_BPS = Decimal("500")
HARD_MAX_CLOCK_SKEW_S = 120
# Native STOP_LOSS_LIMIT: max distance between the trigger and its limit price.
HARD_MAX_PROTECTIVE_LIMIT_OFFSET_BPS = Decimal("500")

# --------------------------------------------------------------------------
# Exchange rate limiting (Binance spot: 6000 request weight per minute).
# The soft threshold pauses OUR requests before the exchange starts
# rejecting them; the hard threshold backs off firmly.
# --------------------------------------------------------------------------
RATE_LIMIT_WEIGHT_PER_MINUTE = 6000
RATE_LIMIT_SOFT_RATIO = 0.75
RATE_LIMIT_HARD_RATIO = 0.90
RATE_LIMIT_SOFT_DELAY_S = 2.0
RATE_LIMIT_HARD_DELAY_S = 10.0

# Only spot. There is deliberately no representation of leverage, margin,
# futures, borrowing or shorting anywhere in the type system.
SUPPORTED_ORDER_TYPES = ("MARKET", "LIMIT")

# --------------------------------------------------------------------------
# Exchange endpoints. The adapter refuses any (mode, base_url) mismatch.
# --------------------------------------------------------------------------
BINANCE_LIVE_BASE_URL = "https://api.binance.com"
BINANCE_TESTNET_BASE_URL = "https://testnet.binance.vision"

# Env var names are mode-specific by design: a testnet key cannot leak into a
# live session because live mode never reads the testnet variables.
ENV_TESTNET_KEY = "BINANCE_TESTNET_API_KEY"
ENV_TESTNET_SECRET = "BINANCE_TESTNET_API_SECRET"  # noqa: S105 # nosec B105 - env var NAME
ENV_LIVE_KEY = "BINANCE_LIVE_API_KEY"
ENV_LIVE_SECRET = "BINANCE_LIVE_API_SECRET"  # noqa: S105 # nosec B105 - env var NAME
ENV_LIVE_ENABLED = "LIVE_TRADING_ENABLED"
ENV_KILL_SWITCH = "TRADING_KILL_SWITCH"

# --------------------------------------------------------------------------
# Live-mode unlock prerequisites (docs/LIVE_TRADING_CHECKLIST.md)
# --------------------------------------------------------------------------
LIVE_MIN_PAPER_DAYS = 30
LIVE_MIN_PAPER_DECISIONS = 300
LIVE_UNLOCK_VALID_HOURS = 24
LIVE_CONFIRMATION_WORDS = 6  # random words the operator must retype

# --------------------------------------------------------------------------
# Files & quality gate. The quality record must prove a REAL test run:
# a "passed" flag alone is worthless (a suite of zero tests would pass).
# scripts/record_test_run.py produces the record; security/livegate.py
# enforces every requirement below.
# --------------------------------------------------------------------------
STOP_FILE_NAME = "STOP_TRADING"
QUALITY_GATE_FILE = "var/quality/latest_test_run.json"
QUALITY_GATE_MAX_AGE_HOURS = 72
QUALITY_MIN_TESTS = 100
QUALITY_MIN_COVERAGE_PCT = 90.0
REQUIRED_SAFETY_TESTS = (
    "tests/unit/test_sizing.py::test_min_notional_rejected_never_rounded_up",
    "tests/unit/test_sizing.py::test_trapped_exit_below_minimum_notional_rejected",
    "tests/unit/test_sizing.py::test_previous_trapped_position_fixture_cannot_open",
    "tests/unit/test_risk_engine.py::test_daily_loss_limit_blocks_entry",
    "tests/unit/test_risk_engine.py::test_max_drawdown_blocks_entry",
    "tests/unit/test_risk_engine.py::test_rejected_risk_cannot_reach_gateway",
    "tests/unit/test_risk_engine.py::test_active_entry_order_blocks_duplicate_entry",
    "tests/unit/test_killswitch_circuit.py::test_file_kill_switch_blocks",
    "tests/unit/test_paper_exchange.py::test_partial_fill_completes",
    "tests/unit/test_partial_fill_accounting.py::test_partial_entry_across_multiple_fills_updates_position",
    "tests/unit/test_partial_fill_accounting.py::test_partial_exit_leaves_residual_exposure_and_realizes_only_filled_qty",
    "tests/unit/test_partial_fill_accounting.py::test_entry_accounting_rolls_back_as_one_unit",
    "tests/unit/test_partial_fill_accounting.py::test_exit_accounting_rolls_back_as_one_unit",
    "tests/unit/test_review_fixes.py::test_documented_unknown_execution_codes_never_become_rejections",
    "tests/unit/test_review_fixes.py::test_market_lot_size_parsed_and_enforced",
    "tests/unit/test_review_fixes.py::test_binance_transport_headers_enforce_retry_after",
    "tests/unit/test_consecutive_loss_pause.py::test_pause_is_latched_and_time_does_not_clear_it",
    "tests/unit/test_consecutive_loss_pause.py::test_acknowledgement_succeeds_with_dust_and_is_idempotent",
    "tests/unit/test_consecutive_loss_pause.py::test_audit_failure_rolls_back_acknowledgement",
    "tests/unit/test_consecutive_loss_pause.py::test_acknowledgement_survives_database_backup_and_restore",
    "tests/integration/test_cli_commands.py::test_loss_pause_requires_dedicated_ack_and_status_is_explicit",
    "tests/unit/test_redaction.py::test_secrets_never_appear_in_logs",
    "tests/security/test_endpoint_separation.py::test_testnet_adapter_refuses_live_url",
    "tests/security/test_endpoint_separation.py::test_testnet_engine_never_constructs_live_public_data_client",
    "tests/security/test_live_gate.py::test_live_locked_by_default",
    "tests/security/test_live_gate.py::test_live_gate_requires_configured_out_of_band_alerting",
    "tests/security/test_live_gate.py::test_live_gate_requires_postgres_database",
    "tests/security/test_live_gate.py::test_live_gate_rejects_failed_external_connectivity",
    "tests/security/test_live_gate.py::test_backtest_and_fixture_evidence_count_zero_paper_days",
    "tests/security/test_live_gate.py::test_sqlite_qualification_evidence_is_rejected",
    "tests/security/test_live_gate.py::test_tampered_qualification_evidence_is_rejected",
    "tests/security/test_quality_evidence.py::test_quality_evidence_rejects_tampered_coverage_artifact",
    "tests/integration/test_restart_reconciliation.py::test_stale_intent_abandoned_after_restart",
    "tests/integration/test_reconciliation_fail_closed.py::test_reconciliation_exception_fail_closed",
    "tests/integration/test_engine_paths.py::test_unexpected_runtime_failure_blocks_future_entries",
    "tests/integration/test_engine_paths.py::test_live_startup_independently_refuses_non_postgres_database",
    "tests/integration/test_postgres_backend.py::test_postgres_fill_accounting_rolls_back_and_retries_atomically",
    "tests/integration/test_postgres_backend.py::test_postgres_qualification_evidence_records_backend_provenance",
    "tests/unit/test_config.py::test_live_configuration_requires_postgres_and_external_alerts",
    "tests/unit/test_small_gaps.py::test_quality_runner_converts_timeout_to_failure",
    "tests/integration/test_backtest.py::test_next_candle_backtest_execution_uses_subsequent_open",
    "tests/unit/test_binance_adapter_errors.py::test_binance_2013_query_order_returns_none",
    "tests/unit/test_market_data_service_health.py::test_candle_failures_do_not_clear_on_quote_success",
    "tests/unit/test_market_data_validation.py::test_close_above_high_fails",
    "tests/unit/test_market_data_validation.py::test_future_candle_fails",
    "tests/unit/test_daily_report_timezone.py::test_auckland_midnight_report_includes_each_event_once",
    "tests/unit/test_database_shutdown.py::test_engine_shutdown_closes_database_and_is_idempotent",
)

# --------------------------------------------------------------------------
# Operational defaults
# --------------------------------------------------------------------------
DEFAULT_RECV_WINDOW_MS = 5000
MAX_CLOCK_DRIFT_MS = 1000
HTTP_TIMEOUT_S = 10
MAX_RETRIES = 4
RETRY_BACKOFF_BASE_S = 0.5
RETRY_BACKOFF_MAX_S = 8.0
