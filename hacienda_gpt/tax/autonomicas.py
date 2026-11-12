"""Regional deductions computed from data (see ``DeduccionCalculable``).

One generic routine covers the deductions that are a fixed amount per unit or
a percentage of an expense. It never decides a requirement it cannot see: those
are listed with the deduction and repeated as a warning whenever it is applied.
"""

from __future__ import annotations

from calendar import isleap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from hacienda_gpt.tax.params import (
    CAMPOS_UNIDAD_HIJOS,
    PERIODOS_POR_HIJO,
    AjustePorHijo,
    Condiciones,
    DeduccionCalculable,
    FiltroDescendientes,
    Prorrateo,
    TramoPorcentaje,
    Variante,
)

ZERO = Decimal(0)
UNO = Decimal(1)
HALF = Decimal("0.5")
CENT = Decimal("0.01")
# Above this many mutually exclusive deductions the best combination is not searched exhaustively.
MAX_GRUPO_EXHAUSTIVO = 12

ETIQUETA_BASE = {
    "base_imponible_total": "base imponible (general más ahorro)",
    "base_imponible_general": "base imponible general",
    "base_liquidable_total": "base liquidable (general más ahorro)",
    "base_liquidable_general": "base liquidable general",
    "base_imponible_total_menos_minimos": "base imponible menos el mínimo personal y familiar",
    "base_liquidable_total_menos_minimos": "base liquidable menos el mínimo personal y familiar",
    "base_imponible_total_menos_minimo_descendientes": "base imponible menos el mínimo por descendientes",
}
ETIQUETA_ETAPA = {
    "infantil_primer_ciclo": "primer ciclo de infantil",
    "infantil_segundo_ciclo": "segundo ciclo de infantil",
    "primaria": "primaria",
    "eso": "ESO",
    "educacion_especial": "educación especial",
    "bachillerato": "bachillerato",
    "fp_basica": "FP básica",
    "fp_grado_medio": "FP de grado medio",
    "fp_grado_superior": "FP de grado superior",
    "universidad": "universidad",
}
FALTA_DETALLE = "depende de datos de cada descendiente (edad, estudios…) que no constan: indícalos en «descendientes»"


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
    # Regional gross tax, for the caps and deductions that are a share of it.
    cuota_integra_autonomica: Decimal | None = None


@dataclass(frozen=True)
class Resultado:
    importe: Decimal
    # Why a deduction the user gave data for was not applied (None when applied or simply not triggered).
    motivo: str | None = None
    # Description of the variant whose figures were used, if any.
    variante: str | None = None
    # Assumptions made while computing it, to be shown with the deduction.
    avisos: tuple[str, ...] = ()
    # Not triggered by any datum, but it cannot be ruled out without the children's detail.
    falta_detalle: bool = False


def campo_que_la_activa(d: DeduccionCalculable) -> str | None:
    """The input whose presence triggers the deduction (None: only personal conditions)."""
    if d.tipo == "porcentaje":
        return None if d.sobre_cuota else d.base
    return d.unidad or ("descendientes" if d.unidad_descendientes is not None else None)


# --------------------------------------------------------------------------- #
# Children
# --------------------------------------------------------------------------- #


def _hijos(inp: Any) -> list[Any] | None:
    """The per-child detail, or None when the taxpayer gave only counts."""
    return list(getattr(inp, "descendientes", None) or []) or None


def cumple_filtro(hijo: Any, f: FiltroDescendientes, ejercicio: int) -> bool:
    if f.edad_min is not None and hijo.edad < f.edad_min:
        return False
    if f.edad_max is not None and hijo.edad > f.edad_max:
        return False
    if f.discapacidad_min is not None and hijo.grado_discapacidad < f.discapacidad_min:
        return False
    if f.discapacidad_max is not None and hijo.grado_discapacidad > f.discapacidad_max:
        return False
    if f.etapas and hijo.etapa_educativa not in f.etapas:
        return False
    if f.anios_desde_llegada_max is not None and hijo.llegada(ejercicio) > f.anios_desde_llegada_max:
        return False
    if f.adoptado is not None and (hijo.ejercicio_adopcion is not None) != f.adoptado:
        return False
    if f.adopcion_internacional is not None and hijo.adopcion_internacional != f.adopcion_internacional:
        return False
    return f.parto_multiple is None or hijo.parto_multiple == f.parto_multiple


_SOLO_FILTRO = set(FiltroDescendientes.model_fields)
# Filters an old count answers exactly, for taxpayers who gave no per-child detail.
_RECUENTO_EQUIVALENTE: list[tuple[dict[str, Any], str]] = [
    ({}, "num_descendientes"),
    ({"edad_max": 2}, "num_descendientes_menores_3"),
    ({"anios_desde_llegada_max": 0}, "hijos_nacidos_en_el_ejercicio"),
    ({"anios_desde_llegada_max": 2}, "hijos_nacidos_ultimos_3_ejercicios"),
    ({"etapas": ["infantil_primer_ciclo"]}, "num_hijos_primer_ciclo_infantil"),
]


