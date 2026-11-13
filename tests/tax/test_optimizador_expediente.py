"""What the case keeps for the optimiser: the spouse's figures (``datos_conyuge``) and the
preferences (``preferencias_optimizacion``), two structured facts like ``descendientes``.

Stored only valid and canonical (PUT, the agent's ``registrar_hechos``), never offered to the
extractors, and used as the DEFAULT of what a run does not say: by the optimiser, POST /optimize,
the agent's tools and the year-end plan, each request field overriding the stored one."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall
from hacienda_gpt.agent.tools import JOINT_TOOL, RECORD_FACTS_TOOL, Toolbox
from hacienda_gpt.api.api import app
from hacienda_gpt.decision.fact_extractor import (
    _PROVIDER_SCHEMA,
    _RESPONSE_SCHEMA,
    EXPECTED_FACT_TYPE,
    EXTRACTOR_FACT_NAMES,
    OpenAIFactExtractor,
)
from hacienda_gpt.decision.schemas import CaseState, Fact
from hacienda_gpt.proactive.jobs import plan_fin_de_ejercicio
from hacienda_gpt.proactive.store import NotificationStore
from hacienda_gpt.tax.documentos import DOCUMENT_FACTS
from hacienda_gpt.tax.irpf import IRPFInput
from hacienda_gpt.tax.optimizador import (
    DETALLE_A_TEXTO,
    PlanOptimo,
    PreferenciasOptimizacion,
    Restricciones,
    detalle_a_texto,
    normalizar_preferencias,
    optimizar_desde_hechos,
    preferencias_a_texto,
)
from hacienda_gpt.tax.profile import (
    DATOS_CONYUGE,
    DETALLE_FACT_TYPES,
    PREFERENCIAS_OPTIMIZACION,
    DatosConyuge,
    DatosInvalidosError,
    datos_conyuge_a_texto,
    normalizar_datos_conyuge,
)

D = Decimal
NOV = date(2026, 11, 13)
client = TestClient(app)

# A high earner married to someone who earns little (the household of tests/tax/test_optimizador.py).
USUARIO = {
    "rendimiento_trabajo_bruto": 70000.0,
    "cotizaciones_seguridad_social": 4445.0,
    "retenciones": 15000.0,
    "comunidad_autonoma": "Madrid",
    "estado_civil": "casado",
}
CONYUGE = {"rendimiento_trabajo_bruto": 4000, "cotizaciones_seguridad_social": 254, "edad": 44}
PREFERENCIAS = {
    "acepta_iliquidez": True,
    "aportacion_maxima_pensiones": 1000,
    "perdidas_latentes": [{"id": "p-1", "descripcion": "Fondo indexado", "importe": 500}],
}


def _guardados(conyuge: dict | None = CONYUGE, preferencias: dict | None = PREFERENCIAS, **otros) -> dict:
    """Case facts as the store holds them: the structured ones as their canonical JSON text."""
    facts = {**USUARIO, **otros}
    if conyuge is not None:
        facts[DATOS_CONYUGE] = datos_conyuge_a_texto(conyuge)
    if preferencias is not None:
        facts[PREFERENCIAS_OPTIMIZACION] = preferencias_a_texto(preferencias)
    return facts


def _sin_textos(plan: PlanOptimo) -> dict:
    """The plan's figures and choices, without the sentences that say where the inputs came from."""
    return plan.model_dump(mode="json", exclude={"supuestos", "explicacion"})


def _case(facts: dict) -> CaseState:
    return CaseState(
        case_id="c-exp",
        user_id="u",
        jurisdiction="ES",
        tax_period="2026",
        facts=[
            Fact(fact_id=f"fact_{k}", name=k, value=v, value_type=EXPECTED_FACT_TYPE[k], source="t", confidence=1.0)
            for k, v in facts.items()
        ],
    )


class _NoSearch:
    def search(self, *args, **kwargs):  # pragma: no cover - never called
        return []


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


# --------------------------------------------------------------------------- #
# Vocabulary and canonical form
# --------------------------------------------------------------------------- #


