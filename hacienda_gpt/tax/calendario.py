"""Fiscal calendar: which filing deadlines apply to a profile, and when.

Recurring deadlines are data (``data/calendario.json``) read from the
regulations that set them. A deadline is only listed when the profile says the
obligation applies — a salaried employee is not shown the quarterly VAT return.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.params import DATA_DIR, Fuente

Aplica = Literal["siempre", "actividad_economica", "actividad_economica_iva_trimestral", "bienes_en_el_extranjero"]


class _Plazo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mes: int = Field(ge=1, le=12)
    dia: int = Field(ge=1, le=31)
    # 0: the deadline falls in the period's own year; 1: in the following year.
    desfase_anual: int = Field(ge=0, le=1)
    periodo: str
    ajustar_inhabil: bool


class _Obligacion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    modelo: str | None
    titulo: str
    detalle: str
    aplica: Aplica
    plazos: list[_Plazo] = Field(min_length=1)
    fuente: Fuente


class _Calendario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    consultado_el: str
    regla_dia_inhabil: Fuente
    obligaciones: list[_Obligacion]


class Vencimiento(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    modelo: str | None
    titulo: str
    periodo: str
    fecha: date
    # True when `fecha` was moved off a weekend from the date the rule states.
    ajustada: bool
    dias_restantes: int
    detalle: str
    referencia: str
    verificado: bool
    nota: str | None = None


@lru_cache(maxsize=1)
def _load() -> _Calendario:
    return _Calendario.model_validate(json.loads((DATA_DIR / "calendario.json").read_text(encoding="utf-8")))


def _tiene_actividad(facts: dict[str, Any]) -> bool:
    return (
        facts.get("alta_actividad_economica") is True
        or facts.get("categoria_contribuyente") == "autonomo"
        or facts.get("regimen_cotizacion") == "RETA"
    )


def _aplica(regla: Aplica, facts: dict[str, Any]) -> bool:
    if regla == "siempre":
        return True
    if regla == "actividad_economica":
        return _tiene_actividad(facts)
    if regla == "actividad_economica_iva_trimestral":
        # Monthly filers (large companies, REDEME) follow another schedule.
        return _tiene_actividad(facts) and facts.get("periodicidad_iva") != "mensual"
    return facts.get("bienes_en_el_extranjero") is True


def _siguiente_habil(day: date) -> date:
    while day.weekday() >= 5:  # Saturday / Sunday
        day += timedelta(days=1)
    return day


def proximos_vencimientos(facts: dict[str, Any], *, hoy: date, dias: int = 365) -> list[Vencimiento]:
    """Deadlines that apply to ``facts`` from ``hoy`` through the next ``dias`` days."""
    calendario = _load()
    limite = hoy + timedelta(days=dias)
    out: list[Vencimiento] = []
    for obligacion in calendario.obligaciones:
        if not _aplica(obligacion.aplica, facts):
            continue
        for plazo in obligacion.plazos:
            # A deadline in year Y belongs to period Y - desfase; scan the periods
            # whose deadline can land inside the window.
            for periodo_anio in range(hoy.year - 1, limite.year + 1):
                try:
                    legal = date(periodo_anio + plazo.desfase_anual, plazo.mes, plazo.dia)
                except ValueError:
                    continue
                fecha = _siguiente_habil(legal) if plazo.ajustar_inhabil else legal
                if not hoy <= fecha <= limite:
                    continue
                notas = [obligacion.fuente.nota] if obligacion.fuente.nota else []
                if fecha != legal:
                    notas.append(f"El {legal.strftime('%d/%m/%Y')} es fin de semana: pasa al siguiente día hábil.")
                out.append(
                    Vencimiento(
                        id=f"{obligacion.id}:{periodo_anio}:{plazo.mes:02d}",
                        modelo=obligacion.modelo,
                        titulo=obligacion.titulo,
                        periodo=plazo.periodo.format(anio=periodo_anio),
                        fecha=fecha,
                        ajustada=fecha != legal,
                        dias_restantes=(fecha - hoy).days,
                        detalle=obligacion.detalle,
                        referencia=obligacion.fuente.referencia,
                        verificado=obligacion.fuente.verificado,
                        nota=" ".join(notas) or None,
                    )
                )
    return sorted(out, key=lambda v: (v.fecha, v.id))


def aviso_festivos() -> str:
    return _load().regla_dia_inhabil.nota or ""
