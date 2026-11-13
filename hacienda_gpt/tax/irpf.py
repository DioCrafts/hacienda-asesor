"""IRPF calculator: a pure function from a taxpayer's figures to the result.

Scope (state-level rules, residents, one taxpayer or one family unit):
work income with its expenses and reduction, other general-base income given
net, savings income, joint-filing and pension-plan reductions, personal and
family minimums, general and savings scales, donation / transitional-housing /
work-income / maternity deductions, and withholdings.

What it does NOT model is reported in ``avisos`` on every result rather than
silently ignored: autonomous-community scales that are not loaded, regional
deductions and minimums, carry-forward of losses, pro-rating by months, and
the detailed rules for rental and business income (those are taken as net
figures the user supplies).
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hacienda_gpt.tax import autonomicas
from hacienda_gpt.tax.arrendamiento import DatosArrendamiento, reduccion_arrendamiento
from hacienda_gpt.tax.compensacion import compensar_ahorro
from hacienda_gpt.tax.params import (
    CAMPOS_GASTO,
    GASTOS_DESGLOSADOS,
    DeduccionesMadrid,
    Escala,
    EtapaEducativa,
    Fuente,
    IRPFParams,
    Minimos,
    RepartoDeducciones,
    _MadridEducacion,
    load_params,
    normalizar_comunidad,
)
from hacienda_gpt.tax.vigencia_normas import estado_de

CENT = Decimal("0.01")
ZERO = Decimal(0)

Tributacion = Literal["individual", "conjunta_biparental", "conjunta_monoparental"]


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def repartir_deducciones_estatales(deducciones: Decimal, reparto: RepartoDeducciones) -> tuple[Decimal, Decimal]:
    """(State share, regional share) of the general State deductions (arts. 67.1.b and 77.1.a LIRPF).

    The regional share is ``parte_autonomica`` of the total, rounded to the cent; the State bears the rest. Used by
    :func:`calcular_irpf` and, with proposed figures, by the re-encoding loop's test cases.
    """
    autonomica = _round(deducciones * reparto.parte_autonomica)
    return _round(deducciones) - autonomica, autonomica


class Descendiente(BaseModel):
    """One child who gives the right to the minimum for descendants, with the facts
    regional deductions depend on. Give the age or the date of birth (or both)."""

    model_config = ConfigDict(extra="forbid")

    edad: int | None = Field(default=None, ge=0, le=130)  # reached at 31 December of the tax year
    fecha_nacimiento: date | None = None
    grado_discapacidad: int = Field(default=0, ge=0, le=100)
    etapa_educativa: EtapaEducativa | None = None
    ejercicio_adopcion: int | None = Field(default=None, ge=1900, le=2200)  # tax year of the adoption, if adopted
    adopcion_internacional: bool = False
    parto_multiple: bool = False  # born in a multiple birth, or adopted with others on the same date
    meses_guarderia: int | None = Field(default=None, ge=0, le=12)  # months at nursery in the tax year
    # This child's share of expense inputs (``params.CAMPOS_GASTO``), e.g. {"gastos_guarderia": 1800}.
    gastos: dict[str, Decimal] = Field(default_factory=dict)

    def llegada(self, ejercicio: int) -> int:
        """Tax years since the birth or, for adopted children, the adoption (0 = this year)."""
        assert self.edad is not None
        return ejercicio - (self.ejercicio_adopcion or ejercicio - self.edad)


class PrevisionSocialMiembro(BaseModel):
    """One member's own figures inside a joint return, for the limits of art. 52 LIRPF that art. 84.2.1.º
    applies member by member: their contributions and the income their 30 % cap is worked out from."""

    model_config = ConfigDict(extra="forbid")

    aportacion_plan_pensiones: Decimal = Field(default=ZERO, ge=0)
    contribucion_empresa_plan_pensiones: Decimal = Field(default=ZERO, ge=0)
    rendimiento_trabajo_bruto: Decimal = Field(default=ZERO, ge=0)
    cotizaciones_seguridad_social: Decimal = Field(default=ZERO, ge=0)
    otros_gastos_deducibles_trabajo: Decimal = Field(default=ZERO, ge=0)
    rendimiento_actividad_neto: Decimal = ZERO


class IRPFInput(BaseModel):
    """One scenario. Amounts are annual, in euros; absent means zero."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    comunidad_autonoma: str | None = None
    tributacion: Tributacion = "individual"

    # Personal and family circumstances
    edad: int | None = Field(default=None, ge=0, le=130)
    grado_discapacidad: int = Field(default=0, ge=0, le=100)
    num_descendientes: int = Field(default=0, ge=0, le=20)
    num_descendientes_menores_3: int = Field(default=0, ge=0, le=20)
    # True when the minimum for descendants is shared with the other parent (50 %).
    descendientes_compartidos: bool = False
    num_ascendientes_mayores_65: int = Field(default=0, ge=0, le=10)
    num_ascendientes_mayores_75: int = Field(default=0, ge=0, le=10)

    # General base
    rendimiento_trabajo_bruto: Decimal = Field(default=ZERO, ge=0)
    cotizaciones_seguridad_social: Decimal = Field(default=ZERO, ge=0)
    otros_gastos_deducibles_trabajo: Decimal = Field(default=ZERO, ge=0)  # art. 19.2 b) to e)
    rendimiento_actividad_neto: Decimal = ZERO
    rendimiento_alquiler_neto: Decimal = ZERO  # net and already reduced
    # Letting a dwelling: net income (rent minus deductible expenses) BEFORE the
    # art. 23.2 reduction, which the calculator works out from the contract facts.
    rendimiento_alquiler_vivienda_sin_reducir: Decimal = ZERO
    arrendamiento_fecha_contrato: date | None = None
    arrendamiento_gran_tenedor: bool = False
    arrendamiento_primer_alquiler: bool = False
    # Initial rent vs the last rent of the previous contract, in percent (-6 = lowered by 6 %).
    arrendamiento_variacion_renta: Decimal | None = None
    arrendamiento_mismo_inquilino: bool = False
    arrendamiento_renta_bajo_indice: bool | None = None
    arrendamiento_zona_tensionada: bool = False
    arrendamiento_inquilino_joven: bool = False
    arrendamiento_alquiler_social: bool = False
    arrendamiento_vivienda_rehabilitada: bool = False
    arrendamiento_prorroga_tacita: bool = False
    otras_rentas_base_general: Decimal = ZERO

    # Savings base
    rendimiento_capital_mobiliario: Decimal = ZERO
    ganancias_patrimoniales: Decimal = ZERO
    # Negative savings balances brought forward from the four previous years (positive figures).
    rendimientos_negativos_pendientes: Decimal = Field(default=ZERO, ge=0)
    perdidas_patrimoniales_pendientes: Decimal = Field(default=ZERO, ge=0)

    # Reductions and deductions
    aportacion_plan_pensiones: Decimal = Field(default=ZERO, ge=0)
    contribucion_empresa_plan_pensiones: Decimal = Field(default=ZERO, ge=0)
    # Joint return only: each member's contributions and own income, since art. 84.2.1.º LIRPF applies the
    # art. 52 limits member by member. The totals above (contributions, work and activity income) are then
    # their sums. ``conjunta._conjunta`` fills it from each spouse's figures.
    prevision_social_por_miembro: list[PrevisionSocialMiembro] | None = Field(default=None, min_length=1, max_length=20)
    donativos: Decimal = Field(default=ZERO, ge=0)
    donativos_recurrentes: bool = False
    pagos_vivienda_habitual_pre2013: Decimal = Field(default=ZERO, ge=0)
    # Maternity deduction: number of children under 3 the taxpayer claims it for.
    hijos_deduccion_maternidad: int = Field(default=0, ge=0, le=20)
    gastos_guarderia: Decimal = Field(default=ZERO, ge=0)

    # Family unit, for regional income ceilings
    # Taxable bases (general + savings) of the OTHER members of the family unit.
    base_imponible_resto_unidad_familiar: Decimal = Field(default=ZERO, ge=0)
    miembros_unidad_familiar: int | None = Field(default=None, ge=1, le=20)
    familia_numerosa: bool = False
    familia_numerosa_especial: bool = False
    familia_monoparental: bool = False
    hijos_nacidos_en_el_ejercicio: int = Field(default=0, ge=0, le=20)  # born or adopted in the tax year
    gastos_libros_texto: Decimal = Field(default=ZERO, ge=0)  # textbooks and school material
    # Paid to carers, nurseries or centres for looking after children, elders or disabled relatives.
    gastos_cuidado_familiares: Decimal = Field(default=ZERO, ge=0)
    # Ascendants or descendants with a disability who depend on the taxpayer.
    num_familiares_discapacidad_a_cargo: int = Field(default=0, ge=0, le=20)
    # Inputs of regional deductions beyond the family ones (see params.CAMPOS_*).
    menores_acogidos: int = Field(default=0, ge=0, le=20)
    hijos_adoptados_en_el_ejercicio: int = Field(default=0, ge=0, le=20)
    donativos_autonomicos: Decimal = Field(default=ZERO, ge=0)  # to the entities the regional law names
    inversion_vivienda_habitual: Decimal = Field(default=ZERO, ge=0)  # paid to buy or rehabilitate the main home
    inversion_empresas_nueva_creacion: Decimal = Field(default=ZERO, ge=0)
    gastos_eficiencia_energetica: Decimal = Field(default=ZERO, ge=0)
    gastos_salud: Decimal = Field(default=ZERO, ge=0)
    gastos_deporte_cultura: Decimal = Field(default=ZERO, ge=0)
    viudedad: bool = False
    vivienda_en_municipio_rural: bool = False
    # Lives in a municipality the regional law treats as small, rural or at risk of depopulation.
    municipio_rural_o_despoblacion: bool = False

    # Regional (Comunidad de Madrid) deductions
    hijos_nacidos_ultimos_3_ejercicios: int = Field(default=0, ge=0, le=20)  # born/adopted this year or the two before
    alquiler_vivienda_habitual: Decimal = Field(default=ZERO, ge=0)  # rent paid in the year
    gastos_escolaridad: Decimal = Field(default=ZERO, ge=0)
    gastos_ensenanza_idiomas: Decimal = Field(default=ZERO, ge=0)
    gastos_uniformes_escolares: Decimal = Field(default=ZERO, ge=0)
    num_hijos_gastos_educativos: int | None = Field(default=None, ge=0, le=20)
    num_hijos_primer_ciclo_infantil: int = Field(default=0, ge=0, le=20)
    cotizaciones_empleada_hogar: Decimal = Field(default=ZERO, ge=0)
    donativos_fundaciones_madrid: Decimal = Field(default=ZERO, ge=0)

    # ---- Finer facts for regional deductions (see params.CAMPOS_* and DeduccionCalculable) ----
    # One entry per child giving the right to the minimum for descendants. When given,
    # the counts above that are left at zero are filled in from it.
    descendientes: list[Descendiente] | None = None
    edad_conyuge: int | None = Field(default=None, ge=0, le=130)
    grado_discapacidad_conyuge: int = Field(default=0, ge=0, le=100)
    # Ascendants or descendants with a disability of 65 % or more who depend on the taxpayer.
    num_familiares_discapacidad_65_a_cargo: int = Field(default=0, ge=0, le=20)
    # Over-65s or disabled people, not close relatives, taken in without payment.
    num_personas_acogidas_mayores_o_discapacidad: int = Field(default=0, ge=0, le=20)
    # Inhabitants of the municipality of residence (each regional law has its own threshold).
    habitantes_municipio: int | None = Field(default=None, ge=0)
    # Parts of the year
    dias_alquiler_vivienda_habitual: int | None = Field(default=None, ge=0, le=366)  # lease in force
    dias_acogimiento: int | None = Field(default=None, ge=0, le=366)  # living with the fostered minors
    dias_desempleo: int | None = Field(default=None, ge=0, le=366)
    # Tax returns (yours included) the deduction for the same lease / the same home is shared between.
    num_declaraciones_mismo_alquiler: int | None = Field(default=None, ge=1, le=20)
    num_declaraciones_misma_vivienda: int | None = Field(default=None, ge=1, le=20)
    # Decisive eligibility facts
    progenitores_trabajan: bool = False  # you and, if any, the other parent earn work or business income
    victima_violencia_genero: bool = False
    victima_terrorismo: bool = False
    vivienda_protegida: bool = False
    vivienda_nueva_construccion: bool = False
    primera_vivienda: bool = False
    ejercicio_viudedad: int | None = Field(default=None, ge=1900, le=2200)
    ejercicio_adquisicion_vivienda: int | None = Field(default=None, ge=1900, le=2200)
    ejercicio_traslado_residencia: int | None = Field(default=None, ge=1900, le=2200)
    # Narrower expenses: parts of the broad inputs above (see params.GASTOS_DESGLOSADOS).
    gastos_instalaciones_renovables: Decimal = Field(default=ZERO, ge=0)  # part of gastos_eficiencia_energetica
    gastos_deporte: Decimal = Field(default=ZERO, ge=0)  # parts of gastos_deporte_cultura
    gastos_cultura: Decimal = Field(default=ZERO, ge=0)
    gastos_honorarios_sanitarios: Decimal = Field(default=ZERO, ge=0)  # parts of gastos_salud
    gastos_salud_bucodental: Decimal = Field(default=ZERO, ge=0)
    gastos_salud_mental: Decimal = Field(default=ZERO, ge=0)
    gastos_optica: Decimal = Field(default=ZERO, ge=0)
    gastos_enfermedades_raras: Decimal = Field(default=ZERO, ge=0)
    gastos_ela: Decimal = Field(default=ZERO, ge=0)
    pagos_adquisicion_vivienda: Decimal = Field(default=ZERO, ge=0)  # parts of inversion_vivienda_habitual
    pagos_rehabilitacion_vivienda: Decimal = Field(default=ZERO, ge=0)
    intereses_prestamo_vivienda: Decimal = Field(default=ZERO, ge=0)
    donativos_medio_ambiente: Decimal = Field(default=ZERO, ge=0)  # parts of donativos_autonomicos
    donativos_cultura_patrimonio: Decimal = Field(default=ZERO, ge=0)
    donativos_investigacion: Decimal = Field(default=ZERO, ge=0)
    donativos_cooperacion_fines_sociales: Decimal = Field(default=ZERO, ge=0)
    donativos_lengua_propia: Decimal = Field(default=ZERO, ge=0)
    donativos_fundaciones_autonomicas: Decimal = Field(default=ZERO, ge=0)
    # Expenses with no broader input
    gastos_veterinarios: Decimal = Field(default=ZERO, ge=0)
    adquisicion_vehiculo_electrico: Decimal = Field(default=ZERO, ge=0)
    inversion_economia_social: Decimal = Field(default=ZERO, ge=0)

    retenciones: Decimal = Field(default=ZERO, ge=0)

    @model_validator(mode="after")
    def _coherencia_de_los_desgloses(self) -> IRPFInput:
        """Keep totals and their breakdowns consistent: a total left at zero is the
        sum of its parts, and a breakdown may never exceed (or, per child, differ from) it."""
        if self.descendientes:
            for hijo in self.descendientes:
                if hijo.fecha_nacimiento is not None:
                    edad = self.ejercicio - hijo.fecha_nacimiento.year
                    if edad < 0 or (hijo.edad is not None and hijo.edad != edad):
                        raise ValueError("descendientes: la edad no cuadra con la fecha de nacimiento y el ejercicio")
                    hijo.edad = edad
                if hijo.edad is None:
                    raise ValueError("descendientes: cada uno necesita la edad o la fecha de nacimiento")
                if set(hijo.gastos) - CAMPOS_GASTO or any(v < 0 for v in hijo.gastos.values()):
                    raise ValueError(f"descendientes: gastos desconocidos o negativos: {sorted(hijo.gastos)}")
                if hijo.ejercicio_adopcion is not None and not (
                    self.ejercicio - hijo.edad <= hijo.ejercicio_adopcion <= self.ejercicio
                ):
                    raise ValueError("descendientes: el ejercicio de adopción no cuadra con la edad")
            hijos = self.descendientes
            recuentos = {
                "num_descendientes": len(hijos),
                "num_descendientes_menores_3": sum(1 for h in hijos if (h.edad or 0) < 3),
                "hijos_nacidos_en_el_ejercicio": sum(1 for h in hijos if h.llegada(self.ejercicio) == 0),
                "hijos_nacidos_ultimos_3_ejercicios": sum(1 for h in hijos if h.llegada(self.ejercicio) <= 2),
                "hijos_adoptados_en_el_ejercicio": sum(1 for h in hijos if h.ejercicio_adopcion == self.ejercicio),
                "num_hijos_primer_ciclo_infantil": sum(
                    1 for h in hijos if h.etapa_educativa == "infantil_primer_ciclo"
                ),
            }
            for campo, segun_detalle in recuentos.items():
                if not getattr(self, campo):
                    setattr(self, campo, segun_detalle)
                elif getattr(self, campo) != segun_detalle:
                    raise ValueError(
                        f"«{campo}» ({getattr(self, campo)}) no cuadra con el detalle de descendientes "
                        f"({segun_detalle})"
                    )
            for campo in sorted({g for h in hijos for g in h.gastos}):
                suma = sum((h.gastos.get(campo, ZERO) for h in hijos), ZERO)
                if not getattr(self, campo):
                    setattr(self, campo, suma)
                elif abs(getattr(self, campo) - suma) > CENT:
                    raise ValueError(
                        f"el desglose por hijo de «{campo}» ({suma:.2f} €) no coincide con el total "
                        f"({getattr(self, campo):.2f} €)"
                    )
        for total, partes in GASTOS_DESGLOSADOS.items():
            suma = sum((getattr(self, parte) for parte in partes), ZERO)
            if not getattr(self, total):
                setattr(self, total, suma)
            elif suma > getattr(self, total) + CENT:
                raise ValueError(
                    f"«{total}» ({getattr(self, total):.2f} €) es el total de {', '.join(partes)}, que suman más "
                    f"({suma:.2f} €)"
                )
        return self

    @model_validator(mode="after")
    def _miembros_de_la_conjunta(self) -> IRPFInput:
        """The members' figures of a joint return add up to its totals (a total left at zero is their sum)."""
        miembros = self.prevision_social_por_miembro
        if miembros is None:
            return self
        if self.tributacion == "individual":
            raise ValueError("«prevision_social_por_miembro» solo se usa en la declaración conjunta")
        for campo in PrevisionSocialMiembro.model_fields:
            suma = sum((getattr(m, campo) for m in miembros), ZERO)
            if not getattr(self, campo):
                setattr(self, campo, suma)
            elif abs(getattr(self, campo) - suma) > CENT:
                raise ValueError(
                    f"«{campo}» ({getattr(self, campo):.2f} €) no coincide con la suma de los miembros de la unidad "
                    f"familiar en «prevision_social_por_miembro» ({suma:.2f} €)"
                )
        return self