def contar_descendientes(inp: Any, f: FiltroDescendientes) -> int | None:
    """How many children match ``f``; None when it cannot be known from the data given."""
    hijos = _hijos(inp)
    if hijos is not None:
        return sum(1 for h in hijos if cumple_filtro(h, f, int(inp.ejercicio)))
    puesto = f.model_dump(include=_SOLO_FILTRO, exclude_defaults=True)
    for filtro, campo in _RECUENTO_EQUIVALENTE:
        if puesto == filtro:
            return int(getattr(inp, campo) or 0)
    return None if int(inp.num_descendientes or 0) else 0


def describe_filtro(f: FiltroDescendientes) -> str:
    partes: list[str] = []
    if f.edad_min is not None and f.edad_max is not None:
        partes.append(f"de {f.edad_min} a {f.edad_max} años")
    elif f.edad_max is not None:
        partes.append(f"de hasta {f.edad_max} años")
    elif f.edad_min is not None:
        partes.append(f"de {f.edad_min} años o más")
    if f.discapacidad_min is not None:
        partes.append(f"con discapacidad de al menos el {f.discapacidad_min} %")
    if f.discapacidad_max is not None:
        partes.append(f"con discapacidad de hasta el {f.discapacidad_max} %")
    if f.etapas:
        partes.append("en " + " o ".join(ETIQUETA_ETAPA[e] for e in f.etapas))
    if f.anios_desde_llegada_max is not None:
        partes.append(
            "nacidos o adoptados en el ejercicio"
            if f.anios_desde_llegada_max == 0
            else f"nacidos o adoptados en el ejercicio o en los {f.anios_desde_llegada_max} anteriores"
        )
    if f.adoptado is not None:
        partes.append("adoptados" if f.adoptado else "no adoptados")
    if f.adopcion_internacional:
        partes.append("de adopción internacional")
    if f.parto_multiple:
        partes.append("de parto o adopción múltiple")
    return ", ".join(partes)


def _hijos_de_la_unidad(d: DeduccionCalculable, inp: Any) -> list[Any] | None:
    """The children the deduction's unit counts, when the per-child detail tells."""
    hijos = _hijos(inp)
    if hijos is None:
        return None
    ejercicio = int(inp.ejercicio)
    if d.unidad_descendientes is not None:
        return [h for h in hijos if cumple_filtro(h, d.unidad_descendientes, ejercicio)]
    if d.unidad == "num_descendientes":
        return hijos
    if d.unidad == "num_descendientes_menores_3":
        return [h for h in hijos if h.edad < 3]
    if d.unidad == "hijos_nacidos_en_el_ejercicio":
        return [h for h in hijos if h.llegada(ejercicio) == 0]
    if d.unidad == "hijos_nacidos_ultimos_3_ejercicios":
        return [h for h in hijos if h.llegada(ejercicio) <= 2]
    if d.unidad == "hijos_adoptados_en_el_ejercicio":
        return [h for h in hijos if h.ejercicio_adopcion == ejercicio]
    if d.unidad == "num_hijos_primer_ciclo_infantil":
        return [h for h in hijos if h.etapa_educativa == "infantil_primer_ciclo"]
    return None  # num_hijos_gastos_educativos: the detail does not say which


def _cumpleanos(nacimiento: date, edad: int) -> date:
    try:
        return nacimiento.replace(year=nacimiento.year + edad)
    except ValueError:  # 29 February
        return date(nacimiento.year + edad, 3, 1)


def dias_en_edad(hijo: Any, ejercicio: int, edad_min: int | None, edad_max: int | None) -> int | None:
    """Days of the tax year on which the child's age is within the bounds (None: unknown)."""
    inicio, fin = date(ejercicio, 1, 1), date(ejercicio + 1, 1, 1)
    nacimiento = hijo.fecha_nacimiento
    if nacimiento is None:
        # Only the age at 31 December is known: decided when every possible birthday agrees.
        cumple_siempre = hijo.edad >= 1 and (edad_min is None or hijo.edad - 1 >= edad_min)
        if cumple_siempre and (edad_max is None or hijo.edad <= edad_max):
            return (fin - inicio).days
        if (edad_max is not None and hijo.edad - 1 > edad_max) or (edad_min is not None and hijo.edad < edad_min):
            return 0
        return None
    desde = max(inicio, _cumpleanos(nacimiento, edad_min or 0))
    hasta = fin if edad_max is None else min(fin, _cumpleanos(nacimiento, edad_max + 1))
    return max(0, (hasta - desde).days)


# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #


