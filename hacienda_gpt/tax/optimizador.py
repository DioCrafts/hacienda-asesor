"""Deterministic tax optimiser: the best COMBINATION of the choices the law leaves to a household.

The rest of the package values each lever on its own: :mod:`beneficios` changes one input at a
time and :mod:`conjunta` compares filing modes with the figures as they are. Levers interact,
though. A pension contribution lowers the base liquidable and can bring it under the income
ceiling of a regional deduction; realising a loss lowers the base imponible; joint filing moves
every threshold; art. 49 LIRPF caps the offset between the two halves of the savings base at
25 %. Adding up standalone savings is therefore wrong in both directions: two levers can unlock
the same deduction (the sum overstates), or only their combination can (the sum understates).

This module searches the combinations. It has no tax formula of its own: every figure it reports
is the result of :func:`calcular_irpf` on the scenario it describes, so the plan carries the same
trace of lines and legal references as the calculator.

Levers — only choices the law leaves to the taxpayer, with every limit read from the parameter
data (``data/irpf_<year>.json``):

* filing jointly or separately (arts. 82-84 LIRPF), for a married couple whose two sets of figures
  are known, or for a single parent who says so (``familia_monoparental``);
* an additional contribution to pension plans per spouse, within the art. 52.1 limits in
  ``prevision_social`` (the general limit and the share of net work + activity income), each
  limit applied to each spouse separately also in a joint return (art. 84.2.1.º LIRPF);
* the donation the user will make anyway (or up to a budget they set): which kind of recipient
  (a Ley 49/2002 entity, the same entity as the two previous years, or an entity that also gives
  the right to a regional deduction of their community) and, filing separately, which spouse pays;
* realising latent capital losses to offset gains (art. 49 LIRPF, ``compensacion_ahorro``).

Search: a grid of candidate values per continuous lever — zero, the maximum allowed, and every
breakpoint where the lever moves a monitored base exactly onto a threshold read from the data
(regional income ceilings, ends of progressive reductions, scale brackets, the art. 20 "other
income" test, the 30 % cap, donation tiers and caps), each ±1 cent. The grid is enumerated
exhaustively when it is small enough (``busqueda.metodo == "exhaustiva"``; a couple's separate
returns side by side, since once the coupling levers are fixed each return depends on its own);
otherwise coordinate ascent restarted from every single-lever optimum (``"heuristica"``). Both are followed by a
coordinate-ascent polish with breakpoints recomputed at the incumbent, and by a trim that lowers
each amount to the smallest one with the same total tax (never commit more than needed). Ties go
to the smaller commitment, then to the current filing mode, then to the smaller values. No
randomness, Decimal throughout, stable ordering: the same input gives the same plan.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
import itertools
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hacienda_gpt.tax.conjunta import _conjunta, hijos_comunes
from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult, _minimo_descendientes, calcular_irpf
from hacienda_gpt.tax.params import (
    GASTOS_DESGLOSADOS,
    DeduccionCalculable,
    IRPFParams,
    Variante,
    load_params,
)
from hacienda_gpt.tax.profile import (
    ACTIVIDAD_FACT_FIELDS,
    CORE_FIELDS,
    RECUENTOS_DEL_DETALLE,
    DatosInvalidosError,
    _problemas,
    build_input_con_actividad,
)

ZERO = Decimal(0)
CENT = Decimal("0.01")
# calcular_irpf runs the search may make (each takes 0.1-0.5 ms); the per-lever analysis that follows
# (standalone, marginal, trim) is small and bounded and runs outside this cap.
MAX_EVALUACIONES = 3000
# Grid points enumerated exhaustively at most (per filing mode).
MAX_COMBINACIONES = 20000
# The trim lowers an amount only when it saves at least this much commitment (a cent or two less with
# the same rounded tax is noise).
_RECORTE_MINIMO = Decimal(1)

Naturaleza = Literal["ahorro_definitivo", "diferimiento", "requiere_gastar"]
Titular = Literal["contribuyente", "conyuge"]
TipoActivo = Literal["valores_cotizados", "valores_no_cotizados", "otro_elemento"]
Fase = Literal["antes_del_cierre", "solo_opciones_de_declaracion"]
CausaNoDisponible = Literal["fecha", "restricciones", "faltan_datos", "sin_margen", "no_calculable"]

DESTINO_ESTATAL = "ley_49_2002"
DESTINO_RECURRENTE = "ley_49_2002_recurrente"
_PREFIJO_AUTONOMICO = "autonomica:"


# --------------------------------------------------------------------------- #
# Legal texts the optimiser quotes (requirements, not parameters)
# --------------------------------------------------------------------------- #


class TextoLegal(BaseModel):
    """A passage of the law shown to the user as it reads in the BOE consolidated text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    referencia: str
    boe_id: str
    texto: str


_LIRPF = "BOE-A-2006-20764"
_LEY_49 = "BOE-A-2002-25039"
# Copied literally from https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764 (version published
# on 11/11/2026, the one the 2026 parameters were read from) and ...id=BOE-A-2002-25039, read on
# 13/11/2026. They are requirements the optimiser shows; no figure in them feeds a computation.
TEXTOS_LEGALES: dict[str, TextoLegal] = {
    t.referencia: t
    for t in (
        TextoLegal(
            referencia="art. 12 LIRPF",
            boe_id=_LIRPF,
            texto="1. El período impositivo será el año natural. 2. El Impuesto se devengará el 31 de diciembre de "
            "cada año, sin perjuicio de lo establecido en el artículo siguiente.",
        ),
        TextoLegal(
            referencia="art. 14.1.c) LIRPF",
            boe_id=_LIRPF,
            texto="c) Las ganancias y pérdidas patrimoniales se imputarán al período impositivo en que tenga lugar "
            "la alteración patrimonial.",
        ),
        TextoLegal(
            referencia="art. 17.2.a).3.ª LIRPF",
            boe_id=_LIRPF,
            texto="En todo caso, tendrán la consideración de rendimientos del trabajo: a) Las siguientes "
            "prestaciones: […] 3.ª Las prestaciones percibidas por los beneficiarios de planes de pensiones",
        ),
        TextoLegal(
            referencia="art. 33.5.e) LIRPF",
            boe_id=_LIRPF,
            texto="e) Las derivadas de las transmisiones de elementos patrimoniales, cuando el transmitente vuelva a "
            "adquirirlos dentro del año siguiente a la fecha de dicha transmisión. Esta pérdida patrimonial se "
            "integrará cuando se produzca la posterior transmisión del elemento patrimonial.",
        ),
        TextoLegal(
            referencia="art. 33.5.f) LIRPF",
            boe_id=_LIRPF,
            texto="f) Las derivadas de las transmisiones de valores o participaciones admitidos a negociación en "
            "alguno de los mercados secundarios oficiales de valores definidos en la Directiva 2004/39/CE del "
            "Parlamento Europeo y del Consejo de 21 de abril de 2004 relativa a los mercados de instrumentos "
            "financieros, cuando el contribuyente hubiera adquirido valores homogéneos dentro de los dos meses "
            "anteriores o posteriores a dichas transmisiones.",
        ),
        TextoLegal(
            referencia="art. 33.5.g) LIRPF",
            boe_id=_LIRPF,
            texto="g) Las derivadas de las transmisiones de valores o participaciones no admitidos a negociación en "
            "alguno de los mercados secundarios oficiales de valores definidos en la Directiva 2004/39/CE del "
            "Parlamento Europeo y del Consejo de 21 de abril de 2004 relativa a los mercados de instrumentos "
            "financieros, cuando el contribuyente hubiera adquirido valores homogéneos en el año anterior o "
            "posterior a dichas transmisiones.",
        ),
        TextoLegal(
            referencia="art. 33.5, último párrafo, LIRPF",
            boe_id=_LIRPF,
            texto="En los casos previstos en los párrafos f) y g) anteriores, las pérdidas patrimoniales se "
            "integrarán a medida que se transmitan los valores o participaciones que permanezcan en el patrimonio "
            "del contribuyente.",
        ),
        TextoLegal(
            referencia="art. 52.2 LIRPF",
            boe_id=_LIRPF,
            texto="2. Los partícipes, mutualistas o asegurados que hubieran efectuado aportaciones a los sistemas de "
            "previsión social a que se refiere el artículo 51 de esta Ley, podrán reducir en los cinco ejercicios "
            "siguientes las cantidades aportadas incluyendo, en su caso, las aportaciones del promotor o las "
            "realizadas por la empresa que les hubiesen sido imputadas, que no hubieran podido ser objeto de "
            "reducción en la base imponible por insuficiencia de la misma o por aplicación del límite porcentual "
            "establecido en el apartado 1 anterior.",
        ),
        TextoLegal(
            referencia="art. 61.1.ª LIRPF",
            boe_id=_LIRPF,
            texto="1.ª Cuando dos o más contribuyentes tengan derecho a la aplicación del mínimo por descendientes, "
            "ascendientes o discapacidad, respecto de los mismos ascendientes o descendientes, su importe se "
            "prorrateará entre ellos por partes iguales.",
        ),
        TextoLegal(
            referencia="art. 82.1.2.ª LIRPF",
            boe_id=_LIRPF,
            texto="2.ª En los casos de separación legal, o cuando no existiera vínculo matrimonial, la formada por el "
            "padre o la madre y todos los hijos que convivan con uno u otro y que reúnan los requisitos a que se "
            "refiere la regla 1.ª de este artículo.",
        ),
        TextoLegal(
            referencia="art. 83.1 LIRPF",
            boe_id=_LIRPF,
            texto="La opción por la tributación conjunta no vinculará para períodos sucesivos.",
        ),
        TextoLegal(
            referencia="art. 83.2 LIRPF",
            boe_id=_LIRPF,
            texto="La opción ejercitada para un período impositivo no podrá ser modificada con posterioridad "
            "respecto del mismo una vez finalizado el plazo reglamentario de declaración.",
        ),
        TextoLegal(
            referencia="art. 84.2.1.º LIRPF",
            boe_id=_LIRPF,
            texto="1.º Los límites máximos de reducción en la base imponible previstos en los artículos 52, 53 y 54 "
            "y en la disposición adicional undécima de esta Ley, serán aplicados individualmente por cada "
            "partícipe o mutualista integrado en la unidad familiar.",
        ),
        TextoLegal(
            referencia="art. 19.1 Ley 49/2002",
            boe_id=_LEY_49,
            texto="Si en los dos períodos impositivos inmediatos anteriores se hubieran realizado donativos, "
            "donaciones o aportaciones con derecho a deducción en favor de una misma entidad, siendo el importe "
            "del donativo, donación o aportación de este ejercicio y el del período impositivo anterior, igual o "
            "superior, en cada uno de ellos, al del ejercicio inmediato anterior, el porcentaje de deducción "
            "aplicable a la base de la deducción en favor de esa misma entidad que exceda de 250 euros, será el "
            "45 por ciento.",
        ),
    )
}


def _cita(referencia: str) -> str:
    texto = TEXTOS_LEGALES[referencia]
    return f"{texto.referencia} ({texto.boe_id}): «{texto.texto}»"


# --------------------------------------------------------------------------- #
# Input: the user's constraints
# --------------------------------------------------------------------------- #


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PosicionLatente(_Modelo):
    """A latent loss (or gain) the user could realise by selling before 31 December."""

    descripcion: str = Field(default="", max_length=200)
    # Positive figure: the loss (or gain) the sale would realise.
    importe: Decimal = Field(gt=0, le=Decimal("1000000000"))
    # Listed securities or fund units, unlisted ones, or any other asset: the repurchase rule differs.
    tipo: TipoActivo = "valores_cotizados"
    titular: Titular = "contribuyente"


class Restricciones(_Modelo):
    """What the user can and will do. Absent = not stated (the plan says what it assumed)."""

    # Cash each spouse can still put into pension plans this year (on top of what the profile holds).
    # None = not stated: the plan assumes up to the legal limit and says so.
    aportacion_maxima_pensiones: Decimal | None = Field(default=None, ge=0)
    aportacion_maxima_pensiones_conyuge: Decimal | None = Field(default=None, ge=0)
    # Whether the user accepts money locked until retirement. None = not stated (assumed, and said).
    acepta_iliquidez: bool | None = None
    # Donation the user will make this year anyway: the optimiser only chooses its recipient and donor.
    donativo_previsto: Decimal = Field(default=ZERO, ge=0, le=Decimal("100000000"))
    # Most the user is willing to donate in total; None = exactly ``donativo_previsto``.
    presupuesto_donativos: Decimal | None = Field(default=None, ge=0, le=Decimal("100000000"))
    # Last year's donation to an entity the user also gave to the year before, with last year's
    # amount not below the previous one (art. 19.1 Ley 49/2002; the user states it holds).
    donativo_recurrente_importe_anterior: Decimal | None = Field(default=None, gt=0)
    perdidas_latentes: list[PosicionLatente] = Field(default_factory=list, max_length=20)
    ganancias_latentes: list[PosicionLatente] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _presupuesto_coherente(self) -> Restricciones:
        if self.presupuesto_donativos is not None and self.presupuesto_donativos < self.donativo_previsto:
            raise ValueError(
                "el presupuesto de donativos no puede ser menor que el donativo que ya tienes previsto hacer"
            )
        return self

    @property
    def tope_donativos(self) -> Decimal:
        return self.donativo_previsto if self.presupuesto_donativos is None else self.presupuesto_donativos


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


