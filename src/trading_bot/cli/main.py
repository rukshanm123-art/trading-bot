"""trading-bot CLI.

Sensitive commands (approve / resume / live unlock) are local-machine only by
design: they act through the local database file/socket and an interactive
TTY. No network control surface exists.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from trading_bot import __version__
from trading_bot.config import constants as C
from trading_bot.config.loader import ConfigError, load_config
from trading_bot.control.approval import ApprovalService
from trading_bot.control.killswitch import KillSwitch
from trading_bot.core.enums import KillSwitchSource, Mode
from trading_bot.core.types import ZERO, dec
from trading_bot.exchange.interface import RealClock
from trading_bot.logging_setup import setup_logging
from trading_bot.reporting.performance import (
    benchmark_comparison,
    equity_curve_from_rows,
    max_drawdown_pct,
    ratio_metrics,
    trade_stats,
)
from trading_bot.security.livegate import LiveGate
from trading_bot.security.secrets import EnvSecretProvider
from trading_bot.storage.audit import AuditLog
from trading_bot.storage.db import Database
from trading_bot.storage.repositories import Repositories

log = logging.getLogger(__name__)

DEFAULT_CONFIG = "config/paper.yaml"
_OPEN_DBS: list[Database] = []


def _load(args) -> tuple:
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    db = Database(cfg.db.url)
    _OPEN_DBS.append(db)
    db.migrate(Path("migrations"))
    return cfg, db, Repositories(db)


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "operator"


# ======================================================================
def cmd_run(args) -> int:
    from trading_bot.engine.trader import TradingEngine

    cfg, db, _repos = _load(args)
    if args.paper_only and cfg.mode != Mode.PAPER:
        print(
            f"error: this command runs PAPER mode only; config says {cfg.mode.value}",
            file=sys.stderr,
        )
        return 2
    engine = TradingEngine(cfg, config_path=args.config, db=db)
    print(
        f"starting engine: mode={cfg.mode.value} symbol={cfg.symbol} "
        f"interval={cfg.interval} strategy={cfg.strategy.name}"
    )
    try:
        engine.run(max_cycles=args.cycles)
    except PermissionError as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 3
    return 0


# ======================================================================
def cmd_status(args) -> int:
    cfg, db, repos = _load(args)
    mode = cfg.mode
    kill = KillSwitch(repos)
    kill_active, kill_reason = kill.check()

    latest = repos.balances.latest(mode)
    position = repos.positions.open_position(mode)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    de = repos.daily_equity.get(day, mode)
    last_recon = repos.events.last_reconciliation()
    last_decision = repos.decisions.last_decision(mode)
    pending = repos.orders.non_terminal_orders(mode)

    endpoint = {
        Mode.PAPER: "paper simulator (public market data only)",
        Mode.TESTNET: C.BINANCE_TESTNET_BASE_URL,
        Mode.LIVE: C.BINANCE_LIVE_BASE_URL,
    }[mode]

    equity = dec(str(latest["equity"])) if latest else ZERO
    peak = repos.balances.peak_equity(mode) or equity
    drawdown = (peak - equity) / peak * 100 if peak > ZERO else ZERO
    start = dec(str(de["start_equity"])) if de else equity
    day_pnl = equity - start

    balances = json.loads(latest["balances_json"]) if latest else {}

    r = cfg.risk
    print(
        f"""
trading-bot status
==================
Mode:                {mode.value.upper()}
Endpoint class:      {endpoint}
Kill switch:         {'ACTIVE — ' + kill_reason if kill_active else 'inactive'}
Paused flag:         {repos.flags.is_true(repos.flags.PAUSED)}
Approval until:      {repos.flags.get(repos.flags.APPROVAL_UNTIL) or '(none)'}

Equity:              {equity} (peak {peak}, drawdown {drawdown:.2f}%)
Day P&L (equity):    {day_pnl}
Balances:            {json.dumps(balances)}
Open position:       {json.dumps(position.as_dict()) if position else 'none'}
Pending orders:      {len(pending)}
Unknown orders:      {len(repos.orders.unknown_orders(mode))}

Risk limits:         risk/trade {r.max_risk_per_trade_pct}% | alloc {r.max_position_allocation_pct}% | reserve {r.min_cash_reserve_pct}%
                     daily loss {r.max_daily_loss_pct}% | 7d loss {r.max_7d_loss_pct}% | drawdown {r.max_drawdown_pct}%
                     entries/day {r.max_entries_per_day} | cooldown {r.cooldown_after_loss_hours}h | pause after {r.pause_after_consecutive_losses} losses

