"""The household modules' sources, for the maintenance loop (like the tax laws in ``tax/fuentes.py``).

Every :class:`FuenteModulo` whose identifier (or URL) names a BOE document joins the BOE watch:

* ``boe-novedades`` treats it as a watched law (a new provision that amends it is detected);
* ``params-check`` asks the BOE when the consolidated text was last updated and lists the module's law for
  re-reading when that date is later than the module's reading. The contract's ``FuenteModulo`` carries no
  reading date yet, so the first date seen is kept as the baseline in ``MODULOS_FUENTES_STATE_PATH``; a
  ``consultado_el`` attribute on the source, when a module provides one, takes precedence.

Sources outside the BOE (REE, Banco de España…) are listed for the operator; their formats are watched by
each module's own adapter, since the core cannot know how to parse them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from typing import Any

from hacienda_gpt import settings
from hacienda_gpt.modulos.registro import FISCAL, Registro, registro

_BOE_ID = re.compile(r"BOE-[A-Z]-\d{4}-\d+")


@dataclass(frozen=True)
class FuenteVigilada:
    modulo: str
    identificador: str
    descripcion: str
    url: str
    boe_id: str | None
    consultado_el: date | None


def _fecha(valor: Any) -> date | None:
    if isinstance(valor, date):
        return valor
    if isinstance(valor, str):
        try:
            return date.fromisoformat(valor[:10])
        except ValueError:
            return None
    return None


def fuentes_de_modulos(reg: Registro | None = None) -> list[FuenteVigilada]:
    """Sources of every registered household module (the tax module's are in ``tax/fuentes.py``)."""
    out: list[FuenteVigilada] = []
    for modulo in (reg or registro()).modulos.values():
        if modulo.clave == FISCAL:
            continue
        for fuente in modulo.fuentes:
            encontrado = _BOE_ID.search(fuente.identificador) or _BOE_ID.search(fuente.url or "")
            out.append(
                FuenteVigilada(
                    modulo=modulo.clave,
                    identificador=fuente.identificador,
                    descripcion=fuente.descripcion,
                    url=fuente.url,
                    boe_id=encontrado.group(0) if encontrado else None,
                    consultado_el=_fecha(getattr(fuente, "consultado_el", None)),
                )
            )
    return out


def boe_ids_de_modulos(reg: Registro | None = None) -> frozenset[str]:
    return frozenset(f.boe_id for f in fuentes_de_modulos(reg) if f.boe_id)


def leer_estado(path: str | Path | None = None) -> dict[str, Any]:
    ruta = Path(path or settings.MODULOS_FUENTES_STATE_PATH)
    if not ruta.exists():
        return {"fuentes": {}}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fuentes": {}}
    return datos if isinstance(datos, dict) and isinstance(datos.get("fuentes"), dict) else {"fuentes": {}}


def guardar_estado(estado: Mapping[str, Any], path: str | Path | None = None) -> None:
    ruta = Path(path or settings.MODULOS_FUENTES_STATE_PATH)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(ruta)


def revisar(
    fechas_boe: Mapping[str, date],
    estado: Mapping[str, Any],
    *,
    fuentes: list[FuenteVigilada],
    marcar_revisadas: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(module laws to re-read, new state). A law seen for the first time sets its baseline and is not listed.

    With ``marcar_revisadas`` the operator says the modules were re-read: every baseline moves to the BOE's date.
    """
    lineas: dict[str, Any] = dict(estado.get("fuentes", {}))
    pendientes: list[dict[str, Any]] = []
    for fuente in fuentes:
        if fuente.boe_id is None or fuente.boe_id not in fechas_boe:
            continue
        fecha = fechas_boe[fuente.boe_id]
        registrada = _fecha((lineas.get(fuente.boe_id) or {}).get("linea_base"))
        if registrada is None or marcar_revisadas:
            lineas[fuente.boe_id] = {"linea_base": fecha.isoformat()}
            registrada = fecha
        referencia = fuente.consultado_el or registrada
        if fecha > referencia:
            pendientes.append(
                {
                    "modulo": fuente.modulo,
                    "boe_id": fuente.boe_id,
                    "norma": fuente.descripcion,
                    "leida_el": referencia.isoformat(),
                    "actualizada_en_boe_el": fecha.isoformat(),
                }
            )
    return pendientes, {**dict(estado), "fuentes": lineas}


__all__ = [
    "FuenteVigilada",
    "boe_ids_de_modulos",
    "fuentes_de_modulos",
    "guardar_estado",
    "leer_estado",
    "revisar",
]
