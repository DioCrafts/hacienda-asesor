"""The finer inputs of the regional deductions, end to end: case facts -> bridge -> calculator,
the agent tools and the ``/simulate`` endpoint."""

from decimal import Decimal
import json

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall
from hacienda_gpt.agent.tools import BENEFITS_TOOL, CALCULATE_TOOL, JOINT_TOOL, RECORD_FACTS_TOOL, Toolbox
from hacienda_gpt.api.api import app
from hacienda_gpt.decision.fact_extractor import (
    _PROVIDER_SCHEMA,
    _RESPONSE_SCHEMA,
    _SYSTEM_PROMPT,
    EXPECTED_FACT_TYPE,
    EXTRACTOR_FACT_NAMES,
    OpenAIFactExtractor,
)
from hacienda_gpt.decision.schemas import CaseState, Fact
from hacienda_gpt.tax.documentos import DOCUMENT_FACTS
from hacienda_gpt.tax.irpf import Descendiente, IRPFInput
from hacienda_gpt.tax.params import GASTOS_DESGLOSADOS
from hacienda_gpt.tax.profile import (
    DESCENDIENTES,
    PROFILE_FACT_TYPES,
    RECUENTOS_DEL_DETALLE,
    DatosInvalidosError,
    build_input,
    descendientes_a_texto,
    normalizar_descendientes,
)
from hacienda_gpt.tax.simulador import simular

D = Decimal
client = TestClient(app)

HIJOS = [
    {"edad": 2, "etapa_educativa": "infantil_primer_ciclo", "gastos": {"gastos_guarderia": 1800}},
    {"fecha_nacimiento": "2018-05-01", "etapa_educativa": "primaria", "gastos": {"gastos_libros_texto": 120.5}},
]
# Inputs that are not profile facts on purpose: the year, choices made per calculation, and the
# children's detail (a structured fact of its own).
_NO_SON_HECHOS = {
    "ejercicio",
    "tributacion",
    "descendientes_compartidos",
    "otros_gastos_deducibles_trabajo",
    "otras_rentas_base_general",
    "donativos_recurrentes",
    "hijos_deduccion_maternidad",
    DESCENDIENTES,
}


class _NoSearch:
    def search(self, query, *, k, filters):
        return []


def _case(**facts) -> CaseState:
    return CaseState(
        case_id="c",
        user_id="u",
        jurisdiction="ES",
        tax_period="2026",
        facts=[
            Fact(fact_id=f"fact_{k}", name=k, value=v, value_type=EXPECTED_FACT_TYPE[k], source="t", confidence=1.0)
            for k, v in facts.items()
        ],
    )


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


def test_every_calculator_input_is_a_profile_fact_of_the_right_type() -> None:
    assert set(IRPFInput.model_fields) - set(PROFILE_FACT_TYPES) == _NO_SON_HECHOS
    for name, kind in PROFILE_FACT_TYPES.items():
        if name not in IRPFInput.model_fields:
            continue
        annotation = str(IRPFInput.model_fields[name].annotation)
        expected = "boolean" if "bool" in annotation else "string" if "str" in annotation else "number"
        if name == "arrendamiento_fecha_contrato":
            expected = "string"
        assert kind == expected, name
    # The extractor is told what each new fact means, and the document reader may propose them.
    for name in ("habitantes_municipio", "gastos_optica", "progenitores_trabajan", "ejercicio_viudedad"):
        assert f"    {name} " in _SYSTEM_PROMPT and name in DOCUMENT_FACTS


def test_the_children_detail_is_a_case_fact_but_no_extractor_may_emit_it() -> None:
    assert EXPECTED_FACT_TYPE[DESCENDIENTES].value == "string"
    assert DESCENDIENTES not in EXTRACTOR_FACT_NAMES and DESCENDIENTES not in DOCUMENT_FACTS
    for schema in (_RESPONSE_SCHEMA["schema"], _PROVIDER_SCHEMA):
        assert DESCENDIENTES not in schema["properties"]["facts"]["items"]["properties"]["name"]["enum"]
    raw = [{"name": DESCENDIENTES, "value_string": "dos hijos de 3 y 8 años", "confidence": 0.9}]
    assert OpenAIFactExtractor._materialize_facts(raw) == []


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #


