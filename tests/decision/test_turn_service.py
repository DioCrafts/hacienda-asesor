from datetime import UTC, datetime

from hacienda_gpt.decision.fact_extractor import RegexFactExtractor
from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.decision.turn_service import process_turn, resolve_tax_period


def _fresh_case() -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="case_ts",
        user_id="u",
        jurisdiction="ES",
        tax_period="2025",
        created_at=now,
        updated_at=now,
    )


def test_process_turn_tracks_ask_counts_and_eventually_gives_up() -> None:
    extractor = RegexFactExtractor()
    case = _fresh_case()

    # The same fact-less IRPF query keeps the critical facts missing, so the
    # question policy re-asks the top fact each turn until it gives up.
    asked_fact = None
    for expected_count in (1, 2, 3):
        outcome = process_turn(case=case, user_input="Tengo dudas de IRPF", extractor=extractor)
        case = outcome.case_state
        assert outcome.selected_questions, "policy should ask while critical facts are missing"
        asked_fact = outcome.selected_questions[0].target_fact
        assert case.ask_counts[asked_fact] == expected_count
        assert asked_fact in case.asked_facts

    # Fourth ask exceeds MAX_ASKS_PER_FACT (3): the fact is abandoned and no
    # longer reported as missing.
    outcome = process_turn(case=case, user_input="Tengo dudas de IRPF", extractor=extractor)
    case = outcome.case_state
    assert asked_fact in case.gave_up_facts
    assert all(m.fact_name != asked_fact for m in case.missing_facts)


def test_process_turn_resolves_tax_period_from_extracted_fact() -> None:
    extractor = RegexFactExtractor()
    case = _fresh_case()
    outcome = process_turn(
        case=case,
        user_input="Soy residente en España y consulto el IRPF de 2023",
        extractor=extractor,
    )
    assert outcome.case_state.tax_period == "2023"


def test_resolve_tax_period_defaults_to_case_when_absent() -> None:
    case = _fresh_case()
    assert resolve_tax_period(case, facts=[]) == "2025"
