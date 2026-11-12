"""Calendar, simulator and document ingestion (step 4)."""

import base64
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ProviderError, ToolCall
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.api.api import app, get_document_provider
from hacienda_gpt.tax.calendario import proximos_vencimientos
from hacienda_gpt.tax.documentos import DocumentoNoLegibleError, analizar_documento, decode_upload, extract_text
from hacienda_gpt.tax.profile import DatosInvalidosError
from hacienda_gpt.tax.simulador import simular

D = Decimal
client = TestClient(app)


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #


def test_employee_sees_only_universal_deadlines() -> None:
    items = proximos_vencimientos({"categoria_contribuyente": "asalariado"}, hoy=date(2026, 11, 12))

    assert [(v.id.split(":")[0], v.fecha) for v in items] == [
        ("irpf_cierre_ejercicio", date(2026, 12, 31)),
        ("irpf_declaracion", date(2027, 6, 30)),
    ]
    year_end, renta = items
    assert year_end.periodo == "Ejercicio 2026" and year_end.dias_restantes == 49 and year_end.verificado
    assert renta.periodo == "Renta 2026" and renta.modelo == "100"
    # The campaign date comes from a yearly order that was not checked: flagged.
    assert renta.verificado is False and "orden" in renta.nota


def test_self_employed_gets_quarterly_deadlines_shifted_off_weekends() -> None:
    facts = {"alta_actividad_economica": True, "periodicidad_iva": "trimestral"}
    items = proximos_vencimientos(facts, hoy=date(2026, 11, 12), dias=300)
    by = {(v.id.split(":")[0], v.periodo): v for v in items}

    q4_vat = by[("iva_trimestral", "4T 2026")]
    # 30 January 2027 is a Saturday → moved to Monday 1 February.
    assert q4_vat.fecha == date(2027, 2, 1) and q4_vat.ajustada and "fin de semana" in q4_vat.nota
    assert by[("irpf_pago_fraccionado", "4T 2026")].fecha == date(2027, 2, 1)
    assert (
        by[("iva_trimestral", "1T 2027")].fecha == date(2027, 4, 20) and not by[("iva_trimestral", "1T 2027")].ajustada
    )
    assert by[("iva_trimestral", "2T 2027")].fecha == date(2027, 7, 20)
    assert [v.fecha for v in items] == sorted(v.fecha for v in items)

    monthly = proximos_vencimientos(
        {"alta_actividad_economica": True, "periodicidad_iva": "mensual"}, hoy=date(2026, 11, 12)
    )
    ids = {v.id.split(":")[0] for v in monthly}
    assert "iva_trimestral" not in ids and "irpf_pago_fraccionado" in ids


def test_foreign_assets_add_modelo_720_and_past_deadlines_are_excluded() -> None:
    items = proximos_vencimientos({"bienes_en_el_extranjero": True}, hoy=date(2027, 1, 10), dias=120)
    assert [(v.modelo, v.fecha, v.periodo) for v in items] == [("720", date(2027, 3, 31), "Ejercicio 2026")]
    assert "50.000" in items[0].detalle


def test_calendar_tool_returns_citable_dates() -> None:
    class NoSearch:
        def search(self, query, *, k, filters):
            return []

    ledger = EvidenceLedger()
    outcome = Toolbox(searcher=NoSearch(), ledger=ledger).execute(
        ToolCall(id="c", name="calendario_fiscal", arguments={"dias": 400})
    )
    assert not outcome.is_error and outcome.content.startswith("[F1] Calendario fiscal")
    assert "Declaración de la Renta" in outcome.content and "festivos" in outcome.content
    assert ledger.get("F1").source_type == "calculo"


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #


