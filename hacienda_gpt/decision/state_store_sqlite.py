from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.decision.state_store import CaseStateStore


class SQLiteCaseStateStore(CaseStateStore):
    """SQLite implementation of case-state persistence.

    Storage shape is intentionally JSON-centric to keep migration to Postgres simple:
    - a relational envelope with indexed keys (`case_id`, `user_id`, timestamps)
    - full domain payload serialized as JSON for schema-versioned evolution.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS case_states (
                    case_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    FOREIGN KEY (case_id) REFERENCES case_states(case_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_case_states_user ON case_states(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_case_states_updated ON case_states(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_case_time ON audit_events(case_id, event_time)")

    def get_case(self, case_id: str) -> CaseState | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM case_states WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            return None
        return CaseState.model_validate_json(row["payload_json"])

    def save_case(self, case_state: CaseState) -> None:
        payload_json = case_state.model_dump_json()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO case_states (case_id, user_id, status, schema_version, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    status = excluded.status,
                    schema_version = excluded.schema_version,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (
                    case_state.case_id,
                    case_state.user_id,
                    case_state.status.value,
                    case_state.schema_version,
                    case_state.updated_at.isoformat(),
                    payload_json,
                ),
            )

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
