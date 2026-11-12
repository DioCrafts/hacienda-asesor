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
from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATA_DIR = Path(__file__).resolve().parent / "data"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fuente(_Model):
    referencia: str
    en_vigor_desde: str
    verificado: bool
    nota: str | None = None
    # BOE id of a decree-law this parameter exists only because of: if Congress
    # repeals it, the parameter goes too (see hacienda_gpt.tax.vigencia_normas).
    condicionada_a: str | None = None
    # Date (AAAAMMDD) of the superseded wording this was read from, when it is not the
    # current text: https://www.boe.es/buscar/act.php?id=<id>&tn=1&p=<date>.
    version_historica: str | None = None


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


class CompensacionAhorroParams(_Model):
    # Share of a positive savings balance that negatives of the other kind may absorb (art. 49.1).
    limite_cruzado: Decimal
    fuente: Fuente


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


BaseLimite = Literal[
    "base_imponible_total",
    "base_imponible_general",
    "base_liquidable_total",
    "base_liquidable_general",
    "base_imponible_total_menos_minimos",
    "base_liquidable_total_menos_minimos",
    "base_imponible_total_menos_minimo_descendientes",
]
# Inputs a data-driven regional deduction may refer to.
CAMPOS_UNIDAD = frozenset(
    {
        "num_descendientes",
        "num_descendientes_menores_3",
        "hijos_nacidos_en_el_ejercicio",
        "hijos_nacidos_ultimos_3_ejercicios",
        "num_ascendientes_mayores_65",
        "num_ascendientes_mayores_75",
        "num_hijos_gastos_educativos",
        "num_hijos_primer_ciclo_infantil",
        "num_familiares_discapacidad_a_cargo",
        "menores_acogidos",
        "hijos_adoptados_en_el_ejercicio",
        # Ascendants or descendants with a disability of 65 % or more who depend on the taxpayer.
        "num_familiares_discapacidad_65_a_cargo",
        # Elderly or disabled people, not close relatives, taken in without payment.
        "num_personas_acogidas_mayores_o_discapacidad",
    }
)
# Units that count the taxpayer's children: their caps can be applied child by child.
CAMPOS_UNIDAD_HIJOS = frozenset(
    {
        "num_descendientes",
        "num_descendientes_menores_3",
        "hijos_nacidos_en_el_ejercicio",
        "hijos_nacidos_ultimos_3_ejercicios",
        "num_hijos_gastos_educativos",
        "num_hijos_primer_ciclo_infantil",
        "hijos_adoptados_en_el_ejercicio",
    }
)
# Broad expense inputs and the narrower, mutually exclusive inputs they are the total of
# (``IRPFInput`` keeps each total at least equal to the sum of its parts).
GASTOS_DESGLOSADOS: dict[str, tuple[str, ...]] = {
    "gastos_eficiencia_energetica": ("gastos_instalaciones_renovables",),
    "gastos_deporte_cultura": ("gastos_deporte", "gastos_cultura"),
    "gastos_salud": (
        "gastos_honorarios_sanitarios",
        "gastos_salud_bucodental",
        "gastos_salud_mental",
        "gastos_optica",
        "gastos_enfermedades_raras",
        "gastos_ela",
    ),
    "inversion_vivienda_habitual": (
        "pagos_adquisicion_vivienda",
        "pagos_rehabilitacion_vivienda",
        "intereses_prestamo_vivienda",
    ),
    "donativos_autonomicos": (
        "donativos_medio_ambiente",
        "donativos_cultura_patrimonio",
        "donativos_investigacion",
        "donativos_cooperacion_fines_sociales",
        "donativos_lengua_propia",
        "donativos_fundaciones_autonomicas",
    ),
}
CAMPOS_GASTO = frozenset(
    {
        "alquiler_vivienda_habitual",
        "gastos_guarderia",
        "gastos_libros_texto",
        "gastos_escolaridad",
        "gastos_ensenanza_idiomas",
        "gastos_uniformes_escolares",
        "cotizaciones_empleada_hogar",
        "gastos_cuidado_familiares",
        "donativos_autonomicos",
        "inversion_vivienda_habitual",
        "inversion_empresas_nueva_creacion",
        "gastos_eficiencia_energetica",
        "gastos_salud",
        "gastos_deporte_cultura",
        "gastos_veterinarios",
        "adquisicion_vehiculo_electrico",
        "inversion_economia_social",
        *(parte for partes in GASTOS_DESGLOSADOS.values() for parte in partes),
    }
)
CAMPOS_REQUISITO = frozenset(
    {
        "familia_numerosa",
        "familia_numerosa_especial",
        "familia_monoparental",
        "municipio_rural_o_despoblacion",
        "viudedad",
        "vivienda_en_municipio_rural",
        "progenitores_trabajan",
        "victima_violencia_genero",
        "victima_terrorismo",
        "vivienda_protegida",
        "vivienda_nueva_construccion",
        "primera_vivienda",
    }
)
# Days of the tax year (declared by the taxpayer) an amount or a cap may be prorated by.
CAMPOS_PERIODO = frozenset({"dias_alquiler_vivienda_habitual", "dias_acogimiento", "dias_desempleo"})
# Per-child periods: days of the year the child is within the ages of ``unidad_descendientes``
# (computed from the date of birth), and months at nursery (declared for each child).
PERIODOS_POR_HIJO = frozenset({"dias_hijo_en_edad", "meses_guarderia_hijo"})
# Number of tax returns a cap is shared between.
CAMPOS_REPARTO = frozenset({"num_declaraciones_mismo_alquiler", "num_declaraciones_misma_vivienda"})
# Tax years in which something happened.
CAMPOS_EJERCICIO = frozenset({"ejercicio_viudedad", "ejercicio_adquisicion_vivienda", "ejercicio_traslado_residencia"})
# Not an expense: a percentage deduction may also be a share of the regional gross tax.
BASE_CUOTA = "cuota_integra_autonomica"

