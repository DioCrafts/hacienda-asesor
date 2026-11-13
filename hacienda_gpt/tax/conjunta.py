"""Joint or separate returns: which costs a married couple less?

Spouses may file one joint return or two individual ones (arts. 82-84 LIRPF).
Joint filing adds a reduction but stacks both incomes on one scale, so it
usually pays only when one spouse earns little. This module builds the three
returns from each spouse's figures and compares them. The joint return's own
rules (art. 84.2) are applied by :func:`calcular_irpf` from what
:func:`_conjunta` hands it: one set of amounts and limits for the summed
income, each member's pension-plan limits (``prevision_social_por_miembro``)
and each spouse's age and disability in the minimum.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import itertools
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict

from hacienda_gpt.tax.irpf import Descendiente, IRPFInput, IRPFResult, PrevisionSocialMiembro, calcular_irpf
from hacienda_gpt.tax.params import IRPFParams
from hacienda_gpt.tax.profile import DatosInvalidosError

# Facts of the household rather than of each spouse (counts of its children and dependants, days of the
# year): the larger of the two figures is taken, not the sum.
_EL_MAYOR = frozenset(
    {
        "num_descendientes",
        "num_descendientes_menores_3",
        "hijos_nacidos_en_el_ejercicio",
        "hijos_nacidos_ultimos_3_ejercicios",
        "hijos_adoptados_en_el_ejercicio",
        "hijos_deduccion_maternidad",
        "num_hijos_gastos_educativos",
        "num_hijos_primer_ciclo_infantil",
        "num_familiares_discapacidad_a_cargo",
        "num_familiares_discapacidad_65_a_cargo",
        "menores_acogidos",
        "num_personas_acogidas_mayores_o_discapacidad",
        "miembros_unidad_familiar",
        "dias_alquiler_vivienda_habitual",
        "dias_acogimiento",
        "dias_desempleo",
    }
)
# One fact both spouses share (a year, a place, a contract, a percentage): the first spouse's, or the
# second's if the first gave none.
_DE_UNO = frozenset(
    {
        "ejercicio",
        "comunidad_autonoma",
        "arrendamiento_fecha_contrato",
        "arrendamiento_variacion_renta",
        "habitantes_municipio",
        "num_declaraciones_mismo_alquiler",
        "num_declaraciones_misma_vivienda",
        "ejercicio_viudedad",
        "ejercicio_adquisicion_vivienda",
        "ejercicio_traslado_residencia",
    }
)
# Set by _conjunta itself.
_APARTE = frozenset(
    {
        "tributacion",
        "descendientes_compartidos",
        "descendientes",
        "prevision_social_por_miembro",
        "base_imponible_resto_unidad_familiar",
        "edad",
        "grado_discapacidad",
        "edad_conyuge",
        "grado_discapacidad_conyuge",
    }
)

# The household's children, copied to a spouse who gave none (the maternity deduction stays with the mother,
# the per-child expenses with whoever gave them).
HIJOS_COMUNES = (
    "num_descendientes",
    "num_descendientes_menores_3",
    "hijos_nacidos_en_el_ejercicio",
    "hijos_nacidos_ultimos_3_ejercicios",
    "hijos_adoptados_en_el_ejercicio",
    "num_hijos_primer_ciclo_infantil",
    "num_hijos_gastos_educativos",
    "familia_numerosa",
    "familia_numerosa_especial",
)

# What identifies a child in ``descendientes`` (everything but the expenses).
_HECHOS_HIJO = tuple(campo for campo in Descendiente.model_fields if campo != "gastos")
# Children of the same age whose pairing is tried exhaustively when both spouses give their detail.
_MAX_HIJOS_MISMA_EDAD = 6


class ComparacionTributacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    individual_a: IRPFResult
    individual_b: IRPFResult
    conjunta: IRPFResult
    # Sum of both individual results and the joint one (positive: to pay; negative: refund).
    total_individual: Decimal
    total_conjunta: Decimal
    mejor: str  # "conjunta" | "individual" | "indiferente"
    ahorro: Decimal
    avisos: list[str]


class HijosComunes(NamedTuple):
    """The spouses' figures for their separate returns, children common to both."""

    a: IRPFInput
    b: IRPFInput
    # Whether that spouse gave no children and was given the other's.
    copiados_a: bool
    copiados_b: bool


