"""Notifications written by the proactive jobs, stored next to the cases.

The jobs run daily and are idempotent by construction: every notification
carries a ``key`` that is unique per case, and :meth:`NotificationStore.add`
refuses a second write for the same ``(case_id, key)``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["plazo", "fin_de_ejercicio", "cambio_normativo"]
Severity = Literal["info", "aviso", "urgente"]


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    case_id: str = Field(min_length=1)
    kind: Kind
    # Dedupe key, unique per case.
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    # Plain text / light markdown, in Spanish.
    body: str
    severity: Severity
    # Concrete next steps, in Spanish.
    actions: list[str] = Field(default_factory=list)
    # Legal references / BOE ids.
    sources: list[str] = Field(default_factory=list)
    # Estimated saving in euros when the notification is about money.
    amount: Decimal | None = None
    due_date: date | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    read_at: datetime | None = None


# ``list`` is also a method of the store below, so the builtin needs another name inside the class.
_DeliveryRows = list[dict[str, str]]


class NotificationStore:
    """SQLite store for notifications (same database file as the cases)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    case_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    payload_json TEXT NOT NULL,
                    UNIQUE (case_id, key)
                )
                """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_case ON notifications(case_id, seq)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(self, notification: Notification) -> bool:
        """Store ``notification``; False (and no write) when its ``(case_id, key)`` already exists."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO notifications (id, case_id, key, created_at, read_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    notification.id,
                    notification.case_id,
                    notification.key,
                    notification.created_at.isoformat(),
                    notification.read_at.isoformat() if notification.read_at else None,
                    notification.model_dump_json(exclude={"read_at"}),
                ),
            )
            return cursor.rowcount == 1

    def list(self, case_id: str, *, unread_only: bool = False) -> list[Notification]:
        """Notifications of a case, newest first."""
        query = "SELECT payload_json, read_at FROM notifications WHERE case_id = ?"
        if unread_only:
            query += " AND read_at IS NULL"
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY created_at DESC, seq DESC", (case_id,)).fetchall()
        out: list[Notification] = []
        for row in rows:
            notification = Notification.model_validate_json(row["payload_json"])
            # The column is the source of truth for the read state.
            notification.read_at = datetime.fromisoformat(row["read_at"]) if row["read_at"] else None
            out.append(notification)
        return out

    def mark_read(self, case_id: str, notification_id: str) -> bool:
        """Mark as read; False when the notification does not exist for that case.

        Idempotent: an already-read notification keeps its original ``read_at``.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE case_id = ? AND id = ?",
                (datetime.now(UTC).isoformat(), case_id, notification_id),
            )
            return cursor.rowcount == 1

    def count_unread(self, case_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE case_id = ? AND read_at IS NULL", (case_id,)
            ).fetchone()
        return int(row["n"])

    def remove_superseded(self, case_id: str, *, key_prefix: str, keep_key: str) -> int:
        """Delete the notifications of a case whose key starts with ``key_prefix``, except ``keep_key``.

        Used when a notification replaces an earlier one about the same subject
        (a year-end plan recomputed after the profile changed), so the user is
        never left reading figures that no longer match their data.
        """
        escaped = key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM notifications WHERE case_id = ? AND key LIKE ? ESCAPE '\\' AND key != ?",
                (case_id, escaped + "%", keep_key),
            )
            return cursor.rowcount

    def _has_deliveries_table(self) -> bool:
        # ``notification_deliveries`` belongs to proactive/entrega.py and only
        # exists once delivery outside the app has been used on this database.
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'notification_deliveries'"
        ).fetchone()
        return row is not None

    def deliveries_for_case(self, case_id: str) -> _DeliveryRows:
        """Delivery records (notification, channel, time) of a case's notifications, oldest first."""
        with self._lock:
            if not self._has_deliveries_table():
                return []
            rows = self._conn.execute(
                "SELECT d.notification_id, d.canal, d.delivered_at FROM notification_deliveries d "
                "JOIN notifications n ON n.id = d.notification_id WHERE n.case_id = ? ORDER BY d.delivered_at",
                (case_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_for_case(self, case_id: str) -> int:
        """Erase every notification of a case and its delivery records; returns the notifications removed."""
        with self._lock, self._conn:
            if self._has_deliveries_table():
                self._conn.execute(
                    "DELETE FROM notification_deliveries WHERE notification_id IN "
                    "(SELECT id FROM notifications WHERE case_id = ?)",
                    (case_id,),
                )
            cursor = self._conn.execute("DELETE FROM notifications WHERE case_id = ?", (case_id,))
            return cursor.rowcount