def test_both_are_structured_case_facts_that_no_extractor_may_emit() -> None:
    for nombre in (DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION):
        assert EXPECTED_FACT_TYPE[nombre].value == "string"
        assert nombre not in EXTRACTOR_FACT_NAMES and nombre not in DOCUMENT_FACTS
        for schema in (_RESPONSE_SCHEMA["schema"], _PROVIDER_SCHEMA):
            assert nombre not in schema["properties"]["facts"]["items"]["properties"]["name"]["enum"]
        raw = [{"name": nombre, "value_string": "mi mujer gana 12.000 €", "confidence": 0.9}]
        assert OpenAIFactExtractor._materialize_facts(raw) == []
        # ...but the agent may record them (validated, see below).
        assert nombre in RECORD_FACTS_TOOL.input_schema["properties"]["hechos"]["items"]["properties"]["nombre"]["enum"]
        assert nombre in RECORD_FACTS_TOOL.description
    # Every structured fact has its validator.
    assert set(DETALLE_A_TEXTO) == set(DETALLE_FACT_TYPES)


def test_the_spouse_fields_are_calculator_fields_with_the_calculator_bounds() -> None:
    for nombre, campo in DatosConyuge.model_fields.items():
        assert nombre in IRPFInput.model_fields, nombre
        propio = IRPFInput.model_fields[nombre]
        limites = {type(m).__name__: m for m in propio.metadata}
        nuestros = {type(m).__name__: m for m in campo.metadata}
        if "Ge" in limites:  # the same lower bound as the calculator (a stricter upper one is a sanity cap)
            assert nuestros["Ge"].ge == limites["Ge"].ge, nombre
        if "Le" in limites:
            assert nuestros["Le"].le == limites["Le"].le, nombre
        assert campo.default is None
    # What is shared with the user, or a choice, is not a figure of the spouse.
    for compartido in ("comunidad_autonoma", "descendientes", "num_descendientes", "tributacion", "ejercicio"):
        assert compartido not in DatosConyuge.model_fields


def test_spouse_figures_have_one_canonical_text() -> None:
    texto = datos_conyuge_a_texto({"rendimiento_trabajo_bruto": 12000.0, "edad": 41.0, "retenciones": "900.50"})
    assert texto == '{"edad":41,"rendimiento_trabajo_bruto":"12000","retenciones":"900.5"}'
    # Whatever form an amount comes in, and stored again: the same text.
    assert datos_conyuge_a_texto('{"retenciones": 900.5, "rendimiento_trabajo_bruto": "12000.00", "edad": 41}') == texto
    assert datos_conyuge_a_texto(texto) == texto
    # A stated zero is a figure (a spouse without income), not an absent one.
    assert json.loads(datos_conyuge_a_texto({"rendimiento_trabajo_bruto": 0, "necesita_asistencia": False})) == {
        "necesita_asistencia": False,
        "rendimiento_trabajo_bruto": "0",
    }
    assert normalizar_datos_conyuge({"ganancias_patrimoniales": -300}) == {"ganancias_patrimoniales": "-300"}


def test_preferences_have_one_canonical_text_and_keep_each_position_id_and_description() -> None:
    texto = preferencias_a_texto(
        {
            "aportacion_maxima_pensiones": 1500.0,
            "acepta_iliquidez": True,
            "donativo_previsto": 0,  # the default: not kept
            "presupuesto_donativos": None,
            "perdidas_latentes": [
                {"id": "p-1", "descripcion": "Fondo indexado", "importe": 800, "tipo": "valores_cotizados"},
                {"id": "p-2", "importe": "120.50", "titular": "conyuge", "tipo": "otro_elemento"},
            ],
            "ganancias_latentes": [],
            "tributacion_prevista": "conjunta_biparental",
        }
    )
    assert json.loads(texto) == {
        "acepta_iliquidez": True,
        "aportacion_maxima_pensiones": "1500",
        "perdidas_latentes": [
            {"descripcion": "Fondo indexado", "id": "p-1", "importe": "800"},
            {"id": "p-2", "importe": "120.5", "tipo": "otro_elemento", "titular": "conyuge"},
        ],
        "tributacion_prevista": "conjunta_biparental",
    }
    assert preferencias_a_texto(texto) == texto
    # "No puedo aportar nada más" is a statement (0), unlike not saying it.
    assert normalizar_preferencias({"aportacion_maxima_pensiones": 0}) == {"aportacion_maxima_pensiones": "0"}
    assert normalizar_preferencias({}) == {}
    # The date of the plan and anything the optimiser derives are not preferences.
    for campo in ("fecha", "ahorro_total", "palancas", "ejercicio"):
        assert campo not in PreferenciasOptimizacion.model_fields


