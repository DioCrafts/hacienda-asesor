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

from hacienda_gpt.tax.params import Condiciones, DeduccionCalculable, Variante

ZERO = Decimal(0)
HALF = Decimal("0.5")

ETIQUETA_BASE = {
    "base_imponible_total": "base imponible (general más ahorro)",
    "base_imponible_general": "base imponible general",
    "base_liquidable_total": "base liquidable (general más ahorro)",
    "base_liquidable_general": "base liquidable general",
    "base_imponible_total_menos_minimos": "base imponible menos el mínimo personal y familiar",
    "base_liquidable_total_menos_minimos": "base liquidable menos el mínimo personal y familiar",
    "base_imponible_total_menos_minimo_descendientes": "base imponible menos el mínimo por descendientes",
}


@dataclass(frozen=True)
class Bases:
    """The income figures a regional ceiling may refer to."""

    base_imponible_total: Decimal
    base_imponible_general: Decimal
    base_liquidable_total: Decimal
    base_liquidable_general: Decimal
    base_imponible_total_menos_minimos: Decimal
    base_liquidable_total_menos_minimos: Decimal
    base_imponible_total_menos_minimo_descendientes: Decimal
    base_imponible_ahorro: Decimal
    # Taxable bases of the other members of the family unit.
    resto_unidad_familiar: Decimal


@dataclass(frozen=True)
class Resultado:
    importe: Decimal
    # Why a deduction the user gave data for was not applied (None when applied or simply not triggered).
    motivo: str | None = None
    # Description of the variant whose figures were used, if any.
    variante: str | None = None


def campo_que_la_activa(d: DeduccionCalculable) -> str | None:
    """The input whose presence triggers the deduction (None: only personal conditions)."""
    return d.base if d.tipo == "porcentaje" else d.unidad


def incumplidas(c: Condiciones, inp: Any, ingreso: Decimal, *, conjunta: bool) -> list[str]:
    """Why ``c`` does not hold for this taxpayer (empty when it does)."""
    motivos: list[str] = []
    if c.edad_min is not None or c.edad_max is not None:
        if inp.edad is None:
            motivos.append("falta tu edad")
        elif c.edad_min is not None and inp.edad < c.edad_min:
            motivos.append(f"tener al menos {c.edad_min} años")
        elif c.edad_max is not None and inp.edad > c.edad_max:
            motivos.append(f"no superar los {c.edad_max} años")
    if c.discapacidad_min is not None and inp.grado_discapacidad < c.discapacidad_min:
        motivos.append(f"un grado de discapacidad de al menos el {c.discapacidad_min} %")
    if c.discapacidad_max is not None and inp.grado_discapacidad > c.discapacidad_max:
        motivos.append(f"un grado de discapacidad de hasta el {c.discapacidad_max} %")
    for requisito in c.requiere:
        if not getattr(inp, requisito):
            motivos.append(f"«{requisito.replace('_', ' ')}»")
    for campo, minimo in c.unidades_min.items():
        if int(getattr(inp, campo) or 0) < minimo:
            motivos.append(f"«{campo.replace('_', ' ')}» de al menos {minimo}")
    for campo, maximo in c.unidades_max.items():
        if int(getattr(inp, campo) or 0) > maximo:
            motivos.append(f"«{campo.replace('_', ' ')}» de {maximo} como máximo")
    if c.tributacion is not None and (c.tributacion == "conjunta") != conjunta:
        motivos.append(f"tributación {c.tributacion}")
    if c.ingreso_max is not None and ingreso > c.ingreso_max:
        motivos.append(f"ingresos de hasta {c.ingreso_max:.2f} €")
    return motivos


def variante_aplicable(d: DeduccionCalculable, inp: Any, ingreso: Decimal, *, conjunta: bool) -> Variante | None:
    return next((v for v in d.variantes if not incumplidas(v.cuando, inp, ingreso, conjunta=conjunta)), None)


def _por_orden(importes: list[Decimal], nacidos: int, descendientes: int) -> Decimal:
    """Sum of the amounts for the ``nacidos`` youngest of ``descendientes`` children."""
    total = max(descendientes, nacidos)
    return sum((importes[min(orden, len(importes)) - 1] for orden in range(total - nacidos + 1, total + 1)), ZERO)


