"""Net income of a self-employed activity in estimación directa simplificada.

Income minus the expenses the taxpayer reports minus the flat allowance for
"provisiones deducibles y gastos de difícil justificación" (art. 30 RIRPF),
then the reductions of art. 32 LIRPF in the order the Act gives them: 32.1
(irregular income), 32.2 (dependent self-employed or low income) and 32.3
(new activity). The figure it returns in ``rendimiento_actividad_neto`` is
what ``IRPFInput.rendimiento_actividad_neto`` expects.

Limits, all repeated as ``avisos`` on the result: one taxpayer, the whole of
their activities given as one set of totals, individual filing; expenses are
taken as given (the calculator does not judge whether they are deductible);
estimación objetiva and estimación directa normal are not computed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


CeutaMelilla = Literal["ceuta", "melilla"]
SupuestoRetencion = Literal[
    "recaudador_municipal", "mediador_seguros", "delegado_loterias", "actividad_ingresos_reducidos"
]


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

    # Art. 32.1: the part of the net income generated over more than two years, or of the kinds
    # art. 25 RIRPF lists as notoriously irregular, imputed to this single year. The taxpayer
    # states it qualifies; the calculator only applies the percentage and its cap.
    rendimientos_irregulares: Decimal = Field(default=ZERO, ge=0)
    # Art. 32.3: the taxpayer states that the activity is new in the sense of that paragraph, that
    # this is the first year with a positive net income or the following one, and that no more
    # than half of the income comes from a former employer.
    reduccion_inicio_actividad: bool = False
    # The activity is carried on in Ceuta or Melilla and its income is entitled to the deduction
    # of art. 68.4 LIRPF (which is a deduction of the IRPF itself: not computed here).
    ceuta_melilla: CeutaMelilla | None = None

    @model_validator(mode="after")
    def _coherencia(self) -> ActividadInput:
        if self.rendimientos_irregulares > self.ingresos_integros:
            raise ValueError(
                "Los rendimientos irregulares (art. 32.1 LIRPF) son una parte del rendimiento de la actividad: no "
                "pueden superar los ingresos íntegros."
            )
        return self


class ResultadoActividad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    ingresos_integros: Decimal
    gastos_deducibles: Decimal
    # Income minus reported expenses, before the flat allowance.
    rendimiento_neto_previo: Decimal
    gastos_dificil_justificacion: Decimal
    rendimiento_neto: Decimal
    # Art. 32.1 (irregular income) and art. 32.3 (new activity); zero when not asked for.
    reduccion_art_32_1: Decimal = ZERO
    reduccion_art_32_3: Decimal = ZERO
    reduccion_art_32_2: Decimal
    # Which reduction of art. 32.2 was applied, if any.
    reduccion_aplicada: str | None = None
    rendimiento_neto_reducido: Decimal
    # The value for IRPFInput.rendimiento_actividad_neto (= rendimiento_neto_reducido).
    rendimiento_actividad_neto: Decimal
    # Whether last year's turnover allows the simplified method ("no supere" the limit of art. 30.1 LIRPF);
    # None when ``cifra_negocios_ano_anterior`` was not given.
    modalidad_simplificada_aplicable: bool | None = None
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def porcentaje_dificil_justificacion(
    p: AutonomosParams, ceuta_melilla: CeutaMelilla | None = None, trimestre: int | None = None
) -> tuple[Decimal, Fuente]:
    """Percentage of the flat allowance and where it comes from.

    The general one, or the year's special one for Ceuta when the parameters
    have it. ``trimestre`` is for the payments on account: the special
    percentage only counts from the quarter the provision allows.
    """
    ceuta = p.gastos_dificil_justificacion_ceuta
    if (
        ceuta_melilla == "ceuta"
        and ceuta is not None
        and (trimestre is None or trimestre >= ceuta.pagos_fraccionados_desde_trimestre)
    ):
        return ceuta.porcentaje, ceuta.fuente
    return p.gastos_dificil_justificacion.porcentaje, p.gastos_dificil_justificacion.fuente


def gastos_dificil_justificacion(
    rendimiento_previo: Decimal, p: AutonomosParams, porcentaje: Decimal | None = None
) -> Decimal:
    """Flat allowance of art. 30.2.ª RIRPF: a percentage of the positive net income, capped per year."""
    g = p.gastos_dificil_justificacion
    if rendimiento_previo <= 0:
        return ZERO
    return min(_round(rendimiento_previo * (g.porcentaje if porcentaje is None else porcentaje)), g.limite_anual)


def avisos_ceuta_melilla(p: AutonomosParams, ceuta_melilla: CeutaMelilla | None, t: Traza) -> None:
    """What the Ceuta/Melilla flag did and did not do, for the three places that use it."""
    ceuta = p.gastos_dificil_justificacion_ceuta
    if ceuta_melilla is None:
        if ceuta is not None:
            t.aviso(
                f"Gastos de difícil justificación al porcentaje general. Si la actividad se desarrolla en Ceuta con "
                f"derecho a la deducción del art. 68.4 LIRPF, indícalo: en {p.ejercicio} su porcentaje es el "
                f"{ceuta.porcentaje * 100:.0f} % ({ceuta.fuente.referencia})."
            )
        return
    ciudad = "Ceuta" if ceuta_melilla == "ceuta" else "Melilla"
    t.aviso(
        f"Has indicado que la actividad se desarrolla en {ciudad} y que sus rendimientos tienen derecho a la deducción "
        "del art. 68.4 LIRPF; el cálculo no comprueba ese derecho y trata toda la actividad como desarrollada allí. "
        "Esa deducción (el 60 % de la parte de la cuota que corresponde a esas rentas) es de la cuota del IRPF: ni este "
        "cálculo ni el simulador de IRPF la aplican, así que el IRPF anual estimado saldrá más alto que el real."
    )
    if ceuta_melilla == "melilla" and ceuta is not None:
        t.aviso(
            f"El porcentaje del {ceuta.porcentaje * 100:.0f} % de gastos de difícil justificación de {p.ejercicio} es "
            f"solo para Ceuta ({ceuta.fuente.referencia}): en Melilla se aplica el general."
        )


def tipo_retencion_profesional(
    p: AutonomosParams,
    inicio_actividad: bool = False,
    *,
    supuesto: SupuestoRetencion | None = None,
    ceuta_melilla: bool = False,
) -> Decimal:
    """Withholding rate a professional puts on invoices to businesses (art. 95.1 RIRPF).

    ``supuesto`` is one of the cases of the fourth paragraph (collectors,
    insurance brokers using external assistants, lottery delegates, and the
    low-income activities of letter d), whose conditions are checked with
    :func:`cumple_supuesto_d_retencion`). ``ceuta_melilla``: the income is
    entitled to the art. 68.4 LIRPF deduction, which cuts the rate.
    """
    r = p.retencion_profesionales
    tipo = r.otros_supuestos if supuesto is not None else r.inicio_actividad if inicio_actividad else r.general
    return tipo * (1 - r.reduccion_ceuta_melilla) if ceuta_melilla else tipo


def cumple_supuesto_d_retencion(
    p: AutonomosParams, integros_actividades_letra_d: Decimal, integros_actividades_y_trabajo: Decimal
) -> bool:
    """Figures of art. 95.1 d) RIRPF, both of the previous year: gross income of the activities
    of that letter below the limit and above the required share of the gross income from
    economic activities plus work. Whether the activity is one of the IAE groups (or an artist's
    service) and the notice to the payer are not checked."""
    r = p.retencion_profesionales
    return (
        integros_actividades_letra_d < r.supuesto_d_limite_ingresos
        and integros_actividades_letra_d > r.supuesto_d_min_proporcion * integros_actividades_y_trabajo
    )


class RetencionProfesionalInput(BaseModel):
    """Who issues the invoice, for the withholding rate of art. 95.1 RIRPF."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    # The taxpayer states that the start-up rate applies (period of the start or the following ones the
    # article allows, no professional activity in the year before the start, payer informed).
    inicio_actividad: bool = False
    # One of the cases of the fourth paragraph of art. 95.1 (letters a to d).
    supuesto: SupuestoRetencion | None = None
    # The income is entitled to the deduction of art. 68.4 LIRPF (Ceuta or Melilla): the rate is cut.
    ceuta_melilla: bool = False
    # Letter d), both of the PREVIOUS year: gross income of the activities of that letter, and gross income
    # from economic activities plus work. Both or neither; with them the figures of the letter are checked.
    integros_actividades_letra_d: Decimal | None = Field(default=None, ge=0)
    integros_actividades_y_trabajo: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coherencia(self) -> RetencionProfesionalInput:
        letra_d, total = self.integros_actividades_letra_d, self.integros_actividades_y_trabajo
        if (letra_d is None) != (total is None):
            raise ValueError(
                "Para comprobar la letra d) del art. 95.1 RIRPF hacen falta las dos cifras del año anterior: los "
                "rendimientos íntegros de esas actividades y los de actividades económicas y del trabajo."
            )
        if letra_d is not None and total is not None and letra_d > total:
            raise ValueError(
                "Los rendimientos íntegros de las actividades de la letra d) forman parte de los de actividades "
                "económicas y del trabajo: no pueden superarlos."
            )
        return self


