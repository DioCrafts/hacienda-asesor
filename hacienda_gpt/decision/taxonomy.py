from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SupportedIntent(str, Enum):
    DECLARACION_IRPF = "declaracion_irpf"
    IVA = "iva"
    AUTONOMO = "autonomo"
    GENERIC_TRIBUTARY = "generic_tributary"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntentFactRequirements:
    required_facts: tuple[str, ...]
    blocking_critical_facts: tuple[str, ...]


INTENT_TAXONOMY: dict[SupportedIntent, IntentFactRequirements] = {
    SupportedIntent.DECLARACION_IRPF: IntentFactRequirements(
        required_facts=(
            "residencia_fiscal",
            "periodo_fiscal",
            "tipo_renta",
            "importe_renta_aproximado",
        ),
        blocking_critical_facts=(
            "residencia_fiscal",
            "periodo_fiscal",
            "tipo_renta",
        ),
    ),
    SupportedIntent.IVA: IntentFactRequirements(
        required_facts=(
            "residencia_fiscal",
            "alta_actividad_economica",
            "periodicidad_iva",
            "periodo_fiscal",
        ),
        blocking_critical_facts=(
            "residencia_fiscal",
            "alta_actividad_economica",
            "periodicidad_iva",
        ),
    ),
    SupportedIntent.AUTONOMO: IntentFactRequirements(
        required_facts=(
            "residencia_fiscal",
            "alta_actividad_economica",
            "fecha_inicio_actividad",
            "regimen_cotizacion",
        ),
        blocking_critical_facts=(
            "residencia_fiscal",
            "alta_actividad_economica",
            "fecha_inicio_actividad",
        ),
    ),
    SupportedIntent.GENERIC_TRIBUTARY: IntentFactRequirements(
        required_facts=(
            "residencia_fiscal",
            "periodo_fiscal",
            "tema_tributario",
        ),
        blocking_critical_facts=(
            "residencia_fiscal",
            "tema_tributario",
        ),
    ),
    SupportedIntent.UNKNOWN: IntentFactRequirements(
        required_facts=("tema_tributario",),
        blocking_critical_facts=("tema_tributario",),
    ),
}


def required_facts_for_intent(intent: SupportedIntent) -> tuple[str, ...]:
    return INTENT_TAXONOMY[intent].required_facts


def blocking_facts_for_intent(intent: SupportedIntent) -> tuple[str, ...]:
    return INTENT_TAXONOMY[intent].blocking_critical_facts