def _persona_incumple(c: Condiciones, edad: int | None, grado: int) -> list[str]:
    """Why one person (taxpayer or spouse) does not meet the age/disability bounds."""
    motivos: list[str] = []
    if c.edad_min is not None or c.edad_max is not None:
        if edad is None:
            motivos.append("falta tu edad")
        elif c.edad_min is not None and edad < c.edad_min:
            motivos.append(f"tener al menos {c.edad_min} años")
        elif c.edad_max is not None and edad > c.edad_max:
            motivos.append(f"no superar los {c.edad_max} años")
    if c.discapacidad_min is not None and grado < c.discapacidad_min:
        motivos.append(f"un grado de discapacidad de al menos el {c.discapacidad_min} %")
    if c.discapacidad_max is not None and grado > c.discapacidad_max:
        motivos.append(f"un grado de discapacidad de hasta el {c.discapacidad_max} %")
    return motivos


def _conyuge(inp: Any) -> tuple[int | None, int] | None:
    """Age and disability of the spouse, when anything is known about them."""
    edad, grado = getattr(inp, "edad_conyuge", None), int(getattr(inp, "grado_discapacidad_conyuge", 0) or 0)
    return (edad, grado) if edad is not None or grado else None


def _cuenta_el_conyuge(c: Condiciones, *, conjunta: bool) -> bool:
    return c.conyuge == "siempre" or (c.conyuge == "en_conjunta" and conjunta)


def incumplidas(c: Condiciones, inp: Any, ingreso: Decimal, *, conjunta: bool) -> list[str]:
    """Why ``c`` does not hold for this taxpayer (empty when it does)."""
    motivos = _persona_incumple(c, inp.edad, inp.grado_discapacidad)
    if motivos and _cuenta_el_conyuge(c, conjunta=conjunta):
        conyuge = _conyuge(inp)
        if conyuge is not None and not _persona_incumple(c, *conyuge):
            motivos = []
        else:
            motivos = [f"{m} (tú o tu cónyuge)" for m in motivos]
    if c.conyuge_discapacidad_min is not None and (
        int(getattr(inp, "grado_discapacidad_conyuge", 0) or 0) < c.conyuge_discapacidad_min
    ):
        motivos.append(f"un grado de discapacidad de tu cónyuge de al menos el {c.conyuge_discapacidad_min} %")
    for requisito in c.requiere:
        if not getattr(inp, requisito):
            motivos.append(f"«{requisito.replace('_', ' ')}»")
    for campo, minimo in c.unidades_min.items():
        if int(getattr(inp, campo) or 0) < minimo:
            motivos.append(f"«{campo.replace('_', ' ')}» de al menos {minimo}")
    for campo, maximo in c.unidades_max.items():
        if int(getattr(inp, campo) or 0) > maximo:
            motivos.append(f"«{campo.replace('_', ' ')}» de {maximo} como máximo")
    for requisito_hijos in c.descendientes:
        cuantos = contar_descendientes(inp, requisito_hijos)
        cuales = describe_filtro(requisito_hijos)
        if cuantos is None:
            motivos.append(f"faltan los datos de tus descendientes para saber cuántos hay {cuales}".rstrip())
        elif cuantos < requisito_hijos.minimo:
            motivos.append(f"al menos {requisito_hijos.minimo} descendiente(s) {cuales}".rstrip())
        elif requisito_hijos.maximo is not None and cuantos > requisito_hijos.maximo:
            motivos.append(f"no más de {requisito_hijos.maximo} descendiente(s) {cuales}".rstrip())
    if c.habitantes_max is not None:
        habitantes = getattr(inp, "habitantes_municipio", None)
        if habitantes is None:
            motivos.append("falta el número de habitantes de tu municipio")
        elif habitantes > c.habitantes_max:
            motivos.append(f"un municipio de hasta {c.habitantes_max} habitantes")
    ejercicio = int(getattr(inp, "ejercicio", 0) or 0)
    for campo, anios in c.anios_desde_max.items():
        cuando = getattr(inp, campo, None)
        if cuando is None:
            motivos.append(f"falta «{campo.replace('_', ' ')}»")
        elif not 0 <= ejercicio - cuando <= anios:
            motivos.append(
                f"«{campo.replace('_', ' ')}» en este ejercicio"
                if anios == 0
                else f"«{campo.replace('_', ' ')}» en este ejercicio o en los {anios} anteriores"
            )
    for campo, primero in c.ejercicio_min.items():
        cuando = getattr(inp, campo, None)
        if cuando is None:
            motivos.append(f"falta «{campo.replace('_', ' ')}»")
        elif cuando < primero:
            motivos.append(f"«{campo.replace('_', ' ')}» no anterior a {primero}")
    if c.tributacion is not None and (c.tributacion == "conjunta") != conjunta:
        motivos.append(f"tributación {c.tributacion}")
    if c.ingreso_max is not None and ingreso > c.ingreso_max:
        motivos.append(f"ingresos de hasta {c.ingreso_max:.2f} €")
    return motivos


def variante_aplicable(d: DeduccionCalculable, inp: Any, ingreso: Decimal, *, conjunta: bool) -> Variante | None:
    return next((v for v in d.variantes if not incumplidas(v.cuando, inp, ingreso, conjunta=conjunta)), None)


