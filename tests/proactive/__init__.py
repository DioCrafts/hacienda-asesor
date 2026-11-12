"""Shared builders for the proactive tests."""

from __future__ import annotations

from typing import Any

from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType


def make_case(case_id: str = "c1", tax_period: str = "2026", **facts: Any) -> CaseState:
    def value_type(value: Any) -> FactValueType:
        if isinstance(value, bool):
            return FactValueType.BOOLEAN
        if isinstance(value, int | float):
            return FactValueType.NUMBER
        return FactValueType.STRING

    return CaseState(
        case_id=case_id,
        user_id=f"user-{case_id}",
        jurisdiction="ES",
        tax_period=tax_period,
        facts=[
            Fact(
                fact_id=f"fact_{name}",
                name=name,
                value=float(value) if isinstance(value, int) and not isinstance(value, bool) else value,
                value_type=value_type(value),
                source="test",
                confidence=1.0,
            )
            for name, value in facts.items()
        ],
    )


SALARIED = {
    "rendimiento_trabajo_bruto": 45000,
    "cotizaciones_seguridad_social": 2900,
    "retenciones": 9000,
    "comunidad_autonoma": "Madrid",
}