class Declaracion(_Modelo):
    """One return of the household in a scenario."""

    titular: Literal["contribuyente", "conyuge", "conjunta"]
    resultado: IRPFResult


class ResultadoHogar(_Modelo):
    tributacion: Literal["individual", "conjunta_biparental", "conjunta_monoparental"]
    declaraciones: list[Declaracion]
    # Sum of the returns' results (positive: to pay; negative: refund) and of their cuota líquida.
    resultado_total: Decimal
    cuota_liquida_total: Decimal


class PalancaElegida(_Modelo):
    """A lever the optimal plan uses, with what it is worth inside the plan and on its own."""

    id: str
    nombre: str
    titular: Literal["contribuyente", "conyuge", "hogar"]
    # What to do, in words ("Aportar 1.500,00 €", "Declaración conjunta", …).
    accion: str
    # Amount of the action (contribution, donation, loss to realise); None for a filing option.
    importe: Decimal | None = None
    # Filing mode or donation recipient chosen; None for a plain amount.
    opcion: str | None = None
    naturaleza: Naturaleza
    # Euros the lever locks, spends or realises (0 for a filing option).
    compromiso: Decimal
    # Tax the plan would pay more without this lever (the rest of the plan unchanged).
    ahorro_marginal: Decimal
    # Saving of this lever alone, at its own best value, from the current situation.
    ahorro_aislado: Decimal
    accion_aislada: str
    ahorro_por_euro: Decimal | None = None
    # For donations: what the donation costs once the tax saving is deducted.
    coste_neto: Decimal | None = None
    plazo: date | None = None
    referencia: str
    verificado: bool
    requisitos: list[str]
    explicacion: str


class PalancaNoDisponible(_Modelo):
    id: str
    nombre: str
    causa: CausaNoDisponible
    detalle: str
    datos_que_faltan: list[str] = Field(default_factory=list)


class PalancaDescartada(_Modelo):
    """A choice that was considered and is not offered, with the reason."""

    id: str
    nombre: str
    motivo: str
    referencia: str


class CambioLinea(_Modelo):
    """A line of the household's trace that changes between the current situation and the plan
    (amounts added over the household's returns)."""

    concepto: str
    antes: Decimal
    despues: Decimal
    referencia: str | None = None


class Busqueda(_Modelo):
    metodo: Literal["exhaustiva", "heuristica"]
    # calcular_irpf runs made (deterministic: the same input makes the same number).
    evaluaciones: int
    limite_evaluaciones: int
    # Grid points enumerated (all of them when exhaustive).
    combinaciones: int
    # Candidate values per lever on the initial grid.
    candidatos: dict[str, int]
    nota: str


class PlanOptimo(_Modelo):
    ejercicio: int
    fecha: date
    fase: Fase
    con_conyuge: bool
    base: ResultadoHogar
    optimo: ResultadoHogar
    # base.resultado_total - optimo.resultado_total.
    ahorro_total: Decimal
    # Sum of each chosen lever's standalone saving, and how far the combination departs from it.
    suma_ahorros_aislados: Decimal
    interaccion: Decimal
    explicacion_interaccion: list[str]
    palancas: list[PalancaElegida]
    no_disponibles: list[PalancaNoDisponible]
    descartadas: list[PalancaDescartada]
    oportunidades: list[str]
    cambios_traza: list[CambioLinea]
    citas: list[str]
    textos_legales: list[TextoLegal]
    avisos: list[str]
    supuestos: list[str]
    datos_que_faltan: list[str]
    busqueda: Busqueda
    explicacion: str


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _eur(importe: Decimal) -> str:
    """1234.5 → '1.234,50 €' (sign kept)."""
    texto = f"{abs(importe):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if importe < 0 else ''}{texto} €"


def _abajo(valor: Decimal) -> Decimal:
    return valor.quantize(CENT, rounding=ROUND_FLOOR)


def _arriba(valor: Decimal) -> Decimal:
    return valor.quantize(CENT, rounding=ROUND_CEILING)


def _alrededor(valor: Decimal) -> set[Decimal]:
    """The cents just below, on and just above ``valor`` (a threshold may be inclusive or strict)."""
    abajo, arriba = _abajo(valor), _arriba(valor)
    return {abajo - CENT, abajo, arriba, arriba + CENT}


def _linea(result: IRPFResult, prefijo: str) -> Decimal:
    return sum((line.importe for line in result.lineas if line.concepto.startswith(prefijo)), ZERO)


def _fecha(dia: date) -> str:
    return dia.strftime("%d/%m/%Y")


def _unicos(items: Iterable[str]) -> list[str]:
    vistos: dict[str, None] = {}
    for item in items:
        if item and item not in vistos:
            vistos[item] = None
    return list(vistos)


# Bases a lever moves one euro per euro (while it acts). "base_familia" is the base imponible of
# the whole family unit (own + ``base_imponible_resto_unidad_familiar``); "otras_rentas" is the
# income other than work that arts. 20 and DA 61.ª LIRPF compare with their ceiling.
_MUEVE_PENSION = frozenset({"base_liquidable_total", "base_liquidable_general", "base_liquidable_total_menos_minimos"})
_MUEVE_PERDIDA = frozenset(
    {
        "base_imponible_total",
        "base_imponible_ahorro",
        "base_liquidable_total",
        "base_liquidable_ahorro",
        "base_imponible_total_menos_minimos",
        "base_liquidable_total_menos_minimos",
        "base_imponible_total_menos_minimo_descendientes",
        "base_familia",
        "otras_rentas",
    }
)
_MUEVE_RESTO = frozenset({"base_familia"})


def _valores_base(inp: IRPFInput, res: IRPFResult, p: IRPFParams) -> dict[str, Decimal]:
    """The monitored bases of a computed return."""
    big, bia = res.base_imponible_general, res.base_imponible_ahorro
    blg, bla = res.base_liquidable_general, res.base_liquidable_ahorro
    bit, blt, minimo = big + bia, blg + bla, res.minimo_personal_familiar
    alquiler = inp.rendimiento_alquiler_vivienda_sin_reducir + _linea(res, "Reducción por arrendamiento de vivienda")
    otras = sum(
        (
            max(ZERO, v)
            for v in (
                inp.rendimiento_actividad_neto,
                inp.rendimiento_alquiler_neto,
                alquiler,
                inp.otras_rentas_base_general,
                inp.rendimiento_capital_mobiliario,
                inp.ganancias_patrimoniales,
            )
        ),
        ZERO,
    )
    return {
        "base_imponible_total": bit,
        "base_imponible_general": big,
        "base_imponible_ahorro": bia,
        "base_liquidable_total": blt,
        "base_liquidable_general": blg,
        "base_liquidable_ahorro": bla,
        "base_imponible_total_menos_minimos": bit - minimo,
        "base_liquidable_total_menos_minimos": blt - minimo,
        "base_imponible_total_menos_minimo_descendientes": bit - _minimo_descendientes(inp, p.minimos),
        "base_familia": bit + inp.base_imponible_resto_unidad_familiar,
        "otras_rentas": otras,
    }


def _umbrales_calculable(d: DeduccionCalculable, inp: IRPFInput, conjunta: bool) -> Iterator[tuple[str, Decimal]]:
    """Income thresholds of one data-driven regional deduction (see autonomicas.evaluar)."""
    base = d.base_limite
    limites: list[Decimal | None] = []
    fuentes: list[DeduccionCalculable | Variante] = [d, *d.variantes]
    for fuente in fuentes:
        individual, conj = fuente.limite_individual, fuente.limite_conjunta
        if conj is None and individual is not None and d.limite_conjunta_incremento is not None:
            conj = individual + d.limite_conjunta_incremento
        limites.append(conj if conjunta and conj is not None else individual)
    for limite in [lim for lim in limites if lim is not None]:
        yield base, limite
        for ajuste in d.ajustes_limite:
            if ajuste.factor is not None:
                yield base, limite * ajuste.factor
    r = d.reduccion_progresiva
    if r is not None:
        yield base, r.desde_conjunta if conjunta and r.desde_conjunta is not None else r.desde_individual
        yield base, r.hasta_conjunta if conjunta and r.hasta_conjunta is not None else r.hasta_individual
    condiciones = [
        d.condiciones,
        *d.alternativas,
        *(v.cuando for v in d.variantes),
        *(a.cuando for a in d.ajustes_limite),
    ]
    for c in condiciones:
        if c.ingreso_max is not None:
            yield base, c.ingreso_max
    if d.limite_unidad_familiar is not None:
        yield "base_familia", d.limite_unidad_familiar
    if d.base_ahorro_max is not None:
        yield "base_imponible_ahorro", d.base_ahorro_max
    if d.gasto_minimo_sobre_base:
        gasto = sum((Decimal(getattr(inp, b) or 0) for b in d.bases), ZERO)
        if gasto:
            yield base, gasto / d.gasto_minimo_sobre_base


def _umbrales(inp: IRPFInput, res: IRPFResult, p: IRPFParams) -> list[tuple[str, Decimal]]:
    """Every threshold on a monitored base that the data sets for this return."""
    conjunta = inp.tributacion != "individual"
    out: list[tuple[str, Decimal]] = []
    comunidad = p.comunidad(inp.comunidad_autonoma)
    general = comunidad.escala_general if comunidad is not None else p.escala_general_estatal
    for escala in (p.escala_general_estatal, general):
        out += [("base_liquidable_general", t.hasta) for t in escala.tramos if t.hasta is not None]
    for escala in (p.escala_ahorro_estatal, p.escala_ahorro_autonomica):
        out += [("base_liquidable_ahorro", t.hasta) for t in escala.tramos if t.hasta is not None]
    # The minimum is taxed at zero first on the general base, then on the savings base.
    for minimo in (res.minimo_personal_familiar, res.minimo_autonomico):
        out += [("base_liquidable_general", minimo), ("base_liquidable_total", minimo)]
    r20 = p.trabajo.reduccion
    out.append(("otras_rentas", r20.max_otras_rentas))
    if p.deduccion_rendimientos_trabajo is not None:
        out.append(("otras_rentas", p.deduccion_rendimientos_trabajo.max_otras_rentas))
    av = p.alquiler_vivienda_habitual
    if av is not None and inp.alquiler_vivienda_habitual:
        out += [("base_imponible_total", av.umbral_inferior), ("base_imponible_total", av.umbral_superior)]
    if comunidad is not None:
        for d in comunidad.deducciones_calculables:
            out.extend(_umbrales_calculable(d, inp, conjunta))
        m = comunidad.deducciones_madrid
        if m is not None:
            miembros = inp.miembros_unidad_familiar or (
                1 + inp.num_descendientes + (1 if inp.tributacion == "conjunta_biparental" else 0)
            )
            for limites in (m.nacimiento, m.alquiler):
                out.append(
                    (
                        "base_imponible_total",
                        limites.limite_base_conjunta if conjunta else limites.limite_base_individual,
                    )
                )
                out.append(("base_familia", limites.limite_base_unidad_familiar))
            if inp.alquiler_vivienda_habitual and m.alquiler.porcentaje_minimo_sobre_base:
                out.append(
                    ("base_imponible_total", inp.alquiler_vivienda_habitual / m.alquiler.porcentaje_minimo_sobre_base)
                )
            out.append(("base_familia", m.gastos_educativos.limite_base_por_miembro * miembros))
            out.append(("base_familia", m.cuidado.limite_base_por_miembro * miembros))
            out.append(("base_familia", m.familias_ingresos_reducidos.limite_suma_bases))
    return out


def _sumar_donativo(inp: IRPFInput, destino: str, importe: Decimal) -> dict[str, Any]:
    """Input changes for donating ``importe`` to a recipient of kind ``destino``."""
    cambios: dict[str, Any] = {"donativos": inp.donativos + importe}
    if destino == DESTINO_RECURRENTE:
        cambios["donativos_recurrentes"] = True
    elif destino.startswith(_PREFIJO_AUTONOMICO):
        campo = destino.removeprefix(_PREFIJO_AUTONOMICO)
        cambios[campo] = getattr(inp, campo) + importe
        for total, partes in GASTOS_DESGLOSADOS.items():
            if campo in partes:
                # Keep the broad total equal to the sum of its parts (IRPFInput's rule).
                cambios[total] = getattr(inp, total) + importe
    return cambios


def _agregar_lineas(resultados: Iterable[IRPFResult]) -> dict[str, tuple[Decimal, str | None]]:
    """Trace lines added over a household's returns, by concept, in order of first appearance."""
    out: dict[str, tuple[Decimal, str | None]] = {}
    for res in resultados:
        for line in res.lineas:
            previo, referencia = out.get(line.concepto, (ZERO, line.referencia))
            out[line.concepto] = (previo + line.importe, referencia or line.referencia)
    return out


# --------------------------------------------------------------------------- #
# The search space
# --------------------------------------------------------------------------- #

Donativo = tuple[str, int, Decimal]  # (destination, donor 0/1, amount)


