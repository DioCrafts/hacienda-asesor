from datetime import UTC, datetime

from hacienda_gpt.decision.interpreter import QuestionPrompt
from hacienda_gpt.decision.question_policy import QuestionPolicy
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType, RiskLevel
from hacienda_gpt.decision.taxonomy import SupportedIntent


def _case_with_known_and_asked() -> CaseState:
    now = datetime.now(UTC)
    case = CaseState(
        case_id="c1",
        user_id="u1",
        jurisdiction="ES",
        tax_period="2025",
        facts=[
            Fact(
                fact_id="f1",
                name="residencia_fiscal",
                value="ES",
                value_type=FactValueType.STRING,
                source="user_input",
                confidence=0.9,
            )
        ],
        created_at=now,
        updated_at=now,
    )
    # emulate asked facts memory managed by orchestrator layer
    case.__pydantic_extra__ = {"asked_facts": ["tipo_renta"]}
    return case


def test_question_policy_avoids_redundant_and_non_critical_questions() -> None:
    policy = QuestionPolicy()
    case = _case_with_known_and_asked()

    questions = [
        QuestionPrompt(
            question_id="q1",
            question_text="¿Cuál es tu residencia fiscal?",
            target_fact="residencia_fiscal",
            reason="critical",
            priority=RiskLevel.HIGH,
        ),
        QuestionPrompt(
            question_id="q2",
            question_text="¿Qué tipo de renta tienes?",
            target_fact="tipo_renta",
            reason="critical",
            priority=RiskLevel.HIGH,
        ),
        QuestionPrompt(
            question_id="q3",
            question_text="¿Cuál es el importe aproximado?",
            target_fact="importe_renta_aproximado",
            reason="useful",
            priority=RiskLevel.MEDIUM,
        ),
    ]

    result = policy.select_next_questions(case, SupportedIntent.DECLARACION_IRPF, questions, max_questions=2)

    # residencia already known, tipo_renta already asked -> filtered out
    assert [q.question_id for q in result.selected_questions] == ["q3"]


def test_question_policy_reduces_turns_by_picking_highest_gain_first() -> None:
    now = datetime.now(UTC)
    case = CaseState(
        case_id="c2",
        user_id="u2",
        jurisdiction="ES",
        tax_period="2025",
        facts=[],
        created_at=now,
        updated_at=now,
    )

    questions = [
        QuestionPrompt(
            question_id="q_low",
            question_text="Dato opcional",
            target_fact="tema_tributario",
            reason="generic",
            priority=RiskLevel.LOW,
        ),
        QuestionPrompt(
            question_id="q_high",
            question_text="¿Qué tipo de renta tienes?",
            target_fact="tipo_renta",
            reason="critical",
            priority=RiskLevel.HIGH,
        ),
    ]

    result = QuestionPolicy().select_next_questions(
        case,
        SupportedIntent.DECLARACION_IRPF,
        questions,
        max_questions=1,
    )

    assert len(result.selected_questions) == 1
    assert result.selected_questions[0].question_id == "q_high"
