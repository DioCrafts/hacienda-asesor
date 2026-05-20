from hacienda_gpt.decision.interpreter_light import detect_facts_and_missing


def test_detect_facts_extracts_residency_and_intent() -> None:
    facts, missing = detect_facts_and_missing("Soy residente en España y tengo dudas sobre IRPF")
    names = {fact.name for fact in facts}
    assert "residencia_fiscal" in names
    assert "intencion_irpf" in names
    assert len(missing) == 1


def test_detect_facts_no_missing_when_income_mentioned() -> None:
    facts, missing = detect_facts_and_missing("Tengo ingresos del trabajo y quiero revisar la renta")
    assert len(facts) >= 1
    assert missing == []