def test_new_scalar_facts_reach_the_calculator_input() -> None:
    inp = build_input(
        ejercicio=2026,
        facts={
            "edad_conyuge": 33.0,
            "grado_discapacidad_conyuge": 65.0,
            "habitantes_municipio": 2400.0,
            "dias_alquiler_vivienda_habitual": 146.0,
            "num_declaraciones_mismo_alquiler": 2.0,
            "ejercicio_adquisicion_vivienda": 2024.0,
            "progenitores_trabajan": True,
            "vivienda_protegida": True,
            "gastos_optica": 300.0,
            "gastos_salud_bucodental": 150.5,
            "intereses_prestamo_vivienda": 2000.0,
            "gastos_veterinarios": 80.0,
        },
    )
    assert (inp.edad_conyuge, inp.grado_discapacidad_conyuge, inp.habitantes_municipio) == (33, 65, 2400)
    assert (inp.dias_alquiler_vivienda_habitual, inp.num_declaraciones_mismo_alquiler) == (146, 2)
    assert inp.ejercicio_adquisicion_vivienda == 2024 and inp.progenitores_trabajan and inp.vivienda_protegida
    # A total nobody gave is the sum of its parts.
    assert inp.gastos_salud == D("450.5") and inp.inversion_vivienda_habitual == D(2000)
    assert inp.gastos_veterinarios == D(80)


def test_children_detail_is_stored_as_json_text_and_derives_the_counts() -> None:
    texto = descendientes_a_texto(HIJOS)
    assert json.loads(texto) == [
        {"edad": 2, "etapa_educativa": "infantil_primer_ciclo", "gastos": {"gastos_guarderia": "1800"}},
        {"etapa_educativa": "primaria", "fecha_nacimiento": "2018-05-01", "gastos": {"gastos_libros_texto": "120.5"}},
    ]
    assert descendientes_a_texto(texto) == texto  # canonical: storing it again changes nothing
    assert normalizar_descendientes([Descendiente(edad=4)]) == [{"edad": 4}]

    # Stale counts and totals recorded before the detail was known do not block the calculation.
    inp = build_input(
        ejercicio=2026,
        facts={DESCENDIENTES: texto, "num_descendientes": 1.0, "gastos_guarderia": 500.0, "gastos_libros_texto": 120.5},
    )
    assert inp.descendientes is not None and [h.edad for h in inp.descendientes] == [2, 8]
    assert (inp.num_descendientes, inp.num_descendientes_menores_3, inp.num_hijos_primer_ciclo_infantil) == (2, 1, 1)
    assert (inp.hijos_nacidos_en_el_ejercicio, inp.hijos_nacidos_ultimos_3_ejercicios) == (0, 1)
    assert inp.gastos_guarderia == D(1800) and inp.gastos_libros_texto == D("120.5")

    # A detail given for one calculation replaces the stored one and the stored counts...
    otro = build_input(
        ejercicio=2026, facts={DESCENDIENTES: texto, "num_descendientes": 2.0}, cambios={DESCENDIENTES: [{"edad": 0}]}
    )
    assert (otro.num_descendientes, otro.hijos_nacidos_en_el_ejercicio, otro.gastos_guarderia) == (1, 1, D(0))
    # ...and an empty one removes it: the counts are again whatever the facts say.
    sin = build_input(ejercicio=2026, facts={DESCENDIENTES: texto, "num_descendientes": 5.0}, datos={DESCENDIENTES: []})
    assert sin.descendientes is None and sin.num_descendientes == 5


