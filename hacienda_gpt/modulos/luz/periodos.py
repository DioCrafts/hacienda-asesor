"""Time periods of the 2.0TD toll (Circular 3/2020, art. 7): which hour is P1, P2 or P3.

An hour is identified by its local date and its ordinal position in that day (0 = the first hour after
midnight). Normal days have 24 hours; the day clocks go forward has 23 and the day they go back has 25,
which is how distributors number them (hours 1-23 / 1-25). Both days are Sundays, so every hour is P3.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from hacienda_gpt.modulos.luz.datos import DatoNoDisponibleError, HorasPeriodo, Periodo3, cargar_regulados

Zona = Literal["peninsula", "baleares", "canarias", "ceuta", "melilla"]
ZONAS: tuple[Zona, ...] = ("peninsula", "baleares", "canarias", "ceuta", "melilla")
_TZ = {
    "peninsula": "Europe/Madrid",
    "baleares": "Europe/Madrid",
    "canarias": "Atlantic/Canary",
    "ceuta": "Africa/Ceuta",
    "melilla": "Africa/Ceuta",
}


def zona_horaria(zona: Zona) -> ZoneInfo:
    return ZoneInfo(_TZ[zona])


@lru_cache(maxsize=64)
def festivos_nacionales(anio: int) -> frozenset[date]:
    """Days that are P3 all day besides weekends: 6 January and the fixed national holidays loaded."""
    datos = cargar_regulados()
    if anio not in datos.anios_festivos():
        raise DatoNoDisponibleError(
            f"No están cargados los festivos nacionales de {anio} (hay {sorted(datos.anios_festivos())}): "
            "hace falta la resolución de fiestas laborales de ese año publicada en el BOE."
        )
    return frozenset({date(anio, 1, 6)} | {f.fecha for f in datos.festivos if f.fecha.year == anio})


def es_dia_valle(dia: date) -> bool:
    """Saturday, Sunday, 6 January or a fixed non-substitutable national holiday: every hour is P3."""
    return dia.weekday() >= 5 or dia in festivos_nacionales(dia.year)


def horas_del_dia(dia: date, zona: Zona = "peninsula") -> int:
    tz = zona_horaria(zona)
    inicio = datetime(dia.year, dia.month, dia.day, tzinfo=tz).astimezone(UTC)
    siguiente = dia + timedelta(days=1)
    fin = datetime(siguiente.year, siguiente.month, siguiente.day, tzinfo=tz).astimezone(UTC)
    return int((fin - inicio).total_seconds() // 3600)


def hora_local(dia: date, indice: int, zona: Zona = "peninsula") -> int:
    """Local clock hour (0-23) at which the ``indice``-th hour of the day starts."""
    tz = zona_horaria(zona)
    inicio = datetime(dia.year, dia.month, dia.day, tzinfo=tz).astimezone(UTC)
    return (inicio + timedelta(hours=indice)).astimezone(tz).hour


def _tabla(zona: Zona) -> HorasPeriodo:
    p = cargar_regulados().periodos_2_0td
    return p.ceuta_melilla if zona in ("ceuta", "melilla") else p.peninsula_baleares_canarias


def periodo_de_hora(hora: int, zona: Zona = "peninsula") -> Periodo3:
    """Period of a working-day hour starting at ``hora`` (local)."""
    tabla = _tabla(zona)
    for periodo in ("p1", "p2", "p3"):
        if any(a <= hora < b for a, b in getattr(tabla, periodo)):
            return periodo  # type: ignore[return-value]
    raise ValueError(f"hora fuera de rango: {hora}")


def periodo_energia(dia: date, indice: int, zona: Zona = "peninsula") -> Periodo3:
    """Energy period (P1 punta, P2 llano, P3 valle) of the ``indice``-th hour of ``dia``."""
    if es_dia_valle(dia):
        return "p3"
    return periodo_de_hora(hora_local(dia, indice, zona), zona)


def periodo_potencia(dia: date, indice: int, zona: Zona = "peninsula") -> Literal["p1", "p2"]:
    """Power period: P1 groups punta and llano, P2 is valle (art. 7.4 Circular 3/2020)."""
    return "p2" if periodo_energia(dia, indice, zona) == "p3" else "p1"


def describir_periodos(zona: Zona = "peninsula") -> str:
    tabla = _tabla(zona)

    def franjas(periodo: str) -> str:
        return ", ".join(f"{a}-{b} h" for a, b in getattr(tabla, periodo))

    return (
        f"Días laborables: punta (P1) {franjas('p1')}; llano (P2) {franjas('p2')}; valle (P3) {franjas('p3')}. "
        "Sábados, domingos, 6 de enero y festivos nacionales de fecha fija: todo el día valle (P3)."
    )