@pytest.mark.parametrize(
    ("nombre", "valor", "mensaje"),
    [
        (DATOS_CONYUGE, "mi mujer gana 12.000", "no se puede leer"),
        (DATOS_CONYUGE, "[1, 2]", "debe ser un objeto"),
        (DATOS_CONYUGE, "{}", "están vacías"),
        (DATOS_CONYUGE, '{"salario": 1}', "salario: campo desconocido. Campos admitidos: rendimiento_trabajo_bruto"),
        (DATOS_CONYUGE, '{"comunidad_autonoma": "Madrid"}', "comunidad_autonoma: campo desconocido"),
        (DATOS_CONYUGE, '{"rendimiento_trabajo_bruto": -5}', "rendimiento_trabajo_bruto: debe ser mayor o igual que 0"),
        (DATOS_CONYUGE, '{"rendimiento_trabajo_bruto": "NaN"}', "debe ser un número finito"),
        (DATOS_CONYUGE, '{"rendimiento_trabajo_bruto": 1e12}', "debe ser menor o igual que 1000000000"),
        (DATOS_CONYUGE, '{"edad": 41.5}', "edad: debe ser un número entero, sin decimales"),
        (DATOS_CONYUGE, '{"grado_discapacidad": 120}', "grado_discapacidad: debe ser menor o igual que 100"),
        (DATOS_CONYUGE, '{"necesita_asistencia": "a veces"}', "necesita_asistencia: debe ser verdadero o falso"),
        (DATOS_CONYUGE, "[" * 5000, "no se puede leer"),
        (DATOS_CONYUGE, '{"a": "' + "x" * 20001 + '"}', "demasiado largo"),
        (PREFERENCIAS_OPTIMIZACION, "hasta 1.500 al plan", "no se puede leer"),
        (PREFERENCIAS_OPTIMIZACION, '{"fecha": "2026-12-01"}', "fecha: campo desconocido. Campos admitidos"),
        (PREFERENCIAS_OPTIMIZACION, '{"aportacion_maxima_pensiones": -1}', "debe ser mayor o igual que 0"),
        (PREFERENCIAS_OPTIMIZACION, '{"acepta_iliquidez": "quizá"}', "acepta_iliquidez: debe ser verdadero o falso"),
        (
            PREFERENCIAS_OPTIMIZACION,
            '{"donativo_previsto": 300, "presupuesto_donativos": 100}',
            "el presupuesto de donativos no puede ser menor",
        ),
        (PREFERENCIAS_OPTIMIZACION, '{"perdidas_latentes": [{"importe": 0}]}', "pérdida latente 1, importe: debe ser"),
        (PREFERENCIAS_OPTIMIZACION, '{"ganancias_latentes": [{"importe": 5, "tipo": "piso"}]}', "ganancia latente 1"),
        (
            PREFERENCIAS_OPTIMIZACION,
            '{"perdidas_latentes": [{"id": "a", "importe": 1}, {"id": "a", "importe": 2}]}',
            "mismo identificador",
        ),
        (PREFERENCIAS_OPTIMIZACION, '{"perdidas_latentes": [{"id": "a b", "importe": 1}]}', "id: tiene un formato"),
        (PREFERENCIAS_OPTIMIZACION, '{"tributacion_prevista": "conjunta_monoparental"}', "debe ser uno de estos"),
        (PREFERENCIAS_OPTIMIZACION, json.dumps({"perdidas_latentes": [{"importe": 1}] * 21}), "como máximo 20"),
    ],
)
def test_bad_values_are_refused_with_a_spanish_sentence(nombre: str, valor: str, mensaje: str) -> None:
    with pytest.raises(DatosInvalidosError, match=mensaje):
        detalle_a_texto(nombre, valor)


# --------------------------------------------------------------------------- #
# PUT /cases/{id}/facts/{name}
# --------------------------------------------------------------------------- #


