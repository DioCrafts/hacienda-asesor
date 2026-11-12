"""Bridge between the user's case facts and a calculator scenario.

Profile facts share their names with :class:`IRPFInput` fields on purpose, so a
fact the advisor records ("aportacion_plan_pensiones = 1000") feeds the
calculator with no translation layer to drift out of sync.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from hacienda_gpt.tax.actividad import ActividadInput, ResultadoActividad, calcular_rendimiento_actividad
from hacienda_gpt.tax.irpf import IRPFInput
from hacienda_gpt.tax.params import EjercicioNoDisponibleError

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
    # Letting a dwelling: net income before the art. 23.2 reduction, and the contract facts that decide it.
    "rendimiento_alquiler_vivienda_sin_reducir": "number",
    "arrendamiento_fecha_contrato": "string",
    "arrendamiento_gran_tenedor": "boolean",
    "arrendamiento_primer_alquiler": "boolean",
    "arrendamiento_variacion_renta": "number",
    "arrendamiento_mismo_inquilino": "boolean",
    "arrendamiento_renta_bajo_indice": "boolean",
    "arrendamiento_zona_tensionada": "boolean",
    "arrendamiento_inquilino_joven": "boolean",
    "arrendamiento_alquiler_social": "boolean",
    "arrendamiento_vivienda_rehabilitada": "boolean",
    "arrendamiento_prorroga_tacita": "boolean",
    "rendimiento_capital_mobiliario": "number",
    "ganancias_patrimoniales": "number",
    "rendimientos_negativos_pendientes": "number",
    "perdidas_patrimoniales_pendientes": "number",
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
    "familia_numerosa_especial": "boolean",
    "familia_monoparental": "boolean",
    "hijos_nacidos_en_el_ejercicio": "number",
    "gastos_libros_texto": "number",
    "gastos_cuidado_familiares": "number",
    "num_familiares_discapacidad_a_cargo": "number",
    "municipio_rural_o_despoblacion": "boolean",
    # Drives the fiscal calendar (modelo 720), not the IRPF computation.
    "bienes_en_el_extranjero": "boolean",
}

# Facts of a self-employed activity, annual totals without VAT; also merged into the extractor's
# vocabulary. Kept apart from PROFILE_FACT_TYPES because they are not IRPFInput fields:
# ``build_input`` turns them into ``rendimiento_actividad_neto`` when that figure is not given.
ACTIVIDAD_FACT_TYPES: dict[str, str] = {
    "actividad_ingresos_integros": "number",
    "actividad_seguridad_social_autonomo": "number",
    "actividad_suministros": "number",
    "actividad_alquileres": "number",
    "actividad_servicios_profesionales": "number",
    "actividad_otros_gastos": "number",
    "actividad_amortizaciones": "number",
    "es_autonomo": "boolean",
    # Professional (IAE sections 2 and 3) rather than business activity: matters for modelo 130.
    "actividad_profesional": "boolean",
}

# Activity facts that feed the net-income calculator -> the ActividadInput field each one fills.
ACTIVIDAD_FACT_FIELDS: dict[str, str] = {
    "actividad_ingresos_integros": "ingresos_integros",
    "actividad_seguridad_social_autonomo": "seguridad_social_autonomo",
    "actividad_suministros": "suministros",
    "actividad_alquileres": "alquileres",
    "actividad_servicios_profesionales": "servicios_profesionales",
    "actividad_otros_gastos": "otros_gastos",
    "actividad_amortizaciones": "amortizaciones",
}
# Accepted in ``datos``/``cambios`` although they are not IRPFInput fields.
_ACTIVIDAD_NAMES = frozenset(ACTIVIDAD_FACT_TYPES)
_NETO = "rendimiento_actividad_neto"

_INTEGER_FIELDS = frozenset(
    name for name, field in IRPFInput.model_fields.items() if field.annotation in (int, int | None)
)
# What a first useful simulation needs; the rest default to zero.
CORE_FIELDS = ("rendimiento_trabajo_bruto", "cotizaciones_seguridad_social", "retenciones", "comunidad_autonoma")


class DatosInvalidosError(ValueError):
    """The scenario data cannot be turned into a calculator input."""


class ActividadPerfil(BaseModel):
    """What ``build_input`` did with the activity facts of a scenario."""

    model_config = ConfigDict(extra="forbid")

    # True when ``rendimiento_actividad_neto`` of the IRPF input was computed from the breakdown.
    calculado: bool
    # The computation with its lines and warnings (None when it was not run).
    resultado: ResultadoActividad | None = None
    # Sentence for the user, to be shown with the IRPF result.
    aviso: str


def _capas(*sources: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [{name: value for name, value in (source or {}).items() if value is not None} for source in sources]


def actividad_del_perfil(
    *,
    ejercicio: int,
    facts: dict[str, Any] | None = None,
    datos: dict[str, Any] | None = None,
    cambios: dict[str, Any] | None = None,
) -> ActividadPerfil | None:
    """Net income of the activity from the ``actividad_*`` facts, or None when there are none.

    The layers are the same as in :func:`build_input` (facts < datos < cambios).
    The most specific layer that says anything about the activity decides: if
    it gives ``rendimiento_actividad_neto`` that explicit figure is used as it
    is; otherwise the breakdown (merged across the layers) is computed in
    estimación directa simplificada. So a figure typed in a scenario is not
    silently overridden by a stored one, and the other way round.

    Only income and expenses are passed on: the art. 32.2 LIRPF reductions
    need facts the profile does not hold (other income as the law counts it,
    the single-client requirements), so none is applied and the result's
    ``avisos`` say so.
    """
    capas = _capas(facts, datos, cambios)
    desglose: dict[str, Any] = {}
    for capa in capas:
        desglose.update({name: capa[name] for name in ACTIVIDAD_FACT_FIELDS if name in capa})
    if not desglose:
        return None
    for capa in reversed(capas):
        if _NETO in capa:
            return ActividadPerfil(
                calculado=False,
                aviso=(
                    "Hay ingresos y gastos de la actividad guardados, pero se ha usado el rendimiento neto de la "
                    f"actividad indicado directamente ({capa[_NETO]} €); el desglose no se ha tenido en cuenta."
                ),
            )
        if any(name in capa for name in ACTIVIDAD_FACT_FIELDS):
            break
    try:
        entrada = ActividadInput(
            ejercicio=ejercicio, **{ACTIVIDAD_FACT_FIELDS[name]: value for name, value in desglose.items()}
        )
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise DatosInvalidosError(f"Datos de la actividad no válidos: {problems}") from exc
    try:
        resultado = calcular_rendimiento_actividad(entrada)
    except EjercicioNoDisponibleError as exc:
        return ActividadPerfil(
            calculado=False,
            aviso=(
                f"ATENCIÓN: {exc} El rendimiento de la actividad NO está incluido en este cálculo: no se ha podido "
                "obtener a partir de los ingresos y gastos guardados. Indica directamente el rendimiento neto de la "
                "actividad de ese ejercicio."
            ),
        )
    return ActividadPerfil(
        calculado=True,
        resultado=resultado,
        aviso=(
            f"El rendimiento neto de la actividad ({resultado.rendimiento_actividad_neto} €) se ha calculado a "
            f"partir de los ingresos ({resultado.ingresos_integros} €) y gastos ({resultado.gastos_deducibles} €) "
            "de la actividad en estimación directa simplificada"
            + (
                f", con {resultado.gastos_dificil_justificacion} € de gastos de difícil justificación"
                if resultado.gastos_dificil_justificacion
                else ""
            )
            + " y sin aplicar las reducciones del art. 32 LIRPF. Los gastos se toman tal como están indicados."
        ),
    )


def build_input_con_actividad(
    *,
    ejercicio: int,
    facts: dict[str, Any] | None = None,
    datos: dict[str, Any] | None = None,
    cambios: dict[str, Any] | None = None,
) -> tuple[IRPFInput, ActividadPerfil | None]:
    """:func:`build_input` plus what was done with the activity facts (None when there are none)."""
    merged: dict[str, Any] = {}
    for source in (facts or {}, datos or {}, cambios or {}):
        for name, value in source.items():
            if name in IRPFInput.model_fields and name != "ejercicio" and value is not None:
                # Counts arrive as floats from the fact store (NUMBER facts).
                if name in _INTEGER_FIELDS and isinstance(value, float) and value.is_integer():
                    value = int(value)
                merged[name] = value
    unknown = sorted(
        name
        for source in (datos or {}, cambios or {})
        for name in source
        if name not in IRPFInput.model_fields and name not in _ACTIVIDAD_NAMES
    )
    if unknown:
        raise DatosInvalidosError(
            f"Campos desconocidos: {', '.join(unknown)}. Campos válidos: {', '.join(IRPFInput.model_fields)}."
        )
    actividad = actividad_del_perfil(ejercicio=ejercicio, facts=facts, datos=datos, cambios=cambios)
    if actividad is not None and actividad.calculado and actividad.resultado is not None:
        merged[_NETO] = actividad.resultado.rendimiento_actividad_neto
    try:
        return IRPFInput(ejercicio=ejercicio, **merged), actividad
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise DatosInvalidosError(f"Datos no válidos para el cálculo: {problems}") from exc


def build_input(
    *,
    ejercicio: int,
    facts: dict[str, Any] | None = None,
    datos: dict[str, Any] | None = None,
    cambios: dict[str, Any] | None = None,
) -> IRPFInput:
    """Layer explicit ``datos`` and scenario ``cambios`` over the case facts.

    When the layers hold ``actividad_*`` facts and no explicit
    ``rendimiento_actividad_neto`` overrides them, that figure is computed
    from them (see :func:`actividad_del_perfil`). Callers that show the result
    to the user should use :func:`build_input_con_actividad` to get the
    sentence that says so.
    """
    return build_input_con_actividad(ejercicio=ejercicio, facts=facts, datos=datos, cambios=cambios)[0]
