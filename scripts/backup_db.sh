#!/usr/bin/env bash
# Database backup helper. SQLite: consistent file copy. Postgres: pg_dump.
# Usage: ./scripts/backup_db.sh [output-dir]
set -euo pipefail

OUT_DIR="${1:-var/backups}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR"

if [[ -n "${DATABASE_URL:-}" && "$DATABASE_URL" == postgresql://* ]]; then
    command -v pg_dump >/dev/null || { echo "pg_dump not found"; exit 1; }
    pg_dump "$DATABASE_URL" --format=custom --file="$OUT_DIR/trading_bot-$STAMP.pgdump"
    echo "postgres backup: $OUT_DIR/trading_bot-$STAMP.pgdump"
else
    # default SQLite path used by config/paper.yaml
    DB_FILE="${2:-var/trading_bot.db}"
    [[ -f "$DB_FILE" ]] || { echo "no database at $DB_FILE"; exit 1; }
    sqlite3 "$DB_FILE" "PRAGMA wal_checkpoint(TRUNCATE);" || true
    cp "$DB_FILE" "$OUT_DIR/trading_bot-$STAMP.db"
    echo "sqlite backup: $OUT_DIR/trading_bot-$STAMP.db"
fi

# Retention: keep the newest 30 backups
ls -1t "$OUT_DIR" | tail -n +31 | while read -r old; do rm -f "$OUT_DIR/$old"; done
