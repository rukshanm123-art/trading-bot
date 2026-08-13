#!/usr/bin/env bash
# Back up everything live qualification depends on, and prove it restores.
#
# WHY BOTH HALVES: the qualification evidence ledger is HMAC-signed with a key
# stored in the database (control_flags.qualification_evidence_key). The ledger
# without the key is unverifiable; the key without the ledger proves nothing.
# Losing either one restarts the 30-day clock at zero, so they are captured
# together, in one timestamped set, with checksums.
#
# `db backup` in the CLI is SQLite-only and refuses PostgreSQL, which is what
# the qualification deployment actually runs — hence this script.
#
# Safe to run against a LIVE engine: pg_dump is MVCC-consistent and takes no
# locks that block writers, and the ledger is append-only.
#
# Usage:
#   ./scripts/backup_qualification.sh                 # back up + verify
#   ./scripts/backup_qualification.sh --verify-restore # also restore into a
#                                                      # scratch DB and check it
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_ROOT="${BACKUP_ROOT:-$HOME/trading-bot-backups}"
RETAIN="${RETAIN:-14}"
DB_USER="${DB_USER:-bot}"
DB_NAME="${DB_NAME:-trading_bot}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_ROOT/$STAMP"
VERIFY_RESTORE=0
[[ "${1:-}" == "--verify-restore" ]] && VERIFY_RESTORE=1

if docker info >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    COMPOSE=(sudo docker compose)
fi

log() { echo "$(date -u +%H:%M:%SZ) $*"; }
fail() { echo "BACKUP FAILED: $*" >&2; exit 1; }

STAGED=""
SCRATCH=""
cleanup() {
    # A trap STRING containing "${COMPOSE[@]}" word-splits and corrupts the
    # handler, so cleanup lives in a function where the array expands normally.
    [[ -n "$STAGED" ]] && "${COMPOSE[@]}" exec -T db rm -f "$STAGED" >/dev/null 2>&1
    [[ -n "$SCRATCH" ]] && "${COMPOSE[@]}" exec -T db dropdb -U "$DB_USER" \
        --if-exists "$SCRATCH" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

mkdir -p "$DEST"
log "backing up to $DEST"

# Sweep debris from any run that died before its cleanup ran (a killed shell,
# an OOM, an earlier bug). Staged dumps are a few MB each and would otherwise
# accumulate in the db container until it restarts.
"${COMPOSE[@]}" exec -T db sh -c 'rm -f /tmp/verify_*.dump' >/dev/null 2>&1 || true

# ---- 1. PostgreSQL (decisions, positions, reports, evidence HMAC key) -------
log "pg_dump ..."
"${COMPOSE[@]}" exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc \
    > "$DEST/$DB_NAME.dump" || fail "pg_dump failed"
[[ -s "$DEST/$DB_NAME.dump" ]] || fail "pg_dump produced an empty file"

# ---- 2. Evidence ledger + quality artifacts (inside the bot volume) ---------
log "capturing var/quality ..."
"${COMPOSE[@]}" exec -T bot tar -cf - -C /app var/quality \
    > "$DEST/quality.tar" 2>/dev/null || fail "could not capture var/quality"
[[ -s "$DEST/quality.tar" ]] || fail "quality.tar is empty"

# ---- 3. Provenance: what was running when this was taken -------------------
{
    echo "taken_at_utc=$STAMP"
    echo "git_commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    "${COMPOSE[@]}" exec -T bot cat /app/.build_info.json 2>/dev/null || true
} > "$DEST/MANIFEST.txt"

# ---- 4. Checksums so silent corruption is detectable later -----------------
( cd "$DEST" && sha256sum ./*.dump ./*.tar > SHA256SUMS )
log "checksums written"

# ---- 5. Integrity check of the dump itself --------------------------------
# pg_restore --list parses the archive's table of contents; a truncated or
# corrupt dump fails here rather than at 3am when it is actually needed.
# The archive must be staged as a FILE inside the container: a custom-format
# dump needs to seek, and a piped /dev/stdin cannot, so reading it from a pipe
# fails even when the dump is perfectly good.
STAGED=/tmp/verify_${STAMP}.dump
"${COMPOSE[@]}" exec -T db sh -c "cat > $STAGED" < "$DEST/$DB_NAME.dump" \
    || fail "could not stage the dump inside the db container"

if "${COMPOSE[@]}" exec -T db pg_restore --list "$STAGED" > "$DEST/toc.txt" 2>/dev/null; then
    log "dump TOC readable ($(wc -l < "$DEST/toc.txt") entries)"
else
    fail "pg_restore could not read the dump — it is NOT a usable backup"
fi

# ---- 6. Optional: prove it actually restores ------------------------------
# A backup that has never been restored is a hypothesis. This restores into a
# THROWAWAY database on the same server (never touching the live one) and
# checks the rows that qualification depends on.
if [[ $VERIFY_RESTORE -eq 1 ]]; then
    SCRATCH="restore_check_$(date -u +%s)"  # cleaned up by the EXIT trap
    log "restore drill into scratch database $SCRATCH ..."
    "${COMPOSE[@]}" exec -T db createdb -U "$DB_USER" "$SCRATCH" || fail "createdb failed"

    "${COMPOSE[@]}" exec -T db pg_restore -U "$DB_USER" -d "$SCRATCH" "$STAGED" \
        >/dev/null 2>&1 || fail "pg_restore into scratch failed"

    CHECKS=$("${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$SCRATCH" -t -A -F'|' -c "
        SELECT
          (SELECT count(*) FROM decisions),
          (SELECT count(*) FROM positions),
          (SELECT count(*) FROM audit_log),
          (SELECT count(*) FROM control_flags WHERE key='qualification_evidence_key');
    " | tr -d ' ')
    IFS='|' read -r N_DEC N_POS N_AUD N_KEY <<< "$CHECKS"
    log "restored: decisions=$N_DEC positions=$N_POS audit=$N_AUD evidence_key=$N_KEY"
    [[ "${N_DEC:-0}" -gt 0 ]] || fail "restored database has no decisions"
    [[ "${N_KEY:-0}" -eq 1 ]] || fail "restored database is MISSING the evidence HMAC key"
    echo "restore_verified=yes decisions=$N_DEC positions=$N_POS audit=$N_AUD" \
        >> "$DEST/MANIFEST.txt"
    log "restore drill PASSED"
fi

# ---- 7. Prune old sets ----------------------------------------------------
mapfile -t OLD < <(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | head -n -"$RETAIN")
for d in "${OLD[@]:-}"; do
    [[ -n "$d" ]] && rm -rf "$d" && log "pruned $(basename "$d")"
done

log "OK — $(du -sh "$DEST" | cut -f1) in $DEST"
echo
echo "REMINDER: this copy is on the SAME HOST as the data it protects."
echo "Pull it somewhere else, or losing the instance still loses everything:"
echo "  scp -r -i <key> opc@<host>:$DEST ~/trading-bot-backups/"
