"""Modelo 130: quarterly payment on account of the IRPF for activities in
estimación directa (arts. 109 to 111 RIRPF).

One quarter at a time, from cumulative figures since 1 January: the percentage
of the cumulative net income, minus what was due in the previous quarters of
the year, minus the withholdings borne (professionals), minus the flat amount
of art. 110.3.c) for low incomes. Also says whether a professional is excused
from the payment because most of the income bore withholding (art. 109.2).

Not modelled, and said so in ``avisos``: estimación objetiva and farming
(modelo 131 / art. 110.1.b and c), the Ceuta and Melilla reduction of
art. 110.2, the housing deduction of art. 110.3.d) and voluntary higher
percentages (art. 110.4).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hacienda_gpt.tax.actividad import ZERO, Traza, _round, gastos_dificil_justificacion
from hacienda_gpt.tax.autonomos_params import AutonomosParams, load_autonomos_params
from hacienda_gpt.tax.irpf import Linea


class Modelo130Input(BaseModel):
    """One quarter. Amounts in euros without VAT, cumulative from 1 January to the end of the quarter."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    trimestre: int = Field(ge=1, le=4)
    ingresos_acumulados: Decimal = Field(default=ZERO, ge=0)
    # Deductible expenses as the taxpayer reports them, WITHOUT the flat allowance of art. 30 RIRPF.
    gastos_acumulados: Decimal = Field(default=ZERO, ge=0)
    # Estimación directa simplificada: add the flat allowance for hard-to-justify expenses.
    aplicar_gastos_dificil_justificacion: bool = True
    retenciones_acumuladas: Decimal = Field(default=ZERO, ge=0)
    # Sum of the positive amounts of the previous quarters of the year BEFORE the
    # art. 110.3.c) reduction (``pago_previo_a_minoracion`` of each previous result).
    pagos_trimestres_anteriores: Decimal = Field(default=ZERO, ge=0)
    # Professional (IAE sections 2 and 3) rather than business activity.
    actividad_profesional: bool = True

    # Art. 110.3.c): net income from economic activities of the previous year. None = unknown, not applied.
    rendimiento_neto_ejercicio_anterior: Decimal | None = None
    # Part of that reduction that previous quarters of this year could not absorb.
    minoracion_pendiente_trimestres_anteriores: Decimal = Field(default=ZERO, ge=0)

    # Art. 109.2 and 109.5: is the professional obliged to make the payment at all?
    ingresos_ano_anterior: Decimal | None = Field(default=None, ge=0)
    ingresos_con_retencion_ano_anterior: Decimal | None = Field(default=None, ge=0)
    inicio_actividad: bool = False  # the activity started this year
    ingresos_con_retencion_acumulados: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coherencia(self) -> Modelo130Input:
        # Art. 110.1.a) and 110.3.c) speak of "los trimestres anteriores del mismo año" and of
        # "los siguientes pagos fraccionados correspondientes al mismo período impositivo":
        # the first quarter has nothing behind it.
        if self.trimestre == 1 and (
            self.pagos_trimestres_anteriores or self.minoracion_pendiente_trimestres_anteriores
        ):
            raise ValueError(
                "En el primer trimestre no hay pagos fraccionados ni minoración pendiente de trimestres anteriores "
                "del mismo año: deja esos importes a cero."
            )
        # The share of income that bore withholding (art. 109.2 and 109.5) cannot exceed the income itself.
        if (
            self.ingresos_ano_anterior is not None
            and self.ingresos_con_retencion_ano_anterior is not None
            and self.ingresos_con_retencion_ano_anterior > self.ingresos_ano_anterior
        ):
            raise ValueError(
                "Los ingresos con retención del año anterior no pueden superar los ingresos totales de ese año."
            )
        if (
            self.ingresos_con_retencion_acumulados is not None
            and self.ingresos_con_retencion_acumulados > self.ingresos_acumulados
        ):
            raise ValueError("Los ingresos con retención del período no pueden superar los ingresos acumulados.")
        return self