def hijos_comunes(a: IRPFInput, b: IRPFInput, *, sin_hijos_a: bool = False, sin_hijos_b: bool = False) -> HijosComunes:
    """Each spouse's figures for a SEPARATE return, the children common to both.

    The minimum for descendants is for each child who lives with the taxpayer (art. 58.1 LIRPF) and,
    «cuando dos o más contribuyentes tengan derecho a la aplicación del mínimo por descendientes [...]
    respecto de los mismos [...] descendientes, su importe se prorrateará entre ellos por partes iguales»
    (art. 61.1.ª): two parents filing separately apply half each (``descendientes_compartidos``, which
    :func:`calcular_irpf` applies to the minimum and to the regional deductions shared between parents).

    A spouse who gives no children gets the other's (counts and detail; the per-child expenses stay with
    whoever gave them), unless ``sin_hijos_*`` says they stated having none: they are then not the
    children's parent, and the other spouse keeps their own ``descendientes_compartidos``.
    """
    copiar_a = not a.num_descendientes and b.num_descendientes > 0 and not sin_hijos_a
    copiar_b = not b.num_descendientes and a.num_descendientes > 0 and not sin_hijos_b
    if copiar_a:
        a = _con_hijos_de(a, b)
    if copiar_b:
        b = _con_hijos_de(b, a)
    if a.num_descendientes and b.num_descendientes:
        a = a.model_copy(update={"descendientes_compartidos": True})
        b = b.model_copy(update={"descendientes_compartidos": True})
    return HijosComunes(a=a, b=b, copiados_a=copiar_a, copiados_b=copiar_b)


def _con_hijos_de(destino: IRPFInput, origen: IRPFInput) -> IRPFInput:
    comunes: dict[str, Any] = {campo: getattr(origen, campo) for campo in HIJOS_COMUNES}
    if origen.descendientes:
        comunes["descendientes"] = [h.model_copy(update={"gastos": {}}) for h in origen.descendientes]
    return destino.model_copy(update=comunes)


def _unir_descendientes(da: list[Descendiente], db: list[Descendiente]) -> list[Descendiente]:
    """One detail of the household's children from the detail both spouses gave.

    They must describe the same children: paired by age, with the same facts (a fact one spouse left
    out is taken from the other) and each child's expenses added up. When it cannot be told which child
    is which (several of the same age with the facts and expenses of both spouses), the merge is refused
    rather than guessed, since the regional caps apply child by child.
    """
    porque = (
        "En la declaración conjunta los hijos son los de la unidad familiar: indica el mismo detalle en los dos "
        "cónyuges (cada uno con lo que haya pagado por cada hijo) o solo en uno de ellos (con los gastos de cada "
        "hijo sumados)."
    )

    def edades(hijos: list[Descendiente]) -> str:
        return ", ".join(str(h.edad) for h in sorted(hijos, key=lambda h: h.edad or 0)) or "ninguno"

    if sorted(h.edad or 0 for h in da) != sorted(h.edad or 0 for h in db):
        raise DatosInvalidosError(
            "Los dos cónyuges han indicado el detalle de sus hijos («descendientes»), pero no describen los mismos "
            f"hijos (edades: {edades(da)} en uno; {edades(db)} en el otro). " + porque
        )
    grupos_a: dict[int, list[int]] = defaultdict(list)
    grupos_b: dict[int, list[Descendiente]] = defaultdict(list)
    for i, hijo in enumerate(da):
        grupos_a[hijo.edad or 0].append(i)
    for hijo in db:
        grupos_b[hijo.edad or 0].append(hijo)
    unidos: dict[int, Descendiente] = {}
    for edad, indices in grupos_a.items():
        if len(indices) > _MAX_HIJOS_MISMA_EDAD:
            raise DatosInvalidosError(
                f"Hay más de {_MAX_HIJOS_MISMA_EDAD} hijos de {edad} años en el detalle de los dos cónyuges: no se "
                "pueden emparejar. " + porque
            )
        posibles: dict[tuple[Any, ...], list[Descendiente]] = {}
        for orden in itertools.permutations(grupos_b[edad]):
            pares = [_unir_hijo(da[i], otro) for i, otro in zip(indices, orden, strict=True)]
            if all(par is not None for par in pares):
                hijos = [par for par in pares if par is not None]
                posibles.setdefault(tuple(sorted(_clave_hijo(h) for h in hijos)), hijos)
        if not posibles:
            raise DatosInvalidosError(
                f"Los datos de {'el hijo' if len(indices) == 1 else 'los hijos'} de {edad} años no coinciden entre "
                "los dos cónyuges (fecha de nacimiento, etapa educativa, discapacidad, adopción…). " + porque
            )
        if len(posibles) > 1:
            raise DatosInvalidosError(
                f"Hay varios hijos de {edad} años y no se puede saber a cuál de ellos corresponde cada dato o gasto "
                "que indica cada cónyuge. " + porque
            )
        (hijos,) = posibles.values()
        unidos.update(zip(indices, hijos, strict=True))
    return [unidos[i] for i in range(len(da))]


