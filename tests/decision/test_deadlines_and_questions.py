from datetime import UTC, date, datetime, timedelta

from hacienda_gpt.decision.interpreter import interpret_turn
from hacienda_gpt.decision.planner import Planner
from hacienda_gpt.decision.rules import ObligationDeadline
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType, ObligationCandidate, RiskLevel


def _fact(name: str, value) -> Fact:
    value_type = FactValueType.BOOLEAN if isinstance(value, bool) else FactValueType.STRING
    return Fact(fact_id=f"fact_{name}", name=name, value=value, value_type=value_type, source="t", confidence=0.9)


def _case(tax_period: str, **facts) -> CaseState:
    return CaseState(
        case_id="c",
        user_id="u",
        jurisdiction="ES",
        tax_period=tax_period,
        facts=[_fact(name, value) for name, value in facts.items()],
    )


def test_deadline_resolves_relative_to_fiscal_year() -> None:
    assert ObligationDeadline(month=6, day=30, year_offset=1).resolve(2025) == date(2026, 6, 30)
    assert ObligationDeadline(month=2, day=30).resolve(2025) is None  # impossible date → no deadline, not a crash


def test_irpf_obligation_is_due_the_following_june_not_on_december_31() -> None:
    case = _case("2025", residencia_fiscal="ES", periodo_fiscal="2025", tipo_renta="trabajo", menciona_ingresos=True)

    (obligation,) = evaluate_rules(case_state=case, recent_facts=[]).candidate_obligations
    plan = Planner().plan(case, [obligation])

    assert obligation.due_date == date(2026, 6, 30)
    assert "campaña de Renta" in obligation.due_date_note
    assert plan.ranked_actions[0].action.due_date == date(2026, 6, 30)


def test_rules_cover_non_prescribed_past_years() -> None:
    case = _case("2022", residencia_fiscal="ES", periodo_fiscal="2022", tipo_renta="trabajo", menciona_ingresos=True)
    assert [o.obligation_id for o in evaluate_rules(case_state=case, recent_facts=[]).candidate_obligations] == [
        "obl_irpf_candidate"
    ]


def test_urgency_follows_the_real_deadline() -> None:
    planner = Planner()
    today = datetime.now(UTC).date()

    def urgency(due: date | None) -> float:
        obligation = ObligationCandidate(
            obligation_id="o",
            title="t",
            description="d",
            jurisdiction="ES",
            tax_period=str(today.year),
            risk_level=RiskLevel.MEDIUM,
            confidence=0.5,
            due_date=due,
        )
        return planner.plan(_case(str(today.year)), [obligation]).ranked_actions[0].score_breakdown["urgency"]

    assert urgency(today - timedelta(days=1)) == 1.0
    assert urgency(today + timedelta(days=10)) == 0.9
    assert urgency(today + timedelta(days=60)) == 0.7
    assert urgency(today + timedelta(days=300)) == 0.4
    assert urgency(None) == 0.4


def test_iva_intent_asks_for_every_fact_its_rule_requires() -> None:
    # The IVA rule needs alta_actividad_economica + periodicidad_iva; neither had
    # a question, so the rule could never fire unless the user volunteered them.
    result = interpret_turn("Tengo dudas con el IVA del modelo 303", [], None)

    targets = {question.target_fact for question in result.next_questions}
    assert {"residencia_fiscal", "alta_actividad_economica", "periodicidad_iva", "periodo_fiscal"} <= targets
    assert "tipo_renta" not in targets  # not required for IVA


def test_known_facts_are_not_asked_again() -> None:
    case = _case("2025", alta_actividad_economica=True, periodicidad_iva="trimestral", residencia_fiscal="ES")
    result = interpret_turn("Tengo dudas con el IVA de 2025", [], case)
    assert {q.target_fact for q in result.next_questions} == set()