class Linea(BaseModel):
    """One line of the computation, traceable to its legal basis."""

    model_config = ConfigDict(extra="forbid")

    concepto: str
    importe: Decimal
    referencia: str | None = None


class IRPFResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    tributacion: Tributacion
    base_imponible_general: Decimal
    base_imponible_ahorro: Decimal
    base_liquidable_general: Decimal
    base_liquidable_ahorro: Decimal
    minimo_personal_familiar: Decimal
    # Minimum used for the regional share (differs when the community sets its own).
    minimo_autonomico: Decimal
    # Community whose scale and minimums were applied; None = approximated.
    comunidad_aplicada: str | None = None
    cuota_integra_estatal: Decimal
    cuota_integra_autonomica: Decimal
    cuota_liquida: Decimal
    retenciones: Decimal
    # Positive: to pay. Negative: refund.
    resultado: Decimal
    tipo_medio_efectivo: Decimal  # cuota líquida / total base liquidable, in %
    # Negative savings balances left for the following years.
    rendimientos_negativos_a_compensar: Decimal = ZERO
    perdidas_patrimoniales_a_compensar: Decimal = ZERO
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


class _Trace:
    def __init__(self) -> None:
        self.lineas: list[Linea] = []
        self.avisos: list[str] = []
        self.no_verificados: list[str] = []

    def linea(self, concepto: str, importe: Decimal, fuente: Fuente | str | None = None) -> None:
        referencia = fuente.referencia if isinstance(fuente, Fuente) else fuente
        self.lineas.append(Linea(concepto=concepto, importe=_round(importe), referencia=referencia))
        if isinstance(fuente, Fuente) and not fuente.verificado and fuente.referencia not in self.no_verificados:
            self.no_verificados.append(fuente.referencia)

    def aviso(self, text: str) -> None:
        if text not in self.avisos:
            self.avisos.append(text)