def evaluar(d: DeduccionCalculable, inp: Any, bases: Bases, *, conjunta: bool) -> Resultado | None:
    """Amount of one deduction, the reason it is refused, or None when nothing triggers it."""
    unidades = int(getattr(inp, d.unidad) or 0) if d.unidad else 0
    gasto = Decimal(getattr(inp, d.base) or 0) if d.base else ZERO
    if d.tipo == "porcentaje":
        if not gasto:
            return None
    elif d.unidad is not None and not unidades:
        return None
    activada_por_dato = campo_que_la_activa(d) is not None
    ingreso: Decimal = getattr(bases, d.base_limite)
    etiqueta = ETIQUETA_BASE[d.base_limite]

    personales = incumplidas(d.condiciones, inp, ingreso, conjunta=conjunta)
    if personales:
        # A deduction triggered only by personal circumstances is simply not the user's case.
        return Resultado(ZERO, "exige " + "; ".join(personales)) if activada_por_dato else None
    if d.alternativas and all(incumplidas(a, inp, ingreso, conjunta=conjunta) for a in d.alternativas):
        if not activada_por_dato:
            return None
        opciones = " / ".join(", ".join(incumplidas(a, inp, ingreso, conjunta=conjunta)) for a in d.alternativas)
        return Resultado(ZERO, f"no cumples ninguna de las condiciones alternativas ({opciones})")

    v = variante_aplicable(d, inp, ingreso, conjunta=conjunta)

    def cifra(nombre: str) -> Any:
        propia = getattr(v, nombre) if v is not None else None
        return propia if propia is not None else getattr(d, nombre)

    limite_conjunta, limite_individual = cifra("limite_conjunta"), cifra("limite_individual")
    limite = limite_conjunta if conjunta and limite_conjunta is not None else limite_individual
    if limite is not None and (ingreso >= limite if d.limite_es_estricto else ingreso > limite):
        if not activada_por_dato and not d.requiere:
            # Reached only through a count or an age: not worth a refusal notice.
            return None
        return Resultado(ZERO, f"tu {etiqueta} ({ingreso:.2f} €) supera el límite de {limite:.2f} €")
    if d.limite_unidad_familiar is not None:
        familia = bases.base_imponible_total + bases.resto_unidad_familiar
        if familia > d.limite_unidad_familiar:
            return Resultado(
                ZERO,
                f"la suma de bases de la unidad familiar ({familia:.2f} €) supera {d.limite_unidad_familiar:.2f} €",
            )
    if d.base_ahorro_max is not None and bases.base_imponible_ahorro > d.base_ahorro_max:
        return Resultado(
            ZERO, f"tu base del ahorro ({bases.base_imponible_ahorro:.2f} €) supera {d.base_ahorro_max:.2f} €"
        )
    if d.gasto_minimo_sobre_base is not None and gasto <= d.gasto_minimo_sobre_base * ingreso:
        return Resultado(
            ZERO, f"el gasto no supera el {d.gasto_minimo_sobre_base * 100:.0f} % de tu {etiqueta} ({ingreso:.2f} €)"
        )

    r = d.reduccion_progresiva
    factor = Decimal(1)
    if r is not None:
        desde = r.desde_conjunta if conjunta and r.desde_conjunta is not None else r.desde_individual
        hasta = r.hasta_conjunta if conjunta and r.hasta_conjunta is not None else r.hasta_individual
        if ingreso >= hasta:
            return Resultado(ZERO, f"tu {etiqueta} ({ingreso:.2f} €) alcanza los {hasta:.2f} € en que se extingue")
        if ingreso > desde:
            factor = (hasta - ingreso) / (hasta - desde)
    sobre_tope = r is not None and r.aplica_a == "maximo"

    if d.tipo == "fija":
        por_orden = v.importes_por_orden if v is not None and v.importes_por_orden else None
        fijo = v.importe if v is not None and v.importe is not None else None
        if por_orden is None and fijo is None:
            por_orden, fijo = d.importes_por_orden, d.importe
        if por_orden:
            importe = _por_orden(por_orden, unidades, int(inp.num_descendientes))
        else:
            assert fijo is not None
            importe = fijo * (unidades if d.unidad else 1)
        importe *= factor
    else:
        importe = gasto * cifra("porcentaje")
        maximo_por_unidad, maximo = cifra("maximo_por_unidad"), cifra("maximo")
        tope = factor if sobre_tope else Decimal(1)
        if maximo_por_unidad is not None:
            importe = min(importe, maximo_por_unidad * max(1, unidades) * tope)
        if maximo is not None:
            importe = min(importe, maximo * tope)
        if not sobre_tope:
            importe *= factor

    if d.reparto_descendientes and inp.descendientes_compartidos and not conjunta:
        importe *= HALF
    return Resultado(importe, variante=v.descripcion if v is not None else None)


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
    aplicadas: set[str] = set()
    for d in deducciones:
        resultado = evaluar(d, inp, bases, conjunta=conjunta)
        if resultado is None:
            continue
        excluyentes = sorted(aplicadas & set(d.incompatible_con))
        if excluyentes and resultado.motivo is None:
            aviso(f"Deducción autonómica «{d.titulo}» no aplicada: es incompatible con {', '.join(excluyentes)}.")
            continue
        if resultado.motivo is not None:
            aviso(f"Deducción autonómica «{d.titulo}» no aplicada: {resultado.motivo}.")
            continue
        importe = resultado.importe.quantize(Decimal("0.01"))
        if not importe:
            continue
        linea(f"Deducción autonómica: {d.titulo}", -importe, f"{d.articulo} {norma}")
        if resultado.variante:
            aviso(f"«{d.titulo}»: se han aplicado las cifras de la variante «{resultado.variante}».")
        if d.importes_por_orden or any(v.importes_por_orden for v in d.variantes):
            aviso(
                f"«{d.titulo}»: el importe depende del orden del hijo; se ha supuesto que los nacidos en el año son "
                "los menores de los descendientes que has indicado."
            )
        if d.unidad and d.maximo_por_unidad is not None and not getattr(inp, d.unidad):
            aviso(f"«{d.titulo}»: el tope depende de «{d.unidad}», que no consta; se ha supuesto 1.")
        if d.requisitos_no_comprobados:
            aviso(
                f"«{d.titulo}» ({d.articulo}) se ha aplicado sin comprobar estos requisitos: "
                + "; ".join(r.rstrip(".") for r in d.requisitos_no_comprobados)
                + "."
            )
        total += importe
        aplicadas.add(d.articulo)
    return total