def _nuevo_caso(usuario: str) -> str:
    respuesta = client.post("/cases", json={"user_id": usuario, "jurisdiction": "ES", "tax_period": "2026"})
    assert respuesta.status_code == 200
    return respuesta.json()["case_id"]


def _hecho(caso: dict, nombre: str):
    return next((f for f in caso["facts"] if f["name"] == nombre), None)


def test_put_stores_both_facts_validated_and_canonical() -> None:
    case_id = _nuevo_caso("u-exp-put")
    respuesta = client.put(
        f"/cases/{case_id}/facts/{DATOS_CONYUGE}",
        json={"value": json.dumps({"edad": 41.0, "rendimiento_trabajo_bruto": 12000.5, "retenciones": 0})},
    )
    assert respuesta.status_code == 200, respuesta.text
    guardado = _hecho(respuesta.json(), DATOS_CONYUGE)
    assert guardado["value"] == '{"edad":41,"rendimiento_trabajo_bruto":"12000.5","retenciones":"0"}'
    assert guardado["value_type"] == "string" and guardado["source"] == "user_edit"

    respuesta = client.put(
        f"/cases/{case_id}/facts/{PREFERENCIAS_OPTIMIZACION}",
        json={"value": json.dumps({**PREFERENCIAS, "donativo_previsto": 0, "tributacion_prevista": "individual"})},
    )
    assert respuesta.status_code == 200, respuesta.text
    assert _hecho(respuesta.json(), PREFERENCIAS_OPTIMIZACION)["value"] == (
        '{"acepta_iliquidez":true,"aportacion_maxima_pensiones":"1000","perdidas_latentes":'
        '[{"descripcion":"Fondo indexado","id":"p-1","importe":"500"}],"tributacion_prevista":"individual"}'
    )
    # Removing them is the ordinary fact deletion.
    borrado = client.delete(f"/cases/{case_id}/facts/{DATOS_CONYUGE}")
    assert borrado.status_code == 200 and _hecho(borrado.json(), DATOS_CONYUGE) is None


@pytest.mark.parametrize(
    ("nombre", "valor"),
    [
        (DATOS_CONYUGE, "doce mil"),
        (DATOS_CONYUGE, "{}"),
        (DATOS_CONYUGE, '{"rendimiento_trabajo_bruto": -1}'),
        (DATOS_CONYUGE, '{"descendientes": []}'),
        (DATOS_CONYUGE, 12000),
        (DATOS_CONYUGE, True),
        (DATOS_CONYUGE, "[" * 3000 + "]" * 3000),
        (PREFERENCIAS_OPTIMIZACION, "sí"),
        (PREFERENCIAS_OPTIMIZACION, '{"perdidas_latentes": {"importe": 5}}'),
        (PREFERENCIAS_OPTIMIZACION, '{"donativo_previsto": 300, "presupuesto_donativos": 100}'),
        (PREFERENCIAS_OPTIMIZACION, '{"perdidas_latentes": [{"importe": 5, "titular": "hijo"}]}'),
        (PREFERENCIAS_OPTIMIZACION, json.dumps({"ganancias_latentes": [{"importe": 1, "descripcion": "x" * 201}]})),
        (PREFERENCIAS_OPTIMIZACION, False),
    ],
)
def test_put_answers_422_in_spanish_and_stores_nothing(nombre: str, valor) -> None:
    case_id = _nuevo_caso("u-exp-422")
    respuesta = client.put(f"/cases/{case_id}/facts/{nombre}", json={"value": valor})
    assert respuesta.status_code == 422, respuesta.text
    detalle = respuesta.json()["detail"]
    assert isinstance(detalle, str) and detalle
    assert _hecho(client.get(f"/cases/{case_id}").json(), nombre) is None


# --------------------------------------------------------------------------- #
# The optimiser: stored values are the defaults, a run's own replace them field by field
# --------------------------------------------------------------------------- #


