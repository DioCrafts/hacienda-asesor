from hacienda_gpt.decision.fact_extractor import ExtractionPayload
from hacienda_gpt.decision.interpreter import Intent, interpret_turn
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType


class _StaticExtractor:
    """Returns a fixed payload regardless of input, for deterministic tests."""

    def __init__(self, payload: ExtractionPayload) -> None:
        self._payload = payload

    def extract(self, user_input, chat_history, current_case_state):
        del user_input, chat_history, current_case_state
        return self._payload


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
    # The merge lives in `all_facts` (accumulated); `extracted_facts` is only
    # this turn's input.
    names = {fact.name for fact in result.all_facts}
    assert "residencia_fiscal" in names
    assert "menciona_ingresos" in names


def test_interpret_turn_separates_turn_facts_from_accumulated() -> None:
    # Regression: `extracted_facts` must hold ONLY this turn's facts, `all_facts`
    # the merge with the case. Conflating them made audit/explainer report prior
    # facts as "detected this turn" and risked overwriting history on persist.
    state = CaseState(
        case_id="c",
        user_id="u",
        jurisdiction="ES",
        tax_period="2025",
        facts=[
            Fact(
                fact_id="fact_residencia_fiscal",
                name="residencia_fiscal",
                value="ES",
                value_type=FactValueType.STRING,
                source="prior_turn",
                confidence=0.9,
            )
        ],
    )

    result = interpret_turn(
        user_input="Tengo ingresos de trabajo este año",
        chat_history=[],
        current_case_state=state,
    )

    turn_names = {f.name for f in result.extracted_facts}
    all_names = {f.name for f in result.all_facts}
    # residencia came from a PRIOR turn -> only in the accumulated view.
    assert "residencia_fiscal" not in turn_names
    assert "residencia_fiscal" in all_names
    # menciona_ingresos came from THIS turn -> in both.
    assert "menciona_ingresos" in turn_names
    assert "menciona_ingresos" in all_names


def test_irpf_asks_tipo_renta_even_when_income_is_mentioned() -> None:
    # Regression for the IRPF deadlock: mentioning income (menciona_ingresos)
    # must NOT suppress the tipo_renta question. The IRPF rule requires
    # tipo_renta, so suppressing it left the rule blocked on a fact the dialog
    # had stopped asking for.
    extractor = _StaticExtractor(
        ExtractionPayload(
            intent="declaracion_irpf",
            intent_confidence=0.9,
            extracted_facts=[
                Fact(
                    fact_id="fact_residencia_fiscal",
                    name="residencia_fiscal",
                    value="ES",
                    value_type=FactValueType.STRING,
                    source="llm_extractor",
                    confidence=0.9,
                ),
                Fact(
                    fact_id="fact_menciona_ingresos",
                    name="menciona_ingresos",
                    value=True,
                    value_type=FactValueType.BOOLEAN,
                    source="llm_extractor",
                    confidence=0.9,
                ),
            ],
        )
    )

    result = interpret_turn(
        user_input="Soy residente en España y tengo ingresos",
        chat_history=[],
        current_case_state=None,
        extractor=extractor,
    )

    missing = {item.fact_name for item in result.missing_facts}
    assert "tipo_renta" in missing
    assert any(q.target_fact == "tipo_renta" for q in result.next_questions)


def test_interpret_turn_unknown_intent_adds_uncertainty_and_questions() -> None:
    result = interpret_turn(
        user_input="Hola, necesito ayuda con unos papeles",
        chat_history=[],
        current_case_state=None,
    )
    assert result.intent is Intent.UNKNOWN
    assert any(item.code == "intent_ambiguous" for item in result.uncertainties)
    assert len(result.next_questions) >= 1
