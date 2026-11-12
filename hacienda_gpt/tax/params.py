"""IRPF parameters per fiscal year, loaded from versioned JSON.

Every figure the calculator uses lives in ``data/irpf_<year>.json`` next to the
legal reference it was read from and a ``verificado`` flag. Nothing numeric is
hard-coded in the arithmetic, so a law change is a data change with a diff a
human can review — and an unverified figure is reported as such in every
result that used it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
import unicodedata

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
    # The cumulative tax the law prints for the start of this bracket ("cuota
    # íntegra" column). When given it is used as printed — published scales
    # round it to the cent, and recomputing it from the rates can differ by one.
    cuota_hasta_inicio: Decimal | None = None


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
            if tramo.cuota_hasta_inicio is not None:
                total = tramo.cuota_hasta_inicio
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


class AlquilerViviendaHabitual(_Model):
    """State deduction for renting the main home (art. 68.6 LIRPF)."""

    porcentaje: Decimal
    base_maxima: Decimal
    # Up to this taxable base the full maximum base applies...
    umbral_inferior: Decimal
    # ...from this one there is no deduction; in between the maximum base shrinks.
    umbral_superior: Decimal
    pendiente: Decimal
    fuente: Fuente


class _Arrendamiento2021(_Model):
    porcentaje: Decimal
    fuente: Fuente


class _Arrendamiento2023(_Model):
    rebaja_zona_tensionada: Decimal
    joven_o_social: Decimal
    rehabilitada: Decimal
    resto: Decimal
    rebaja_minima: Decimal  # percent the rent must drop by, exclusive
    fuente: Fuente


class _Arrendamiento2026(_Model):
    # Applies to contracts signed after this date (transitional provision 38).
    contratos_posteriores_a: date
    rebaja_minima: Decimal
    maxima: Decimal
    zona_tensionada_joven: Decimal
    zona_tensionada: Decimal
    joven: Decimal
    rebaja_resto: Decimal
    renta_no_superior: Decimal
    # (rent increase up to, in percent; reduction). None = any larger increase.
    incrementos: list[tuple[Decimal | None, Decimal]]
    primer_alquiler_sobre_indice: Decimal
    social: Decimal
    rehabilitada: Decimal
    prorroga_tacita: Decimal
    fuente: Fuente


class ReduccionArrendamiento(_Model):
    """Reduction of net income from letting a dwelling (art. 23.2 LIRPF), by contract date."""

    fecha_ley_vivienda: date
    regimen_2021: _Arrendamiento2021
    regimen_2023: _Arrendamiento2023
    regimen_2026: _Arrendamiento2026 | None = None


class ObligacionDeclarar(_Model):
    limite_trabajo_un_pagador: Decimal
    limite_trabajo_varios_pagadores: Decimal
    limite_segundo_pagador: Decimal
    limite_capital_y_ganancias_con_retencion: Decimal
    fuente: Fuente


class DeduccionAutonomica(_Model):
    """A regional deduction, summarised from the article that creates it, for
    the benefit catalogue."""

    articulo: str
    titulo: str
    resumen: str
    limites: str
    # Income ceilings (general + savings taxable base), when the law sets a plain one.
    limite_base_individual: Decimal | None = None
    limite_base_conjunta: Decimal | None = None
    en_vigor_desde: str


class _MadridNacimiento(_Model):
    importe_por_hijo: Decimal
    limite_base_individual: Decimal
    limite_base_conjunta: Decimal
    limite_base_unidad_familiar: Decimal
    fuente: Fuente


class _MadridAlquiler(_Model):
    porcentaje: Decimal
    maximo: Decimal
    edad_maxima_exclusiva: int
    porcentaje_minimo_sobre_base: Decimal
    limite_base_individual: Decimal
    limite_base_conjunta: Decimal
    limite_base_unidad_familiar: Decimal
    fuente: Fuente


class _MadridEducacion(_Model):
    porcentaje_escolaridad_idiomas: Decimal
    porcentaje_vestuario: Decimal
    maximo_por_hijo: Decimal
    maximo_por_hijo_con_escolaridad: Decimal
    maximo_por_hijo_primer_ciclo_infantil: Decimal
    limite_base_por_miembro: Decimal
    fuente: Fuente


class _MadridCuidado(_Model):
    porcentaje: Decimal
    maximo: Decimal
    porcentaje_familia_numerosa: Decimal
    maximo_familia_numerosa: Decimal
    limite_base_por_miembro: Decimal
    fuente: Fuente


class _MadridDonativos(_Model):
    porcentaje: Decimal
    limite_base_liquidable: Decimal
    fuente: Fuente


class _MadridFamilias(_Model):
    porcentaje: Decimal
    minimo_descendientes: int
    limite_suma_bases: Decimal
    fuente: Fuente


class DeduccionesMadrid(_Model):
    """Parameters of the Madrid deductions the calculator applies."""

    nacimiento: _MadridNacimiento
    alquiler: _MadridAlquiler
    gastos_educativos: _MadridEducacion
    cuidado: _MadridCuidado
    donativos: _MadridDonativos
    familias_ingresos_reducidos: _MadridFamilias


class RepartoDeducciones(_Model):
    # Share of the general state deductions (donations, housing) borne by the
    # regional tax; the rest reduces the state tax.
    parte_autonomica: Decimal
    fuente: Fuente


class Comunidad(_Model):
    """What an autonomous community legislates for its share of the IRPF."""

    nombre: str
    alias: list[str] = Field(default_factory=list)
    fuente_principal: FuentePrincipal
    escala_general: Escala
    # Regional personal/family minimums (art. 56.3 LIRPF); None = the state ones apply.
    minimos: Minimos | None = None
    deducciones: list[DeduccionAutonomica] = Field(default_factory=list)
    # Deductions the calculator subtracts for this community (Madrid only, so far).
    deducciones_madrid: DeduccionesMadrid | None = None
    # Other deductions the community has, by article and title only.
    otras_deducciones: list[str] = Field(default_factory=list)


def normalizar_comunidad(nombre: str) -> str:
    """'Comunidad de Madrid ' → 'comunidad de madrid' (case/accents/spacing-insensitive)."""
    decomposed = unicodedata.normalize("NFKD", nombre)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.lower().split())


class IRPFParams(_Model):
    ejercicio: int
    fuente_principal: FuentePrincipal
    nota_ejercicio: str | None = None
    # Amending norms these parameters have been checked against, and what came of it.
    revisiones: list[dict[str, str]] = Field(default_factory=list)
    trabajo: Trabajo
    tributacion_conjunta: TributacionConjunta
    prevision_social: PrevisionSocial
    minimos: Minimos
    escala_general_estatal: Escala
    escala_ahorro_estatal: Escala
    escala_ahorro_autonomica: Escala
    # Each community legislates its own general scale, minimums and deductions;
    # one that is not loaded here is approximated by the calculator (and flagged).
    comunidades: dict[str, Comunidad] = Field(default_factory=dict)
    reparto_deducciones_estatales: RepartoDeducciones
    deduccion_rendimientos_trabajo: DeduccionTrabajo | None = None
    donativos: Donativos
    maternidad: Maternidad
    vivienda_habitual_transitoria: ViviendaTransitoria
    # None = the deduction does not exist in that year.
    alquiler_vivienda_habitual: AlquilerViviendaHabitual | None = None
    obligacion_declarar: ObligacionDeclarar
    reduccion_arrendamiento: ReduccionArrendamiento | None = None

    def comunidad(self, nombre: str | None) -> Comunidad | None:
        """Look a community up by key or any of its aliases."""
        if not nombre:
            return None
        wanted = normalizar_comunidad(nombre)
        for key, comunidad in self.comunidades.items():
            if wanted == key or wanted in {normalizar_comunidad(alias) for alias in comunidad.alias}:
                return comunidad
        return None


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
