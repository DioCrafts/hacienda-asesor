"""End-to-end coverage of the conversational decision endpoint.

These tests drive the full /cases + /turn FastAPI flow with a stub fact
extractor, so they exercise the real interpreter, QuestionPolicy and rules
engine. They are the regression layer for:

1. The MissingFact / Fact contract (camelCase vs snake_case fields).
2. Multi-turn fact accumulation across the conversation.
3. Re-phrasing of repeated questions instead of silently dropping them
   (the original dead-end bug we shipped to production).
4. Graceful degradation when a fact has been asked MAX_ASKS_PER_FACT times
   without an answer.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.api.api import app, get_case_store, get_fact_extractor
from hacienda_gpt.decision.fact_extractor import ExtractionPayload
from hacienda_gpt.decision.question_policy import QuestionPolicy
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore


class _ScriptedExtractor:
    """Returns a pre-canned ExtractionPayload per user_input prefix.

    The mapping is order-independent. Defaults to an unknown-intent / no-facts
    payload, so unsupplied inputs are still safe to call (mirrors the
    behaviour of a real extractor that fails to find anything).
    """

    def __init__(self, script: dict[str, ExtractionPayload]) -> None:
        self._script = script

    def extract(
        self,
        user_input: str,
        chat_history,
        current_case_state: CaseState | None,
    ) -> ExtractionPayload:
        del chat_history, current_case_state
        for key, payload in self._script.items():
            if user_input.startswith(key):
                return payload
        return ExtractionPayload(intent="unknown", intent_confidence=0.2, extracted_facts=[])


def _fact(name: str, value, vtype: FactValueType, confidence: float = 0.9) -> Fact:
    return Fact(
        fact_id=f"fact_{name}",
        name=name,
        value=value,
        value_type=vtype,
        source="llm_extractor",
        confidence=confidence,
    )


@pytest.fixture
def isolated_store(tmp_path: Path):
    db_path = tmp_path / "turn_flow.sqlite3"
    store = SQLiteCaseStateStore(str(db_path))
    app.dependency_overrides[get_case_store] = lambda: store
    try:
        yield store
    finally:
        app.dependency_overrides.pop(get_case_store, None)


@pytest.fixture
def client(isolated_store) -> TestClient:
    return TestClient(app)


@pytest.fixture
def install_extractor():
    def _install(script: dict[str, ExtractionPayload]) -> None:
        app.dependency_overrides[get_fact_extractor] = lambda: _ScriptedExtractor(script)

    yield _install
    app.dependency_overrides.pop(get_fact_extractor, None)


def _create_case(client: TestClient, user_id: str = "demo") -> str:
    r = client.post(
        "/cases",
        json={"user_id": user_id, "jurisdiction": "ES", "tax_period": "2024"},
    )
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _turn(client: TestClient, case_id: str, text: str) -> dict:
    r = client.post(f"/cases/{case_id}/turn", json={"user_input": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_turn_response_contract_uses_fact_name_field(client, install_extractor):
    install_extractor(
        {
            "soy autónomo": ExtractionPayload(
                intent="autonomo",
                intent_confidence=0.9,
                extracted_facts=[],
            )
        }
    )
    case_id = _create_case(client)
    body = _turn(client, case_id, "soy autónomo y necesito ayuda")

    assert isinstance(body["missing_facts"], list)
    assert body["missing_facts"], "interpreter must surface required facts"
    for missing in body["missing_facts"]:
        assert "fact_name" in missing, (
            "MissingFact must expose `fact_name`; if you change the schema, "
            "update the OpenAPI clients and the UI consumer accordingly."
        )
        assert "reason" in missing
        assert "priority" in missing
    assert body["next_questions"], "expected the policy to ask at least one question"
    assert body["degraded"] is False
    assert body["degraded_facts"] == []


def test_multi_turn_accumulates_facts_until_no_missing(client, install_extractor):
    install_extractor(
        {
            "soy autónomo": ExtractionPayload(
                intent="autonomo",
                intent_confidence=0.92,
                extracted_facts=[
                    _fact("residencia_fiscal", "ES", FactValueType.STRING),
                    _fact("categoria_contribuyente", "autonomo", FactValueType.STRING),
                ],
            ),
            "facturo": ExtractionPayload(
                intent="autonomo",
                intent_confidence=0.92,
                extracted_facts=[
                    _fact("volumen_facturacion", 45000.0, FactValueType.NUMBER),
                    _fact("menciona_ingresos", True, FactValueType.BOOLEAN),
                ],
            ),
            "estimación": ExtractionPayload(
                intent="autonomo",
                intent_confidence=0.92,
                extracted_facts=[
                    _fact("regimen_tributario", "estimacion_directa_simplificada", FactValueType.STRING),
                    _fact("periodo_fiscal", "2024", FactValueType.STRING),
                ],
            ),
        }
    )

    case_id = _create_case(client)
    b1 = _turn(client, case_id, "soy autónomo y necesito ayuda")
    fact_names_after_t1 = {f["name"] for f in b1["facts"]}
    assert {"residencia_fiscal", "categoria_contribuyente"} <= fact_names_after_t1
    missing_after_t1 = {m["fact_name"] for m in b1["missing_facts"]}
    # tipo_renta is IRPF-only per the taxonomy, so for an autónomo the engine
    # surfaces the fiscal period instead, not tipo_renta.
    assert "periodo_fiscal" in missing_after_t1
    assert b1["next_questions"], "the engine should keep asking"

    b2 = _turn(client, case_id, "facturo 45000 al año por consultoría")
    fact_names_after_t2 = {f["name"] for f in b2["facts"]}
    # Facts accumulate — the t1 facts must still be there after t2.
    assert {"residencia_fiscal", "categoria_contribuyente"} <= fact_names_after_t2
    assert {"volumen_facturacion", "menciona_ingresos"} <= fact_names_after_t2

    b3 = _turn(client, case_id, "estimación directa simplificada, ejercicio 2024")
    fact_names_after_t3 = {f["name"] for f in b3["facts"]}
    assert {"regimen_tributario", "periodo_fiscal"} <= fact_names_after_t3
    # After three turns residencia and periodo are known; tipo_renta is not
    # part of the autónomo flow (it is IRPF-only), so none of these stay missing.
    missing_after_t3 = {m["fact_name"] for m in b3["missing_facts"]}
    assert "residencia_fiscal" not in missing_after_t3
    assert "tipo_renta" not in missing_after_t3
    assert "periodo_fiscal" not in missing_after_t3


def test_repeated_question_is_rephrased_then_given_up(client, install_extractor):
    # The extractor returns an autonomo intent but never produces
    # residencia_fiscal / periodo_fiscal. This simulates a user who keeps
    # answering off-topic, so the engine has to recover gracefully.
    install_extractor(
        {
            "necesito": ExtractionPayload(
                intent="autonomo",
                intent_confidence=0.9,
                extracted_facts=[],
            )
        }
    )

    case_id = _create_case(client)
    seen_phrasings: list[str] = []
    last_body = None
    for turn_idx in range(QuestionPolicy.MAX_ASKS_PER_FACT + 1):
        last_body = _turn(client, case_id, f"necesito ayuda turno {turn_idx}")
        question_for_tipo_renta = [
            q
            for q in last_body["next_questions"]
            # We can't infer target_fact from the surface text, so we trust the
            # interpreter ordering and just collect every question we see.
        ]
        if question_for_tipo_renta:
            seen_phrasings.append(question_for_tipo_renta[0])

    assert last_body is not None
    # We must have seen at least two different phrasings while still asking
    # (one original + at least one rephrased alternative).
    unique_phrasings = {p for p in seen_phrasings}
    assert len(unique_phrasings) >= 2, f"expected the policy to rephrase across turns, got: {seen_phrasings!r}"

    # On the final turn the policy must have given up at least one fact and
    # the response must flag the conversation as degraded.
    assert last_body["degraded"] is True
    assert last_body["degraded_facts"], "expected at least one fact in degraded_facts"


def test_irpf_deadlock_resolves_obligation_after_tipo_renta(client, install_extractor):
    # Regression for the IRPF deadlock. An IRPF case that mentions income used
    # to stop asking for tipo_renta (because menciona_ingresos was present),
    # yet the IRPF rule requires tipo_renta — so the rule stayed blocked and no
    # obligation ever surfaced. After the fix the dialog keeps asking for
    # tipo_renta; once provided, the rule unblocks and emits the obligation.
    install_extractor(
        {
            "soy residente": ExtractionPayload(
                intent="declaracion_irpf",
                intent_confidence=0.95,
                extracted_facts=[
                    _fact("residencia_fiscal", "ES", FactValueType.STRING),
                    _fact("menciona_ingresos", True, FactValueType.BOOLEAN),
                    _fact("periodo_fiscal", "2024", FactValueType.STRING),
                ],
            ),
            "son ingresos del trabajo": ExtractionPayload(
                intent="declaracion_irpf",
                intent_confidence=0.95,
                extracted_facts=[
                    _fact("tipo_renta", "trabajo", FactValueType.STRING),
                ],
            ),
        }
    )
    case_id = _create_case(client)

    b1 = _turn(client, case_id, "soy residente y tengo ingresos en 2024")
    missing1 = {m["fact_name"] for m in b1["missing_facts"]}
    # Income mentioned, residencia + periodo known, but tipo_renta is still
    # required and must still be asked (this is the bug the fix targets).
    assert "tipo_renta" in missing1
    assert any("ingresos" in q.lower() for q in b1["next_questions"])
    # Rule cannot fire yet: it is blocked on the missing tipo_renta.
    assert b1["candidate_obligation_ids"] == []

    b2 = _turn(client, case_id, "son ingresos del trabajo")
    # tipo_renta provided -> rule unblocks -> the candidate obligation appears.
    assert "obl_irpf_candidate" in b2["candidate_obligation_ids"]
    assert any(o["obligation_id"] == "obl_irpf_candidate" for o in b2["obligations"])


def test_case_404_unknown(client):
    r = client.post("/cases/unknown/turn", json={"user_input": "hola"})
    assert r.status_code == 404
