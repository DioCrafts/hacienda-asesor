"""Bridge between the user's case facts and a calculator scenario.

Profile facts share their names with :class:`IRPFInput` fields on purpose, so a
fact the advisor records ("aportacion_plan_pensiones = 1000") feeds the
calculator with no translation layer to drift out of sync.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from hacienda_gpt.tax.actividad import ActividadInput, ResultadoActividad, calcular_rendimiento_actividad
from hacienda_gpt.tax.autonomos_params import load_autonomos_params
from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult, calcular_irpf
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
    "menores_acogidos": "number",
    "hijos_adoptados_en_el_ejercicio": "number",
    "donativos_autonomicos": "number",
    "inversion_vivienda_habitual": "number",
    "inversion_empresas_nueva_creacion": "number",
    "gastos_eficiencia_energetica": "number",
    "gastos_salud": "number",
    "gastos_deporte_cultura": "number",
    "viudedad": "boolean",
    "vivienda_en_municipio_rural": "boolean",
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
    # The user states that ALL the requirements of art. 32.2.2.º LIRPF hold (single unrelated
    # client or TRADE, etc.) and takes the art. 32.2.1.º reduction instead of the flat allowance.
    "actividad_cumple_requisitos_dependiente": "boolean",
    # Art. 32.1 LIRPF: part of the net income that is irregular (see ActividadInput).
    "actividad_rendimientos_irregulares": "number",
    # Art. 32.3 LIRPF: the user states the new-activity reduction applies this year.
    "actividad_reduccion_inicio": "boolean",
    # "ceuta" or "melilla": activity carried on there with the right to the art. 68.4 deduction.
    "actividad_ceuta_melilla": "string",
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
    "actividad_rendimientos_irregulares": "rendimientos_irregulares",
}
# Statements about the activity (not figures) -> ActividadInput field. They only matter when
# there is a breakdown to compute.
ACTIVIDAD_OPCION_FIELDS: dict[str, str] = {
    "actividad_cumple_requisitos_dependiente": "cumple_requisitos_reduccion_dependiente",
    "actividad_reduccion_inicio": "reduccion_inicio_actividad",
    "actividad_ceuta_melilla": "ceuta_melilla",
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
    # What the art. 32.2 LIRPF reductions were evaluated with (None: not derived).
    otras_rentas_no_exentas: Decimal | None = None
    reduccion_rendimientos_trabajo: Decimal | None = None


def _capas(*sources: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [{name: value for name, value in (source or {}).items() if value is not None} for source in sources]


def _merge_irpf(*sources: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        for name, value in (source or {}).items():
            if name in IRPFInput.model_fields and name != "ejercicio" and value is not None:
                # Counts arrive as floats from the fact store (NUMBER facts).
                if name in _INTEGER_FIELDS and isinstance(value, float) and value.is_integer():
                    value = int(value)
                merged[name] = value
    return merged


def _irpf_input(ejercicio: int, merged: dict[str, Any]) -> IRPFInput:
    try:
        return IRPFInput(ejercicio=ejercicio, **merged)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise DatosInvalidosError(f"Datos no válidos para el cálculo: {problems}") from exc


_LINEA_REDUCCION_TRABAJO = "Reducción por obtención de rendimientos del trabajo"
_LINEA_NETO_TRABAJO = "Rendimiento neto reducido del trabajo"


def _linea(result: IRPFResult, concepto: str) -> Decimal | None:
    return next((line.importe for line in result.lineas if line.concepto == concepto), None)


def _reduccion_trabajo(ejercicio: int, merged: dict[str, Any], actividad_neto: Decimal) -> Decimal:
    """Art. 20 LIRPF reduction the IRPF calculator applies when the activity's net income is this."""
    result = calcular_irpf(_irpf_input(ejercicio, {**merged, _NETO: actividad_neto}))
    return abs(_linea(result, _LINEA_REDUCCION_TRABAJO) or Decimal(0))


def _otras_rentas(ejercicio: int, merged: dict[str, Any]) -> Decimal | None:
    """Non-exempt income other than the activity, as the IRPF calculator sees the profile.

    Net figures, each kind floored at zero (the convention the calculator
    itself uses for the "other income" test of art. 20): net work income
    before the art. 20 reduction, the rest of the general base, interest and
    dividends, and capital gains. None when it cannot be read off the result.
    """
    sin_actividad = _irpf_input(ejercicio, {**merged, _NETO: Decimal(0)})
    result = calcular_irpf(sin_actividad)
    trabajo_reducido = Decimal(0)
    trabajo = Decimal(0)
    if sin_actividad.rendimiento_trabajo_bruto:
        reducido = _linea(result, _LINEA_NETO_TRABAJO)
        if reducido is None:
            return None
        trabajo_reducido = reducido
        trabajo = reducido + abs(_linea(result, _LINEA_REDUCCION_TRABAJO) or Decimal(0))
    return (
        trabajo
        + max(Decimal(0), result.base_imponible_general - trabajo_reducido)
        + max(Decimal(0), sin_actividad.rendimiento_capital_mobiliario)
        + max(Decimal(0), sin_actividad.ganancias_patrimoniales)
    )


