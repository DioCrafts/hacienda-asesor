from __future__ import annotations

import re

from hacienda_gpt.decision.schemas import Fact, FactValueType, MissingFact, RiskLevel


def detect_facts_and_missing(user_input: str) -> tuple[list[Fact], list[MissingFact]]:
    """Lightweight heuristic extractor for UI debug mode.

    This is intentionally conservative and only extracts a minimal set of facts from
    explicit user wording. It does not provide tax determination by itself.
    """

    text = user_input.lower()
    facts: list[Fact] = []
    missing: list[MissingFact] = []

    if any(token in text for token in ["residente en españa", "residencia fiscal en españa", "vivo en españa"]):
        facts.append(
            Fact(
                fact_id="fact_residencia_fiscal",
                name="residencia_fiscal",
                value="ES",
                value_type=FactValueType.STRING,
                source="user_input",
                confidence=0.8,
            )
        )

    if re.search(r"\birpf\b|\brenta\b", text):
        facts.append(
            Fact(
                fact_id="fact_intent_irpf",
                name="intencion_irpf",
                value=True,
                value_type=FactValueType.BOOLEAN,
                source="user_input",
                confidence=0.75,
            )
        )

    if "ingres" not in text and "rendimiento" not in text:
        missing.append(
            MissingFact(
                fact_name="tipo_renta",
                reason="necesario_para_clasificar_posible_obligacion",
                priority=RiskLevel.HIGH,
            )
        )

    return facts, missing