class ResultadoModelo130(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    trimestre: int
    rendimiento_neto_acumulado: Decimal
    # Percentage of the cumulative net income (zero when it is negative).
    pago_sobre_rendimiento: Decimal
    # After previous quarters and withholdings, floored at zero. Add it to
    # ``pagos_trimestres_anteriores`` of the following quarters.
    pago_previo_a_minoracion: Decimal
    minoracion_aplicada: Decimal
    # Reduction of art. 110.3.c) left for later quarters of the same year.
    minoracion_pendiente: Decimal
    # What the computation gives; never negative (art. 111.1: a "declaración negativa" is filed, nothing is refunded).
    resultado: Decimal
    # True/False when it can be told from the figures given; None when it cannot.
    obligado_a_pago_fraccionado: bool | None
    motivo_obligacion: str
    # ``resultado``, or zero when the taxpayer is not obliged.
    a_ingresar: Decimal
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def _obligacion(inp: Modelo130Input, p: AutonomosParams) -> tuple[bool | None, str]:
    umbral = p.pago_fraccionado.exencion_porcentaje_retenido
    pct = f"{umbral * 100:.0f} %"
    if not inp.actividad_profesional:
        return True, "La excepción del art. 109.2 RIRPF solo alcanza a las actividades profesionales."
    if inp.inicio_actividad:
        if inp.ingresos_con_retencion_acumulados is None:
            return None, (
                "Inicio de actividad: se mira el porcentaje de ingresos con retención del período del pago "
                "fraccionado (art. 109.5 RIRPF) y no se ha indicado."
            )
        if inp.ingresos_acumulados <= 0:
            return (
                None,
                "Inicio de actividad sin ingresos en el período: no puede calcularse el porcentaje con retención.",
            )
        retenido = inp.ingresos_con_retencion_acumulados / inp.ingresos_acumulados
        referencia = "en el período del pago fraccionado (art. 109.5 RIRPF)"
    else:
        if inp.ingresos_ano_anterior is None or inp.ingresos_con_retencion_ano_anterior is None:
            return None, (
                f"Depende de si al menos el {pct} de los ingresos del año natural anterior tuvo retención "
                "(art. 109.2 RIRPF) y no se han indicado esos datos."
            )
        if inp.ingresos_ano_anterior <= 0:
            return None, (
                "Sin ingresos de la actividad en el año anterior no puede calcularse el porcentaje con retención; si "
                "la actividad empieza este año, indícalo (art. 109.5 RIRPF)."
            )
        retenido = inp.ingresos_con_retencion_ano_anterior / inp.ingresos_ano_anterior
        referencia = "en el año natural anterior (art. 109.2 RIRPF)"
    if retenido >= umbral:
        return False, f"El {retenido * 100:.2f} % de los ingresos tuvo retención {referencia}: al menos el {pct}."
    return True, f"Solo el {retenido * 100:.2f} % de los ingresos tuvo retención {referencia}: menos del {pct}."


def calcular_modelo_130(inp: Modelo130Input, params: AutonomosParams | None = None) -> ResultadoModelo130:
    """Quarterly payment on account (modelo 130) for one quarter, with its trace."""
    p = params or load_autonomos_params(inp.ejercicio)
    pf = p.pago_fraccionado
    t = Traza()

    t.linea("Ingresos íntegros desde el 1 de enero", inp.ingresos_acumulados, "art. 110.1.a) RIRPF")
    t.linea("Gastos deducibles desde el 1 de enero", -inp.gastos_acumulados, "art. 110.1.a) RIRPF")
    previo = inp.ingresos_acumulados - inp.gastos_acumulados
    dificil = gastos_dificil_justificacion(previo, p) if inp.aplicar_gastos_dificil_justificacion else ZERO
    if dificil:
        g = p.gastos_dificil_justificacion
        t.linea(
            f"Provisiones deducibles y gastos de difícil justificación ({g.porcentaje * 100:.0f} %, "
            f"máximo {g.limite_anual} €)",
            -dificil,
            g.fuente,
        )
        for aviso in g.avisos:
            t.aviso(aviso)
    neto = previo - dificil
    t.linea("Rendimiento neto acumulado", neto, "art. 110.1.a) RIRPF")

    sobre_rendimiento = _round(max(ZERO, neto) * pf.porcentaje)
    t.linea(f"{pf.porcentaje * 100:.0f} % del rendimiento neto acumulado", sobre_rendimiento, pf.fuente)
    restante = sobre_rendimiento
    if inp.pagos_trimestres_anteriores:
        t.linea(
            "Pagos fraccionados de los trimestres anteriores (antes de la minoración del art. 110.3.c)",
            -inp.pagos_trimestres_anteriores,
            "art. 110.1.a) RIRPF",
        )
        restante -= inp.pagos_trimestres_anteriores
    if inp.retenciones_acumuladas:
        if inp.actividad_profesional:
            t.linea(
                "Retenciones e ingresos a cuenta soportados desde el 1 de enero",
                -inp.retenciones_acumuladas,
                "art. 110.3.a) RIRPF",
            )
            restante -= inp.retenciones_acumuladas
        else:
            t.aviso(
                "No se han restado las retenciones: en estimación directa el art. 110.3.a) RIRPF solo permite "
                "deducirlas a las actividades profesionales, al arrendamiento de inmuebles urbanos que sea actividad "
                "económica y a la cesión de derechos de imagen."
            )
    previo_minoracion = _round(max(ZERO, restante))
    t.linea("Pago fraccionado antes de la minoración", previo_minoracion, "art. 110 RIRPF")
    if restante < 0:
        t.aviso(
            "Los pagos anteriores y las retenciones superan el porcentaje sobre el rendimiento: el resultado es cero, "
            "no negativo; se presenta declaración negativa y el exceso se recupera en la declaración anual "
            "(art. 111.1 RIRPF)."
        )

    disponible = inp.minoracion_pendiente_trimestres_anteriores
    if inp.rendimiento_neto_ejercicio_anterior is None:
        t.aviso(
            f"No se ha aplicado la minoración del art. 110.3.c) RIRPF (de {pf.minoracion_tramos[0].importe} a "
            f"{pf.minoracion_tramos[-1].importe} € por trimestre si el rendimiento neto de actividades del año "
            f"anterior no pasó de {pf.minoracion_limite} €) porque no se ha indicado ese rendimiento."
        )
    else:
        disponible += pf.minoracion(inp.rendimiento_neto_ejercicio_anterior)
    minoracion = min(disponible, previo_minoracion)
    pendiente = disponible - minoracion
    if minoracion:
        t.linea("Minoración por rendimientos netos bajos del ejercicio anterior", -minoracion, "art. 110.3.c) RIRPF")
    if pendiente:
        t.aviso(
            f"Quedan {_round(pendiente)} € de minoración del art. 110.3.c) RIRPF sin aplicar: "
            + (
                "se pierden, porque solo pueden deducirse en pagos fraccionados del mismo año."
                if inp.trimestre == 4
                else "pueden deducirse en los trimestres siguientes del mismo año cuyo importe lo permita."
            )
        )
    resultado = previo_minoracion - minoracion
    t.linea("Resultado del pago fraccionado", resultado, "arts. 110 y 111 RIRPF")

    obligado, motivo = _obligacion(inp, p)
    if obligado is False:
        t.aviso(f"No estás obligado a efectuar este pago fraccionado. {motivo}")
    elif obligado is None:
        t.aviso(f"No se ha podido determinar si estás obligado al pago fraccionado. {motivo}")
    t.aviso(
        "Los gastos se han restado tal como los has indicado: el cálculo no valora si son deducibles. El rendimiento "
        "neto es el acumulado del año sin las reducciones del art. 32 LIRPF."
    )
    t.aviso(
        "Cálculo para actividades en estimación directa (art. 110.1.a RIRPF). No cubre la estimación objetiva ni las "
        "actividades agrícolas, ganaderas, forestales o pesqueras (modelo 131 y art. 110.1.b y c), la reducción del "
        "60 % para Ceuta y Melilla (art. 110.2), la deducción por financiación de vivienda habitual (art. 110.3.d) ni "
        "porcentajes voluntarios superiores (art. 110.4)."
    )
    t.cerrar()

    return ResultadoModelo130(
        ejercicio=inp.ejercicio,
        trimestre=inp.trimestre,
        rendimiento_neto_acumulado=_round(neto),
        pago_sobre_rendimiento=sobre_rendimiento,
        pago_previo_a_minoracion=previo_minoracion,
        minoracion_aplicada=_round(minoracion),
        minoracion_pendiente=ZERO if inp.trimestre == 4 else _round(pendiente),
        resultado=_round(resultado),
        obligado_a_pago_fraccionado=obligado,
        motivo_obligacion=motivo,
        a_ingresar=ZERO if obligado is False else _round(resultado),
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto("BOE-A-2007-6820"),
    )