def _rendimientos_para_prevision_social(bruto: Decimal, gastos: Decimal, actividad: Decimal, p: IRPFParams) -> Decimal:
    """Net work income (after the art. 19.2 expenses, before the art. 20 reduction) plus net activity income:
    what the 30 % cap of art. 52.1.a) is applied to."""
    neto_trabajo = ZERO
    if bruto:
        neto_previo = bruto - gastos
        neto_trabajo = max(ZERO, neto_previo - max(ZERO, min(p.trabajo.otros_gastos, neto_previo)))
    return neto_trabajo + actividad


def _reduccion_trabajo(neto_previo: Decimal, otras_rentas: Decimal, p: IRPFParams) -> Decimal:
    """Art. 20: reduction for low work income, phased out in two slopes."""
    r = p.trabajo.reduccion
    if neto_previo >= r.umbral_superior or otras_rentas > r.max_otras_rentas:
        return ZERO
    if neto_previo <= r.umbral_inferior:
        return r.importe_maximo
    if neto_previo <= r.umbral_medio:
        return r.importe_maximo - r.pendiente_tramo_1 * (neto_previo - r.umbral_inferior)
    return r.importe_tramo_2 - r.pendiente_tramo_2 * (neto_previo - r.umbral_medio)


def _minimo_descendientes(inp: IRPFInput, m: Minimos) -> Decimal:
    descendientes = ZERO
    for index in range(inp.num_descendientes):
        descendientes += m.descendientes[min(index, len(m.descendientes) - 1)]
    descendientes += m.incremento_descendiente_menor_3 * min(inp.num_descendientes_menores_3, inp.num_descendientes)
    if descendientes and inp.descendientes_compartidos:
        descendientes /= 2
    return descendientes


def _incremento_por_edad(edad: int | None, m: Minimos) -> Decimal:
    """Art. 57.2: what the taxpayer minimum rises by over 65 and over 75."""
    incremento = ZERO
    if edad is not None and edad > 65:
        incremento += m.incremento_mayor_65
    if edad is not None and edad > 75:
        incremento += m.incremento_mayor_75
    return incremento


def _minimo_discapacidad_propia(grado: int, m: Minimos) -> Decimal:
    """Art. 60.1: the taxpayer's own disability minimum, with the assistance expenses from 65 %."""
    if grado >= 65:
        return m.discapacidad_65 + m.gastos_asistencia
    if grado >= 33:
        return m.discapacidad
    return ZERO


def _minimo(inp: IRPFInput, m: Minimos, t: _Trace | None = None) -> Decimal:
    """Personal and family minimum under ``m`` (state amounts, or a community's).

    A joint return with both spouses has one taxpayer minimum (art. 57.1) but the age increments
    (art. 57.2) and disability minimum (art. 60.1) of each spouse: art. 84.2.2.º LIRPF, «se tendrán en
    cuenta las circunstancias personales de cada uno de los cónyuges». The other spouse's are
    ``edad_conyuge`` and ``grado_discapacidad_conyuge``. With a trace, each component is its own line.
    """
    total = m.contribuyente + _incremento_por_edad(inp.edad, m)

    descendientes = _minimo_descendientes(inp, m)

    mayores_75 = min(inp.num_ascendientes_mayores_75, inp.num_ascendientes_mayores_65)
    ascendientes = m.ascendiente * inp.num_ascendientes_mayores_65 + m.incremento_ascendiente_mayor_75 * mayores_75

    discapacidad = _minimo_discapacidad_propia(inp.grado_discapacidad, m)

    edad_conyuge = discapacidad_conyuge = ZERO
    if inp.tributacion == "conjunta_biparental":
        edad_conyuge = _incremento_por_edad(inp.edad_conyuge, m)
        discapacidad_conyuge = _minimo_discapacidad_propia(inp.grado_discapacidad_conyuge, m)

    if t is not None:
        t.linea("Mínimo del contribuyente", total, m.fuente)
        if edad_conyuge:
            t.linea(
                "Incremento del mínimo por la edad del cónyuge (conjunta, art. 84.2.2.º LIRPF)", edad_conyuge, m.fuente
            )
        if discapacidad_conyuge:
            t.linea(
                "Mínimo por discapacidad del cónyuge (conjunta, art. 84.2.2.º LIRPF)", discapacidad_conyuge, m.fuente
            )
        if descendientes:
            label = "Mínimo por descendientes" + (" (50 %, compartido)" if inp.descendientes_compartidos else "")
            t.linea(label, descendientes, m.fuente)
            t.aviso(
                "El mínimo por descendientes exige que convivan contigo, sean menores de 25 años (o con discapacidad) "
                "y no tengan rentas superiores a 8.000 €; el cálculo da por cumplidos esos requisitos."
            )
        if ascendientes:
            t.linea("Mínimo por ascendientes", ascendientes, m.fuente)
        if discapacidad:
            t.linea("Mínimo por discapacidad del contribuyente", discapacidad, m.fuente)

    return total + edad_conyuge + descendientes + ascendientes + discapacidad + discapacidad_conyuge


