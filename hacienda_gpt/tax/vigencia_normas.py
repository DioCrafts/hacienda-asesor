"""Is a decree-law the parameters depend on still standing?

A real decreto-ley is in force from publication but must be validated by
Congress within thirty working days; if it is repealed instead, whatever it
added to the tax code goes with it. Parameters that exist only because of such
a norm say so (``fuente.condicionada_a``), and the calculator asks here before
using them.

The answer comes from a small status file a scheduled check keeps up to date
from the BOE's own record of each norm. With no file, the norm is "pendiente":
the parameter is used and the user is warned.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict

from hacienda_gpt import settings

Estado = Literal["pendiente", "convalidada", "derogada"]


class EstadoNorma(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boe_id: str
    estado: Estado
    # What the BOE says, verbatim, when it says something.
    detalle: str = ""
    comprobado_el: date | None = None


def estado_desde_xml(boe_id: str, xml: str | bytes, *, hoy: date | None = None) -> EstadoNorma:
    """Read validation / repeal from the BOE's XML of the disposition (``/diario_boe/xml.php?id=…``)."""
    root = ElementTree.fromstring(xml)

    def texto(path: str) -> str:
        node = root.find(path)
        return " ".join((node.text or "").split()) if node is not None else ""

    posteriores = [
        (" ".join((p.findtext("palabra") or "").split()), " ".join((p.findtext("texto") or "").split()))
        for p in root.iterfind(".//analisis/referencias/posteriores/posterior")
    ]
    derogacion = next(((p, t) for p, t in posteriores if p.upper().startswith("SE DEROGA")), None)
    if texto(".//metadatos/estatus_derogacion") == "S" or derogacion is not None:
        detalle = " ".join(derogacion) if derogacion else f"derogada el {texto('.//metadatos/fecha_derogacion')}"
        return EstadoNorma(boe_id=boe_id, estado="derogada", detalle=detalle, comprobado_el=hoy)
    convalidacion = next(((p, t) for p, t in posteriores if "convalidaci" in p.lower()), None)
    if convalidacion is not None:
        return EstadoNorma(boe_id=boe_id, estado="convalidada", detalle=" ".join(convalidacion), comprobado_el=hoy)
    return EstadoNorma(boe_id=boe_id, estado="pendiente", comprobado_el=hoy)


def _ruta(path: str | Path | None) -> Path:
    return Path(path or settings.NORM_STATUS_PATH)


_cache: tuple[Path, float, dict[str, EstadoNorma]] | None = None


def leer_estados(path: str | Path | None = None) -> dict[str, EstadoNorma]:
    """The status file, re-read only when it changes on disk."""
    global _cache
    ruta = _ruta(path)
    try:
        mtime = ruta.stat().st_mtime
    except OSError:
        return {}
    if _cache is not None and _cache[0] == ruta and _cache[1] == mtime:
        return _cache[2]
    try:
        raw = json.loads(ruta.read_text(encoding="utf-8"))
        estados = {boe_id: EstadoNorma(**item) for boe_id, item in raw.items()}
    except (OSError, ValueError):
        return {}
    _cache = (ruta, mtime, estados)
    return estados


def guardar_estados(estados: dict[str, EstadoNorma], path: str | Path | None = None) -> None:
    ruta = _ruta(path)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    payload = {boe_id: estado.model_dump(mode="json") for boe_id, estado in sorted(estados.items())}
    ruta.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def estado_de(boe_id: str | None, path: str | Path | None = None) -> EstadoNorma | None:
    """Status of the norm a parameter depends on (None when it depends on none)."""
    if not boe_id:
        return None
    return leer_estados(path).get(boe_id) or EstadoNorma(boe_id=boe_id, estado="pendiente")
