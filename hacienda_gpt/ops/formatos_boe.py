"""Declarative rules and pure extractors for the BOE formats the app depends on.

What used to be key names and XML paths written inside the parsers is now the
``reglas`` of a versioned spec (``hacienda_gpt/ops/data/formatos/*.json``, see
:mod:`hacienda_gpt.ops.formatos`). The functions here apply those rules and
nothing else: no clock, no network, no project state, so the same call can be
replayed on a stored sample.

Kept free of imports from the tax and proactive packages: the parsers there
import this module.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Annotated, Any
from xml.etree import ElementTree

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from hacienda_gpt.ops.formatos import patron_seguro


class _Reglas(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _a_tupla(value: Any) -> Any:
    return (value,) if isinstance(value, str) else value


# One name, or several alternatives tried in order: a renamed key or a moved element is added
# next to the old one, so that payloads in the previous format are still read the same.
Alias = Annotated[tuple[str, ...], BeforeValidator(_a_tupla), Field(min_length=1)]
# A path from the top of a JSON document: each step is an ``Alias``.
Ruta = tuple[Alias, ...]


def _de(node: Any, alias: tuple[str, ...]) -> Any:
    """``node[name]`` for the first alternative present (None when none is)."""
    if isinstance(node, Mapping):
        for nombre in alias:
            value = node.get(nombre)
            if value is not None:
                return value
    return None


def _lista(value: Any) -> list[Any]:
    """The BOE's JSON gives a bare object where a list has a single element."""
    return value if isinstance(value, list) else [] if value is None else [value]


def _limpio(text: str | None) -> str:
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
# Daily summary: /datosabiertos/api/boe/sumario/<AAAAMMDD> (JSON)
# --------------------------------------------------------------------------- #


class ReglasSumario(_Reglas):
    # Keys from the top of the payload down to the object that holds the issues.
    raiz: Ruta
    diario: Alias
    seccion: Alias
    codigo: Alias
    departamento: Alias
    nombre: Alias
    epigrafe: Alias
    # Nested grouping some departments use between the heading and the items.
    texto: Alias
    item: Alias
    identificador: Alias
    titulo: Alias
    url_html: Alias
    # Section code as published → code the app uses ("1": general dispositions, "3": other dispositions).
    alias_seccion: dict[str, str]
    # Where the API reports its own status, and the value that means success.
    estado: Ruta
    estado_ok: str


def extraer_sumario(
    reglas: ReglasSumario, payload: Mapping[str, Any], secciones: frozenset[str] | None = None
) -> list[dict[str, str]]:
    """Flatten one day's summary into items: boe_id, seccion, departamento, epigrafe, titulo, url."""
    out: list[dict[str, str]] = []

    def recorrer(node: Any, seccion: str, departamento: str, epigrafe: str) -> None:
        if not isinstance(node, dict):
            return
        for item in _lista(_de(node, reglas.item)):
            if isinstance(item, dict) and _de(item, reglas.identificador) and _de(item, reglas.titulo):
                out.append(
                    {
                        "boe_id": str(_de(item, reglas.identificador)),
                        "seccion": seccion,
                        "departamento": departamento,
                        "epigrafe": epigrafe,
                        "titulo": " ".join(str(_de(item, reglas.titulo)).split()),
                        "url": str(_de(item, reglas.url_html) or ""),
                    }
                )
        for sub in _lista(_de(node, reglas.epigrafe)):
            if isinstance(sub, dict):
                recorrer(sub, seccion, departamento, str(_de(sub, reglas.nombre) or epigrafe))
        for sub in _lista(_de(node, reglas.texto)):
            recorrer(sub, seccion, departamento, epigrafe)

    sumario: Any = payload
    for paso in reglas.raiz:
        sumario = _de(sumario, paso) or {}
    for diario in _lista(_de(sumario, reglas.diario)):
        for seccion in _lista(_de(diario, reglas.seccion) if isinstance(diario, dict) else None):
            codigo = str(_de(seccion, reglas.codigo) or "")
            codigo = reglas.alias_seccion.get(codigo, codigo)
            if secciones is not None and codigo not in secciones:
                continue
            for departamento in _lista(_de(seccion, reglas.departamento)):
                if isinstance(departamento, dict):
                    recorrer(departamento, codigo, str(_de(departamento, reglas.nombre) or ""), "")
    return out


def valor_en(payload: Any, ruta: Ruta) -> Any:
    node = payload
    for paso in ruta:
        node = _de(node, paso)
    return node


# --------------------------------------------------------------------------- #
# One disposition: /diario_boe/xml.php?id=<ID> (XML)
# --------------------------------------------------------------------------- #


class ReglasDocumentoXml(_Reglas):
    # ElementTree paths from the document root; several alternatives are tried in order.
    identificador: Alias
    rango: Alias
    origen: Alias
    departamento: Alias
    materia: Alias
    # Earlier norms this one modifies, and later ones that affect it.
    anterior: Alias
    posterior: Alias
    # Inside each reference: the attribute with the referenced id, the action and its text.
    atributo_referencia: Alias
    palabra: Alias
    texto: Alias
    estatus_derogacion: Alias
    fecha_derogacion: Alias
    # Vocabulary of the BOE's analysis.
    valor_derogada: str
    prefijo_deroga: str
    marca_convalidacion: str