def _cuota_con_minimo(escala: Escala, base: Decimal, minimo: Decimal) -> Decimal:
    """Scale applied to the base, minus the scale applied to the minimum (arts. 63 and 66)."""
    return _round(escala.cuota(base)) - _round(escala.cuota(min(minimo, base)))


_MADRID_INPUTS = (
    "hijos_nacidos_ultimos_3_ejercicios",
    "alquiler_vivienda_habitual",
    "gastos_escolaridad",
    "gastos_ensenanza_idiomas",
    "gastos_uniformes_escolares",
    "cotizaciones_empleada_hogar",
    "donativos_fundaciones_madrid",
)


_GASTOS_EDUCATIVOS_MADRID = ("gastos_escolaridad", "gastos_ensenanza_idiomas", "gastos_uniformes_escolares")


def _gastos_educativos_madrid(inp: IRPFInput, e: _MadridEducacion, hijos: int) -> tuple[Decimal, bool]:
    """Art. 11 DLeg. 1/2010 before sharing between parents: (amount, computed child by child?).

    The cap is per child and on that child's own expenses. Art. 11.4 raises it «por cada
    uno de los hijos» once «el contribuyente tuviese derecho a practicar deducción por
    gastos de escolaridad», so the higher cap holds for every child as soon as there are
    schooling fees; for a child in the first cycle of infant education only the schooling
    counts. Child by child when ``descendientes`` breaks the three
    expenses down; otherwise, with several children, a single cap is granted (the
    lowest any distribution of the expenses could give).
    """

    def de_un_hijo(escolaridad: Decimal, idiomas: Decimal, vestuario: Decimal, *, primer_ciclo: bool) -> Decimal:
        if primer_ciclo:
            return min(escolaridad * e.porcentaje_escolaridad_idiomas, e.maximo_por_hijo_primer_ciclo_infantil)
        bruto = (escolaridad + idiomas) * e.porcentaje_escolaridad_idiomas + vestuario * e.porcentaje_vestuario
        return min(bruto, e.maximo_por_hijo_con_escolaridad if hay_escolaridad else e.maximo_por_hijo)

    totales = [getattr(inp, campo) for campo in _GASTOS_EDUCATIVOS_MADRID]
    hay_escolaridad = totales[0] > ZERO
    detalle = inp.descendientes or []
    desglosado = [sum((h.gastos.get(campo, ZERO) for h in detalle), ZERO) for campo in _GASTOS_EDUCATIVOS_MADRID]
    if detalle and all(abs(suma - total) <= CENT for suma, total in zip(desglosado, totales, strict=True)):
        return (
            sum(
                (
                    de_un_hijo(
                        *(h.gastos.get(campo, ZERO) for campo in _GASTOS_EDUCATIVOS_MADRID),
                        primer_ciclo=h.etapa_educativa == "infantil_primer_ciclo",
                    )
                    for h in detalle
                ),
                ZERO,
            ),
            True,
        )
    infantil = min(inp.num_hijos_primer_ciclo_infantil, hijos)
    if hijos == 1:
        return de_un_hijo(*totales, primer_ciclo=infantil == 1), True
    # Several children and no breakdown: the worst case is every expense belonging to one child.
    posibles = [de_un_hijo(*totales, primer_ciclo=False)] if infantil < hijos else []
    if infantil:
        posibles.append(de_un_hijo(*totales, primer_ciclo=True))
    return min(posibles), False


def _deducciones_madrid(
    inp: IRPFInput,
    d: DeduccionesMadrid,
    t: _Trace,
    *,
    base_imponible: Decimal,
    base_liquidable: Decimal,
    cuota_autonomica: Decimal,
    estatales_sobre_autonomica: Decimal,
    otras_autonomicas: Decimal = ZERO,
) -> Decimal:
    """Madrid deductions (DLeg. 1/2010). Each one that is claimed but fails a
    requirement is explained in an aviso instead of being silently dropped."""
    conjunta = inp.tributacion != "individual"
    base_familia = base_imponible + inp.base_imponible_resto_unidad_familiar
    miembros = inp.miembros_unidad_familiar
    if miembros is None:
        miembros = 1 + inp.num_descendientes + (1 if inp.tributacion == "conjunta_biparental" else 0)
    if not conjunta and not inp.base_imponible_resto_unidad_familiar and any(getattr(inp, n) for n in _MADRID_INPUTS):
        t.aviso(
            "Varias deducciones madrileñas tienen un límite de renta de toda la unidad familiar. No consta la renta de "
            "tu cónyuge: si estás casado, indícala en «base_imponible_resto_unidad_familiar» o las deducciones pueden "
            "salir concedidas sin corresponderte."
        )
    # Shared between both parents when they file separately.
    reparto = Decimal("0.5") if inp.descendientes_compartidos and not conjunta else Decimal(1)
    total = ZERO

    def fuera(nombre: str, motivo: str) -> None:
        t.aviso(f"Deducción madrileña por {nombre} no aplicada: {motivo}")

    # Art. 4 — birth or adoption
    if inp.hijos_nacidos_ultimos_3_ejercicios:
        n = d.nacimiento
        limite = n.limite_base_conjunta if conjunta else n.limite_base_individual
        if base_imponible > limite:
            fuera("nacimiento o adopción", f"tu base imponible ({base_imponible:.2f} €) supera {limite:.2f} €.")
        elif base_familia > n.limite_base_unidad_familiar:
            fuera(
                "nacimiento o adopción",
                f"la suma de bases de la unidad familiar ({base_familia:.2f} €) supera {n.limite_base_unidad_familiar:.2f} €.",
            )
        else:
            importe = n.importe_por_hijo * inp.hijos_nacidos_ultimos_3_ejercicios * reparto
            t.linea("Deducción autonómica por nacimiento o adopción de hijos", -importe, n.fuente)
            t.aviso(
                "La deducción por nacimiento exige convivir con el hijo; en partos o adopciones múltiples el primer "
                "año se incrementa en 721,70 € por hijo, lo que no se calcula aquí."
            )
            total += importe

    # Art. 8 — rent, under 40
    if inp.alquiler_vivienda_habitual:
        a = d.alquiler
        limite = a.limite_base_conjunta if conjunta else a.limite_base_individual
        minimo_renta = a.porcentaje_minimo_sobre_base * base_imponible
        if inp.edad is None:
            fuera("alquiler", f"falta tu edad (solo para menores de {a.edad_maxima_exclusiva} años).")
        elif inp.edad >= a.edad_maxima_exclusiva:
            fuera("alquiler", f"es solo para menores de {a.edad_maxima_exclusiva} años.")
        elif base_imponible > limite:
            fuera("alquiler", f"tu base imponible ({base_imponible:.2f} €) supera {limite:.2f} €.")
        elif base_familia > a.limite_base_unidad_familiar:
            fuera(
                "alquiler",
                f"la suma de bases de la unidad familiar ({base_familia:.2f} €) supera {a.limite_base_unidad_familiar:.2f} €.",
            )
        elif inp.alquiler_vivienda_habitual <= minimo_renta:
            fuera("alquiler", f"el alquiler pagado no supera el 20 % de tu base imponible ({minimo_renta:.2f} €).")
        else:
            importe = min(inp.alquiler_vivienda_habitual * a.porcentaje, a.maximo)
            t.linea("Deducción autonómica por alquiler de vivienda habitual", -importe, a.fuente)
            t.aviso(
                "La deducción madrileña por alquiler exige conservar el justificante del depósito de la fianza (o la "
                "denuncia por no haberlo recibido), art. 18.4.c) del DLeg. 1/2010; no se ha comprobado."
            )
            total += importe

    limite_familiar_gastos = d.gastos_educativos.limite_base_por_miembro * miembros
    excede_familiar = base_familia > limite_familiar_gastos
    motivo_familiar = (
        f"la base imponible de la unidad familiar ({base_familia:.2f} €) supera {limite_familiar_gastos:.2f} € "
        f"(30.930 € × {miembros} miembros)."
    )
    usa_limite_familiar = False

    # Art. 11 — education expenses
    gastos = inp.gastos_escolaridad + inp.gastos_ensenanza_idiomas + inp.gastos_uniformes_escolares
    if gastos:
        e = d.gastos_educativos
        hijos = (
            inp.num_hijos_gastos_educativos if inp.num_hijos_gastos_educativos is not None else inp.num_descendientes
        )
        usa_limite_familiar = True
        if hijos == 0:
            fuera("gastos educativos", "no constan hijos por los que apliques el mínimo por descendientes.")
        elif excede_familiar:
            fuera("gastos educativos", motivo_familiar)
        else:
            importe, exacto = _gastos_educativos_madrid(inp, e, hijos)
            importe *= reparto
            t.linea("Deducción autonómica por gastos educativos", -importe, e.fuente)
            t.aviso(
                (
                    "En gastos educativos el máximo se ha aplicado hijo a hijo con el desglose que has dado "
                    f"({e.maximo_por_hijo_con_escolaridad:.2f} € por hijo cuando hay gastos de escolaridad, "
                    f"{e.maximo_por_hijo:.2f} € si no los hay). "
                    if exacto
                    else (
                        "En gastos educativos la ley fija el máximo por cada hijo y sobre sus propios gastos. Como no "
                        f"consta el reparto de los gastos entre tus {hijos} hijos, se ha aplicado un único máximo, como "
                        "si todos los gastos fueran de un solo hijo (puede salir de menos): indica en «descendientes» "
                        "los gastos de cada hijo y su etapa educativa para calcularlo con exactitud. "
                        if hijos > 1
                        else ""
                    )
                )
                + "Los gastos deben ir ya minorados en becas; la escolaridad "
                "pública o concertada no genera deducción, y escolaridad y vestuario solo cuentan en Infantil, Básica "
                "Obligatoria y FP Básica (no en Bachillerato). En primer ciclo de Infantil solo cuenta la escolaridad."
            )
            total += importe

    # Art. 11 bis — household employee caring for children under 3 / dependants
    if inp.cotizaciones_empleada_hogar:
        c = d.cuidado
        usa_limite_familiar = True
        if not (inp.num_descendientes_menores_3 or inp.grado_discapacidad >= 33):
            fuera(
                "cuidado de menores o dependientes",
                "requiere un hijo menor de tres años o convivir con un mayor dependiente o una persona con discapacidad "
                "(esto último no consta en el perfil).",
            )
        elif excede_familiar:
            fuera("cuidado de menores o dependientes", motivo_familiar)
        else:
            porcentaje, maximo = (
                (c.porcentaje_familia_numerosa, c.maximo_familia_numerosa)
                if inp.familia_numerosa
                else (c.porcentaje, c.maximo)
            )
            importe = min(inp.cotizaciones_empleada_hogar * porcentaje, maximo)
            t.linea("Deducción autonómica por cuidado de menores o dependientes", -importe, c.fuente)
            t.aviso(
                "La deducción por cuidado solo cuenta las cotizaciones de los meses en que se cumple el requisito, exige "
                "estar de alta como empleador con al menos 40 horas mensuales contratadas y, si es por un hijo menor de "
                "tres años, que ambos progenitores trabajen; el cálculo lo da por cumplido."
            )
            total += importe
    if usa_limite_familiar and inp.miembros_unidad_familiar is None:
        t.aviso(
            f"El límite de renta familiar se ha calculado con {miembros} miembros, deducidos de tus datos; indica el "
            "número de miembros de la unidad familiar y sus bases imponibles si no es correcto."
        )

    # Art. 9 — donations to Madrid foundations and sports clubs
    if inp.donativos_fundaciones_madrid:
        dn = d.donativos
        base = min(inp.donativos_fundaciones_madrid, dn.limite_base_liquidable * base_liquidable)
        importe = base * dn.porcentaje
        t.linea("Deducción autonómica por donativos a fundaciones y clubes deportivos", -importe, dn.fuente)
        t.aviso(
            "La deducción madrileña por donativos solo vale para fundaciones inscritas en el Registro de Fundaciones "
            "de la Comunidad de Madrid y clubes deportivos elementales o básicos."
        )
        total += importe

    # Art. 14 — two or more descendants and low income: 10 % of what remains
    f = d.familias_ingresos_reducidos
    if inp.num_descendientes >= f.minimo_descendientes and base_familia <= f.limite_suma_bases:
        resto = max(ZERO, cuota_autonomica - total - otras_autonomicas - estatales_sobre_autonomica)
        importe = resto * f.porcentaje
        if importe:
            t.linea(
                "Deducción autonómica para familias con dos o más descendientes e ingresos reducidos",
                -importe,
                f.fuente,
            )
            t.aviso(
                "La deducción para familias con ingresos reducidos suma las bases de ambos progenitores y de los "
                "propios descendientes: indícalas en la base del resto de la unidad familiar."
            )
            total += importe
    return total


