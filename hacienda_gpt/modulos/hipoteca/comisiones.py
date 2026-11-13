"""Early-repayment fees: the most the bank may charge, by the law that governs the loan and the deed.

The regime depends on the signing date (read from ``data/normativa.json``):

- signed on or after the entry into force of Ley 5/2019: art. 23.5 (variable tranche), 23.7 (fixed
  tranche) and 23.6 (novation or subrogation from variable to fixed), plus the temporary suspension of
  the disposición adicional primera of RDL 19/2022 for variable loans;
- signed under Ley 41/2007 (from its entry into force until Ley 5/2019): art. 8 caps the
  «compensación por desistimiento»; art. 9 the interest-rate-risk compensation;
- signed before Ley 41/2007: Ley 2/1994 caps the fee of variable loans; fixed loans keep what was agreed.

The result is always a CAP: the law also says the fee cannot exceed the lender's financial loss
(art. 23.8 Ley 5/2019), which the module does not compute, so the real fee may be lower or zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from hacienda_gpt.modulos.hipoteca.calculo import euros, sumar_meses
from hacienda_gpt.modulos.hipoteca.modelo import Prestamo
from hacienda_gpt.modulos.hipoteca.normativa import Normativa

ZERO = Decimal(0)
Operacion = Literal["amortizacion", "cambio_a_fijo", "subrogacion"]


@dataclass
class Comision:
    porcentaje: Decimal  # fraction of the amount repaid (0.0025 = 0.25 %)
    importe: Decimal
    fundamento: list[str] = field(default_factory=list)
    citas: list[str] = field(default_factory=list)
    es_maximo: bool = True  # an upper bound (the real fee cannot exceed the lender's loss)
    incierta: bool = False  # depends on a deed term the user has not given


def _anos_desde(inicio: date, fecha: date) -> int:
    """Whole years elapsed from ``inicio`` to ``fecha``."""
    anos = fecha.year - inicio.year
    if (fecha.month, fecha.day) < (inicio.month, inicio.day):
        anos -= 1
    return max(anos, 0)


def tramo_en(p: Prestamo, fecha: date) -> Literal["fijo", "variable"]:
    if p.tipo == "fijo":
        return "fijo"
    if p.tipo == "mixto" and p.meses_tipo_inicial and fecha < sumar_meses(p.fecha_firma, p.meses_tipo_inicial):
        return "fijo"
    return "variable"


def _pactada(p: Prestamo, fecha: date, tramo: str) -> Decimal | None:
    """Fee the deed sets for that date and tranche (fraction), 0 if it sets none, None if unknown."""
    if p.comisiones_pactadas is None:
        return None
    anos = _anos_desde(p.fecha_firma, fecha)
    for c in p.comisiones_pactadas:
        if c.tramo not in ("todo", tramo):
            continue
        if c.desde_anos <= anos and (c.hasta_anos is None or anos < c.hasta_anos):
            return c.porcentaje / 100
    return ZERO


def comision_maxima(
    p: Prestamo, n: Normativa, fecha: date, importe: Decimal, operacion: Operacion = "amortizacion"
) -> Comision:
    """Cap of the early-repayment fee for repaying ``importe`` on ``fecha``.

    ``operacion``: "amortizacion" (plain partial or total repayment), "cambio_a_fijo" (novation or
    subrogation that turns a variable loan into a fixed one, art. 23.6) or "subrogacion" (to another
    bank keeping a variable rate)."""
    tramo = tramo_en(p, fecha)
    anos = _anos_desde(p.fecha_firma, fecha)
    pactada = _pactada(p, fecha, tramo)
    res = _regimen(p, n, fecha, tramo, anos, operacion, pactada)
    res.importe = euros(importe * res.porcentaje)
    return res


def _con_pactada(limite: Decimal, pactada: Decimal | None, res: Comision) -> Comision:
    if pactada is None:
        res.porcentaje = limite
        res.incierta = limite > 0
        if limite > 0:
            res.fundamento.append(
                "No consta la comisión de tu escritura: se muestra el máximo legal. Si tu contrato fija menos "
                "(o ninguna), se aplica lo pactado."
            )
    else:
        res.porcentaje = min(limite, pactada)
        if pactada < limite:
            res.fundamento.append(f"Tu escritura fija {pactada * 100:.2f} %, por debajo del máximo legal.")
    return res


def _regimen(
    p: Prestamo,
    n: Normativa,
    fecha: date,
    tramo: str,
    anos: int,
    operacion: Operacion,
    pactada: Decimal | None,
) -> Comision:
    res = Comision(porcentaje=ZERO, importe=ZERO)
    ley5 = n.ley_5_2019
    # Art. 23.6 Ley 5/2019 applies to every contract, whatever its date (disposición transitoria 1.ª.3).
    if operacion == "cambio_a_fijo" and tramo == "variable":
        g = n.cambio_a_fijo
        res.citas += [g.fuente.boe_id]
        if _suspendida(n, fecha):
            return _suspension(n, res)
        if anos < g.anos:
            res.fundamento.append(
                f"Cambio de variable a fijo en los {g.anos} primeros años: como máximo el "
                f"{g.limite * 100:.2f} % del capital amortizado ({g.cita_corta()})."
            )
            return _con_pactada(g.limite, pactada, res)
        res.fundamento.append(
            f"Cambio de variable a fijo pasados los {g.anos} primeros años: sin comisión ({g.cita_corta()})."
        )
        res.es_maximo = False
        return res

    if p.fecha_firma >= ley5.entrada_en_vigor:
        if tramo == "variable":
            g = n.comision_variable
            res.citas.append(g.fuente.boe_id)
            if _suspendida(n, fecha):
                return _suspension(n, res)
            if anos < g.opcion_b_anos:
                limite = g.opcion_b_limite
                texto = (
                    f"Tramo variable, {g.opcion_b_anos} primeros años: el contrato puede fijar hasta el "
                    f"{g.opcion_b_limite * 100:.2f} % (opción b) o hasta el {g.opcion_a_limite * 100:.2f} % "
                    f"(opción a, cinco años), nunca ambas ({g.cita_corta()})."
                )
            elif anos < g.opcion_a_anos:
                limite = g.opcion_a_limite
                texto = (
                    f"Tramo variable, entre el año {g.opcion_b_anos} y el {g.opcion_a_anos}: solo cabe comisión si "
                    f"el contrato eligió la opción a), como máximo el {g.opcion_a_limite * 100:.2f} % ({g.cita_corta()})."
                )
            else:
                res.fundamento.append(
                    f"Tramo variable pasados {g.opcion_a_anos} años desde la firma: la ley no permite comisión "
                    f"({g.cita_corta()})."
                )
                res.es_maximo = False
                return res
            res.fundamento.append(texto)
            return _con_pactada(limite, pactada, res)
        g7 = n.comision_fijo
        res.citas.append(g7.fuente.boe_id)
        inicio_fijo = p.fecha_firma  # the fixed tranche of a mixed loan starts at signing too
        if _anos_desde(inicio_fijo, fecha) < g7.anos_iniciales:
            limite = g7.limite_inicial
            texto = f"Tramo fijo, {g7.anos_iniciales} primeros años: como máximo el {limite * 100:.2f} %"
        else:
            limite = g7.limite_posterior
            texto = f"Tramo fijo, después de {g7.anos_iniciales} años: como máximo el {limite * 100:.2f} %"
        res.fundamento.append(f"{texto} del capital amortizado ({g7.cita_corta()}).")
        return _con_pactada(limite, pactada, res)

    l41 = n.ley_41_2007
    if p.fecha_firma >= l41.entrada_en_vigor:
        res.citas.append(l41.fuente.boe_id)
        if anos < l41.desistimiento_anos:
            limite = l41.desistimiento_limite_inicial
        else:
            limite = l41.desistimiento_limite_posterior
        res.fundamento.append(
            f"Préstamo firmado antes de la Ley 5/2019: compensación por desistimiento de como máximo el "
            f"{limite * 100:.2f} % ({l41.cita_corta()}, art. 8)."
        )
        if not p.vivienda_habitual:
            res.fundamento.append(
                "La Ley 41/2007 limita así los préstamos sobre vivienda de personas físicas; confirma que es tu caso."
            )
        _con_pactada(limite, pactada, res)
        periodo = p.revision_meses if p.tipo == "variable" else None
        if periodo is not None and periodo <= l41.riesgo_tipo_revision_max_meses:
            res.fundamento.append(
                f"Sin compensación por riesgo de tipo de interés: el periodo de revisión es de {periodo} meses "
                f"({l41.cita_corta()}, art. 9.1)."
            )
        elif p.compensacion_riesgo_tipo is not None:
            res.porcentaje += p.compensacion_riesgo_tipo / 100
            res.fundamento.append(
                f"Más la compensación por riesgo de tipo de interés pactada ({p.compensacion_riesgo_tipo} %), "
                "que solo se cobra si la cancelación causa una pérdida al banco (art. 9 Ley 41/2007)."
            )
        else:
            res.incierta = True
            res.fundamento.append(
                "Puede añadirse una compensación por riesgo de tipo de interés si tu contrato la pacta y la "
                "cancelación causa pérdida al banco (art. 9 Ley 41/2007); indica el porcentaje de tu escritura."
            )
        return res

    l2 = n.ley_2_1994
    res.citas.append(l2.fuente.boe_id)
    if tramo == "variable":
        limite = l2.limite_subrogacion_variable if operacion == "subrogacion" else l2.limite_no_subrogatoria_variable
        res.fundamento.append(
            f"Préstamo variable anterior a la Ley 41/2007: como máximo el {limite * 100:.2f} % "
            f"({l2.cita_corta()})."
        )
        return _con_pactada(limite, pactada, res)
    res.incierta = pactada is None
    res.porcentaje = pactada or ZERO
    res.fundamento.append(
        "Préstamo fijo anterior a la Ley 41/2007: la ley no fija un tope; se aplica lo pactado en la escritura"
        + (" (no consta)." if pactada is None else ".")
    )
    return res


def _suspendida(n: Normativa, fecha: date) -> bool:
    s = n.suspension_comisiones_2022_2024
    return s.desde <= fecha <= s.hasta


def _suspension(n: Normativa, res: Comision) -> Comision:
    s = n.suspension_comisiones_2022_2024
    res.citas.append(s.fuente.boe_id)
    res.es_maximo = False
    res.fundamento.append(
        f"Del {s.desde:%d/%m/%Y} al {s.hasta:%d/%m/%Y} no se devengó comisión en préstamos variables "
        f"({s.cita_corta()})."
    )
    return res