def test_contradictions_are_reported_in_spanish_never_as_a_crash() -> None:
    casos: list[tuple[dict, str]] = [
        # A count typed next to the detail, or over it in a what-if.
        ({"datos": {DESCENDIENTES: HIJOS, "num_descendientes": 3}}, r"«num_descendientes» \(3\) no cuadra.*Quita"),
        ({"facts": {DESCENDIENTES: descendientes_a_texto(HIJOS)}, "cambios": {"num_descendientes_menores_3": 2}}, "Qu"),
        ({"datos": {DESCENDIENTES: HIJOS, "gastos_guarderia": 100}}, "desglose por hijo de «gastos_guarderia»"),
        # A broad total smaller than its parts, given together.
        ({"datos": {"gastos_salud": 100, "gastos_optica": 300}}, r"«gastos_salud» \(100.00 €\) es el total"),
        # Malformed detail.
        ({"facts": {DESCENDIENTES: "dos hijos"}}, "no se puede leer.*guardado en el expediente"),
        ({"datos": {DESCENDIENTES: {"edad": 3}}}, "debe ser una lista"),
        ({"datos": {DESCENDIENTES: [{"grado_discapacidad": 20}]}}, "hijo 1: falta la edad"),
        ({"datos": {DESCENDIENTES: [{"edad": 3}, {"edad": 4, "grado_discapacidad": 120}]}}, "hijo 2: grado_disc.*100"),
        ({"datos": {DESCENDIENTES: [{"edad": 3, "nombre": "Ana"}]}}, "nombre: campo desconocido"),
        ({"datos": {DESCENDIENTES: [{"edad": 3, "gastos": {"sueldo": 1}}]}}, r"gastos desconocidos \(sueldo\)"),
        ({"datos": {DESCENDIENTES: [{"edad": 3, "etapa_educativa": "cole"}]}}, "debe ser uno de estos valores"),
        ({"datos": {DESCENDIENTES: [{"edad": 3, "fecha_nacimiento": "2020-01-01"}]}}, "la edad no cuadra"),
        ({"datos": {DESCENDIENTES: [{"edad": 3, "ejercicio_adopcion": 2010}]}}, "adopción no cuadra"),
        ({"datos": {DESCENDIENTES: [{"edad": 1}] * 21}}, "como máximo 20"),
        # Scalars out of range: the message is in Spanish.
        ({"datos": {"dias_desempleo": 400}}, "dias_desempleo: debe ser menor o igual que 366"),
        ({"datos": {"habitantes_municipio": "muchos"}}, "habitantes_municipio: debe ser un número entero"),
        ({"datos": {"num_declaraciones_mismo_alquiler": 0}}, "debe ser mayor o igual que 1"),
    ]
    for kwargs, mensaje in casos:
        with pytest.raises(DatosInvalidosError, match=mensaje):
            build_input(ejercicio=2026, **kwargs)


def test_a_stored_total_gives_way_to_parts_that_arrive_later() -> None:
    # Parts given for one calculation over an older stored total: the total follows them.
    assert (
        build_input(ejercicio=2026, facts={"gastos_salud": 500.0}, cambios={"gastos_optica": 800}).gastos_salud == 800
    )
    # Stored facts gathered at different times: the parts are the finer datum.
    hechos = {"donativos_autonomicos": 100.0, "donativos_investigacion": 250.0}
    assert build_input(ejercicio=2026, facts=hechos).donativos_autonomicos == 250
    # A total that already covers its parts stays (it may include expenses of no listed kind).
    assert build_input(ejercicio=2026, facts={"gastos_salud": 900.0, "gastos_optica": 800.0}).gastos_salud == 900
    # A total given AFTER the parts and smaller than them is a contradiction to report.
    with pytest.raises(DatosInvalidosError, match="suman más"):
        build_input(ejercicio=2026, facts={"gastos_optica": 800.0}, cambios={"gastos_salud": 500})
    # A per-child part counts too.
    inp = build_input(
        ejercicio=2026,
        facts={
            "gastos_salud": 50.0,
            DESCENDIENTES: descendientes_a_texto([{"edad": 9, "gastos": {"gastos_optica": 200}}]),
        },
    )
    assert inp.gastos_optica == 200 and inp.gastos_salud == 200
    assert all(parte in PROFILE_FACT_TYPES for partes in GASTOS_DESGLOSADOS.values() for parte in partes)


def test_children_detail_changes_a_regional_result() -> None:
    """Madrid, art. 11: the cap is per child, so the breakdown by child decides the deduction."""
    datos = {
        "comunidad_autonoma": "Madrid",
        "rendimiento_trabajo_bruto": 30000,
        "cotizaciones_seguridad_social": 1905,
        "retenciones": 4000,
    }
    agregado = simular(ejercicio=2026, datos={**datos, "num_descendientes": 2, "gastos_escolaridad": 16000})
    hijos = [{"edad": 6, "gastos": {"gastos_escolaridad": 8000}}, {"edad": 9, "gastos": {"gastos_escolaridad": 8000}}]
    detallado = simular(ejercicio=2026, datos={**datos, DESCENDIENTES: hijos})
    a, b = agregado.escenarios[0].resultado, detallado.escenarios[0].resultado
    assert b.cuota_liquida < a.cuota_liquida
    # The counts came from the detail: the benefits catalogue does not ask for them again.
    faltan = {dato for beneficio in detallado.beneficios for dato in beneficio.datos_que_faltan}
    assert not faltan & set(RECUENTOS_DEL_DETALLE)


# --------------------------------------------------------------------------- #
# Agent tools
# --------------------------------------------------------------------------- #


