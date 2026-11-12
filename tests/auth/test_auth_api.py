from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import sqlite3

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt import settings
from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.api.api import app, get_agent, get_document_provider
from hacienda_gpt.auth.deps import CONSENT_DOCUMENTS_DETAIL
from hacienda_gpt.auth.store import CONSENT_TEXT_VERSION
from hacienda_gpt.proactive.entrega import EntregaStore
from hacienda_gpt.proactive.store import Notification
from .conftest import CONSENTS, PASSWORD, Env


def _notify(env: Env, case_id: str, key: str = "k1") -> Notification:
    notification = Notification(case_id=case_id, kind="plazo", key=key, title="Modelo 130", body="…", severity="aviso")
    assert env.notifications.add(notification)
    return notification


def _count(env: Env, table: str) -> int:
    with sqlite3.connect(env.db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --- AUTH_MODE off ------------------------------------------------------------


def test_auth_is_off_by_default_and_changes_nothing() -> None:
    assert settings.AUTH_MODE == "off"
    client = TestClient(app)
    assert client.get("/auth/config").json()["auth_mode"] == "off"
    # Accounts cannot be created while they would protect nothing.
    for method, path in [
        ("post", "/auth/register"),
        ("post", "/auth/login"),
        ("post", "/auth/logout"),
        ("get", "/auth/me"),
        ("get", "/me/export"),
        ("get", "/me/consents"),
        ("put", "/me/consents"),
        ("delete", "/me"),
    ]:
        assert client.request(method, path, json={}).status_code == 404, path


def test_with_auth_off_cases_need_no_session_and_keep_the_client_user_id(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUTH_MODE", "off")
    created = env.client.post("/cases", json={"user_id": "u1", "tax_period": "2025"})
    assert created.status_code == 200 and created.json()["user_id"] == "u1"
    assert env.client.get(f"/cases/{created.json()['case_id']}").status_code == 200
    # Nothing about accounts was written.
    with sqlite3.connect(env.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "users" not in tables and "case_owners" not in tables


def test_unknown_auth_mode_is_refused_at_startup(monkeypatch) -> None:
    import importlib

    monkeypatch.setenv("AUTH_MODE", "requierd")
    with pytest.raises(ValueError, match="AUTH_MODE"):
        importlib.reload(settings)
    monkeypatch.delenv("AUTH_MODE")
    assert importlib.reload(settings).AUTH_MODE == "off"


# --- register / login / logout ------------------------------------------------


def test_config_reports_required(env: Env) -> None:
    body = env.client.get("/auth/config").json()
    assert body == {
        "auth_mode": "required",
        "password_min_length": 10,
        "consent_text_version": CONSENT_TEXT_VERSION,
        "privacy_notice_url": "",
    }


def test_register_login_me_logout(env: Env) -> None:
    registered = env.client.post(
        "/auth/register", json={"email": " Ana@Example.com", "password": PASSWORD, "consents": CONSENTS}
    )
    assert registered.status_code == 201
    body = registered.json()
    assert body["user"]["email"] == "ana@example.com" and set(body) == {"token", "expires_at", "user"}
    assert "password" not in str(body)

    headers = {"Authorization": f"Bearer {body['token']}"}
    assert env.client.get("/auth/me", headers=headers).json() == body["user"]

    login = env.client.post("/auth/login", json={"email": "ANA@example.com", "password": PASSWORD})
    assert login.status_code == 200 and login.json()["token"] != body["token"]
    second = {"Authorization": f"Bearer {login.json()['token']}"}

    assert env.client.post("/auth/logout", headers=headers).status_code == 204
    assert env.client.get("/auth/me", headers=headers).status_code == 401  # revoked
    assert env.client.post("/auth/logout", headers=headers).status_code == 401
    assert env.client.get("/auth/me", headers=second).status_code == 200  # the other session lives on


def test_tokens_are_stored_hashed(env: Env) -> None:
    headers = env.register("ana@example.com")
    token = headers["_token"]
    with sqlite3.connect(env.db_path) as conn:
        stored = [row[0] for row in conn.execute("SELECT token_hash FROM auth_sessions")]
    assert stored == [hashlib.sha256(token.encode()).hexdigest()]
    with open(env.db_path, "rb") as handle:
        raw = handle.read()
    assert token.encode() not in raw and PASSWORD.encode() not in raw


def test_register_validation(env: Env) -> None:
    def register(**changes) -> int:
        payload = {"email": "ana@example.com", "password": PASSWORD, "consents": CONSENTS, **changes}
        return env.client.post("/auth/register", json=payload).status_code

    assert register(password="corta-123") == 422  # 9 characters
    assert register(email="no-es-correo") == 422
    assert register(consents={**CONSENTS, "tratamiento_datos": False}) == 422
    assert register(consents={}) == 422
    assert register(extra="x") == 422
    assert env.auth.authenticate("ana@example.com", PASSWORD) is None  # none of those created an account
    assert register() == 201
    duplicate = env.client.post(
        "/auth/register", json={"email": "ANA@example.com", "password": PASSWORD, "consents": CONSENTS}
    )
    assert duplicate.status_code == 409 and _count(env, "users") == 1


def test_registration_records_the_consents_given(env: Env) -> None:
    headers = env.register("ana@example.com", consents={**CONSENTS, "avisos_por_correo": True})
    consents = {c["type"]: c for c in env.client.get("/me/consents", headers=headers).json()["consents"]}
    assert consents["tratamiento_datos"]["granted"] is True
    assert consents["avisos_por_correo"]["granted"] is True
    assert consents["envio_documentos_a_proveedor_llm"]["granted"] is False
    assert all(c["text_version"] == CONSENT_TEXT_VERSION and c["timestamp"] for c in consents.values())


@pytest.mark.parametrize(
    "authorization", [None, "", "Bearer", "Bearer ", "Bearer nope", "Basic abc", "nope", "Bearer " + "a" * 43]
)
def test_invalid_or_missing_session_is_401(env: Env, authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = env.client.get("/auth/me", headers=headers)
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"
    assert env.client.post("/cases", json={"user_id": "u", "tax_period": "2025"}, headers=headers).status_code == 401


def test_expired_session_is_401(env: Env) -> None:
    user = env.auth.create_user("ana@example.com", PASSWORD)
    token, _ = env.auth.create_session(user.id, now=datetime(2020, 1, 1, tzinfo=UTC))
    assert env.client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_login_failures_do_not_reveal_whether_the_account_exists(env: Env) -> None:
    env.register("ana@example.com")
    wrong = env.client.post("/auth/login", json={"email": "ana@example.com", "password": "otra-clave-larga"})
    unknown = env.client.post("/auth/login", json={"email": "nadie@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_rate_limit_per_email(env: Env) -> None:
    env.register("ana@example.com")
    env.register("luis@example.com")
    bad = {"email": "Ana@example.com", "password": "otra-clave-larga"}
    for _ in range(3):  # the fixture allows 3 failures
        assert env.client.post("/auth/login", json=bad).status_code == 401
    blocked = env.client.post("/auth/login", json=bad)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    # Even the right password is refused while blocked (no oracle for the attacker)...
    assert env.client.post("/auth/login", json={"email": "ana@example.com", "password": PASSWORD}).status_code == 429
    # ...and another account is unaffected.
    assert env.client.post("/auth/login", json={"email": "luis@example.com", "password": PASSWORD}).status_code == 200


def test_successful_login_resets_the_failure_count(env: Env) -> None:
    env.register("ana@example.com")
    for _ in range(4):
        for _ in range(2):
            bad = env.client.post("/auth/login", json={"email": "ana@example.com", "password": "otra-clave-larga"})
            assert bad.status_code == 401
        assert (
            env.client.post("/auth/login", json={"email": "ana@example.com", "password": PASSWORD}).status_code == 200
        )


def test_api_key_still_applies_on_top_of_the_session(env: Env, monkeypatch) -> None:
    headers = env.register("ana@example.com")
    monkeypatch.setenv("HACIENDA_API_KEY", "s3cret")
    payload = {"user_id": "u", "tax_period": "2025"}
    assert env.client.post("/cases", json=payload, headers=headers).status_code == 401
    assert env.client.get("/auth/me", headers=headers).status_code == 401
    with_key = {**headers, "X-API-Key": "s3cret"}
    assert env.client.post("/cases", json=payload, headers=with_key).status_code == 200
    assert env.client.post("/cases", json=payload, headers={"X-API-Key": "s3cret"}).status_code == 401
    assert env.client.get("/auth/config").status_code == 200


# --- isolation between users ---------------------------------------------------


def _case_requests(case_id: str, notification_id: str) -> list[tuple[str, str, dict | None]]:
    """Every endpoint that reaches a case, by path or by body."""
    document = {"filename": "c.txt", "content_base64": base64.b64encode(b"Retenciones: 100,00").decode()}
    return [
        ("get", f"/cases/{case_id}", None),
        ("get", f"/cases/{case_id}/audit", None),
        ("get", f"/cases/{case_id}/audit/export", None),
        ("post", f"/cases/{case_id}/turn", {"user_input": "Soy residente en España"}),
        ("put", f"/cases/{case_id}/facts/edad", {"value": 40}),
        ("delete", f"/cases/{case_id}/facts/edad", None),
        ("get", f"/cases/{case_id}/calendar", None),
        ("post", f"/cases/{case_id}/documents", document),
        ("get", f"/cases/{case_id}/notifications", None),
        ("post", f"/cases/{case_id}/notifications/{notification_id}/read", {}),
        ("post", f"/cases/{case_id}/proactive/run", {}),
        ("post", "/agent", {"query": "¿Qué plazo tengo?", "case_id": case_id}),
        ("post", "/agent/stream", {"query": "¿Qué plazo tengo?", "case_id": case_id}),
        ("post", "/simulate", {"case_id": case_id}),
    ]


class _MustNotRun:
    name, model = "fake", "fake-1"

    def __getattr__(self, name: str):
        raise AssertionError(f"reached the model ({name}) for a request that had to be refused")


@pytest.fixture
def no_model() -> None:
    app.dependency_overrides[get_agent] = _MustNotRun
    app.dependency_overrides[get_document_provider] = _MustNotRun
    yield
    app.dependency_overrides.pop(get_agent, None)
    app.dependency_overrides.pop(get_document_provider, None)


def test_every_case_endpoint_is_covered_by_the_isolation_tests() -> None:
    covered = {path.replace("case_x", "{case_id}") for _, path, _ in _case_requests("case_x", "{notification_id}")}
    covered = {path.replace("/facts/edad", "/facts/{fact_name}") for path in covered}
    declared = {route.path for route in app.routes if "{case_id}" in getattr(route, "path", "")}
    assert declared <= covered, declared - covered


def test_another_users_case_is_404_on_every_endpoint(env: Env, no_model) -> None:
    ana = env.register("ana@example.com", consents={**CONSENTS, "envio_documentos_a_proveedor_llm": True})
    luis = env.register("luis@example.com", consents={**CONSENTS, "envio_documentos_a_proveedor_llm": True})
    case_id = env.new_case(ana)
    notification = _notify(env, case_id)
    before = env.cases.get_case(case_id)
    audit_before = env.cases.list_audit_events(case_id)

    for method, path, body in _case_requests(case_id, notification.id):
        theirs = env.client.request(method, path, json=body, headers=luis)
        assert theirs.status_code == 404, (method, path, theirs.text)
        # Indistinguishable from a case that does not exist.
        missing = env.client.request(method, path.replace(case_id, "case_nope"), json=body, headers=luis)
        if "case_id" in (body or {}):
            missing = env.client.request(method, path, json={**body, "case_id": "case_nope"}, headers=luis)
        assert (missing.status_code, missing.json()) == (theirs.status_code, theirs.json()), (method, path)
        # And without a session nobody gets anywhere.
        assert env.client.request(method, path, json=body).status_code == 401, (method, path)

    # Nothing was read, changed or marked through those attempts.
    assert env.cases.get_case(case_id) == before
    assert env.cases.list_audit_events(case_id) == audit_before
    assert env.notifications.count_unread(case_id) == 1


def test_the_owner_reaches_every_case_endpoint(env: Env) -> None:
    ana = env.register("ana@example.com", consents={**CONSENTS, "envio_documentos_a_proveedor_llm": True})
    case_id = env.new_case(ana)
    notification = _notify(env, case_id)

    class _Down:
        name, model = "fake", "fake-1"

        def run(self, *args, **kwargs):
            raise ProviderError("sin red")

        def complete_json(self, **kwargs):
            raise ProviderError("sin red")

    app.dependency_overrides[get_agent] = _Down
    app.dependency_overrides[get_document_provider] = _Down
    try:
        for method, path, body in _case_requests(case_id, notification.id):
            response = env.client.request(method, path, json=body, headers=ana)
            # 503: the request got past ownership and reached the (unavailable) model.
            expected = 503 if path.endswith(("/documents", "/agent")) else 200
            assert response.status_code == expected, (method, path, response.text)
            if path == "/agent/stream":
                assert '"status": 503' in response.text
    finally:
        app.dependency_overrides.pop(get_agent, None)
        app.dependency_overrides.pop(get_document_provider, None)


def test_a_case_belongs_to_the_session_user_not_to_the_user_id_in_the_body(env: Env) -> None:
    ana = env.register("ana@example.com")
    luis = env.register("luis@example.com")
    created = env.client.post("/cases", json={"user_id": luis["_user_id"], "tax_period": "2025"}, headers=ana)
    assert created.json()["user_id"] == ana["_user_id"]
    case_id = created.json()["case_id"]
    assert env.client.get(f"/cases/{case_id}", headers=ana).status_code == 200
    assert env.client.get(f"/cases/{case_id}", headers=luis).status_code == 404


def test_cases_created_before_accounts_existed_are_not_reachable(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUTH_MODE", "off")
    legacy = env.client.post("/cases", json={"user_id": "web-1", "tax_period": "2025"}).json()["case_id"]
    monkeypatch.setattr(settings, "AUTH_MODE", "required")
    ana = env.register("ana@example.com")
    assert env.client.get(f"/cases/{legacy}", headers=ana).status_code == 404
    assert env.cases.get_case(legacy) is not None  # still there for the operator / batch jobs


def test_endpoints_without_a_case_still_need_a_session(env: Env, no_model) -> None:
    assert env.client.post("/agent", json={"query": "hola"}).status_code == 401
    assert env.client.post("/agent/stream", json={"query": "hola"}).status_code == 401
    assert env.client.post("/simulate", json={"datos": {}}).status_code == 401
    assert env.client.post("/qa", json={"query": "hola"}).status_code == 401
    # Liveness and metrics stay open.
    assert env.client.get("/health").status_code == 200
    assert env.client.get("/metrics").status_code == 200


def test_proactive_jobs_run_without_a_session(env: Env) -> None:
    from hacienda_gpt.proactive.jobs import ejecutar_para_caso, hoy_en_espana

    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    [case] = env.cases.list_all_cases()
    assert case.case_id == case_id
    assert isinstance(ejecutar_para_caso(case, hoy=hoy_en_espana(), store=env.notifications), dict)


# --- consent gating of documents ----------------------------------------------


class _DocProvider:
    name, model = "fake", "fake-1"

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def complete_json(self, **kwargs):
        self.requests.append(kwargs)
        return {"tipo_documento": "Certificado", "hechos": []}


def test_documents_need_consent_before_anything_reaches_the_provider(env: Env) -> None:
    ana = env.register("ana@example.com")  # consent not given
    case_id = env.new_case(ana)
    provider = _DocProvider()
    app.dependency_overrides[get_document_provider] = lambda: provider
    payload = {"filename": "c.txt", "content_base64": base64.b64encode(b"Retenciones practicadas: 100,00").decode()}
    try:
        refused = env.client.post(f"/cases/{case_id}/documents", json=payload, headers=ana)
        assert refused.status_code == 403 and refused.json()["detail"] == CONSENT_DOCUMENTS_DETAIL
        assert "consentimiento" in refused.json()["detail"]
        assert provider.requests == []
        assert all(e["event_type"] != "document_analyzed" for e in env.cases.list_audit_events(case_id))

        granted = env.client.put("/me/consents", json={"envio_documentos_a_proveedor_llm": True}, headers=ana)
        assert granted.status_code == 200
        assert env.client.post(f"/cases/{case_id}/documents", json=payload, headers=ana).status_code == 200
        assert len(provider.requests) == 1

        # Withdrawing it closes the door again.
        env.client.put("/me/consents", json={"envio_documentos_a_proveedor_llm": False}, headers=ana)
        assert env.client.post(f"/cases/{case_id}/documents", json=payload, headers=ana).status_code == 403
        assert len(provider.requests) == 1
    finally:
        app.dependency_overrides.pop(get_document_provider, None)


def test_documents_are_not_gated_when_auth_is_off(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUTH_MODE", "off")
    case_id = env.client.post("/cases", json={"user_id": "u1", "tax_period": "2025"}).json()["case_id"]
    provider = _DocProvider()
    app.dependency_overrides[get_document_provider] = lambda: provider
    payload = {"filename": "c.txt", "content_base64": base64.b64encode(b"Retenciones practicadas: 100,00").decode()}
    try:
        assert env.client.post(f"/cases/{case_id}/documents", json=payload).status_code == 200
    finally:
        app.dependency_overrides.pop(get_document_provider, None)


# --- consents -------------------------------------------------------------------


def test_consents_can_be_toggled_and_keep_their_history(env: Env) -> None:
    ana = env.register("ana@example.com")

    def state() -> dict[str, bool]:
        body = env.client.get("/me/consents", headers=ana).json()
        assert body["text_version"] == CONSENT_TEXT_VERSION
        return {c["type"]: c["granted"] for c in body["consents"]}

    assert state() == CONSENTS
    updated = env.client.put("/me/consents", json={"avisos_por_correo": True}, headers=ana)
    assert updated.status_code == 200
    assert state() == {**CONSENTS, "avisos_por_correo": True}
    # Repeating the same choice does not add records.
    env.client.put("/me/consents", json={"avisos_por_correo": True}, headers=ana)
    env.client.put("/me/consents", json={"avisos_por_correo": False}, headers=ana)
    history = [(c.type, c.granted) for c in env.auth.consent_history(ana["_user_id"])]
    assert history[-2:] == [("avisos_por_correo", True), ("avisos_por_correo", False)] and len(history) == 5

    # The consent the account depends on can only be withdrawn by deleting the account.
    withdrawn = env.client.put("/me/consents", json={"tratamiento_datos": False}, headers=ana)
    assert withdrawn.status_code == 409 and "elimina tu cuenta" in withdrawn.json()["detail"]
    assert state()["tratamiento_datos"] is True
    assert env.client.put("/me/consents", json={"otro": True}, headers=ana).status_code == 422
    assert env.client.get("/me/consents").status_code == 401


def test_consents_are_per_user(env: Env) -> None:
    ana = env.register("ana@example.com")
    luis = env.register("luis@example.com")
    env.client.put("/me/consents", json={"envio_documentos_a_proveedor_llm": True}, headers=ana)
    theirs = {c["type"]: c["granted"] for c in env.client.get("/me/consents", headers=luis).json()["consents"]}
    assert theirs["envio_documentos_a_proveedor_llm"] is False


# --- export -----------------------------------------------------------------------


def _populate(env: Env, headers: dict, *, age: int) -> tuple[str, Notification]:
    case_id = env.new_case(headers)
    saved = env.client.put(f"/cases/{case_id}/facts/edad", json={"value": age}, headers=headers)
    assert saved.status_code == 200, saved.text
    notification = _notify(env, case_id)
    deliveries = EntregaStore(env.db_path)
    try:
        assert deliveries.registrar(notification.id, "email", ahora=datetime.now(UTC))
    finally:
        deliveries.close()
    return case_id, notification


def test_export_contains_everything_about_the_user_and_nothing_about_others(env: Env) -> None:
    ana = env.register("ana@example.com", consents={**CONSENTS, "avisos_por_correo": True})
    luis = env.register("luis@example.com")
    case_id, notification = _populate(env, ana, age=41)
    other_case, other_notification = _populate(env, luis, age=77)

    response = env.client.get("/me/export", headers=ana)
    assert response.status_code == 200
    export = response.json()

    assert export["profile"] == {
        "id": ana["_user_id"],
        "email": "ana@example.com",
        "created_at": export["profile"]["created_at"],
    }
    assert {(c["type"], c["granted"], c["text_version"]) for c in export["consents"]} == {
        ("tratamiento_datos", True, CONSENT_TEXT_VERSION),
        ("envio_documentos_a_proveedor_llm", False, CONSENT_TEXT_VERSION),
        ("avisos_por_correo", True, CONSENT_TEXT_VERSION),
    }
    assert all(c["timestamp"] for c in export["consents"])
    assert len(export["sessions"]) == 1 and set(export["sessions"][0]) == {"created_at", "expires_at", "revoked_at"}

    [exported] = export["cases"]
    assert exported["case"]["case_id"] == case_id and exported["case"]["user_id"] == ana["_user_id"]
    assert [(f["name"], f["value"]) for f in exported["case"]["facts"]] == [("edad", 41)]
    assert [e["event_type"] for e in exported["audit_events"]] == ["case_created", "fact_edited_by_user"]
    assert [n["id"] for n in exported["notifications"]] == [notification.id]
    assert [(d["notification_id"], d["canal"]) for d in exported["notification_deliveries"]] == [
        (notification.id, "email")
    ]

    raw = response.text
    assert other_case not in raw and other_notification.id not in raw
    assert luis["_user_id"] not in raw and "luis@example.com" not in raw
    # No credentials in the export.
    assert ana["_token"] not in raw and hashlib.sha256(ana["_token"].encode()).hexdigest() not in raw
    assert "password" not in raw and "scrypt" not in raw
    assert env.client.get("/me/export").status_code == 401


# --- erasure ----------------------------------------------------------------------


def test_delete_account_needs_the_password_again(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    assert env.client.request("DELETE", "/me", json={"password": "otra-clave-larga"}, headers=ana).status_code == 403
    assert env.client.request("DELETE", "/me", json={}, headers=ana).status_code == 422
    assert env.client.request("DELETE", "/me", headers=ana).status_code == 422
    assert env.client.request("DELETE", "/me", json={"password": PASSWORD}).status_code == 401
    assert env.client.get(f"/cases/{case_id}", headers=ana).status_code == 200  # nothing was erased

    # Guessing the password through this endpoint is rate limited too.
    for _ in range(2):
        env.client.request("DELETE", "/me", json={"password": "otra-clave-larga"}, headers=ana)
    assert env.client.request("DELETE", "/me", json={"password": PASSWORD}, headers=ana).status_code == 429
    assert env.auth.get_user(ana["_user_id"]) is not None


def test_delete_account_erases_everything_and_only_that(env: Env) -> None:
    ana = env.register("ana@example.com", consents={**CONSENTS, "avisos_por_correo": True})
    luis = env.register("luis@example.com")
    first, _ = _populate(env, ana, age=41)
    second, _ = _populate(env, ana, age=42)
    other_case, other_notification = _populate(env, luis, age=77)
    second_session = env.client.post("/auth/login", json={"email": "ana@example.com", "password": PASSWORD}).json()

    assert env.client.request("DELETE", "/me", json={"password": PASSWORD}, headers=ana).status_code == 204

    # Every session is dead, the account is gone and the address is free again.
    assert env.client.get("/auth/me", headers=ana).status_code == 401
    other_headers = {"Authorization": f"Bearer {second_session['token']}"}
    assert env.client.get("/auth/me", headers=other_headers).status_code == 401
    assert env.client.post("/auth/login", json={"email": "ana@example.com", "password": PASSWORD}).status_code == 401

    # Nothing of the user is left in ANY table of the database file.
    with sqlite3.connect(env.db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        dump = "\n".join(
            str(row) for table in tables if table != "sqlite_sequence" for row in conn.execute(f"SELECT * FROM {table}")
        )
    assert {
        "users",
        "auth_sessions",
        "user_consents",
        "case_owners",
        "case_states",
        "audit_events",
        "notifications",
        "notification_deliveries",
    } <= set(tables)
    for needle in (ana["_user_id"], "ana@example.com", first, second):
        assert needle not in dump, needle
    assert env.cases.get_case(first) is None and env.cases.get_case(second) is None
    assert env.cases.list_audit_events(first) == [] and env.notifications.list(first) == []
    assert env.notifications.deliveries_for_case(first) == []

    # The other user keeps everything.
    assert env.client.get(f"/cases/{other_case}", headers=luis).status_code == 200
    assert [n.id for n in env.notifications.list(other_case)] == [other_notification.id]
    assert len(env.notifications.deliveries_for_case(other_case)) == 1
    assert len(env.cases.list_audit_events(other_case)) == 2
    assert _count(env, "users") == 1 and _count(env, "case_states") == 1 and _count(env, "notifications") == 1

    # A new account on the same address starts empty.
    again = env.register("ana@example.com")
    assert again["_user_id"] != ana["_user_id"]
    assert env.client.get("/me/export", headers=again).json()["cases"] == []
    assert env.client.get(f"/cases/{first}", headers=again).status_code == 404
