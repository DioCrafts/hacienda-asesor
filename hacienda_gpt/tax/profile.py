"""Bridge between the user's case facts and a calculator scenario.

Profile facts share their names with :class:`IRPFInput` fields on purpose, so a
fact the advisor records ("aportacion_plan_pensiones = 1000") feeds the
calculator with no translation layer to drift out of sync.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from hacienda_gpt.decision.schemas import FactValueType
from hacienda_gpt.tax.irpf import IRPFInput

# Profile facts and their types; merged into the extractor's vocabulary.
PROFILE_FACT_TYPES: dict[str, FactValueType] = {
    "comunidad_autonoma": FactValueType.STRING,
    "estado_civil": FactValueType.STRING,
    "edad": FactValueType.NUMBER,
    "grado_discapacidad": FactValueType.NUMBER,
    "num_descendientes": FactValueType.NUMBER,
    "num_descendientes_menores_3": FactValueType.NUMBER,
    "num_ascendientes_mayores_65": FactValueType.NUMBER,
    "num_ascendientes_mayores_75": FactValueType.NUMBER,
    "rendimiento_trabajo_bruto": FactValueType.NUMBER,
    "cotizaciones_seguridad_social": FactValueType.NUMBER,
    "retenciones": FactValueType.NUMBER,
    "rendimiento_actividad_neto": FactValueType.NUMBER,
    "rendimiento_alquiler_neto": FactValueType.NUMBER,
    "rendimiento_capital_mobiliario": FactValueType.NUMBER,
    "ganancias_patrimoniales": FactValueType.NUMBER,
    "aportacion_plan_pensiones": FactValueType.NUMBER,
    "contribucion_empresa_plan_pensiones": FactValueType.NUMBER,
    "donativos": FactValueType.NUMBER,
    "pagos_vivienda_habitual_pre2013": FactValueType.NUMBER,
    "gastos_guarderia": FactValueType.NUMBER,
}

_INTEGER_FIELDS = frozenset(
    name for name, field in IRPFInput.model_fields.items() if field.annotation in (int, int | None)
)
# What a first useful simulation needs; the rest default to zero.
CORE_FIELDS = ("rendimiento_trabajo_bruto", "cotizaciones_seguridad_social", "retenciones", "comunidad_autonoma")


class DatosInvalidosError(ValueError):
    """The scenario data cannot be turned into a calculator input."""


def build_input(
    *,
    ejercicio: int,
    facts: dict[str, Any] | None = None,
    datos: dict[str, Any] | None = None,
    cambios: dict[str, Any] | None = None,
) -> IRPFInput:
    """Layer explicit ``datos`` and scenario ``cambios`` over the case facts."""
    merged: dict[str, Any] = {}
    for source in (facts or {}, datos or {}, cambios or {}):
        for name, value in source.items():
            if name in IRPFInput.model_fields and name != "ejercicio" and value is not None:
                # Counts arrive as floats from the fact store (NUMBER facts).
                if name in _INTEGER_FIELDS and isinstance(value, float) and value.is_integer():
                    value = int(value)
                merged[name] = value
    unknown = sorted(
        name for source in (datos or {}, cambios or {}) for name in source if name not in IRPFInput.model_fields
    )
    if unknown:
        raise DatosInvalidosError(
            f"Campos desconocidos: {', '.join(unknown)}. Campos válidos: {', '.join(IRPFInput.model_fields)}."
        )
    try:
        return IRPFInput(ejercicio=ejercicio, **merged)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise DatosInvalidosError(f"Datos no válidos para el cálculo: {problems}") from exc


def missing_core_fields(inp: IRPFInput, provided: set[str]) -> list[str]:
    """Core figures nobody supplied (so the simulation silently used zero)."""
    return [name for name in CORE_FIELDS if name not in provided]