# --------------------------------------------------------------------------- #
# Amounts
# --------------------------------------------------------------------------- #


def _por_orden(importes: list[Decimal], nacidos: int, descendientes: int) -> Decimal:
    """Sum of the amounts for the ``nacidos`` youngest of ``descendientes`` children."""
    total = max(descendientes, nacidos)
    return sum((importes[min(orden, len(importes)) - 1] for orden in range(total - nacidos + 1, total + 1)), ZERO)


def _del_orden(importes: list[Decimal], orden: int) -> Decimal:
    return importes[min(orden, len(importes)) - 1]


def _por_tramos(tramos: list[TramoPorcentaje], base: Decimal) -> Decimal:
    """Tiered rate: each tier's percentage on the slice of ``base`` that falls in it."""
    importe, desde = ZERO, ZERO
    for tramo in tramos:
        hasta = base if tramo.hasta is None else min(base, tramo.hasta)
        if hasta > desde:
            importe += (hasta - desde) * tramo.porcentaje
            desde = hasta
    return importe


def _dias_del_ejercicio(ejercicio: int) -> int:
    return 366 if isleap(ejercicio) else 365


def factor_periodo(pr: Prorrateo, valor: int, ejercicio: int) -> Decimal:
    """Share of the amount (or cap) that corresponds to ``valor`` days or months."""
    if pr.por_cada_periodo:
        return Decimal(valor)
    if pr.tramos:
        return next((t.factor for t in reversed(pr.tramos) if valor >= t.desde), ZERO)
    total = pr.total or (12 if pr.dato.startswith("meses") else _dias_del_ejercicio(ejercicio))
    return min(UNO, Decimal(valor) / Decimal(total))


def _minimo(*valores: Decimal | None) -> Decimal | None:
    presentes = [v for v in valores if v is not None]
    return min(presentes) if presentes else None


