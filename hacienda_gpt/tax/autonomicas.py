"""Regional deductions computed from data (see ``DeduccionCalculable``).

One generic routine covers the deductions that are a fixed amount per unit or
a percentage of an expense. It never decides a requirement it cannot see: those
are listed with the deduction and repeated as a warning whenever it is applied.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hacienda_gpt.tax.params import DeduccionCalculable

ZERO = Decimal(0)
HALF = Decimal("0.5")

ETIQUETA_BASE = {
    "base_imponible_total": "base imponible (general más ahorro)",
    "base_imponible_general": "base imponible general",
    "base_liquidable_total": "base liquidable (general más ahorro)",
    "base_liquidable_general": "base liquidable general",
    "base_imponible_total_menos_minimos": "base imponible menos el mínimo personal y familiar",
}


@dataclass(frozen=True)
class Bases:
    """The income figures a regional ceiling may refer to."""

    base_imponible_total: Decimal
    base_imponible_general: Decimal
    base_liquidable_total: Decimal
    base_liquidable_general: Decimal
    base_imponible_total_menos_minimos: Decimal
    # Taxable bases of the other members of the family unit.
    resto_unidad_familiar: Decimal


@dataclass(frozen=True)
class Resultado:
    importe: Decimal
    # Why a deduction the user gave data for was not applied (None when applied or simply not triggered).
    motivo: str | None = None


def campo_que_la_activa(d: DeduccionCalculable) -> str | None:
    """The input whose presence triggers the deduction (None: only personal conditions)."""
    return d.base if d.tipo == "porcentaje" else d.unidad


def evaluar(d: DeduccionCalculable, inp: Any, bases: Bases, *, conjunta: bool) -> Resultado | None:
    """Amount of one deduction, the reason it is refused, or None when nothing triggers it."""
    unidades = int(getattr(inp, d.unidad) or 0) if d.unidad else 0
    gasto = Decimal(getattr(inp, d.base) or 0) if d.base else ZERO
    if d.tipo == "porcentaje":
        if not gasto:
            return None
    elif d.unidad is not None and not unidades:
        return None

    personales: list[str] = []
    if d.edad_min is not None or d.edad_max is not None:
        if inp.edad is None:
            personales.append("falta tu edad")
        elif d.edad_min is not None and inp.edad < d.edad_min:
            personales.append(f"exige tener al menos {d.edad_min} años")
        elif d.edad_max is not None and inp.edad > d.edad_max:
            personales.append(f"exige no superar los {d.edad_max} años")
    if d.discapacidad_min is not None and inp.grado_discapacidad < d.discapacidad_min:
        personales.append(f"exige un grado de discapacidad de al menos el {d.discapacidad_min} %")
    for requisito in d.requiere:
        if not getattr(inp, requisito):
            personales.append(f"exige «{requisito.replace('_', ' ')}»")
    if personales:
        # A deduction triggered only by personal circumstances is simply not the user's case.
        return Resultado(ZERO, "; ".join(personales)) if campo_que_la_activa(d) else None

    ingreso: Decimal = getattr(bases, d.base_limite)
    etiqueta = ETIQUETA_BASE[d.base_limite]
    limite = d.limite_conjunta if conjunta and d.limite_conjunta is not None else d.limite_individual
    if limite is not None and (ingreso >= limite if d.limite_es_estricto else ingreso > limite):
        return Resultado(ZERO, f"tu {etiqueta} ({ingreso:.2f} €) supera el límite de {limite:.2f} €")
    if d.limite_unidad_familiar is not None:
        familia = bases.base_imponible_total + bases.resto_unidad_familiar
        if familia > d.limite_unidad_familiar:
            return Resultado(
                ZERO,
                f"la suma de bases de la unidad familiar ({familia:.2f} €) supera {d.limite_unidad_familiar:.2f} €",
            )
    if d.gasto_minimo_sobre_base is not None and gasto <= d.gasto_minimo_sobre_base * ingreso:
        return Resultado(
            ZERO, f"el gasto no supera el {d.gasto_minimo_sobre_base * 100:.0f} % de tu {etiqueta} ({ingreso:.2f} €)"
        )

    if d.tipo == "fija":
        assert d.importe is not None
        importe = d.importe * (unidades if d.unidad else 1)
    else:
        assert d.porcentaje is not None
        importe = gasto * d.porcentaje
        if d.maximo_por_unidad is not None:
            importe = min(importe, d.maximo_por_unidad * max(1, unidades))
        if d.maximo is not None:
            importe = min(importe, d.maximo)

    r = d.reduccion_progresiva
    if r is not None:
        desde = r.desde_conjunta if conjunta and r.desde_conjunta is not None else r.desde_individual
        hasta = r.hasta_conjunta if conjunta and r.hasta_conjunta is not None else r.hasta_individual
        if ingreso >= hasta:
            return Resultado(ZERO, f"tu {etiqueta} ({ingreso:.2f} €) alcanza los {hasta:.2f} € en que se extingue")
        if ingreso > desde:
            importe *= (hasta - ingreso) / (hasta - desde)
    if d.reparto_descendientes and inp.descendientes_compartidos and not conjunta:
        importe *= HALF
    return Resultado(importe)


def aplicar(
    deducciones: list[DeduccionCalculable],
    inp: Any,
    bases: Bases,
    *,
    conjunta: bool,
    norma: str,
    linea: Callable[[str, Decimal, str], None],
    aviso: Callable[[str], None],
) -> Decimal:
    """Apply every triggered deduction; returns their total (not yet capped by the regional tax)."""
    total = ZERO
    for d in deducciones:
        resultado = evaluar(d, inp, bases, conjunta=conjunta)
        if resultado is None:
            continue
        if resultado.motivo is not None:
            aviso(f"Deducción autonómica «{d.titulo}» no aplicada: {resultado.motivo}.")
            continue
        importe = resultado.importe.quantize(Decimal("0.01"))
        if not importe:
            continue
        linea(f"Deducción autonómica: {d.titulo}", -importe, f"{d.articulo} {norma}")
        if d.unidad and d.maximo_por_unidad is not None and not getattr(inp, d.unidad):
            aviso(f"«{d.titulo}»: el tope depende de «{d.unidad}», que no consta; se ha supuesto 1.")
        if d.requisitos_no_comprobados:
            aviso(
                f"«{d.titulo}» ({d.articulo}) se ha aplicado sin comprobar estos requisitos: "
                + "; ".join(r.rstrip(".") for r in d.requisitos_no_comprobados)
                + "."
            )
        total += importe
    return total