def test_stored_figures_and_preferences_give_the_plan_of_the_same_values_sent_explicitly() -> None:
    guardado = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=_guardados())
    explicito = optimizar_desde_hechos(
        ejercicio=2026, fecha=NOV, facts=USUARIO, conyuge=CONYUGE, restricciones=PREFERENCIAS
    )
    assert guardado.con_conyuge and _sin_textos(guardado) == _sin_textos(explicito)
    assert _sin_textos(guardado)["optimo"]["tributacion"] == "conjunta_biparental"
    pension = next(p for p in guardado.palancas if p.id == "plan_pensiones_contribuyente")
    assert pension.importe is not None and pension.importe <= D(1000)  # the stored cash limit holds
    # The plan says what it took from the case, in words.
    assert guardado.supuestos[0] == (
        "Se han usado las cifras de tu cónyuge guardadas en tu expediente: salario bruto 4.000,00 €; cotizaciones a "
        "la Seguridad Social 254,00 €; edad 44 años."
    )
    assert guardado.supuestos[1] == (
        "Se han usado tus preferencias guardadas en el expediente: puedes aportar hasta 1.000,00 € más a tu plan de "
        "pensiones; aceptas que el dinero quede inmovilizado hasta la jubilación; inversiones con pérdidas latentes: "
        "Fondo indexado (500,00 €)."
    )
    assert not any("Se han usado" in s for s in explicito.supuestos)
    assert "Se han usado las cifras de tu cónyuge" in guardado.explicacion


def test_a_run_overrides_the_stored_values_field_by_field() -> None:
    facts = _guardados()
    # One spouse figure replaced, one removed with null; the rest stay the stored ones.
    plan = optimizar_desde_hechos(
        ejercicio=2026,
        fecha=NOV,
        facts=facts,
        conyuge={"rendimiento_trabajo_bruto": 9000, "edad": None},
        restricciones=Restricciones(aportacion_maxima_pensiones=None),
    )
    esperado = optimizar_desde_hechos(
        ejercicio=2026,
        fecha=NOV,
        facts=USUARIO,
        conyuge={"rendimiento_trabajo_bruto": 9000, "cotizaciones_seguridad_social": 254},
        restricciones={"acepta_iliquidez": True, "perdidas_latentes": PREFERENCIAS["perdidas_latentes"]},
    )
    assert _sin_textos(plan) == _sin_textos(esperado)
    assert plan.supuestos[0] == (
        "Se han usado las cifras de tu cónyuge guardadas en tu expediente (las demás son las de esta consulta): "
        "cotizaciones a la Seguridad Social 254,00 €."
    )
    # The stored cash limit was overridden (None = not stated): the plan assumes the legal maximum and says so.
    assert "puedes aportar hasta" not in plan.supuestos[1]
    assert any("no has indicado cuánto puedes aportar" in s for s in plan.supuestos)

    # A dict counts the keys it has: here everything is the run's own, nothing from the case.
    todo = optimizar_desde_hechos(
        ejercicio=2026,
        fecha=NOV,
        facts=facts,
        conyuge={**CONYUGE},
        restricciones={campo: None for campo in ("aportacion_maxima_pensiones", "acepta_iliquidez")}
        | {"perdidas_latentes": []},
    )
    assert not any(s.startswith("Se han usado") for s in todo.supuestos)

    # A run can leave the spouse out (the API's explicit "conyuge": null).
    solo = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts, sin_conyuge=True)
    assert not solo.con_conyuge and len(solo.base.declaraciones) == 1


def test_stored_spouse_figures_need_a_married_user() -> None:
    for estado, dice in (("soltero", "tu estado civil es «soltero»"), (None, "no consta tu estado civil")):
        facts = _guardados(preferencias=None)
        if estado is None:
            del facts["estado_civil"]
        else:
            facts["estado_civil"] = estado
        plan = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts)
        assert not plan.con_conyuge
        assert any(dice in s and "no se han usado" in s for s in plan.supuestos)
        # The run's datos decide the civil status too.
        casado = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts, datos={"estado_civil": "casada"})
        assert casado.con_conyuge