def _unir_hijo(x: Descendiente, y: Descendiente) -> Descendiente | None:
    """The same child as given by both spouses, or None if their facts contradict each other."""
    datos: dict[str, Any] = {}
    for campo in _HECHOS_HIJO:
        vx, vy = getattr(x, campo), getattr(y, campo)
        # A fact left at its default (None, 0, False) was not given.
        vacio = Descendiente.model_fields[campo].default
        if vx == vy or vy == vacio:
            datos[campo] = vx
        elif vx == vacio:
            datos[campo] = vy
        else:
            return None
    gastos = {
        campo: x.gastos.get(campo, Decimal(0)) + y.gastos.get(campo, Decimal(0))
        for campo in sorted(set(x.gastos) | set(y.gastos))
    }
    return Descendiente(**datos, gastos=gastos)


def _clave_hijo(hijo: Descendiente) -> tuple[Any, ...]:
    return (
        tuple(str(getattr(hijo, campo)) for campo in _HECHOS_HIJO),
        tuple(sorted((campo, importe) for campo, importe in hijo.gastos.items() if importe)),
    )


def _miembro(x: IRPFInput) -> PrevisionSocialMiembro:
    return PrevisionSocialMiembro(**{campo: getattr(x, campo) for campo in PrevisionSocialMiembro.model_fields})


def _conjunta(a: IRPFInput, b: IRPFInput) -> IRPFInput:
    """The joint return of spouses ``a`` and ``b`` (each with their own figures).

    Incomes, expenses and contributions are added (art. 84.5 LIRPF: «serán gravadas acumuladamente»); the
    household's counts take the larger figure; each spouse's contributions and own income go to
    ``prevision_social_por_miembro`` (art. 84.2.1.º) and the second spouse's age and disability to
    ``edad_conyuge`` / ``grado_discapacidad_conyuge`` (art. 84.2.2.º). Raises :class:`DatosInvalidosError`
    when both give the detail of their children and it does not describe the same children.
    """
    datos: dict[str, Any] = {}
    for nombre in IRPFInput.model_fields:
        if nombre in _APARTE:
            continue
        va, vb = getattr(a, nombre), getattr(b, nombre)
        if nombre in _DE_UNO:
            datos[nombre] = va if va is not None else vb
        elif isinstance(va, bool) or isinstance(vb, bool):
            datos[nombre] = bool(va) or bool(vb)
        elif nombre in _EL_MAYOR:
            datos[nombre] = max(va or 0, vb or 0) if va is not None or vb is not None else None
        elif isinstance(va, Decimal) or isinstance(vb, Decimal):
            datos[nombre] = (va or Decimal(0)) + (vb or Decimal(0)) if va is not None or vb is not None else None
        elif isinstance(va, int) or isinstance(vb, int):
            datos[nombre] = (va or 0) + (vb or 0)
        else:
            datos[nombre] = va if va is not None else vb
    datos["tributacion"] = "conjunta_biparental"
    datos["descendientes_compartidos"] = False
    # Income of the "other members" is now inside the joint return.
    datos["base_imponible_resto_unidad_familiar"] = Decimal(0)
    # Each spouse's personal circumstances (art. 84.2.2.º LIRPF): a's own, and b's as the spouse's.
    datos["edad"] = a.edad if a.edad is not None else b.edad_conyuge
    datos["grado_discapacidad"] = a.grado_discapacidad or b.grado_discapacidad_conyuge
    datos["edad_conyuge"] = b.edad if b.edad is not None else a.edad_conyuge
    datos["grado_discapacidad_conyuge"] = b.grado_discapacidad or a.grado_discapacidad_conyuge
    datos["prevision_social_por_miembro"] = [_miembro(a), _miembro(b)]
    if a.descendientes and b.descendientes:
        datos["descendientes"] = _unir_descendientes(a.descendientes, b.descendientes)
    else:
        datos["descendientes"] = a.descendientes or b.descendientes
    return IRPFInput(**datos)