def test_tool_schemas_describe_the_new_inputs() -> None:
    # Every calculator input is named in the description the model reads (the benefits tool
    # refers to the calculator's list).
    for spec in (CALCULATE_TOOL, JOINT_TOOL):
        assert [name for name in IRPFInput.model_fields if name != "ejercicio" and name not in spec.description] == []
    assert "mismos `datos` que calcular_irpf" in BENEFITS_TOOL.description
    hijos = CALCULATE_TOOL.input_schema["properties"]["datos"]["properties"][DESCENDIENTES]
    assert hijos["type"] == "array"
    # ...and the structured one has a schema that mirrors the calculator's model.
    assert set(hijos["items"]["properties"]) == set(Descendiente.model_fields)
    assert hijos["items"]["additionalProperties"] is False
    assert "primaria" in hijos["items"]["properties"]["etapa_educativa"]["enum"]
    escenario = CALCULATE_TOOL.input_schema["properties"]["escenarios"]["items"]["properties"]["cambios"]
    assert escenario["properties"][DESCENDIENTES] is hijos
    assert JOINT_TOOL.input_schema["properties"]["conyuge_a"]["properties"][DESCENDIENTES] is hijos
    assert BENEFITS_TOOL.input_schema["properties"]["datos"]["properties"][DESCENDIENTES] is hijos
    assert (
        DESCENDIENTES in RECORD_FACTS_TOOL.input_schema["properties"]["hechos"]["items"]["properties"]["nombre"]["enum"]
    )
    assert "descendientes" in RECORD_FACTS_TOOL.description


def test_calculate_tool_takes_the_detail_and_turns_contradictions_into_tool_errors() -> None:
    toolbox = Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger())
    base = {"rendimiento_trabajo_bruto": 30000, "comunidad_autonoma": "Madrid"}

    ok = toolbox.execute(_call("calcular_irpf", ejercicio=2026, datos={**base, DESCENDIENTES: HIJOS}))
    assert not ok.is_error and "Mínimo por descendientes" in ok.content

    mal = toolbox.execute(
        _call("calcular_irpf", ejercicio=2026, datos={**base, DESCENDIENTES: HIJOS, "num_descendientes": 3})
    )
    assert mal.is_error and "no cuadra con el detalle de descendientes" in mal.content and "Quita" in mal.content
    mal = toolbox.execute(
        _call(
            "calcular_irpf",
            ejercicio=2026,
            datos=base,
            escenarios=[{"nombre": "x", "cambios": {"gastos_salud": 10, "gastos_optica": 20}}],
        )
    )
    assert mal.is_error and "«gastos_salud»" in mal.content
    mal = toolbox.execute(_call("listar_beneficios_fiscales", ejercicio=2026, datos={DESCENDIENTES: [{"edad": -1}]}))
    assert mal.is_error and "hijo 1: edad: debe ser mayor o igual que 0" in mal.content


def test_record_facts_validates_and_canonicalises_the_children_detail() -> None:
    toolbox = Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger(), case=_case(num_descendientes=1.0))

    rechazado = toolbox.execute(
        _call("registrar_hechos", hechos=[{"nombre": DESCENDIENTES, "valor": "dos hijos de 2 y 8 años"}])
    )
    assert rechazado.is_error and "no se puede leer" in rechazado.content
    rechazado = toolbox.execute(_call("registrar_hechos", hechos=[{"nombre": DESCENDIENTES, "valor": '[{"e": 1}]'}]))
    assert rechazado.is_error and "hijo 1" in rechazado.content

    # As JSON text (what the schema asks for) or as a list: stored as the same canonical text.
    for valor in (json.dumps(HIJOS), HIJOS):
        ok = toolbox.execute(_call("registrar_hechos", hechos=[{"nombre": DESCENDIENTES, "valor": valor}]))
        assert not ok.is_error
        guardado = next(f for f in toolbox.case.facts if f.name == DESCENDIENTES)
        assert guardado.value == descendientes_a_texto(HIJOS) and guardado.value_type.value == "string"

    # The stored detail now feeds every calculation; the stale count of the case does not get in the way.
    resultado = toolbox.execute(_call("listar_beneficios_fiscales", datos={"rendimiento_trabajo_bruto": 30000}))
    assert not resultado.is_error
    beneficios = {b["nombre"]: b for b in json.loads(resultado.content)["beneficios"]}
    assert "2 descendiente(s)" in beneficios["Mínimo por descendientes"]["detalle"]