EtapaEducativa = Literal[
    "infantil_primer_ciclo",
    "infantil_segundo_ciclo",
    "primaria",
    "eso",
    "educacion_especial",
    "bachillerato",
    "fp_basica",
    "fp_grado_medio",
    "fp_grado_superior",
    "universidad",
]


class ReduccionProgresiva(_Model):
    """The amount shrinks linearly from 100 % at ``desde`` to nothing at ``hasta``."""

    desde_individual: Decimal
    hasta_individual: Decimal
    desde_conjunta: Decimal | None = None
    hasta_conjunta: Decimal | None = None
    # What shrinks: the deduction itself, or (percentage deductions) its cap.
    aplica_a: Literal["importe", "maximo"] = "importe"


class FiltroDescendientes(_Model):
    """Which of the taxpayer's children count; every bound present must hold.

    Evaluated on ``IRPFInput.descendientes`` (one entry per child). Ages are those
    reached at the accrual date (31 December), inclusive.
    """

    edad_min: int | None = None
    edad_max: int | None = None
    discapacidad_min: int | None = None
    discapacidad_max: int | None = None
    etapas: list[EtapaEducativa] = Field(default_factory=list)  # any of them
    # Tax years since the birth or, for adopted children, the adoption (0 = this year).
    anios_desde_llegada_max: int | None = None
    adoptado: bool | None = None
    adopcion_internacional: bool | None = None
    parto_multiple: bool | None = None  # multiple birth, or several adoptions on the same date

    @property
    def vacio(self) -> bool:
        return self == FiltroDescendientes()


class RequisitoDescendientes(FiltroDescendientes):
    """The number of children matching the filter must be within [minimo, maximo]."""

    minimo: int = 1
    maximo: int | None = None


