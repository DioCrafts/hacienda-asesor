"""Bridge between the user's case facts and a calculator scenario.

Profile facts share their names with :class:`IRPFInput` fields on purpose, so a
fact the advisor records ("aportacion_plan_pensiones = 1000") feeds the
calculator with no translation layer to drift out of sync.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hacienda_gpt.tax.actividad import ActividadInput, ResultadoActividad, calcular_rendimiento_actividad
from hacienda_gpt.tax.autonomos_params import load_autonomos_params
from hacienda_gpt.tax.irpf import Descendiente, IRPFInput, IRPFResult, calcular_irpf
from hacienda_gpt.tax.params import CAMPOS_GASTO, GASTOS_DESGLOSADOS, EjercicioNoDisponibleError

# Profile facts and their value types ("string" | "number" | "boolean"); merged
# into the extractor's vocabulary. Plain strings on purpose: this module must
# not import ``hacienda_gpt.decision`` (which imports it back).
PROFILE_FACT_TYPES: dict[str, str] = {
    "comunidad_autonoma": "string",
    "estado_civil": "string",
    "edad": "number",
    "grado_discapacidad": "number",
    "necesita_asistencia": "boolean",
    "num_descendientes": "number",
    "num_descendientes_menores_3": "number",
    "num_ascendientes_mayores_65": "number",
    "num_ascendientes_mayores_75": "number",
    # Minimum for disability of descendants and ascendants (art. 60.2 LIRPF).
    "num_descendientes_discapacidad": "number",
    "num_descendientes_discapacidad_65": "number",
    "num_descendientes_asistencia": "number",
    "num_ascendientes_discapacidad": "number",
    "num_ascendientes_discapacidad_65": "number",
    "num_ascendientes_asistencia": "number",
    "num_ascendientes_discapacidad_hasta_65": "number",
    "num_declaraciones_mismos_ascendientes": "number",
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
    "num_hijos_gastos_educativos": "number",
    "num_hijos_primer_ciclo_infantil": "number",
    # Finer facts for regional deductions: spouse, municipality, parts of the year, sharing.
    "edad_conyuge": "number",
    "grado_discapacidad_conyuge": "number",
    "necesita_asistencia_conyuge": "boolean",
    "num_familiares_discapacidad_65_a_cargo": "number",
    "num_personas_acogidas_mayores_o_discapacidad": "number",
    "habitantes_municipio": "number",
    "dias_alquiler_vivienda_habitual": "number",
    "dias_acogimiento": "number",
    "dias_desempleo": "number",
    "num_declaraciones_mismo_alquiler": "number",
    "num_declaraciones_misma_vivienda": "number",
    "progenitores_trabajan": "boolean",
    "victima_violencia_genero": "boolean",
    "victima_terrorismo": "boolean",
    "vivienda_protegida": "boolean",
    "vivienda_nueva_construccion": "boolean",
    "primera_vivienda": "boolean",
    "ejercicio_viudedad": "number",
    "ejercicio_adquisicion_vivienda": "number",
    "ejercicio_traslado_residencia": "number",
    # Narrower expenses: parts of a broad total above (params.GASTOS_DESGLOSADOS).
    "gastos_instalaciones_renovables": "number",
    "gastos_deporte": "number",
    "gastos_cultura": "number",
    "gastos_honorarios_sanitarios": "number",
    "gastos_salud_bucodental": "number",
    "gastos_salud_mental": "number",
    "gastos_optica": "number",
    "gastos_enfermedades_raras": "number",
    "gastos_ela": "number",
    "pagos_adquisicion_vivienda": "number",
    "pagos_rehabilitacion_vivienda": "number",
    "intereses_prestamo_vivienda": "number",
    "donativos_medio_ambiente": "number",
    "donativos_cultura_patrimonio": "number",
    "donativos_investigacion": "number",
    "donativos_cooperacion_fines_sociales": "number",
    "donativos_lengua_propia": "number",
    "donativos_fundaciones_autonomicas": "number",
    "gastos_veterinarios": "number",
    "adquisicion_vehiculo_electrico": "number",
    "inversion_economia_social": "number",
    # Drives the fiscal calendar (modelo 720), not the IRPF computation.
    "bienes_en_el_extranjero": "boolean",
}

# The detail of each child: ONE fact whose value is the JSON text of a list of objects with the
# fields of :class:`hacienda_gpt.tax.irpf.Descendiente` (the fact store, the PUT endpoint and the
# agent's ``registrar_hechos`` only carry strings, numbers and booleans). It is not offered to
# the conversational extractor nor to the document reader: free text would not parse. It is
# written by the simulator and by the agent, both through :func:`descendientes_a_texto`.
DESCENDIENTES = "descendientes"
# Two more facts of the same kind, written by the optimiser screen and by the agent:
# ``datos_conyuge`` — the spouse's own figures (a JSON object, :class:`DatosConyuge`) — and
# ``preferencias_optimizacion`` — what the user can and will do before 31 December (a JSON object,
# ``hacienda_gpt.tax.optimizador.PreferenciasOptimizacion``). Every structured fact is checked and
# canonicalised by ``hacienda_gpt.tax.optimizador.detalle_a_texto`` before it is stored.
DATOS_CONYUGE = "datos_conyuge"
PREFERENCIAS_OPTIMIZACION = "preferencias_optimizacion"
DETALLE_FACT_TYPES: dict[str, str] = {
    DESCENDIENTES: "string",
    DATOS_CONYUGE: "string",
    PREFERENCIAS_OPTIMIZACION: "string",
}
MAX_DESCENDIENTES = 20
# Longest JSON text accepted for the spouse's figures and for the optimiser's preferences.
MAX_TEXTO_DETALLE = 20000
# Counts the calculator derives from the detail (IRPFInput fills them when they are zero and
# rejects a different figure): with the detail they are never asked for again.
RECUENTOS_DEL_DETALLE: tuple[str, ...] = (
    "num_descendientes",
    "num_descendientes_menores_3",
    "hijos_nacidos_en_el_ejercicio",
    "hijos_nacidos_ultimos_3_ejercicios",
    "hijos_adoptados_en_el_ejercicio",
    "num_hijos_primer_ciclo_infantil",
    "num_descendientes_discapacidad",
    "num_descendientes_discapacidad_65",
    "num_descendientes_asistencia",
)
# Counts that hold others (IRPFInput takes one left at zero as the sum of its parts and rejects one smaller than
# that sum), inner ones first. A part may also come from the children's detail.
RECUENTOS_DESGLOSADOS: dict[str, tuple[str, ...]] = {
    "num_descendientes_discapacidad": ("num_descendientes_discapacidad_65", "num_descendientes_asistencia"),
    "num_descendientes": ("num_descendientes_discapacidad",),
    "num_ascendientes_discapacidad": ("num_ascendientes_discapacidad_65", "num_ascendientes_asistencia"),
    "num_familiares_discapacidad_a_cargo": ("num_descendientes_discapacidad", "num_ascendientes_discapacidad"),
    "num_familiares_discapacidad_65_a_cargo": ("num_descendientes_discapacidad_65", "num_ascendientes_discapacidad_65"),
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


_MENSAJES_VALIDACION: dict[str, str] = {
    "greater_than_equal": "debe ser mayor o igual que {ge}",
    "greater_than": "debe ser mayor que {gt}",
    "less_than_equal": "debe ser menor o igual que {le}",
    "less_than": "debe ser menor que {lt}",
    "int_parsing": "debe ser un número entero",
    "int_from_float": "debe ser un número entero, sin decimales",
    "int_type": "debe ser un número entero",
    "decimal_parsing": "debe ser un importe numérico",
    "decimal_type": "debe ser un importe numérico",
    "bool_parsing": "debe ser verdadero o falso",
    "bool_type": "debe ser verdadero o falso",
    "string_type": "debe ser un texto",
    "date_from_datetime_parsing": "debe ser una fecha con formato AAAA-MM-DD",
    "date_parsing": "debe ser una fecha con formato AAAA-MM-DD",
    "date_type": "debe ser una fecha con formato AAAA-MM-DD",
    "literal_error": "debe ser uno de estos valores: {expected}",
    "extra_forbidden": "campo desconocido",
    "missing": "falta este dato",
    "list_type": "debe ser una lista",
    "dict_type": "debe ser un objeto {{campo: valor}}",
    "model_type": "debe ser un objeto {{campo: valor}}",
    "too_long": "admite como máximo {max_length} elementos",
    "finite_number": "debe ser un número finito",
    "string_too_long": "admite como máximo {max_length} caracteres",
    "string_pattern_mismatch": "tiene un formato no válido",
}


def _problemas(exc: ValidationError) -> str:
    """The errors of a pydantic validation as one Spanish sentence the user (or the agent) can act on."""
    partes: list[str] = []
    for error in exc.errors():
        donde = ".".join(
            f"hijo {p + 1}" if isinstance(p, int) and i and error["loc"][i - 1] == DESCENDIENTES else str(p)
            for i, p in enumerate(error["loc"])
        )
        plantilla = _MENSAJES_VALIDACION.get(error["type"])
        if error["type"] == "value_error":
            mensaje = error["msg"].removeprefix("Value error, ")
        elif plantilla is not None:
            try:
                mensaje = plantilla.format(**(error.get("ctx") or {})).replace("' or '", "' o '")
            except (KeyError, IndexError):
                mensaje = error["msg"]
        else:
            mensaje = error["msg"]
        partes.append(f"{donde}: {mensaje}" if donde else mensaje)
    return "; ".join(partes)


def normalizar_descendientes(valor: Any) -> list[dict[str, Any]]:
    """The detail of the children as a list of plain JSON-ready dicts, whatever shape it came in.

    Accepts the JSON text kept in the fact store or a list of objects (tool
    arguments, simulator). Each child is checked against
    :class:`Descendiente` on its own; what depends on the tax year (the age
    against the date of birth, the year of adoption) and on the other inputs
    is checked by ``IRPFInput``. Raises :class:`DatosInvalidosError` with a
    sentence in Spanish.
    """
    if isinstance(valor, str):
        try:
            valor = json.loads(valor) if valor.strip() else []
        except json.JSONDecodeError as exc:
            raise DatosInvalidosError(
                "El detalle de los hijos («descendientes») no se puede leer: debe ser una lista JSON con un objeto "
                'por hijo, por ejemplo [{"edad": 2, "gastos": {"gastos_guarderia": 1800}}].'
            ) from exc
    if not isinstance(valor, list):
        raise DatosInvalidosError(
            "El detalle de los hijos («descendientes») debe ser una lista con un objeto por hijo, por ejemplo "
            '[{"edad": 2, "gastos": {"gastos_guarderia": 1800}}].'
        )
    if len(valor) > MAX_DESCENDIENTES:
        raise DatosInvalidosError(f"El detalle de los hijos admite como máximo {MAX_DESCENDIENTES} hijos.")
    hijos: list[dict[str, Any]] = []
    for numero, item in enumerate(valor, start=1):
        if isinstance(item, Descendiente):
            item = item.model_dump(mode="json")
        if not isinstance(item, dict):
            raise DatosInvalidosError(f"Detalle de los hijos, hijo {numero}: debe ser un objeto {{campo: valor}}.")
        try:
            hijo = Descendiente.model_validate(item)
        except ValidationError as exc:
            raise DatosInvalidosError(f"Detalle de los hijos, hijo {numero}: {_problemas(exc)}.") from exc
        if hijo.edad is None and hijo.fecha_nacimiento is None:
            raise DatosInvalidosError(
                f"Detalle de los hijos, hijo {numero}: falta la edad a 31 de diciembre o la fecha de nacimiento."
            )
        desconocidos = sorted(set(hijo.gastos) - CAMPOS_GASTO)
        if desconocidos:
            raise DatosInvalidosError(
                f"Detalle de los hijos, hijo {numero}: gastos desconocidos ({', '.join(desconocidos)}). "
                f"Gastos admitidos: {', '.join(sorted(CAMPOS_GASTO))}."
            )
        if any(importe < 0 for importe in hijo.gastos.values()):
            raise DatosInvalidosError(f"Detalle de los hijos, hijo {numero}: los gastos no pueden ser negativos.")
        hijo.gastos = {campo: importe for campo, importe in hijo.gastos.items() if importe}
        hijos.append(hijo.model_dump(mode="json", exclude_defaults=True))
    return hijos


def descendientes_a_texto(valor: Any) -> str:
    """Canonical JSON text of the children's detail, as the ``descendientes`` fact stores it."""
    return json.dumps(normalizar_descendientes(valor), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def importes_canonicos(valor: Any) -> Any:
    """Every Decimal as its shortest exact text ("1500", "120.5"), so one amount has one stored text
    whatever form it came in (1500, 1500.0, "1500.00"). Lists and dicts are walked."""
    if isinstance(valor, Decimal):
        return "0" if valor == 0 else format(valor.normalize(), "f")
    if isinstance(valor, dict):
        return {clave: importes_canonicos(v) for clave, v in valor.items()}
    if isinstance(valor, list):
        return [importes_canonicos(v) for v in valor]
    return valor


def objeto_json(valor: Any, que: str, ejemplo: str) -> dict[str, Any]:
    """A ``{campo: valor}`` object from the JSON text kept in the fact store, or as it came (tool
    arguments, request bodies). ``que`` names it in the Spanish error; ``ejemplo`` shows the shape."""
    if isinstance(valor, str):
        if len(valor) > MAX_TEXTO_DETALLE:
            raise DatosInvalidosError(
                f"{que}: el texto es demasiado largo (como máximo {MAX_TEXTO_DETALLE} caracteres)."
            )
        try:
            valor = json.loads(valor) if valor.strip() else None
        except (json.JSONDecodeError, RecursionError) as exc:
            raise DatosInvalidosError(
                f"{que}: el texto no se puede leer; debe ser un objeto JSON {{campo: valor}}, por ejemplo {ejemplo}."
            ) from exc
    if not isinstance(valor, dict):
        raise DatosInvalidosError(f"{que}: debe ser un objeto {{campo: valor}}, por ejemplo {ejemplo}.")
    return valor


# Amounts beyond this are refused when stored (the calculator itself has no upper bound): a sanity cap.
_IMPORTE_MAXIMO = Decimal("1000000000")


class DatosConyuge(BaseModel):
    """The spouse's own figures kept in the case (``datos_conyuge``).

    The calculator's fields for ONE person that the optimiser and the joint comparison take for the
    other spouse (income, what was withheld, contributions, age, disability), with the bounds of
    :class:`IRPFInput` plus a sanity cap. What is shared with the user is left out on purpose: the
    region, the children (common, in the user's ``descendientes``) and the filing mode (a choice,
    kept in the optimiser's preferences). Absent = not known; a stated zero is a figure and is kept.
    """

    model_config = ConfigDict(extra="forbid")

    rendimiento_trabajo_bruto: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    cotizaciones_seguridad_social: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    otros_gastos_deducibles_trabajo: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    retenciones: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    rendimiento_actividad_neto: Decimal | None = Field(default=None, ge=-_IMPORTE_MAXIMO, le=_IMPORTE_MAXIMO)
    rendimiento_alquiler_neto: Decimal | None = Field(default=None, ge=-_IMPORTE_MAXIMO, le=_IMPORTE_MAXIMO)
    rendimiento_capital_mobiliario: Decimal | None = Field(default=None, ge=-_IMPORTE_MAXIMO, le=_IMPORTE_MAXIMO)
    ganancias_patrimoniales: Decimal | None = Field(default=None, ge=-_IMPORTE_MAXIMO, le=_IMPORTE_MAXIMO)
    rendimientos_negativos_pendientes: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    perdidas_patrimoniales_pendientes: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    aportacion_plan_pensiones: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    contribucion_empresa_plan_pensiones: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    donativos: Decimal | None = Field(default=None, ge=0, le=_IMPORTE_MAXIMO)
    edad: int | None = Field(default=None, ge=0, le=130)
    grado_discapacidad: int | None = Field(default=None, ge=0, le=100)
    necesita_asistencia: bool | None = None


CAMPOS_CONYUGE: tuple[str, ...] = tuple(DatosConyuge.model_fields)


def normalizar_datos_conyuge(valor: Any) -> dict[str, Any]:
    """The spouse's figures as a JSON-ready dict with one canonical text per amount.

    Accepts the JSON text kept in the fact store or an object. Raises :class:`DatosInvalidosError`
    with a sentence in Spanish: unknown fields, values out of range, or an empty object (a spouse
    with no figure at all would be taken as one without income).
    """
    datos = objeto_json(
        valor, "Cifras de tu cónyuge («datos_conyuge»)", '{"rendimiento_trabajo_bruto": 12000, "edad": 41}'
    )
    try:
        modelo = DatosConyuge.model_validate(datos)
    except ValidationError as exc:
        pista = (
            f" Campos admitidos: {', '.join(CAMPOS_CONYUGE)}."
            if any(e["type"] == "extra_forbidden" for e in exc.errors())
            else ""
        )
        raise DatosInvalidosError(f"Cifras de tu cónyuge: {_problemas(exc)}.{pista}") from exc
    limpio: dict[str, Any] = importes_canonicos(modelo.model_dump(exclude_none=True))
    if not limpio:
        raise DatosInvalidosError(
            "Las cifras de tu cónyuge están vacías: indica al menos una (su salario, aunque sea 0) o quita el dato "
            "del expediente."
        )
    return limpio


def datos_conyuge_a_texto(valor: Any) -> str:
    """Canonical JSON text of the spouse's figures, as the ``datos_conyuge`` fact stores it."""
    return json.dumps(normalizar_datos_conyuge(valor), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decimal(valor: Any) -> Decimal | None:
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        numero = Decimal(str(valor))
    except InvalidOperation:
        return None
    return numero if numero.is_finite() else None


def _conciliar(merged: dict[str, Any], origen: dict[str, int]) -> None:
    """Make the layers agree where the calculator would otherwise reject them.

    ``IRPFInput`` refuses a count that differs from the children's detail, a
    total that differs from its per-child breakdown and a broad total smaller
    than its parts. That is right for figures given together, but the layers
    are written at different times: a count the advisor recorded months ago
    must not make every later calculation fail once the detail is known. So a
    derived figure (a count, a total) gives way when it comes from a LESS
    specific layer than the detail or the parts that now determine it, or
    when both sit in the stored facts (layer 0), which nobody wrote as one
    consistent set. Figures given in the same explicit layer (``datos`` or
    ``cambios``), or a derived figure in a more specific layer than its
    detail, are left alone, and the calculator's error says what does not add up.
    """
    por_hijo: dict[str, Decimal] = {}
    capa_detalle = -1
    if DESCENDIENTES in merged:
        try:
            hijos = normalizar_descendientes(merged[DESCENDIENTES])
        except DatosInvalidosError as exc:
            if origen[DESCENDIENTES] == 0:
                raise DatosInvalidosError(
                    f"{exc} Es el detalle guardado en el expediente: vuelve a guardarlo desde el simulador."
                ) from exc
            raise
        if not hijos:
            # An empty list is "no detail": the counts are whatever the layers say.
            del merged[DESCENDIENTES]
        else:
            merged[DESCENDIENTES] = hijos
            capa_detalle = origen[DESCENDIENTES]
            for hijo in hijos:
                for campo, importe in (hijo.get("gastos") or {}).items():
                    por_hijo[campo] = por_hijo.get(campo, Decimal(0)) + Decimal(str(importe))
            for campo in (*RECUENTOS_DEL_DETALLE, *por_hijo):
                if campo in merged and (origen[campo] == 0 or origen[campo] < capa_detalle):
                    del merged[campo]

    def cede(total: str, capa_partes: int) -> bool:
        return origen[total] == 0 or origen[total] < capa_partes

    for total, partes in GASTOS_DESGLOSADOS.items():
        importe_total = _decimal(merged.get(total))
        if not importe_total:
            continue
        suma, capa_partes = Decimal(0), -1
        for parte in partes:
            if parte in merged:
                importe = _decimal(merged[parte])
                capa = origen[parte]
            elif parte in por_hijo:
                importe, capa = por_hijo[parte], capa_detalle
            else:
                continue
            if importe:
                suma += importe
                capa_partes = max(capa_partes, capa)
        if suma > importe_total + Decimal("0.01") and cede(total, capa_partes):
            # The calculator takes a missing total as the sum of its parts.
            del merged[total]

    # The same for the counts that hold others; a part may come from the children's detail.
    del_detalle: dict[str, int] = {}
    if capa_detalle >= 0:
        grados = [(int(h.get("grado_discapacidad") or 0), bool(h.get("necesita_asistencia"))) for h in hijos]
        del_detalle = {
            "num_descendientes": len(grados),
            "num_descendientes_discapacidad": sum(1 for g, _ in grados if g >= 33),
            "num_descendientes_discapacidad_65": sum(1 for g, _ in grados if g >= 65),
            "num_descendientes_asistencia": sum(1 for g, a in grados if 33 <= g < 65 and a),
        }
    recuentos: dict[str, tuple[Decimal, int]] = {}

    def recuento(nombre: str) -> tuple[Decimal, int]:
        """(count, layer it comes from) as the calculator will see it."""
        if nombre in recuentos:
            return recuentos[nombre]
        if nombre in merged:
            return _decimal(merged[nombre]) or Decimal(0), origen[nombre]
        if nombre in del_detalle:
            return Decimal(del_detalle[nombre]), capa_detalle
        return Decimal(0), -1

    for total, partes in RECUENTOS_DESGLOSADOS.items():
        valores = [recuento(parte) for parte in partes]
        suma = sum((valor for valor, _ in valores), Decimal(0))
        capa_partes = max((capa for valor, capa in valores if valor), default=-1)
        actual, _ = recuento(total)
        if actual and suma > actual and total in merged and cede(total, capa_partes):
            # Left out, the calculator takes the sum of the parts.
            del merged[total]
            actual = Decimal(0)
        if not actual:
            recuentos[total] = (suma, capa_partes)


def _merge_irpf(*sources: dict[str, Any] | None) -> dict[str, Any]:
    """Calculator fields of the layers, the later (more specific) layer winning.

    The first layer is taken to be the stored case facts (see :func:`_conciliar`).
    """
    merged: dict[str, Any] = {}
    origen: dict[str, int] = {}
    for capa, source in enumerate(sources):
        for name, value in (source or {}).items():
            if name in IRPFInput.model_fields and name != "ejercicio" and value is not None:
                # Counts arrive as floats from the fact store (NUMBER facts).
                if name in _INTEGER_FIELDS and isinstance(value, float) and value.is_integer():
                    value = int(value)
                merged[name] = value
                origen[name] = capa
    _conciliar(merged, origen)
    return merged


def _irpf_input(ejercicio: int, merged: dict[str, Any]) -> IRPFInput:
    try:
        return IRPFInput(ejercicio=ejercicio, **merged)
    except ValidationError as exc:
        pista = (
            " Con el detalle de cada hijo («descendientes») no hace falta indicar los recuentos de hijos ni los "
            "totales de los gastos repartidos por hijo: se calculan a partir del detalle. Quita esos campos o "
            "corrige el detalle."
            if merged.get(DESCENDIENTES)
            and any(
                e["type"] == "value_error"
                and ("detalle de descendientes" in e["msg"] or "desglose por hijo" in e["msg"])
                for e in exc.errors()
            )
            else ""
        )
        raise DatosInvalidosError(f"Datos no válidos para el cálculo: {_problemas(exc)}.{pista}") from exc


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
        raise DatosInvalidosError(f"Datos de la actividad no válidos: {_problemas(exc)}") from exc
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