def test_joint_tool_reports_a_sum_that_contradicts_the_detail() -> None:
    toolbox = Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger())
    a = {"rendimiento_trabajo_bruto": 38000, "cotizaciones_seguridad_social": 2400, DESCENDIENTES: HIJOS}

    ok = toolbox.execute(_call("comparar_declaracion_conjunta", ejercicio=2026, conyuge_a=a, conyuge_b={}))
    assert not ok.is_error and "Conjunta" in ok.content

    mal = toolbox.execute(
        _call("comparar_declaracion_conjunta", ejercicio=2026, conyuge_a=a, conyuge_b={"gastos_guarderia": 600})
    )
    assert mal.is_error and "solo en conyuge_a" in mal.content and "gastos_guarderia" in mal.content


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def test_simulate_endpoint_with_children_detail_from_the_request_and_from_the_case() -> None:
    datos = {"rendimiento_trabajo_bruto": 30000, "cotizaciones_seguridad_social": 1905, "comunidad_autonoma": "Madrid"}
    hijos = [{"edad": 6, "gastos": {"gastos_escolaridad": 8000}}, {"edad": 9, "gastos": {"gastos_escolaridad": 8000}}]

    respuesta = client.post(
        "/simulate",
        json={
            "ejercicio": 2026,
            "datos": {**datos, "num_descendientes": 2, "gastos_escolaridad": 16000},
            "escenarios": [{"nombre": "Sin detalle", "cambios": {}}, {"nombre": "Con detalle", "cambios": {}}],
        },
    )
    assert respuesta.status_code == 200
    sin_detalle = D(respuesta.json()["escenarios"][0]["resultado"]["cuota_liquida"])

    case_id = client.post("/cases", json={"user_id": "u-hijos", "jurisdiction": "ES", "tax_period": "2026"}).json()[
        "case_id"
    ]
    for nombre, valor in (*datos.items(), ("num_descendientes", 1), (DESCENDIENTES, json.dumps(hijos))):
        assert client.put(f"/cases/{case_id}/facts/{nombre}", json={"value": valor}).status_code == 200
    con_caso = client.post("/simulate", json={"case_id": case_id}).json()
    assert D(con_caso["escenarios"][0]["resultado"]["cuota_liquida"]) < sin_detalle

    # What the simulator sends while the user edits: a new detail, or none at all.
    cambios = client.post(
        "/simulate",
        json={
            "case_id": case_id,
            "escenarios": [
                {"nombre": "Tu expediente", "cambios": {}},
                {"nombre": "Simulación", "cambios": {DESCENDIENTES: []}},
            ],
        },
    )
    assert cambios.status_code == 200
    assert D(cambios.json()["escenarios"][1]["ahorro_frente_a_base"]) < 0  # one child (the stored count), no expenses


def test_simulate_endpoint_answers_422_with_a_readable_message() -> None:
    def detalle(datos: dict) -> str:
        respuesta = client.post("/simulate", json={"ejercicio": 2026, "datos": datos})
        assert respuesta.status_code == 422, respuesta.text
        return respuesta.json()["detail"]

    assert "«num_descendientes» (3) no cuadra con el detalle" in detalle({DESCENDIENTES: HIJOS, "num_descendientes": 3})
    assert "es el total de" in detalle({"gastos_deporte_cultura": 10, "gastos_deporte": 30})
    assert "hijo 1: falta la edad" in detalle({DESCENDIENTES: [{}]})
    assert "debe ser una lista" in detalle({DESCENDIENTES: 2})
    assert "fecha_nacimiento: debe ser una fecha" in detalle({DESCENDIENTES: [{"fecha_nacimiento": "ayer"}]})
    assert "edad_conyuge: debe ser menor o igual que 130" in detalle({"edad_conyuge": 200})

    # A detail saved wrong by hand: every later simulation says what to do, with a 422.
    case_id = client.post("/cases", json={"user_id": "u-roto", "jurisdiction": "ES", "tax_period": "2026"}).json()[
        "case_id"
    ]
    client.put(f"/cases/{case_id}/facts/{DESCENDIENTES}", json={"value": "tres"})
    roto = client.post("/simulate", json={"case_id": case_id})
    assert roto.status_code == 422 and "vuelve a guardarlo desde el simulador" in roto.json()["detail"]
    # ...and the simulator can always override it.
    arreglado = client.post(
        "/simulate", json={"case_id": case_id, "datos": {DESCENDIENTES: [{"edad": 1}]}, "escenarios": None}
    )
    assert arreglado.status_code == 200