def _alquiler_vivienda_reducido(inp: IRPFInput, p: IRPFParams, t: _Trace) -> Decimal:
    """Net income from letting a dwelling after the art. 23.2 reduction."""
    neto = inp.rendimiento_alquiler_vivienda_sin_reducir
    if not neto:
        return ZERO
    t.linea("Rendimiento neto del alquiler de vivienda, antes de la reducción (dato aportado)", neto)
    if neto < 0:
        t.aviso("El rendimiento del alquiler es negativo: la reducción del art. 23.2 LIRPF solo se aplica al positivo.")
        return neto
    ra = p.reduccion_arrendamiento
    if ra is None or inp.arrendamiento_fecha_contrato is None:
        t.aviso(
            "Falta la fecha del contrato de alquiler: sin ella no se sabe qué redacción del art. 23.2 LIRPF se aplica y "
            "NO se ha aplicado ninguna reducción, de modo que el resultado puede ser peor que el real."
        )
        return neto
    nuevo = ra.regimen_2026
    vigencia = estado_de(nuevo.fuente.condicionada_a) if nuevo is not None else None
    if nuevo is not None and inp.arrendamiento_fecha_contrato > nuevo.contratos_posteriores_a and vigencia is not None:
        if vigencia.estado == "derogada":
            # The wording the decree-law gave to art. 23.2 is gone: the previous one applies.
            ra = ra.model_copy(update={"regimen_2026": None})
            t.aviso(
                "La redacción del art. 23.2 LIRPF que dio el Real Decreto-ley 29/2026 no se aplica porque ha sido "
                f"derogado ({vigencia.detalle or 'según el BOE'}); se usa la redacción anterior."
            )
        elif vigencia.estado == "pendiente":
            t.aviso(
                "La reducción aplicada es la del Real Decreto-ley 29/2026, pendiente de convalidación por el Congreso: "
                "si se deroga, se aplicará la redacción anterior del art. 23.2 LIRPF."
            )
    r = reduccion_arrendamiento(
        DatosArrendamiento(
            fecha_contrato=inp.arrendamiento_fecha_contrato,
            gran_tenedor=inp.arrendamiento_gran_tenedor,
            primer_alquiler=inp.arrendamiento_primer_alquiler,
            variacion_renta=inp.arrendamiento_variacion_renta,
            mismo_inquilino=inp.arrendamiento_mismo_inquilino,
            renta_bajo_indice=inp.arrendamiento_renta_bajo_indice,
            zona_tensionada=inp.arrendamiento_zona_tensionada,
            inquilino_joven=inp.arrendamiento_inquilino_joven,
            alquiler_social=inp.arrendamiento_alquiler_social,
            vivienda_rehabilitada=inp.arrendamiento_vivienda_rehabilitada,
            prorroga_tacita=inp.arrendamiento_prorroga_tacita,
        ),
        ra,
    )
    for aviso in r.avisos:
        t.aviso(aviso)
    reduccion = _round(neto * r.porcentaje)
    if reduccion:
        t.linea(
            f"Reducción por arrendamiento de vivienda ({r.porcentaje * 100:.0f} %: {r.motivo})",
            -reduccion,
            r.referencia,
        )
    else:
        t.aviso(f"No se aplica reducción por arrendamiento de vivienda: {r.motivo} ({r.regimen}).")
    t.aviso(
        f"Reducción por arrendamiento calculada como {r.regimen}. Solo vale para vivienda (no locales ni alquiler "
        "turístico), para el rendimiento declarado en la autoliquidación y con un único contrato durante todo el año; "
        "los gastos deducibles y la amortización los aportas tú ya restados."
    )
    return neto - reduccion