def comparar_tributacion(
    a: IRPFInput,
    b: IRPFInput,
    params: IRPFParams | None = None,
    *,
    sin_hijos_a: bool = False,
    sin_hijos_b: bool = False,
) -> ComparacionTributacion:
    """Two individual returns against one joint return for spouses ``a`` and ``b``.

    The children either spouse gives are common to both (see :func:`hijos_comunes`; ``sin_hijos_*``: that
    spouse stated having none). In each separate return the other spouse's base imponible is the rest of
    the family unit's, unless one was given.
    """
    comunes = hijos_comunes(a, b, sin_hijos_a=sin_hijos_a, sin_hijos_b=sin_hijos_b)
    sueltas = [x.model_copy(update={"tributacion": "individual"}) for x in (comunes.a, comunes.b)]
    primeras = [calcular_irpf(x, params) for x in sueltas]
    # Regional deductions with a family-income ceiling need the other spouse's base imponible.
    resto = [r.base_imponible_general + r.base_imponible_ahorro for r in reversed(primeras)]
    ind_a, ind_b = (
        (
            primera
            if x.base_imponible_resto_unidad_familiar or not otra
            else calcular_irpf(x.model_copy(update={"base_imponible_resto_unidad_familiar": otra}), params)
        )
        for x, primera, otra in zip(sueltas, primeras, resto, strict=True)
    )
    conjunta = calcular_irpf(_conjunta(comunes.a, comunes.b), params)
    total_individual = ind_a.resultado + ind_b.resultado
    diferencia = total_individual - conjunta.resultado
    mejor = "conjunta" if diferencia > 0 else "individual" if diferencia < 0 else "indiferente"
    avisos: list[str] = []
    if comunes.a.num_descendientes and comunes.b.num_descendientes:
        avisos.append(
            "Los hijos se han tomado como comunes: en las declaraciones individuales cada cónyuge aplica la mitad "
            "del mínimo por descendientes (art. 61.1.ª LIRPF)."
        )
    for copiado, quien, otro in ((comunes.copiados_a, "A", "B"), (comunes.copiados_b, "B", "A")):
        if copiado:
            avisos.append(
                f"El cónyuge {quien} no ha indicado hijos: se han tomado los del cónyuge {otro}. Sus gastos por hijo "
                f"(guardería, colegio…) figuran solo en la declaración del cónyuge {otro}. Si no fueran hijos del "
                f"cónyuge {quien}, su declaración individual no tendría ese mínimo y el cónyuge {otro} lo aplicaría "
                "entero (salvo que lo comparta con el otro progenitor)."
            )
    avisos += [
        "En cada declaración individual, la base imponible del resto de la unidad familiar (para los límites de "
        "renta de las deducciones autonómicas) es la del otro cónyuge, salvo que se haya indicado otra; no constan "
        "rentas de los hijos.",
        "En la conjunta se aplica un solo mínimo del contribuyente, con los incrementos por edad y el mínimo por "
        "discapacidad de cada cónyuge (art. 84.2.2.º LIRPF), y cada cónyuge aplica su propio límite de planes de "
        "pensiones (art. 84.2.1.º LIRPF). Los demás importes y límites (los gastos y la reducción de los "
        "rendimientos del trabajo, los límites de renta de las deducciones estatales) se aplican una sola vez a las "
        "rentas sumadas (art. 84.2 LIRPF); las deducciones autonómicas, con los límites que su ley fija para la "
        "conjunta.",
        "La opción se elige cada año al presentar la declaración y no puede cambiarse después para ese ejercicio.",
    ]
    return ComparacionTributacion(
        individual_a=ind_a,
        individual_b=ind_b,
        conjunta=conjunta,
        total_individual=total_individual,
        total_conjunta=conjunta.resultado,
        mejor=mejor,
        ahorro=abs(diferencia),
        avisos=avisos,
    )
