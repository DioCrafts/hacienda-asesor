from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import sqlite3

import pytest

from hacienda_gpt import settings
from hacienda_gpt.auth.passwords import PasswordPolicyError, hash_password, verify_password
from hacienda_gpt.auth.ratelimit import LoginRateLimiter
from hacienda_gpt.auth.store import (
    CONSENT_TEXT_VERSION,
    AuthStore,
    EmailAlreadyRegisteredError,
    InvalidEmailError,
    normalize_email,
)

PASSWORD = "una-clave-larga-1"


@pytest.fixture
def store(tmp_path) -> AuthStore:
    return AuthStore(str(tmp_path / "auth.sqlite3"))


def _rows(store: AuthStore, query: str) -> list[tuple]:
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(query).fetchall()


def test_password_hash_is_salted_scrypt_and_verifies() -> None:
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second  # per-password salt
    assert first.startswith(f"scrypt${settings.AUTH_SCRYPT_N}$8$1$") and PASSWORD not in first
    assert verify_password(PASSWORD, first) and verify_password(PASSWORD, second)
    assert not verify_password(PASSWORD + "x", first)


@pytest.mark.parametrize(
    "stored", ["", "plain", "scrypt$256$8$1$!!$!!", "md5$256$8$1$AAAA$AAAA", "scrypt$3$8$1$AAAA$AAAA"]
)
def test_verify_rejects_malformed_hashes(stored: str) -> None:
    assert verify_password(PASSWORD, stored) is False


def test_hash_keeps_verifying_after_the_work_factor_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = hash_password(PASSWORD)
    monkeypatch.setattr(settings, "AUTH_SCRYPT_N", 2**9)
    assert verify_password(PASSWORD, stored)


def test_store_does_not_touch_the_database_until_used(tmp_path) -> None:
    path = tmp_path / "sub" / "auth.sqlite3"
    AuthStore(str(path))
    assert not path.exists() and not path.parent.exists()


def test_email_is_normalised_and_unique(store: AuthStore) -> None:
    assert normalize_email("  Ana.García@Example.COM ") == "ana.garcía@example.com"
    user = store.create_user("  Ana@Example.COM ", PASSWORD)
    assert user.email == "ana@example.com" and user.id.startswith("user_")
    with pytest.raises(EmailAlreadyRegisteredError):
        store.create_user("ANA@example.com", PASSWORD)
    for bad in ["", "sin-arroba", "a@b", "a b@c.es", "a@@b.es", "x" * 250 + "@b.es"]:
        with pytest.raises(InvalidEmailError):
            store.create_user(bad, PASSWORD)


def test_password_policy_minimum_length(store: AuthStore) -> None:
    with pytest.raises(PasswordPolicyError):
        store.create_user("ana@example.com", "123456789")  # 9 characters
    assert store.create_user("ana@example.com", "1234567890").email == "ana@example.com"
    assert _rows(store, "SELECT COUNT(*) FROM users") == [(1,)]


def test_authenticate(store: AuthStore) -> None:
    user = store.create_user("ana@example.com", PASSWORD)
    assert store.authenticate("ANA@example.com ", PASSWORD) == user
    assert store.authenticate("ana@example.com", "otra-clave-larga") is None
    assert store.authenticate("nadie@example.com", PASSWORD) is None
    assert store.authenticate("no es un correo", PASSWORD) is None
    assert store.verify_user_password(user.id, PASSWORD)
    assert not store.verify_user_password(user.id, "otra-clave-larga")
    assert not store.verify_user_password("user_nope", PASSWORD)


def test_password_is_never_stored_in_clear(store: AuthStore) -> None:
    store.create_user("ana@example.com", PASSWORD)
    [(stored,)] = _rows(store, "SELECT password_hash FROM users")
    assert stored.startswith("scrypt$") and PASSWORD not in stored


def test_session_tokens_are_stored_only_as_sha256(store: AuthStore) -> None:
    user = store.create_user("ana@example.com", PASSWORD)
    token, session = store.create_session(user.id)
    assert len(token) >= 43  # 32 random bytes, url-safe base64
    [(token_hash,)] = _rows(store, "SELECT token_hash FROM auth_sessions")
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    with open(store.db_path, "rb") as handle:
        assert token.encode() not in handle.read()
    assert store.get_session_user(token) == user
    # The stored hash is not itself a credential.
    assert store.get_session_user(token_hash) is None
    assert store.get_session_user("") is None and store.get_session_user("x" * 43) is None
    assert session.expires_at - session.created_at == timedelta(hours=settings.AUTH_SESSION_TTL_HOURS)


