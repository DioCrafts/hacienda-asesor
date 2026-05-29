from datetime import UTC, datetime

from hacienda_gpt.decision.interpreter import QuestionPrompt
from hacienda_gpt.decision.question_policy import QuestionPolicy
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType, RiskLevel
from hacienda_gpt.decision.taxonomy import SupportedIntent


def _case_with_known_and_gave_up() -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
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
        gave_up_facts=["tipo_renta"],
        created_at=now,
        updated_at=now,
    )


def test_question_policy_avoids_known_and_gave_up_facts() -> None:
    policy = QuestionPolicy()
    case = _case_with_known_and_gave_up()

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

    assert [q.question_id for q in result.selected_questions] == ["q3"]
    assert result.newly_gave_up == []


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


def test_question_policy_rephrases_after_first_ask() -> None:
    now = datetime.now(UTC)
    case = CaseState(
        case_id="c3",
        user_id="u3",
        jurisdiction="ES",
        tax_period="2025",
        facts=[],
        ask_counts={"tipo_renta": 1},
        created_at=now,
        updated_at=now,
    )

    questions = [
        QuestionPrompt(
            question_id="q_tipo_renta",
            question_text="¿Qué tipo de ingresos has tenido (trabajo, actividad económica, capital u otros)?",
            target_fact="tipo_renta",
            reason="critical",
            priority=RiskLevel.HIGH,
        )
    ]

    result = QuestionPolicy().select_next_questions(case, SupportedIntent.DECLARACION_IRPF, questions, max_questions=1)

    assert len(result.selected_questions) == 1
    rephrased = result.selected_questions[0]
    assert rephrased.target_fact == "tipo_renta"
    # The text must be one of the alternatives, not the original phrasing.
    assert (
        rephrased.question_text != "¿Qué tipo de ingresos has tenido (trabajo, actividad económica, capital u otros)?"
    )
    assert rephrased.question_text in QuestionPolicy.REPHRASING_TABLE["tipo_renta"]


def test_question_policy_gives_up_after_max_asks() -> None:
    now = datetime.now(UTC)
    case = CaseState(
        case_id="c4",
        user_id="u4",
        jurisdiction="ES",
        tax_period="2025",
        facts=[],
        ask_counts={"tipo_renta": QuestionPolicy.MAX_ASKS_PER_FACT},
        created_at=now,
        updated_at=now,
    )

    questions = [
        QuestionPrompt(
            question_id="q_tipo_renta",
            question_text="¿Qué tipo de ingresos has tenido?",
            target_fact="tipo_renta",
            reason="critical",
            priority=RiskLevel.HIGH,
        )
    ]

    result = QuestionPolicy().select_next_questions(case, SupportedIntent.DECLARACION_IRPF, questions, max_questions=1)

    assert result.selected_questions == []
    assert result.newly_gave_up == ["tipo_renta"]
