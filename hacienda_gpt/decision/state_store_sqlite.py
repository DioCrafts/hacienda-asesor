from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.decision.state_store import CaseStateStore, CaseVersionConflictError


class SQLiteCaseStateStore(CaseStateStore):
    """SQLite implementation of case-state persistence.

    Storage shape is intentionally JSON-centric to keep migration to Postgres simple:
    - a relational envelope with indexed keys (`case_id`, `user_id`, timestamps)
    - full domain payload serialized as JSON for schema-versioned evolution.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None
        self._initialize()

    def _get_conn(self) -> sqlite3.Connection:
        """Return the reusable connection, opening it once on first use.

        Callers must hold ``self._lock``. The connection is shared across
        operations (``check_same_thread=False`` plus the lock serialize all
        access), so the WAL / foreign-keys PRAGMAs are applied a single time
        instead of on every query.
        """
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._conn = conn
        return self._conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            # Roll back so a failed statement never leaves the reused
            # connection stuck mid-transaction for the next caller.
            conn.rollback()
            raise

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS case_states (
                    case_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """)
            # Migration for databases created before optimistic-concurrency
            # tracking: add the column if it's missing. SQLite has no
            # "ADD COLUMN IF NOT EXISTS", so probe the schema first.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(case_states)")}
            if "version" not in cols:
                conn.execute("ALTER TABLE case_states ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    FOREIGN KEY (case_id) REFERENCES case_states(case_id) ON DELETE CASCADE
                )
                """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_case_states_user ON case_states(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_case_states_updated ON case_states(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_case_time ON audit_events(case_id, event_time)")

    def get_case(self, case_id: str) -> CaseState | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM case_states WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            return None
        return CaseState.model_validate_json(row["payload_json"])

    def save_case(self, case_state: CaseState) -> CaseState:
        """Persist with optimistic concurrency; return the stored state.

        Creation (no existing row) keeps the supplied ``version`` as-is. An
        update requires the persisted ``version`` to still equal the in-memory
        one — if another turn advanced it in between we raise
        :class:`CaseVersionConflictError` rather than overwrite the newer state. The
        SELECT + write run under ``self._lock`` on the single shared connection,
        so the check-then-write is atomic against other store callers.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT version FROM case_states WHERE case_id = ?", (case_state.case_id,)).fetchone()

            if row is None:
                stored = case_state
                conn.execute(
                    """
                    INSERT INTO case_states
                        (case_id, user_id, status, schema_version, version, updated_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored.case_id,
                        stored.user_id,
                        stored.status.value,
                        stored.schema_version,
                        stored.version,
                        stored.updated_at.isoformat(),
                        stored.model_dump_json(),
                    ),
                )
                return stored

            current_version = int(row["version"])
            if current_version != case_state.version:
                raise CaseVersionConflictError(case_state.case_id, expected=case_state.version, found=current_version)

            stored = case_state.model_copy(update={"version": current_version + 1})
            cursor = conn.execute(
                """
                UPDATE case_states SET
                    user_id = ?,
                    status = ?,
                    schema_version = ?,
                    version = ?,
                    updated_at = ?,
                    payload_json = ?
                WHERE case_id = ? AND version = ?
                """,
                (
                    stored.user_id,
                    stored.status.value,
                    stored.schema_version,
                    stored.version,
                    stored.updated_at.isoformat(),
                    stored.model_dump_json(),
                    stored.case_id,
                    current_version,
                ),
            )
            if cursor.rowcount == 0:
                # Lost the race between the SELECT and the UPDATE.
                raise CaseVersionConflictError(case_state.case_id, expected=case_state.version, found=current_version)
            return stored

    def list_cases(self, user_id: str) -> list[CaseState]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM case_states WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
            ).fetchall()
        return [CaseState.model_validate_json(row["payload_json"]) for row in rows]

    def append_audit_event(self, case_id: str, event: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        event_payload = {"event_time": now, **event}
        with self._lock, self._connect() as conn:
            case_exists = conn.execute("SELECT 1 FROM case_states WHERE case_id = ?", (case_id,)).fetchone()
            if case_exists is None:
                raise KeyError(f"Unknown case_id: {case_id}")
            conn.execute(
                "INSERT INTO audit_events (case_id, event_time, event_json) VALUES (?, ?, ?)",
                (case_id, now, json.dumps(event_payload, ensure_ascii=False)),
            )

    def list_audit_events(self, case_id: str) -> list[dict[str, Any]]:
        """Testing/diagnostic helper. Not part of the abstract store contract."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT event_json FROM audit_events WHERE case_id = ? ORDER BY id ASC", (case_id,)
            ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]