def test_stored_filing_plan_and_spouse_positions_only_apply_with_the_spouse() -> None:
    prefs = {
        "acepta_iliquidez": True,
        "tributacion_prevista": "conjunta_biparental",
        "perdidas_latentes": [
            {"id": "a", "importe": 300, "descripcion": "Acciones"},
            {"id": "b", "importe": 700, "titular": "conyuge", "descripcion": "Fondo de mi mujer"},
        ],
    }
    pareja = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=_guardados(preferencias=prefs))
    assert pareja.base.tributacion == "conjunta_biparental"
    assert "pensabais presentar una declaración conjunta" in pareja.supuestos[1]
    assert "Fondo de mi mujer (700,00 €, de tu cónyuge)" in pareja.supuestos[1]
    # The run's own starting point wins.
    individual = optimizar_desde_hechos(
        ejercicio=2026, fecha=NOV, facts=_guardados(preferencias=prefs), datos={"tributacion": "individual"}
    )
    assert individual.base.tributacion == "individual"
    assert "pensabais" not in " ".join(individual.supuestos)

    # Without the spouse: no starting joint return, and the spouse's stored loss is left out (said), not refused.
    facts = _guardados(preferencias=prefs, estado_civil="soltero")
    solo = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts)
    assert not solo.con_conyuge and solo.base.tributacion == "individual"
    assert any("inversiones de tu cónyuge: sin sus cifras no se han tenido en cuenta" in s for s in solo.supuestos)
    assert "Fondo de mi mujer" not in " ".join(solo.supuestos)
    # A spouse position sent by the run itself, without the spouse, is still an error.
    with pytest.raises(DatosInvalidosError, match="cónyuge"):
        optimizar_desde_hechos(
            ejercicio=2026,
            fecha=NOV,
            facts=facts,
            restricciones={"perdidas_latentes": [{"importe": 5, "titular": "conyuge"}]},
        )


def test_a_run_that_contradicts_the_stored_preferences_is_told_why() -> None:
    facts = _guardados(preferencias={"presupuesto_donativos": 500})
    with pytest.raises(DatosInvalidosError, match="presupuesto de donativos.*preferencias guardadas en tu expediente"):
        optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts, restricciones={"donativo_previsto": 800})


def test_a_stored_value_that_no_longer_validates_is_left_out_and_said() -> None:
    facts = {**USUARIO, DATOS_CONYUGE: '{"sueldo": 1}', PREFERENCIAS_OPTIMIZACION: "no es JSON"}
    plan = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts)
    assert not plan.con_conyuge
    assert any("Las cifras de tu cónyuge guardadas en tu expediente no se han podido usar" in a for a in plan.avisos)
    assert any("Las preferencias del optimizador guardadas" in a for a in plan.avisos)


# --------------------------------------------------------------------------- #
# POST /optimize
# --------------------------------------------------------------------------- #


def test_optimize_endpoint_uses_the_case_and_takes_the_request_over_it() -> None:
    case_id = _nuevo_caso("u-exp-opt")
    for nombre, valor in USUARIO.items():
        assert client.put(f"/cases/{case_id}/facts/{nombre}", json={"value": valor}).status_code == 200
    for nombre, valor in ((DATOS_CONYUGE, CONYUGE), (PREFERENCIAS_OPTIMIZACION, PREFERENCIAS)):
        assert client.put(f"/cases/{case_id}/facts/{nombre}", json={"value": json.dumps(valor)}).status_code == 200

    def plan(**cuerpo) -> PlanOptimo:
        respuesta = client.post("/optimize", json={"case_id": case_id, "fecha": "2026-11-13", **cuerpo})
        assert respuesta.status_code == 200, respuesta.text
        return PlanOptimo.model_validate(respuesta.json())

    guardado = plan()
    assert guardado.con_conyuge and guardado.supuestos[0].startswith("Se han usado las cifras de tu cónyuge")
    assert _sin_textos(guardado) == _sin_textos(
        optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=USUARIO, conyuge=CONYUGE, restricciones=PREFERENCIAS)
    )
    # What the optimiser screen sends: every field of the screen, the stored values typed over or cleared.
    pantalla = plan(
        conyuge={"rendimiento_trabajo_bruto": 4000, "cotizaciones_seguridad_social": 254, "edad": 44},
        restricciones={
            "aportacion_maxima_pensiones": None,
            "aportacion_maxima_pensiones_conyuge": None,
            "acepta_iliquidez": True,
            "donativo_previsto": 0,
            "presupuesto_donativos": None,
            "donativo_recurrente_importe_anterior": None,
            "perdidas_latentes": [],
            "ganancias_latentes": [],
        },
        datos={"tributacion": "individual"},
    )
    assert not any(s.startswith("Se han usado") for s in pantalla.supuestos)
    assert any("no has indicado cuánto puedes aportar" in s for s in pantalla.supuestos)
    # An explicit null leaves the stored spouse out of this run.
    assert not plan(conyuge=None).con_conyuge
    # A request that clashes with the stored preferences: 422 with the reason.
    respuesta = client.post(
        "/optimize",
        json={"case_id": case_id, "restricciones": {"donativo_previsto": 50, "presupuesto_donativos": 10}},
    )
    assert respuesta.status_code == 422 and "presupuesto de donativos" in respuesta.json()["detail"]