class Condiciones(_Model):
    """A set of checkable conditions; ALL of the ones present must hold."""

    edad_min: int | None = None  # inclusive, taxpayer's age at accrual
    edad_max: int | None = None
    discapacidad_min: int | None = None  # taxpayer's degree of disability, inclusive
    discapacidad_max: int | None = None
    # Whether the spouse may be the one who meets the age/disability bounds above
    # (one person must meet them all): never, in a joint return, or in any return.
    conyuge: Literal["no", "en_conjunta", "siempre"] = "no"
    # Degree of disability the SPOUSE must have, inclusive.
    conyuge_discapacidad_min: int | None = None
    requiere: list[str] = Field(default_factory=list)  # flags that must be true
    # Counts that must reach / not exceed a number, e.g. {"num_descendientes": 2};
    # also days of the year, e.g. {"dias_desempleo": 183}.
    unidades_min: dict[str, int] = Field(default_factory=dict)
    unidades_max: dict[str, int] = Field(default_factory=dict)
    # Conditions on the children, one by one (ages, disability, school stage…).
    descendientes: list[RequisitoDescendientes] = Field(default_factory=list)
    # Inhabitants of the municipality of residence, inclusive ("menos de 3.000" → 2999).
    habitantes_max: int | None = None
    # Something happened at most N tax years ago (0 = this year), e.g. {"ejercicio_viudedad": 2};
    # or not before a given year, e.g. {"ejercicio_adquisicion_vivienda": 2021}.
    anios_desde_max: dict[str, int] = Field(default_factory=dict)
    ejercicio_min: dict[str, int] = Field(default_factory=dict)
    tributacion: Literal["individual", "conjunta"] | None = None
    # Ceiling on the income named by the deduction's ``base_limite`` (inclusive).
    ingreso_max: Decimal | None = None

    @model_validator(mode="after")
    def _campos_conocidos(self) -> Condiciones:
        if set(self.requiere) - CAMPOS_REQUISITO:
            raise ValueError(f"requisitos desconocidos: {self.requiere}")
        if (set(self.unidades_min) | set(self.unidades_max)) - CAMPOS_UNIDAD - CAMPOS_PERIODO:
            raise ValueError(f"unidades desconocidas: {sorted(set(self.unidades_min) | set(self.unidades_max))}")
        if (set(self.anios_desde_max) | set(self.ejercicio_min)) - CAMPOS_EJERCICIO:
            raise ValueError(f"ejercicios desconocidos: {sorted(set(self.anios_desde_max) | set(self.ejercicio_min))}")
        con_persona = (self.edad_min, self.edad_max, self.discapacidad_min, self.discapacidad_max)
        if self.conyuge != "no" and all(x is None for x in con_persona):
            raise ValueError("«conyuge» solo tiene sentido con una condición de edad o discapacidad")
        return self

    @property
    def vacia(self) -> bool:
        return self == Condiciones()


class TramoPorcentaje(_Model):
    """One tier of a tiered rate: ``porcentaje`` on the expense up to ``hasta`` (None = the rest)."""

    hasta: Decimal | None = None
    porcentaje: Decimal


class Variante(_Model):
    """Different figures for taxpayers who meet ``cuando``; unset figures keep the general ones."""

    descripcion: str
    cuando: Condiciones
    importe: Decimal | None = None
    importes_por_orden: list[Decimal] | None = None
    porcentaje: Decimal | None = None
    tramos_porcentaje: list[TramoPorcentaje] | None = None
    maximo: Decimal | None = None
    # Instead of ``maximo``: euros added to the deduction's general cap ("se incrementará en 100 euros").
    maximo_incremento: Decimal | None = None
    maximo_por_unidad: Decimal | None = None
    base_maxima: Decimal | None = None
    base_maxima_por_unidad: Decimal | None = None
    limite_individual: Decimal | None = None
    limite_conjunta: Decimal | None = None

    @model_validator(mode="after")
    def _con_condicion(self) -> Variante:
        if self.cuando.vacia:
            raise ValueError("una variante necesita al menos una condición")
        if self.maximo is not None and self.maximo_incremento is not None:
            raise ValueError("una variante lleva maximo o maximo_incremento, no los dos")
        return self


class BaseAdicional(_Model):
    """Another expense that feeds the same deduction, under the same caps."""

    base: str
    porcentaje: Decimal | None = None  # None = the deduction's own percentage


class TramoProrrateo(_Model):
    """From ``desde`` days (or months) on, this share of the amount or cap is granted."""

    desde: int
    factor: Decimal


