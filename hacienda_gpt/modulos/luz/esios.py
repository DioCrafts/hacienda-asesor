"""Adapter for the hourly PVPC prices of Red Eléctrica (ESIOS API, indicator 1001).

REE is not reachable from the machine this module was written on: the adapter follows the API format REE
documents (https://api.esios.ree.es) and is tested only with fixtures built from that format.

- ``GET https://api.esios.ree.es/indicators/1001?start_date=…&end_date=…`` with the headers
  ``Accept: application/json; application/vnd.esios-api-v1+json`` and ``x-api-key: <token>``. The token is
  personal (requested from REE by e-mail) and is read from the environment variable ``LUZ_ESIOS_TOKEN``.
- The answer is ``{"indicator": {"id": 1001, "values": [{"value": <€/MWh>, "datetime": "<local ISO>",
  "datetime_utc": "<UTC ISO>", "geo_id": <id>, "geo_name": "<name>"}, …]}}``.
- Indicator 1001 is the "término de facturación de energía activa del PVPC 2.0TD": the hourly energy
  price of the bill, tolls and charges of energy included (art. 8.2 RD 216/2014; REE publishes it the
  day before, around 20:15). Values are €/MWh; the module uses €/kWh.
- Geographic ids (REE): 8741 Península, 8742 Canarias, 8743 Baleares, 8744 Ceuta, 8745 Melilla.

Since the day-ahead market moved to 15-minute products, REE may publish several values per hour: they
are averaged per hour (consumption is hourly), which is an approximation and is reported.

No personal data is ever sent: the request only carries dates, the indicator and the token.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from typing import Any
import urllib.parse
import urllib.request

from hacienda_gpt.modulos.luz.periodos import Zona, zona_horaria

URL = "https://api.esios.ree.es/indicators/1001"
INDICADOR = 1001
GEO_IDS: dict[Zona, int] = {"peninsula": 8741, "canarias": 8742, "baleares": 8743, "ceuta": 8744, "melilla": 8745}
TOKEN_ENV = "LUZ_ESIOS_TOKEN"
Fetch = Callable[[str, Mapping[str, str]], bytes]


class PreciosNoDisponiblesError(RuntimeError):
    """Prices could not be obtained. The message is Spanish, for the user."""


def url_consulta(desde: date, hasta: date, zona: Zona = "peninsula") -> str:
    params = {
        "start_date": f"{desde.isoformat()}T00:00:00",
        "end_date": f"{hasta.isoformat()}T23:59:59",
        "geo_ids[]": str(GEO_IDS[zona]),
    }
    return f"{URL}?{urllib.parse.urlencode(params)}"


def _fetch_http(url: str, cabeceras: Mapping[str, str]) -> bytes:  # pragma: no cover - network
    peticion = urllib.request.Request(url, headers=dict(cabeceras))
    with urllib.request.urlopen(peticion, timeout=20) as respuesta:
        return bytes(respuesta.read())


def interpretar(
    respuesta: Any, zona: Zona = "peninsula", avisos: list[str] | None = None
) -> dict[tuple[date, int], Decimal]:
    """(local date, ordinal hour of the day) -> €/kWh from an indicator-1001 answer (dict or JSON text)."""
    if isinstance(respuesta, bytes | str):
        try:
            respuesta = json.loads(respuesta)
        except json.JSONDecodeError as exc:
            raise PreciosNoDisponiblesError("La respuesta de precios no es un JSON válido.") from exc
    try:
        valores = respuesta["indicator"]["values"]
        id_indicador = respuesta["indicator"].get("id", INDICADOR)
    except (KeyError, TypeError) as exc:
        raise PreciosNoDisponiblesError("La respuesta no tiene el formato de un indicador de ESIOS.") from exc
    if id_indicador != INDICADOR:
        raise PreciosNoDisponiblesError(f"Se esperaba el indicador 1001 (PVPC 2.0TD) y llegó el {id_indicador}.")
    tz = zona_horaria(zona)
    geo = GEO_IDS[zona]
    grupos: dict[datetime, list[Decimal]] = defaultdict(list)
    for v in valores:
        if v.get("geo_id", geo) != geo:
            continue
        try:
            instante = datetime.fromisoformat(str(v["datetime_utc"]).replace("Z", "+00:00"))
            precio = Decimal(str(v["value"])) / 1000
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise PreciosNoDisponiblesError(f"Valor de precio ilegible: {v}") from exc
        hora_utc = instante.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        grupos[hora_utc].append(precio)
    salida: dict[tuple[date, int], Decimal] = {}
    subhorario = False
    for hora_utc, precios in sorted(grupos.items()):
        local = hora_utc.astimezone(tz)
        dia = local.date()
        medianoche = datetime(dia.year, dia.month, dia.day, tzinfo=tz).astimezone(UTC)
        indice = int((hora_utc - medianoche).total_seconds() // 3600)
        if len(precios) > 1:
            subhorario = True
        salida[(dia, indice)] = sum(precios, Decimal(0)) / len(precios)
    if subhorario and avisos is not None:
        avisos.append("REE publica varios precios por hora (cuartos de hora): se ha usado la media de cada hora.")
    return salida


def obtener_precios(
    desde: date,
    hasta: date,
    zona: Zona = "peninsula",
    fetch: Fetch | None = None,
    token: str | None = None,
    avisos: list[str] | None = None,
) -> dict[tuple[date, int], Decimal]:
    """Hourly PVPC prices between two local dates (inclusive), from ESIOS."""
    token = token or os.environ.get(TOKEN_ENV)
    if not token:
        raise PreciosNoDisponiblesError(
            "No hay acceso a los precios horarios del PVPC: falta el token de la API de REE (variable "
            f"{TOKEN_ENV}). También puedes pegar la respuesta JSON del indicador 1001 de ESIOS."
        )
    cabeceras = {
        "Accept": "application/json; application/vnd.esios-api-v1+json",
        "Content-Type": "application/json",
        "x-api-key": token,
    }
    try:
        cuerpo = (fetch or _fetch_http)(url_consulta(desde, hasta, zona), cabeceras)
    except PreciosNoDisponiblesError:
        raise
    except Exception as exc:  # network errors of any kind become a readable message
        raise PreciosNoDisponiblesError(f"No se han podido descargar los precios de REE ({exc}).") from exc
    precios = interpretar(cuerpo, zona, avisos)
    dias = {desde + timedelta(days=i) for i in range((hasta - desde).days + 1)}
    precios = {k: v for k, v in precios.items() if k[0] in dias}
    if not precios:
        raise PreciosNoDisponiblesError(
            f"REE aún no ha publicado precios para {desde:%d-%m-%Y}"
            + (f" a {hasta:%d-%m-%Y}" if hasta != desde else "")
            + " (los del día siguiente salen hacia las 20:15)."
        )
    return precios