@dataclass(frozen=True)
class _Punto:
    """One combination of lever values. Amounts are on top of what the profile already holds."""

    conjunta: bool
    pension: tuple[Decimal, Decimal] = (ZERO, ZERO)
    perdida: tuple[Decimal, Decimal] = (ZERO, ZERO)
    donativo: Donativo | None = None

    def orden(self) -> tuple[Any, ...]:
        return (self.conjunta, self.pension, self.perdida, self.donativo or ("", -1, ZERO))


@dataclass(frozen=True)
class _Evaluacion:
    total: Decimal
    # (label, input, result) per return.
    declaraciones: tuple[tuple[str, IRPFInput, IRPFResult], ...]


@dataclass(frozen=True)
class _Destino:
    id: str
    etiqueta: str
    requisitos: tuple[str, ...]
    referencia: str
    verificado: bool


class _PresupuestoAgotadoError(Exception):
    pass


@dataclass
class _Coordenada:
    nombre: str
    tipo: Literal["pension", "perdida", "donativo"]
    # 0 / 1: that spouse; None: the household total (joint return).
    titular: int | None
    minimo: Decimal = ZERO
    maximo: Decimal = ZERO


@dataclass
class _Estado:
    """Per-run bookkeeping of the search."""

    metodo: Literal["exhaustiva", "heuristica"] = "exhaustiva"
    combinaciones: int = 0
    candidatos: dict[str, int] = field(default_factory=dict)