class Prorrateo(_Model):
    """The amount, or its caps, in proportion to part of the year.

    ``dato`` is a number of days declared by the taxpayer (``CAMPOS_PERIODO``) or a
    per-child period (``PERIODOS_POR_HIJO``). Without ``tramos`` the factor is
    dato / total (at most 1), ``total`` defaulting to the days of the year (12 for
    months). With ``tramos`` it is the factor of the last tier reached (0 below the
    first). With ``por_cada_periodo`` the amount is granted once per day/month.
    A per-child period prorates each child's amount (fixed deductions) or cap.
    """

    dato: str
    # "maximo" scales every cap of a percentage deduction (amount and expense caps).
    aplica_a: Literal["importe", "maximo"] = "importe"
    # Only with "dias_hijo_en_edad": the ages (inclusive) whose days are counted,
    # e.g. edad_max 2 = the days of the year on which the child is under three.
    edad_min: int | None = None
    edad_max: int | None = None
    total: int | None = None
    tramos: list[TramoProrrateo] = Field(default_factory=list)
    por_cada_periodo: bool = False
    # When the datum is not known: refuse, or assume the whole year (with a warning).
    si_falta: Literal["denegar", "completo"] = "denegar"

    @model_validator(mode="after")
    def _coherente(self) -> Prorrateo:
        if self.dato not in CAMPOS_PERIODO | PERIODOS_POR_HIJO:
            raise ValueError(f"periodo desconocido: {self.dato}")
        if self.por_cada_periodo and (self.tramos or self.total is not None or self.aplica_a != "importe"):
            raise ValueError("por_cada_periodo no admite tramos, total ni aplicarse al máximo")
        if self.total is not None and self.total <= 0:
            raise ValueError("total de prorrateo no válido")
        if (self.dato == "dias_hijo_en_edad") != (self.edad_min is not None or self.edad_max is not None):
            raise ValueError("dias_hijo_en_edad, y solo él, lleva edad_min o edad_max")
        if [t.desde for t in self.tramos] != sorted({t.desde for t in self.tramos}):
            raise ValueError("los tramos de prorrateo van en orden creciente y sin repetir")
        return self


class AjustePorHijo(_Model):
    """Figures that differ for the children matching ``filtro`` (the first match of each child wins)."""

    filtro: FiltroDescendientes
    importe: Decimal | None = None
    maximo_por_unidad: Decimal | None = None
    base_maxima_por_unidad: Decimal | None = None

    @model_validator(mode="after")
    def _con_filtro(self) -> AjustePorHijo:
        if self.filtro.vacio:
            raise ValueError("un ajuste por hijo necesita un filtro")
        return self


class AjusteLimite(_Model):
    """Income ceilings raised (``factor``) or lifted (``sin_limite``) for those who meet ``cuando``."""

    cuando: Condiciones
    factor: Decimal | None = None
    sin_limite: bool = False

    @model_validator(mode="after")
    def _coherente(self) -> AjusteLimite:
        if self.cuando.vacia:
            raise ValueError("un ajuste de límite necesita al menos una condición")
        if (self.factor is None) == (not self.sin_limite) or (self.factor is not None and self.factor <= 0):
            raise ValueError("un ajuste de límite lleva factor o sin_limite, uno de los dos")
        return self


class LimiteConjunto(_Model):
    """A cap shared by every deduction of the community that names the same ``grupo``."""

    grupo: str
    maximo: Decimal | None = None
    maximo_fraccion_cuota: Decimal | None = None  # of the regional gross tax

    @model_validator(mode="after")
    def _con_tope(self) -> LimiteConjunto:
        if self.maximo is None and self.maximo_fraccion_cuota is None:
            raise ValueError("un límite conjunto necesita un máximo")
        return self


