#!/usr/bin/env python3
"""Testnet-only watchdog: auto-recover from Binance Spot Testnet resets.

Binance wipes and re-funds testnet accounts every few days. A reset re-adds
~1 BTC the engine never bought, so startup reconciliation fail-closes and the
circuit breaker latches the kill switch. (The wiped kline history self-heals
on the 5m interval within ~2h, so only the balance side needs recovery.)

This script detects that specific signature and recovers it hands-free:
sell the faucet BTC back to quote -> restart the stack -> clear the latched
circuit-breaker halt -> Telegram summary.

SAFETY — this automation deliberately does two things we never allow in live
(selling holdings, clearing a safety halt), so it is fenced in hard. It
refuses to act unless ALL of these hold:

  * the resolved config is mode=testnet AND the adapter endpoint is the
    official Spot Testnet host (never live, whatever env is set);
  * LIVE_TRADING_ENABLED is not true and no live API keys are present;
  * the engine reports NO open position (never liquidates a real position);
  * the unexplained base balance looks like the faucet grant (>= MIN_FAUCET_BTC);
  * the kill switch, if active, was latched by the CIRCUIT BREAKER. An
    operator stop (cli/env/db_flag/file) is NEVER auto-cleared;
  * every serious clause in the latched reason is independently re-verified as
    RESOLVED in the current engine state - a real drawdown breach, outstanding
    unknown orders, a database-integrity failure or a still-mismatched
    reconciliation all block the clear. The latched text is stale by
    definition and a reset trips several clauses at once, so text matching
    alone is never sufficient. Unrecognised clauses block too.

Anything it will not handle, it reports and leaves alone. Run from cron on
the testnet host (see docs/TESTNET_DRILLS.md); it holds a lock file and a
cooldown so overlapping or runaway runs are impossible.

Usage:
    python3 scripts/testnet_reset_watchdog.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import time
import urllib.parse
import urllib.request

# NOTE: timezone.utc, not datetime.UTC — this script must run on the VM HOST
# (during a real reset the container is crash-looping, so `docker compose exec`
# is unavailable), and Oracle Linux 9 ships Python 3.9. Keep this file 3.9-clean.
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = "docker-compose.testnet.yml"
CONFIG = "config/testnet.yaml"
TESTNET_HOST = "testnet.binance.vision"
BASE = f"https://{TESTNET_HOST}"
BASE_ASSET = "BTC"

MIN_FAUCET_BTC = Decimal("0.5")  # faucet grants ~1 BTC; well above any dust
LOCK_FILE = Path("/tmp/testnet_reset_watchdog.lock")  # nosec B108 - fixed, own host
STATE_FILE = ROOT / "var" / "testnet_watchdog_state.json"
COOLDOWN_S = 3600  # never recover more than once an hour
LOCK_STALE_S = 1800

# Circuit-breaker reason fragments a testnet reset legitimately produces.
RESET_SIGNATURE = ("market-data failures", "reconciliation mismatch", "api errors")
# Circuit-breaker clauses that are NEVER auto-cleared on stale text alone: each
# must be independently confirmed resolved in the CURRENT engine state below.
# (These are circuit.py's exact `hard` reasons — matching the real strings, not
# risk-engine entry blocks, which never latch the circuit breaker.)
SERIOUS_CLAUSES = ("unknown order", "database integrity", "drawdown")


def log(msg: str) -> None:
    # datetime.UTC is 3.11+; this file must run on the VM host's Python 3.9
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017
    print(f"{stamp} {msg}", flush=True)


# ----------------------------------------------------------------- guards
def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v
    return env


def assert_testnet_only(env: dict[str, str]) -> None:
    """Fail closed unless this is unambiguously the testnet deployment."""
    cfg = (ROOT / CONFIG).read_text()
    if not re.search(r"^mode:\s*testnet\s*$", cfg, re.MULTILINE):
        raise SystemExit(f"REFUSING: {CONFIG} is not mode: testnet")
    if not (ROOT / COMPOSE_FILE).exists():
        raise SystemExit(f"REFUSING: {COMPOSE_FILE} not found")
    live_flag = env.get("LIVE_TRADING_ENABLED") or os.environ.get("LIVE_TRADING_ENABLED") or ""
    if live_flag.strip().lower() in ("true", "1", "yes"):
        raise SystemExit("REFUSING: LIVE_TRADING_ENABLED is set")
    for key in ("BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET"):
        if (env.get(key) or os.environ.get(key) or "").strip():
            raise SystemExit(f"REFUSING: live credential {key} is present")


# ------------------------------------------------------------- exchange io
def _keys(env: dict[str, str]) -> tuple[str, str]:
    key = os.environ.get("BINANCE_TESTNET_API_KEY") or env.get("BINANCE_TESTNET_API_KEY", "")
    sec = os.environ.get("BINANCE_TESTNET_API_SECRET") or env.get("BINANCE_TESTNET_API_SECRET", "")
    if not key or not sec:
        raise SystemExit("REFUSING: testnet credentials not found")
    return key, sec


def _signed(env: dict[str, str], path: str, params: dict, method: str = "GET"):
    key, sec = _keys(env)
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), query.encode(), hashlib.sha256).hexdigest()
    if method == "GET":
        req = urllib.request.Request(
            f"{BASE}{path}?{query}&signature={sig}", headers={"X-MBX-APIKEY": key}
        )
    else:
        req = urllib.request.Request(
            BASE + path,
            data=f"{query}&signature={sig}".encode(),
            headers={"X-MBX-APIKEY": key},
            method="POST",
        )
    if urllib.parse.urlparse(req.full_url).hostname != TESTNET_HOST:
        raise SystemExit("REFUSING: request host is not the Spot Testnet")
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - host asserted
        return json.load(resp)


def _public(path: str, params: dict):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as resp:  # nosec B310 - fixed host
        return json.load(resp)


def free_balance(env: dict[str, str], asset: str) -> Decimal:
    acct = _signed(env, "/api/v3/account", {})
    return next((Decimal(b["free"]) for b in acct["balances"] if b["asset"] == asset), Decimal(0))


# --------------------------------------------------------------- engine io
def compose(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, *args]
    if not _docker_ok():
        cmd = ["sudo", *cmd]
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_ok() -> bool:
    return (
        subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["docker", "info"], capture_output=True, timeout=30, check=False
        ).returncode
        == 0
    )


def engine_status() -> str:
    """`status` via a one-off container so it works while the main one is
    crash-looping. Read-only: it never places an order."""
    proc = compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "bot",
        "python",
        "-m",
        "trading_bot",
        "--config",
        CONFIG,
        "status",
    )
    return proc.stdout + proc.stderr


def parse_status(text: str) -> dict:
    def grab(label: str) -> str:
        m = re.search(rf"^{label}:\s*(.+)$", text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    kill = grab("Kill switch")
    position = grab("Open position")
    equity = grab("Equity")
    recon = grab("Last reconciliation")
    unknown_raw = grab("Unknown orders")
    dd = re.search(r"drawdown\s*([0-9.]+)\s*%", equity)
    # `parsed` is False when the CLI produced no usable status (container down,
    # command failed). Every consumer must then fail closed rather than guess.
    return {
        "parsed": bool(kill or position or equity),
        "kill_raw": kill,
        "kill_active": kill.upper().startswith("ACTIVE"),
        "position": position,
        "flat": position.lower().startswith("none"),
        "unknown_orders": int(unknown_raw) if unknown_raw.isdigit() else None,
        "drawdown_pct": Decimal(dd.group(1)) if dd else None,
        "reconciliation_ok": recon.upper().endswith("OK"),
    }


def max_drawdown_pct() -> Decimal:
    """The configured drawdown ceiling, read from the testnet config."""
    m = re.search(
        r"^\s*max_drawdown_pct:\s*\"?([0-9.]+)", (ROOT / CONFIG).read_text(), re.MULTILINE
    )
    return Decimal(m.group(1)) if m else Decimal("8")


def notify(env: dict[str, str], subject: str, body: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")
    log(f"NOTIFY {subject}: {body}")
    if not token or not chat:
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": f"[testnet watchdog] {subject}\n{body}"[:4000]}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"
        )
        urllib.request.urlopen(req, timeout=15).read()  # nosec B310 - fixed host
    except Exception as exc:  # notification failure must never break recovery
        log(f"telegram notify failed: {type(exc).__name__}")


# ---------------------------------------------------------------- recovery
def sell_faucet_base(env: dict[str, str], qty_free: Decimal, dry_run: bool) -> str:
    info = _public("/api/v3/exchangeInfo", {"symbol": "BTCUSDT"})
    step = Decimal("0.00001")
    for f in info["symbols"][0]["filters"]:
        if f["filterType"] == "LOT_SIZE" and Decimal(f["stepSize"]) > 0:
            step = Decimal(f["stepSize"])
    qty = (qty_free // step) * step
    qty_str = format(qty.normalize(), "f")
    if qty <= 0:
        return "nothing to sell"
    if dry_run:
        return f"DRY-RUN would sell {qty_str} {BASE_ASSET}"
    res = _signed(
        env,
        "/api/v3/order",
        {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": qty_str},
        method="POST",
    )
    return f"sold {res.get('executedQty')} {BASE_ASSET} -> {res.get('cummulativeQuoteQty')} USDT"


def clear_halt_if_reset(status: dict, dry_run: bool) -> str:
    """Clear the latched halt ONLY for a testnet reset whose underlying causes
    are provably resolved RIGHT NOW.

    The latched reason string is stale by definition (it describes the moment
    of the trip), and a reset trips several clauses at once — so matching text
    alone could clear a genuinely serious halt that merely co-occurred with
    reset noise. Every serious clause is therefore re-checked against the
    CURRENT engine state, and anything unrecognised or unresolved is left for
    a human."""
    if not status["parsed"]:
        return "NOT cleared (could not read engine status)"
    if not status["kill_active"]:
        return "kill switch already inactive"
    reason = status["kill_raw"]
    low = reason.lower()
    if "circuit_breaker" not in low:
        return f"NOT cleared (operator stop, needs a human): {reason}"

    # Re-verify every serious clause against live state; stale text never suffices.
    if "unknown order" in low and status["unknown_orders"] != 0:
        return f"NOT cleared (unknown orders still open): {reason}"
    if "database integrity" in low:
        return f"NOT cleared (database integrity — needs a human): {reason}"
    if "drawdown" in low:
        current, ceiling = status["drawdown_pct"], max_drawdown_pct()
        if current is None:
            return f"NOT cleared (drawdown clause, current drawdown unreadable): {reason}"
        if current >= ceiling:
            return f"NOT cleared (REAL drawdown {current}% >= {ceiling}%): {reason}"
    if not status["reconciliation_ok"]:
        return f"NOT cleared (reconciliation still MISMATCH): {reason}"
    # Only reset-consistent clauses may remain.
    residual = [
        c for c in low.split(";") if not any(s in c for s in RESET_SIGNATURE + SERIOUS_CLAUSES)
    ]
    if residual:
        return f"NOT cleared (unrecognised clause {residual}): {reason}"
    if not any(s in low for s in RESET_SIGNATURE):
        return f"NOT cleared (no reset signature): {reason}"
    if dry_run:
        return "DRY-RUN would clear circuit-breaker halt"
    proc = compose("exec", "-T", "bot", "python", "-m", "trading_bot", "--config", CONFIG, "resume")
    return "halt cleared" if proc.returncode == 0 else f"resume failed: {proc.stderr[:160]}"


def _cooldown_blocked() -> bool:
    try:
        last = json.loads(STATE_FILE.read_text()).get("last_recovery_ts", 0)
    except (OSError, ValueError):
        return False
    return (time.time() - float(last)) < COOLDOWN_S


def _record_recovery() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_recovery_ts": time.time()}))


def _acquire_lock() -> bool:
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_S:
            return False
        LOCK_FILE.unlink(missing_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="detect and report; change nothing")
    args = ap.parse_args()

    env = _load_env()
    assert_testnet_only(env)

    if not _acquire_lock():
        log("another watchdog run is in progress; exiting")
        return 0
    try:
        base_free = free_balance(env, BASE_ASSET)
        status = parse_status(engine_status())
        log(f"{BASE_ASSET} free={base_free} | flat={status['flat']} | kill={status['kill_raw']}")

        if base_free < MIN_FAUCET_BTC:
            log("no faucet balance detected; nothing to do")
            return 0
        if not status["parsed"]:
            notify(
                env,
                "manual attention needed",
                f"Unexplained {base_free} {BASE_ASSET} on the account, but the engine "
                f"status could not be read (container down?). Refusing to sell blind.",
            )
            return 1
        if not status["flat"]:
            notify(
                env,
                "manual attention needed",
                f"Unexplained {base_free} {BASE_ASSET} but an OPEN POSITION exists "
                f"({status['position']}). Refusing to sell — please review.",
            )
            return 1
        if _cooldown_blocked():
            log("within cooldown window since last recovery; exiting")
            return 0

        log("testnet reset signature detected — recovering")
        # Record BEFORE selling: if anything later in the flow crashes, the
        # cooldown still prevents the next cron tick from selling again.
        if not args.dry_run:
            _record_recovery()
        sold = sell_faucet_base(env, base_free, args.dry_run)
        log(sold)

        if not args.dry_run:
            compose("up", "-d", "bot", timeout=300)
            for _ in range(14):  # lock handoff takes up to ~2 min
                time.sleep(12)
                if "healthy" in (compose("ps", "--format", "{{.Status}}").stdout or ""):
                    break

        cleared = clear_halt_if_reset(parse_status(engine_status()), args.dry_run)
        log(cleared)

        final = parse_status(engine_status())
        notify(
            env,
            "reset recovered" if not final["kill_active"] else "reset partially recovered",
            f"{sold}\n{cleared}\nkill switch: {final['kill_raw'] or 'inactive'}",
        )
        return 0
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