def evaluar(d: DeduccionCalculable, inp: Any, bases: Bases, *, conjunta: bool) -> Resultado | None:
    """Amount of one deduction, the reason it is refused, or None when nothing triggers it."""
    ejercicio = int(getattr(inp, "ejercicio", 0) or 0)
    con_unidad = d.unidad is not None or d.unidad_descendientes is not None
    de_hijos = d.unidad_descendientes is not None or d.unidad in CAMPOS_UNIDAD_HIJOS
    unidades_desconocidas = False
    if d.unidad_descendientes is not None:
        contados = contar_descendientes(inp, d.unidad_descendientes)
        unidades_desconocidas, unidades = contados is None, contados or 0
    else:
        unidades = int(getattr(inp, d.unidad) or 0) if d.unidad else 0
    gastos = {b: Decimal(getattr(inp, b) or 0) for b in d.bases}
    gasto = sum(gastos.values(), ZERO)
    cuota = bases.cuota_integra_autonomica
    activada_por_dato = campo_que_la_activa(d) is not None
    ingreso: Decimal = getattr(bases, d.base_limite)
    etiqueta = ETIQUETA_BASE[d.base_limite]

    if d.tipo == "porcentaje":
        if d.sobre_cuota:
            if con_unidad and not unidades and not unidades_desconocidas:
                return None
        elif not gasto:
            return None
    elif con_unidad and not unidades:
        if unidades_desconocidas and not incumplidas(d.condiciones, inp, ingreso, conjunta=conjunta):
            return Resultado(ZERO, FALTA_DETALLE, falta_detalle=True)
        return None

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
    notas: list[str] = []

    def cifra(nombre: str) -> Any:
        propia = getattr(v, nombre) if v is not None else None
        if nombre == "maximo" and v is not None and v.maximo_incremento is not None and d.maximo is not None:
            return d.maximo + v.maximo_incremento
        return propia if propia is not None else getattr(d, nombre)

    def hecho(importe: Decimal, motivo: str | None = None, *, falta_detalle: bool = False) -> Resultado:
        return Resultado(importe, motivo, v.descripcion if v is not None else None, tuple(notas), falta_detalle)

    limite_conjunta, limite_individual = cifra("limite_conjunta"), cifra("limite_individual")
    if d.limite_conjunta_incremento is not None and limite_individual is not None and limite_conjunta is None:
        limite_conjunta = limite_individual + d.limite_conjunta_incremento
    limite = limite_conjunta if conjunta and limite_conjunta is not None else limite_individual
    ajuste_limite = next(
        (a for a in d.ajustes_limite if not incumplidas(a.cuando, inp, ingreso, conjunta=conjunta)), None
    )
    if limite is not None and ajuste_limite is not None:
        limite = None if ajuste_limite.sin_limite else limite * (ajuste_limite.factor or UNO)
    factor = sobre_limite = UNO
    if limite is not None and (ingreso >= limite if d.limite_es_estricto else ingreso > limite):
        if d.factor_sobre_limite is None:
            if not activada_por_dato and not d.requiere:
                # Reached only through a count or an age: not worth a refusal notice.
                return None
            return Resultado(ZERO, f"tu {etiqueta} ({ingreso:.2f} €) supera el límite de {limite:.2f} €")
        sobre_limite = d.factor_sobre_limite
        notas.append(
            f"tu {etiqueta} ({ingreso:.2f} €) supera el límite de {limite:.2f} €: se aplica el "
            f"{d.factor_sobre_limite * 100:.0f} % de la deducción"
        )
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
    necesita_cuota = d.sobre_cuota or d.maximo_fraccion_cuota is not None
    if necesita_cuota and cuota is None:
        return Resultado(ZERO, "no consta la cuota íntegra autonómica")

    r = d.reduccion_progresiva
    if r is not None:
        desde = r.desde_conjunta if conjunta and r.desde_conjunta is not None else r.desde_individual
        hasta = r.hasta_conjunta if conjunta and r.hasta_conjunta is not None else r.hasta_individual
        if ingreso >= hasta:
            return Resultado(ZERO, f"tu {etiqueta} ({ingreso:.2f} €) alcanza los {hasta:.2f} € en que se extingue")
        if ingreso > desde:
            factor = (hasta - ingreso) / (hasta - desde)
    sobre_tope = r is not None and r.aplica_a == "maximo"

    # Part of the year: one factor for the whole deduction, or one per child.
    pr = d.prorrateo
    por_hijo = pr is not None and pr.dato in PERIODOS_POR_HIJO
    periodo_importe = periodo_tope = UNO
    if pr is not None and not por_hijo:
        valor = getattr(inp, pr.dato, None)
        nombre = pr.dato.replace("_", " ")
        if valor is None and pr.si_falta == "denegar":
            return hecho(ZERO, f"se prorratea por «{nombre}», que no consta")
        if valor is None:
            notas.append(f"se prorratea por «{nombre}», que no consta: se ha supuesto todo el año")
        else:
            periodo = factor_periodo(pr, int(valor), ejercicio)
            if not periodo:
                return hecho(ZERO, f"«{nombre}» ({valor}) no alcanza el mínimo exigido")
            if pr.aplica_a == "importe":
                periodo_importe = periodo
            else:
                periodo_tope = periodo
            if pr.dato == "dias_acogimiento" and unidades > 1:
                notas.append("se han aplicado los mismos días de convivencia a todos los menores acogidos")

    def factor_hijo(hijo: Any) -> Decimal:
        """Per-child share of the year (1 when the deduction is not prorated child by child)."""
        if pr is None or not por_hijo:
            return UNO
        if pr.dato == "meses_guarderia_hijo":
            valor = hijo.meses_guarderia
            falta = "los meses de guardería"
        else:
            valor = dias_en_edad(hijo, ejercicio, pr.edad_min, pr.edad_max)
            falta = "la fecha de nacimiento"
        if valor is None:
            if pr.si_falta == "completo":
                notas.append(f"falta {falta} de un descendiente: se le ha supuesto todo el año")
                return UNO
            notas.append(f"falta {falta} de un descendiente: no se ha contado")
            return ZERO
        return factor_periodo(pr, int(valor), ejercicio)

    def ajuste_de(hijo: Any) -> AjustePorHijo | None:
        return next((a for a in d.ajustes_por_hijo if cumple_filtro(hijo, a.filtro, ejercicio)), None)

    partes_iguales = int(getattr(inp, d.dividir_entre, None) or 1) if d.dividir_entre else 1
    de_la_unidad = _hijos_de_la_unidad(d, inp) if de_hijos else None
    hijo_a_hijo = de_hijos and (bool(d.ajustes_por_hijo) or por_hijo)
    if hijo_a_hijo and de_la_unidad is None:
        if d.ajustes_por_hijo or (pr is not None and pr.si_falta == "denegar"):
            return hecho(ZERO, FALTA_DETALLE)
        notas.append("no constan los datos de cada descendiente: se ha supuesto todo el año para todos")
        hijo_a_hijo = False

    if d.tipo == "fija":
        por_orden = v.importes_por_orden if v is not None and v.importes_por_orden else None
        fijo = v.importe if v is not None and v.importe is not None else None
        if por_orden is None and fijo is None:
            por_orden, fijo = d.importes_por_orden, d.importe
        if por_orden and d.unidad == "hijos_nacidos_en_el_ejercicio":
            importe = _por_orden(por_orden, unidades, int(inp.num_descendientes))
        elif por_orden and d.unidad_descendientes is not None:
            hermanos = _hijos(inp)
            if hermanos is None or de_la_unidad is None:
                return hecho(ZERO, FALTA_DETALLE)
            # The order of each child is its rank by age among all the children (the eldest is the first).
            por_edad = sorted(hermanos, key=lambda h: -h.edad)
            orden_de = {id(h): puesto + 1 for puesto, h in enumerate(por_edad)}
            importe = sum((_del_orden(por_orden, orden_de[id(h)]) * factor_hijo(h) for h in de_la_unidad), ZERO)
        elif por_orden:
            importe = sum((_del_orden(por_orden, orden) for orden in range(1, unidades + 1)), ZERO)
        else:
            assert fijo is not None
            if hijo_a_hijo:
                assert de_la_unidad is not None
                importe = ZERO
                for hijo in de_la_unidad:
                    ajuste = ajuste_de(hijo)
                    propio = ajuste.importe if ajuste is not None and ajuste.importe is not None else fijo
                    importe += propio * factor_hijo(hijo)
            else:
                importe = fijo * (unidades if con_unidad else 1)
        if d.por_contribuyente and conjunta:
            personas = [(inp.edad, inp.grado_discapacidad), *([c] if (c := _conyuge(inp)) is not None else [])]
            importe *= max(1, sum(1 for edad, grado in personas if not _persona_incumple(d, edad, grado)))
        importe *= factor * periodo_importe
        maximo = cifra("maximo")
        if maximo is not None:
            importe = min(importe, maximo)
        if d.maximo_fraccion_cuota is not None and cuota is not None:
            importe = min(importe, d.maximo_fraccion_cuota * cuota)
    else:
        porcentaje: Decimal | None = d.porcentaje
        tramos: list[TramoPorcentaje] | None = d.tramos_porcentaje
        if v is not None and v.tramos_porcentaje is not None:
            porcentaje, tramos = None, v.tramos_porcentaje
        elif v is not None and v.porcentaje is not None:
            porcentaje, tramos = v.porcentaje, None
        de_cada_base = {a.base: a.porcentaje for a in d.bases_adicionales if a.porcentaje is not None}

        def bruto(partes: dict[str, Decimal], base_maxima: Decimal | None) -> Decimal:
            """Percentage (or tiers) of the expenses, the expense capped at ``base_maxima``."""
            total = sum(partes.values(), ZERO)
            if tramos is not None:
                return _por_tramos(tramos, total if base_maxima is None else min(total, base_maxima))
            assert porcentaje is not None
            if base_maxima is not None and total > base_maxima:
                return base_maxima * porcentaje  # a single percentage (the model forbids otherwise)
            return sum((g * de_cada_base.get(b, porcentaje) for b, g in partes.items()), ZERO)

        maximo_por_unidad, maximo = cifra("maximo_por_unidad"), cifra("maximo")
        base_maxima_por_unidad, base_maxima = cifra("base_maxima_por_unidad"), cifra("base_maxima")
        # Every cap shrinks with the income band (when the law says so), the part of the year and the sharing.
        tope = (factor if sobre_tope else UNO) * periodo_tope / (partes_iguales if d.dividir == "maximo" else 1)
        tope_base = periodo_tope / (partes_iguales if d.dividir == "base_maxima" else 1)
        base_global = _minimo(
            base_maxima * tope_base if base_maxima is not None else None,
            (
                d.base_maxima_fraccion * max(ZERO, getattr(bases, d.base_maxima_fraccion_de))
                if d.base_maxima_fraccion is not None
                else None
            ),
        )
        por_unidad = maximo_por_unidad is not None or base_maxima_por_unidad is not None
        con_topes_por_hijo = por_unidad or any(
            a.maximo_por_unidad is not None or a.base_maxima_por_unidad is not None for a in d.ajustes_por_hijo
        )
        if d.unidad_descendientes is not None and unidades_desconocidas and con_topes_por_hijo:
            return hecho(ZERO, FALTA_DETALLE)

        def topes_de(hijo: Any) -> tuple[Decimal | None, Decimal | None]:
            """This child's cap on the deduction and on the expense."""
            ajuste, parte = ajuste_de(hijo), factor_hijo(hijo)
            m = ajuste.maximo_por_unidad if ajuste is not None and ajuste.maximo_por_unidad is not None else None
            b = (
                ajuste.base_maxima_por_unidad
                if ajuste is not None and ajuste.base_maxima_por_unidad is not None
                else None
            )
            m = maximo_por_unidad if m is None else m
            b = base_maxima_por_unidad if b is None else b
            return (m * tope * parte if m is not None else None, b * tope_base * parte if b is not None else None)

        todos = _hijos(inp) if de_hijos and con_topes_por_hijo else None
        # Child by child only when the breakdown accounts for the whole of every expense.
        con_desglose = todos is not None and all(
            abs(sum((h.gastos.get(b, ZERO) for h in todos), ZERO) - g) <= CENT for b, g in gastos.items() if g
        )
        if d.sobre_cuota:
            assert cuota is not None and porcentaje is not None
            importe = cuota * porcentaje
            if con_unidad and maximo_por_unidad is not None:
                importe = min(importe, maximo_por_unidad * max(1, unidades) * tope)
        elif con_desglose:
            # Child by child: each child's expenses against that child's own caps.
            assert todos is not None
            importe, fuera = ZERO, 0
            for hijo in todos:
                suyos = {b: hijo.gastos.get(b, ZERO) for b in gastos}
                if not any(suyos.values()):
                    continue
                if d.unidad_descendientes is not None and not cumple_filtro(hijo, d.unidad_descendientes, ejercicio):
                    fuera += 1
                    continue
                tope_hijo, base_hijo = topes_de(hijo)
                suyo = bruto(suyos, base_hijo)
                importe += suyo if tope_hijo is None else min(suyo, tope_hijo)
            if fuera:
                notas.append(
                    f"no se han contado los gastos de {fuera} descendiente(s) que no cumplen la condición "
                    f"({describe_filtro(d.unidad_descendientes) if d.unidad_descendientes else 'unidad'})"
                )
            if base_global is not None:
                importe = min(importe, bruto({"": base_global}, None))
        else:
            if por_unidad and not unidades and d.si_faltan_unidades == "denegar":
                nombre = (
                    f"descendientes {describe_filtro(d.unidad_descendientes)}".rstrip()
                    if d.unidad_descendientes is not None
                    else f"«{(d.unidad or '').replace('_', ' ')}»"
                )
                return hecho(ZERO, f"hay gasto pero no consta ninguna unidad ({nombre})")
            if hijo_a_hijo and de_la_unidad:
                # Aggregate expense against the sum of each child's own caps (a child without one lifts it).
                topes = [topes_de(h) for h in de_la_unidad]
                tope_unidades = (
                    None if any(m is None for m, _ in topes) else sum((m for m, _ in topes if m is not None), ZERO)
                )
                base_unidades = (
                    None if any(b is None for _, b in topes) else sum((b for _, b in topes if b is not None), ZERO)
                )
            else:
                if hijo_a_hijo:
                    notas.append("ningún descendiente cumple la condición: se ha supuesto uno, todo el año")
                cuantas = max(1, unidades)
                tope_unidades = maximo_por_unidad * cuantas * tope if maximo_por_unidad is not None else None
                base_unidades = (
                    base_maxima_por_unidad * cuantas * tope_base if base_maxima_por_unidad is not None else None
                )
            importe = bruto(gastos, _minimo(base_global, base_unidades))
            if tope_unidades is not None:
                importe = min(importe, tope_unidades)
        if maximo is not None:
            importe = min(importe, maximo * tope)
        if d.maximo_fraccion_cuota is not None and cuota is not None:
            importe = min(importe, d.maximo_fraccion_cuota * cuota)
        if not sobre_tope:
            importe *= factor
        importe *= periodo_importe

    importe *= sobre_limite
    if d.dividir == "importe" and partes_iguales > 1:
        importe /= partes_iguales
    if d.reparto_descendientes and inp.descendientes_compartidos and not conjunta:
        importe *= HALF
    return hecho(importe)