class DeduccionCalculable(Condiciones):
    """A regional deduction simple enough to be computed from data alone.

    Either a fixed amount per unit ("fija") or a percentage of an expense
    ("porcentaje"). Eligibility: the conditions on the deduction itself must
    all hold and, when ``alternativas`` is not empty, at least one of them too.
    ``variantes`` change the figures for some taxpayers: the first one whose
    conditions hold is used. Whatever the calculator cannot check is listed in
    ``requisitos_no_comprobados`` and repeated every time the deduction is applied.
    """

    articulo: str
    titulo: str
    resumen: str
    tipo: Literal["fija", "porcentaje"]
    importe: Decimal | None = None
    # Fixed amount by order (first, second… the last one repeats). With unidad
    # "hijos_nacidos_en_el_ejercicio" the order is that of the child among the
    # taxpayer's children; with ``unidad_descendientes`` it is the child's rank by
    # age among all the children; with any other unit, the first, second… unit.
    importes_por_orden: list[Decimal] | None = None
    unidad: str | None = None
    # The unit is the number of children matching this filter (instead of ``unidad``).
    unidad_descendientes: FiltroDescendientes | None = None
    # An expense is given but the unit count is zero: assume one unit (with a warning) or refuse.
    si_faltan_unidades: Literal["suponer_una", "denegar"] = "suponer_una"
    porcentaje: Decimal | None = None
    # Tiered rate on the expense instead of ``porcentaje`` (e.g. 20 % up to 250 €, 25 % on the rest).
    tramos_porcentaje: list[TramoPorcentaje] | None = None
    # An expense input or ``BASE_CUOTA``.
    base: str | None = None
    bases_adicionales: list[BaseAdicional] = Field(default_factory=list)
    # Cap on the deduction: in euros (also the total of a fixed deduction), per unit,
    # or as a fraction of the regional gross tax.
    maximo: Decimal | None = None
    maximo_por_unidad: Decimal | None = None
    maximo_fraccion_cuota: Decimal | None = None
    # Cap on the expense the percentage applies to: in euros, per unit, or as a
    # fraction of the income named by ``base_maxima_fraccion_de``.
    base_maxima: Decimal | None = None
    base_maxima_por_unidad: Decimal | None = None
    base_maxima_fraccion: Decimal | None = None
    base_maxima_fraccion_de: BaseLimite = "base_liquidable_total"
    base_limite: BaseLimite = "base_imponible_total"
    limite_individual: Decimal | None = None
    limite_conjunta: Decimal | None = None
    # Instead of ``limite_conjunta``: euros added to the individual ceiling in a joint return.
    limite_conjunta_incremento: Decimal | None = None
    limite_es_estricto: bool = False
    # Taxpayers for whom the ceilings above are higher or do not apply (the first match wins).
    ajustes_limite: list[AjusteLimite] = Field(default_factory=list)
    # Above the ceiling the deduction is not lost but multiplied by this (e.g. 0.5).
    factor_sobre_limite: Decimal | None = None
    limite_unidad_familiar: Decimal | None = None
    # Ceiling on the savings taxable base alone, when the law sets one.
    base_ahorro_max: Decimal | None = None
    reduccion_progresiva: ReduccionProgresiva | None = None
    gasto_minimo_sobre_base: Decimal | None = None
    prorrateo: Prorrateo | None = None
    ajustes_por_hijo: list[AjustePorHijo] = Field(default_factory=list)
    reparto_descendientes: bool = False
    # The cap (or the amount, or the expense cap) is shared equally between this many tax returns.
    dividir_entre: str | None = None
    dividir: Literal["maximo", "importe", "base_maxima"] = "maximo"
    # Joint return: a fixed deduction without unit is granted once per spouse who meets
    # the age/disability conditions (needs ``conyuge`` "en_conjunta").
    por_contribuyente: bool = False
    alternativas: list[Condiciones] = Field(default_factory=list)
    variantes: list[Variante] = Field(default_factory=list)
    # Articles of this community's deductions that exclude this one when they have been
    # applied (the first in file order wins). Deprecated: prefer ``excluyente_con``.
    incompatible_con: list[str] = Field(default_factory=list)
    # Articles this one cannot be combined with: of each group of mutually exclusive
    # deductions the calculator keeps the combination that deducts most.
    excluyente_con: list[str] = Field(default_factory=list)
    limite_conjunto: LimiteConjunto | None = None
    requisitos_no_comprobados: list[str] = Field(default_factory=list)
    en_vigor_desde: str
    # Day (AAAA-MM-DD) this entry was re-encoded from the BOE by the automatic loop
    # (hacienda_gpt.tax.recodificacion). While set, nobody has reviewed it yet and the
    # calculator says so every time it uses the deduction; ``params-confirm`` clears it.
    recodificada_el: str | None = None

    @property
    def condiciones(self) -> Condiciones:
        return Condiciones(**{name: getattr(self, name) for name in Condiciones.model_fields})

    @property
    def sobre_cuota(self) -> bool:
        return self.base == BASE_CUOTA

    @property
    def bases(self) -> list[str]:
        """Every expense input that feeds the deduction."""
        return [b for b in (self.base, *(a.base for a in self.bases_adicionales)) if b and b != BASE_CUOTA]

    @model_validator(mode="after")
    def _coherente(self) -> DeduccionCalculable:
        if self.unidad is not None and self.unidad not in CAMPOS_UNIDAD:
            raise ValueError(f"unidad desconocida: {self.unidad}")
        if self.unidad is not None and self.unidad_descendientes is not None:
            raise ValueError("unidad y unidad_descendientes son excluyentes")
        con_unidad = self.unidad is not None or self.unidad_descendientes is not None
        por_hijos = self.unidad_descendientes is not None or self.unidad in CAMPOS_UNIDAD_HIJOS
        if any(a.vacia for a in self.alternativas):
            raise ValueError("una alternativa vacía la cumple cualquiera")
        por_orden = [self.importes_por_orden, *(v.importes_por_orden for v in self.variantes)]
        if any(lista is not None for lista in por_orden) and not con_unidad:
            raise ValueError("importes_por_orden exige una unidad")
        if self.dividir_entre is not None and self.dividir_entre not in CAMPOS_REPARTO:
            raise ValueError(f"reparto desconocido: {self.dividir_entre}")
        if self.ajustes_por_hijo and not por_hijos:
            raise ValueError("ajustes_por_hijo exige una unidad de hijos")
        if self.prorrateo is not None:
            if self.prorrateo.dato in PERIODOS_POR_HIJO and not por_hijos:
                raise ValueError("un prorrateo por hijo exige una unidad de hijos")
            if (
                self.prorrateo.dato in PERIODOS_POR_HIJO
                and self.tipo == "porcentaje"
                and self.prorrateo.aplica_a != "maximo"
            ):
                raise ValueError("en una deducción porcentual el prorrateo por hijo se aplica al máximo")
        if self.limite_conjunta_incremento is not None and (
            self.limite_conjunta is not None or self.limite_individual is None
        ):
            raise ValueError("limite_conjunta_incremento exige limite_individual y excluye limite_conjunta")
        if any(v.maximo_incremento is not None for v in self.variantes) and self.maximo is None:
            raise ValueError("maximo_incremento exige un maximo general")
        if self.factor_sobre_limite is not None and not 0 < self.factor_sobre_limite < 1:
            raise ValueError("factor_sobre_limite fuera de rango")
        if self.por_contribuyente and (self.tipo != "fija" or con_unidad or self.conyuge != "en_conjunta"):
            raise ValueError("por_contribuyente exige una deducción fija sin unidad y conyuge «en_conjunta»")
        fracciones = (self.maximo_fraccion_cuota, self.base_maxima_fraccion)
        if any(f is not None and not 0 < f <= 1 for f in fracciones):
            raise ValueError("fracción fuera de rango")
        if self.tipo == "fija":
            if self.porcentaje is not None or self.base is not None:
                raise ValueError("una deducción fija no lleva porcentaje ni base")
            solo_porcentual = (
                self.tramos_porcentaje,
                self.bases_adicionales,
                self.base_maxima,
                self.base_maxima_por_unidad,
                self.base_maxima_fraccion,
                self.maximo_por_unidad,
            )
            if any(solo_porcentual) or any(
                v.tramos_porcentaje or v.base_maxima or v.base_maxima_por_unidad for v in self.variantes
            ):
                raise ValueError("una deducción fija no lleva tramos, bases ni máximo por unidad")
            if (self.importe is None) == (self.importes_por_orden is None):
                raise ValueError("una deducción fija lleva importe o importes_por_orden, uno de los dos")
            if (self.importe is not None and self.importe <= 0) or (
                self.importes_por_orden is not None and not self.importes_por_orden
            ):
                raise ValueError("importe no válido")
            if any(v.porcentaje is not None for v in self.variantes):
                raise ValueError("una variante de deducción fija no lleva porcentaje")
            if not con_unidad and self.condiciones.vacia and not self.alternativas:
                raise ValueError("una deducción fija sin unidad necesita una condición comprobable")
            if self.prorrateo is not None and self.prorrateo.aplica_a == "maximo":
                raise ValueError("en una deducción fija el prorrateo se aplica al importe")
            if any(a.maximo_por_unidad or a.base_maxima_por_unidad for a in self.ajustes_por_hijo):
                raise ValueError("un ajuste por hijo de una deducción fija solo lleva importe")
        else:
            tramos = [self.tramos_porcentaje, *(v.tramos_porcentaje for v in self.variantes)]
            if (self.porcentaje is None) == (self.tramos_porcentaje is None):
                raise ValueError("una deducción porcentual lleva porcentaje (0-1) o tramos_porcentaje, uno de los dos")
            if self.porcentaje is not None and not 0 < self.porcentaje <= 1:
                raise ValueError("una deducción porcentual lleva porcentaje (0-1) y una base de gasto conocida")
            if self.base not in CAMPOS_GASTO and not self.sobre_cuota:
                raise ValueError("una deducción porcentual lleva porcentaje (0-1) y una base de gasto conocida")
            for escala in tramos:
                if escala is None:
                    continue
                topes = [t.hasta for t in escala]
                if (
                    not escala
                    or topes[-1] is not None
                    or any(t is None for t in topes[:-1])
                    or topes[:-1] != sorted(topes[:-1])  # type: ignore[type-var]
                    or any(not 0 < t.porcentaje <= 1 for t in escala)
                ):
                    raise ValueError("tramos_porcentaje: topes crecientes, el último sin tope, porcentajes en (0, 1]")
            if any(a.base not in CAMPOS_GASTO for a in self.bases_adicionales) or len(set(self.bases)) != len(
                self.bases
            ):
                raise ValueError("bases_adicionales: gastos conocidos y sin repetir")
            if any(a.porcentaje is not None and not 0 < a.porcentaje <= 1 for a in self.bases_adicionales):
                raise ValueError("porcentaje de base adicional fuera de rango")
            con_tope_de_base = (
                self.base_maxima is not None
                or self.base_maxima_por_unidad is not None
                or self.base_maxima_fraccion is not None
                or any(v.base_maxima is not None or v.base_maxima_por_unidad is not None for v in self.variantes)
                or any(a.base_maxima_por_unidad is not None for a in self.ajustes_por_hijo)
            )
            if any(a.porcentaje is not None for a in self.bases_adicionales) and (
                con_tope_de_base or any(t is not None for t in tramos)
            ):
                raise ValueError("bases con porcentajes distintos no admiten base máxima ni tramos")
            if self.sobre_cuota:
                if self.bases_adicionales or con_tope_de_base or any(t is not None for t in tramos):
                    raise ValueError("una deducción sobre la cuota no lleva otras bases, base máxima ni tramos")
                if not con_unidad and self.condiciones.vacia and not self.alternativas:
                    raise ValueError("una deducción sobre la cuota necesita una condición comprobable")
            if self.importe is not None or self.importes_por_orden is not None:
                raise ValueError("una deducción porcentual no lleva importe")
            if any(v.importe is not None or v.importes_por_orden is not None for v in self.variantes):
                raise ValueError("una variante de deducción porcentual no lleva importe")
            if any(a.importe is not None for a in self.ajustes_por_hijo):
                raise ValueError("un ajuste por hijo de una deducción porcentual no lleva importe")
            if any(v.porcentaje is not None and not 0 < v.porcentaje <= 1 for v in self.variantes):
                raise ValueError("porcentaje de variante fuera de rango")
            por_unidad = (
                self.maximo_por_unidad is not None
                or self.base_maxima_por_unidad is not None
                or any(v.maximo_por_unidad or v.base_maxima_por_unidad for v in self.variantes)
            )
            if por_unidad and not con_unidad:
                raise ValueError("maximo_por_unidad exige unidad")
        return self


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
    # Deductions computed generically from their data (see DeduccionCalculable).
    deducciones_calculables: list[DeduccionCalculable] = Field(default_factory=list)
    # Caveats about this community's data that the calculator repeats as warnings.
    notas: list[str] = Field(default_factory=list)


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
    compensacion_ahorro: CompensacionAhorroParams | None = None

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
