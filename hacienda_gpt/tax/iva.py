"""Modelo 303: quarterly VAT return in the general regime, domestic operations only.

Output VAT by rate from the taxable bases, minus the deductible input VAT the
taxpayer reports, minus the balance carried from previous periods (arts. 90,
91 and 99 LIVA). It does not decide which rate a product bears or whether an
input quota is deductible, and it leaves out — saying so in ``avisos`` —
prorrata, the special regimes, intra-community operations, imports, reverse
charge and corrections of previous periods.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.actividad import ZERO, Traza, _round
from hacienda_gpt.tax.autonomos_params import AutonomosParams, load_autonomos_params
from hacienda_gpt.tax.irpf import Linea


class Modelo303Input(BaseModel):
    """One quarter. Bases without VAT; absent means zero."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    trimestre: int = Field(ge=1, le=4)
    # Taxable bases of the quarter's sales, by the rate they bear.
    base_tipo_general: Decimal = Field(default=ZERO, ge=0)
    base_tipo_reducido: Decimal = Field(default=ZERO, ge=0)
    base_tipo_superreducido: Decimal = Field(default=ZERO, ge=0)
    # Deductible input VAT of the quarter (quotas, not bases), as the taxpayer reports it.
    cuotas_soportadas_deducibles: Decimal = Field(default=ZERO, ge=0)  # current goods and services
    cuotas_soportadas_bienes_inversion: Decimal = Field(default=ZERO, ge=0)
    # Balance in the taxpayer's favour carried from previous returns.
    cuotas_a_compensar_periodos_anteriores: Decimal = Field(default=ZERO, ge=0)


class ResultadoModelo303(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    trimestre: int
    iva_devengado: Decimal
    iva_deducible: Decimal
    # Output minus input VAT of the quarter, before the carried balance.
    diferencia: Decimal
    compensacion_aplicada: Decimal
    # Positive: to pay. Negative: balance in the taxpayer's favour.
    resultado: Decimal
    tipo_resultado: Literal["a_ingresar", "a_compensar", "cero"]
    # Balance to carry to the following returns (zero when there is something to pay).
    saldo_a_compensar: Decimal
    # Only the last return of the year lets a general-regime taxpayer ask for the refund.
    puede_solicitar_devolucion: bool
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def calcular_modelo_303(inp: Modelo303Input, params: AutonomosParams | None = None) -> ResultadoModelo303:
    """Quarterly VAT return (modelo 303, general regime) for one quarter, with its trace."""
    p = params or load_autonomos_params(inp.ejercicio)
    iva = p.iva
    t = Traza()

    devengado = ZERO
    for nombre, base, tipo, referencia in (
        ("general", inp.base_tipo_general, iva.tipo_general, "art. 90.uno LIVA"),
        ("reducido", inp.base_tipo_reducido, iva.tipo_reducido, "art. 91.uno LIVA"),
        ("superreducido", inp.base_tipo_superreducido, iva.tipo_superreducido, "art. 91.dos LIVA"),
    ):
        if base:
            cuota = _round(base * tipo)
            t.linea(f"IVA devengado al tipo {nombre} ({tipo * 100:.0f} % de {_round(base)} €)", cuota, referencia)
            devengado += cuota
    t.usa(iva.fuente)
    t.linea("Total IVA devengado", devengado, "arts. 90 y 91 LIVA")

    deducible = inp.cuotas_soportadas_deducibles + inp.cuotas_soportadas_bienes_inversion
    if inp.cuotas_soportadas_deducibles:
        t.linea(
            "IVA soportado deducible en bienes y servicios corrientes",
            -inp.cuotas_soportadas_deducibles,
            "art. 99.uno LIVA",
        )
    if inp.cuotas_soportadas_bienes_inversion:
        t.linea(
            "IVA soportado deducible en bienes de inversión",
            -inp.cuotas_soportadas_bienes_inversion,
            "art. 99.uno LIVA",
        )
    diferencia = devengado - deducible
    t.linea("Diferencia del trimestre", diferencia, "art. 99.uno LIVA")

    pendiente = inp.cuotas_a_compensar_periodos_anteriores
    aplicada = min(pendiente, max(ZERO, diferencia))
    if pendiente:
        t.linea("Cuotas a compensar de períodos anteriores", -pendiente, "art. 99.cinco LIVA")
        t.aviso(
            f"Las cuotas a compensar de períodos anteriores solo valen si no han pasado {iva.anos_compensacion} años "
            "desde la presentación de la declaración en que se originaron (art. 99.cinco LIVA); el cálculo no lo comprueba."
        )
    resultado = _round(diferencia - pendiente)
    saldo = max(ZERO, -resultado)
    tipo_resultado: Literal["a_ingresar", "a_compensar", "cero"] = (
        "a_ingresar" if resultado > 0 else "a_compensar" if resultado < 0 else "cero"
    )
    t.linea("Resultado (positivo: a ingresar; negativo: a compensar)", resultado, "art. 99 LIVA")

    devolucion = inp.trimestre == 4 and saldo > 0
    if saldo:
        t.aviso(
            f"Quedan {saldo} € a tu favor. "
            + (
                "Al ser la última declaración del año puedes pedir la devolución (art. 115.uno LIVA) o dejarlos a "
                "compensar; si pides la devolución ya no puedes compensarlos."
                if devolucion
                else "En el régimen general se compensan en las declaraciones siguientes; la devolución solo puede "
                "pedirse en la del último período del año (art. 115.uno LIVA), salvo inscripción en el registro de "
                "devolución mensual (art. 116 LIVA)."
            )
        )
    t.aviso(
        "Las cuotas soportadas se han restado tal como las has indicado: el cálculo no valora si son deducibles "
        "(afectación a la actividad, factura completa, exclusiones y límites de los arts. 95 y 96 LIVA) ni qué tipo "
        "corresponde a cada venta; se aplica el tipo vigente en el devengo (art. 90.dos LIVA)."
    )
    t.aviso(
        "Cálculo solo para el régimen general con operaciones interiores. No cubre: prorrata, regímenes especiales "
        "(recargo de equivalencia, simplificado, criterio de caja, agricultura, ganadería y pesca), operaciones "
        "intracomunitarias, importaciones y exportaciones, inversión del sujeto pasivo, tipo del 0 %, facturas "
        "rectificativas ni regularización de bienes de inversión."
    )
    t.cerrar()

    return ResultadoModelo303(
        ejercicio=inp.ejercicio,
        trimestre=inp.trimestre,
        iva_devengado=_round(devengado),
        iva_deducible=_round(deducible),
        diferencia=_round(diferencia),
        compensacion_aplicada=_round(aplicada),
        resultado=resultado,
        tipo_resultado=tipo_resultado,
        saldo_a_compensar=saldo,
        puede_solicitar_devolucion=devolucion,
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto("BOE-A-1992-28740"),
    )
