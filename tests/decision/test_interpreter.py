from hacienda_gpt.decision.interpreter import Intent, interpret_turn
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType


def test_interpret_turn_detects_irpf_intent_in_colloquial_spanish() -> None:
    result = interpret_turn(
        user_input="Oye, tengo un lío con la renta de 2025, ¿me toca presentar IRPF?",
        chat_history=[],
        current_case_state=None,
    )
    assert result.intent is Intent.DECLARACION_IRPF
    assert result.confidence > 0.8


def test_interpret_turn_detects_iva_intent_and_missing_residency() -> None:
    result = interpret_turn(
        user_input="Soy freelance y no sé si el modelo 303 del IVA lo presento mensual o trimestral",
        chat_history=[],
        current_case_state=None,
    )
    assert result.intent is Intent.IVA
    missing = {item.fact_name for item in result.missing_facts}
    assert "residencia_fiscal" in missing


def test_interpret_turn_merges_facts_from_current_case_state() -> None:
    state = CaseState(
        case_id="case1",
        user_id="u1",
        jurisdiction="ES",
        tax_period="2025",
        facts=[
            Fact(
                fact_id="fact_residencia_fiscal",
                name="residencia_fiscal",
                value="ES",
                value_type=FactValueType.STRING,
                source="user_input",
                confidence=0.9,
            )
        ],
    )

    result = interpret_turn(
        user_input="Tengo ingresos de trabajo este año",
        chat_history=[{"role": "assistant", "content": "cuéntame más"}],
        current_case_state=state,
    )
    names = {fact.name for fact in result.extracted_facts}
    assert "residencia_fiscal" in names
    assert "menciona_ingresos" in names


def test_interpret_turn_unknown_intent_adds_uncertainty_and_questions() -> None:
    result = interpret_turn(
        user_input="Hola, necesito ayuda con unos papeles",
        chat_history=[],
        current_case_state=None,
    )
    assert result.intent is Intent.UNKNOWN
    assert any(item.code == "intent_ambiguous" for item in result.uncertainties)
    assert len(result.next_questions) >= 1