def test_simulator_compares_scenarios_and_lists_levers() -> None:
    sim = simular(
        ejercicio=2026,
        facts={"rendimiento_trabajo_bruto": 50000.0, "cotizaciones_seguridad_social": 3175.0},
        datos={"retenciones": 9000},
        escenarios=[
            {"nombre": "Actual", "cambios": {}},
            {"nombre": "Con plan", "cambios": {"aportacion_plan_pensiones": 1500}},
            {"nombre": "Más sueldo", "cambios": {"rendimiento_trabajo_bruto": 55000}},
        ],
    )
    assert [e.nombre for e in sim.escenarios] == ["Actual", "Con plan", "Más sueldo"]
    assert [e.ahorro_frente_a_base for e in sim.escenarios][:2] == [D("0.00"), D("555.00")]
    assert sim.escenarios[2].ahorro_frente_a_base < 0  # earning more costs more tax
    assert sim.datos_no_aportados == ["comunidad_autonoma"]
    assert {b.id for b in sim.beneficios} >= {"plan_pensiones", "donativos", "deducciones_autonomicas"}
    madrid = simular(ejercicio=2026, datos={"rendimiento_trabajo_bruto": 50000, "comunidad_autonoma": "Madrid"})
    assert madrid.escenarios[0].resultado.comunidad_aplicada == "Comunidad de Madrid"
    assert madrid.datos_no_aportados == ["cotizaciones_seguridad_social", "retenciones"]

    # An interactive simulator judges the levers on the scenario being edited:
    # once the plan is maxed out there, it is no longer offered.
    edited = simular(
        ejercicio=2026,
        escenarios=[
            {"nombre": "Base", "cambios": {}},
            {"nombre": "Editado", "cambios": {"rendimiento_trabajo_bruto": 50000, "aportacion_plan_pensiones": 1500}},
        ],
        beneficios_sobre_ultimo=True,
    )
    assert {b.id: b.estado for b in edited.beneficios}["plan_pensiones"] == "aplicado"
    assert "plan_pensiones" not in {b.id for b in simular(ejercicio=2026).beneficios}  # no income, no lever

    with pytest.raises(DatosInvalidosError):
        simular(ejercicio=2026, escenarios=[{"nombre": "x", "cambios": {"sueldo": 1}}])
    with pytest.raises(DatosInvalidosError):
        simular(ejercicio=2026, escenarios=[{"nombre": "x", "cambios": {}}] * 7)


def test_simulate_endpoint_with_and_without_case() -> None:
    body = client.post(
        "/simulate",
        json={"ejercicio": 2026, "datos": {"rendimiento_trabajo_bruto": 30000, "cotizaciones_seguridad_social": 1905}},
    ).json()
    assert body["escenarios"][0]["resultado"]["cuota_liquida"] == "4939.50"
    assert any("aproximado" in aviso for aviso in body["escenarios"][0]["resultado"]["avisos"])

    case_id = client.post("/cases", json={"user_id": "u-sim", "jurisdiction": "ES", "tax_period": "2026"}).json()[
        "case_id"
    ]
    client.put(f"/cases/{case_id}/facts/rendimiento_trabajo_bruto", json={"value": "50.000 €"})
    with_case = client.post(
        "/simulate",
        json={
            "case_id": case_id,
            "escenarios": [{"nombre": "A", "cambios": {}}, {"nombre": "B", "cambios": {"donativos": 250}}],
        },
    ).json()
    assert with_case["ejercicio"] == 2026
    assert with_case["escenarios"][1]["ahorro_frente_a_base"] == "200.00"

    assert client.post("/simulate", json={"ejercicio": 2019}).status_code == 422
    assert client.post("/simulate", json={"datos": {"sueldo": 1}}).status_code == 422
    assert client.post("/simulate", json={"case_id": "case_nope"}).status_code == 404


def test_calendar_endpoint_follows_the_case_profile() -> None:
    case_id = client.post("/cases", json={"user_id": "u-cal", "jurisdiction": "ES", "tax_period": "2026"}).json()[
        "case_id"
    ]
    before = client.get(f"/cases/{case_id}/calendar").json()
    assert {v["id"].split(":")[0] for v in before["vencimientos"]} == {"irpf_cierre_ejercicio", "irpf_declaracion"}
    assert "festivos" in before["aviso"]

    client.put(f"/cases/{case_id}/facts/alta_actividad_economica", json={"value": True})
    after = client.get(f"/cases/{case_id}/calendar?dias=400").json()
    assert {"iva_trimestral", "irpf_pago_fraccionado"} <= {v["id"].split(":")[0] for v in after["vencimientos"]}
    assert client.get("/cases/case_nope/calendar").status_code == 404


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

CERTIFICADO = """CERTIFICADO DE RETENCIONES E INGRESOS A CUENTA DEL IRPF
Ejercicio 2026
Retribuciones dinerarias íntegras: 38.250,40
Gastos fiscalmente deducibles (Seguridad Social): 2.428,90
Retenciones practicadas: 6.502,57
"""


