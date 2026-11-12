from fastapi.testclient import TestClient

from hacienda_gpt.agent.agent import TaxAgent
from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.api.api import app, get_agent
from .fakes import FakeSearcher, ScriptedProvider, corpus, text_turn, tool_turn, verdict

client = TestClient(app)


def _override(provider: ScriptedProvider) -> None:
    app.dependency_overrides[get_agent] = lambda: TaxAgent(provider=provider, searcher=FakeSearcher(corpus()))


def teardown_function() -> None:
    app.dependency_overrides.pop(get_agent, None)


def test_agent_endpoint_returns_verified_answer_with_citations_and_trace() -> None:
    _override(
        ScriptedProvider(
            [
                tool_turn(("buscar_fuentes", {"consulta": "plazo reinversión"})),
                text_turn("El plazo es de dos años [F2]."),
            ],
            [verdict("respuesta", ("Plazo dos años", ["F2"], "respaldada"))],
        )
    )

    response = client.post("/agent", json={"query": "¿Plazo para reinvertir?"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "verified"
    assert body["answer"] == "El plazo es de dos años [F2]."
    assert body["citations"][0]["fragment_id"] == "F2" and body["citations"][0]["authority"] == "DGT"
    assert body["tool_trace"][0]["tool"] == "buscar_fuentes"
    assert body["claims"][0]["verdict"] == "respaldada"
    assert body["case_id"] is None and body["case_updated"] is False
    assert body["obligations"] == []
    assert "updated_case" not in body


def test_agent_endpoint_persists_case_changes_and_audits() -> None:
    case_id = client.post("/cases", json={"user_id": "u-agent", "jurisdiction": "ES", "tax_period": "2025"}).json()[
        "case_id"
    ]
    _override(
        ScriptedProvider(
            [
                tool_turn(("registrar_hechos", {"hechos": [{"nombre": "residencia_fiscal", "valor": "ES"}]})),
                text_turn("¿Qué tipo de ingresos has tenido?"),
            ],
            [verdict("pregunta_aclaratoria")],
        )
    )

    response = client.post("/agent", json={"query": "Vivo en Madrid", "case_id": case_id})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "clarification" and body["case_updated"] is True and body["case_id"] == case_id

    stored = client.get(f"/cases/{case_id}").json()
    assert [(f["name"], f["value"]) for f in stored["facts"]] == [("residencia_fiscal", "ES")]
    events = client.get(f"/cases/{case_id}/audit").json()["events"]
    agent_events = [e for e in events if e.get("event_type") == "agent_answer"]
    assert len(agent_events) == 1
    assert agent_events[0]["tool_trace"][0]["tool"] == "registrar_hechos"
    assert agent_events[0]["mode"] == "clarification"


def test_agent_endpoint_errors() -> None:
    _override(ScriptedProvider([]))
    assert client.post("/agent", json={"query": "x", "case_id": "case_nope"}).status_code == 404
    assert client.post("/agent", json={"query": ""}).status_code == 422

    class Failing(ScriptedProvider):
        def start(self, **kwargs):
            raise ProviderError("Claude: credenciales no válidas")

    _override(Failing([]))
    failed = client.post("/agent", json={"query": "¿IVA?"})
    assert failed.status_code == 503 and "credenciales" in failed.json()["detail"]


def test_agent_endpoint_returns_case_obligations_after_rules_run() -> None:
    case_id = client.post("/cases", json={"user_id": "u-obl", "jurisdiction": "ES", "tax_period": "2025"}).json()[
        "case_id"
    ]
    facts = [
        {"nombre": "residencia_fiscal", "valor": "ES"},
        {"nombre": "periodo_fiscal", "valor": "2025"},
        {"nombre": "tipo_renta", "valor": "trabajo"},
        {"nombre": "menciona_ingresos", "valor": True},
    ]
    _override(
        ScriptedProvider(
            [
                tool_turn(("registrar_hechos", {"hechos": facts})),
                tool_turn(("evaluar_obligaciones", {})),
                text_turn("¿Cuánto ingresaste aproximadamente?"),
            ],
            [verdict("pregunta_aclaratoria")],
        )
    )

    body = client.post("/agent", json={"query": "Trabajo en España en 2025", "case_id": case_id}).json()

    assert [o["obligation_id"] for o in body["obligations"]] == ["obl_irpf_candidate"]
    assert body["obligations"][0]["due_date"] == "2026-06-30"
    stored = client.get(f"/cases/{case_id}").json()
    assert [o["obligation_id"] for o in stored["obligation_candidates"]] == ["obl_irpf_candidate"]


def _stream(payload: dict) -> list[dict]:
    import json

    response = client.post("/agent/stream", json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line]


def test_agent_stream_emits_progress_then_one_result() -> None:
    _override(
        ScriptedProvider(
            [
                tool_turn(("buscar_fuentes", {"consulta": "plazo reinversión"})),
                text_turn("El plazo es de dos años [F2]."),
            ],
            [verdict("respuesta", ("Plazo dos años", ["F2"], "respaldada"))],
        )
    )

    events = _stream({"query": "¿Plazo para reinvertir?"})

    progress = [e["data"] for e in events if e["event"] == "progress"]
    assert [p["type"] for p in progress] == [
        "thinking",
        "tool_call",
        "tool_result",
        "thinking",
        "text_delta",
        "text_delta",
        "verifying",
    ]
    # The draft answer is streamed chunk by chunk before it is verified.
    assert progress[4]["text"] + progress[5]["text"] == "El plazo es de dos años [F2]."
    assert progress[1] == {
        "type": "tool_call",
        "tool": "buscar_fuentes",
        "arguments": {"consulta": "plazo reinversión"},
    }
    assert progress[2]["summary"].startswith("3 fragmentos")
    assert progress[6] == {"type": "verifying", "citations": 1}

    assert [e["event"] for e in events if e["event"] != "progress"] == ["result"]
    result = events[-1]["data"]
    assert result["mode"] == "verified" and result["citations"][0]["fragment_id"] == "F2"
    assert result["facts"] == [] and result["tax_period"] is None


def test_agent_stream_reports_provider_failure_as_terminal_error_line() -> None:
    class Failing(ScriptedProvider):
        def start(self, **kwargs):
            raise ProviderError("Claude: límite de peticiones alcanzado")

    _override(Failing([]))
    events = _stream({"query": "¿IVA?"})
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["status"] == 503 and "límite" in events[-1]["data"]["detail"]
    # A missing case is still a plain HTTP error, before any streaming starts.
    assert client.post("/agent/stream", json={"query": "x", "case_id": "case_nope"}).status_code == 404


def test_agent_response_exposes_case_facts_and_user_can_delete_one() -> None:
    case_id = client.post("/cases", json={"user_id": "u-facts", "jurisdiction": "ES", "tax_period": "2025"}).json()[
        "case_id"
    ]
    facts = [
        {"nombre": "residencia_fiscal", "valor": "ES"},
        {"nombre": "periodo_fiscal", "valor": "2025"},
        {"nombre": "tipo_renta", "valor": "trabajo"},
        {"nombre": "menciona_ingresos", "valor": True},
    ]
    _override(
        ScriptedProvider(
            [
                tool_turn(("registrar_hechos", {"hechos": facts})),
                tool_turn(("evaluar_obligaciones", {})),
                text_turn("¿Cuánto ingresaste?"),
            ],
            [verdict("pregunta_aclaratoria")],
        )
    )
    body = client.post("/agent", json={"query": "Trabajo en España", "case_id": case_id}).json()
    assert {f["name"] for f in body["facts"]} == {
        "residencia_fiscal",
        "periodo_fiscal",
        "tipo_renta",
        "menciona_ingresos",
    }
    assert body["tax_period"] == "2025" and len(body["obligations"]) == 1

    removed = client.delete(f"/cases/{case_id}/facts/tipo_renta")
    assert removed.status_code == 200
    assert "tipo_renta" not in {f["name"] for f in removed.json()["facts"]}
    # The IRPF rule needs tipo_renta: without it the obligation no longer stands.
    assert removed.json()["obligation_candidates"] == []
    events = client.get(f"/cases/{case_id}/audit").json()["events"]
    assert any(e.get("event_type") == "fact_removed_by_user" and e["fact_name"] == "tipo_renta" for e in events)

    assert client.delete(f"/cases/{case_id}/facts/tipo_renta").status_code == 404
    assert client.delete("/cases/case_nope/facts/tipo_renta").status_code == 404


def test_user_can_edit_and_add_case_facts() -> None:
    case_id = client.post("/cases", json={"user_id": "u-edit", "jurisdiction": "ES", "tax_period": "2025"}).json()[
        "case_id"
    ]

    def put(name: str, value):
        return client.put(f"/cases/{case_id}/facts/{name}", json={"value": value})

    assert put("residencia_fiscal", "ES").status_code == 200
    assert put("tipo_renta", "trabajo").status_code == 200
    assert put("menciona_ingresos", True).status_code == 200
    # Typed as the user would: Spanish thousands separator and currency sign.
    amount = put("importe_renta_aproximado", "45.000 €").json()
    assert {f["name"]: f["value"] for f in amount["facts"]}["importe_renta_aproximado"] == 45000.0

    # Setting the fiscal year moves the case's period and makes the IRPF rule fire.
    edited = put("periodo_fiscal", "2024").json()
    assert edited["tax_period"] == "2024"
    assert [o["obligation_id"] for o in edited["obligation_candidates"]] == ["obl_irpf_candidate"]
    assert edited["obligation_candidates"][0]["due_date"] == "2025-06-30"

    # Editing replaces, never duplicates.
    replaced = put("tipo_renta", "capital_mobiliario").json()
    assert [f["value"] for f in replaced["facts"] if f["name"] == "tipo_renta"] == ["capital_mobiliario"]
    assert next(f for f in replaced["facts"] if f["name"] == "tipo_renta")["source"] == "user_edit"

    assert put("importe_renta_aproximado", "mucho").status_code == 422
    assert put("periodo_fiscal", "el año pasado").status_code == 422
    assert put("periodo_fiscal", "1800").status_code == 422
    assert put("color_favorito", "azul").status_code == 404
    assert client.put("/cases/case_nope/facts/tipo_renta", json={"value": "trabajo"}).status_code == 404

    events = client.get(f"/cases/{case_id}/audit").json()["events"]
    assert sum(1 for e in events if e.get("event_type") == "fact_edited_by_user") == 6
