"""Synthetic but realistic household data for the ``luz`` tests (deterministic, no network).

A 4,6 kW flat in the Peninsula: ~2.900 kWh/year, base load at night, morning and evening peaks, more at
weekends' midday, winter heating and some summer air conditioning. Readings have three decimals, like the
distributors' files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import math
import random
from zoneinfo import ZoneInfo

import pytest

from hacienda_gpt.modulos.luz.periodos import hora_local, horas_del_dia

CUPS = "ES0031405912345678JY0F"
TZ = ZoneInfo("Europe/Madrid")


def consumo_hora(dia: date, hora: int, rnd: random.Random) -> Decimal:
    finde = dia.weekday() >= 5
    perfil = [
        0.16,
        0.13,
        0.12,
        0.12,
        0.12,
        0.13,
        0.18,
        0.32,
        0.36,
        0.22,
        0.20,
        0.22,
        0.26,
        0.34,
        0.38,
        0.30,
        0.24,
        0.26,
        0.34,
        0.48,
        0.62,
        0.70,
        0.52,
        0.30,
    ]
    base = perfil[hora] * (1.25 if finde and 10 <= hora <= 15 else 1.0)
    estacion = 1 + 0.35 * math.cos(2 * math.pi * (dia.timetuple().tm_yday - 15) / 365)
    if dia.month in (7, 8) and 14 <= hora <= 19:
        estacion += 0.5  # air conditioning
    valor = base * estacion * rnd.uniform(0.75, 1.25)
    return Decimal(str(round(valor, 3)))


def serie(desde: date, hasta: date, semilla: int = 2026) -> list[tuple[date, int, int, Decimal]]:
    """(date, ordinal hour, local start hour, kWh) for every real hour between two dates."""
    rnd = random.Random(semilla)
    salida = []
    dia = desde
    while dia <= hasta:
        for indice in range(horas_del_dia(dia)):
            hora = hora_local(dia, indice)
            salida.append((dia, indice, hora, consumo_hora(dia, hora, rnd)))
        dia += timedelta(days=1)
    return salida


def csv_datadis(filas: list[tuple[date, int, int, Decimal]]) -> str:
    lineas = ["CUPS;Fecha;Hora;Consumo_kWh;Metodo_obtencion;Energia_vertida_kWh"]
    for dia, indice, _, kwh in filas:
        lineas.append(f"{CUPS};{dia:%Y/%m/%d};{indice + 1:02d}:00;{str(kwh).replace('.', ',')};Real;0")
    return "\n".join(lineas) + "\n"


def csv_distribuidora(filas: list[tuple[date, int, int, Decimal]]) -> str:
    lineas = ["CUPS;Fecha;Hora;AE_kWh;REAL/ESTIMADO"]
    for dia, indice, _, kwh in filas:
        lineas.append(f"{CUPS};{dia:%d/%m/%Y};{indice + 1};{str(kwh).replace('.', ',')};R")
    return "\n".join(lineas) + "\n"


def precio_pvpc(dia: date, hora: int) -> Decimal:
    """Plausible €/MWh of indicator 1001: cheap at night and solar midday, expensive in the evening."""
    curva = [95, 88, 84, 82, 83, 90, 110, 140, 150, 120, 95, 80, 72, 70, 75, 90, 120, 160, 190, 210, 205, 180, 150, 115]
    ajuste = (dia.toordinal() % 7) * 1.5
    return Decimal(str(round(curva[hora] + ajuste, 2)))


def respuesta_esios(desde: date, hasta: date, geo_id: int = 8741, cuartos: bool = False) -> dict:
    valores = []
    dia = desde
    while dia <= hasta:
        inicio = datetime(dia.year, dia.month, dia.day, tzinfo=TZ).astimezone(UTC)
        for indice in range(horas_del_dia(dia)):
            instante = inicio + timedelta(hours=indice)
            local = instante.astimezone(TZ)
            precio = precio_pvpc(dia, local.hour)
            pasos = 4 if cuartos else 1
            for q in range(pasos):
                t = instante + timedelta(minutes=15 * q)
                valores.append(
                    {
                        "value": float(precio + Decimal(q) - Decimal("1.5") if cuartos else precio),
                        "datetime": t.astimezone(TZ).isoformat(timespec="milliseconds"),
                        "datetime_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "tz_time": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "geo_id": geo_id,
                        "geo_name": "Península",
                    }
                )
        dia += timedelta(days=1)
    return {
        "indicator": {"name": "Término de facturación de energía activa del PVPC 2.0TD", "id": 1001, "values": valores}
    }


@pytest.fixture(scope="session")
def anio() -> list[tuple[date, int, int, Decimal]]:
    # From October 2025 to October 2026: both days the clocks go back (25 h) and one forward (23 h).
    return serie(date(2025, 10, 1), date(2026, 10, 31))


@pytest.fixture(scope="session")
def csv_anio(anio: list[tuple[date, int, int, Decimal]]) -> str:
    return csv_datadis(anio)


@pytest.fixture
def fetch_esios() -> Iterator[Callable[[str, dict[str, str]], bytes]]:
    """A fake ESIOS endpoint: answers any date range in the documented format and records the calls."""
    from urllib.parse import parse_qs, urlparse

    llamadas: list[tuple[str, dict[str, str]]] = []

    def fetch(url: str, cabeceras: dict[str, str]) -> bytes:
        llamadas.append((url, dict(cabeceras)))
        q = parse_qs(urlparse(url).query)
        desde = date.fromisoformat(q["start_date"][0][:10])
        hasta = date.fromisoformat(q["end_date"][0][:10])
        return json.dumps(respuesta_esios(desde, hasta, int(q["geo_ids[]"][0]))).encode()

    fetch.llamadas = llamadas  # type: ignore[attr-defined]
    yield fetch


@pytest.fixture(scope="session")
def sint() -> object:
    """The generators above, for tests that need other periods or formats."""
    from types import SimpleNamespace

    return SimpleNamespace(
        serie=serie,
        csv_datadis=csv_datadis,
        csv_distribuidora=csv_distribuidora,
        respuesta_esios=respuesta_esios,
        precio_pvpc=precio_pvpc,
        CUPS=CUPS,
    )
