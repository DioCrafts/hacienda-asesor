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
