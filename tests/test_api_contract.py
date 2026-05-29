from fastapi.testclient import TestClient
from langchain_core.documents import Document

from hacienda_gpt.api.api import app, get_qa_chain

client = TestClient(app)


class _StubQAChain:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def invoke(self, inputs: dict) -> dict:
        return self.payload


def test_health() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_case_and_get_case_and_audit() -> None:
    created = client.post("/cases", json={"user_id": "u1", "jurisdiction": "ES", "tax_period": "2025"})
    assert created.status_code == 200
    case_id = created.json()["case_id"]

    got = client.get(f"/cases/{case_id}")
    assert got.status_code == 200
    assert got.json()["case_id"] == case_id

    audit = client.get(f"/cases/{case_id}/audit")
    assert audit.status_code == 200
    assert audit.json()["case_id"] == case_id


def test_post_turn_contract() -> None:
    created = client.post("/cases", json={"user_id": "u2", "jurisdiction": "ES", "tax_period": "2025"})
    case_id = created.json()["case_id"]
    turn = client.post(
        f"/cases/{case_id}/turn", json={"user_input": "Soy residente en España y tengo dudas de IRPF 2025"}
    )
    assert turn.status_code == 200
    body = turn.json()
    assert body["case_id"] == case_id
    assert "facts" in body
    assert "missing_facts" in body
    assert "candidate_obligation_ids" in body
    assert "next_questions" in body


def test_post_turn_updates_case_tax_period_from_extracted_fact() -> None:
    created = client.post("/cases", json={"user_id": "u3", "jurisdiction": "ES", "tax_period": "2025"})
    case_id = created.json()["case_id"]

    turn = client.post(
        f"/cases/{case_id}/turn",
        json={"user_input": "Tengo dudas de IRPF para 2024 y soy residente en España"},
    )
    assert turn.status_code == 200

    got = client.get(f"/cases/{case_id}")
    assert got.status_code == 200
    assert got.json()["tax_period"] == "2024"


def test_not_found_case() -> None:
    r = client.get("/cases/does-not-exist")
    assert r.status_code == 404


def test_qa_returns_envelope_with_citations() -> None:
    docs = [
        Document(
            page_content="Residencia fiscal en España...",
            metadata={"title": "Residencia fiscal IRPF", "source_url": "https://sede/x"},
        )
    ]
    app.dependency_overrides[get_qa_chain] = lambda: _StubQAChain(
        {"answer": "Si eres residente, declaras IRPF.", "context": docs}
    )
    try:
        r = client.post("/qa", json={"query": "¿IRPF?", "chat_history": []})
    finally:
        app.dependency_overrides.pop(get_qa_chain, None)

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "cited"
    assert body["citations"][0]["locator"] == "https://sede/x"
    assert "Si eres residente" in body["answer"]


def test_get_case_store_is_cached() -> None:
    from hacienda_gpt.api import api

    api._build_case_store.cache_clear()
    try:
        assert api.get_case_store() is api.get_case_store()
    finally:
        api._build_case_store.cache_clear()


def test_qa_abstains_when_no_context() -> None:
    app.dependency_overrides[get_qa_chain] = lambda: _StubQAChain({"answer": "respuesta", "context": []})
    try:
        r = client.post("/qa", json={"query": "¿Y esto?", "chat_history": []})
    finally:
        app.dependency_overrides.pop(get_qa_chain, None)

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "abstained"
    assert body["raw_answer"] == "respuesta"
    assert body["citations"] == []
