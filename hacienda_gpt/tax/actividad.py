"""Net income of a self-employed activity in estimación directa simplificada.

Income minus the expenses the taxpayer reports minus the flat allowance for
"provisiones deducibles y gastos de difícil justificación" (art. 30 RIRPF),
then the reductions of art. 32.2 LIRPF. The figure it returns in
``rendimiento_actividad_neto`` is what ``IRPFInput.rendimiento_actividad_neto``
expects.

Limits, all repeated as ``avisos`` on the result: one taxpayer, the whole of
their activities given as one set of totals, individual filing; expenses are
taken as given (the calculator does not judge whether they are deductible);
estimación objetiva and estimación directa normal are not computed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.autonomos_params import AutonomosParams, load_autonomos_params
from hacienda_gpt.tax.irpf import Linea
from hacienda_gpt.tax.params import Fuente

CENT = Decimal("0.01")
ZERO = Decimal(0)


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class Traza:
    """Lines and warnings of one computation (shared by the three self-employed calculators)."""

    def __init__(self) -> None:
        self.lineas: list[Linea] = []
        self.avisos: list[str] = []
        self.no_verificados: list[str] = []

    def linea(self, concepto: str, importe: Decimal, fuente: Fuente | str | None = None) -> None:
        referencia = fuente.referencia if isinstance(fuente, Fuente) else fuente
        self.lineas.append(Linea(concepto=concepto, importe=_round(importe), referencia=referencia))
        if isinstance(fuente, Fuente):
            self.usa(fuente)

    def usa(self, fuente: Fuente) -> None:
        """Record that a figure from this source went into the result."""
        if not fuente.verificado and fuente.referencia not in self.no_verificados:
            self.no_verificados.append(fuente.referencia)

    def aviso(self, text: str) -> None:
        if text not in self.avisos:
            self.avisos.append(text)

    def cerrar(self) -> None:
        for nombre in self.no_verificados:
            self.aviso(f"Parámetro no contrastado con la fuente en la última revisión: {nombre}.")


class ActividadInput(BaseModel):
    """One year of activity. Amounts in euros, without VAT; absent means zero."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    ingresos_integros: Decimal = Field(default=ZERO, ge=0)

    # Deductible expenses, totals by category, as the taxpayer reports them.
    seguridad_social_autonomo: Decimal = Field(default=ZERO, ge=0)  # RETA contributions
    suministros: Decimal = Field(default=ZERO, ge=0)
    alquileres: Decimal = Field(default=ZERO, ge=0)
    servicios_profesionales: Decimal = Field(default=ZERO, ge=0)
    otros_gastos: Decimal = Field(default=ZERO, ge=0)
    amortizaciones: Decimal = Field(default=ZERO, ge=0)

    # Turnover of the previous year, only to warn when the simplified method is not available.
    cifra_negocios_ano_anterior: Decimal | None = Field(default=None, ge=0)

    # Art. 32.2.1.º LIRPF. The taxpayer states that ALL the requirements of
    # art. 32.2.2.º hold and opts for it; it replaces the flat allowance.
    cumple_requisitos_reduccion_dependiente: bool = False
    # Non-exempt income other than from economic activities (work, capital, gains…).
    # None = unknown: the reductions that depend on it are not applied.
    otras_rentas_no_exentas: Decimal | None = Field(default=None, ge=0)
    discapacidad: bool = False
    # Needs help from third parties, reduced mobility, or a degree of 65 % or more.
    discapacidad_ampliada: bool = False
    # Art. 20 LIRPF reduction the taxpayer applies to work income (joint cap of art. 32.2.3.º).
    reduccion_rendimientos_trabajo: Decimal = Field(default=ZERO, ge=0)


class ResultadoActividad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    ingresos_integros: Decimal
    gastos_deducibles: Decimal
    # Income minus reported expenses, before the flat allowance.
    rendimiento_neto_previo: Decimal
    gastos_dificil_justificacion: Decimal
    rendimiento_neto: Decimal
    reduccion_art_32_2: Decimal
    # Which reduction of art. 32.2 was applied, if any.
    reduccion_aplicada: str | None = None
    rendimiento_neto_reducido: Decimal
    # The value for IRPFInput.rendimiento_actividad_neto (= rendimiento_neto_reducido).
    rendimiento_actividad_neto: Decimal
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def gastos_dificil_justificacion(rendimiento_previo: Decimal, p: AutonomosParams) -> Decimal:
    """Flat allowance of art. 30.2.ª RIRPF: a percentage of the positive net income, capped per year."""
    g = p.gastos_dificil_justificacion
    if rendimiento_previo <= 0:
        return ZERO
    return min(_round(rendimiento_previo * g.porcentaje), g.limite_anual)


