"""The spouse's figures and the optimiser's preferences are personal financial data of the account:
they are in the export (``GET /me/export``) and nothing of them survives ``DELETE /me``."""

from __future__ import annotations

from datetime import date
import json
import sqlite3

from hacienda_gpt.proactive.jobs import plan_fin_de_ejercicio
from hacienda_gpt.tax.profile import DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION
from .conftest import PASSWORD, Env

# Distinctive values: if any of them is left anywhere in the database file, the test sees it.
SALARIO_CONYUGE = "23456.78"
DESCRIPCION = "FondoSecretoXYZ"
CONYUGE = {"rendimiento_trabajo_bruto": float(SALARIO_CONYUGE), "cotizaciones_seguridad_social": 1490, "edad": 52}
PREFERENCIAS = {
    "aportacion_maxima_pensiones": 1234,
    "acepta_iliquidez": True,
    "perdidas_latentes": [{"id": "fila-1", "descripcion": DESCRIPCION, "importe": 987}],
}


def _caso_con_datos_del_optimizador(env: Env, headers: dict) -> str:
    case_id = env.client.post("/cases", json={"user_id": "x", "tax_period": "2026"}, headers=headers).json()["case_id"]
    for nombre, valor in (
        ("rendimiento_trabajo_bruto", 52000),
        ("cotizaciones_seguridad_social", 3300),
        ("comunidad_autonoma", "Madrid"),
        ("estado_civil", "casado"),
        (DATOS_CONYUGE, json.dumps(CONYUGE)),
        (PREFERENCIAS_OPTIMIZACION, json.dumps(PREFERENCIAS)),
    ):
        respuesta = env.client.put(f"/cases/{case_id}/facts/{nombre}", json={"value": valor}, headers=headers)
        assert respuesta.status_code == 200, respuesta.text
    # The year-end plan quotes the stored figures in its assumptions: a notification of the account.
    case = env.cases.get_case(case_id)
    assert case is not None
    assert plan_fin_de_ejercicio(case, hoy=date(2026, 11, 13), store=env.notifications) == 1
    return case_id


def test_the_export_carries_both_facts_and_what_was_derived_from_them(env: Env) -> None:
    ana = env.register("ana@example.com")
    luis = env.register("luis@example.com")
    case_id = _caso_con_datos_del_optimizador(env, ana)

    export = env.client.get("/me/export", headers=ana).json()
    [exportado] = export["cases"]
    assert exportado["case"]["case_id"] == case_id
    hechos = {f["name"]: f["value"] for f in exportado["case"]["facts"]}
    assert json.loads(hechos[DATOS_CONYUGE]) == {
        "cotizaciones_seguridad_social": "1490",
        "edad": 52,
        "rendimiento_trabajo_bruto": SALARIO_CONYUGE,
    }
    assert json.loads(hechos[PREFERENCIAS_OPTIMIZACION]) == {
        "acepta_iliquidez": True,
        "aportacion_maxima_pensiones": "1234",
        "perdidas_latentes": [{"descripcion": DESCRIPCION, "id": "fila-1", "importe": "987"}],
    }
    # Each edit is in the case's audit trail, and the year-end plan that used them is in the notifications.
    editados = [e.get("fact_name") for e in exportado["audit_events"] if e["event_type"] == "fact_edited_by_user"]
    assert DATOS_CONYUGE in editados and PREFERENCIAS_OPTIMIZACION in editados
    [aviso] = exportado["notifications"]
    assert (
        "Se han usado las cifras de tu cónyuge guardadas en tu expediente: salario bruto 23.456,78 €" in aviso["body"]
    )
    assert DESCRIPCION in aviso["body"]

    # Nobody else sees them.
    otro = env.client.get("/me/export", headers=luis).text
    assert SALARIO_CONYUGE not in otro and DESCRIPCION not in otro and case_id not in otro