# --------------------------------------------------------------------------- #
# Applying a community's deductions
# --------------------------------------------------------------------------- #


def _mejor_combinacion(deducciones: list[DeduccionCalculable], importes: dict[int, Decimal]) -> dict[int, list[int]]:
    """Of each group of mutually exclusive deductions keep the combination that deducts most.

    ``importes`` holds the amount of every deduction that would be applied, by position.
    Returns the positions dropped, each with the kept ones that exclude it.
    """
    por_articulo: dict[str, list[int]] = {}
    for i in importes:
        por_articulo.setdefault(deducciones[i].articulo, []).append(i)
    vecinos: dict[int, set[int]] = {i: set() for i in importes}
    for i in importes:
        for articulo in deducciones[i].excluyente_con:
            for j in por_articulo.get(articulo, []):
                if j != i:
                    vecinos[i].add(j)
                    vecinos[j].add(i)
    descartadas: dict[int, list[int]] = {}
    vistos: set[int] = set()
    for origen in sorted(importes):
        if origen in vistos or not vecinos[origen]:
            continue
        grupo, pendientes = [], [origen]
        while pendientes:
            i = pendientes.pop()
            if i not in vistos:
                vistos.add(i)
                grupo.append(i)
                pendientes.extend(vecinos[i] - vistos)
        grupo.sort()
        elegidas: list[int] = []
        if len(grupo) <= MAX_GRUPO_EXHAUSTIVO:
            mejor: tuple[Decimal, tuple[bool, ...]] | None = None
            for mascara in range(1, 1 << len(grupo)):
                dentro = [i for k, i in enumerate(grupo) if mascara >> k & 1]
                if any(j in vecinos[i] for i in dentro for j in dentro):
                    continue
                # Ties go to the deductions that come first in the file.
                clave = (sum((importes[i] for i in dentro), ZERO), tuple(i in dentro for i in grupo))
                if mejor is None or clave > mejor:
                    mejor, elegidas = clave, dentro
        else:
            for i in sorted(grupo, key=lambda i: (-importes[i], i)):
                if not vecinos[i] & set(elegidas):
                    elegidas.append(i)
        for i in grupo:
            if i not in elegidas:
                descartadas[i] = sorted(vecinos[i] & set(elegidas))
    return descartadas


