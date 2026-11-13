"""Adapter for the Base de Datos Nacional de Subvenciones (infosubvenciones.es).

The BDNS publishes, for every call, the exact application window and the kind of beneficiary in a
structured form. Its public API was NOT reachable from the machine this module was written on, so the
adapter is built from the format the BDNS documents for ``/bdnstrans/api/convocatorias?numConv=<id>``
(a JSON object with ``fechaInicioSolicitud``, ``fechaFinSolicitud``, ``abierto``,
``tiposBeneficiarios[].descripcion``, ``regiones[].descripcion``…) and tested with fixtures. Which keys
are read is data (:data:`RUTAS`), so a change of format is a one-line fix; any failure returns None and
the call keeps what the BOE extract says.

It is used only when ``AYUDAS_BDNS=1`` (or a fetcher is injected): no network access by default.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
import json
import os
from typing import Any
import urllib.request

from pydantic import BaseModel, ConfigDict, Field

URL_CONVOCATORIA = "https://www.infosubvenciones.es/bdnstrans/api/convocatorias?numConv={id}"
URL_FICHA = "https://www.infosubvenciones.es/bdnstrans/GE/es/convocatoria/{id}"
_USER_AGENT = "hacienda-asesor/0.1 (ayudas)"

# Field -> path in the BDNS JSON ("*" walks a list).
RUTAS: dict[str, tuple[str, ...]] = {
    "codigo": ("codigoBDNS",),
    "descripcion": ("descripcion",),
    "abierto": ("abierto",),
    "fecha_inicio": ("fechaInicioSolicitud",),
    "fecha_fin": ("fechaFinSolicitud",),
    "texto_fin": ("textFin",),
    "tipos_beneficiarios": ("tiposBeneficiarios", "*", "descripcion"),
    "regiones": ("regiones", "*", "descripcion"),
    "sede": ("sedeElectronica",),
    "presupuesto": ("presupuestoTotal",),
}
# BDNS beneficiary types that mean households (people without an economic activity).
TIPO_PERSONAS = "PERSONAS FÍSICAS QUE NO DESARROLLAN ACTIVIDAD ECONÓMICA"
TIPO_ACTIVIDAD = "PYME Y PERSONAS FÍSICAS QUE DESARROLLAN ACTIVIDAD ECONÓMICA"


class DatosBDNS(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codigo: str
    descripcion: str = ""
    abierto: bool | None = None
    fecha_inicio: date | None = None
    fecha_fin: date | None = None
    texto_fin: str = ""
    tipos_beneficiarios: list[str] = Field(default_factory=list)
    regiones: list[str] = Field(default_factory=list)
    sede: str = ""
    presupuesto: str = ""

    @property
    def para_personas(self) -> bool:
        return any(t.strip().upper() == TIPO_PERSONAS for t in self.tipos_beneficiarios)

    @property
    def solo_entidades(self) -> bool:
        return bool(self.tipos_beneficiarios) and not any(
            t.strip().upper() in {TIPO_PERSONAS, TIPO_ACTIVIDAD} for t in self.tipos_beneficiarios
        )


def _valor(nodo: Any, ruta: tuple[str, ...]) -> Any:
    if not ruta:
        return nodo
    paso, resto = ruta[0], ruta[1:]
    if paso == "*":
        return [v for item in (nodo if isinstance(nodo, list) else []) if (v := _valor(item, resto)) is not None]
    return _valor(nodo.get(paso), resto) if isinstance(nodo, Mapping) else None


def _fecha(valor: Any) -> date | None:
    if not isinstance(valor, str) or len(valor) < 10:
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def leer(payload: Mapping[str, Any]) -> DatosBDNS | None:
    """Parse one call's JSON (a bare object, or a list / ``content`` page with one)."""
    nodo: Any = payload
    if isinstance(nodo, Mapping) and isinstance(nodo.get("content"), list):
        nodo = nodo["content"][0] if nodo["content"] else None
    if isinstance(nodo, list):
        nodo = nodo[0] if nodo else None
    if not isinstance(nodo, Mapping):
        return None
    codigo = _valor(nodo, RUTAS["codigo"])
    if codigo in (None, ""):
        return None
    abierto = _valor(nodo, RUTAS["abierto"])
    return DatosBDNS(
        codigo=str(codigo),
        descripcion=str(_valor(nodo, RUTAS["descripcion"]) or ""),
        abierto=abierto if isinstance(abierto, bool) else None,
        fecha_inicio=_fecha(_valor(nodo, RUTAS["fecha_inicio"])),
        fecha_fin=_fecha(_valor(nodo, RUTAS["fecha_fin"])),
        texto_fin=str(_valor(nodo, RUTAS["texto_fin"]) or ""),
        tipos_beneficiarios=[str(v) for v in _valor(nodo, RUTAS["tipos_beneficiarios"]) or []],
        regiones=[str(v) for v in _valor(nodo, RUTAS["regiones"]) or []],
        sede=str(_valor(nodo, RUTAS["sede"]) or ""),
        presupuesto=str(_valor(nodo, RUTAS["presupuesto"]) or ""),
    )


ObtenerBDNS = Callable[[str], Mapping[str, Any] | None]


def descargar(bdns_id: str, timeout: int = 20) -> Mapping[str, Any] | None:
    """GET the call from the BDNS API; None on any failure (the host may be unreachable)."""
    request = urllib.request.Request(
        URL_CONVOCATORIA.format(id=bdns_id), headers={"Accept": "application/json", "User-Agent": _USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            datos = json.loads(response.read())
    except Exception:
        return None
    return datos if isinstance(datos, Mapping) else ({"content": datos} if isinstance(datos, list) else None)


def activo() -> bool:
    return os.environ.get("AYUDAS_BDNS", "").strip() in {"1", "true"}


def consultar(bdns_id: str, obtener: ObtenerBDNS | None = None) -> DatosBDNS | None:
    """The BDNS data of a call, or None (disabled, unreachable or unreadable)."""
    if obtener is None:
        if not activo():
            return None
        obtener = descargar
    try:
        payload = obtener(bdns_id)
    except Exception:
        return None
    return leer(payload) if payload else None
