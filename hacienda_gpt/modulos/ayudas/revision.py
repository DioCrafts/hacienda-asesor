"""Daily reading of the BOE for new calls (no CLI): the core's daily job calls
:func:`actualizar_convocatorias` once a day, before running the per-case ``avisos``.

It reads the days not yet read (at most ``dias_max`` back), keeps the candidate calls of sections III
and V-B, reads each one's text and stores the classified result (see ``almacen``). Network access goes
through injectable fetchers, so tests never touch the network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
import urllib.request

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.modulos.ayudas import almacen as almacen_mod
from hacienda_gpt.modulos.ayudas import bdns
from hacienda_gpt.modulos.ayudas.convocatorias import Convocatoria, Plazo, Senal, analizar, candidatas

SUMARIO_URL = "https://www.boe.es/datosabiertos/api/boe/sumario/{dia}"
XML_URL = "https://www.boe.es/diario_boe/xml.php?id={id}"
_USER_AGENT = "hacienda-asesor/0.1 (ayudas)"
DIAS_MAX = 7

SumarioDe = Callable[[date], Mapping[str, Any] | None]
XmlDe = Callable[[str], str | bytes | None]


def _get(url: str, accept: str, timeout: int = 60, intentos: int = 3) -> bytes | None:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": _USER_AGENT})
    for intento in range(intentos):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return bytes(response.read())
        except HTTPError as exc:
            if exc.code == 404:
                return None
            if intento == intentos - 1:
                raise
        except (URLError, ConnectionError, TimeoutError):
            if intento == intentos - 1:
                raise
        time.sleep(2 * (intento + 1))
    return None


def sumario_boe(dia: date) -> Mapping[str, Any] | None:
    """The BOE summary of ``dia`` (None when there was no issue)."""
    raw = _get(SUMARIO_URL.format(dia=dia.strftime("%Y%m%d")), "application/json")
    if not raw:
        return None
    payload = json.loads(raw)
    return payload if isinstance(payload, Mapping) else None


def xml_boe(boe_id: str) -> bytes | None:
    time.sleep(0.3)
    return _get(XML_URL.format(id=boe_id), "application/xml")


class ResumenRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dias_leidos: list[date] = Field(default_factory=list)
    nuevas: list[str] = Field(default_factory=list)
    errores: list[str] = Field(default_factory=list)
    omitida: bool = False


def enriquecer_con_bdns(conv: Convocatoria, datos: bdns.DatosBDNS) -> Convocatoria:
    """Exact deadline and beneficiary type from the BDNS, when available."""
    cambios: dict[str, Any] = {}
    if datos.fecha_fin is not None:
        plazo = Plazo(
            tipo="bdns",
            fin=datos.fecha_fin,
            cita=f"BDNS {datos.codigo}: fechaFinSolicitud {datos.fecha_fin.isoformat()}",
        )
        cambios["plazos"] = sorted([*conv.plazos, plazo], key=lambda p: p.fin)
    if conv.destinatario in ("desconocido", "mixto"):
        if datos.para_personas:
            cambios["destinatario"] = "personas" if not datos.solo_entidades else conv.destinatario
        elif datos.solo_entidades:
            cambios["destinatario"] = "entidades"
        if "destinatario" in cambios and cambios["destinatario"] != conv.destinatario:
            cambios["senales"] = [
                *conv.senales,
                Senal(regla="BDNS tiposBeneficiarios", fragmento="; ".join(datos.tipos_beneficiarios)[:400]),
            ]
    return conv.model_copy(update=cambios) if cambios else conv


def actualizar_convocatorias(
    hoy: date,
    *,
    sumario_de: SumarioDe | None = None,
    xml_de: XmlDe | None = None,
    bdns_de: bdns.ObtenerBDNS | None = None,
    directorio: Any = None,
    dias_max: int = DIAS_MAX,
    forzar: bool = False,
) -> ResumenRevision:
    """Read the BOE days not read yet and store the new calls. Never raises; errors go in the summary."""
    sumario_de = sumario_de or sumario_boe
    xml_de = xml_de or xml_boe
    almacen = almacen_mod.cargar(directorio)
    resumen = ResumenRevision()
    if almacen.ultima_revision == hoy and not forzar:
        resumen.omitida = True
        return resumen
    inicio = hoy - timedelta(days=dias_max - 1)
    if almacen.revisado_hasta is not None:
        inicio = max(inicio, almacen.revisado_hasta + timedelta(days=1))
    dia = inicio
    revisado_hasta = almacen.revisado_hasta
    while dia <= hoy:
        try:
            payload = sumario_de(dia)
        except Exception as exc:  # one bad day must not lose the others; it is retried next run
            resumen.errores.append(f"sumario {dia.isoformat()}: {exc}")
            break
        if payload is not None:
            fallo = False
            for item in candidatas(payload, dia):
                if item.boe_id in almacen.convocatorias:
                    continue
                try:
                    xml = xml_de(item.boe_id)
                except Exception as exc:
                    resumen.errores.append(f"xml {item.boe_id}: {exc}")
                    fallo = True
                    continue
                if not xml:
                    resumen.errores.append(f"xml {item.boe_id}: vacío")
                    fallo = True
                    continue
                conv = analizar(item, xml, hoy)
                if conv.bdns:
                    datos = bdns.consultar(conv.bdns, bdns_de)
                    if datos is not None:
                        conv = enriquecer_con_bdns(conv, datos)
                almacen.convocatorias[conv.boe_id] = conv
                resumen.nuevas.append(conv.boe_id)
            if fallo:
                break
        resumen.dias_leidos.append(dia)
        revisado_hasta = dia
        dia += timedelta(days=1)
    almacen.revisado_hasta = revisado_hasta
    if not resumen.errores:
        # With errors the run is repeated the same day (the failed day was not marked as read).
        almacen.ultima_revision = hoy
    almacen.errores = [*almacen.errores, *(f"{hoy.isoformat()} {e}" for e in resumen.errores)]
    almacen.purgar(hoy)
    try:
        almacen_mod.guardar(almacen, directorio)
    except OSError as exc:
        resumen.errores.append(f"almacén: {exc}")
    return resumen
