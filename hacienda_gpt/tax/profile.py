"""Bridge between the user's case facts and a calculator scenario.

Profile facts share their names with :class:`IRPFInput` fields on purpose, so a
fact the advisor records ("aportacion_plan_pensiones = 1000") feeds the
calculator with no translation layer to drift out of sync.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from hacienda_gpt.tax.irpf import IRPFInput

# Profile facts and their value types ("string" | "number" | "boolean"); merged
# into the extractor's vocabulary. Plain strings on purpose: this module must
# not import ``hacienda_gpt.decision`` (which imports it back).
PROFILE_FACT_TYPES: dict[str, str] = {
    "comunidad_autonoma": "string",
    "estado_civil": "string",
    "edad": "number",
    "grado_discapacidad": "number",
    "num_descendientes": "number",
    "num_descendientes_menores_3": "number",
    "num_ascendientes_mayores_65": "number",
    "num_ascendientes_mayores_75": "number",
    "rendimiento_trabajo_bruto": "number",
    "cotizaciones_seguridad_social": "number",
    "retenciones": "number",
    "rendimiento_actividad_neto": "number",
    "rendimiento_alquiler_neto": "number",
    "rendimiento_capital_mobiliario": "number",
    "ganancias_patrimoniales": "number",
    "aportacion_plan_pensiones": "number",
    "contribucion_empresa_plan_pensiones": "number",
    "donativos": "number",
    "pagos_vivienda_habitual_pre2013": "number",
    "gastos_guarderia": "number",
    # Regional deductions (Comunidad de Madrid) and family-unit income ceilings.
    "alquiler_vivienda_habitual": "number",
    "gastos_escolaridad": "number",
    "gastos_ensenanza_idiomas": "number",
    "gastos_uniformes_escolares": "number",
    "cotizaciones_empleada_hogar": "number",
    "hijos_nacidos_ultimos_3_ejercicios": "number",
    "donativos_fundaciones_madrid": "number",
    "base_imponible_resto_unidad_familiar": "number",
    "miembros_unidad_familiar": "number",
    "familia_numerosa": "boolean",
    # Drives the fiscal calendar (modelo 720), not the IRPF computation.
    "bienes_en_el_extranjero": "boolean",
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
