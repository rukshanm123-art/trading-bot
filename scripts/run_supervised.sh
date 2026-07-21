#!/usr/bin/env bash
# Supervised runner: restarts the bot after crashes with exponential backoff.
# Respects the kill switches: if STOP_TRADING exists or TRADING_KILL_SWITCH is
# set, the loop still restarts the process (the bot itself refuses entries and
# keeps monitoring exits), but a clean exit (code 0) ends supervision.
#
# Usage: ./scripts/run_supervised.sh [config/paper.yaml]
set -euo pipefail

CONFIG="${1:-config/paper.yaml}"
cd "$(dirname "$0")/.."

# Load deploy env (DATABASE_URL, notifier secrets) if a .env is present, so a
# Postgres-backed qualification run always reaches the right database.
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    echo "[supervisor] loaded .env (DATABASE_URL=${DATABASE_URL:+set})"
fi

BACKOFF=5
MAX_BACKOFF=300

while true; do
    echo "[supervisor] starting trading-bot ($CONFIG) at $(date -u +%FT%TZ)"
    set +e
    PYTHONPATH=src .venv/bin/python -m trading_bot --config "$CONFIG" paper run
    RC=$?
    set -e
    if [[ $RC -eq 0 ]]; then
        echo "[supervisor] clean exit; supervision ends"
        exit 0
    fi
    echo "[supervisor] bot exited rc=$RC; restarting in ${BACKOFF}s"
    sleep "$BACKOFF"
    BACKOFF=$(( BACKOFF * 2 ))
    (( BACKOFF > MAX_BACKOFF )) && BACKOFF=$MAX_BACKOFF
done
