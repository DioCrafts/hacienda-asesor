"""The ``hipoteca_prestamo`` fact: what the household tells us about its mortgage (from the deed and the
bank's statements), validated into a :class:`Prestamo`.

Rates are percentages as people write them ("2,5" or "2.5" means 2.5 % nominal a year, TIN). Money is
Decimal. Everything the bank already applied (the rate of each past review, past early repayments) can
be given, so the schedule reproduces the bank's figures instead of re-deriving them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hacienda_gpt.modulos.hipoteca.indices import INDICES


def _decimal(value: Any) -> Any:
    """Accept '2,5', '150.000,00', 2.5 or Decimal; leave anything else to pydantic."""
    if isinstance(value, str):
        texto = value.strip().replace("€", "").replace("%", "").replace(" ", "")
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            return Decimal(texto)
        except InvalidOperation:
            return value
    if isinstance(value, float):
        return Decimal(str(value))
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bonificacion(_Model):
    """A linked product (nómina, seguro…) that lowers the spread while it is kept."""

    concepto: str = Field(min_length=1, max_length=120)
    reduccion_diferencial: Decimal = Field(default=Decimal(0), ge=0, le=5)  # percentage points
    coste_anual: Decimal = Field(default=Decimal(0), ge=0)
    cumplida: bool = True

    _d = field_validator("reduccion_diferencial", "coste_anual", mode="before")(_decimal)


class RevisionConocida(_Model):
    """A rate the bank actually applied from ``fecha`` (its letter or statement)."""

    fecha: date
    tipo: Decimal = Field(ge=-5, le=30)

    _d = field_validator("tipo", mode="before")(_decimal)


class AmortizacionRealizada(_Model):
    fecha: date
    importe: Decimal = Field(gt=0)
    modalidad: Literal["plazo", "cuota"] = "plazo"
    comision_pagada: Decimal = Field(default=Decimal(0), ge=0)

    _d = field_validator("importe", "comision_pagada", mode="before")(_decimal)


class ComisionPactada(_Model):
    """A fee for early repayment the deed sets (a percentage of the amount repaid) for a span of years."""

    porcentaje: Decimal = Field(ge=0, le=10)  # 0.25 means 0.25 %
    desde_anos: int = Field(default=0, ge=0, le=50)
    hasta_anos: int | None = Field(default=None, ge=1, le=50)  # exclusive; None = to the end
    tramo: Literal["todo", "fijo", "variable"] = "todo"

    _d = field_validator("porcentaje", mode="before")(_decimal)


class ComparacionBanco(_Model):
    """What the bank's statement says, to check the schedule against (the module's acceptance test)."""

    fecha: date
    cuota: Decimal | None = Field(default=None, gt=0)
    capital_pendiente: Decimal | None = Field(default=None, ge=0)

    _d = field_validator("cuota", "capital_pendiente", mode="before")(_decimal)


class Prestamo(_Model):
    capital_inicial: Decimal = Field(gt=0, le=Decimal("100000000"))
    fecha_firma: date
    plazo_meses: int = Field(gt=0, le=600)
    tipo: Literal["fijo", "variable", "mixto"] = "variable"
    # Rate applied in the first period (the fixed rate for "fijo", the fixed tranche for "mixto", the
    # initial rate for "variable"), as the deed or the bank shows it, with bonifications already applied.
    tipo_inicial: Decimal = Field(ge=-5, le=30)
    # Months the initial rate lasts: variable -> until the first review; mixto -> the fixed tranche.
    meses_tipo_inicial: int | None = Field(default=None, ge=1, le=600)
    indice: str | None = "euribor_12m"
    diferencial: Decimal | None = Field(default=None, ge=-5, le=20)
    revision_meses: int = Field(default=12, ge=1, le=120)
    # Which index value a review uses: the one of the month N months before the review month
    # ("meses_antes", N = indice_meses_antes), or the last one the BOE had published on the review date.
    indice_regla: Literal["meses_antes", "ultimo_publicado"] = "meses_antes"
    indice_meses_antes: int = Field(default=2, ge=0, le=12)
    # Rounding of the reviewed rate the deed may set (e.g. to the nearest 0.125 point).
    redondeo_tipo_multiplo: Decimal | None = Field(default=None, gt=0, le=1)
    tipo_minimo: Decimal | None = Field(default=None, ge=0, le=30)  # "cláusula suelo" if the deed has one
    tipo_maximo: Decimal | None = Field(default=None, gt=0, le=30)
    periodicidad_meses: Literal[1, 3, 6, 12] = 1
    sistema: Literal["frances"] = "frances"
    fecha_primera_cuota: date | None = None
    bonificaciones: list[Bonificacion] = Field(default_factory=list, max_length=20)
    revisiones_conocidas: list[RevisionConocida] = Field(default_factory=list, max_length=600)
    amortizaciones_realizadas: list[AmortizacionRealizada] = Field(default_factory=list, max_length=200)
    # None: the deed's fee is unknown (the legal maximum is shown). []: the deed sets no fee.
    comisiones_pactadas: list[ComisionPactada] | None = None
    # Ley 41/2007 art. 9: interest-rate-risk compensation the deed sets (percent of the amount repaid).
    compensacion_riesgo_tipo: Decimal | None = Field(default=None, ge=0, le=10)
    vivienda_habitual: bool = True
    adquisicion_antes_2013: bool | None = None
    entidad: str | None = Field(default=None, max_length=120)
    comprobacion_banco: list[ComparacionBanco] = Field(default_factory=list, max_length=50)

    _d = field_validator(
        "capital_inicial",
        "tipo_inicial",
        "diferencial",
        "redondeo_tipo_multiplo",
        "tipo_minimo",
        "tipo_maximo",
        "compensacion_riesgo_tipo",
        mode="before",
    )(_decimal)

    @model_validator(mode="after")
    def _coherente(self) -> Prestamo:
        if self.plazo_meses % self.periodicidad_meses:
            raise ValueError("El plazo en meses debe ser múltiplo de la periodicidad de las cuotas.")
        if self.tipo != "fijo":
            if self.indice is None or self.diferencial is None:
                raise ValueError("Un préstamo variable o mixto necesita índice y diferencial.")
            if self.indice not in INDICES:
                raise ValueError(f"Índice desconocido: {self.indice}. Válidos: {', '.join(INDICES)}.")
            if self.meses_tipo_inicial is None:
                raise ValueError("Indica cuántos meses dura el tipo inicial (meses_tipo_inicial).")
            if self.meses_tipo_inicial >= self.plazo_meses:
                raise ValueError("El tipo inicial no puede durar todo el préstamo; entonces es un préstamo fijo.")
        if self.fecha_primera_cuota is not None and self.fecha_primera_cuota <= self.fecha_firma:
            raise ValueError("La primera cuota debe ser posterior a la firma.")
        return self

    @property
    def num_cuotas(self) -> int:
        return self.plazo_meses // self.periodicidad_meses

    def reduccion_bonificaciones(self) -> Decimal:
        return sum((b.reduccion_diferencial for b in self.bonificaciones if b.cumplida), Decimal(0))

    def coste_bonificaciones(self) -> Decimal:
        return sum((b.coste_anual for b in self.bonificaciones if b.cumplida), Decimal(0))


def validar_prestamo(valor: Any) -> dict[str, Any]:
    """``validar`` of the fact: the canonical JSON of the loan, or ``ValueError`` in Spanish."""
    if not isinstance(valor, dict):
        raise ValueError("hipoteca_prestamo debe ser un objeto JSON con los datos del préstamo.")
    try:
        prestamo = Prestamo.model_validate(valor)
    except ValidationError as exc:
        errores = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'préstamo'}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"Datos de la hipoteca no válidos: {errores}") from exc
    return prestamo.model_dump(mode="json", exclude_defaults=True)


def prestamo_del_perfil(perfil: dict[str, Any] | Any) -> Prestamo | None:
    raw = perfil.get("hipoteca_prestamo") if hasattr(perfil, "get") else None
    if raw is None:
        return None
    return Prestamo.model_validate(raw)
