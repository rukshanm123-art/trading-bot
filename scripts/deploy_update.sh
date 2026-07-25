#!/usr/bin/env bash
# Update a Docker deployment to the current origin commit.
#
# Use this instead of a bare `docker compose up -d --build`: it stamps the
# image with the deployed commit (GIT_COMMIT). Without that stamp the running
# container cannot identify its own code, and every qualification evidence
# record it writes is rejected by the live gate — silently burning the paper
# period.
#
# Usage: ./scripts/deploy_update.sh [--no-pull]
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

if [[ -n "$(git status --porcelain)" ]]; then
    echo "warning: working tree is dirty; evidence will name commit $GIT_COMMIT" >&2
fi

# The VM's docker usually needs sudo; a developer Mac usually does not.
if docker info >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    COMPOSE=(sudo -E docker compose)
fi

echo "[deploy] building ${GIT_COMMIT:0:12}"
"${COMPOSE[@]}" up -d --build

echo "[deploy] confirming the container can identify itself"
"${COMPOSE[@]}" exec -T bot cat /app/.build_info.json
