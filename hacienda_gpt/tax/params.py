"""IRPF parameters per fiscal year, loaded from versioned JSON.

Every figure the calculator uses lives in ``data/irpf_<year>.json`` next to the
legal reference it was read from and a ``verificado`` flag. Nothing numeric is
hard-coded in the arithmetic, so a law change is a data change with a diff a
human can review — and an unverified figure is reported as such in every
result that used it.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DATA_DIR = Path(__file__).resolve().parent / "data"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fuente(_Model):
    referencia: str
    en_vigor_desde: str
    verificado: bool
    nota: str | None = None


class FuentePrincipal(_Model):
    norma: str
    boe_id: str
    url: str
    consolidado_a: str
    consultado_el: str
    sha256_html: str


class Tramo(_Model):
    hasta: Decimal | None  # None = "en adelante"
    tipo: Decimal


class Escala(_Model):
    tramos: list[Tramo] = Field(min_length=1)
    fuente: Fuente

    def cuota(self, base: Decimal) -> Decimal:
        """Tax on ``base`` under this progressive scale (unrounded)."""
        total = Decimal(0)
        previous = Decimal(0)
        for tramo in self.tramos:
            if base <= previous:
                break
            upper = base if tramo.hasta is None else min(base, tramo.hasta)
            total += (upper - previous) * tramo.tipo
            if tramo.hasta is None:
                break
            previous = tramo.hasta
        return total


class ReduccionTrabajo(_Model):
    umbral_superior: Decimal
    umbral_inferior: Decimal
    umbral_medio: Decimal
    importe_maximo: Decimal
    pendiente_tramo_1: Decimal
    importe_tramo_2: Decimal
    pendiente_tramo_2: Decimal
    max_otras_rentas: Decimal


class Trabajo(_Model):
    otros_gastos: Decimal
    reduccion: ReduccionTrabajo
    fuente: Fuente


class TributacionConjunta(_Model):
    reduccion_biparental: Decimal
    reduccion_monoparental: Decimal
    fuente: Fuente


class PrevisionSocial(_Model):
    limite_general: Decimal
    incremento_contribucion_empresarial: Decimal
    porcentaje_rendimientos: Decimal
    fuente: Fuente


class Minimos(_Model):
    contribuyente: Decimal
    incremento_mayor_65: Decimal
    incremento_mayor_75: Decimal
    descendientes: list[Decimal] = Field(min_length=1)
    incremento_descendiente_menor_3: Decimal
    ascendiente: Decimal
    incremento_ascendiente_mayor_75: Decimal
    discapacidad: Decimal
    discapacidad_65: Decimal
    gastos_asistencia: Decimal
    fuente: Fuente


class DeduccionTrabajo(_Model):
    umbral_superior: Decimal
    umbral_inferior: Decimal
    importe_maximo: Decimal
    pendiente: Decimal
    max_otras_rentas: Decimal
    fuente: Fuente


class Donativos(_Model):
    tramo_primero: Decimal
    tipo_primero: Decimal
    tipo_resto: Decimal
    tipo_resto_recurrente: Decimal
    limite_base_liquidable: Decimal
    fuente: Fuente


class Maternidad(_Model):
    importe_por_hijo: Decimal
    incremento_guarderia: Decimal
    fuente: Fuente


class ViviendaTransitoria(_Model):
    tipo: Decimal
    base_maxima: Decimal
    fuente: Fuente


class ObligacionDeclarar(_Model):
    limite_trabajo_un_pagador: Decimal
    limite_trabajo_varios_pagadores: Decimal
    limite_segundo_pagador: Decimal
    limite_capital_y_ganancias_con_retencion: Decimal
    fuente: Fuente


class IRPFParams(_Model):
    ejercicio: int
    fuente_principal: FuentePrincipal
    nota_ejercicio: str | None = None
    trabajo: Trabajo
    tributacion_conjunta: TributacionConjunta
    prevision_social: PrevisionSocial
    minimos: Minimos
    escala_general_estatal: Escala
    escala_ahorro_estatal: Escala
    escala_ahorro_autonomica: Escala
    # Keyed by autonomous community code. Each community legislates its own
    # general scale; one that is not loaded here is approximated (and flagged).
    escalas_generales_autonomicas: dict[str, Escala] = Field(default_factory=dict)
    deduccion_rendimientos_trabajo: DeduccionTrabajo | None = None
    donativos: Donativos
    maternidad: Maternidad
    vivienda_habitual_transitoria: ViviendaTransitoria
    obligacion_declarar: ObligacionDeclarar


class EjercicioNoDisponibleError(ValueError):
    """No parameter file exists for the requested fiscal year."""


def ejercicios_disponibles() -> list[int]:
    return sorted(int(path.stem.rsplit("_", 1)[1]) for path in DATA_DIR.glob("irpf_*.json"))


@lru_cache(maxsize=16)
def load_params(ejercicio: int) -> IRPFParams:
    path = DATA_DIR / f"irpf_{ejercicio}.json"
    if not path.exists():
        disponibles = ", ".join(str(year) for year in ejercicios_disponibles())
        raise EjercicioNoDisponibleError(
            f"No hay parámetros del IRPF cargados para {ejercicio} (disponibles: {disponibles})."
        )
    return IRPFParams.model_validate(json.loads(path.read_text(encoding="utf-8")))
