#!/usr/bin/env bash
# Install (or refresh) the testnet reset watchdog cron entry on the TESTNET host.
#
# Runs scripts/testnet_reset_watchdog.py every 10 minutes. The watchdog itself
# refuses to do anything unless it is unambiguously the testnet deployment, so
# installing it on the wrong host is inert rather than dangerous — but only
# install it on the testnet VM.
#
# RECOMMENDED ROLLOUT — install in --dry-run first. The "does nothing when
# healthy" path is easy to verify, but the RECOVERY path can only be observed
# on a real reset. Run observe-only until the log shows it correctly diagnosed
# one, then re-install without --dry-run to let it act.
#
# Usage: ./scripts/install_testnet_watchdog.sh [--dry-run | --uninstall]
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
MARKER="# trading-bot testnet reset watchdog"
LOG="$ROOT/var/testnet_watchdog.log"

if [[ "${1:-}" == "--uninstall" ]]; then
    crontab -l 2>/dev/null | grep -v "$MARKER" | crontab - || true
    echo "watchdog cron removed"
    exit 0
fi

DRY=""
MODE="LIVE (will sell faucet BTC and clear a circuit-breaker halt)"
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY=" --dry-run"
    MODE="DRY-RUN (observe only, never acts)"
fi

if ! grep -qE '^mode:[[:space:]]*testnet' config/testnet.yaml; then
    echo "error: config/testnet.yaml is not mode: testnet — refusing to install" >&2
    exit 1
fi

mkdir -p "$ROOT/var"

# sudo is needed for docker on the Oracle VMs; keep the env minimal.
ENTRY="*/10 * * * * cd $ROOT && /usr/bin/python3 scripts/testnet_reset_watchdog.py$DRY >> $LOG 2>&1 $MARKER"

# replace any previous entry, then append the current one
( crontab -l 2>/dev/null | grep -v "$MARKER" || true; echo "$ENTRY" ) | crontab -

echo "installed in mode: $MODE"
crontab -l | grep "$MARKER"
echo
echo "log: $LOG"
echo "run once now: python3 scripts/testnet_reset_watchdog.py --dry-run"
if [[ -n "$DRY" ]]; then
    echo
    echo "Observe-only. After it has correctly diagnosed one REAL reset in the"
    echo "log above, re-run this script with no arguments to let it act."
fi