Last market update:  {latest['ts'] if latest else '(never)'}
Last reconciliation: {(last_recon['ts'] + (' OK' if last_recon['ok'] else ' MISMATCH')) if last_recon else '(never)'}
Last decision:       {(last_decision['ts'] + ' ' + str(last_decision['signal_action']) + ' — ' + str(last_decision['explanation'] or '')) if last_decision else '(never)'}
""".rstrip()
    )
    return 0


# ======================================================================
def cmd_stop(args) -> int:
    cfg, db, repos = _load(args)
    kill = KillSwitch(repos)
    kill.activate(KillSwitchSource.CLI, args.reason or f"manual stop by {_actor()}")
    AuditLog(db).append("cli.stop", {"actor": _actor(), "reason": args.reason or ""})
    print(
        "kill switch ACTIVATED. New entries are blocked in every mode.\n"
        f"A {C.STOP_FILE_NAME} file was also created as an independent backstop.\n"
        "Reset with: python -m trading_bot resume --note '<why>'"
    )
    return 0


def cmd_resume(args) -> int:
    cfg, db, repos = _load(args)
    kill = KillSwitch(repos)
    blockers = kill.reset(_actor(), args.note or "")
    ApprovalService(repos, cfg, RealClock()).resume(_actor(), args.note or "")
    AuditLog(db).append("cli.resume", {"actor": _actor(), "note": args.note or ""})
    if blockers:
        print("kill switch partially reset; remaining blockers:")
        for b in blockers:
            print(f"  - {b}")
        return 1
    print(
        "kill switch reset and pause cleared. "
        "(DAILY_APPROVAL mode still requires `approve` before entries resume.)"
    )
    return 0


def cmd_pause(args) -> int:
    cfg, db, repos = _load(args)
    ApprovalService(repos, cfg, RealClock()).pause(_actor(), args.note or "")
    print("paused: no new entries until `resume`. Exits/stop monitoring continue.")
    return 0


def cmd_approve(args) -> int:
    cfg, db, repos = _load(args)
    svc = ApprovalService(repos, cfg, RealClock())
    until = svc.approve(args.hours, _actor())
    AuditLog(db).append("cli.approve", {"actor": _actor(), "hours": args.hours})
    print(f"trading approved until {until}")
    return 0


# ======================================================================
def cmd_close_preview(args) -> int:
    cfg, db, repos = _load(args)
    position = repos.positions.open_position(cfg.mode)
    if position is None:
        print("no open position")
        return 0
    latest = repos.balances.latest(cfg.mode)
    price = dec(str(latest["quote_price"])) if latest and latest["quote_price"] else None
    if cfg.data.source == "exchange":
        try:
            from trading_bot.exchange.binance import BinancePublicData

            price = BinancePublicData().get_price(cfg.symbol).bid
        except Exception as exc:
            log.warning("live quote unavailable for preview, using last snapshot: %s", exc)
    if price is None:
        print("no price available for preview")
        return 1
    fee = position.qty * price * cfg.paper.taker_fee_bps / dec("10000")
    proceeds = position.qty * price - fee
    cost = position.avg_entry_price * position.qty + position.entry_fee
    print(
        f"""
close-position PREVIEW (no order will be placed)
  position:   {position.qty} {cfg.symbol} @ {position.avg_entry_price}
  stop:       {position.stop_price}
  mark (bid): {price}
  est fee:    {fee}
  est net:    {proceeds}
  est P&L:    {proceeds - cost}
