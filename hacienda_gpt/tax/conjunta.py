"""Joint or separate returns: which costs a married couple less?

Spouses may file one joint return or two individual ones (arts. 82-84 LIRPF).
Joint filing adds a reduction but stacks both incomes on one scale, so it
usually pays only when one spouse earns little. This module runs the three
returns and compares them; it adds no rule of its own.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult, calcular_irpf
from hacienda_gpt.tax.params import IRPFParams

# Counts that describe the household, not each spouse: the larger figure is taken, not the sum.
_DEL_HOGAR = frozenset(
    {
        "num_descendientes",
        "num_descendientes_menores_3",
        "hijos_nacidos_en_el_ejercicio",
        "hijos_nacidos_ultimos_3_ejercicios",
        "hijos_deduccion_maternidad",
        "num_hijos_gastos_educativos",
        "num_hijos_primer_ciclo_infantil",
        "num_familiares_discapacidad_a_cargo",
    }
)
# Taken from the first spouse.
_DEL_PRIMERO = frozenset(
    {"ejercicio", "comunidad_autonoma", "edad", "grado_discapacidad", "arrendamiento_fecha_contrato"}
)


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


def _conjunta(a: IRPFInput, b: IRPFInput) -> IRPFInput:
    datos: dict[str, Any] = {}
    for nombre in IRPFInput.model_fields:
        va, vb = getattr(a, nombre), getattr(b, nombre)
        if nombre in _DEL_PRIMERO:
            datos[nombre] = va
        elif nombre in ("tributacion", "descendientes_compartidos"):
            continue
        elif isinstance(va, bool) or isinstance(vb, bool):
            datos[nombre] = bool(va) or bool(vb)
        elif nombre in _DEL_HOGAR:
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
    return IRPFInput(**datos)


def comparar_tributacion(a: IRPFInput, b: IRPFInput, params: IRPFParams | None = None) -> ComparacionTributacion:
    """Two individual returns against one joint return for spouses ``a`` and ``b``."""
    hijos = max(a.num_descendientes, b.num_descendientes) > 0
    ind_a = calcular_irpf(
        a.model_copy(update={"tributacion": "individual", "descendientes_compartidos": hijos}), params
    )
    ind_b = calcular_irpf(
        b.model_copy(update={"tributacion": "individual", "descendientes_compartidos": hijos}), params
    )
    conjunta = calcular_irpf(_conjunta(a, b), params)
    total_individual = ind_a.resultado + ind_b.resultado
    diferencia = total_individual - conjunta.resultado
    mejor = "conjunta" if diferencia > 0 else "individual" if diferencia < 0 else "indiferente"
    avisos = [
        "Los hijos se han tomado como comunes: en las declaraciones individuales cada cónyuge aplica la mitad del "
        "mínimo por descendientes.",
        "En la conjunta se usan la edad y la discapacidad del primer cónyuge; si el segundo tiene más de 65 años o "
        "discapacidad, su mínimo no está incluido y la conjunta saldría algo mejor.",
        "Las deducciones con límite por contribuyente (plan de pensiones, alquiler, autonómicas) se han sumado sin "
        "comprobar el límite de cada cónyuge; revisa el detalle si alguna pesa en el resultado.",
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