class _Optimizador:
    def __init__(
        self,
        contribuyente: IRPFInput,
        conyuge: IRPFInput | None,
        *,
        conocidos: frozenset[str],
        conocidos_conyuge: frozenset[str],
        restricciones: Restricciones,
        fecha: date,
        estado_civil: str | None,
        params: IRPFParams,
        max_evaluaciones: int,
        avisos_previos: list[str],
    ) -> None:
        self.p = params
        self.r = restricciones
        self.fecha = fecha
        self.ejercicio = contribuyente.ejercicio
        self.cierre = date(self.ejercicio, 12, 31)
        self.fase: Fase = "antes_del_cierre" if fecha <= self.cierre else "solo_opciones_de_declaracion"
        self.conocidos = conocidos
        self.conocidos_conyuge = conocidos_conyuge
        self.estado_civil = (estado_civil or "").strip().lower() or None
        self.max_evaluaciones = max_evaluaciones
        # calcular_irpf runs allowed right now (None: no cap; the search phases set one).
        self._tope: int | None = None
        self.evaluaciones = 0
        self._cache: dict[tuple[Any, ...], IRPFResult] = {}
        self._entradas: dict[tuple[Any, ...], IRPFInput] = {}
        self._evaluaciones_punto: dict[_Punto, _Evaluacion] = {}
        self._bi_cache: dict[tuple[int, Decimal], Decimal] = {}
        self.avisos: list[str] = list(avisos_previos)
        self.supuestos: list[str] = []
        self.no_disponibles: list[PalancaNoDisponible] = []
        self.estado = _Estado()

        self.con_conyuge = conyuge is not None
        self.hijos_de_ambos = False
        if conyuge is not None:
            if contribuyente.tributacion == "conjunta_monoparental":
                raise DatosInvalidosError(
                    "Con los datos de tu cónyuge, la declaración conjunta es la de la unidad familiar con cónyuge "
                    "(conjunta_biparental), no la monoparental."
                )
            # Children common to both separate returns, each applying half the minimum (as comparar_tributacion).
            comunes = hijos_comunes(
                contribuyente,
                conyuge,
                sin_hijos_a=bool(conocidos & {"num_descendientes", "descendientes"}),
                sin_hijos_b=bool(conocidos_conyuge & {"num_descendientes", "descendientes"}),
            )
            if comunes.copiados_b:
                self.supuestos.append(
                    "Tu cónyuge no ha indicado hijos: se han tomado los tuyos como comunes. Sus gastos (guardería, "
                    "colegio…) figuran solo en tu declaración."
                )
            if comunes.copiados_a:
                self.supuestos.append(
                    "No has indicado hijos: se han tomado los de tu cónyuge como comunes. Sus gastos (guardería, "
                    "colegio…) figuran solo en su declaración."
                )
            self.hijos_de_ambos = bool(comunes.a.num_descendientes and comunes.b.num_descendientes)
            self.individuales = [x.model_copy(update={"tributacion": "individual"}) for x in (comunes.a, comunes.b)]
            self.base_conjunta = contribuyente.tributacion == "conjunta_biparental"
        # A single return already marked joint without the spouse's own figures: it is kept as it is.
        self.conjunta_sin_desglose = conyuge is None and contribuyente.tributacion == "conjunta_biparental"
        if conyuge is None:
            self.individuales = [
                (
                    contribuyente
                    if self.conjunta_sin_desglose
                    else contribuyente.model_copy(update={"tributacion": "individual"})
                )
            ]
            self.base_conjunta = contribuyente.tributacion != "individual"
        self.modos = self._modos_disponibles(contribuyente)
        self._j0 = self._entrada_conjunta_base() if True in self.modos else None

        ps = self.p.prevision_social
        # Net work + activity income of each spouse (the base of the art. 52.1 a) cap), from a run.
        self.a0 = [inp.aportacion_plan_pensiones for inp in self.individuales]
        self.e0 = [inp.contribucion_empresa_plan_pensiones for inp in self.individuales]
        self.rn: list[Decimal] = []
        for i, inp in enumerate(self.individuales):
            res = self._calcular(("R", i), _fijo(inp, self.p))
            self.rn.append(self._rendimientos_para_limite(inp, res))
        self.tope30 = [_abajo(ps.porcentaje_rendimientos * max(ZERO, rn)) for rn in self.rn]
        self.util = [
            min(ps.limite_general, max(ZERO, tope - min(e, ps.incremento_contribucion_empresarial)))
            for tope, e in zip(self.tope30, self.e0, strict=True)
        ]
        self.max_pension = [self._max_pension(i) for i in range(len(self.individuales))]
        if not self.con_conyuge and any(
            pos.titular == "conyuge" for pos in (*self.r.perdidas_latentes, *self.r.ganancias_latentes)
        ):
            raise DatosInvalidosError(
                "Hay pérdidas o ganancias latentes de tu cónyuge, pero no constan sus datos: indícalos o quita esas "
                "posiciones."
            )
        self.max_perdida = [
            sum((pos.importe for pos in self.r.perdidas_latentes if self._indice(pos.titular) == i), ZERO)
            for i in range(len(self.individuales))
        ]
        self.perdidas_disponibles()
        self.destinos = self._destinos()
        self._lever_donativo = self._donativo_disponible()
        comunidad = self.p.comunidad(contribuyente.comunidad_autonoma)
        # Whether the other spouse's base imponible can change a deduction (family-unit income ceilings).
        self._resto_importa = comunidad is not None and (
            comunidad.deducciones_madrid is not None
            or any(d.limite_unidad_familiar is not None for d in comunidad.deducciones_calculables)
        )
        if self.conjunta_sin_desglose:
            self.avisos.append(
                "Tu expediente indica declaración conjunta con las cifras de toda la unidad familiar: el límite de "
                "planes de pensiones se ha aplicado una sola vez, aunque cada cónyuge tiene el suyo "
                f"({_cita('art. 84.2.1.º LIRPF')}). Indica las cifras de tu cónyuge por separado para optimizarlo."
            )

    # ------------------------------------------------------------------ #
    # Availability of each lever
    # ------------------------------------------------------------------ #

    def _indice(self, titular: Titular) -> int:
        return 0 if titular == "contribuyente" else 1

    def _modos_disponibles(self, inp: IRPFInput) -> list[bool]:
        """Filing modes to compare (False: separate, True: joint), the current one first."""
        tc = self.p.tributacion_conjunta
        if self.con_conyuge:
            return [self.base_conjunta, not self.base_conjunta]
        if self.conjunta_sin_desglose:
            self.no_disponibles.append(
                PalancaNoDisponible(
                    id="tributacion",
                    nombre="Tributación conjunta o individual",
                    causa="faltan_datos",
                    detalle="Tu expediente indica declaración conjunta con las cifras de toda la unidad familiar. "
                    "Para comparar con las individuales (y aplicar a cada cónyuge su propio límite de planes de "
                    "pensiones) hacen falta las cifras de tu cónyuge por separado.",
                    datos_que_faltan=["conyuge"],
                )
            )
            return [True]
        monoparental_posible = (
            inp.num_descendientes > 0 and inp.familia_monoparental and not inp.descendientes_compartidos
        ) and not (self.estado_civil or "").startswith("casad")
        if monoparental_posible:
            return [self.base_conjunta, not self.base_conjunta]
        if self.estado_civil is not None and self.estado_civil.startswith("casad"):
            self.no_disponibles.append(
                PalancaNoDisponible(
                    id="tributacion",
                    nombre="Tributación conjunta o individual",
                    causa="faltan_datos",
                    detalle="Estás casado: podéis presentar una declaración conjunta, que reduce la base en "
                    f"{_eur(tc.reduccion_biparental)} ({tc.fuente.referencia}) pero suma las rentas de los dos. Para "
                    "compararla hacen falta las cifras de tu cónyuge.",
                    datos_que_faltan=["conyuge"],
                )
            )
        elif self.estado_civil is None and "estado_civil" not in self.conocidos:
            self.no_disponibles.append(
                PalancaNoDisponible(
                    id="tributacion",
                    nombre="Tributación conjunta o individual",
                    causa="faltan_datos",
                    detalle="Si estás casado, la declaración conjunta puede salir mejor: indica tu estado civil y, "
                    "si lo estás, las cifras de tu cónyuge.",
                    datos_que_faltan=["estado_civil"],
                )
            )
        return [self.base_conjunta]

    def _entrada_conjunta_base(self) -> IRPFInput:
        if not self.con_conyuge:
            if self.conjunta_sin_desglose:
                return self.individuales[0]
            return self.individuales[0].model_copy(update={"tributacion": "conjunta_monoparental"})
        try:
            return _conjunta(*self.individuales)
        except ValidationError as exc:
            problemas = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
            raise DatosInvalidosError(
                f"No se puede formar la declaración conjunta sumando los datos de los dos cónyuges: {problemas}. "
                "Un gasto repartido por hijo en `descendientes` debe darse por hijo también en el otro cónyuge, o "
                "solo en uno de los dos."
            ) from exc

    def _rendimientos_para_limite(self, inp: IRPFInput, res: IRPFResult) -> Decimal:
        """Net work income before the art. 20 reduction plus net activity income, as calcular_irpf caps on."""
        neto = _linea(res, "Rendimiento neto reducido del trabajo")
        reduccion = -_linea(res, "Reducción por obtención de rendimientos del trabajo")
        return neto + reduccion + inp.rendimiento_actividad_neto

    def _max_pension(self, i: int) -> Decimal:
        nombre = "Aportación a tu plan de pensiones" if i == 0 else "Aportación al plan de pensiones de tu cónyuge"
        lever_id = f"plan_pensiones_{'contribuyente' if i == 0 else 'conyuge'}"
        ps = self.p.prevision_social
        efectivo = self.r.aportacion_maxima_pensiones if i == 0 else self.r.aportacion_maxima_pensiones_conyuge
        margen = max(ZERO, self.util[i] - self.a0[i])

        def fuera(causa: CausaNoDisponible, detalle: str, faltan: list[str] | None = None) -> Decimal:
            self.no_disponibles.append(
                PalancaNoDisponible(
                    id=lever_id, nombre=nombre, causa=causa, detalle=detalle, datos_que_faltan=faltan or []
                )
            )
            return ZERO

        if self.fase != "antes_del_cierre":
            return fuera(
                "fecha",
                f"El ejercicio {self.ejercicio} terminó el {_fecha(self.cierre)}: una aportación hecha ahora cuenta "
                f"para {self.ejercicio + 1} ({TEXTOS_LEGALES['art. 12 LIRPF'].referencia}).",
            )
        if self.r.acepta_iliquidez is False:
            return fuera(
                "restricciones",
                "Has indicado que no quieres inmovilizar dinero: un plan de pensiones solo se puede rescatar en la "
                "jubilación y en los demás supuestos tasados.",
            )
        conocidos = self.conocidos if i == 0 else self.conocidos_conyuge
        if not self.rn[i] or self.rn[i] < 0:
            faltan = [
                c
                for c in ("rendimiento_trabajo_bruto", "rendimiento_actividad_neto")
                if c not in conocidos
                and not (c == "rendimiento_actividad_neto" and set(ACTIVIDAD_FACT_FIELDS) & conocidos)
            ]
            sin_datos = len(faltan) == 2
            return fuera(
                "faltan_datos" if sin_datos else "sin_margen",
                f"La reducción no puede superar el {ps.porcentaje_rendimientos * 100:.0f} % de los rendimientos netos "
                f"del trabajo y de actividades económicas ({ps.fuente.referencia}), "
                + ("y no constan." if sin_datos else "y no los hay (o son negativos)."),
                faltan if sin_datos else None,
            )
        if margen <= 0:
            return fuera(
                "sin_margen",
                f"Ya se aplica todo lo que admite el límite de este año ({_eur(self.util[i])}: el menor entre "
                f"{_eur(ps.limite_general)} y el {ps.porcentaje_rendimientos * 100:.0f} % de los rendimientos netos "
                f"del trabajo y de actividades, {ps.fuente.referencia}).",
            )
        if efectivo is not None and efectivo <= 0:
            return fuera("restricciones", "Has indicado que no puedes aportar más este año.")
        if efectivo is None:
            self.supuestos.append(
                f"{nombre}: no has indicado cuánto puedes aportar; se ha supuesto que puedes llegar al máximo que "
                f"reduce la base este año ({_eur(margen)})."
            )
        if self.r.acepta_iliquidez is None:
            supuesto = (
                "No has dicho si aceptas inmovilizar dinero en un plan de pensiones: se ha supuesto que sí. El dinero "
                "solo se puede rescatar en la jubilación y en los demás supuestos tasados."
            )
            if supuesto not in self.supuestos:
                self.supuestos.append(supuesto)
        return margen if efectivo is None else min(margen, efectivo)

    def _destinos(self) -> list[_Destino]:
        """Kinds of recipient the donation can go to, from the data of the user's community."""
        d = self.p.donativos
        estatal = [
            "La entidad debe estar acogida a la Ley 49/2002 y certificar el donativo.",
            f"Hay que hacerlo efectivo antes del {_fecha(self.cierre)}: {_cita('art. 12 LIRPF')}.",
            f"La base de la deducción no puede superar el {d.limite_base_liquidable * 100:.0f} % de la base liquidable.",
        ]
        destinos = [
            _Destino(
                id=DESTINO_ESTATAL,
                etiqueta="una entidad acogida a la Ley 49/2002",
                requisitos=tuple(estatal),
                referencia=d.fuente.referencia,
                verificado=d.fuente.verificado,
            )
        ]
        anterior = self.r.donativo_recurrente_importe_anterior
        if anterior is not None:
            destinos.append(
                _Destino(
                    id=DESTINO_RECURRENTE,
                    etiqueta="la misma entidad a la que donaste los dos años anteriores",
                    requisitos=(
                        *estatal,
                        f"Donar a esa entidad al menos lo del año pasado ({_eur(anterior)}); lo que exceda de "
                        f"{_eur(d.tramo_primero)} deduce el {d.tipo_resto_recurrente * 100:.0f} %: "
                        f"{_cita('art. 19.1 Ley 49/2002')}.",
                        "Que en los dos años anteriores donaras a esa misma entidad y que lo del año pasado no fuera "
                        "menos que lo del anterior (lo indicas tú; no se ha comprobado).",
                    ),
                    referencia=d.fuente.referencia,
                    verificado=d.fuente.verificado,
                )
            )
        comunidad = self.p.comunidad(self.individuales[0].comunidad_autonoma)
        if comunidad is None:
            return destinos
        norma = f"{comunidad.fuente_principal.norma.split(',')[0]} ({comunidad.fuente_principal.boe_id})"
        por_campo: dict[str, list[DeduccionCalculable]] = {}
        for ded in comunidad.deducciones_calculables:
            for campo in ded.bases:
                if campo.startswith("donativos"):
                    por_campo.setdefault(campo, []).append(ded)
        for campo, deducciones in por_campo.items():
            aptas = [ded for ded in deducciones if not self._cruza_con_estatal(ded)]
            if not aptas:
                continue
            requisitos = [
                *estatal,
                "Que la entidad dé derecho además a "
                + " y ".join(f"«{ded.titulo}» ({ded.articulo} {norma})" for ded in aptas)
                + "; si no está acogida a la Ley 49/2002, solo habría deducción autonómica.",
                "Que la ley autonómica permita aplicar su deducción a la misma cantidad que la estatal: no lo excluye "
                "en los datos, pero no se ha comprobado.",
            ]
            for ded in aptas:
                requisitos += [f"{ded.articulo}: {r.rstrip('.')}." for r in ded.requisitos_no_comprobados]
                if ded.recodificada_el:
                    requisitos.append(f"{ded.articulo}: recodificada automáticamente, pendiente de revisión humana.")
            destinos.append(
                _Destino(
                    id=_PREFIJO_AUTONOMICO + campo,
                    etiqueta="una entidad que dé derecho además a "
                    + " y ".join(f"«{ded.titulo}» ({ded.articulo})" for ded in aptas),
                    requisitos=tuple(requisitos),
                    referencia=f"{d.fuente.referencia}; " + "; ".join(f"{ded.articulo} {norma}" for ded in aptas),
                    verificado=d.fuente.verificado,
                )
            )
        if comunidad.deducciones_madrid is not None:
            dm = comunidad.deducciones_madrid.donativos
            destinos.append(
                _Destino(
                    id=_PREFIJO_AUTONOMICO + "donativos_fundaciones_madrid",
                    etiqueta="una fundación inscrita en el Registro de Fundaciones de la Comunidad de Madrid",
                    requisitos=(
                        *estatal,
                        "La deducción madrileña solo vale para fundaciones inscritas en el Registro de Fundaciones de "
                        "la Comunidad de Madrid y clubes deportivos elementales o básicos; para la estatal, la entidad "
                        "debe estar además acogida a la Ley 49/2002.",
                        "Que la ley autonómica permita aplicar su deducción a la misma cantidad que la estatal: no se "
                        "ha comprobado.",
                    ),
                    referencia=f"{d.fuente.referencia}; {dm.fuente.referencia}",
                    verificado=d.fuente.verificado and dm.fuente.verificado,
                )
            )
        return destinos

    @staticmethod
    def _cruza_con_estatal(ded: DeduccionCalculable) -> bool:
        """The deduction's own notes say its base interacts with the state donation deduction."""
        return any("estatal" in r.lower() and "donativ" in r.lower() for r in ded.requisitos_no_comprobados)

    def descartadas_autonomicas(self) -> list[PalancaDescartada]:
        comunidad = self.p.comunidad(self.individuales[0].comunidad_autonoma)
        if comunidad is None:
            return []
        norma = f"{comunidad.fuente_principal.norma.split(',')[0]} ({comunidad.fuente_principal.boe_id})"
        return [
            PalancaDescartada(
                id=f"donativo_autonomico:{ded.articulo}",
                nombre=f"Donativo con deducción autonómica «{ded.titulo}»",
                motivo="Su base se cruza con la de la deducción estatal por las mismas cantidades (lo dicen sus "
                "requisitos) y la calculadora no hace ese cruce: no se propone como destino del donativo.",
                referencia=f"{ded.articulo} {norma}",
            )
            for ded in comunidad.deducciones_calculables
            if any(campo.startswith("donativos") for campo in ded.bases) and self._cruza_con_estatal(ded)
        ]

    def _donativo_disponible(self) -> bool:
        d = self.p.donativos
        nombre = "Donativo"
        if self.r.tope_donativos <= 0:
            self.no_disponibles.append(
                PalancaNoDisponible(
                    id="donativo",
                    nombre=nombre,
                    causa="restricciones",
                    detalle="No has indicado ningún donativo previsto ni un presupuesto para donar, y el optimizador "
                    "no propone gastar dinero. Como referencia, a una entidad acogida a la Ley 49/2002 los primeros "
                    f"{_eur(d.tramo_primero)} deducen el {d.tipo_primero * 100:.0f} % y el resto el "
                    f"{d.tipo_resto * 100:.0f} % ({d.tipo_resto_recurrente * 100:.0f} % si es recurrente), "
                    f"{d.fuente.referencia}.",
                )
            )
            return False
        if self.fase != "antes_del_cierre":
            self.no_disponibles.append(
                PalancaNoDisponible(
                    id="donativo",
                    nombre=nombre,
                    causa="fecha",
                    detalle=f"El ejercicio {self.ejercicio} terminó el {_fecha(self.cierre)}: un donativo hecho "
                    f"ahora deduce en {self.ejercicio + 1}.",
                )
            )
            return False
        if self.individuales[0].comunidad_autonoma is None:
            self.no_disponibles.append(
                PalancaNoDisponible(
                    id="donativo_autonomico",
                    nombre="Donativo con deducción autonómica",
                    causa="faltan_datos",
                    detalle="Sin tu comunidad autónoma no se puede saber qué donativos dan además deducción "
                    "autonómica.",
                    datos_que_faltan=["comunidad_autonoma"],
                )
            )
        return True

    def perdidas_disponibles(self) -> None:
        nombre = {0: "Realizar pérdidas latentes (tuyas)", 1: "Realizar pérdidas latentes de tu cónyuge"}
        for i in range(len(self.individuales)):
            lever_id = f"perdidas_{'contribuyente' if i == 0 else 'conyuge'}"
            if not self.max_perdida[i]:
                if i == 0:
                    self.no_disponibles.append(
                        PalancaNoDisponible(
                            id=lever_id,
                            nombre=nombre[i],
                            causa="faltan_datos",
                            detalle="No has indicado inversiones con pérdidas latentes. Si las tienes y este año has "
                            "obtenido ganancias o rendimientos del capital, venderlas antes del 31 de diciembre "
                            f"puede compensarlos ({self.p.compensacion_ahorro.fuente.referencia if self.p.compensacion_ahorro else 'art. 49 LIRPF'}).",
                            datos_que_faltan=["perdidas_latentes"],
                        )
                    )
                continue
            if self.fase != "antes_del_cierre":
                self.no_disponibles.append(
                    PalancaNoDisponible(
                        id=lever_id,
                        nombre=nombre[i],
                        causa="fecha",
                        detalle=f"Una venta hecha ahora se imputa a {self.ejercicio + 1}: "
                        f"{_cita('art. 14.1.c) LIRPF')}",
                    )
                )
                self.max_perdida[i] = ZERO
                continue
            if self.p.compensacion_ahorro is None:
                self.no_disponibles.append(
                    PalancaNoDisponible(
                        id=lever_id,
                        nombre=nombre[i],
                        causa="no_calculable",
                        detalle="Los parámetros del ejercicio no incluyen la compensación del art. 49 LIRPF.",
                    )
                )
                self.max_perdida[i] = ZERO

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def _calcular(self, clave: tuple[Any, ...], construir: Callable[[], tuple[IRPFInput, IRPFParams]]) -> IRPFResult:
        res = self._cache.get(clave)
        if res is None:
            if self._tope is not None and self.evaluaciones >= self._tope:
                raise _PresupuestoAgotadoError
            inp, params = construir()
            self.evaluaciones += 1
            res = calcular_irpf(inp, params)
            self._cache[clave] = res
            self._entradas[clave] = inp
        return res

    def _bi(self, i: int, perdida: Decimal) -> Decimal:
        """Base imponible (general + savings) of spouse ``i`` after realising ``perdida``; no other lever moves it."""
        clave = (i, perdida)
        if clave not in self._bi_cache:
            base = self.individuales[i]
            res = self._calcular(
                ("BI", i, perdida),
                _con_cambios(base, {"ganancias_patrimoniales": base.ganancias_patrimoniales - perdida}, self.p),
            )
            self._bi_cache[clave] = res.base_imponible_general + res.base_imponible_ahorro
        return self._bi_cache[clave]

    def evaluar(self, punto: _Punto) -> _Evaluacion:
        hecha = self._evaluaciones_punto.get(punto)
        if hecha is None:
            hecha = self._evaluaciones_punto[punto] = self._evaluar(punto)
        return hecha

    def _evaluar(self, punto: _Punto) -> _Evaluacion:
        declaraciones: list[tuple[str, IRPFInput, IRPFResult]] = []
        don = punto.donativo
        if punto.conjunta:
            assert self._j0 is not None
            j0 = self._j0
            perdida = punto.perdida[0] + punto.perdida[1]
            cambios: dict[str, Any] = {
                "ganancias_patrimoniales": j0.ganancias_patrimoniales - perdida,
                "aportacion_plan_pensiones": j0.aportacion_plan_pensiones + sum(punto.pension, ZERO),
            }
            if j0.prevision_social_por_miembro:
                # Each spouse's own contribution (the user is the first member): the calculator applies each
                # member's limits (art. 84.2.1.º LIRPF).
                miembros = list(j0.prevision_social_por_miembro)
                for i, extra in enumerate(punto.pension[: len(miembros)]):
                    miembros[i] = miembros[i].model_copy(
                        update={"aportacion_plan_pensiones": miembros[i].aportacion_plan_pensiones + extra}
                    )
                cambios["prevision_social_por_miembro"] = miembros
            don_clave = (don[0], 0, don[2]) if don is not None else None
            if don is not None:
                cambios |= _sumar_donativo(j0, don[0], don[2])
            clave: tuple[Any, ...] = ("J", punto.pension, perdida, don_clave)
            res = self._calcular(clave, _con_cambios(j0, cambios, self.p))
            declaraciones.append(("conjunta", self._entradas[clave], res))
        else:
            for i, base in enumerate(self.individuales):
                propio = don if don is not None and don[1] == i else None
                resto = (
                    self._bi(1 - i, punto.perdida[1 - i] if self._resto_importa else ZERO) if self.con_conyuge else None
                )
                cambios_i: dict[str, Any] = {
                    "aportacion_plan_pensiones": base.aportacion_plan_pensiones + punto.pension[i],
                    "ganancias_patrimoniales": base.ganancias_patrimoniales - punto.perdida[i],
                }
                if resto is not None:
                    cambios_i["base_imponible_resto_unidad_familiar"] = resto
                if propio is not None:
                    cambios_i |= _sumar_donativo(base, propio[0], propio[2])
                clave = ("I", i, punto.pension[i], punto.perdida[i], propio, resto)
                res = self._calcular(clave, _con_cambios(base, cambios_i, self.p))
                declaraciones.append(("contribuyente" if i == 0 else "conyuge", self._entradas[clave], res))
        total = sum((res.resultado for _, _, res in declaraciones), ZERO)
        return _Evaluacion(total=total, declaraciones=tuple(declaraciones))

    def compromiso(self, punto: _Punto) -> Decimal:
        extra = punto.donativo[2] - self.r.donativo_previsto if punto.donativo is not None else ZERO
        return sum(punto.pension, ZERO) + sum(punto.perdida, ZERO) + max(ZERO, extra)

    def clave(self, punto: _Punto) -> tuple[Any, ...]:
        """Lexicographic order: least tax, then least committed (to the euro), then round amounts (a cent less
        with the same rounded tax is noise), then the current mode, then smaller values."""
        importes = [*punto.pension, *punto.perdida, *([punto.donativo[2]] if punto.donativo else [])]
        return (
            self.evaluar(punto).total,
            self.compromiso(punto).quantize(Decimal(1)),
            sum(1 for v in importes if v != v.to_integral_value()),
            0 if punto.conjunta == self.base_conjunta else 1,
            punto.orden(),
        )

    # ------------------------------------------------------------------ #
    # Coordinates and their candidate values
    # ------------------------------------------------------------------ #

    def coordenadas(self, conjunta: bool, *, busqueda: bool) -> list[_Coordenada]:
        """The levers of a filing mode. In a joint return both spouses' amounts act on the same
        base, so the search moves their total (allocated to the user first, then the spouse)."""
        coords: list[_Coordenada] = []
        pares = conjunta and self.con_conyuge and busqueda
        if pares:
            if sum(self.max_pension, ZERO) > 0:
                coords.append(_Coordenada("pension_total", "pension", None, ZERO, sum(self.max_pension, ZERO)))
            if sum(self.max_perdida, ZERO) > 0:
                coords.append(_Coordenada("perdida_total", "perdida", None, ZERO, sum(self.max_perdida, ZERO)))
        else:
            for i in range(len(self.individuales)):
                if self.max_pension[i] > 0:
                    coords.append(_Coordenada(f"pension_{i}", "pension", i, ZERO, self.max_pension[i]))
            for i in range(len(self.individuales)):
                if self.max_perdida[i] > 0:
                    coords.append(_Coordenada(f"perdida_{i}", "perdida", i, ZERO, self.max_perdida[i]))
        if self._lever_donativo:
            coords.append(_Coordenada("donativo", "donativo", None, self.r.donativo_previsto, self.r.tope_donativos))
        return coords

    def valor(self, punto: _Punto, c: _Coordenada) -> Any:
        if c.tipo == "donativo":
            return punto.donativo
        valores = punto.pension if c.tipo == "pension" else punto.perdida
        return sum(valores, ZERO) if c.titular is None else valores[c.titular]

    def con(self, punto: _Punto, c: _Coordenada, valor: Any) -> _Punto:
        if c.tipo == "donativo":
            return replace(punto, donativo=valor)
        maximos = self.max_pension if c.tipo == "pension" else self.max_perdida
        if c.titular is None:
            primero = min(valor, maximos[0])
            nuevos = (primero, valor - primero)
        else:
            actuales = punto.pension if c.tipo == "pension" else punto.perdida
            nuevos = (valor, actuales[1]) if c.titular == 0 else (actuales[0], valor)
        return replace(punto, pension=nuevos) if c.tipo == "pension" else replace(punto, perdida=nuevos)

    def _declaracion(self, ev: _Evaluacion, etiqueta: str) -> tuple[IRPFInput, IRPFResult] | None:
        for nombre, inp, res in ev.declaraciones:
            if nombre == etiqueta:
                return inp, res
        return None

    def candidatos(self, punto: _Punto, c: _Coordenada) -> list[Any]:
        if c.tipo == "donativo":
            return self._candidatos_donativo(punto)
        ev = self.evaluar(punto)
        actual: Decimal = self.valor(punto, c)
        valores: set[Decimal] = {c.minimo, c.maximo, actual}
        # (return, lever kind: True pension, False loss, None the other spouse's loss through the family base)
        contextos: list[tuple[str, bool | None]] = []
        if punto.conjunta:
            contextos.append(("conjunta", c.tipo == "pension"))
        elif c.titular is not None:
            propio = "contribuyente" if c.titular == 0 else "conyuge"
            contextos.append((propio, c.tipo == "pension"))
            if c.tipo == "perdida" and self.con_conyuge:
                contextos.append(("conyuge" if c.titular == 0 else "contribuyente", None))
        for etiqueta, es_pension in contextos:
            decl = self._declaracion(ev, etiqueta)
            if decl is None:
                continue
            inp, res = decl
            mueve = _MUEVE_PENSION if es_pension else _MUEVE_PERDIDA if es_pension is False else _MUEVE_RESTO
            base = _valores_base(inp, res, self.p)
            for nombre_base, umbral in _umbrales(inp, res, self.p):
                if nombre_base in mueve:
                    valores |= _alrededor(actual + base[nombre_base] - umbral)
            if es_pension is False:
                # Where the realised loss stops offsetting gains, where the earlier years' losses would
                # have taken over, and where the 25 % cross-offset against capital income is used up.
                ganancia = inp.ganancias_patrimoniales
                valores |= _alrededor(actual + ganancia)
                valores |= _alrededor(actual + ganancia - inp.perdidas_patrimoniales_pendientes)
                if self.p.compensacion_ahorro is not None:
                    cupo = self.p.compensacion_ahorro.limite_cruzado * max(ZERO, inp.rendimiento_capital_mobiliario)
                    valores |= _alrededor(actual + ganancia + cupo)
        return sorted(v for v in valores if c.minimo <= v <= c.maximo)

    def _donantes(self, conjunta: bool) -> list[int]:
        return [0, 1] if self.con_conyuge and not conjunta else [0]

    def _candidatos_donativo(self, punto: _Punto) -> list[Donativo]:
        ev = self.evaluar(punto)
        d = self.p.donativos
        minimo, maximo = self.r.donativo_previsto, self.r.tope_donativos
        comunidad = self.p.comunidad(self.individuales[0].comunidad_autonoma)
        out: set[Donativo] = set()
        for donante in self._donantes(punto.conjunta):
            if punto.conjunta:
                etiqueta = "conjunta"
                base_inp = self._j0
            else:
                etiqueta = "contribuyente" if donante == 0 else "conyuge"
                base_inp = self.individuales[donante]
            decl = self._declaracion(ev, etiqueta)
            assert decl is not None and base_inp is not None
            inp, res = decl
            bases = _valores_base(inp, res, self.p)
            for destino in self.destinos:
                if destino.id == DESTINO_RECURRENTE and (base_inp.donativos and not base_inp.donativos_recurrentes):
                    # The calculator applies the recurring rate to the whole donation base of the return.
                    continue
                importes: set[Decimal] = {minimo, maximo}
                if maximo > minimo:
                    puntos = [
                        d.tramo_primero - base_inp.donativos,
                        d.limite_base_liquidable * bases["base_liquidable_total"] - base_inp.donativos,
                    ]
                    if destino.id.startswith(_PREFIJO_AUTONOMICO) and comunidad is not None:
                        campo = destino.id.removeprefix(_PREFIJO_AUTONOMICO)
                        for ded in comunidad.deducciones_calculables:
                            if campo not in ded.bases:
                                continue
                            previo = sum((Decimal(getattr(base_inp, b) or 0) for b in ded.bases), ZERO)
                            fuentes: list[DeduccionCalculable | Variante] = [ded, *ded.variantes]
                            for fuente in fuentes:
                                pct = fuente.porcentaje if fuente.porcentaje is not None else ded.porcentaje
                                tope = fuente.maximo if fuente.maximo is not None else ded.maximo
                                if pct and tope is not None:
                                    puntos.append(tope / pct - previo)
                                for tramo in fuente.tramos_porcentaje or []:
                                    if tramo.hasta is not None:
                                        puntos.append(tramo.hasta - previo)
                                if fuente.base_maxima is not None:
                                    puntos.append(fuente.base_maxima - previo)
                            if ded.base_maxima_fraccion is not None:
                                puntos.append(ded.base_maxima_fraccion * bases[ded.base_maxima_fraccion_de] - previo)
                        if campo == "donativos_fundaciones_madrid" and comunidad.deducciones_madrid is not None:
                            dm = comunidad.deducciones_madrid.donativos
                            puntos.append(
                                dm.limite_base_liquidable * bases["base_liquidable_total"]
                                - base_inp.donativos_fundaciones_madrid
                            )
                    for valor in puntos:
                        importes |= _alrededor(valor)
                if destino.id == DESTINO_RECURRENTE:
                    anterior = self.r.donativo_recurrente_importe_anterior or ZERO
                    importes = {v for v in importes | {anterior} if v >= anterior}
                out |= {(destino.id, donante, v) for v in importes if minimo <= v <= maximo and v > 0}
        return sorted(out)

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def candidatos_de(self, punto: _Punto, c: _Coordenada) -> list[Any]:
        """Candidate values of a coordinate at ``punto`` (the donation may also be "none" if nothing is due)."""
        if c.tipo != "donativo":
            return self.candidatos(punto, c)
        opciones: list[Any] = [None] if c.minimo <= 0 else []
        return opciones + list(self._candidatos_donativo(punto))

    def _mejor(self, puntos: Iterable[_Punto]) -> _Punto:
        return min(puntos, key=self.clave)

    def _ascenso(self, inicio: _Punto, coords: list[_Coordenada], *, max_pasadas: int = 10) -> _Punto:
        """Coordinate ascent: each lever in turn to its best value given the others, until nothing moves."""
        actual = inicio
        for _ in range(max_pasadas):
            previo = actual
            for c in coords:
                actual = self._mejor([actual, *(self.con(actual, c, v) for v in self.candidatos_de(actual, c))])
            if actual == previo:
                break
        return actual

    def _inicio(self, conjunta: bool) -> _Punto:
        don: Donativo | None = None
        if self._lever_donativo and self.r.donativo_previsto > 0:
            # They will donate anyway: start from the plain choice (a Ley 49/2002 entity, the user pays).
            don = (DESTINO_ESTATAL, 0, self.r.donativo_previsto)
        return _Punto(conjunta=conjunta, donativo=don)

    def buscar(self) -> _Punto:
        mejores: list[_Punto] = []
        for n, conjunta in enumerate(self.modos):
            restantes = len(self.modos) - n
            limite = self.evaluaciones + max(1, (self.max_evaluaciones - self.evaluaciones) // restantes)
            coords = self.coordenadas(conjunta, busqueda=True)
            mejores.append(self._buscar_modo(self._inicio(conjunta), coords, limite=limite))
        self._tope = None
        return self._mejor(mejores)

    def _buscar_modo(self, inicio: _Punto, coords: list[_Coordenada], *, limite: int) -> _Punto:
        self._tope = limite
        modo = "conjunta" if inicio.conjunta else "individual"
        try:
            self.evaluar(inicio)
            rejilla = [self.candidatos_de(inicio, c) for c in coords]
        except _PresupuestoAgotadoError:
            self.estado.metodo = "heuristica"
            return inicio
        for c, valores in zip(coords, rejilla, strict=True):
            self.estado.candidatos[f"{modo}:{c.nombre}"] = len(valores)
        mejor = inicio
        completa = False
        try:
            exhaustivo = self._exhaustiva(inicio, coords, rejilla)
            if exhaustivo is not None:
                mejor, completa = exhaustivo, True
        except _PresupuestoAgotadoError:
            pass
        if not completa:
            # Coordinate ascent restarted from every single-lever optimum and from every lever at its maximum.
            self.estado.metodo = "heuristica"
            try:
                arranques = [inicio, mejor]
                todo = inicio
                for c, valores in zip(coords, rejilla, strict=True):
                    if valores:
                        arranques.append(self._mejor([self.con(inicio, c, v) for v in valores]))
                        todo = self.con(todo, c, valores[-1])
                arranques.append(todo)
                for arranque in _unicos_puntos(arranques):
                    candidato = self._ascenso(arranque, coords)
                    if self.clave(candidato) < self.clave(mejor):
                        mejor = candidato
            except _PresupuestoAgotadoError:
                pass
        try:
            # Polish: breakpoints recomputed at the incumbent catch thresholds the initial grid could not see
            # (e.g. the second spouse's amount that completes a total).
            mejor = self._ascenso(mejor, coords)
        except _PresupuestoAgotadoError:
            pass
        return mejor

    def _exhaustiva(self, inicio: _Punto, coords: list[_Coordenada], rejilla: list[list[Any]]) -> _Punto | None:
        """The best point of the grid, or None when the grid is too large to walk.

        Separate returns of a couple are searched side by side: once the levers that couple them are
        fixed (the donation, whose donor is a choice, and the losses when a family-unit income ceiling
        makes one spouse's base matter to the other), the household's tax is the sum of two returns
        that depend on disjoint levers, so each side's best is found on its own. Exact, and the grid
        grows as a sum instead of a product.
        """
        nombres = [c.nombre for c in coords]
        if not inicio.conjunta and self.con_conyuge:
            acoplados = [n for n in nombres if n == "donativo" or (n.startswith("perdida_") and self._resto_importa)]
            lados = [[n for n in nombres if n not in acoplados and n.endswith(f"_{i}")] for i in (0, 1)]
        else:
            acoplados, lados = nombres, [[], []]
        por_nombre = {c.nombre: (c, valores) for c, valores in zip(coords, rejilla, strict=True)}

        def producto(grupo: list[str]) -> int:
            total = 1
            for nombre in grupo:
                total *= max(1, len(por_nombre[nombre][1]))
            return total

        tamano = (
            producto(acoplados) * sum(producto(lado) for lado in lados if lado) if any(lados) else producto(acoplados)
        )
        if tamano > MAX_COMBINACIONES:
            return None

        def aplicar(punto: _Punto, grupo: list[str], valores: tuple[Any, ...]) -> _Punto:
            for nombre, valor in zip(grupo, valores, strict=True):
                punto = self.con(punto, por_nombre[nombre][0], valor)
            return punto

        def opciones(grupo: list[str]) -> Iterator[tuple[Any, ...]]:
            return itertools.product(*(por_nombre[n][1] or [self.valor(inicio, por_nombre[n][0])] for n in grupo))

        mejor, mejor_clave = inicio, self.clave(inicio)
        for combinacion in opciones(acoplados):
            punto = aplicar(inicio, acoplados, combinacion)
            for i, lado in enumerate(lados):
                if not lado:
                    continue
                etiqueta = "contribuyente" if i == 0 else "conyuge"
                mejor_lado: tuple[Any, ...] | None = None
                elegido = punto
                for valores in opciones(lado):
                    candidato = aplicar(punto, lado, valores)
                    self.estado.combinaciones += 1
                    decl = self._declaracion(self.evaluar(candidato), etiqueta)
                    assert decl is not None
                    clave_lado = (
                        decl[1].resultado,
                        sum(valores, ZERO).quantize(Decimal(1)),
                        sum(1 for v in valores if v != v.to_integral_value()),
                        valores,
                    )
                    if mejor_lado is None or clave_lado < mejor_lado:
                        mejor_lado, elegido = clave_lado, candidato
                punto = elegido
            if not any(lados):
                self.estado.combinaciones += 1
            clave = self.clave(punto)
            if clave < mejor_clave:
                mejor, mejor_clave = punto, clave
        return mejor

    def recortar(self, punto: _Punto) -> _Punto:
        """Lower each amount to the smallest one that keeps the same total tax (to the cent)."""
        objetivo = self.evaluar(punto).total
        for c in reversed(self.coordenadas(punto.conjunta, busqueda=False)):
            punto = self._recortar_una(punto, c, objetivo)
        return punto

    def _recortar_una(self, punto: _Punto, c: _Coordenada, objetivo: Decimal) -> _Punto:
        if c.tipo == "donativo":
            don = punto.donativo
            if don is None or don[2] <= c.minimo:
                return punto
            actual = don[2]

            def poner(v: Decimal) -> _Punto:
                return replace(punto, donativo=(don[0], don[1], v) if v > 0 else None)

        else:
            actual = self.valor(punto, c)
            if actual <= c.minimo:
                return punto

            def poner(v: Decimal) -> _Punto:
                return self.con(punto, c, v)

        if self.evaluar(poner(c.minimo)).total == objetivo:
            return poner(c.minimo)
        bajo, alto = c.minimo, actual
        while alto - bajo > CENT:
            medio = max(_abajo((bajo + alto) / 2), bajo + CENT)
            if self.evaluar(poner(medio)).total == objetivo:
                alto = medio
            else:
                bajo = medio
        # A few cents less with the same rounded tax is noise, not a saving of commitment.
        return poner(alto) if actual - alto >= _RECORTE_MINIMO else punto

    # ------------------------------------------------------------------ #
    # The plan
    # ------------------------------------------------------------------ #

    def _hogar(self, punto: _Punto) -> ResultadoHogar:
        ev = self.evaluar(punto)
        if not punto.conjunta:
            tributacion: Literal["individual", "conjunta_biparental", "conjunta_monoparental"] = "individual"
        elif self.con_conyuge or self.conjunta_sin_desglose:
            tributacion = "conjunta_biparental"
        else:
            tributacion = "conjunta_monoparental"
        return ResultadoHogar(
            tributacion=tributacion,
            declaraciones=[Declaracion(titular=etiqueta, resultado=res) for etiqueta, _, res in ev.declaraciones],  # type: ignore[arg-type]
            resultado_total=ev.total,
            cuota_liquida_total=sum((res.cuota_liquida for _, _, res in ev.declaraciones), ZERO),
        )

    def _palancas(self) -> list[tuple[str, str, int | None]]:
        """(id, kind, spouse) of every lever the plan reports on."""
        out: list[tuple[str, str, int | None]] = []
        if len(self.modos) > 1:
            out.append(("tributacion", "modo", None))
        quien = ("contribuyente", "conyuge")
        out += [
            (f"plan_pensiones_{quien[i]}", "pension", i)
            for i in range(len(self.individuales))
            if self.max_pension[i] > 0
        ]
        out += [
            (f"perdidas_{quien[i]}", "perdida", i) for i in range(len(self.individuales)) if self.max_perdida[i] > 0
        ]
        if self._lever_donativo:
            out.append(("donativo", "donativo", None))
        return out

    def _usada(self, tipo: str, i: int | None, base: _Punto, punto: _Punto) -> bool:
        if tipo == "modo":
            return punto.conjunta != base.conjunta
        if tipo == "pension":
            assert i is not None
            return punto.pension[i] > 0
        if tipo == "perdida":
            assert i is not None
            return punto.perdida[i] > 0
        return punto.donativo is not None

    def _sin(self, tipo: str, i: int | None, base: _Punto, punto: _Punto) -> _Punto:
        """``punto`` without this lever (back to the current situation for it)."""
        if tipo == "modo":
            return replace(punto, conjunta=base.conjunta)
        if tipo == "donativo":
            return replace(punto, donativo=None)
        assert i is not None
        c = _Coordenada(f"{tipo}_{i}", tipo, i)  # type: ignore[arg-type]
        return self.con(punto, c, ZERO)

    def _solo(self, tipo: str, i: int | None, base: _Punto) -> _Punto:
        """The best this lever does on its own, from the current situation."""
        if tipo == "modo":
            return replace(base, conjunta=not base.conjunta)
        if tipo == "donativo":
            opciones = self._candidatos_donativo(base)
            return self._mejor([replace(base, donativo=v) for v in opciones]) if opciones else base
        assert i is not None
        maximo = self.max_pension[i] if tipo == "pension" else self.max_perdida[i]
        c = _Coordenada(f"{tipo}_{i}", tipo, i, ZERO, maximo)  # type: ignore[arg-type]
        return self._mejor([base, *(self.con(base, c, v) for v in self.candidatos(base, c))])

    def _accion(self, tipo: str, i: int | None, punto: _Punto) -> str:
        cierre = _fecha(self.cierre)
        if tipo == "modo":
            if not punto.conjunta:
                return "Presentar dos declaraciones individuales"
            return "Presentar declaración conjunta" + ("" if self.con_conyuge else " (monoparental)")
        if tipo == "pension":
            assert i is not None
            quien = "a tu plan de pensiones" if i == 0 else "al plan de pensiones de tu cónyuge"
            return f"Aportar {_eur(punto.pension[i])} {quien} antes del {cierre}"
        if tipo == "perdida":
            assert i is not None
            return (
                f"Vender antes del {cierre} inversiones con {_eur(punto.perdida[i])} de pérdida latente: "
                + "; ".join(self._desglose_perdidas(i, punto.perdida[i]))
            )
        don = punto.donativo
        if don is None:
            return "No donar"
        destino = next(d for d in self.destinos if d.id == don[0])
        quien = " (lo paga tu cónyuge y se lo deduce en su declaración)" if don[1] == 1 else ""
        return f"Donar {_eur(don[2])} a {destino.etiqueta} antes del {cierre}{quien}"

    def _desglose_perdidas(self, i: int, importe: Decimal) -> list[str]:
        """Which positions to sell, in the order the user gave them."""
        partes: list[str] = []
        queda = importe
        for pos in self.r.perdidas_latentes:
            if self._indice(pos.titular) != i or queda <= 0:
                continue
            usar = min(queda, pos.importe)
            nombre = (
                pos.descripcion
                or {
                    "valores_cotizados": "valores cotizados",
                    "valores_no_cotizados": "valores no cotizados",
                    "otro_elemento": "otro elemento patrimonial",
                }[pos.tipo]
            )
            parcial = (
                " (venta parcial: la pérdida que realiza depende de qué títulos vendas)" if usar < pos.importe else ""
            )
            partes.append(f"{nombre}, {_eur(usar)} de pérdida{parcial}")
            queda -= usar
        return partes

    def _requisitos(self, tipo: str, i: int | None, punto: _Punto) -> tuple[list[str], str, bool]:
        """Requirements, legal reference and verification flag of a lever."""
        cierre = _fecha(self.cierre)
        if tipo == "modo":
            tc = self.p.tributacion_conjunta
            reqs = [_cita("art. 83.1 LIRPF"), _cita("art. 83.2 LIRPF")]
            if self.con_conyuge:
                reqs += [
                    "En la conjunta deben incluirse las rentas de todos los miembros de la unidad familiar; aquí se "
                    "han sumado las de los dos cónyuges (las de los hijos no constan y se toman como cero).",
                    _cita("art. 84.2.1.º LIRPF"),
                ]
            else:
                reqs += [
                    "Unidad familiar formada por el padre o la madre y todos los hijos que convivan con él o ella "
                    f"({_cita('art. 82.1.2.ª LIRPF')}); la reducción monoparental no se aplica si convives con el "
                    "otro progenitor. No se ha comprobado.",
                ]
            return reqs, tc.fuente.referencia, tc.fuente.verificado
        if tipo == "pension":
            assert i is not None
            ps = self.p.prevision_social
            reqs = [
                f"Hacer la aportación antes del {cierre}: {_cita('art. 12 LIRPF')}",
                f"Reduce como máximo {_eur(ps.limite_general)} al año y no más del "
                f"{ps.porcentaje_rendimientos * 100:.0f} % de los rendimientos netos del trabajo y de actividades "
                f"económicas ({ps.fuente.referencia}): en este caso, {_eur(self.util[i])} en total"
                + (f", de los que ya constan {_eur(self.a0[i])}" if self.a0[i] else "")
                + ".",
                "El límite es conjunto para todas las aportaciones a sistemas de previsión social del art. 51 LIRPF "
                "(planes de pensiones, mutualidades, planes de previsión asegurados…).",
                "El dinero queda inmovilizado hasta la jubilación y los demás supuestos tasados de rescate.",
                "Al rescatarlo tributa como rendimiento del trabajo: " + _cita("art. 17.2.a).3.ª LIRPF") + ".",
            ]
            referencia = ps.fuente.referencia
            if punto.conjunta and self.con_conyuge:
                reqs.append(_cita("art. 84.2.1.º LIRPF"))
                referencia += "; art. 84.2.1.º LIRPF"
            return reqs, referencia, ps.fuente.verificado
        if tipo == "perdida":
            assert i is not None
            ca = self.p.compensacion_ahorro
            assert ca is not None
            tipos = {pos.tipo for pos in self.r.perdidas_latentes if self._indice(pos.titular) == i}
            reqs = [f"Vender antes del {cierre}: {_cita('art. 14.1.c) LIRPF')}"]
            if "valores_cotizados" in tipos:
                reqs.append(
                    "Valores cotizados: no compres valores homogéneos en los dos meses anteriores ni posteriores a la "
                    f"venta, o la pérdida no se computa todavía: {_cita('art. 33.5.f) LIRPF')} "
                    f"{_cita('art. 33.5, último párrafo, LIRPF')}"
                )
            if "valores_no_cotizados" in tipos:
                reqs.append(
                    "Valores no cotizados: no compres valores homogéneos en el año anterior ni en el posterior a la "
                    f"venta: {_cita('art. 33.5.g) LIRPF')}"
                )
            if "otro_elemento" in tipos:
                reqs.append(
                    f"Otros elementos: no vuelvas a comprarlo en el año siguiente: {_cita('art. 33.5.e) LIRPF')}"
                )
            reqs += [
                f"Las pérdidas se compensan primero con las ganancias del año y, con el límite del "
                f"{ca.limite_cruzado * 100:.0f} %, con los rendimientos del capital ({ca.fuente.referencia})."
                + (f" {ca.fuente.nota}" if ca.fuente.nota else ""),
                "Solo pérdidas patrimoniales (acciones, fondos, ETF…): las de bonos y otros activos financieros son "
                "rendimientos del capital mobiliario y no se tratan aquí.",
                "La pérdida es la diferencia entre el valor de venta y el de compra, gastos incluidos; el importe lo "
                "indicas tú y no se ha comprobado.",
            ]
            return reqs, ca.fuente.referencia, ca.fuente.verificado
        don = punto.donativo
        assert don is not None
        destino = next(d for d in self.destinos if d.id == don[0])
        return list(destino.requisitos), destino.referencia, destino.verificado

    def _analisis_interaccion(
        self, base: _Punto, optimo: _Punto, solos: list[_Punto], interaccion: Decimal
    ) -> list[str]:
        ev_base, ev_opt = self.evaluar(base), self.evaluar(optimo)
        lin_base = _agregar_lineas(r for _, _, r in ev_base.declaraciones)
        lin_opt = _agregar_lineas(r for _, _, r in ev_opt.declaraciones)
        lin_solos = [_agregar_lineas(r for _, _, r in self.evaluar(s).declaraciones) for s in solos]
        conceptos = _unicos([*lin_opt, *lin_base, *(c for lineas in lin_solos for c in lineas)])
        mensajes: list[str] = []
        for concepto in conceptos:
            if not concepto.startswith(("Deducción", "Reducción", "Compensación")):
                continue
            antes = lin_base.get(concepto, (ZERO, None))[0]
            despues = lin_opt.get(concepto, (ZERO, None))[0]
            por_separado = sum((lineas.get(concepto, (ZERO, None))[0] - antes for lineas in lin_solos), ZERO)
            if abs((despues - antes) - por_separado) < CENT:
                continue
            if not antes and despues and not any(lineas.get(concepto, (ZERO, None))[0] for lineas in lin_solos):
                mensajes.append(
                    f"«{concepto}» ({_eur(-despues)}) solo se aplica con la combinación: ninguna palanca por "
                    "separado la activa."
                )
            else:
                mensajes.append(
                    f"«{concepto}»: {_eur(-(despues - antes))} con el plan combinado frente a {_eur(-por_separado)} "
                    "sumando las palancas por separado."
                )
        avisos_opt = set(r_aviso for _, _, r in ev_opt.declaraciones for r_aviso in r.avisos)
        avisos_solos = [set(a for _, _, r in self.evaluar(s).declaraciones for a in r.avisos) for s in solos]
        for _, _, res in ev_base.declaraciones:
            for aviso in res.avisos:
                umbral = any(marca in aviso for marca in ("supera", "se extingue", "no aplicada", "no se aplica"))
                if umbral and aviso not in avisos_opt and all(aviso in avisos for avisos in avisos_solos):
                    mensajes.append(
                        f"Con el plan combinado deja de ocurrir lo que advierte el cálculo actual: «{aviso}»"
                    )
        if abs(interaccion) >= CENT and not mensajes:
            mensajes.append(
                "La diferencia viene de la escala de gravamen: cada euro de reducción ahorra al tipo marginal que "
                "queda después de aplicar las demás palancas."
                if interaccion < 0
                else "La diferencia viene de la escala de gravamen y de los mínimos: las palancas juntas llevan la "
                "base a un tramo que ninguna alcanza por separado."
            )
        return _unicos(mensajes)

    def _oportunidades(self, optimo: _Punto) -> list[str]:
        """Realising latent gains that losses would absorb at no cost this year (information, not a saving)."""
        if self.fase != "antes_del_cierre" or not self.r.ganancias_latentes:
            return []
        ev = self.evaluar(optimo)
        out: list[str] = []
        grupos = [[0, 1]] if optimo.conjunta and self.con_conyuge else [[i] for i in range(len(self.individuales))]
        for grupo in grupos:
            ganancias = sum(
                (pos.importe for pos in self.r.ganancias_latentes if self._indice(pos.titular) in grupo), ZERO
            )
            if not ganancias:
                continue
            etiqueta = "conjunta" if optimo.conjunta else ("contribuyente" if grupo[0] == 0 else "conyuge")
            decl = self._declaracion(ev, etiqueta)
            if decl is None:
                continue
            pendientes = decl[1].perdidas_patrimoniales_a_compensar
            importe = min(ganancias, pendientes)
            quien = "" if grupo != [1] else " de tu cónyuge"
            if importe <= 0:
                out.append(
                    f"Realizar las plusvalías latentes{quien} ({_eur(ganancias)}) este año aumentaría el impuesto: "
                    "no hay pérdidas pendientes que las absorban."
                )
                continue
            perdida = list(optimo.perdida)
            perdida[grupo[0]] -= importe
            prueba = replace(optimo, perdida=(perdida[0], perdida[1]))
            coste = self.evaluar(prueba).total - ev.total
            ca = self.p.compensacion_ahorro
            referencia = ca.fuente.referencia if ca is not None else "art. 49 LIRPF"
            if coste <= 0:
                out.append(
                    f"Puedes vender antes del {_fecha(self.cierre)} hasta {_eur(importe)} de plusvalías latentes{quien} "
                    f"sin pagar más este año: las absorben pérdidas que, si no, quedarían pendientes ({referencia}). "
                    "No es un ahorro de este año: solo sube el valor de compra de lo que vuelvas a comprar, y la "
                    "ganancia futura será menor."
                )
            else:
                out.append(
                    f"Vender {_eur(importe)} de plusvalías latentes{quien} costaría {_eur(coste)} más este año, aunque "
                    f"haya pérdidas pendientes ({referencia})."
                )
        return out

    def ejecutar(self) -> PlanOptimo:
        base = _Punto(conjunta=self.base_conjunta)
        ev_base = self.evaluar(base)
        optimo = self.buscar()
        self._tope = None
        if self.clave(base) < self.clave(optimo) and self._factible(base):
            optimo = base
        optimo = self.recortar(optimo)
        ev_opt = self.evaluar(optimo)

        palancas: list[PalancaElegida] = []
        solos: list[_Punto] = []
        naturalezas: dict[str, Naturaleza] = {
            "modo": "ahorro_definitivo",
            "pension": "diferimiento",
            "perdida": "diferimiento",
            "donativo": "requiere_gastar",
        }
        titulares = ("contribuyente", "conyuge")
        nombres = {
            "modo": "Tributación conjunta o individual",
            "pension": ("Aportación a tu plan de pensiones", "Aportación al plan de pensiones de tu cónyuge"),
            "perdida": ("Venta de inversiones con pérdidas", "Venta de inversiones con pérdidas de tu cónyuge"),
            "donativo": "Donativo",
        }
        for lever_id, tipo, i in self._palancas():
            if not self._usada(tipo, i, base, optimo):
                if tipo in ("pension", "perdida") and i is not None:
                    self.no_disponibles.append(
                        PalancaNoDisponible(
                            id=lever_id,
                            nombre=nombres[tipo][i],
                            causa="sin_margen",
                            detalle="Con tus datos no reduce el impuesto de este año en ninguna combinación.",
                        )
                    )
                continue
            solo = self._solo(tipo, i, base)
            solos.append(solo)
            marginal = self.evaluar(self._sin(tipo, i, base, optimo)).total - ev_opt.total
            aislado = ev_base.total - self.evaluar(solo).total
            if tipo == "modo":
                importe, compromiso = None, ZERO
            elif tipo == "donativo":
                assert optimo.donativo is not None
                importe = compromiso = optimo.donativo[2]
            else:
                assert i is not None
                importe = compromiso = (optimo.pension if tipo == "pension" else optimo.perdida)[i]
            por_euro = (marginal / compromiso).quantize(Decimal("0.0001")) if compromiso else None
            requisitos, referencia, verificado = self._requisitos(tipo, i, optimo)
            nombre_t = nombres[tipo]
            nombre = nombre_t if isinstance(nombre_t, str) else nombre_t[i or 0]
            accion = self._accion(tipo, i, optimo)
            explicacion = self._explicar_palanca(tipo, i, optimo, solo, marginal, aislado, compromiso)
            palancas.append(
                PalancaElegida(
                    id=lever_id,
                    nombre=nombre,
                    titular="hogar" if i is None else titulares[i],  # type: ignore[arg-type]
                    accion=accion,
                    importe=importe,
                    opcion=(
                        self._hogar(optimo).tributacion
                        if tipo == "modo"
                        else optimo.donativo[0] if tipo == "donativo" and optimo.donativo else None
                    ),
                    naturaleza=naturalezas[tipo],
                    compromiso=compromiso,
                    ahorro_marginal=marginal,
                    ahorro_aislado=aislado,
                    accion_aislada=self._accion(tipo, i, solo),
                    ahorro_por_euro=por_euro,
                    coste_neto=compromiso - marginal if tipo == "donativo" else None,
                    plazo=None if tipo == "modo" else self.cierre,
                    referencia=referencia,
                    verificado=verificado,
                    requisitos=requisitos,
                    explicacion=explicacion,
                )
            )

        ahorro_total = ev_base.total - ev_opt.total
        suma_aislados = sum((p.ahorro_aislado for p in palancas), ZERO)
        interaccion = ahorro_total - suma_aislados if palancas else ZERO
        # A lever that saves nothing (or costs) on its own is exactly what a one-at-a-time search never picks.
        solo_en_combinacion = [
            f"«{p.accion}» por sí sola "
            + ("no ahorra nada" if not p.ahorro_aislado else f"costaría {_eur(-p.ahorro_aislado)} más")
            + f"; dentro del plan ahorra {_eur(p.ahorro_marginal)}: una búsqueda palanca a palanca no la elegiría."
            for p in palancas
            if p.ahorro_aislado <= 0 < p.ahorro_marginal
        ]
        explicacion_interaccion = _unicos(
            [*solo_en_combinacion, *(self._analisis_interaccion(base, optimo, solos, interaccion) if palancas else [])]
        )

        lin_base = _agregar_lineas(r for _, _, r in ev_base.declaraciones)
        lin_opt = _agregar_lineas(r for _, _, r in ev_opt.declaraciones)
        cambios = []
        for concepto in _unicos([*lin_opt, *lin_base]):
            antes, ref_a = lin_base.get(concepto, (ZERO, None))
            despues, ref_d = lin_opt.get(concepto, (ZERO, None))
            if abs(despues - antes) >= CENT:
                cambios.append(CambioLinea(concepto=concepto, antes=antes, despues=despues, referencia=ref_d or ref_a))

        textos = [
            t
            for ref, t in TEXTOS_LEGALES.items()
            if any(ref in r for p in palancas for r in p.requisitos)
            or (len(self.modos) > 1 and ref in ("art. 83.1 LIRPF", "art. 83.2 LIRPF"))
        ]
        citas = _unicos(
            [
                *(p.referencia for p in palancas),
                *(c.referencia or "" for c in cambios),
                *(f"{t.referencia} ({t.boe_id})" for t in textos),
                *(r.fuente for _, _, r in ev_opt.declaraciones),
            ]
        )
        supuestos = list(self.supuestos)
        if self.con_conyuge:
            if self.hijos_de_ambos:
                supuestos.append(
                    "Los hijos se han tomado como comunes: en las declaraciones individuales cada cónyuge aplica la "
                    f"mitad del mínimo por descendientes ({_cita('art. 61.1.ª LIRPF')})."
                )
            supuestos += [
                "En las declaraciones individuales, la base imponible del resto de la unidad familiar de cada cónyuge "
                "es la del otro en cada escenario (no constan rentas de los hijos).",
                "En la conjunta, cada cónyuge aplica su propio límite de planes de pensiones "
                f"({_cita('art. 84.2.1.º LIRPF')}).",
            ]
        avisos = _unicos([*self.avisos, *(a for _, _, r in ev_opt.declaraciones for a in r.avisos)])
        if self.con_conyuge and any(e for e in self.e0) and optimo.conjunta:
            avisos.append(
                "La contribución de la empresa al plan de pensiones es retribución en especie: debe estar incluida en "
                "el rendimiento íntegro del trabajo indicado."
            )
        faltan = _unicos(
            [
                *(c for c in CORE_FIELDS if c not in self.conocidos),
                *(d for nd in self.no_disponibles for d in nd.datos_que_faltan),
            ]
        )
        nota = (
            "Se han evaluado todas las combinaciones de la rejilla de candidatos (cero, el máximo permitido y cada "
            "importe que lleva una base justo a un umbral de los datos, ±1 céntimo) y después se ha refinado "
            "alrededor del óptimo y recortado cada importe al mínimo que da el mismo resultado."
            if self.estado.metodo == "exhaustiva"
            else "La rejilla de candidatos era demasiado grande para recorrerla entera: se ha usado ascenso por "
            "coordenadas desde el óptimo de cada palanca por separado y desde todas al máximo. Es el mejor plan "
            "encontrado, no está garantizado que sea el mejor posible."
        )
        busqueda = Busqueda(
            metodo=self.estado.metodo,
            evaluaciones=self.evaluaciones,
            limite_evaluaciones=self.max_evaluaciones,
            combinaciones=self.estado.combinaciones,
            candidatos=dict(sorted(self.estado.candidatos.items())),
            nota=nota,
        )
        plan = PlanOptimo(
            ejercicio=self.ejercicio,
            fecha=self.fecha,
            fase=self.fase,
            con_conyuge=self.con_conyuge,
            base=self._hogar(base),
            optimo=self._hogar(optimo),
            ahorro_total=ahorro_total,
            suma_ahorros_aislados=suma_aislados,
            interaccion=interaccion,
            explicacion_interaccion=explicacion_interaccion,
            palancas=palancas,
            no_disponibles=self.no_disponibles,
            descartadas=[*_DESCARTADAS, *self.descartadas_autonomicas()],
            oportunidades=self._oportunidades(optimo),
            cambios_traza=cambios,
            citas=citas,
            textos_legales=textos,
            avisos=avisos,
            supuestos=_unicos(supuestos),
            datos_que_faltan=faltan,
            busqueda=busqueda,
            explicacion="",
        )
        return plan.model_copy(update={"explicacion": explicar(plan)})

    def _factible(self, punto: _Punto) -> bool:
        """The current situation is a valid plan unless a donation the user will make anyway is missing."""
        return not (self._lever_donativo and self.r.donativo_previsto > 0 and punto.donativo is None)

    def _explicar_palanca(
        self,
        tipo: str,
        i: int | None,
        optimo: _Punto,
        solo: _Punto,
        marginal: Decimal,
        aislado: Decimal,
        compromiso: Decimal,
    ) -> str:
        partes = [f"Dentro del plan ahorra {_eur(marginal)}"]
        if compromiso:
            partes[0] += f" ({_eur(marginal / compromiso * 100).removesuffix(' €')} céntimos por euro)"
        if solo == optimo or self._accion(tipo, i, solo) == self._accion(tipo, i, optimo):
            partes.append(f"por sí sola ahorraría {_eur(aislado)}")
        else:
            partes.append(
                f"por sí sola, lo mejor sería «{self._accion(tipo, i, solo)}», con un ahorro de {_eur(aislado)}"
                if aislado >= 0
                else f"por sí sola costaría {_eur(-aislado)} más"
            )
        texto = "; ".join(partes) + "."
        if tipo == "pension":
            texto += (
                " Es un diferimiento: reduce la base liquidable este año, pero lo que rescates tributará como "
                "rendimiento del trabajo."
            )
        elif tipo == "perdida":
            texto += (
                " Es un diferimiento: si vuelves a comprar, el nuevo valor de compra es más bajo y la ganancia futura "
                "será mayor; si no, solo adelantas el uso de una pérdida que ya existía."
            )
        elif tipo == "donativo":
            assert optimo.donativo is not None
            texto += (
                f" Requiere gastar: el donativo te cuesta {_eur(compromiso - marginal)} una vez descontado el ahorro."
            )
            corriente = replace(optimo, donativo=(DESTINO_ESTATAL, 0, optimo.donativo[2]))
            mejora = self.evaluar(corriente).total - self.evaluar(optimo).total
            if mejora >= CENT:
                texto += (
                    f" Elegir este destino{' y que lo pague tu cónyuge' if optimo.donativo[1] == 1 else ''} ahorra "
                    f"{_eur(mejora)} más que donarlo a cualquier entidad de la Ley 49/2002."
                )
        else:
            texto += " Es un ahorro definitivo: la opción se elige al presentar la declaración, cada año."
        return texto


def _fijo(inp: IRPFInput, params: IRPFParams) -> Callable[[], tuple[IRPFInput, IRPFParams]]:
    return lambda: (inp, params)


def _con_cambios(
    inp: IRPFInput, cambios: dict[str, Any], params: IRPFParams
) -> Callable[[], tuple[IRPFInput, IRPFParams]]:
    """Builds the scenario only when it is not cached (``model_copy`` skips validation: the changes keep
    every total equal to its parts, see :func:`_sumar_donativo`)."""
    return lambda: (inp.model_copy(update=cambios), params)


def _unicos_puntos(puntos: Iterable[_Punto]) -> list[_Punto]:
    vistos: dict[_Punto, None] = {}
    for p in puntos:
        vistos.setdefault(p, None)
    return list(vistos)


# Choices considered and not offered, with the reason (shown in every plan).
_DESCARTADAS: tuple[PalancaDescartada, ...] = (
    PalancaDescartada(
        id="plan_pensiones_del_conyuge",
        nombre="Aportar al plan de pensiones de un cónyuge con rentas bajas",
        motivo="La ley prevé una reducción adicional por estas aportaciones, pero sus límites no están en los "
        "parámetros versionados de la calculadora: no se calcula ni se propone.",
        referencia="art. 51.7 LIRPF",
    ),
    PalancaDescartada(
        id="incremento_plan_de_empleo",
        nombre="Ampliar el límite con aportaciones propias a un plan de empleo",
        motivo="El incremento depende de la contribución de la empresa y de una tabla de multiplicadores que la "
        "calculadora no modela; las contribuciones de la empresa no las decide el contribuyente.",
        referencia="art. 52.1.b).1.º LIRPF",
    ),
    PalancaDescartada(
        id="reparto_minimo_descendientes",
        nombre="Elegir qué progenitor aplica el mínimo por descendientes",
        motivo="No es una opción: la ley lo reparte por partes iguales entre quienes tienen derecho.",
        referencia="art. 61.1.ª LIRPF",
    ),
    PalancaDescartada(
        id="reparto_deducciones_autonomicas",
        nombre="Elegir quién aplica las deducciones autonómicas por hijos, alquiler o vivienda",
        motivo="Las leyes autonómicas las prorratean entre quienes tienen derecho o las atribuyen a quien paga o "
        "es titular; la calculadora ya aplica ese reparto. No es una elección libre.",
        referencia="deducciones_calculables (reparto_descendientes, dividir_entre)",
    ),
    PalancaDescartada(
        id="maternidad",
        nombre="Elegir quién aplica la deducción por maternidad",
        motivo="Corresponde a la madre por ley; el padre solo en supuestos tasados. No es una elección.",
        referencia="art. 81 LIRPF",
    ),
    PalancaDescartada(
        id="momento_de_cobro",
        nombre="Adelantar o retrasar cobros del trabajo",
        motivo="Los rendimientos del trabajo se imputan al año en que son exigibles, no cuando se cobran: no es "
        "una elección del contribuyente.",
        referencia="art. 14.1.a) LIRPF",
    ),
    PalancaDescartada(
        id="inversiones_con_deduccion",
        nombre="Invertir para deducir (empresas de nueva creación, eficiencia energética, vehículo eléctrico…)",
        motivo="Son decisiones de inversión o de gasto con riesgo y finalidad propios: si las haces, indica el "
        "importe y la calculadora aplica la deducción que corresponda, pero el optimizador no las propone.",
        referencia="deducciones estatales y autonómicas por inversión",
    ),
)


def explicar(plan: PlanOptimo) -> str:
    """A short Spanish explanation of the plan, built from its figures (no language model)."""

    def estado(total: Decimal) -> str:
        if total > 0:
            return f"a pagar {_eur(total)}"
        if total < 0:
            return f"a devolver {_eur(-total)}"
        return "ni a pagar ni a devolver"

    modo = {
        "individual": "declaración individual" if not plan.con_conyuge else "declaraciones individuales",
        "conjunta_biparental": "declaración conjunta",
        "conjunta_monoparental": "declaración conjunta monoparental",
    }
    partes = [
        f"Con los datos actuales, tu IRPF de {plan.ejercicio} sale {estado(plan.base.resultado_total)} "
        f"({modo[plan.base.tributacion]})."
    ]
    if not plan.palancas:
        partes.append(
            "No hay ninguna combinación de las opciones disponibles que lo reduzca"
            + (
                ": a estas alturas solo quedan las opciones de la declaración."
                if plan.fase == "solo_opciones_de_declaracion"
                else "."
            )
        )
    else:
        partes.append(
            f"Con el plan óptimo sale {estado(plan.optimo.resultado_total)} ({modo[plan.optimo.tributacion]}): "
            f"ahorras {_eur(plan.ahorro_total)}."
        )
        partes.append("Qué hacer:")
        etiquetas = {
            "ahorro_definitivo": "ahorro definitivo",
            "diferimiento": "diferimiento",
            "requiere_gastar": "requiere gastar",
        }
        for n, p in enumerate(plan.palancas, start=1):
            partes.append(f"{n}. {p.accion} — {_eur(p.ahorro_marginal)} dentro del plan ({etiquetas[p.naturaleza]}).")
        if abs(plan.interaccion) >= CENT and len(plan.palancas) > 1:
            if plan.interaccion > 0:
                partes.append(
                    f"Juntas ahorran {_eur(plan.interaccion)} más que la suma de cada una por separado "
                    f"({_eur(plan.suma_ahorros_aislados)})."
                )
            else:
                partes.append(
                    f"Juntas ahorran {_eur(-plan.interaccion)} menos que la suma de cada una por separado "
                    f"({_eur(plan.suma_ahorros_aislados)}): no se pueden sumar."
                )
            partes.extend(plan.explicacion_interaccion)
        gastar = [p for p in plan.palancas if p.naturaleza == "requiere_gastar"]
        if gastar:
            partes.append(
                "Parte del ahorro exige gastar: "
                + "; ".join(f"{p.nombre.lower()}, coste neto {_eur(p.coste_neto or ZERO)}" for p in gastar)
                + "."
            )
    if plan.supuestos:
        partes.append("Supuestos: " + " ".join(plan.supuestos))
    bloqueadas = [nd for nd in plan.no_disponibles if nd.causa in ("fecha", "faltan_datos", "restricciones")]
    if bloqueadas:
        partes.append("No se han podido usar: " + " ".join(f"{nd.nombre}: {nd.detalle}" for nd in bloqueadas))
    partes.append(plan.busqueda.nota)
    return "\n".join(partes)


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def optimizar(
    contribuyente: IRPFInput,
    *,
    fecha: date,
    conyuge: IRPFInput | None = None,
    conocidos: Iterable[str] = (),
    conocidos_conyuge: Iterable[str] = (),
    restricciones: Restricciones | dict[str, Any] | None = None,
    estado_civil: str | None = None,
    params: IRPFParams | None = None,
    max_evaluaciones: int = MAX_EVALUACIONES,
    avisos_previos: Iterable[str] = (),
) -> PlanOptimo:
    """The best combination of the household's levers for the year of ``contribuyente``.

    ``conyuge`` makes it a married couple (both returns, or one joint return). ``conocidos`` are
    the input fields the user actually gave (a stated zero is not a missing figure). ``fecha``
    decides what is still possible: until 31 December every lever, afterwards only the filing
    choices. Raises :class:`DatosInvalidosError` when the figures cannot be combined.
    """
    if conyuge is not None and conyuge.ejercicio != contribuyente.ejercicio:
        raise DatosInvalidosError("Los datos de los dos cónyuges deben ser del mismo ejercicio.")
    if isinstance(restricciones, dict):
        try:
            restricciones = Restricciones.model_validate(restricciones)
        except ValidationError as exc:
            raise DatosInvalidosError(f"Restricciones no válidas: {_problemas(exc)}.") from exc
    return _Optimizador(
        contribuyente,
        conyuge,
        conocidos=frozenset(conocidos),
        conocidos_conyuge=frozenset(conocidos_conyuge),
        restricciones=restricciones or Restricciones(),
        fecha=fecha,
        estado_civil=estado_civil,
        params=params or load_params(contribuyente.ejercicio),
        max_evaluaciones=max_evaluaciones,
        avisos_previos=list(avisos_previos),
    ).ejecutar()


def conocidos_de(facts: dict[str, Any], datos: dict[str, Any], inp: IRPFInput) -> set[str]:
    """Input fields the user gave (the convention of the simulator and the benefit catalogue)."""
    conocidos = {name for name in (*facts, *datos) if name in IRPFInput.model_fields or name == "estado_civil"}
    if any(
        name in ACTIVIDAD_FACT_FIELDS and value is not None
        for source in (facts, datos)
        for name, value in source.items()
    ):
        conocidos.add("rendimiento_actividad_neto")
    if inp.descendientes:
        conocidos.update(RECUENTOS_DEL_DETALLE)
    return conocidos


def optimizar_desde_hechos(
    *,
    ejercicio: int,
    fecha: date,
    facts: dict[str, Any] | None = None,
    datos: dict[str, Any] | None = None,
    conyuge: dict[str, Any] | None = None,
    restricciones: Restricciones | dict[str, Any] | None = None,
) -> PlanOptimo:
    """:func:`optimizar` from the case facts (plus explicit ``datos``) and, optionally, the spouse's figures.

    Raises ``DatosInvalidosError`` or ``EjercicioNoDisponibleError`` on bad input, like the simulator.
    """
    facts = dict(facts or {})
    datos = dict(datos or {})
    if not isinstance(datos, dict) or (conyuge is not None and not isinstance(conyuge, dict)):
        raise DatosInvalidosError("'datos' y 'conyuge' deben ser objetos {campo: valor}.")
    estado_civil = datos.pop("estado_civil", None) or facts.get("estado_civil")
    contribuyente, actividad = build_input_con_actividad(ejercicio=ejercicio, facts=facts, datos=datos)
    avisos = [actividad.aviso] if actividad is not None else []
    otro: IRPFInput | None = None
    conocidos_conyuge: set[str] = set()
    if conyuge is not None:
        datos_conyuge = {k: v for k, v in conyuge.items() if k != "estado_civil"}
        otro, actividad_b = build_input_con_actividad(
            ejercicio=ejercicio, datos={"comunidad_autonoma": contribuyente.comunidad_autonoma, **datos_conyuge}
        )
        if actividad_b is not None:
            avisos.append(f"Cónyuge: {actividad_b.aviso}")
        conocidos_conyuge = conocidos_de({}, datos_conyuge, otro)
    return optimizar(
        contribuyente,
        fecha=fecha,
        conyuge=otro,
        conocidos=conocidos_de(facts, datos, contribuyente),
        conocidos_conyuge=conocidos_conyuge,
        restricciones=restricciones,
        estado_civil=str(estado_civil) if estado_civil is not None else None,
        avisos_previos=avisos,
    )