def calcular_irpf(inp: IRPFInput, params: IRPFParams | None = None) -> IRPFResult:
    p = params or load_params(inp.ejercicio)
    t = _Trace()
    if p.nota_ejercicio:
        t.aviso(p.nota_ejercicio)

    # ---- 1. Work income (arts. 19 and 20) --------------------------------
    bruto = inp.rendimiento_trabajo_bruto
    neto_trabajo = ZERO
    neto_trabajo_sin_reduccion = ZERO
    alquiler_vivienda = _alquiler_vivienda_reducido(inp, p, t)
    otras_rentas = sum(
        (
            max(ZERO, value)
            for value in (
                inp.rendimiento_actividad_neto,
                inp.rendimiento_alquiler_neto,
                alquiler_vivienda,
                inp.otras_rentas_base_general,
                inp.rendimiento_capital_mobiliario,
                inp.ganancias_patrimoniales,
            )
        ),
        ZERO,
    )
    if bruto:
        t.linea("Rendimiento íntegro del trabajo", bruto)
        gastos = inp.cotizaciones_seguridad_social + inp.otros_gastos_deducibles_trabajo
        if gastos:
            t.linea("Cotizaciones a la Seguridad Social y otros gastos deducibles", -gastos, "art. 19.2 a) a e) LIRPF")
        neto_previo = bruto - gastos
        otros = max(ZERO, min(p.trabajo.otros_gastos, neto_previo))
        t.linea("Otros gastos deducibles", -otros, p.trabajo.fuente)
        reduccion = max(ZERO, _reduccion_trabajo(neto_previo, otras_rentas, p))
        neto_trabajo_sin_reduccion = max(ZERO, neto_previo - otros)
        reduccion = min(reduccion, neto_trabajo_sin_reduccion)
        if reduccion:
            t.linea("Reducción por obtención de rendimientos del trabajo", -reduccion, p.trabajo.fuente)
        neto_trabajo = neto_trabajo_sin_reduccion - reduccion
        t.linea("Rendimiento neto reducido del trabajo", neto_trabajo)

    # ---- 2. Taxable bases -------------------------------------------------
    general = (
        neto_trabajo
        + inp.rendimiento_actividad_neto
        + inp.rendimiento_alquiler_neto
        + alquiler_vivienda
        + inp.otras_rentas_base_general
    )
    if inp.rendimiento_actividad_neto:
        t.linea("Rendimiento neto de actividades económicas (dato aportado)", inp.rendimiento_actividad_neto)
        t.aviso(
            "El rendimiento de la actividad económica se toma como cifra neta que tú aportas: no se calculan gastos "
            "deducibles, amortizaciones ni reducciones propias de la actividad."
        )
    if inp.rendimiento_alquiler_neto:
        t.linea("Rendimiento neto reducido del alquiler (dato aportado)", inp.rendimiento_alquiler_neto)
        t.aviso(
            "El rendimiento del alquiler se toma como cifra neta y ya reducida. Para que la calculadora aplique la "
            "reducción por arrendamiento de vivienda (art. 23.2 LIRPF), aporta el rendimiento antes de la reducción "
            "y la fecha del contrato."
        )
    if inp.otras_rentas_base_general:
        t.linea("Otras rentas de la base general (dato aportado)", inp.otras_rentas_base_general)
    if general < 0:
        t.aviso("La base imponible general sale negativa; se toma como cero y no se calcula su compensación futura.")
        general = ZERO
    ca = p.compensacion_ahorro
    pendiente_rendimientos = pendiente_perdidas = ZERO
    if ca is None:
        ahorro = max(ZERO, inp.rendimiento_capital_mobiliario + inp.ganancias_patrimoniales)
        t.aviso("No se ha calculado la compensación de saldos negativos del ahorro (art. 49 LIRPF).")
    else:
        comp = compensar_ahorro(
            rendimientos=inp.rendimiento_capital_mobiliario,
            ganancias=inp.ganancias_patrimoniales,
            rendimientos_negativos_anteriores=inp.rendimientos_negativos_pendientes,
            perdidas_anteriores=inp.perdidas_patrimoniales_pendientes,
            limite_cruzado=ca.limite_cruzado,
        )
        ahorro = comp.base_ahorro
        pendiente_rendimientos = comp.rendimientos_negativos_pendientes
        pendiente_perdidas = comp.perdidas_pendientes
        if comp.compensado_del_ejercicio:
            t.linea(
                f"Compensación entre rendimientos y ganancias del ahorro (límite del {ca.limite_cruzado * 100:.0f} %)",
                -comp.compensado_del_ejercicio,
                ca.fuente,
            )
        if comp.compensado_de_anteriores:
            t.linea(
                "Compensación de saldos negativos de ejercicios anteriores", -comp.compensado_de_anteriores, ca.fuente
            )
        if pendiente_rendimientos or pendiente_perdidas:
            t.aviso(
                "Quedan saldos negativos del ahorro sin compensar: "
                f"{pendiente_rendimientos:.2f} € de rendimientos y {pendiente_perdidas:.2f} € de pérdidas patrimoniales. "
                "Podrás compensarlos en los cuatro ejercicios siguientes al que se generaron (art. 49 LIRPF); la "
                "calculadora no sabe de qué año es cada uno, así que comprueba que no hayan caducado."
            )
        if inp.rendimientos_negativos_pendientes or inp.perdidas_patrimoniales_pendientes:
            t.aviso(
                "Los saldos negativos de ejercicios anteriores se toman tal como los indicas y se aplican después de "
                "los del propio ejercicio, dentro del mismo límite conjunto; solo valen los de los cuatro años anteriores."
            )
    t.linea("Base imponible general", general)
    t.linea("Base imponible del ahorro", ahorro)
    base_imponible_general, base_imponible_ahorro = general, ahorro

    # ---- 3. Reductions (arts. 84 and 52) ---------------------------------
    if inp.tributacion != "individual":
        conjunta = (
            p.tributacion_conjunta.reduccion_biparental
            if inp.tributacion == "conjunta_biparental"
            else p.tributacion_conjunta.reduccion_monoparental
        )
        a_general = min(conjunta, general)
        a_ahorro = min(conjunta - a_general, ahorro)
        general -= a_general
        ahorro -= a_ahorro
        t.linea("Reducción por tributación conjunta", -(a_general + a_ahorro), p.tributacion_conjunta.fuente)
        t.aviso(
            "En tributación conjunta deben incluirse las rentas de todos los miembros de la unidad familiar; "
            "asegúrate de que las cifras aportadas las suman."
        )
        if inp.tributacion == "conjunta_monoparental":
            t.aviso(
                "La reducción de la declaración conjunta monoparental no se aplica cuando convives con el padre o la "
                "madre de alguno de los hijos de la unidad familiar (art. 84.2.4.º LIRPF); no se ha comprobado."
            )
        t.aviso(
            "En tributación conjunta los importes y límites de la individual se aplican una sola vez a las rentas "
            "sumadas de la unidad familiar, sin multiplicarlos por el número de miembros (art. 84.2 LIRPF): los "
            f"{p.trabajo.otros_gastos:.0f} € de otros gastos del trabajo, la reducción y la deducción por "
            "rendimientos del trabajo y los límites de renta de las deducciones estatales. Los límites de los planes "
            "de pensiones se aplican a cada miembro (art. 84.2.1.º LIRPF)"
            + (
                " y los incrementos del mínimo por edad y el mínimo por discapacidad, a cada cónyuge (art. 84.2.2.º "
                "LIRPF)."
                if inp.tributacion == "conjunta_biparental"
                else "."
            )
        )

    ps = p.prevision_social
    # (own contribution, employer contribution, income of the 30 % cap) of each partícipe: the return's
    # taxpayer, or each member of a joint return whose figures are known (art. 84.2.1.º LIRPF).
    miembros = inp.prevision_social_por_miembro if inp.tributacion != "individual" else None
    participes = (
        [
            (
                m.aportacion_plan_pensiones,
                m.contribucion_empresa_plan_pensiones,
                _rendimientos_para_prevision_social(
                    m.rendimiento_trabajo_bruto,
                    m.cotizaciones_seguridad_social + m.otros_gastos_deducibles_trabajo,
                    m.rendimiento_actividad_neto,
                    p,
                ),
            )
            for m in miembros
        ]
        if miembros
        else [
            (
                inp.aportacion_plan_pensiones,
                inp.contribucion_empresa_plan_pensiones,
                neto_trabajo_sin_reduccion + inp.rendimiento_actividad_neto,
            )
        ]
    )
    aportado = sum((propia + empresa for propia, empresa, _ in participes), ZERO)
    if aportado:
        # Art. 52.1: the lesser of the amount limits and 30 % of the partícipe's net work and activity income.
        limites = [
            (
                min(propia, ps.limite_general) + min(empresa, ps.incremento_contribucion_empresarial),
                ps.porcentaje_rendimientos * max(ZERO, rendimientos),
            )
            for propia, empresa, rendimientos in participes
        ]
        limite_importe = sum((importe for importe, _ in limites), ZERO)
        reducible = sum((min(importe, porcentual) for importe, porcentual in limites), ZERO)
        reduccion_ps = max(ZERO, min(reducible, general))
        general -= reduccion_ps
        t.linea("Reducción por aportaciones a sistemas de previsión social", -reduccion_ps, ps.fuente)
        if miembros:
            t.aviso(
                "En la declaración conjunta, los límites de reducción por aportaciones a sistemas de previsión social "
                "se han aplicado a cada miembro de la unidad familiar (art. 84.2.1.º LIRPF), en el orden indicado: "
                + "; ".join(
                    f"{n}.º, {min(importe, porcentual):.2f} € reducibles de {propia + empresa:.2f} € aportados "
                    f"(límite del {ps.porcentaje_rendimientos * 100:.0f} % de sus rendimientos netos del trabajo y "
                    f"de actividades: {porcentual:.2f} €)"
                    for n, ((propia, empresa, _), (importe, porcentual)) in enumerate(
                        zip(participes, limites, strict=True), start=1
                    )
                    if propia + empresa
                )
                + "."
            )
        elif inp.tributacion != "individual":
            t.aviso(
                "En la declaración conjunta, los límites de los planes de pensiones se aplican a cada miembro de la "
                "unidad familiar (art. 84.2.1.º LIRPF). Sin las cifras de cada uno («prevision_social_por_miembro») "
                "se ha aplicado un único límite a las aportaciones sumadas: la reducción puede ser mayor si aportáis "
                "los dos, o menor si aporta quien no tiene rendimientos del trabajo o de actividades."
            )
        if any(propia > ps.limite_general for propia, _, _ in participes):
            t.aviso(
                (
                    f"Solo se han reducido {ps.limite_general:.0f} € de tu aportación individual. El límite puede ser "
                    "mayor si aportas al mismo plan de empleo al que contribuye tu empresa (tabla del art. 52.1 LIRPF), "
                    "lo que no se calcula aquí."
                )
                if not miembros
                else (
                    f"Solo se han reducido {ps.limite_general:.0f} € de la aportación individual de cada miembro que "
                    "aporta más. El límite puede ser mayor si aporta al mismo plan de empleo al que contribuye su "
                    "empresa (tabla del art. 52.1 LIRPF), lo que no se calcula aquí."
                )
            )
        if reduccion_ps < limite_importe:
            t.aviso(
                "Parte de la aportación a previsión social no se ha podido reducir este año (límite del 30 % de los "
                "rendimientos o base insuficiente); el exceso puede reducirse en los cinco ejercicios siguientes."
            )
        if inp.contribucion_empresa_plan_pensiones:
            t.aviso(
                "La contribución de la empresa al plan de pensiones es retribución en especie: debe estar incluida en "
                "el rendimiento íntegro del trabajo que has indicado."
            )

    t.linea("Base liquidable general", general)
    t.linea("Base liquidable del ahorro", ahorro)

    # ---- 4. Personal and family minimum ----------------------------------
    minimo = _minimo(inp, p.minimos, t)
    minimo_general = min(minimo, general)
    minimo_ahorro = min(minimo - minimo_general, ahorro)

    # ---- 5. Gross tax ------------------------------------------------------
    comunidad = p.comunidad(inp.comunidad_autonoma)
    territorio = normalizar_comunidad(inp.comunidad_autonoma or "")
    if any(nombre in territorio for nombre in ("ceuta", "melilla", "la palma")):
        t.aviso(
            "La deducción por rentas obtenidas en Ceuta o Melilla (art. 68.4 LIRPF; en 2022 a 2026 también para "
            "residentes en la isla de La Palma, disposición adicional 57.ª) NO está incluida en el cálculo: el "
            "resultado real será más favorable."
        )
    if comunidad is None:
        escala_autonomica = p.escala_general_estatal
        minimo_autonomico = minimo
        t.aviso(
            (
                f"La normativa autonómica de «{inp.comunidad_autonoma}» no está cargada"
                if inp.comunidad_autonoma
                else "No se ha indicado comunidad autónoma"
            )
            + ": la parte autonómica se ha aproximado con una escala igual a la estatal. La cuota real será "
            "distinta (cada comunidad aprueba su escala, sus mínimos y sus deducciones)."
        )
    else:
        escala_autonomica = comunidad.escala_general
        # Art. 56.3 LIRPF: a community may set its own minimums for its share.
        minimo_autonomico = _minimo(inp, comunidad.minimos) if comunidad.minimos is not None else minimo
        if comunidad.minimos is not None:
            t.linea(
                f"Mínimo personal y familiar para el gravamen autonómico ({comunidad.nombre})",
                minimo_autonomico,
                comunidad.minimos.fuente,
            )
        t.aviso(
            f"Se aplican la escala y los mínimos de {comunidad.nombre}"
            + (
                ", y las deducciones autonómicas por nacimiento, alquiler, gastos educativos, cuidado de menores o "
                "dependientes, donativos y familias con ingresos reducidos cuando aportas sus datos. El resto de "
                "deducciones autonómicas no se calcula."
                if comunidad.deducciones_madrid is not None
                else ". Sus deducciones autonómicas NO están incluidas en el cálculo."
            )
        )
    if comunidad is not None:
        for nota in comunidad.notas:
            t.aviso(nota)
    minimo_autonomico_general = min(minimo_autonomico, general)
    minimo_autonomico_ahorro = min(minimo_autonomico - minimo_autonomico_general, ahorro)

    estatal = _cuota_con_minimo(p.escala_general_estatal, general, minimo_general) + _cuota_con_minimo(
        p.escala_ahorro_estatal, ahorro, minimo_ahorro
    )
    autonomica = _cuota_con_minimo(escala_autonomica, general, minimo_autonomico_general) + _cuota_con_minimo(
        p.escala_ahorro_autonomica, ahorro, minimo_autonomico_ahorro
    )
    t.linea(
        "Cuota íntegra estatal",
        estatal,
        f"{p.escala_general_estatal.fuente.referencia} y {p.escala_ahorro_estatal.fuente.referencia}",
    )
    t.linea(
        "Cuota íntegra autonómica",
        autonomica,
        f"{escala_autonomica.fuente.referencia} y {p.escala_ahorro_autonomica.fuente.referencia}"
        + ("" if comunidad is not None else " (escala estatal usada como aproximación)"),
    )
    # The scales are cited as text above, so flag an unverified one explicitly.
    for escala in (p.escala_general_estatal, p.escala_ahorro_estatal, escala_autonomica, p.escala_ahorro_autonomica):
        if not escala.fuente.verificado and escala.fuente.referencia not in t.no_verificados:
            t.no_verificados.append(escala.fuente.referencia)
    cuota_integra = estatal + autonomica

    # ---- 6. Deductions from gross tax (arts. 67 and 77) --------------------
    base_liquidable = general + ahorro
    deducciones = ZERO
    if inp.donativos:
        d = p.donativos
        base_donativos = min(inp.donativos, d.limite_base_liquidable * base_liquidable)
        primero = min(base_donativos, d.tramo_primero)
        tipo_resto = d.tipo_resto_recurrente if inp.donativos_recurrentes else d.tipo_resto
        importe = primero * d.tipo_primero + (base_donativos - primero) * tipo_resto
        t.linea("Deducción por donativos", -importe, d.fuente)
        if base_donativos < inp.donativos:
            t.aviso("Los donativos superan el 10 % de la base liquidable; el exceso no da derecho a deducción.")
        deducciones += importe
    if inp.pagos_vivienda_habitual_pre2013:
        v = p.vivienda_habitual_transitoria
        importe = min(inp.pagos_vivienda_habitual_pre2013, v.base_maxima) * v.tipo
        t.linea("Deducción por inversión en vivienda habitual (régimen transitorio)", -importe, v.fuente)
        t.aviso(
            "La deducción por vivienda solo procede si compraste antes de 2013 y ya te la aplicaste entonces; "
            "se ha calculado con el porcentaje estatal más el autonómico general (algunas comunidades fijan otro)."
        )
        deducciones += importe

    # Rent of the main home (art. 68.6): borne by the state tax alone (art. 67.1.a).
    solo_estatal = ZERO
    av = p.alquiler_vivienda_habitual
    vigencia_av = estado_de(av.fuente.condicionada_a) if av is not None else None
    if (
        av is not None
        and inp.alquiler_vivienda_habitual
        and vigencia_av is not None
        and vigencia_av.estado == "derogada"
    ):
        t.aviso(
            "No se aplica la deducción estatal por alquiler (art. 68.6 LIRPF): el Real Decreto-ley que la introdujo "
            f"ha sido derogado ({vigencia_av.detalle or 'según el BOE'})."
        )
    elif av is not None and inp.alquiler_vivienda_habitual:
        base_imponible = base_imponible_general + base_imponible_ahorro
        if base_imponible < av.umbral_superior:
            base_maxima = av.base_maxima
            if base_imponible > av.umbral_inferior:
                base_maxima -= av.pendiente * (base_imponible - av.umbral_inferior)
            solo_estatal = _round(min(inp.alquiler_vivienda_habitual, max(ZERO, base_maxima)) * av.porcentaje)
        if solo_estatal:
            t.linea("Deducción estatal por alquiler de vivienda habitual", -solo_estatal, av.fuente)
            t.aviso(
                "La deducción estatal por alquiler (art. 68.6 LIRPF) la introdujo el Real Decreto-ley 29/2026, en vigor "
                "desde el 8 de octubre de 2026"
                + (
                    " y ya convalidado por el Congreso. "
                    if vigencia_av is not None and vigencia_av.estado == "convalidada"
                    else " y pendiente de convalidación por el Congreso: si se deroga, desaparece. "
                )
                + "Exige que, durante al menos medio año, ni tú ni tu unidad familiar seáis titulares de otra vivienda a "
                "menos de 50 km de la alquilada (no se ha comprobado). Se ha calculado sobre todo el alquiler del año "
                "que has indicado, y se suma a la deducción autonómica por alquiler si la hay: el art. 68.6 no las declara "
                "incompatibles, pero no se ha contrastado con la norma autonómica ni hay criterio administrativo."
            )
        else:
            t.aviso(
                f"No se aplica la deducción estatal por alquiler: tu base imponible ({base_imponible:.2f} €) no es "
                f"inferior a {av.umbral_superior:.2f} € (art. 68.6 LIRPF)."
            )

    # The general deductions are borne half by each tax; the community's own
    # deductions reduce only the regional tax. Neither tax can go below zero.
    parte_estatal, parte_autonomica = repartir_deducciones_estatales(deducciones, p.reparto_deducciones_estatales)
    deducciones_autonomicas = ZERO
    if comunidad is not None and comunidad.deducciones_calculables:
        # Before the hand-coded ones: Madrid's art. 14 is a share of the tax left after every other deduction.
        deducciones_autonomicas = _round(
            autonomicas.aplicar(
                list(comunidad.deducciones_calculables),
                inp,
                autonomicas.Bases(
                    base_imponible_total=base_imponible_general + base_imponible_ahorro,
                    base_imponible_general=base_imponible_general,
                    base_liquidable_total=base_liquidable,
                    base_liquidable_general=general,
                    base_imponible_total_menos_minimos=base_imponible_general + base_imponible_ahorro - minimo,
                    base_liquidable_total_menos_minimos=base_liquidable - minimo,
                    base_imponible_ahorro=base_imponible_ahorro,
                    base_imponible_total_menos_minimo_descendientes=base_imponible_general
                    + base_imponible_ahorro
                    - _minimo_descendientes(inp, p.minimos),
                    resto_unidad_familiar=inp.base_imponible_resto_unidad_familiar,
                    cuota_integra_autonomica=autonomica,
                ),
                conjunta=inp.tributacion != "individual",
                norma=f"{comunidad.fuente_principal.norma.split(',')[0]} ({comunidad.fuente_principal.boe_id})",
                linea=t.linea,
                aviso=t.aviso,
            )
        )
        t.aviso(
            f"De las deducciones de {comunidad.nombre} solo se calculan las que dependen de los datos que has dado; "
            "el resto figuran por título en el catálogo de beneficios."
        )
    elif any(getattr(inp, name) for name in _MADRID_INPUTS):
        t.aviso(
            "Has indicado datos de deducciones autonómicas (alquiler, gastos educativos, nacimiento…), pero la "
            "simulación no tiene deducciones calculables para tu comunidad: no se han aplicado."
        )
    if comunidad is not None and comunidad.deducciones_madrid is not None:
        deducciones_autonomicas += _round(
            _deducciones_madrid(
                inp,
                comunidad.deducciones_madrid,
                t,
                base_imponible=base_imponible_general + base_imponible_ahorro,
                base_liquidable=base_liquidable,
                cuota_autonomica=autonomica,
                estatales_sobre_autonomica=parte_autonomica,
                otras_autonomicas=deducciones_autonomicas,
            )
        )
    liquida_estatal = max(ZERO, estatal - parte_estatal - solo_estatal)
    liquida_autonomica = max(ZERO, autonomica - parte_autonomica - deducciones_autonomicas)
    if autonomica - parte_autonomica - deducciones_autonomicas < 0:
        t.aviso(
            "Las deducciones superan la cuota autonómica: el exceso se pierde (la cuota líquida autonómica no puede "
            "ser negativa, art. 77.2 LIRPF)."
        )
    cuota_liquida = liquida_estatal + liquida_autonomica

    # ---- 7. Work-income deduction (additional provision 61) ---------------
    dt = p.deduccion_rendimientos_trabajo
    if dt is not None and bruto and bruto < dt.umbral_superior and otras_rentas <= dt.max_otras_rentas:
        importe = dt.importe_maximo
        if bruto > dt.umbral_inferior:
            importe -= dt.pendiente * (bruto - dt.umbral_inferior)
        # Capped at the share of gross tax attributable to work income.
        proporcion = (neto_trabajo / base_liquidable) if base_liquidable > 0 else ZERO
        importe = max(ZERO, min(importe, cuota_integra * min(Decimal(1), proporcion), cuota_liquida))
        if importe:
            t.linea("Deducción por obtención de rendimientos del trabajo", -importe, dt.fuente)
            cuota_liquida -= _round(importe)
            t.aviso(
                "La deducción por rendimientos del trabajo exige que procedan de una relación laboral o estatutaria "
                "con prestación efectiva de servicios (no pensiones ni prestaciones)."
            )

    t.linea("Cuota líquida", cuota_liquida)

    # ---- 8. Withholdings and refundable deductions ------------------------
    resultado = cuota_liquida - inp.retenciones
    if inp.retenciones:
        t.linea("Retenciones e ingresos a cuenta", -inp.retenciones)
    if inp.hijos_deduccion_maternidad:
        mat = p.maternidad
        guarderia = min(inp.gastos_guarderia, mat.incremento_guarderia * inp.hijos_deduccion_maternidad)
        importe = mat.importe_por_hijo * inp.hijos_deduccion_maternidad + guarderia
        t.linea("Deducción por maternidad", -importe, mat.fuente)
        t.aviso(
            "La deducción por maternidad se ha calculado para el año completo; se prorratea por los meses en que "
            "se cumplen los requisitos y se reduce en lo que ya hayas cobrado de forma anticipada."
        )
        resultado -= importe

    resultado = _round(resultado)
    t.linea("Resultado de la declaración (positivo: a ingresar; negativo: a devolver)", resultado)

    for nombre in t.no_verificados:
        t.aviso(f"Parámetro no contrastado con la fuente en la última revisión: {nombre}.")

    tipo_medio = _round(cuota_liquida / base_liquidable * 100) if base_liquidable > 0 else ZERO
    fp = p.fuente_principal
    return IRPFResult(
        ejercicio=inp.ejercicio,
        tributacion=inp.tributacion,
        base_imponible_general=_round(base_imponible_general),
        base_imponible_ahorro=_round(base_imponible_ahorro),
        base_liquidable_general=_round(general),
        base_liquidable_ahorro=_round(ahorro),
        rendimientos_negativos_a_compensar=_round(pendiente_rendimientos),
        perdidas_patrimoniales_a_compensar=_round(pendiente_perdidas),
        minimo_personal_familiar=_round(minimo),
        minimo_autonomico=_round(minimo_autonomico),
        comunidad_aplicada=comunidad.nombre if comunidad is not None else None,
        cuota_integra_estatal=estatal,
        cuota_integra_autonomica=autonomica,
        cuota_liquida=_round(cuota_liquida),
        retenciones=_round(inp.retenciones),
        resultado=resultado,
        tipo_medio_efectivo=tipo_medio,
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=f"{fp.norma}, {fp.boe_id}, consolidado a {fp.consolidado_a}"
        + (
            f"; {comunidad.fuente_principal.norma}, {comunidad.fuente_principal.boe_id}, "
            f"consolidado a {comunidad.fuente_principal.consolidado_a}"
            if comunidad is not None
            else ""
        ),
    )
