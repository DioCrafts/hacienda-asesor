"""Which laws the calculator's parameters were read from, and when.

Used to watch those laws for changes: a parameter file is only as current as
the day its source was read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from hacienda_gpt.tax.params import FuentePrincipal, ejercicios_disponibles, load_params


@dataclass(frozen=True)
class NormaParametros:
    boe_id: str
    norma: str
    # None for the state law; the community's key otherwise.
    comunidad: str | None
    comunidad_nombre: str | None
    # The earliest day any parameter file read this law.
    consultado_el: date
    ejercicios: tuple[int, ...]


@lru_cache(maxsize=1)
def normas_de_parametros() -> tuple[NormaParametros, ...]:
    """Every law behind the IRPF parameter files, one entry per BOE id."""
    found: dict[str, NormaParametros] = {}
    for ejercicio in ejercicios_disponibles():
        params = load_params(ejercicio)
        fuentes: list[tuple[FuentePrincipal, str | None, str | None]] = [(params.fuente_principal, None, None)]
        fuentes += [(c.fuente_principal, key, c.nombre) for key, c in params.comunidades.items()]
        for fuente, key, nombre in fuentes:
            consultado = date.fromisoformat(fuente.consultado_el)
            previa = found.get(fuente.boe_id)
            found[fuente.boe_id] = NormaParametros(
                boe_id=fuente.boe_id,
                norma=fuente.norma,
                comunidad=key,
                comunidad_nombre=nombre,
                consultado_el=min(consultado, previa.consultado_el) if previa else consultado,
                ejercicios=(*(previa.ejercicios if previa else ()), ejercicio),
            )
    return tuple(found.values())


def norma_de_parametros(boe_id: str) -> NormaParametros | None:
    return next((n for n in normas_de_parametros() if n.boe_id == boe_id), None)


def comunidad_de(nombre: str | None) -> str | None:
    """Key of the loaded community a free-text name refers to (None when unknown or not loaded)."""
    params = load_params(max(ejercicios_disponibles()))
    comunidad = params.comunidad(nombre)
    return next((key for key, c in params.comunidades.items() if c is comunidad), None)


def desactualizadas(fechas_boe: dict[str, date]) -> list[tuple[NormaParametros, date]]:
    """Laws the BOE has updated after the parameters were read from them."""
    out = [
        (norma, fechas_boe[norma.boe_id])
        for norma in normas_de_parametros()
        if norma.boe_id in fechas_boe and fechas_boe[norma.boe_id] > norma.consultado_el
    ]
    return sorted(out, key=lambda item: item[1], reverse=True)