def test_deleting_the_account_leaves_nothing_of_them(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = _caso_con_datos_del_optimizador(env, ana)
    with sqlite3.connect(env.db_path) as conn:
        antes = "\n".join(
            str(row)
            for table in ("case_states", "audit_events", "notifications")
            for row in conn.execute(f"SELECT * FROM {table}")
        )
    assert SALARIO_CONYUGE in antes and DESCRIPCION in antes  # the needles are really stored

    assert env.client.request("DELETE", "/me", json={"password": PASSWORD}, headers=ana).status_code == 204

    with sqlite3.connect(env.db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        dump = "\n".join(
            str(row) for table in tables if table != "sqlite_sequence" for row in conn.execute(f"SELECT * FROM {table}")
        )
    for needle in (SALARIO_CONYUGE, "23.456,78", DESCRIPCION, case_id):
        assert needle not in dump, needle
    assert env.cases.get_case(case_id) is None and env.notifications.list(case_id) == []


def test_removing_the_facts_takes_them_out_of_the_case(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = _caso_con_datos_del_optimizador(env, ana)
    for nombre in (DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION):
        respuesta = env.client.delete(f"/cases/{case_id}/facts/{nombre}", headers=ana)
        assert respuesta.status_code == 200
        assert nombre not in {f["name"] for f in respuesta.json()["facts"]}
    estado = env.client.get(f"/cases/{case_id}", headers=ana).text
    assert SALARIO_CONYUGE not in estado and DESCRIPCION not in estado
    # Another account can neither read nor write them (404, like a case that does not exist).
    luis = env.register("luis@example.com")
    ajeno = env.client.put(f"/cases/{case_id}/facts/{DATOS_CONYUGE}", json={"value": "{}"}, headers=luis)
    assert ajeno.status_code == 404


def _volcado(env: Env) -> str:
    """Every row of every table, after folding the write-ahead log into the file."""
    with sqlite3.connect(env.db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return "\n".join(
            str(row) for table in tables if table != "sqlite_sequence" for row in conn.execute(f"SELECT * FROM {table}")
        )


def test_removing_a_fact_also_erases_its_value_from_the_audit_trail(env: Env) -> None:
    ana = env.register("ana@example.com")
    case_id = _caso_con_datos_del_optimizador(env, ana)
    # The advisor also wrote the values in the shapes its trail keeps: the facts a recommendation rested on,
    # the facts it recorded and a tool call with the spouse's figures.
    env.cases.append_audit_event(
        case_id,
        {
            "event_type": "recommendation_audit",
            "facts_used": [
                {"name": DATOS_CONYUGE, "value": json.dumps(CONYUGE)},
                {"name": "estado_civil", "value": "casado"},
            ],
        },
    )
    env.cases.append_audit_event(
        case_id,
        {
            "event_type": "agent_answer",
            "input": "¿Cómo pagamos menos?",
            "tool_trace": [
                {
                    "tool": "registrar_hechos",
                    "arguments": {"hechos": [{"nombre": DATOS_CONYUGE, "valor": json.dumps(CONYUGE)}]},
                },
                {"tool": "optimizar_impuestos", "arguments": {"conyuge": CONYUGE, "restricciones": PREFERENCIAS}},
            ],
        },
    )
    assert SALARIO_CONYUGE in _volcado(env) and DESCRIPCION in _volcado(env)

    for nombre in (DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION):
        assert env.client.delete(f"/cases/{case_id}/facts/{nombre}", headers=ana).status_code == 200

    volcado = _volcado(env)
    for needle in (SALARIO_CONYUGE, "23456.78", "23.456,78", DESCRIPCION):
        assert needle not in volcado, needle
    # The rest of the trail is intact: other facts, what the user typed, and the record of each removal.
    eventos = env.cases.list_audit_events(case_id)
    assert any(e.get("fact_name") == "estado_civil" and e.get("value") == "casado" for e in eventos)
    assert any(e.get("input") == "¿Cómo pagamos menos?" for e in eventos)
    borrados = [e for e in eventos if e["event_type"] == "fact_value_erased"]
    assert [e["fact_name"] for e in borrados] == [DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION]
    assert borrados[0]["audit_events_redacted"] >= 3 and borrados[0]["year_end_plans_removed"] == 1
    # The year-end plan quoted them: it is gone until the next run rebuilds it from what is left.
    assert env.notifications.list(case_id) == []


def test_redacting_one_fact_leaves_other_facts_untouched() -> None:
    from hacienda_gpt.decision.audit import ELIMINADO, redactar_hecho

    evento = {
        "event_type": "agent_answer",
        "facts_used": [{"name": "edad", "value": 52}, {"name": "rendimiento_trabajo_bruto", "value": 30000}],
        "tool_trace": [{"arguments": {"datos": {"edad": 52, "rendimiento_trabajo_bruto": 30000}}}],
    }
    redactado, cambiado = redactar_hecho(evento, "rendimiento_trabajo_bruto")
    assert cambiado
    assert redactado["facts_used"] == [
        {"name": "edad", "value": 52},
        {"name": "rendimiento_trabajo_bruto", "value": ELIMINADO},
    ]
    assert redactado["tool_trace"][0]["arguments"]["datos"] == {"edad": 52, "rendimiento_trabajo_bruto": ELIMINADO}
    # Nothing to redact: unchanged and reported as such.
    assert redactar_hecho(evento, "retenciones") == (evento, False)