class ResultadoRetencionProfesional(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    # The rate as a fraction (0.15) and in percentage points (15), so that a rate such as 2.8 % survives a
    # rounding to the cent.
    tipo: Decimal
    tipo_porcentaje: Decimal
    # Whether the figures of letter d) hold; None when they were not given.
    cumple_supuesto_d: bool | None = None
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def calcular_retencion_profesional(
    inp: RetencionProfesionalInput, params: AutonomosParams | None = None
) -> ResultadoRetencionProfesional:
    """Withholding rate a professional puts on an invoice (art. 95.1 RIRPF), with what was not checked."""
    p = params or load_autonomos_params(inp.ejercicio)
    r = p.retencion_profesionales
    t = Traza()
    t.usa(r.fuente)
    cumple: bool | None = None
    if inp.integros_actividades_letra_d is not None and inp.integros_actividades_y_trabajo is not None:
        cumple = cumple_supuesto_d_retencion(p, inp.integros_actividades_letra_d, inp.integros_actividades_y_trabajo)
    cifras_d = (
        f"rendimientos íntegros de esas actividades en el año anterior inferiores a {r.supuesto_d_limite_ingresos} € "
        f"y de más del {r.supuesto_d_min_proporcion * 100:.0f} % de la suma de los de actividades económicas y del "
        "trabajo"
    )
    supuesto = inp.supuesto
    if supuesto == "actividad_ingresos_reducidos":
        if cumple is False:
            supuesto = None
            t.aviso(
                f"No se cumplen las cifras de la letra d) del art. 95.1 RIRPF ({cifras_d}): no se aplica su tipo, "
                "sino el que corresponde sin ese supuesto."
            )
        else:
            if cumple is None:
                t.aviso(
                    f"Tipo de la letra d) del art. 95.1 RIRPF aplicado sin comprobar sus cifras ({cifras_d}): indica "
                    "esos importes del año anterior para comprobarlas."
                )
            t.aviso(
                "La letra d) del art. 95.1 RIRPF solo alcanza a las actividades de los grupos y agrupaciones del IAE "
                "que enumera y a ciertos servicios de artistas y técnicos de las artes escénicas, audiovisuales y "
                "musicales, y exige comunicarlo por escrito al pagador; no se comprueba."
            )
    elif supuesto is not None:
        t.aviso(
            "Tipo del cuarto párrafo del art. 95.1 RIRPF aplicado porque has indicado que el perceptor es "
            f"«{supuesto.replace('_', ' ')}»; no se comprueba."
        )
    if inp.inicio_actividad and supuesto is None:
        t.aviso(
            "Tipo de inicio de actividad aplicado porque lo has indicado: el art. 95.1 RIRPF lo limita al período de "
            "inicio y a los siguientes que señala, exige no haber ejercido actividad profesional alguna en el año "
            "anterior al inicio y comunicarlo por escrito al pagador. No se comprueba."
        )
    if inp.ceuta_melilla:
        t.aviso(
            f"Tipo reducido en un {r.reduccion_ceuta_melilla * 100:.0f} % porque has indicado que los rendimientos "
            "tienen derecho a la deducción del art. 68.4 LIRPF (Ceuta o Melilla); no se comprueba."
        )
    for aviso in r.avisos:
        t.aviso(aviso)
    tipo = tipo_retencion_profesional(p, inp.inicio_actividad, supuesto=supuesto, ceuta_melilla=inp.ceuta_melilla)
    t.cerrar()
    return ResultadoRetencionProfesional(
        ejercicio=inp.ejercicio,
        tipo=tipo,
        tipo_porcentaje=tipo * 100,
        cumple_supuesto_d=cumple,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto("BOE-A-2007-6820", "BOE-A-2006-20764"),
    )


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
    porcentaje, fuente_porcentaje = porcentaje_dificil_justificacion(p, inp.ceuta_melilla)
    dificil = ZERO if dependiente else gastos_dificil_justificacion(previo, p, porcentaje)
    if dificil:
        t.linea(
            f"Provisiones deducibles y gastos de difícil justificación ({porcentaje * 100:.0f} %, "
            f"máximo {g.limite_anual} €)",
            -dificil,
            g.fuente,
        )
        t.usa(fuente_porcentaje)
    neto = previo - dificil
    t.linea("Rendimiento neto", neto, "art. 30 RIRPF")

    # Art. 32.1: 30 % of the irregular part of the net income, on a capped base.
    irr = p.reduccion_irregulares
    reduccion_1 = ZERO
    if inp.rendimientos_irregulares:
        base_1 = min(inp.rendimientos_irregulares, irr.limite_base)
        reduccion_1 = _round(base_1 * irr.porcentaje)
        t.linea(
            f"Reducción del {irr.porcentaje * 100:.0f} % por rendimientos irregulares (sobre {_round(base_1)} €)",
            -reduccion_1,
            irr.fuente,
        )
        t.aviso(
            f"La reducción del art. 32.1 LIRPF se aplica al importe que has indicado como rendimiento neto irregular, "
            f"con el máximo de {irr.limite_base} € de base. Solo valen los rendimientos generados en más de dos años o "
            "los que el art. 25 RIRPF enumera (subvenciones de capital para inmovilizado no amortizable, "
            "indemnizaciones y ayudas por cese de actividad, premios literarios, artísticos o científicos no exentos, "
            "indemnizaciones que sustituyen derechos económicos de duración indefinida), imputados en un único "
            "ejercicio; no vale si la actividad obtiene ese tipo de rendimientos de forma regular o habitual. El "
            "cálculo no lo comprueba."
        )

    # Art. 32.2. Its thresholds are stated on the "rendimientos netos", i.e. before any reduction.
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
    reduccion = _round(min(reduccion, max(ZERO, neto - reduccion_1)))
    if reduccion:
        t.linea(
            "Total reducciones del art. 32.2 LIRPF (el saldo no puede ser negativo)", -reduccion, "art. 32.2.4.º LIRPF"
        )
    if reduccion and reduccion_1:
        t.aviso(
            "Los umbrales del art. 32.2 LIRPF se han medido con el rendimiento neto antes de la reducción del "
            "art. 32.1, que es como la ley los enuncia («rendimientos netos»)."
        )

    # Art. 32.3: 20 % of the positive net income left after 32.1 and 32.2, on a capped base.
    ini = p.reduccion_inicio_actividad
    reduccion_3 = ZERO
    if inp.reduccion_inicio_actividad:
        base_3 = min(max(ZERO, neto - reduccion_1 - reduccion), ini.limite_base)
        reduccion_3 = _round(base_3 * ini.porcentaje)
        if reduccion_3:
            t.linea(
                f"Reducción del {ini.porcentaje * 100:.0f} % por inicio de actividad (sobre {_round(base_3)} €)",
                -reduccion_3,
                ini.fuente,
            )
        t.aviso(
            f"La reducción del art. 32.3 LIRPF se aplica porque has declarado que te corresponde: no ejerciste ninguna "
            "actividad económica en el año anterior al inicio, este es el primer ejercicio con rendimiento neto "
            "positivo o el siguiente, y no más del "
            f"{ini.max_ingresos_antiguo_pagador * 100:.0f} % de los ingresos procede de quien te pagó rendimientos del "
            f"trabajo en el año anterior al inicio. Se calcula sobre el rendimiento ya minorado por las reducciones "
            f"de los apartados 1 y 2, con un máximo de {ini.limite_base} € de base."
        )
    reducido = _round(neto) - reduccion_1 - reduccion - reduccion_3
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
    # The limit is either printed (not checked) or compared: every result relies on it.
    t.usa(eds.fuente)
    simplificada = (
        None
        if inp.cifra_negocios_ano_anterior is None
        else inp.cifra_negocios_ano_anterior <= eds.limite_cifra_negocios
    )
    if simplificada is None:
        t.aviso(
            f"La modalidad simplificada exige que la cifra de negocios del año anterior no supere "
            f"{eds.limite_cifra_negocios} € y no haber renunciado a ella; no se ha comprobado."
        )
    elif not simplificada:
        t.aviso(
            f"La cifra de negocios del año anterior supera {eds.limite_cifra_negocios} €: no corresponde la modalidad "
            f"simplificada sino la normal ({eds.fuente.referencia}), en la que no existe el porcentaje de gastos de "
            "difícil justificación. Este resultado no es válido para ese caso."
        )
    for aviso in g.avisos:
        t.aviso(aviso)
    avisos_ceuta_melilla(p, inp.ceuta_melilla, t)
    if inp.ceuta_melilla is not None and dependiente:
        t.aviso(
            "Con la reducción del art. 32.2.1.º no se restan gastos de difícil justificación, tampoco los del "
            "porcentaje especial de Ceuta."
        )
    pendientes = [
        texto
        for aplicada, texto in (
            (
                bool(inp.rendimientos_irregulares),
                f"la del {irr.porcentaje * 100:.0f} % por rendimientos irregulares (art. 32.1 LIRPF)",
            ),
            (
                inp.reduccion_inicio_actividad,
                f"la del {ini.porcentaje * 100:.0f} % por inicio de actividad (art. 32.3 LIRPF)",
            ),
        )
        if not aplicada
    ]
    if pendientes:
        t.aviso(
            f"No se ha aplicado {' ni '.join(pendientes)} porque no se ha indicado que corresponda; si es tu caso, "
            "indícalo para que se calcule."
        )
    t.cerrar()

    return ResultadoActividad(
        ejercicio=inp.ejercicio,
        ingresos_integros=_round(inp.ingresos_integros),
        gastos_deducibles=_round(gastos),
        rendimiento_neto_previo=_round(previo),
        gastos_dificil_justificacion=dificil,
        rendimiento_neto=_round(neto),
        reduccion_art_32_1=reduccion_1,
        reduccion_art_32_3=reduccion_3,
        reduccion_art_32_2=reduccion,
        reduccion_aplicada=reduccion_aplicada,
        rendimiento_neto_reducido=reducido,
        rendimiento_actividad_neto=reducido,
        modalidad_simplificada_aplicable=simplificada,
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto("BOE-A-2006-20764", "BOE-A-2007-6820"),
    )