To close for real, let the strategy/stop exit, or activate the kill switch with
emergency_position_policy: close_at_market in config.
""".rstrip()
    )
    return 0


# ======================================================================
def cmd_report(args) -> int:
    cfg, db, repos = _load(args)
    if args.kind == "daily":
        from trading_bot.engine.trader import default_rules
        from trading_bot.monitoring.health import HEALTH
        from trading_bot.reporting.daily import DailyReportBuilder

        builder = DailyReportBuilder(repos, cfg, default_rules(cfg.symbol), RealClock())
        day = args.day or builder.local_day()
        kill_active, kill_reason = KillSwitch(repos).check()
        report, md = builder.build(day, HEALTH.snapshot(), kill_active, kill_reason, False)
        out = Path(cfg.reporting.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"daily-{day}.md"
        path.write_text(md, encoding="utf-8")
        repos.reports.insert(day, "daily", cfg.mode, md, report)
        print(md)
        print(f"\n(saved to {path})")
        return 0

    # performance
    closed = repos.positions.closed_positions(cfg.mode, limit=1000)
    pnls = [dec(str(r["realized_pnl"])) for r in closed if r["realized_pnl"] is not None]
    fees = [dec(str(r["exit_fee"])) for r in closed if r["exit_fee"] is not None]
    stats = trade_stats(pnls, fees)
    snaps = repos.db.query(
        "SELECT equity FROM balance_snapshots WHERE mode = ? ORDER BY ts", (cfg.mode.value,)
    )
    curve = equity_curve_from_rows(snaps)
    ratios = ratio_metrics(curve) if len(curve) >= 3 else {}
    first = repos.balances.first(cfg.mode)
    latest = repos.balances.latest(cfg.mode)
    bench = {}
    if first and latest:
        bench = benchmark_comparison(
            dec(str(first["equity"])),
            dec(str(latest["equity"])),
            dec(str(first["quote_price"])) if first["quote_price"] else None,
            dec(str(latest["quote_price"])) if latest["quote_price"] else None,
        )
    print("performance report")
    print("==================")
    print(
        json.dumps(
            {
                "trades": stats,
                "ratios": ratios,
                "benchmarks": bench,
                "max_drawdown_pct": str(max_drawdown_pct(curve)) if curve else None,
            },
            indent=2,
        )
    )
    print("\nNote: past results (paper or live) do not guarantee future performance.")
    return 0


# ======================================================================
def cmd_live(args) -> int:
    cfg, db, repos = _load(args)
    secrets = EnvSecretProvider()
    from trading_bot.notifications.adapters import build_notification_hub

    hub = build_notification_hub(cfg, secrets)
    gate = LiveGate(
        repos,
        cfg,
        secrets,
        config_path=args.config,
        external_alert_probe=hub.verify_external_connectivity,
    )

    if args.live_cmd == "status":
        print("live-mode prerequisites:")
        all_ok = True
        for p in gate.prerequisites():
            mark = "PASS" if p.ok else "FAIL"
            all_ok &= p.ok
            print(f"  [{mark}] {p.name}: {p.detail}")
        print(
            f"  [{'PASS' if gate.is_unlocked() else 'FAIL'}] unlock ceremony: "
            f"{'valid unlock present' if gate.is_unlocked() else 'not completed'}"
        )
        print("\nlive mode is " + ("UNLOCKABLE (run `live unlock`)" if all_ok else "LOCKED"))
        return 0 if all_ok else 1

    # unlock ceremony
    failures = [p for p in gate.prerequisites() if not p.ok]
    if failures:
        print("live unlock refused — unmet prerequisites:")
        for p in failures:
            print(f"  [FAIL] {p.name}: {p.detail}")
        return 1

    print("=" * 66)
    print(" LIVE TRADING UNLOCK — read carefully")
    print("=" * 66)
    print(" Risk configuration that will govern live trading:")
    for k, v in gate.risk_summary().items():
        print(f"   {k:32s} {v}")
    print("""
 WARNINGS
 - Trading can lose ALL deposited capital.
 - A small account is heavily affected by fees, spread and minimum
   order sizes; expected edge may be smaller than round-trip costs.
 - Paper/backtest results do not guarantee live results.
 - The API key must have withdrawals DISABLED (verified at startup).
""")
    if not sys.stdin.isatty():
        print("refused: live unlock requires an interactive terminal")
        return 1
    unlock_id, phrase = gate.start_unlock()
    print(f" Type the following confirmation phrase exactly:\n\n   {phrase}\n")
    typed = input(" phrase> ")
    if not gate.confirm(unlock_id, typed):
        print("confirmation failed — live mode remains locked")
        return 1
    AuditLog(db).append("cli.live_unlock", {"actor": _actor()})
    print(f"""
live unlock recorded (valid {C.LIVE_UNLOCK_VALID_HOURS}h).
Two final steps remain, both deliberate:
  1. export {C.ENV_LIVE_ENABLED}=true
  2. start the engine with a config whose mode is 'live'
