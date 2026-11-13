"""API of the household modules: list, per-user switch, module facts, pending actions, privacy."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import sqlite3

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt import settings
from hacienda_gpt.agent.agent import TaxAgent
from hacienda_gpt.api.api import app, get_agent, get_case_store, get_notification_store
from hacienda_gpt.api.routers.auth import get_login_limiter
from hacienda_gpt.auth.deps import get_auth_store
from hacienda_gpt.auth.ratelimit import LoginRateLimiter
from hacienda_gpt.auth.store import AuthStore
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.store import Notification, NotificationStore
from tests.agent.fakes import FakeSearcher, ScriptedProvider, corpus, text_turn, tool_turn, verdict
from .fakes import Llamadas, agua, aviso, instalar, seguros

PASSWORD = "una-clave-larga-1"
CONSENTS = {"tratamiento_datos": True, "envio_documentos_a_proveedor_llm": False, "avisos_por_correo": False}


@dataclass
class Env:
    client: TestClient
    db_path: str
    auth: AuthStore
    cases: SQLiteCaseStateStore
    notifications: NotificationStore
    llamadas: Llamadas

    def register(self, email: str) -> dict:
        response = self.client.post("/auth/register", json={"email": email, "password": PASSWORD, "consents": CONSENTS})
        assert response.status_code == 201, response.text
        body = response.json()
        return {"Authorization": f"Bearer {body['token']}", "_user_id": body["user"]["id"]}

    def new_case(self, headers: dict) -> str:
        response = self.client.post("/cases", json={"user_id": "x", "tax_period": "2026"}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["case_id"]


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[Env]:
    monkeypatch.setattr(settings, "AUTH_MODE", "required")
    monkeypatch.setattr(settings, "AUTH_SCRYPT_N", 2**8)
    monkeypatch.delenv("HACIENDA_API_KEY", raising=False)
    db_path = str(tmp_path / "state.sqlite3")
    llamadas = Llamadas()
    modulo_agua = agua(llamadas, avisos_fijos=[aviso("factura", titulo="Factura del agua")])

    def anota(argumentos, contexto) -> str:
        contexto.registrar({"agua_personas": argumentos.get("x", 1)})
        return "Anotado."

    modulo_agua.herramientas = (*modulo_agua.herramientas, llamadas.herramienta("agua_anotar", contenido=anota))

    def leer_documento(provider, *, filename: str, data: bytes) -> dict:
        llamadas.registro.append(("leer_documento", {"filename": filename, "data": data.decode()}, None))
        return {"campos": [{"campo": "x", "valor": "3", "evidencia": "personas: 3"}], "avisos": []}

    modulo_agua.leer_documento = leer_documento  # type: ignore[attr-defined]
    modulo_agua.herramienta_de_documento = "agua_anotar"  # type: ignore[attr-defined]
    instalar(modulo_agua, seguros(llamadas))
    environment = Env(
        client=TestClient(app),
        db_path=db_path,
        auth=AuthStore(db_path),
        cases=SQLiteCaseStateStore(db_path),
        notifications=NotificationStore(db_path),
        llamadas=llamadas,
    )
    overrides = {
        get_auth_store: lambda: environment.auth,
        get_case_store: lambda: environment.cases,
        get_notification_store: lambda: environment.notifications,
        get_login_limiter: lambda: LoginRateLimiter(50, 900),
    }
    app.dependency_overrides.update(overrides)
    try:
        yield environment
    finally:
        for dependency in [*overrides, get_agent]:
            app.dependency_overrides.pop(dependency, None)


def _de_modulos(env: Env, case_id: str) -> list[str]:
    return [n.key for n in env.notifications.list(case_id) if n.kind.startswith("modulo:")]


def _modulos(env: Env, headers: dict) -> dict:
    response = env.client.get("/modulos", headers=headers)
    assert response.status_code == 200, response.text
    return {m["clave"]: m for m in response.json()["modulos"]}


def test_modules_are_listed_off_by_default_and_switched_per_user(env: Env) -> None:
    ana, luis = env.register("ana@example.com"), env.register("luis@example.com")
    assert env.client.get("/modulos").status_code == 401
    modulos = _modulos(env, ana)
    assert list(modulos) == ["fiscal", "agua", "seguros"]
    assert modulos["fiscal"]["activo"] and modulos["fiscal"]["obligatorio"]
    assert not modulos["agua"]["activo"] and not modulos["agua"]["elegido"]
    hechos = {h["nombre"]: h for h in modulos["agua"]["hechos"]}
    assert hechos["agua_lecturas"]["tipo"] == "json" and modulos["seguros"]["hechos"][0]["sensible"] is True
    assert {h["nombre"]: h["efecto"] for h in modulos["agua"]["herramientas"]}["agua_cambiar_tarifa"] == "actua"

    response = env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    assert response.status_code == 200
    assert {m["clave"]: m["activo"] for m in response.json()["modulos"]}["agua"] is True
    assert _modulos(env, luis)["agua"]["activo"] is False  # per user
    assert env.client.put("/me/modulos/fiscal", json={"activo": False}, headers=ana).status_code == 409
    assert env.client.put("/me/modulos/nada", json={"activo": True}, headers=ana).status_code == 404
    assert (
        env.client.put("/me/modulos/agua", json={"activo": True, "borrar_datos": True}, headers=ana).status_code == 422
    )
    assert [(c.modulo, c.activo) for c in env.auth.module_choice_history(ana["_user_id"])] == [("agua", True)]


def test_operator_can_switch_a_module_off_for_everyone(env: Env, monkeypatch) -> None:
    ana = env.register("ana@example.com")
    monkeypatch.setattr(settings, "MODULOS_DESACTIVADOS", "seguros")
    assert list(_modulos(env, ana)) == ["fiscal", "agua"]
    assert env.client.put("/me/modulos/seguros", json={"activo": True}, headers=ana).status_code == 404


def test_module_facts_are_validated_and_only_for_active_modules(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    put = lambda name, value: env.client.put(  # noqa: E731
        f"/cases/{case_id}/facts/{name}", json={"value": value}, headers=ana
    )
    assert put("agua_personas", 3).status_code == 409  # module off
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    assert put("agua_personas", 3).status_code == 200
    bad = put("agua_lecturas", '[{"m3": 1}')
    assert bad.status_code == 422 and "JSON válido" in bad.json()["detail"]
    invalid = put("agua_lecturas", '{"m3": 1}')
    assert (
        invalid.status_code == 422
        and invalid.json()["detail"] == "Las lecturas deben ser una lista de objetos con «m3»."
    )
    ok = put("agua_lecturas", '[{"mes": "2026-02", "m3": 9}]')
    assert ok.status_code == 200
    facts = {f["name"]: f["value"] for f in ok.json()["facts"]}
    assert facts == {"agua_personas": 3.0, "agua_lecturas": '[{"m3":9.0,"mes":"2026-02"}]'}
    assert put("seguros_nada", 1).status_code == 404
    # Tax facts work exactly as before.
    assert put("edad", 40).status_code == 200


def _agent_que_actua(env: Env) -> None:
    provider = ScriptedProvider(
        [
            tool_turn(("consultar_agua", {"pregunta": "Cambia mi tarifa"})),
            tool_turn(("agua_cambiar_tarifa", {"x": 1}), ("agua_cambiar_tarifa", {"x": 1})),
            text_turn("Queda pendiente de tu confirmación."),
            text_turn("El cambio de tarifa queda pendiente de tu confirmación."),
        ],
        [verdict("respuesta")],
    )
    app.dependency_overrides[get_agent] = lambda: TaxAgent(provider=provider, searcher=FakeSearcher(corpus()))


def test_pending_actions_run_only_after_the_owner_confirms_once(env: Env) -> None:
    ana, luis = env.register("ana@example.com"), env.register("luis@example.com")
    case_id = env.new_case(ana)
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    _agent_que_actua(env)

    body = env.client.post("/agent", json={"query": "Cambia mi tarifa", "case_id": case_id}, headers=ana).json()

    (accion,) = body["pending_actions"]  # the repeated identical call is one action
    assert accion["estado"] == "pendiente" and accion["herramienta"] == "agua_cambiar_tarifa"
    assert env.llamadas.nombres() == []  # nothing ran
    listed = env.client.get("/acciones", params={"case_id": case_id}, headers=ana).json()["acciones"]
    assert [a["id"] for a in listed] == [accion["id"]]
    # Somebody else cannot see or decide it.
    assert env.client.get("/acciones", params={"case_id": case_id}, headers=luis).status_code == 404
    assert env.client.post(f"/acciones/{accion['id']}/confirmar", headers=luis).status_code == 404
    assert env.client.post(f"/acciones/{accion['id']}/rechazar", headers=luis).status_code == 404
    assert env.client.post(f"/acciones/{accion['id']}/confirmar").status_code == 401

    done = env.client.post(f"/acciones/{accion['id']}/confirmar", headers=ana)
    assert done.status_code == 200 and done.json()["estado"] == "ejecutada"
    assert done.json()["resultado"] == "Tarifa cambiada."
    assert env.llamadas.nombres() == ["agua_cambiar_tarifa"]
    assert env.client.post(f"/acciones/{accion['id']}/confirmar", headers=ana).status_code == 409
    assert env.llamadas.nombres() == ["agua_cambiar_tarifa"]  # at most once
    events = [e["event_type"] for e in env.cases.list_audit_events(case_id)]
    assert "accion_confirmada" in events


def test_rejecting_and_switching_off_mean_it_never_runs(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    _agent_que_actua(env)
    first = env.client.post("/agent", json={"query": "x", "case_id": case_id}, headers=ana).json()["pending_actions"][0]
    assert env.client.post(f"/acciones/{first['id']}/rechazar", headers=ana).json()["estado"] == "rechazada"
    assert env.client.post(f"/acciones/{first['id']}/confirmar", headers=ana).status_code == 409

    _agent_que_actua(env)
    second = env.client.post("/agent", json={"query": "x", "case_id": case_id}, headers=ana).json()["pending_actions"][
        0
    ]
    env.client.put("/me/modulos/agua", json={"activo": False}, headers=ana)
    assert env.client.post(f"/acciones/{second['id']}/confirmar", headers=ana).status_code == 409
    assert env.llamadas.nombres() == []


def test_without_active_modules_the_agent_gets_no_specialists(env: Env) -> None:
    ana = env.register("ana@example.com")
    provider = ScriptedProvider([text_turn("Hola.")], [verdict("abstencion")])
    app.dependency_overrides[get_agent] = lambda: TaxAgent(provider=provider, searcher=FakeSearcher(corpus()))
    body = env.client.post("/agent", json={"query": "Hola"}, headers=ana).json()
    assert body["pending_actions"] == []
    assert not any(t.name.startswith("consultar_agua") for t in provider.started["tools"])


def test_inbox_switching_off_and_erasing_a_module(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    env.client.put(f"/cases/{case_id}/facts/agua_personas", json={"value": 4}, headers=ana)
    run = env.client.post(f"/cases/{case_id}/proactive/run", headers=ana).json()
    assert run["created"]["modulo:agua"] == 1
    assert [n["kind"] for n in run["notifications"] if n["kind"].startswith("modulo:")] == ["modulo:agua"]

    response = env.client.put("/me/modulos/agua", json={"activo": False, "borrar_datos": True}, headers=ana)
    assert response.status_code == 200
    assert _de_modulos(env, case_id) == []
    case = env.client.get(f"/cases/{case_id}", headers=ana).json()
    assert case["facts"] == []
    dump = str(env.cases.list_audit_events(case_id))
    assert "module_data_erased" in dump and "'value': 4" not in dump


def test_deleting_a_module_fact_redacts_it_and_drops_its_notices(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    env.client.put(f"/cases/{case_id}/facts/agua_compania", json={"value": "aguas del sur"}, headers=ana)
    env.client.post(f"/cases/{case_id}/proactive/run", headers=ana)
    assert env.client.delete(f"/cases/{case_id}/facts/agua_compania", headers=ana).status_code == 200
    events = env.cases.list_audit_events(case_id)
    assert "AGUAS DEL SUR" not in str(events)
    assert events[-1]["module_notices_removed"] == 1 and _de_modulos(env, case_id) == []


def test_module_data_is_exported_and_erased_with_the_account(env: Env) -> None:
    ana, luis = env.register("ana@example.com"), env.register("luis@example.com")
    case_id = env.new_case(ana)
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    env.client.put(f"/cases/{case_id}/facts/agua_compania", json={"value": "canal secreto"}, headers=ana)
    env.client.post(f"/cases/{case_id}/proactive/run", headers=ana)
    _agent_que_actua(env)
    env.client.post("/agent", json={"query": "x", "case_id": case_id}, headers=ana)
    other = env.new_case(luis)

    export = env.client.get("/me/export", headers=ana).json()
    (exported,) = export["cases"]
    assert ("agua_compania", "CANAL SECRETO") in [(f["name"], f["value"]) for f in exported["case"]["facts"]]
    assert "modulo:agua" in [n["kind"] for n in exported["notifications"]]
    assert exported["pending_actions"][0]["herramienta"] == "agua_cambiar_tarifa"
    assert [(c["modulo"], c["activo"]) for c in export["module_choices"]] == [("agua", True)]

    assert env.client.request("DELETE", "/me", json={"password": PASSWORD}, headers=ana).status_code == 204
    with sqlite3.connect(env.db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        dump = "\n".join(
            str(row) for table in tables if table != "sqlite_sequence" for row in conn.execute(f"SELECT * FROM {table}")
        )
    assert {"user_module_choices", "acciones_pendientes"} <= set(tables)
    for needle in (ana["_user_id"], case_id, "CANAL SECRETO", "agua_cambiar_tarifa"):
        assert needle not in dump, needle
    assert env.client.get(f"/cases/{other}", headers=luis).status_code == 200


def test_without_accounts_modules_follow_the_operator(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUTH_MODE", "off")
    body = env.client.get("/modulos").json()
    assert body["configurable"] is False
    assert {m["clave"]: m["activo"] for m in body["modulos"]} == {"fiscal": True, "agua": False, "seguros": False}
    assert env.client.put("/me/modulos/agua", json={"activo": True}).status_code == 404
    monkeypatch.setattr(settings, "MODULOS_ACTIVOS_POR_DEFECTO", "todos")
    assert all(m["activo"] for m in env.client.get("/modulos").json()["modulos"])
    # Nothing about accounts was written.
    with sqlite3.connect(env.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "user_module_choices" not in tables


def test_notifications_of_old_kinds_still_read(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    env.notifications.add(
        Notification(case_id=case_id, kind="plazo", key="plazo:x", title="Modelo 303", body="", severity="aviso")
    )
    body = env.client.get(f"/cases/{case_id}/notifications", headers=ana).json()
    assert [n["kind"] for n in body["notifications"]] == ["plazo"]


def test_the_user_runs_a_module_tool_and_reads_a_document(env: Env) -> None:
    import base64

    from hacienda_gpt.api.api import get_document_provider

    ana, luis = env.register("ana@example.com"), env.register("luis@example.com")
    case_id = env.new_case(ana)
    run = lambda nombre, args, headers=ana: env.client.post(  # noqa: E731
        f"/modulos/agua/herramientas/{nombre}", json={"case_id": case_id, "argumentos": args}, headers=headers
    )
    assert run("agua_anotar", {"x": 3}).status_code == 409  # module off
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    assert run("agua_anotar", {"x": 3}, luis).status_code == 404  # not their case
    done = run("agua_anotar", {"x": 3})
    assert done.status_code == 200 and done.json()["case_updated"] is True
    facts = {f["name"]: f["value"] for f in env.client.get(f"/cases/{case_id}", headers=ana).json()["facts"]}
    assert facts["agua_personas"] == 3.0
    # From the UI an acting tool still only becomes a pending action.
    queued = run("agua_cambiar_tarifa", {"x": 1}).json()
    assert queued["resultado"] is None and queued["accion"]["estado"] == "pendiente"
    assert "agua_cambiar_tarifa" not in env.llamadas.nombres()
    assert run("calcular_irpf", {}).status_code == 404

    modulos = _modulos(env, ana)
    assert modulos["agua"]["lee_documentos"] and modulos["agua"]["herramienta_de_documento"] == "agua_anotar"
    upload = {"case_id": case_id, "filename": "f.txt", "content_base64": base64.b64encode(b"personas: 3").decode()}
    app.dependency_overrides[get_document_provider] = lambda: object()
    try:
        refused = env.client.post("/modulos/agua/documento", json=upload, headers=ana)
        assert refused.status_code == 403  # no consent to send documents to the model provider
        env.client.put("/me/consents", json={"envio_documentos_a_proveedor_llm": True}, headers=ana)
        read = env.client.post("/modulos/agua/documento", json=upload, headers=ana)
        assert read.status_code == 200
        assert read.json()["herramienta_destino"] == "agua_anotar"
        assert read.json()["propuesta"]["campos"][0]["valor"] == "3"
        assert env.client.post("/modulos/seguros/documento", json=upload, headers=ana).status_code == 409
    finally:
        app.dependency_overrides.pop(get_document_provider, None)


def test_an_uploaded_file_goes_straight_to_the_tool_and_never_to_the_audit(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = env.new_case(ana)
    env.client.put("/me/modulos/agua", json={"activo": True}, headers=ana)
    secreto = "ES12 3456 7890 CARGO NETFLIX 12,99"
    done = env.client.post(
        "/modulos/agua/herramientas/agua_consumo",
        json={"case_id": case_id, "argumentos": {"contenido": secreto}},
        headers=ana,
    )
    assert done.status_code == 200
    nombre, argumentos, _contexto = env.llamadas.registro[-1]
    assert nombre == "agua_consumo" and argumentos == {"contenido": secreto}
    events = env.cases.list_audit_events(case_id)
    assert events[-1]["argumentos_recibidos"] == ["contenido"] and secreto not in str(events)
