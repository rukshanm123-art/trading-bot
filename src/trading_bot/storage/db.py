"""Database core: SQLite (dev/test) and PostgreSQL (production) behind one API.

SQL is written with ``?`` placeholders and translated for Postgres. Migrations
are plain numbered SQL files applied in order inside a transaction, recorded in
``schema_migrations``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def uid() -> str:
    return uuid.uuid4().hex


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.RLock()
        self._closed = False
        self._transaction_depth = 0
        if url.startswith("sqlite:///"):
            self.backend = "sqlite"
            path = url[len("sqlite:///") :]
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        elif url.startswith("postgresql://"):
            self.backend = "postgres"
            try:
                import psycopg  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - needs optional extra
                raise DatabaseError(
                    "PostgreSQL support requires the 'postgres' extra: pip install '.[postgres]'"
                ) from exc
            self._conn = psycopg.connect(url, autocommit=False)  # pragma: no cover
        else:
            raise DatabaseError(f"Unsupported database URL scheme: {url.split(':', 1)[0]}")

    # ------------------------------------------------------------------
    def _adapt(self, sql: str) -> str:
        if self.backend == "postgres":
            return sql.replace("?", "%s")
        return sql

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise DatabaseError("database connection is closed")

    def __enter__(self) -> Database:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._ensure_open()
            cur = self._conn.cursor()
            try:
                cur.execute(self._adapt(sql), tuple(params))
                if self._transaction_depth == 0:
                    self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute and return the affected-row count (atomic compare-and-swap
        support: the CALLER decides success from the row count, never from a
        separate read)."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(self._adapt(sql), tuple(params))
                count = cur.rowcount
                if self._transaction_depth == 0:
                    self._conn.commit()
                return count if count is not None else 0
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            cur = self._conn.cursor()
            try:
                cur.execute(self._adapt(sql), tuple(params))
                if self.backend == "sqlite":
                    rows = [dict(r) for r in cur.fetchall()]
                else:  # pragma: no cover
                    cols = [d[0] for d in cur.description or []]
                    rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
                if self._transaction_depth == 0:
                    self._conn.commit()
                return rows
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Multiple statements atomically. Yields a cursor using ? placeholders."""
        with self._lock:
            self._ensure_open()
            cur = self._conn.cursor()
            outermost = self._transaction_depth == 0
            self._transaction_depth += 1

            class _TxCursor:
                def __init__(self, inner: Any, adapt: Any) -> None:
                    self._inner = inner
                    self._adapt = adapt

                def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
                    self._inner.execute(self._adapt(sql), tuple(params))

            try:
                yield _TxCursor(cur, self._adapt)
                if outermost:
                    self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1
                cur.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self.backend == "sqlite":
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
            self._closed = True

    # ------------------------------------------------------------------
    def migrate(self, migrations_dir: str | Path) -> list[str]:
        """Apply pending migrations in filename order. Returns versions applied."""
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r["version"] for r in self.query("SELECT version FROM schema_migrations")}
        directory = Path(migrations_dir)
        if not directory.exists():
            raise DatabaseError(f"Migrations directory not found: {directory}")
        done: list[str] = []
        for path in sorted(directory.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            statements = []
            for chunk in path.read_text(encoding="utf-8").split(";"):
                lines = [
                    ln
                    for ln in chunk.splitlines()
                    if ln.strip() and not ln.strip().startswith("--")
                ]
                stmt = "\n".join(lines).strip()
                if stmt:
                    statements.append(stmt)
            with self.transaction() as tx:
                for stmt in statements:
                    tx.execute(stmt)
                tx.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )
            done.append(version)
            log.info("applied migration %s", version)
        return done

    def integrity_check(self) -> bool:
        if self.backend == "sqlite":
            row = self.query_one("PRAGMA integrity_check")
            return bool(row and next(iter(row.values())) == "ok")
        return True  # pragma: no cover - postgres has its own tooling