The engine will still verify key permissions (withdrawals must be disabled)
and every kill switch before the first evaluation.""")
    return 0


# ======================================================================
def cmd_db(args) -> int:
    cfg, db, repos = _load(args)
    if args.db_cmd == "migrate":
        applied = db.migrate(Path("migrations"))
        print(f"migrations applied: {applied or 'none (up to date)'}")
        return 0
    # backup (SQLite file copy; for Postgres use pg_dump — docs/OPERATIONS.md)
    if not cfg.db.url.startswith("sqlite:///"):
        print(
            "db backup here supports SQLite; use pg_dump for PostgreSQL " "(see docs/OPERATIONS.md)"
        )
        return 1
    src = Path(cfg.db.url[len("sqlite:///") :])
    out = Path(args.out or f"var/backups/trading_bot-{datetime.now(UTC):%Y%m%d-%H%M%S}.db")
    out.parent.mkdir(parents=True, exist_ok=True)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(src, out)
    print(f"backup written to {out}")
    return 0


def cmd_audit(args) -> int:
    _cfg, db, _repos = _load(args)
    ok, bad_seq = AuditLog(db).verify_chain()
    if ok:
        print("audit chain OK")
        return 0
    print(f"AUDIT CHAIN BROKEN at seq {bad_seq}")
    return 1


def cmd_quality(args) -> int:
    import runpy

    from trading_bot.security.quality import verify_quality_record

    root = Path.cwd()
    if args.quality_cmd == "run":
        try:
            runpy.run_path(str(root / "scripts/record_test_run.py"), run_name="__main__")
        except SystemExit as exc:
            if int(exc.code or 0) != 0:
                return int(exc.code or 1)
    result = verify_quality_record(root, require_repo=False)
    if result.ok:
        print("quality evidence OK")
        print(
            json.dumps(
                {
                    "tests_collected": result.record.get("tests_collected"),
                    "tests_passed": result.record.get("tests_passed"),
                    "tests_failed": result.record.get("tests_failed"),
                    "coverage_percent": result.record.get("coverage_percent"),
                    "git_state": result.record.get("git_state"),
                },
                indent=2,
            )
        )
        return 0
    print("quality evidence FAILED")
    for failure in result.failures:
        print(f"  - {failure}")
    return 1


# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trading-bot",
        description="Safety-first automated spot trading (paper by default).",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG, help="path to YAML config")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("paper", help="paper trading")
    psub = sp.add_subparsers(dest="paper_cmd", required=True)
    prun = psub.add_parser("run", help="run the paper engine")
    prun.add_argument("--cycles", type=int, default=None)
    prun.set_defaults(fn=cmd_run, paper_only=True)

    sr = sub.add_parser("run", help="run engine in the config's mode (testnet/live gated)")
    sr.add_argument("--cycles", type=int, default=None)
    sr.set_defaults(fn=cmd_run, paper_only=False)

    sub.add_parser("status", help="full status").set_defaults(fn=cmd_status)

    st = sub.add_parser("stop", help="activate kill switch")
    st.add_argument("--reason", default="")
    st.set_defaults(fn=cmd_stop)

    rs = sub.add_parser("resume", help="reset kill switch + pause (manual)")
    rs.add_argument("--note", default="")
    rs.set_defaults(fn=cmd_resume)

    pa = sub.add_parser("pause", help="pause new entries")
    pa.add_argument("--note", default="")
    pa.set_defaults(fn=cmd_pause)

    ap = sub.add_parser("approve", help="approve trading window (DAILY_APPROVAL)")
    ap.add_argument("--hours", type=int, default=None)
    ap.set_defaults(fn=cmd_approve)

    sub.add_parser(
        "close-position-preview", help="preview closing the open position (no order)"
    ).set_defaults(fn=cmd_close_preview)

    rp = sub.add_parser("report", help="reports")
    rsub = rp.add_subparsers(dest="kind", required=True)
    rd = rsub.add_parser("daily")
    rd.add_argument("--day", default=None, help="YYYY-MM-DD (local timezone)")
    rd.set_defaults(fn=cmd_report)
    rsub.add_parser("performance").set_defaults(fn=cmd_report)

    bt = sub.add_parser("backtest", help="backtesting")
    btsub = bt.add_subparsers(dest="bt_cmd", required=True)
    btr = btsub.add_parser("run")
    btr.add_argument("--data", required=True, help="fixture CSV path")
    btr.add_argument("--walk-forward", action="store_true")
    btr.add_argument("--out", default=None)

    def _bt(args):
        from trading_bot.backtest.engine import cmd_backtest

        return cmd_backtest(args)

    btr.set_defaults(fn=_bt)

    lv = sub.add_parser("live", help="live-mode gate")
    lvsub = lv.add_subparsers(dest="live_cmd", required=True)
    lvsub.add_parser("status").set_defaults(fn=cmd_live)
    lvsub.add_parser("unlock").set_defaults(fn=cmd_live)

    dbp = sub.add_parser("db", help="database ops")
    dbsub = dbp.add_subparsers(dest="db_cmd", required=True)
    dbsub.add_parser("migrate").set_defaults(fn=cmd_db)
    dbb = dbsub.add_parser("backup")
    dbb.add_argument("--out", default=None)
    dbb.set_defaults(fn=cmd_db)

    au = sub.add_parser("audit", help="audit log")
    ausub = au.add_subparsers(dest="audit_cmd", required=True)
    ausub.add_parser("verify").set_defaults(fn=cmd_audit)

    qu = sub.add_parser("quality", help="quality evidence")
    qusub = qu.add_subparsers(dest="quality_cmd", required=True)
    qusub.add_parser("run").set_defaults(fn=cmd_quality)
    qusub.add_parser("verify").set_defaults(fn=cmd_quality)

    sub.add_parser("version").set_defaults(fn=lambda a: print(__version__) or 0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        rc = args.fn(args)
        return int(rc or 0)
    finally:
        while _OPEN_DBS:
            db = _OPEN_DBS.pop()
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