# --------------------------------------------------------------------------- #
# Agent tools
# --------------------------------------------------------------------------- #


def test_the_optimiser_tool_starts_from_what_the_case_keeps(monkeypatch) -> None:
    import hacienda_gpt.agent.tools as tools

    monkeypatch.setattr(tools, "hoy_en_espana", lambda: NOV)
    toolbox = Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger(), case=_case(_guardados()))
    resultado = toolbox.execute(_call("optimizar_impuestos"))
    assert not resultado.is_error, resultado.content
    esperado = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=_guardados())
    assert resultado.summary.startswith(f"ahorro {tools._money(esperado.ahorro_total)}")
    assert "Se han usado las cifras de tu cónyuge guardadas en tu expediente" in resultado.content
    assert "puedes aportar hasta 1.000,00 € más a tu plan de pensiones" in resultado.content
    # A constraint the agent passes replaces the stored one (and is not reported as taken from the case).
    otro = toolbox.execute(_call("optimizar_impuestos", aportacion_maxima_pensiones=0))
    assert not otro.is_error
    sin_aportar = optimizar_desde_hechos(
        ejercicio=2026, fecha=NOV, facts=_guardados(), restricciones={"aportacion_maxima_pensiones": 0}
    )
    assert otro.summary.startswith(f"ahorro {tools._money(sin_aportar.ahorro_total)}")
    assert not any(p.id == "plan_pensiones_contribuyente" for p in sin_aportar.palancas)
    assert "a tu plan de pensiones antes del" not in otro.content
    assert "puedes aportar hasta 1.000,00 €" not in otro.content


def test_the_joint_tool_uses_the_stored_spouse_when_the_agent_gives_none() -> None:
    assert "conyuge_b" not in JOINT_TOOL.input_schema.get("required", [])
    ledger = EvidenceLedger()
    toolbox = Toolbox(searcher=_NoSearch(), ledger=ledger, case=_case(_guardados()))
    guardado = toolbox.execute(_call("comparar_declaracion_conjunta"))
    assert not guardado.is_error, guardado.content
    assert "Cónyuge B: cifras guardadas en el expediente — salario bruto 4.000,00 €" in guardado.content

    sin_caso = Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger(), case=_case(USUARIO))
    explicito = sin_caso.execute(_call("comparar_declaracion_conjunta", conyuge_b=CONYUGE))
    assert not explicito.is_error
    # The same three returns and verdict as with the figures passed by the agent.
    veredicto = guardado.content.split("\n", 1)[0].split("] ", 1)[1]
    assert veredicto == explicito.content.split("\n", 1)[0].split("] ", 1)[1]
    assert "Sale mejor la declaración conjunta" in veredicto

    # Field by field: the agent's figure wins, the others are the stored ones.
    mezcla = toolbox.execute(_call("comparar_declaracion_conjunta", conyuge_b={"rendimiento_trabajo_bruto": 30000}))
    assert not mezcla.is_error
    assert (
        "Cónyuge B: cifras guardadas en el expediente — cotizaciones a la Seguridad Social 254,00 €; edad 44 años."
        in mezcla.content
    )
    # Without stored figures the spouse's figures are still required.
    falta = sin_caso.execute(_call("comparar_declaracion_conjunta"))
    assert falta.is_error and "Falta 'conyuge_b'" in falta.content


