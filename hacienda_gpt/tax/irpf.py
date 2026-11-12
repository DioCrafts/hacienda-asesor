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

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax import autonomicas
from hacienda_gpt.tax.arrendamiento import DatosArrendamiento, reduccion_arrendamiento
from hacienda_gpt.tax.params import (
    DeduccionesMadrid,
    Escala,
    Fuente,
    IRPFParams,
    Minimos,
    load_params,
    normalizar_comunidad,
)

CENT = Decimal("0.01")
ZERO = Decimal(0)

Tributacion = Literal["individual", "conjunta_biparental", "conjunta_monoparental"]


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


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

    # Reductions and deductions
    aportacion_plan_pensiones: Decimal = Field(default=ZERO, ge=0)
    contribucion_empresa_plan_pensiones: Decimal = Field(default=ZERO, ge=0)
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

    retenciones: Decimal = Field(default=ZERO, ge=0)


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


def _minimo(inp: IRPFInput, m: Minimos, t: _Trace | None = None) -> Decimal:
    """Personal and family minimum under ``m`` (state amounts, or a community's).

    With a trace, each component is recorded as its own line.
    """
    total = m.contribuyente
    if inp.edad is not None and inp.edad > 65:
        total += m.incremento_mayor_65
    if inp.edad is not None and inp.edad > 75:
        total += m.incremento_mayor_75

    descendientes = ZERO
    for index in range(inp.num_descendientes):
        descendientes += m.descendientes[min(index, len(m.descendientes) - 1)]
    descendientes += m.incremento_descendiente_menor_3 * min(inp.num_descendientes_menores_3, inp.num_descendientes)
    if descendientes and inp.descendientes_compartidos:
        descendientes /= 2

    mayores_75 = min(inp.num_ascendientes_mayores_75, inp.num_ascendientes_mayores_65)
    ascendientes = m.ascendiente * inp.num_ascendientes_mayores_65 + m.incremento_ascendiente_mayor_75 * mayores_75

    discapacidad = ZERO
    if inp.grado_discapacidad >= 65:
        discapacidad = m.discapacidad_65 + m.gastos_asistencia
    elif inp.grado_discapacidad >= 33:
        discapacidad = m.discapacidad

    if t is not None:
        t.linea("Mínimo del contribuyente", total, m.fuente)
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

    return total + descendientes + ascendientes + discapacidad


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


def _deducciones_madrid(
    inp: IRPFInput,
    d: DeduccionesMadrid,
    t: _Trace,
    *,
    base_imponible: Decimal,
    base_liquidable: Decimal,
    cuota_autonomica: Decimal,
    estatales_sobre_autonomica: Decimal,
) -> Decimal:
    """Madrid deductions (DLeg. 1/2010). Each one that is claimed but fails a
    requirement is explained in an aviso instead of being silently dropped."""
    conjunta = inp.tributacion != "individual"
    base_familia = base_imponible + inp.base_imponible_resto_unidad_familiar
    miembros = inp.miembros_unidad_familiar
    if miembros is None:
        miembros = 1 + inp.num_descendientes + (1 if inp.tributacion == "conjunta_biparental" else 0)
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
            infantil = min(inp.num_hijos_primer_ciclo_infantil, hijos)
            por_hijo = e.maximo_por_hijo_con_escolaridad if inp.gastos_escolaridad else e.maximo_por_hijo
            tope = e.maximo_por_hijo_primer_ciclo_infantil * infantil + por_hijo * (hijos - infantil)
            bruto = (
                inp.gastos_escolaridad + inp.gastos_ensenanza_idiomas
            ) * e.porcentaje_escolaridad_idiomas + inp.gastos_uniformes_escolares * e.porcentaje_vestuario
            importe = min(bruto, tope) * reparto
            t.linea("Deducción autonómica por gastos educativos", -importe, e.fuente)
            t.aviso(
                "En gastos educativos el máximo se ha aplicado al conjunto de los hijos (la ley lo fija por hijo) y los "
                "gastos deben ir ya minorados en becas; la escolaridad pública o concertada no genera deducción."
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
        resto = max(ZERO, cuota_autonomica - total - estatales_sobre_autonomica)
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
    ahorro = inp.rendimiento_capital_mobiliario + inp.ganancias_patrimoniales
    if ahorro < 0:
        t.aviso(
            "La base del ahorro sale negativa; se toma como cero. La compensación de pérdidas entre rendimientos y "
            "ganancias y con ejercicios futuros (art. 49 LIRPF) no se calcula."
        )
        ahorro = ZERO
    elif inp.rendimiento_capital_mobiliario < 0 or inp.ganancias_patrimoniales < 0:
        t.aviso(
            "Hay un saldo negativo en el ahorro que se ha compensado íntegramente con el positivo; la ley limita esa "
            "compensación entre rendimientos y ganancias al 25 % (art. 49 LIRPF), así que el resultado puede diferir."
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

    ps = p.prevision_social
    aportado = inp.aportacion_plan_pensiones + inp.contribucion_empresa_plan_pensiones
    if aportado:
        limite_importe = min(inp.aportacion_plan_pensiones, ps.limite_general) + min(
            inp.contribucion_empresa_plan_pensiones, ps.incremento_contribucion_empresarial
        )
        limite_porcentual = ps.porcentaje_rendimientos * max(
            ZERO, neto_trabajo_sin_reduccion + inp.rendimiento_actividad_neto
        )
        reduccion_ps = max(ZERO, min(limite_importe, limite_porcentual, general))
        general -= reduccion_ps
        t.linea("Reducción por aportaciones a sistemas de previsión social", -reduccion_ps, ps.fuente)
        if inp.aportacion_plan_pensiones > ps.limite_general:
            t.aviso(
                f"Solo se han reducido {ps.limite_general:.0f} € de tu aportación individual. El límite puede ser "
                "mayor si aportas al mismo plan de empleo al que contribuye tu empresa (tabla del art. 52.1 LIRPF), "
                "lo que no se calcula aquí."
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
    if av is not None and inp.alquiler_vivienda_habitual:
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
                "desde el 8 de octubre de 2026 y pendiente de convalidación por el Congreso: si se deroga, desaparece. "
                "Exige que, durante al menos medio año, ni tú ni tu unidad familiar seáis titulares de otra vivienda a "
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
    parte_autonomica = _round(deducciones * p.reparto_deducciones_estatales.parte_autonomica)
    parte_estatal = _round(deducciones) - parte_autonomica
    deducciones_autonomicas = ZERO
    if comunidad is not None and comunidad.deducciones_madrid is not None:
        deducciones_autonomicas = _round(
            _deducciones_madrid(
                inp,
                comunidad.deducciones_madrid,
                t,
                base_imponible=base_imponible_general + base_imponible_ahorro,
                base_liquidable=base_liquidable,
                cuota_autonomica=autonomica,
                estatales_sobre_autonomica=parte_autonomica,
            )
        )
    elif comunidad is not None and comunidad.deducciones_calculables:
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
                    resto_unidad_familiar=inp.base_imponible_resto_unidad_familiar,
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
