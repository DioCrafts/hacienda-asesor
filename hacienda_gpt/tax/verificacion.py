"""Re-verify the calculator's parameters against the current text of each law.

Every figure in the parameter files was read from a law on boe.es. This module
turns that provenance into a test that can be re-run at any time: for each
group of parameters it builds a check against the law's current consolidated
text, so when a law changes the maintainer learns WHICH parameters no longer
match instead of having to re-read sixteen laws.

Three kinds of check, from strongest to weakest:

* ``cita``  — a literal passage of the article must still be in the text
  (regional deductions, whose quotes are stored next to the parameters).
* ``tabla`` — all the figures of a scale must appear together, as the table
  the law prints.
* ``cifras`` — each figure must appear somewhere in the text. Weak for round
  numbers, but a changed threshold or amount still shows up.

A passing check does not prove the parameter is right; a failing one proves
the text it was read from is no longer there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import html
import json
from pathlib import Path
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.params import DATA_DIR, Escala, IRPFParams, load_params

Tipo = Literal["cita", "tabla", "cifras"]
VENTANA_TABLA = 6000


class Comprobacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boe_id: str
    # AAAAMMDD of a superseded wording to check against instead of the current text.
    version: str | None = None
    # Where the parameters live in the JSON file, e.g. ["comunidades", "galicia", "escala_general"].
    ruta: list[str]
    descripcion: str
    tipo: Tipo
    cifras: list[Decimal] = Field(default_factory=list)
    citas: list[str] = Field(default_factory=list)


class Resultado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comprobacion: Comprobacion
    ok: bool
    # Figures or quotes no longer found; ["sin texto"] when the law could not be read.
    faltan: list[str] = Field(default_factory=list)


def texto_plano(pagina: str) -> str:
    """Visible text of a BOE page, lower-cased and with whitespace collapsed."""
    sin_codigo = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", pagina, flags=re.S | re.I)
    texto = html.unescape(re.sub(r"<[^>]+>", " ", sin_codigo))
    texto = unicodedata.normalize("NFKC", texto).replace(" ", " ")
    return re.sub(r"\s+", " ", texto).strip().lower()


def _comprimido(texto: str) -> str:
    return re.sub(r"[\s«»\"“”]", "", unicodedata.normalize("NFKC", texto).lower())


def _formas(x: Decimal) -> set[str]:
    """Ways a Spanish law may print ``x``: 13362.22 → "13.362,22", "13 362,22", "13362,22"."""
    out: set[str] = set()
    q = x.normalize()
    for decimales in (0, 1, 2, 3):
        if q != q.quantize(Decimal(1).scaleb(-decimales)):
            continue
        entero, _, fraccion = f"{q:.{decimales}f}".partition(".")
        agrupado = f"{int(entero):,}"
        for forma in (entero, agrupado.replace(",", "."), agrupado.replace(",", " ")):
            out.add(forma + ("," + fraccion if fraccion else ""))
    return out


def _aparece(texto: str, x: Decimal) -> bool:
    if 0 < x < 1 and _aparece(texto, x * 100):
        # A fraction stored as 0.15 is printed as "15 por ciento".
        return True
    return any(re.search(r"(?<![\d.,])" + re.escape(f) + r"(?![\d]|[.,]\d)", texto) for f in _formas(x))


def _juntas(texto: str, cifras: list[Decimal], ventana: int = VENTANA_TABLA) -> bool:
    """Do all the figures appear within one stretch of ``ventana`` characters?"""
    if not cifras:
        return True
    for forma in _formas(cifras[0]):
        for match in re.finditer(r"(?<![\d.,])" + re.escape(forma) + r"(?![\d]|[.,]\d)", texto):
            tramo = texto[max(0, match.start() - ventana // 2) : match.start() + ventana]
            if all(_aparece(tramo, c) for c in cifras):
                return True
    return False


def _cifras_de(node: Any) -> list[Decimal]:
    """Every distinct non-zero figure in a parameter group (its ``fuente`` aside)."""
    found: list[Decimal] = []

    def recorrer(value: Any) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, Decimal):
            if value != 0 and value not in found:
                found.append(value)
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                if name not in ("fuente", "fuente_principal"):
                    recorrer(getattr(value, name))
        elif isinstance(value, dict):
            for item in value.values():
                recorrer(item)
        elif isinstance(value, list | tuple):
            for item in value:
                recorrer(item)

    recorrer(node)
    return found


def _cifras_tabla(escala: Escala) -> list[Decimal]:
    cifras: list[Decimal] = []
    for tramo in escala.tramos:
        for value in (tramo.hasta, tramo.cuota_hasta_inicio, tramo.tipo * 100):
            if value is not None and value != 0 and value not in cifras:
                cifras.append(value)
    return cifras


# Percentages are stored as fractions (0.15) and printed as "15".
_FRACCIONES = (
    "tipo",
    "porcentaje",
    "pendiente",
    "parte_autonomica",
    "gasto_minimo_sobre_base",
    "limite_base_liquidable",
)


def _como_se_imprimen(node: Any) -> list[Decimal]:
    """Figures of a group; a fraction below 1 is also accepted as a percentage when checked."""
    return _cifras_de(node)


@lru_cache(maxsize=1)
def _evidencia_deducciones() -> dict[str, dict[str, Any]]:
    path = DATA_DIR / "evidencia_deducciones.json"
    data: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data


@lru_cache(maxsize=1)
def _evidencia_parametros() -> dict[str, dict[str, Any]]:
    path = DATA_DIR / "evidencia_parametros.json"
    data: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data


_PROCEDENCIA = ("fuente", "recodificada_el")


def huella_de_grupo(valor: Any) -> str:
    """Fingerprint of the figures of a group, whatever way its JSON spells them ("0.10" and "0.1" are one).

    Stored next to the quotes a re-encoding leaves in ``evidencia_parametros.json``: the quotes vouch for a group
    only while the group still holds what they were read for.
    """

    def canon(x: Any) -> Any:
        if isinstance(x, BaseModel):
            return canon(x.model_dump(mode="json"))
        if isinstance(x, dict):
            return {str(k): canon(v) for k, v in sorted(x.items()) if k not in _PROCEDENCIA and v is not None}
        if isinstance(x, list | tuple):
            return [canon(v) for v in x]
        if isinstance(x, bool) or x is None:
            return x
        if isinstance(x, int | float | Decimal | str):
            try:
                return format(Decimal(str(x)).normalize(), "f")
            except InvalidOperation:
                return str(x)
        return str(x)

    return hashlib.sha256(json.dumps(canon(valor), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _valor_en(params: Any, ruta: list[str]) -> Any:
    """What a validated parameter file holds at ``ruta`` (None when nothing is there)."""
    nodo: Any = params
    for paso in ruta:
        if isinstance(nodo, BaseModel):
            nodo = getattr(nodo, paso, None)
        elif isinstance(nodo, dict):
            nodo = nodo.get(paso)
        elif isinstance(nodo, list | tuple):
            campo, hay_igual, valor = paso.partition("=")
            if hay_igual:
                nodo = next((item for item in nodo if str(getattr(item, campo, None)) == valor), None)
            else:
                nodo = nodo[int(paso)] if paso.isdigit() and int(paso) < len(nodo) else None
        else:
            return None
        if nodo is None:
            return None
    return nodo


def comprobaciones(
    params: IRPFParams,
    *,
    evidencia: dict[str, dict[str, Any]] | None = None,
    evidencia_parametros: dict[str, dict[str, Any]] | None = None,
) -> list[Comprobacion]:
    """One check per group of parameters of ``params``.

    ``evidencia``: the quotes of the regional deductions, when they are not the ones shipped with the package.
    ``evidencia_parametros``: likewise for the quotes an automatic re-encoding left for the other groups (scales,
    minimums, groups of figures). A group that has them is checked by those quotes — against the law and version
    they were read from — for as long as it holds the figures they were read for.
    """
    out: list[Comprobacion] = []
    estatal = params.fuente_principal.boe_id
    todas = _evidencia_parametros() if evidencia_parametros is None else evidencia_parametros
    recodificados: dict[str, Any] = todas.get(f"irpf_{params.ejercicio}.json", {})
    usadas: set[str] = set()

    def citas_de(ruta: list[str], valor: Any) -> dict[str, Any] | None:
        entrada = recodificados.get("/".join(ruta))
        if not isinstance(entrada, dict) or not entrada.get("citas"):
            return None
        if entrada.get("sha256_valor") not in (None, huella_de_grupo(valor)):
            return None  # edited since: the quotes no longer speak for it
        usadas.add("/".join(ruta))
        return entrada

    def por_citas(ruta: list[str], valor: Any, descripcion: str, entrada: dict[str, Any]) -> Comprobacion:
        es_escala = isinstance(valor, Escala)
        return Comprobacion(
            boe_id=str(entrada.get("boe_id") or estatal),
            version=entrada.get("version"),
            ruta=ruta,
            descripcion=descripcion,
            tipo="tabla" if es_escala else "cita",
            citas=list(entrada["citas"]),
            cifras=_cifras_tabla(valor) if es_escala else [],
        )

    for nombre in type(params).model_fields:
        value = getattr(params, nombre)
        if not isinstance(value, BaseModel) or not hasattr(value, "fuente"):
            continue
        recodificado = citas_de([nombre], value)
        if recodificado is not None:
            out.append(por_citas([nombre], value, value.fuente.referencia, recodificado))
            continue
        if not value.fuente.verificado:
            # Parameters already flagged as not checked against the law are not re-checked either.
            continue
        if isinstance(value, Escala):
            out.append(
                Comprobacion(boe_id=estatal, ruta=[nombre], descripcion=value.fuente.referencia, tipo="tabla", cifras=_cifras_tabla(value))
            )  # fmt: skip
        elif "Ley 35/2006" in params.fuente_principal.norma and "LIRPF" in value.fuente.referencia.split(";")[0]:
            # Only what the IRPF Act itself prints (donations and the like live in other laws).
            out.append(
                Comprobacion(boe_id=estatal, ruta=[nombre], descripcion=value.fuente.referencia, tipo="cifras", cifras=_como_se_imprimen(value))
            )  # fmt: skip

    evidencia = _evidencia_deducciones() if evidencia is None else evidencia
    for clave, comunidad in params.comunidades.items():
        boe_id = comunidad.fuente_principal.boe_id
        base = ["comunidades", clave]
        descripcion = f"{comunidad.nombre}: escala ({comunidad.escala_general.fuente.referencia})"
        recodificado = citas_de([*base, "escala_general"], comunidad.escala_general)
        if recodificado is not None:
            out.append(por_citas([*base, "escala_general"], comunidad.escala_general, descripcion, recodificado))
        else:
            out.append(
                Comprobacion(
                    boe_id=boe_id,
                    version=comunidad.escala_general.fuente.version_historica,
                    ruta=[*base, "escala_general"],
                    descripcion=descripcion,
                    tipo="tabla",
                    cifras=_cifras_tabla(comunidad.escala_general),
                )
            )
        minimos = comunidad.minimos
        recodificado = citas_de([*base, "minimos"], minimos) if minimos is not None else None
        if minimos is not None and recodificado is not None:
            descripcion = f"{comunidad.nombre}: mínimos ({minimos.fuente.referencia})"
            out.append(por_citas([*base, "minimos"], minimos, descripcion, recodificado))
        elif minimos is not None and "calculad" not in (minimos.fuente.nota or "").lower():
            estatales = set(_cifras_de(params.minimos))
            # Only what the community sets itself; when it copies the State's amounts, its taxpayer minimum.
            propias = [c for c in _cifras_de(minimos) if c not in estatales] or [minimos.contribuyente]
            out.append(
                Comprobacion(
                    boe_id=boe_id,
                    ruta=[*base, "minimos"],
                    descripcion=f"{comunidad.nombre}: mínimos ({minimos.fuente.referencia})",
                    tipo="cifras",
                    cifras=propias,
                )
            )
        if comunidad.deducciones_madrid is not None:
            # Sub-groups re-encoded with quotes are checked by them (below); the rest, by their figures.
            bloque = comunidad.deducciones_madrid
            sin_citas = [
                getattr(bloque, campo)
                for campo in type(bloque).model_fields
                if citas_de([*base, "deducciones_madrid", campo], getattr(bloque, campo)) is None
            ]
            out.append(
                Comprobacion(
                    boe_id=boe_id,
                    ruta=[*base, "deducciones_madrid"],
                    descripcion=f"{comunidad.nombre}: deducciones calculadas",
                    tipo="cifras",
                    cifras=_como_se_imprimen(sin_citas),
                )
            )
        # A year whose wording differs from today's keeps its own quotes, checked on that version.
        del_ejercicio: dict[str, Any] = evidencia.get(f"{clave}@{params.ejercicio}", {})
        for indice, deduccion in enumerate(comunidad.deducciones_calculables):
            historica = deduccion.articulo in del_ejercicio
            citas = (
                del_ejercicio[deduccion.articulo] if historica else evidencia.get(clave, {}).get(deduccion.articulo, [])
            )
            out.append(
                Comprobacion(
                    boe_id=boe_id,
                    version=del_ejercicio.get("_version") if historica else None,
                    ruta=[*base, "deducciones_calculables", str(indice)],
                    descripcion=f"{comunidad.nombre}: {deduccion.articulo} {deduccion.titulo}",
                    tipo="cita" if citas else "cifras",
                    citas=citas,
                    cifras=[] if citas else _como_se_imprimen(deduccion),
                )
            )
    # Groups that have no check of their own (nested regimes, the sub-groups of a block) and were re-encoded.
    emitidas = {"/".join(c.ruta) for c in out}
    for ruta_texto, entrada in recodificados.items():
        ruta = ruta_texto.split("/")
        valor = _valor_en(params, ruta)
        if ruta_texto in emitidas or valor is None or (ruta_texto not in usadas and citas_de(ruta, valor) is None):
            continue
        fuente = getattr(valor, "fuente", None)
        descripcion = getattr(fuente, "referencia", None) or ruta_texto
        if ruta[0] == "comunidades" and len(ruta) > 1 and ruta[1] in params.comunidades:
            descripcion = f"{params.comunidades[ruta[1]].nombre}: {descripcion}"
        out.append(por_citas(ruta, valor, descripcion, entrada))
    return out


TextoDe = Callable[[str, str | None], str | None]


def verificar(checks: Iterable[Comprobacion], texto_de: TextoDe) -> list[Resultado]:
    """Run the checks. ``texto_de(boe_id, version)`` returns the law's page (HTML), or None when unavailable."""
    textos: dict[tuple[str, str | None], tuple[str, str] | None] = {}
    out: list[Resultado] = []
    for check in checks:
        clave = (check.boe_id, check.version)
        if clave not in textos:
            pagina = texto_de(check.boe_id, check.version)
            plano = texto_plano(pagina) if pagina else ""
            if pagina and check.version is None:
                # Format watch (never raises): a page read as empty looks like "every figure is gone".
                from hacienda_gpt.ops import formatos

                formatos.observar("boe_consolidado_html", pagina, contexto=check.boe_id)
            textos[clave] = (plano, _comprimido(plano)) if plano else None
        par = textos[clave]
        if par is None:
            out.append(Resultado(comprobacion=check, ok=False, faltan=["sin texto"]))
            continue
        plano, comprimido = par
        faltan: list[str] = []
        for cita in check.citas:
            partes = [p for p in re.split(r"\[\.\.\.\]|…", cita) if len(_comprimido(p)) > 15] or [cita]
            if any(_comprimido(p) not in comprimido for p in partes):
                faltan.append(cita[:120])
        if check.tipo == "tabla":
            ausentes = [str(c) for c in check.cifras if not _aparece(plano, c)]
            if ausentes:
                faltan += ausentes
            elif not _juntas(plano, check.cifras):
                faltan.append("las cifras ya no aparecen juntas en una tabla")
        elif check.tipo == "cifras":
            faltan += [str(c) for c in check.cifras if not _aparece(plano, c)]
        out.append(Resultado(comprobacion=check, ok=not faltan, faltan=faltan))
    return out


def cargar_y_verificar(ejercicio: int, texto_de: TextoDe) -> list[Resultado]:
    return verificar(comprobaciones(load_params(ejercicio)), texto_de)


# Not a version of a consolidated text: the norm as the daily BOE published it.
VERSION_DIARIO = "diario"


def url_de(boe_id: str, version: str | None = None) -> str:
    if version == VERSION_DIARIO:
        return f"https://www.boe.es/diario_boe/txt.php?id={boe_id}"
    base = f"https://www.boe.es/buscar/act.php?id={boe_id}"
    return f"{base}&tn=1&p={version}" if version else base


def leer_snapshot(directorio: Path) -> TextoDe:
    """A ``texto_de`` that reads ``<boe_id>.html`` (or ``<boe_id>_p<version>.html``) under ``directorio``."""

    def texto_de(boe_id: str, version: str | None = None) -> str | None:
        nombre = f"{boe_id}_p{version}.html" if version else f"{boe_id}.html"
        for path in sorted(directorio.rglob(nombre)):
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    return texto_de