def tipo_retencion_profesional(p: AutonomosParams, inicio_actividad: bool = False) -> Decimal:
    """Withholding rate a professional puts on invoices to businesses (art. 95.1 RIRPF)."""
    r = p.retencion_profesionales
    return r.inicio_actividad if inicio_actividad else r.general


def _reduccion_dependiente(inp: ActividadInput, neto: Decimal, p: AutonomosParams, t: Traza) -> Decimal:
    """Art. 32.2.1.º: fixed amount + income-related amount + disability amount."""
    r = p.reduccion_dependientes
    t.linea("Reducción general por autónomo dependiente o de cliente único", -r.importe_general, r.fuente)
    total = r.importe_general
    if inp.otras_rentas_no_exentas is None:
        t.aviso(
            "No se ha indicado si hay otras rentas distintas de la actividad: no se aplica la reducción adicional "
            "del art. 32.2.1.º a) LIRPF, que exige que no superen "
            f"{r.max_otras_rentas} €."
        )
    elif neto < r.umbral_superior and inp.otras_rentas_no_exentas <= r.max_otras_rentas:
        adicional = r.importe_adicional_maximo
        if neto > r.umbral_inferior:
            adicional -= r.pendiente * (neto - r.umbral_inferior)
        adicional = max(ZERO, adicional)
        t.linea("Reducción adicional por rendimientos netos bajos", -adicional, "art. 32.2.1.º a) LIRPF")
        total += adicional
    if inp.discapacidad or inp.discapacidad_ampliada:
        importe = r.discapacidad_ampliada if inp.discapacidad_ampliada else r.discapacidad
        t.linea("Reducción adicional por discapacidad", -importe, "art. 32.2.1.º b) LIRPF")
        total += importe
    t.aviso(
        "La reducción del art. 32.2.1.º LIRPF se aplica porque has declarado cumplir todos los requisitos del "
        "art. 32.2.2.º (cliente único no vinculado o TRADE, sin rendimientos del trabajo salvo prestaciones de hasta "
        f"{r.max_prestaciones_trabajo} €, al menos el {r.min_ingresos_con_retencion * 100:.0f} % de los ingresos con "
        "retención, sin entidades en atribución de rentas y obligaciones formales del art. 68 RIRPF). El cálculo solo "
        "comprueba el límite de gastos. Es incompatible con los gastos de difícil justificación, que no se han restado."
    )
    return total


def _reduccion_rentas_bajas(inp: ActividadInput, neto: Decimal, p: AutonomosParams, t: Traza) -> Decimal:
    """Art. 32.2.3.º: taxpayers whose non-exempt income is under the limit."""
    r = p.reduccion_rentas_bajas
    if inp.otras_rentas_no_exentas is None:
        if neto < r.limite_rentas:
            t.aviso(
                f"Con rentas no exentas inferiores a {r.limite_rentas} € (incluida la actividad) puede corresponder la "
                "reducción del art. 32.2.3.º LIRPF; no se ha calculado porque no se han indicado las demás rentas "
                "(otras_rentas_no_exentas)."
            )
        return ZERO
    rentas = neto + inp.otras_rentas_no_exentas
    if rentas >= r.limite_rentas:
        return ZERO
    reduccion = r.importe_maximo
    if rentas > r.umbral_inferior:
        reduccion -= r.pendiente * (rentas - r.umbral_inferior)
    tope = max(ZERO, r.limite_conjunto_reduccion_trabajo - inp.reduccion_rendimientos_trabajo)
    reduccion = max(ZERO, min(reduccion, tope))
    t.linea(f"Reducción por rentas no exentas inferiores a {r.limite_rentas} €", -reduccion, r.fuente)
    t.aviso(
        "Reducción del art. 32.2.3.º LIRPF calculada sumando el rendimiento neto de la actividad y las otras rentas "
        "que has indicado; la ley no detalla cómo se computan esas rentas."
    )
    if inp.otras_rentas_no_exentas > 0 and not inp.reduccion_rendimientos_trabajo:
        t.aviso(
            "Si entre las otras rentas hay rendimientos del trabajo, indica la reducción del art. 20 LIRPF que se les "
            f"aplica: junto con la del art. 32.2.3.º no puede pasar de {r.limite_conjunto_reduccion_trabajo} €."
        )
    return reduccion