def test_session_expiry_and_revocation(store: AuthStore) -> None:
    user = store.create_user("ana@example.com", PASSWORD)
    token, session = store.create_session(user.id)
    assert store.get_session_user(token, now=session.expires_at - timedelta(seconds=1)) == user
    assert store.get_session_user(token, now=session.expires_at) is None

    other, _ = store.create_session(user.id)
    assert store.revoke_session(token) is True
    assert store.revoke_session(token) is False
    assert store.get_session_user(token) is None
    assert store.get_session_user(other) == user  # revoking one session leaves the others alone
    assert [s.revoked_at is not None for s in store.list_sessions(user.id)] == [True, False]


def test_consents_are_append_only_records(store: AuthStore) -> None:
    user = store.create_user(
        "ana@example.com", PASSWORD, consents={"tratamiento_datos": True, "avisos_por_correo": False}
    )
    assert store.has_consent(user.id, "tratamiento_datos")
    assert not store.has_consent(user.id, "avisos_por_correo")
    assert not store.has_consent(user.id, "envio_documentos_a_proveedor_llm")  # never decided

    store.record_consent(user.id, "envio_documentos_a_proveedor_llm", True)
    store.record_consent(user.id, "envio_documentos_a_proveedor_llm", False)
    history = store.consent_history(user.id)
    assert [(c.type, c.granted) for c in history] == [
        ("tratamiento_datos", True),
        ("avisos_por_correo", False),
        ("envio_documentos_a_proveedor_llm", True),
        ("envio_documentos_a_proveedor_llm", False),
    ]
    assert all(c.text_version == CONSENT_TEXT_VERSION and c.timestamp.tzinfo is not None for c in history)
    assert not store.has_consent(user.id, "envio_documentos_a_proveedor_llm")
    with pytest.raises(ValueError):
        store.record_consent(user.id, "otro", True)  # type: ignore[arg-type]


def test_case_ownership_and_user_deletion(store: AuthStore) -> None:
    ana = store.create_user("ana@example.com", PASSWORD, consents={"tratamiento_datos": True})
    luis = store.create_user("luis@example.com", PASSWORD, consents={"tratamiento_datos": True})
    store.set_case_owner("case_a", ana.id)
    store.set_case_owner("case_l", luis.id)
    with pytest.raises(sqlite3.IntegrityError):
        store.set_case_owner("case_a", luis.id)  # a case has exactly one owner
    assert store.owns_case("case_a", ana.id) and not store.owns_case("case_a", luis.id)
    assert not store.owns_case("case_unknown", ana.id)
    assert store.case_ids_of(ana.id) == ["case_a"]

    token, _ = store.create_session(ana.id)
    assert store.delete_user(ana.id) is True
    assert store.delete_user(ana.id) is False
    assert store.get_user(ana.id) is None and store.get_session_user(token) is None
    assert store.authenticate("ana@example.com", PASSWORD) is None
    for table in ("users", "auth_sessions", "user_consents", "case_owners"):
        column = "id" if table == "users" else "user_id"
        assert _rows(store, f"SELECT COUNT(*) FROM {table} WHERE {column} = '{ana.id}'") == [(0,)]
    # The other account is untouched.
    assert store.owns_case("case_l", luis.id) and store.has_consent(luis.id, "tratamiento_datos")


def test_login_rate_limiter_window() -> None:
    now = [1000.0]
    limiter = LoginRateLimiter(3, 60, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.retry_after("k") == 0
        limiter.record_failure("k")
    assert limiter.retry_after("k") > 0
    assert limiter.retry_after("other") == 0  # keys are independent
    now[0] += 61
    assert limiter.retry_after("k") == 0
    limiter.record_failure("k")
    limiter.reset("k")
    assert limiter.retry_after("k") == 0
    assert LoginRateLimiter(0, 60).retry_after("k") == 0  # 0 disables it


def test_expired_session_timestamps_are_utc(store: AuthStore) -> None:
    user = store.create_user("ana@example.com", PASSWORD)
    _, session = store.create_session(user.id, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert session.expires_at.tzinfo is not None
