#!/usr/bin/env bash
# Deploy/update the TESTNET stack (docker-compose.testnet.yml).
#
# Separate from scripts/deploy_update.sh, which drives the paper stack. Run
# this on the testnet host only. Like the paper deployer it stamps the image
# with the deployed commit so build provenance is recorded.
#
# Usage: ./scripts/deploy_testnet.sh [--no-pull]
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "${1:-}" != "--no-pull" ]]; then
    git pull --ff-only
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "error: not a git checkout — cannot determine the deployed commit" >&2
    exit 1
fi
GIT_COMMIT="$(git rev-parse HEAD)"
export GIT_COMMIT

COMPOSE_FILE=docker-compose.testnet.yml

if [[ ! -f .env ]] || ! grep -q '^BINANCE_TESTNET_API_KEY=.' .env; then
    echo "error: set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET in .env first" >&2
    echo "       (create keys at https://testnet.binance.vision)" >&2
    exit 1
fi

if docker info >/dev/null 2>&1; then
    COMPOSE=(docker compose -f "$COMPOSE_FILE")
else
    COMPOSE=(sudo -E docker compose -f "$COMPOSE_FILE")
fi

echo "[deploy-testnet] building ${GIT_COMMIT:0:12}"
"${COMPOSE[@]}" up -d --build

echo "[deploy-testnet] container build stamp:"
"${COMPOSE[@]}" exec -T bot cat /app/.build_info.json
