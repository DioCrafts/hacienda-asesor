"""Electricity offers as the user types them from a contract or a bill (facts ``luz_tarifa_actual`` and
``luz_ofertas``). A free-market offer is user data, not law: the module only checks it is coherent."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreciosEnergia(_Model):
    p1: Decimal = Field(ge=0, le=5)
    p2: Decimal = Field(ge=0, le=5)
    p3: Decimal = Field(ge=0, le=5)


class PreciosPotencia(_Model):
    p1: Decimal = Field(ge=0)
    p2: Decimal = Field(ge=0)


class Oferta(_Model):
    """An electricity offer for a 2.0TD supply.

    ``tipo="fijo"``: prices per kWh (one or three periods) and per kW of power. By default the prices are
    the full prices of the contract (they already include tolls and charges, as Spanish offers are quoted);
    with ``incluye_peajes_cargos=false`` the regulated tolls and charges are added on top.
    ``tipo="pvpc"``: the regulated hourly price of the reference retailers; it needs REE prices.
    """

    nombre: str = Field(min_length=1, max_length=120)
    tipo: Literal["fijo", "pvpc"] = "fijo"
    comercializadora: str | None = None
    energia_eur_kwh: PreciosEnergia | None = None
    potencia_eur_kw_dia: PreciosPotencia | None = None
    potencia_eur_kw_anio: PreciosPotencia | None = None
    incluye_peajes_cargos: bool = True
    descuento_energia_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    descuento_potencia_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    # None = the regulated value per CUPS and year, billed by days (what PVPC bills do).
    bono_social_eur_dia: Decimal | None = Field(default=None, ge=0)
    contador: Literal["monofasico", "trifasico", "propio"] = "monofasico"
    # None = the regulated rental price of the meter type, billed by days.
    alquiler_contador_eur_dia: Decimal | None = Field(default=None, ge=0)
    servicios_eur_mes: Decimal = Field(default=Decimal(0), ge=0)
    precio_valido_hasta: date | None = None
    permanencia_hasta: date | None = None
    penalizacion_eur: Decimal | None = Field(default=None, ge=0)
    notas: str | None = Field(default=None, max_length=500)

    @field_validator("energia_eur_kwh", mode="before")
    @classmethod
    def _precio_unico(cls, v: Any) -> Any:
        if isinstance(v, int | float | str | Decimal) and not isinstance(v, bool):
            return {"p1": v, "p2": v, "p3": v}
        return v

    @model_validator(mode="after")
    def _coherente(self) -> Oferta:
        if self.tipo == "fijo":
            if self.energia_eur_kwh is None:
                raise ValueError("una oferta de precio fijo necesita «energia_eur_kwh» (un precio o p1/p2/p3)")
            if (self.potencia_eur_kw_dia is None) == (self.potencia_eur_kw_anio is None):
                raise ValueError("indica el precio de la potencia en «potencia_eur_kw_dia» o en «potencia_eur_kw_anio»")
        elif self.energia_eur_kwh is not None or self.potencia_eur_kw_dia or self.potencia_eur_kw_anio:
            raise ValueError("una oferta PVPC no lleva precios propios: los fija la regulación y REE")
        return self

    def clave(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.nombre.lower()).strip("-")[:60] or "oferta"

    def a_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=False)


def _error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    donde = ".".join(str(x) for x in err["loc"]) or "oferta"
    return f"{donde}: {err['msg']}"


def validar_oferta(valor: Any) -> dict[str, Any]:
    if not isinstance(valor, Mapping):
        raise ValueError("La tarifa debe ser un objeto JSON con nombre, precios de energía y de potencia.")
    try:
        return Oferta.model_validate(dict(valor)).a_json()
    except ValidationError as exc:
        raise ValueError(f"Tarifa no válida: {_error(exc)}") from exc


def validar_ofertas(valor: Any) -> list[dict[str, Any]]:
    if not isinstance(valor, list):
        raise ValueError("Las ofertas deben ser una lista JSON de tarifas.")
    salida = []
    for i, item in enumerate(valor, start=1):
        try:
            salida.append(validar_oferta(item))
        except ValueError as exc:
            raise ValueError(f"Oferta {i}: {exc}") from exc
    nombres = [o["nombre"] for o in salida]
    if len(set(nombres)) != len(nombres):
        raise ValueError("Hay dos ofertas con el mismo nombre: ponles nombres distintos.")
    return salida


def cargar_oferta(valor: Any) -> Oferta | None:
    if valor in (None, "", {}):
        return None
    return Oferta.model_validate(valor)


def cargar_ofertas(valor: Any) -> list[Oferta]:
    if not valor:
        return []
    return [Oferta.model_validate(v) for v in valor]