class _DocProvider:
    name, model = "fake", "fake-1"

    def __init__(self, response) -> None:
        self.response, self.requests = response, []

    def complete_json(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _doc_response(*facts: tuple[str, str, str]) -> dict:
    return {
        "tipo_documento": "Certificado de retenciones",
        "hechos": [{"name": n, "value": v, "evidencia": e} for n, v, e in facts],
    }


def test_document_facts_are_kept_only_when_their_passage_is_in_the_document() -> None:
    provider = _DocProvider(
        _doc_response(
            ("rendimiento_trabajo_bruto", "38250.40", "Retribuciones dinerarias íntegras: 38.250,40"),
            ("retenciones", "6502.57", "retenciones   practicadas:\n6.502,57"),  # whitespace/case differences are fine
            ("periodo_fiscal", "2026", "Ejercicio 2026"),
            ("donativos", "900", "Donativos a entidades: 900,00"),  # passage not in the document → dropped
            ("cotizaciones_seguridad_social", "muchas", "Gastos fiscalmente deducibles (Seguridad Social): 2.428,90"),
            ("tipo_renta", "trabajo", "Retribuciones dinerarias"),  # not a document fact
        )
    )

    analysis = analizar_documento(provider, filename="certificado.txt", data=CERTIFICADO.encode())

    assert {f.name: f.value for f in analysis.hechos_propuestos} == {
        "rendimiento_trabajo_bruto": 38250.40,
        "retenciones": 6502.57,
        "periodo_fiscal": "2026",
    }
    assert analysis.tipo_documento == "Certificado de retenciones" and analysis.paginas == 1
    assert "cotizaciones_seguridad_social" in analysis.avisos[0] and "donativos" in analysis.avisos[0]
    assert "38.250,40" in provider.requests[0]["user"]


def test_unreadable_uploads_are_rejected_with_a_reason() -> None:
    with pytest.raises(DocumentoNoLegibleError, match="base64"):
        decode_upload("¡no es base64!")
    with pytest.raises(DocumentoNoLegibleError, match="vacío"):
        decode_upload("")
    with pytest.raises(DocumentoNoLegibleError, match="Formato no admitido"):
        extract_text(b"PK\x03\x04\x00\x00binary", "nomina.docx")
    with pytest.raises(DocumentoNoLegibleError, match="abrir el PDF"):
        extract_text(b"%PDF-1.7 this is not really a pdf", "roto.pdf")
    assert extract_text("Año 2026 · retención".encode("latin-1"), "a.txt") == ("Año 2026 · retención", 1)


def test_text_is_extracted_from_a_real_pdf() -> None:
    pdfium = pytest.importorskip("pypdfium2")
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(595, 842)
    import io

    buffer = io.BytesIO()
    pdf.save(buffer)
    # A page with no text layer is what a scanned document looks like.
    with pytest.raises(DocumentoNoLegibleError, match="escaneado"):
        extract_text(buffer.getvalue(), "escaneado.pdf")


def test_document_endpoint_proposes_without_writing_to_the_case() -> None:
    case_id = client.post("/cases", json={"user_id": "u-doc", "jurisdiction": "ES", "tax_period": "2026"}).json()[
        "case_id"
    ]
    payload = {"filename": "certificado.txt", "content_base64": base64.b64encode(CERTIFICADO.encode()).decode()}
    app.dependency_overrides[get_document_provider] = lambda: _DocProvider(
        _doc_response(("retenciones", "6502.57", "Retenciones practicadas: 6.502,57"))
    )
    try:
        response = client.post(f"/cases/{case_id}/documents", json=payload)
        assert response.status_code == 200
        assert response.json()["hechos_propuestos"] == [
            {"name": "retenciones", "value": 6502.57, "evidencia": "Retenciones practicadas: 6.502,57"}
        ]
        # Proposals only: the case is untouched until the user confirms.
        assert client.get(f"/cases/{case_id}").json()["facts"] == []
        events = client.get(f"/cases/{case_id}/audit").json()["events"]
        audited = next(e for e in events if e.get("event_type") == "document_analyzed")
        assert audited["proposed_facts"] == ["retenciones"] and "6502" not in str(audited)

        assert client.post(f"/cases/{case_id}/documents", json={**payload, "content_base64": "%%%"}).status_code == 422
        assert client.post("/cases/case_nope/documents", json=payload).status_code == 404
        app.dependency_overrides[get_document_provider] = lambda: _DocProvider(ProviderError("sin red"))
        assert client.post(f"/cases/{case_id}/documents", json=payload).status_code == 503
    finally:
        app.dependency_overrides.pop(get_document_provider, None)
