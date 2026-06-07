from fastapi.testclient import TestClient
from langchain_core.documents import Document
import pytest

from hacienda_gpt.api.api import app, get_fact_extractor, get_qa_chain
from hacienda_gpt.decision.fact_extractor import ExtractionPayload

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
    # The web UI renders full obligation objects; they must stay in sync
    # with the id list the response also exposes.
    assert isinstance(body["obligations"], list)
    assert [o["obligation_id"] for o in body["obligations"]] == body["candidate_obligation_ids"]


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


def test_post_turn_forwards_chat_history_to_extractor() -> None:
    captured: dict = {}

    class _SpyExtractor:
        def extract(self, user_input, chat_history, current_case_state):
            captured["user_input"] = user_input
            captured["chat_history"] = chat_history
            return ExtractionPayload(intent="iva", intent_confidence=0.5, extracted_facts=[])

    created = client.post("/cases", json={"user_id": "uh", "jurisdiction": "ES", "tax_period": "2025"})
    case_id = created.json()["case_id"]

    history = [
        {"role": "user", "content": "Tengo una empresa"},
        {"role": "assistant", "content": "De acuerdo, cuéntame más."},
    ]
    app.dependency_overrides[get_fact_extractor] = lambda: _SpyExtractor()
    try:
        turn = client.post(
            f"/cases/{case_id}/turn",
            json={"user_input": "¿Y el IVA de 2024?", "chat_history": history},
        )
    finally:
        app.dependency_overrides.pop(get_fact_extractor, None)

    assert turn.status_code == 200
    assert captured["user_input"] == "¿Y el IVA de 2024?"
    assert captured["chat_history"] == history


def test_post_turn_chat_history_defaults_to_empty() -> None:
    created = client.post("/cases", json={"user_id": "uh2", "jurisdiction": "ES", "tax_period": "2025"})
    case_id = created.json()["case_id"]
    # Backward compatible: callers may still omit chat_history entirely.
    turn = client.post(f"/cases/{case_id}/turn", json={"user_input": "Dudas de IRPF"})
    assert turn.status_code == 200


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


def test_get_fact_extractor_is_cached() -> None:
    from hacienda_gpt.api import api

    api._build_fact_extractor.cache_clear()
    try:
        assert api.get_fact_extractor() is api.get_fact_extractor()
    finally:
        api._build_fact_extractor.cache_clear()


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


def test_qa_returns_503_when_chain_build_fails(monkeypatch) -> None:
    # No get_qa_chain override: exercise the real dependency, which must turn a
    # failed build (e.g. missing FAISS index / OPENAI_API_KEY) into a clean 503
    # instead of an opaque 500.
    from hacienda_gpt.api import api

    def _boom():
        raise RuntimeError("missing FAISS index")

    monkeypatch.setattr(api, "_build_qa_chain", _boom)
    r = client.post("/qa", json={"query": "¿IRPF?", "chat_history": []})
    assert r.status_code == 503
    assert "missing FAISS index" in r.json()["detail"]


def test_qa_returns_503_when_answering_fails() -> None:
    class _BoomChain:
        def invoke(self, inputs: dict):
            raise RuntimeError("OpenAI unavailable")

    app.dependency_overrides[get_qa_chain] = lambda: _BoomChain()
    try:
        r = client.post("/qa", json={"query": "¿IRPF?", "chat_history": []})
    finally:
        app.dependency_overrides.pop(get_qa_chain, None)

    assert r.status_code == 503
    assert "OpenAI unavailable" in r.json()["detail"]


def test_thread_safe_singleton_caches_build_failure() -> None:
    # Negative caching: a broken build must NOT re-run the expensive factory on
    # every call (the multi-GB embedder reload storm). cache_clear() recovers.
    from hacienda_gpt.api.api import _thread_safe_singleton

    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        raise RuntimeError("boom")

    cached = _thread_safe_singleton(factory)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            cached()
    assert calls["n"] == 1  # failure cached: factory ran once, not per call

    cached.cache_clear()
    with pytest.raises(RuntimeError):
        cached()
    assert calls["n"] == 2  # cleared -> rebuilt
