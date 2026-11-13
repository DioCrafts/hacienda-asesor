"""Users, sessions, consents and case ownership, stored next to the cases.

Same SQLite file as the cases and the notifications, so one backup (and one
erasure) covers everything. The tables are created on first use, not when the
store is built: with ``AUTH_MODE=off`` the database is never touched.

A session token is random, handed to the client once, and only its SHA-256 is
stored — a copy of the database does not let anyone impersonate a user.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
from threading import RLock
from typing import Literal, get_args
import unicodedata
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from hacienda_gpt import settings
from hacienda_gpt.auth.passwords import burn_password_check, check_password_policy, hash_password, verify_password

ConsentType = Literal["tratamiento_datos", "envio_documentos_a_proveedor_llm", "avisos_por_correo"]
CONSENT_TYPES: tuple[ConsentType, ...] = get_args(ConsentType)
# Version of the consent wording the user saw (frontend + docs/privacidad.md).
# Bump it whenever that wording changes; old records keep the version they were given under.
CONSENT_TEXT_VERSION = "2026-11-borrador-1"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_MAX_LENGTH = 254


class InvalidEmailError(ValueError):
    """The e-mail address is not usable. The message is in Spanish, for the user."""


class EmailAlreadyRegisteredError(ValueError):
    pass


def normalize_email(email: str) -> str:
    """Canonical form used for uniqueness and lookup: NFKC, trimmed, lower-cased."""
    normalized = unicodedata.normalize("NFKC", email).strip().casefold()
    if len(normalized) > _EMAIL_MAX_LENGTH or not _EMAIL_RE.fullmatch(normalized):
        raise InvalidEmailError("La dirección de correo no es válida.")
    return normalized


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    email: str
    created_at: datetime


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class ModuleChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modulo: str
    activo: bool
    timestamp: datetime


class ConsentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ConsentType
    granted: bool
    timestamp: datetime
    text_version: str


def _now() -> datetime:
    return datetime.now(UTC)


class AuthStore:
    """SQLite store for accounts (same database file as the cases)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        """Open the connection and create the tables on first use. Callers hold ``self._lock``."""
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS auth_sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        revoked_at TEXT
                    )
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_consents (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        granted INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        text_version TEXT NOT NULL
                    )
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_consents_user ON user_consents(user_id, type, seq)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS case_owners (
                        case_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL
                    )
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_case_owners_user ON case_owners(user_id)")
                # Which household modules the user switched on or off (append-only, like the consents:
                # the history is the record of what the user chose and when).
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_module_choices (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        modulo TEXT NOT NULL,
                        activo INTEGER NOT NULL,
                        timestamp TEXT NOT NULL
                    )
                    """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_user_module_choices_user ON user_module_choices(user_id, modulo, seq)"
                )
            self._conn = conn
        return self._conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._get_conn()
            with conn:  # commit, or roll back on error
                yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._get_conn()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- users ---------------------------------------------------------------

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(id=row["id"], email=row["email"], created_at=datetime.fromisoformat(row["created_at"]))

    def create_user(self, email: str, password: str, *, consents: dict[ConsentType, bool] | None = None) -> User:
        """Create an account and record its initial consents in one transaction.

        Raises ``InvalidEmailError``, ``PasswordPolicyError`` or ``EmailAlreadyRegisteredError``.
        """
        normalized = normalize_email(email)
        check_password_policy(password)
        password_hash = hash_password(password)
        user = User(id=f"user_{uuid4().hex}", email=normalized, created_at=_now())
        with self._write() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (user.id, user.email, password_hash, user.created_at.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise EmailAlreadyRegisteredError(normalized) from exc
            for consent_type, granted in (consents or {}).items():
                self._insert_consent(conn, user.id, consent_type, granted)
        return user

    def get_user(self, user_id: str) -> User | None:
        with self._read() as conn:
            row = conn.execute("SELECT id, email, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user(row) if row else None

    def authenticate(self, email: str, password: str) -> User | None:
        """The user when the e-mail exists and the password matches, else None.

        An unknown e-mail costs the same key derivation as a wrong password, so
        response time does not reveal which addresses have an account.
        """
        try:
            normalized = normalize_email(email)
        except InvalidEmailError:
            normalized = None
        row = None
        if normalized is not None:
            with self._read() as conn:
                row = conn.execute(
                    "SELECT id, email, created_at, password_hash FROM users WHERE email = ?", (normalized,)
                ).fetchone()
        if row is None:
            burn_password_check(password)
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return self._user(row)

    def verify_user_password(self, user_id: str, password: str) -> bool:
        with self._read() as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            burn_password_check(password)
            return False
        return verify_password(password, row["password_hash"])

    def delete_user(self, user_id: str) -> bool:
        """Erase the account, its sessions, its consents and its ownership rows.

        The cases themselves live in other stores; erase those FIRST (see
        ``DELETE /me``) so a failure half-way leaves an account that can retry.
        """
        with self._write() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_consents WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM case_owners WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_module_choices WHERE user_id = ?", (user_id,))
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return cursor.rowcount == 1

    # --- sessions ------------------------------------------------------------

    def create_session(self, user_id: str, *, now: datetime | None = None) -> tuple[str, Session]:
        """Open a session; the returned token is the only copy that will ever exist."""
        created = now or _now()
        token = secrets.token_urlsafe(32)
        session = Session(
            user_id=user_id,
            created_at=created,
            expires_at=created + timedelta(hours=settings.AUTH_SESSION_TTL_HOURS),
        )
        with self._write() as conn:
            # Housekeeping: sessions that expired long ago are of no use to anyone.
            conn.execute(
                "DELETE FROM auth_sessions WHERE expires_at < ?", ((created - timedelta(days=30)).isoformat(),)
            )
            conn.execute(
                "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (hash_token(token), user_id, session.created_at.isoformat(), session.expires_at.isoformat()),
            )
        return token, session

    def get_session_user(self, token: str, *, now: datetime | None = None) -> User | None:
        """The owner of a live (known, not expired, not revoked) session token."""
        if not token:
            return None
        token_hash = hash_token(token)
        with self._read() as conn:
            row = conn.execute(
                "SELECT s.token_hash, s.expires_at, s.revoked_at, u.id, u.email, u.created_at "
                "FROM auth_sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], token_hash):
            return None
        if row["revoked_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= (now or _now()):
            return None
        return self._user(row)

    def revoke_session(self, token: str) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_now().isoformat(), hash_token(token)),
            )
            return cursor.rowcount == 1

    def list_sessions(self, user_id: str) -> list[Session]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT user_id, created_at, expires_at, revoked_at FROM auth_sessions WHERE user_id = ? "
                "ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [
            Session(
                user_id=row["user_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
                revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            )
            for row in rows
        ]

    # --- consents ------------------------------------------------------------

    @staticmethod
    def _insert_consent(conn: sqlite3.Connection, user_id: str, consent_type: ConsentType, granted: bool) -> None:
        if consent_type not in CONSENT_TYPES:
            raise ValueError(f"unknown consent type: {consent_type!r}")
        conn.execute(
            "INSERT INTO user_consents (user_id, type, granted, timestamp, text_version) VALUES (?, ?, ?, ?, ?)",
            (user_id, consent_type, int(granted), _now().isoformat(), CONSENT_TEXT_VERSION),
        )

    @staticmethod
    def _consent(row: sqlite3.Row) -> ConsentRecord:
        return ConsentRecord(
            type=row["type"],
            granted=bool(row["granted"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            text_version=row["text_version"],
        )

    def record_consent(self, user_id: str, consent_type: ConsentType, granted: bool) -> None:
        """Append a consent decision. Records are never updated: the history is the evidence."""
        with self._write() as conn:
            self._insert_consent(conn, user_id, consent_type, granted)

    def consent_history(self, user_id: str) -> list[ConsentRecord]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT type, granted, timestamp, text_version FROM user_consents WHERE user_id = ? ORDER BY seq",
                (user_id,),
            ).fetchall()
        return [self._consent(row) for row in rows]

    def current_consents(self, user_id: str) -> dict[ConsentType, ConsentRecord]:
        """The latest decision per consent type (types never decided are absent)."""
        current: dict[ConsentType, ConsentRecord] = {}
        for record in self.consent_history(user_id):
            current[record.type] = record
        return current

    def has_consent(self, user_id: str, consent_type: ConsentType) -> bool:
        record = self.current_consents(user_id).get(consent_type)
        return record is not None and record.granted

    # --- household modules ---------------------------------------------------

    def record_module_choice(self, user_id: str, modulo: str, activo: bool) -> ModuleChoice:
        """Append the user's decision to switch a module on or off. Never updated: the history is the record."""
        choice = ModuleChoice(modulo=modulo, activo=activo, timestamp=_now())
        with self._write() as conn:
            conn.execute(
                "INSERT INTO user_module_choices (user_id, modulo, activo, timestamp) VALUES (?, ?, ?, ?)",
                (user_id, modulo, int(activo), choice.timestamp.isoformat()),
            )
        return choice

    def module_choice_history(self, user_id: str) -> list[ModuleChoice]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT modulo, activo, timestamp FROM user_module_choices WHERE user_id = ? ORDER BY seq",
                (user_id,),
            ).fetchall()
        return [
            ModuleChoice(
                modulo=row["modulo"], activo=bool(row["activo"]), timestamp=datetime.fromisoformat(row["timestamp"])
            )
            for row in rows
        ]

    def module_choices(self, user_id: str) -> dict[str, ModuleChoice]:
        """The latest decision per module (modules never decided on are absent)."""
        current: dict[str, ModuleChoice] = {}
        for choice in self.module_choice_history(user_id):
            current[choice.modulo] = choice
        return current

    # --- case ownership ------------------------------------------------------

    def set_case_owner(self, case_id: str, user_id: str) -> None:
        with self._write() as conn:
            conn.execute("INSERT INTO case_owners (case_id, user_id) VALUES (?, ?)", (case_id, user_id))

    def owner_of(self, case_id: str) -> str | None:
        """User who owns a case (None for a case created without accounts)."""
        with self._read() as conn:
            row = conn.execute("SELECT user_id FROM case_owners WHERE case_id = ?", (case_id,)).fetchone()
        return str(row["user_id"]) if row is not None else None

    def owns_case(self, case_id: str, user_id: str) -> bool:
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM case_owners WHERE case_id = ? AND user_id = ?", (case_id, user_id)
            ).fetchone()
        return row is not None

    def case_ids_of(self, user_id: str) -> list[str]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT case_id FROM case_owners WHERE user_id = ? ORDER BY rowid", (user_id,)
            ).fetchall()
        return [row["case_id"] for row in rows]