def extraer_documento_xml(reglas: ReglasDocumentoXml, xml: str | bytes) -> dict[str, Any]:
    """Metadata and analysis of a disposition. Raises ``ElementTree.ParseError`` on malformed XML."""
    root = ElementTree.fromstring(xml)

    def texto(node: ElementTree.Element, paths: tuple[str, ...]) -> str:
        for path in paths:
            child = node.find(path)
            if child is not None:
                return _limpio(child.text)
        return ""

    def todos(paths: tuple[str, ...]) -> list[ElementTree.Element]:
        for path in paths:
            found = list(root.iterfind(path))
            if found:
                return found
        return []

    def atributo(node: ElementTree.Element, nombres: tuple[str, ...]) -> str:
        return next((value for nombre in nombres if (value := node.get(nombre))), "")

    return {
        "identificador": texto(root, reglas.identificador),
        "rango": texto(root, reglas.rango),
        "origen": texto(root, reglas.origen),
        "departamento": texto(root, reglas.departamento),
        "materias": [_limpio(m.text) for m in todos(reglas.materia) if m.text],
        "anteriores": [
            {
                "referencia": atributo(anterior, reglas.atributo_referencia),
                "palabra": texto(anterior, reglas.palabra),
                "texto": texto(anterior, reglas.texto),
            }
            for anterior in todos(reglas.anterior)
        ],
        "posteriores": [
            {"palabra": texto(posterior, reglas.palabra), "texto": texto(posterior, reglas.texto)}
            for posterior in todos(reglas.posterior)
        ],
        "estatus_derogacion": texto(root, reglas.estatus_derogacion),
        "fecha_derogacion": texto(root, reglas.fecha_derogacion),
    }


# --------------------------------------------------------------------------- #
# Consolidated law metadata: /datosabiertos/api/legislacion-consolidada/id/<ID>/metadatos (JSON)
# --------------------------------------------------------------------------- #


class ReglasMetadatos(_Reglas):
    estado: Ruta
    estado_ok: str
    datos: Alias
    identificador: Alias
    fecha_actualizacion: Alias


def entrada_metadatos(reglas: ReglasMetadatos, payload: Mapping[str, Any], boe_id: str = "") -> dict[str, Any]:
    """The metadata entry of a consolidated law, with ``fecha_actualizacion`` under its usual name.

    Raises RuntimeError when the API does not report success or returns nothing.
    """
    if valor_en(payload, reglas.estado) != reglas.estado_ok:
        raise RuntimeError(f"BOE metadata status != {reglas.estado_ok}: {payload!r}")
    data = _de(payload, reglas.datos) or []
    if not data:
        raise RuntimeError(f"BOE metadata empty payload for {boe_id}: {payload!r}")
    entry: dict[str, Any] = data[0] if isinstance(data, list) else data
    if "fecha_actualizacion" not in entry and _de(entry, reglas.fecha_actualizacion) is not None:
        entry = {**entry, "fecha_actualizacion": _de(entry, reglas.fecha_actualizacion)}
    return entry


def extraer_metadatos(reglas: ReglasMetadatos, texto: str) -> dict[str, Any]:
    """What the app uses of the metadata entry (for samples and the gate)."""
    entry = entrada_metadatos(reglas, json.loads(texto))
    return {
        "identificador": _de(entry, reglas.identificador),
        "fecha_actualizacion": entry.get("fecha_actualizacion"),
    }


# --------------------------------------------------------------------------- #
# Consolidated law page: /buscar/act.php?id=<ID> (HTML, read as text lines)
# --------------------------------------------------------------------------- #


class ReglasConsolidado(_Reglas):
    """Regular expressions applied to the page's text lines (flags inline, e.g. ``(?i)``)."""

    # A line that starts a reported block: an article or a provision.
    encabezado: str
    # A structural heading that ends the previous block without starting one.
    frontera: str
    # "publicado el DD/MM/AAAA[, en vigor a partir del DD/MM/AAAA]": groups 1-3 and 4-6 are day, month, year.
    version: str
    # "Se modifica…", "Se añade…": the BOE's note on what changed.
    nota: str
    # Labels of the page's version selector, not part of the wording.
    interfaz: str
    # Markup the page is expected to carry (not used to extract; a missing one is a warning).
    marcadores: tuple[str, ...]

    @field_validator("encabezado", "frontera", "version", "nota", "interfaz")
    @classmethod
    def _patron(cls, value: str) -> str:
        return patron_seguro(value)

    @field_validator("version")
    @classmethod
    def _seis_grupos(cls, value: str) -> str:
        import re

        if re.compile(value).groups != 6:
            raise ValueError("el patrón de versión debe tener seis grupos: día, mes y año de publicación y de vigor")
        return value