def _calcular_actividad(datos: dict[str, Any]) -> ResultadoActividad:
    try:
        entrada = ActividadInput(**datos)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors()
        )
        raise DatosInvalidosError(f"Datos de la actividad no válidos: {problems}") from exc
    return calcular_rendimiento_actividad(entrada)


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

    The art. 32.2 LIRPF reductions depend on the rest of the profile, and the
    art. 20 reduction of work income depends in turn on the activity's net
    income. The order that avoids going in circles:

    1. net income of the activity without art. 32.2 (income, expenses, flat
       allowance, art. 32.1 and 32.3 if stated);
    2. the other non-exempt income, from the IRPF calculator run without the
       activity (see :func:`_otras_rentas`);
    3. the art. 20 reduction the IRPF calculator gives with the net income of
       step 1;
    4. the activity again with 2 and 3, which applies art. 32.2.1.º (only when
       the user declared its requirements) or art. 32.2.3.º;
    5. the art. 20 reduction again with the reduced net income: if it grew
       (the activity fell under art. 20's other-income limit), step 4 is
       repeated with it. The net income can then only go up and the art. 20
       reduction down, so the joint cap of art. 32.2.3.º holds and the
       process ends.
    """
    capas = _capas(facts, datos, cambios)
    desglose: dict[str, Any] = {}
    opciones: dict[str, Any] = {}
    for capa in capas:
        desglose.update({name: capa[name] for name in ACTIVIDAD_FACT_FIELDS if name in capa})
        opciones.update({name: capa[name] for name in ACTIVIDAD_OPCION_FIELDS if name in capa})
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

    base: dict[str, Any] = {
        "ejercicio": ejercicio,
        **{ACTIVIDAD_FACT_FIELDS[name]: value for name, value in desglose.items()},
        **{ACTIVIDAD_OPCION_FIELDS[name]: value for name, value in opciones.items()},
    }
    if isinstance(base.get("ceuta_melilla"), str):
        base["ceuta_melilla"] = base["ceuta_melilla"].strip().lower() or None
    quiere_dependiente = bool(base.pop("cumple_requisitos_reduccion_dependiente", False))
    try:
        previo = _calcular_actividad(base)  # step 1
        p = load_autonomos_params(ejercicio)
    except EjercicioNoDisponibleError as exc:
        return ActividadPerfil(
            calculado=False,
            aviso=(
                f"ATENCIÓN: {exc} El rendimiento de la actividad NO está incluido en este cálculo: no se ha podido "
                "obtener a partir de los ingresos y gastos guardados. Indica directamente el rendimiento neto de la "
                "actividad de ese ejercicio."
            ),
        )

    merged = _merge_irpf(facts, datos, cambios)
    merged.pop(_NETO, None)
    irpf = _irpf_input(ejercicio, merged)
    otras = _otras_rentas(ejercicio, merged)  # step 2
    notas: list[str] = []
    resultado = previo
    reduccion_trabajo: Decimal | None = None
    dep = p.reduccion_dependientes
    if otras is None:
        notas.append(
            "no se han podido obtener tus otras rentas del resto del expediente, así que no se ha aplicado ninguna "
            "reducción del art. 32.2 LIRPF"
        )
    elif quiere_dependiente and irpf.rendimiento_trabajo_bruto > dep.max_prestaciones_trabajo:
        quiere_dependiente = False
        notas.append(
            f"no se aplica la reducción de autónomo dependiente (art. 32.2.1.º LIRPF) aunque la has declarado, porque "
            f"en el expediente constan rendimientos del trabajo de {irpf.rendimiento_trabajo_bruto} € y el requisito "
            f"e) del art. 32.2.2.º solo admite prestaciones de hasta {dep.max_prestaciones_trabajo} €"
        )
    if otras is not None:
        opciones_32_2: dict[str, Any] = {"otras_rentas_no_exentas": otras}
        if quiere_dependiente:
            grado = Decimal(irpf.grado_discapacidad) / 100
            opciones_32_2 |= {
                "cumple_requisitos_reduccion_dependiente": True,
                "discapacidad": grado >= dep.grado_discapacidad,
                "discapacidad_ampliada": grado >= dep.grado_discapacidad_ampliada,
            }
        reduccion_trabajo = _reduccion_trabajo(ejercicio, merged, previo.rendimiento_actividad_neto)  # step 3
        resultado = _calcular_actividad(
            {**base, **opciones_32_2, "reduccion_rendimientos_trabajo": reduccion_trabajo}
        )  # step 4
        despues = _reduccion_trabajo(ejercicio, merged, resultado.rendimiento_actividad_neto)  # step 5
        if despues > reduccion_trabajo:
            reduccion_trabajo = despues
            resultado = _calcular_actividad(
                {**base, **opciones_32_2, "reduccion_rendimientos_trabajo": reduccion_trabajo}
            )

    partes = [
        f"El rendimiento neto de la actividad ({resultado.rendimiento_actividad_neto} €) se ha calculado a partir de "
        f"los ingresos ({resultado.ingresos_integros} €) y gastos ({resultado.gastos_deducibles} €) de la actividad "
        "en estimación directa simplificada"
        + (
            f", con {resultado.gastos_dificil_justificacion} € de gastos de difícil justificación"
            if resultado.gastos_dificil_justificacion
            else ""
        )
        + ". Los gastos se toman tal como están indicados."
    ]
    if otras is not None:
        if resultado.reduccion_art_32_2:
            partes.append(
                f"Incluye {resultado.reduccion_art_32_2} € de reducción del {resultado.reduccion_aplicada}"
                + (
                    ", que has declarado que te corresponde"
                    if resultado.reduccion_aplicada == "art. 32.2.1.º LIRPF"
                    else ""
                )
                + "."
            )
        else:
            partes.append("No corresponde ninguna reducción del art. 32.2 LIRPF con los datos del expediente.")
        partes.append(
            f"Para evaluarla se han seguido estos pasos: primero el rendimiento de la actividad sin esa reducción "
            f"({previo.rendimiento_actividad_neto} €); después tus otras rentas no exentas según el resto del "
            f"expediente ({otras} €, por su importe neto: la ley no define cómo se computan); y por último la "
            "reducción por rendimientos del trabajo del art. 20 LIRPF que resulta en el IRPF "
            f"({reduccion_trabajo} €), porque junto con la del art. 32.2.3.º no puede pasar de "
            f"{p.reduccion_rentas_bajas.limite_conjunto_reduccion_trabajo} €."
        )
        if irpf.tributacion != "individual":
            partes.append(
                "En tributación conjunta estas reducciones se calculan con las rentas de toda la unidad familiar "
                "(art. 26.2 RIRPF): las cifras del expediente deben ser las de la unidad."
            )
    partes.extend(nota[0].upper() + nota[1:] + "." for nota in notas)
    extras = [
        texto
        for importe, texto in (
            (resultado.reduccion_art_32_1, "por rendimientos irregulares (art. 32.1 LIRPF)"),
            (resultado.reduccion_art_32_3, "por inicio de actividad (art. 32.3 LIRPF)"),
        )
        if importe
    ]
    if extras:
        partes.append(
            f"También incluye la reducción {' y la reducción '.join(extras)}, por "
            f"{resultado.reduccion_art_32_1 + resultado.reduccion_art_32_3} € en total."
        )
    if resultado.reduccion_aplicada != "art. 32.2.1.º LIRPF" and not opciones.get(
        "actividad_cumple_requisitos_dependiente"
    ):
        partes.append(
            "Si trabajas para un único cliente y cumples los requisitos del art. 32.2.2.º LIRPF, indícalo: la "
            "reducción puede ser mayor."
        )
    return ActividadPerfil(
        calculado=True,
        resultado=resultado,
        aviso=" ".join(partes),
        otras_rentas_no_exentas=otras,
        reduccion_rendimientos_trabajo=reduccion_trabajo,
    )


def build_input_con_actividad(
    *,
    ejercicio: int,
    facts: dict[str, Any] | None = None,
    datos: dict[str, Any] | None = None,
    cambios: dict[str, Any] | None = None,
) -> tuple[IRPFInput, ActividadPerfil | None]:
    """:func:`build_input` plus what was done with the activity facts (None when there are none)."""
    merged = _merge_irpf(facts, datos, cambios)
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
    # Validate the IRPF figures first, so their errors read as before whatever the activity holds.
    entrada = _irpf_input(ejercicio, merged)
    actividad = actividad_del_perfil(ejercicio=ejercicio, facts=facts, datos=datos, cambios=cambios)
    if actividad is not None and actividad.calculado and actividad.resultado is not None:
        entrada = _irpf_input(ejercicio, {**merged, _NETO: actividad.resultado.rendimiento_actividad_neto})
    return entrada, actividad


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