def calcular_rendimiento_actividad(inp: ActividadInput, params: AutonomosParams | None = None) -> ResultadoActividad:
    """Net income of the activity in estimación directa simplificada, with its trace."""
    p = params or load_autonomos_params(inp.ejercicio)
    t = Traza()
    eds = p.estimacion_directa_simplificada

    t.linea("Ingresos íntegros de la actividad", inp.ingresos_integros, "art. 28 LIRPF")
    categorias = [
        ("Seguridad Social del autónomo (RETA)", inp.seguridad_social_autonomo),
        ("Suministros", inp.suministros),
        ("Alquileres", inp.alquileres),
        ("Servicios de profesionales independientes", inp.servicios_profesionales),
        ("Otros gastos deducibles", inp.otros_gastos),
        ("Amortizaciones", inp.amortizaciones),
    ]
    gastos = ZERO
    for concepto, importe in categorias:
        if importe:
            t.linea(f"Gasto: {concepto}", -importe, "arts. 28 y 30 LIRPF")
            gastos += importe
    previo = inp.ingresos_integros - gastos
    t.linea("Diferencia entre ingresos y gastos", previo, "art. 30 RIRPF")

    # Art. 32.2.1.º replaces the flat allowance (art. 30.2.ª RIRPF, art. 32.2.2.º a) LIRPF),
    # so whether it applies has to be settled before the net income.
    dep = p.reduccion_dependientes
    dependiente = inp.cumple_requisitos_reduccion_dependiente and previo > 0
    if dependiente and gastos > inp.ingresos_integros * dep.max_gastos_sobre_ingresos:
        dependiente = False
        t.aviso(
            f"No se aplica la reducción del art. 32.2.1.º LIRPF: los gastos deducibles ({_round(gastos)} €) superan "
            f"el {dep.max_gastos_sobre_ingresos * 100:.0f} % de los ingresos íntegros (requisito del art. 32.2.2.º c)."
        )

    g = p.gastos_dificil_justificacion
    dificil = ZERO if dependiente else gastos_dificil_justificacion(previo, p)
    if dificil:
        t.linea(
            f"Provisiones deducibles y gastos de difícil justificación ({g.porcentaje * 100:.0f} %, "
            f"máximo {g.limite_anual} €)",
            -dificil,
            g.fuente,
        )
    neto = previo - dificil
    t.linea("Rendimiento neto", neto, "art. 30 RIRPF")

    reduccion = ZERO
    reduccion_aplicada: str | None = None
    if dependiente:
        reduccion = _reduccion_dependiente(inp, neto, p, t)
        reduccion_aplicada = "art. 32.2.1.º LIRPF"
    elif neto > 0:
        reduccion = _reduccion_rentas_bajas(inp, neto, p, t)
        if reduccion:
            reduccion_aplicada = "art. 32.2.3.º LIRPF"
    # Art. 32.2.4.º: the reductions cannot turn the net income negative.
    reduccion = _round(min(reduccion, max(ZERO, neto)))
    reducido = _round(neto) - reduccion
    if reduccion:
        t.linea(
            "Total reducciones del art. 32.2 LIRPF (el saldo no puede ser negativo)", -reduccion, "art. 32.2.4.º LIRPF"
        )
    t.linea("Rendimiento neto reducido de la actividad", reducido, "art. 32 LIRPF")

    t.aviso(
        "Los gastos se han restado tal como los has indicado: el cálculo no valora si son deducibles (afectación a la "
        "actividad, justificación con factura, límites de suministros de la vivienda, seguros de salud o manutención "
        "del art. 30.2.5.ª LIRPF, tablas de amortización simplificada)."
    )
    t.aviso(
        "Cálculo para estimación directa simplificada, con todas las actividades del contribuyente sumadas y en "
        "tributación individual. No se calculan la estimación objetiva (módulos) ni la estimación directa normal."
    )
    if inp.cifra_negocios_ano_anterior is None:
        t.aviso(
            f"La modalidad simplificada exige que la cifra de negocios del año anterior no supere "
            f"{eds.limite_cifra_negocios} € y no haber renunciado a ella; no se ha comprobado."
        )
    elif inp.cifra_negocios_ano_anterior > eds.limite_cifra_negocios:
        t.usa(eds.fuente)
        t.aviso(
            f"La cifra de negocios del año anterior supera {eds.limite_cifra_negocios} €: no corresponde la modalidad "
            f"simplificada sino la normal ({eds.fuente.referencia}), en la que no existe el porcentaje de gastos de "
            "difícil justificación. Este resultado no es válido para ese caso."
        )
    for aviso in g.avisos:
        t.aviso(aviso)
    t.aviso(
        "No se aplican la reducción del 30 % por rendimientos irregulares (art. 32.1 LIRPF) ni la del 20 % por inicio "
        "de actividad (art. 32.3 LIRPF); si te corresponden, réstalas del rendimiento antes de llevarlo al IRPF."
    )
    t.cerrar()

    return ResultadoActividad(
        ejercicio=inp.ejercicio,
        ingresos_integros=_round(inp.ingresos_integros),
        gastos_deducibles=_round(gastos),
        rendimiento_neto_previo=_round(previo),
        gastos_dificil_justificacion=dificil,
        rendimiento_neto=_round(neto),
        reduccion_art_32_2=reduccion,
        reduccion_aplicada=reduccion_aplicada,
        rendimiento_neto_reducido=reducido,
        rendimiento_actividad_neto=reducido,
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto("BOE-A-2006-20764", "BOE-A-2007-6820"),
    )