def _aviso_recodificada(d: DeduccionCalculable) -> str:
    """The data of ``d`` was written by the re-encoding loop and no person has confirmed it yet."""
    try:
        fecha = date.fromisoformat(d.recodificada_el or "").strftime("%d/%m/%Y")
    except ValueError:
        fecha = d.recodificada_el or "fecha desconocida"
    return (
        f"«{d.titulo}» ({d.articulo}): deducción recodificada automáticamente el {fecha} a partir del BOE; "
        "pendiente de revisión humana."
    )


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
    resultados = [evaluar(d, inp, bases, conjunta=conjunta) for d in deducciones]
    candidatas = {
        i: r.importe.quantize(CENT)
        for i, r in enumerate(resultados)
        if r is not None and r.motivo is None and r.importe.quantize(CENT)
    }
    descartadas = _mejor_combinacion(deducciones, candidatas) if any(d.excluyente_con for d in deducciones) else {}
    usado_del_grupo: dict[str, Decimal] = {}
    sin_detalle: list[str] = []
    for i, (d, resultado) in enumerate(zip(deducciones, resultados, strict=True)):
        if resultado is None:
            continue
        if resultado.falta_detalle:
            sin_detalle.append(d.titulo)
            continue
        excluyentes = sorted(aplicadas & set(d.incompatible_con))
        if excluyentes and resultado.motivo is None:
            aviso(f"Deducción autonómica «{d.titulo}» no aplicada: es incompatible con {', '.join(excluyentes)}.")
            continue
        if resultado.motivo is not None:
            aviso(f"Deducción autonómica «{d.titulo}» no aplicada: {resultado.motivo}.")
            if d.recodificada_el:
                aviso(_aviso_recodificada(d))
            continue
        importe = resultado.importe.quantize(Decimal("0.01"))
        if not importe:
            continue
        if i in descartadas:
            preferidas = ", ".join(
                f"«{deducciones[j].titulo}» ({deducciones[j].articulo}, {candidatas[j]:.2f} €)" for j in descartadas[i]
            )
            aviso(
                f"Deducción autonómica «{d.titulo}» ({d.articulo}, {importe:.2f} €) no aplicada: es incompatible con "
                f"{preferidas}, que te resulta más favorable."
            )
            continue
        conjunto = d.limite_conjunto
        if conjunto is not None:
            cuota = bases.cuota_integra_autonomica
            if conjunto.maximo_fraccion_cuota is not None and cuota is None:
                aviso(f"Deducción autonómica «{d.titulo}» no aplicada: no consta la cuota íntegra autonómica.")
                continue
            tope = _minimo(
                conjunto.maximo,
                (
                    conjunto.maximo_fraccion_cuota * cuota
                    if conjunto.maximo_fraccion_cuota is not None and cuota is not None
                    else None
                ),
            )
            assert tope is not None
            disponible = max(ZERO, tope - usado_del_grupo.get(conjunto.grupo, ZERO)).quantize(CENT)
            if importe > disponible:
                aviso(
                    f"«{d.titulo}»: reducida de {importe:.2f} € a {disponible:.2f} € por el límite conjunto de "
                    f"{tope:.2f} € que comparte con otras deducciones ({conjunto.grupo})."
                )
                importe = disponible
            usado_del_grupo[conjunto.grupo] = usado_del_grupo.get(conjunto.grupo, ZERO) + importe
            if not importe:
                continue
        linea(f"Deducción autonómica: {d.titulo}", -importe, f"{d.articulo} {norma}")
        if resultado.variante:
            aviso(f"«{d.titulo}»: se han aplicado las cifras de la variante «{resultado.variante}».")
        if (d.importes_por_orden or any(v.importes_por_orden for v in d.variantes)) and (
            d.unidad == "hijos_nacidos_en_el_ejercicio"
        ):
            aviso(
                f"«{d.titulo}»: el importe depende del orden del hijo; se ha supuesto que los nacidos en el año son "
                "los menores de los descendientes que has indicado."
            )
        if d.unidad and d.maximo_por_unidad is not None and not getattr(inp, d.unidad):
            aviso(f"«{d.titulo}»: el tope depende de «{d.unidad}», que no consta; se ha supuesto 1.")
        for nota in resultado.avisos:
            aviso(f"«{d.titulo}»: {nota}.")
        if d.recodificada_el:
            aviso(_aviso_recodificada(d))
        if d.requisitos_no_comprobados:
            aviso(
                f"«{d.titulo}» ({d.articulo}) se ha aplicado sin comprobar estos requisitos: "
                + "; ".join(r.rstrip(".") for r in d.requisitos_no_comprobados)
                + "."
            )
        total += importe
        aplicadas.add(d.articulo)
    if sin_detalle:
        aviso(
            "Estas deducciones autonómicas dependen de la edad u otros datos de cada descendiente y no se han podido "
            "comprobar: " + "; ".join(f"«{t}»" for t in sin_detalle) + ". Indica el detalle en «descendientes»."
        )
    return total