def test_record_facts_validates_and_canonicalises_both() -> None:
    toolbox = Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger(), case=_case(USUARIO))
    mal = toolbox.execute(
        _call(
            "registrar_hechos",
            hechos=[
                {"nombre": DATOS_CONYUGE, "valor": "gana unos 12.000"},
                {"nombre": PREFERENCIAS_OPTIMIZACION, "valor": '{"aportacion_maxima_pensiones": -3}'},
            ],
        )
    )
    assert mal.is_error and "Cifras de tu cónyuge" in mal.content and "Preferencias del optimizador" in mal.content
    assert toolbox.case is not None and not any(f.name == DATOS_CONYUGE for f in toolbox.case.facts)

    bien = toolbox.execute(
        _call(
            "registrar_hechos",
            hechos=[
                {"nombre": DATOS_CONYUGE, "valor": {"rendimiento_trabajo_bruto": 4000.0, "edad": 44}},
                {"nombre": PREFERENCIAS_OPTIMIZACION, "valor": json.dumps({"aportacion_maxima_pensiones": 1000})},
            ],
        )
    )
    assert not bien.is_error
    hechos = {f.name: f.value for f in toolbox.case.facts}
    assert hechos[DATOS_CONYUGE] == '{"edad":44,"rendimiento_trabajo_bruto":"4000"}'
    assert hechos[PREFERENCIAS_OPTIMIZACION] == '{"aportacion_maxima_pensiones":"1000"}'


# --------------------------------------------------------------------------- #
# The year-end plan
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path) -> NotificationStore:
    return NotificationStore(str(tmp_path / "db.sqlite3"))


def test_the_year_end_plan_respects_the_stored_limits_and_says_it_used_them(store) -> None:
    salaried = {
        "rendimiento_trabajo_bruto": 45000.0,
        "cotizaciones_seguridad_social": 2900.0,
        "retenciones": 9000.0,
        "comunidad_autonoma": "Madrid",
    }
    prefs = {"aportacion_maxima_pensiones": 600, "acepta_iliquidez": True, "donativo_previsto": 300}
    facts = {**salaried, PREFERENCIAS_OPTIMIZACION: preferencias_a_texto(prefs)}
    assert plan_fin_de_ejercicio(_case(facts), hoy=NOV, store=store) == 1
    (aviso,) = store.list("c-exp")
    esperado = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts)
    pension = next(p for p in esperado.palancas if p.id == "plan_pensiones_contribuyente")
    assert pension.importe == D(600)  # never above what the user can put in
    assert aviso.amount == esperado.ahorro_total
    assert "Aportar 600,00 € a tu plan de pensiones" in aviso.body
    assert (
        "Se han usado tus preferencias guardadas en el expediente: puedes aportar hasta 600,00 € más a tu plan de "
        "pensiones; aceptas que el dinero quede inmovilizado hasta la jubilación; vas a donar 300,00 € de todos modos."
    ) in aviso.body
    # Without them it assumed the legal maximum and said so; with them it does not need to.
    assert "no has indicado cuánto puedes aportar" not in aviso.body
    assert "No has dicho si aceptas inmovilizar" not in aviso.body
    # The donation the user will make is part of the plan, not a side note.
    assert any(p.id == "donativo" for p in esperado.palancas) and "**Si pensabas donar**" not in aviso.body

    # Changing the stored preferences rewrites the plan (the key hashes every fact, these included).
    facts[PREFERENCIAS_OPTIMIZACION] = preferencias_a_texto({**prefs, "aportacion_maxima_pensiones": 0})
    assert plan_fin_de_ejercicio(_case(facts), hoy=NOV, store=store) == 1
    (nuevo,) = store.list("c-exp")
    assert nuevo.key != aviso.key
    assert not any("a tu plan de pensiones" in accion for accion in nuevo.actions)
    assert "no puedes aportar más a tu plan de pensiones" in nuevo.body


def test_the_year_end_plan_of_a_married_user_uses_the_stored_spouse(store) -> None:
    facts = _guardados(preferencias={"acepta_iliquidez": True, "tributacion_prevista": "individual"})
    assert plan_fin_de_ejercicio(_case(facts), hoy=NOV, store=store) == 1
    (aviso,) = store.list("c-exp")
    esperado = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts=facts)
    assert esperado.con_conyuge and aviso.amount == esperado.ahorro_total
    assert "**Resultado estimado con vuestros datos de hoy (los dos cónyuges):**" in aviso.body
    assert "Se han usado las cifras de tu cónyuge guardadas en tu expediente: salario bruto 4.000,00 €" in aviso.body
    assert "pensabais presentar cada uno su declaración" in aviso.body
    assert "Presentar declaración conjunta" in aviso.body

