from hacienda_gpt.decision.taxonomy import (
    INTENT_TAXONOMY,
    SupportedIntent,
    blocking_facts_for_intent,
    required_facts_for_intent,
)


def test_taxonomy_has_expected_supported_intents() -> None:
    assert set(INTENT_TAXONOMY.keys()) == {
        SupportedIntent.DECLARACION_IRPF,
        SupportedIntent.IVA,
        SupportedIntent.AUTONOMO,
        SupportedIntent.GENERIC_TRIBUTARY,
        SupportedIntent.UNKNOWN,
    }


def test_blocking_facts_are_subset_of_required_facts() -> None:
    for intent, requirements in INTENT_TAXONOMY.items():
        assert set(requirements.blocking_critical_facts).issubset(set(requirements.required_facts)), intent


def test_required_and_blocking_helpers_return_non_empty_values() -> None:
    for intent in SupportedIntent:
        assert required_facts_for_intent(intent)
        assert blocking_facts_for_intent(intent)
