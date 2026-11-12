"""Which text of each law the parameter files were read from, and recording a new reading when it is true.

Every parameter file names the laws its figures were read from — ``fuente_principal`` in the IRPF files and in each
of their communities; ``fuentes_principales``, ``fuentes_complementarias`` and a community's own
``fuente_principal`` in the others — with the date of the consolidated text read (``consolidado_a``), the day it was
read (``consultado_el``) and a hash of the page (``sha256_html``). ``params-check --fail-on-stale`` compares the day
the BOE last updated a law with ``consultado_el``: until that date moves, the law reads as stale.

This module moves it, deterministically, and only when it is true. A law is recorded as read today when:

* its current consolidated text has been read and shows the date of its last update;
* every check of every group, in every parameter file, that quotes it matches that text;
* no article changed since the files last read it hosts a group nobody has re-read against the current text (a
  new condition can sit outside the passages a group quotes, where no check looks);
* nothing about it is open: no proposal waiting for a decision or that did not converge, no group flagged as not
  verified that no check covers, nothing the caller (the autopilot) is still following.

A source that is not the current consolidated text (a version pinned to a date, the PDF of an order) is never
touched: it records a text that does not change. The write is all or nothing over every parameter file, each one
validated by its own model first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hacienda_gpt.tax.params import DATA_DIR, IRPFParams
from hacienda_gpt.tax.recodificacion.control import (
    _sin_acentos,
    articulos_que_contienen,
    clave_de_cabecera,
    extraer_articulos,
    partir_base,
)
from hacienda_gpt.tax.recodificacion.nucleo import (
    Adaptador,
    AlmacenPropuestas,
    Objetivo,
    Propuesta,
    RecodificacionError,
    _limpiar_caches,
    _sangria,
    _volcar,
    adaptadores_registrados,
    leer_ruta,
)
from hacienda_gpt.tax.verificacion import Resultado, TextoDe, comprobaciones, texto_plano, url_de, verificar

logger = logging.getLogger(__name__)

# Parameter files: "<family>_<year>.json".
_FICHERO_DE_PARAMETROS = re.compile(r"^[a-z][a-z_]*_\d{4}\.json$")
_ULTIMA_ACTUALIZACION = re.compile(r"ultima\s+actualizacion,?\s+publicada\s+el\s+(\d{1,2})/(\d{1,2})/(\d{4})")
_ABIERTAS = ("propuesta", "sin_converger")
FechaBoe = Callable[[str], date | None]


class _Registro(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FuenteLeida(_Registro):
    """One place of one parameter file that names a law and the reading it was taken from."""

    fichero: str
    # Path of the node inside the file (a list index as its digits).
    ruta: list[str]
    boe_id: str
    norma: str
    consolidado_a: str
    consultado_el: str
    # Not the current consolidated text (a version pinned to a date, the PDF of an order): never refreshed.
    fijada: bool

    @property
    def donde(self) -> str:
        return f"{self.fichero}:{'/'.join(self.ruta)}"


class EstadoDeLey(_Registro):
    """What is known today about one law the parameters are read from."""

    boe_id: str
    norma: str
    fuentes: list[str] = Field(default_factory=list)
    fijadas: list[str] = Field(default_factory=list)
    # The reading the files record: the latest ``consolidado_a`` and the earliest ``consultado_el``.
    leida_consolidado_a: str = ""
    leida_el: str = ""
    # What the BOE says today: the last update the page shows, and the one its metadata gives (when asked).
    consolidado_a: str = ""
    actualizada_en_boe_el: str = ""
    # The BOE has updated the law after the files read it.
    desactualizada: bool = False
    # Why that could not be known today (its page could not be read, or shows no date): nothing is recorded.
    sin_saber: str = ""
    # Blocks of the law whose wording changed after that reading ("5", "da 61").
    modificados: list[str] = Field(default_factory=list)
    # Groups read from those blocks that nobody has re-read against the current text.
    pendientes_de_releer: list[str] = Field(default_factory=list)
    # Why its reading cannot be recorded today ([] = it can).
    motivos: list[str] = Field(default_factory=list)


class InformeProcedencia(_Registro):
    # Laws whose new reading was recorded: {"boe_id", "consolidado_a", "consultado_el", "ficheros", "sin_registro"}.
    refrescadas: list[dict[str, Any]] = Field(default_factory=list)
    # Laws the BOE has not updated since the files read them: nothing to record.
    al_dia: list[str] = Field(default_factory=list)
    # Laws updated by the BOE whose reading is NOT recorded, with every reason.
    bloqueadas: dict[str, list[str]] = Field(default_factory=dict)
    # Laws whose state could not be known today, and why.
    sin_saber: dict[str, str] = Field(default_factory=dict)
    # Sources left as they are because they name a text that does not change.
    fijadas: list[str] = Field(default_factory=list)
    # Why nothing was written at all, when that is the case (a simulation, texts not read from the BOE today).
    omitido: str | None = None


# --------------------------------------------------------------------------- #
# Where each file records its sources
# --------------------------------------------------------------------------- #


def es_texto_vigente(boe_id: str, url: str) -> bool:
    """Whether a source names the CURRENT consolidated text of its law (the page ``params-verify`` reads)."""
    return url == url_de(boe_id)


def fuentes_del_fichero(nombre: str, raw: Any) -> list[FuenteLeida]:
    """Every node of a parameter file that records the reading of a law: a ``boe_id`` with its dates."""
    encontradas: list[FuenteLeida] = []

    def recorrer(nodo: Any, ruta: list[str]) -> None:
        if isinstance(nodo, dict):
            if isinstance(nodo.get("boe_id"), str) and "consolidado_a" in nodo and "consultado_el" in nodo:
                encontradas.append(
                    FuenteLeida(
                        fichero=nombre,
                        ruta=ruta,
                        boe_id=nodo["boe_id"],
                        norma=str(nodo.get("norma") or nodo["boe_id"]),
                        consolidado_a=str(nodo.get("consolidado_a") or ""),
                        consultado_el=str(nodo.get("consultado_el") or ""),
                        fijada=not es_texto_vigente(nodo["boe_id"], str(nodo.get("url") or "")),
                    )
                )
                return
            for clave, hijo in nodo.items():
                recorrer(hijo, [*ruta, str(clave)])
        elif isinstance(nodo, list):
            for indice, hijo in enumerate(nodo):
                recorrer(hijo, [*ruta, str(indice)])

    recorrer(raw, [])
    return encontradas


def ficheros_de_parametros(data_dir: Path = DATA_DIR) -> list[Path]:
    return sorted(p for p in data_dir.iterdir() if _FICHERO_DE_PARAMETROS.fullmatch(p.name))


def fuentes_de_los_parametros(data_dir: Path = DATA_DIR) -> list[FuenteLeida]:
    fuentes: list[FuenteLeida] = []
    for path in ficheros_de_parametros(data_dir):
        raw = json.loads(path.read_text(encoding="utf-8"))
        fuentes += fuentes_del_fichero(path.name, raw)
    return fuentes


def fecha_de_consolidacion(pagina: str) -> date | None:
    """The last update a BOE consolidated page shows ("Última actualización publicada el 11/11/2026")."""
    fechas: list[date] = []
    for dia, mes, anio in _ULTIMA_ACTUALIZACION.findall(_sin_acentos(texto_plano(pagina))):
        try:
            fechas.append(date(int(anio), int(mes), int(dia)))
        except ValueError:
            continue
    return max(fechas) if fechas else None


# --------------------------------------------------------------------------- #
# Which groups hang from which blocks
# --------------------------------------------------------------------------- #


def bloques_del_grupo(objetivo: Objetivo, pagina: str | None) -> set[str]:
    """Blocks of its law a group is read from: the articles it names, and those where its quotes are found."""
    propios = {partir_base(" ".join(a.lower().split()))[0] for a in objetivo.articulos_ley}
    if pagina and objetivo.citas_antes and objetivo.version is None:
        propios |= {partir_base(b)[0] for b in articulos_que_contienen(pagina, objetivo.citas_antes)}
    return propios


def grupos_de_los_bloques(
    boe_id: str, pagina: str | None, bloques: Iterable[str], *, data_dir: Path = DATA_DIR
) -> list[tuple[Objetivo, list[str]]]:
    """Every group, in any parameter file, read from one of ``bloques`` of the law ``boe_id``, with those blocks."""
    from hacienda_gpt.tax.recodificacion.adaptadores import todos_los_objetivos

    buscados = set(bloques)
    if not buscados:
        return []
    out: list[tuple[Objetivo, list[str]]] = []
    for objetivo in todos_los_objetivos(data_dir=data_dir, boe_id=boe_id).objetivos:
        tocados = sorted(bloques_del_grupo(objetivo, pagina) & buscados)
        if tocados:
            out.append((objetivo, tocados))
    return out


def releido_contra(objetivo: Objetivo, pagina: str, propuestas: Sequence[Propuesta]) -> Propuesta | None:
    """An accepted proposal (re-encoded, or confirmed without changes) made from the text ``pagina`` prints today."""
    for propuesta in reversed(propuestas):
        if (
            propuesta.estado != "aplicada"
            or propuesta.fichero != objetivo.fichero
            or propuesta.ruta != objetivo.ruta
            or propuesta.boe_id != objetivo.boe_id
            or propuesta.version is not None
            or propuesta.norma_texto is not None
        ):
            continue
        if extraer_articulos(pagina, propuesta.articulos_ley).sha256 == propuesta.sha256_texto:
            return propuesta
    return None


# --------------------------------------------------------------------------- #
# What blocks a law
# --------------------------------------------------------------------------- #


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _ley_de(adaptador: Adaptador, ruta: list[str], ficheros: dict[int, dict[str, Any]], data_dir: Path) -> str:
    from hacienda_gpt.tax.recodificacion.adaptadores import construir_objetivo

    try:
        return construir_objetivo(adaptador, ruta, ficheros, data_dir=data_dir, origen="procedencia").boe_id
    except RecodificacionError:
        raw = ficheros[max(ficheros)]
        comunidad = raw.get("comunidades", {}).get(ruta[1], {}) if ruta[0] == "comunidades" else raw
        return str(comunidad.get("fuente_principal", {}).get("boe_id", ""))


def _historico(adaptador: Adaptador, fuente: dict[str, Any]) -> bool:
    from hacienda_gpt.tax.recodificacion.adaptadores import _HISTORICA, GrupoDeCifras

    return (
        bool(fuente.get("version_historica"))
        or bool(_HISTORICA.search(str(fuente.get("referencia") or "")))
        or (isinstance(adaptador, GrupoDeCifras) and adaptador.cifras.congelado)
    )


def _sin_cuadrar(texto_de: TextoDe, data_dir: Path) -> dict[str, list[str]]:
    """Per law: failing checks of every parameter file, and IRPF groups flagged as not verified that nothing checks."""
    from hacienda_gpt.tax.recodificacion.adaptadores import (
        _DEL_IRPF,
        EVIDENCIA_DEDUCCIONES,
        EVIDENCIA_PARAMETROS,
        FICHERO_IRPF,
        _otros_modulos,
    )
    from hacienda_gpt.tax.recodificacion.nucleo import ejercicios_en

    motivos: dict[str, list[str]] = {}

    def anotar(fichero: str, resultados: Iterable[Resultado]) -> None:
        for resultado in resultados:
            if not resultado.ok:
                check = resultado.comprobacion
                faltan = "sin texto" if resultado.faltan == ["sin texto"] else "; ".join(resultado.faltan[:2])
                motivos.setdefault(check.boe_id, []).append(
                    f"{fichero}: {check.descripcion[:120]} no coincide con el texto ({faltan[:160]})"
                )

    evidencia, evidencia_parametros = _json(data_dir / EVIDENCIA_DEDUCCIONES), _json(data_dir / EVIDENCIA_PARAMETROS)
    ficheros = {
        e: _json(data_dir / FICHERO_IRPF.format(ejercicio=e)) for e in ejercicios_en(data_dir, FICHERO_IRPF)
    }
    for ejercicio, raw in ficheros.items():
        nombre = FICHERO_IRPF.format(ejercicio=ejercicio)
        checks = comprobaciones(
            IRPFParams.model_validate(raw), evidencia=evidencia, evidencia_parametros=evidencia_parametros
        )
        anotar(nombre, verificar(checks, texto_de))
        comprobados = ["/".join(c.ruta) for c in checks]
        for adaptador in _DEL_IRPF:
            for ruta in adaptador.grupos(raw):
                nodo = leer_ruta(raw, ruta)
                fuente = nodo.get("fuente") if isinstance(nodo, dict) else None
                if not isinstance(fuente, dict) or fuente.get("verificado") is not False or _historico(adaptador, fuente):
                    continue
                camino = "/".join(ruta)
                if any(camino == c or camino.startswith(c + "/") for c in comprobados):
                    continue
                motivos.setdefault(_ley_de(adaptador, ruta, ficheros, data_dir), []).append(
                    f"{nombre}: {camino} está marcado como no verificado y ninguna comprobación lo cubre"
                )
    for modulo in _otros_modulos():
        verificar_ficheros = getattr(modulo, "verificar_ficheros", None)
        if callable(verificar_ficheros):
            for fichero, resultados in verificar_ficheros(texto_de, data_dir=data_dir).items():
                anotar(fichero, resultados)
    return motivos


def lecturas(
    texto_de: TextoDe, *, data_dir: Path = DATA_DIR, fecha_boe: FechaBoe | None = None
) -> tuple[dict[str, EstadoDeLey], dict[str, str]]:
    """Every law the parameter files read, whether the BOE has updated it since, and which of its blocks changed.

    Cheap and deterministic: one page per law and, for a stale one, the version dates the page prints. A law is
    stale when the page shows a last update later than the earliest reading the files record (``consultado_el``)
    or, given ``fecha_boe(boe_id)`` (the BOE's metadata, what ``params-check`` compares), when that date is. The
    blocks changed are those whose wording was published after that reading. Returns the states and the pages.
    """
    from hacienda_gpt.proactive.impacto import articulos_modificados

    fuentes = fuentes_de_los_parametros(data_dir)
    estados: dict[str, EstadoDeLey] = {}
    paginas: dict[str, str] = {}
    for boe_id in dict.fromkeys(f.boe_id for f in fuentes):
        suyas = [f for f in fuentes if f.boe_id == boe_id]
        vigentes = [f for f in suyas if not f.fijada]
        estado = estados[boe_id] = EstadoDeLey(
            boe_id=boe_id,
            norma=(vigentes or suyas)[0].norma,
            fuentes=[f.donde for f in vigentes],
            fijadas=[f.donde for f in suyas if f.fijada],
        )
        if not vigentes:
            continue
        estado.leida_consolidado_a = max(f.consolidado_a for f in vigentes)
        estado.leida_el = min(f.consultado_el for f in vigentes)
        pagina = texto_de(boe_id, None)
        if not pagina:
            estado.sin_saber = "no se pudo leer su texto consolidado"
            continue
        paginas[boe_id] = pagina
        en_pagina = fecha_de_consolidacion(pagina)
        en_boe = None
        if fecha_boe is not None:
            try:
                en_boe = fecha_boe(boe_id)
            except Exception as exc:  # the page still speaks for itself
                logger.warning("Could not read the BOE metadata of %s: %s", boe_id, exc)
        estado.consolidado_a = en_pagina.isoformat() if en_pagina else ""
        estado.actualizada_en_boe_el = en_boe.isoformat() if en_boe else ""
        if en_pagina is None and en_boe is None:
            estado.sin_saber = "la página no muestra la fecha de su última actualización"
            continue
        estado.desactualizada = any(f is not None and f.isoformat() > estado.leida_el for f in (en_pagina, en_boe))
        if not estado.desactualizada:
            continue
        try:
            desde = date.fromisoformat(estado.leida_el)
        except ValueError:
            continue
        estado.modificados = list(
            dict.fromkeys(
                clave
                for articulo in articulos_modificados(pagina, desde=desde)
                if (clave := clave_de_cabecera(articulo.titulo)) is not None
            )
        )
    return estados, paginas


def diagnosticar(
    texto_de: TextoDe,
    *,
    data_dir: Path = DATA_DIR,
    almacen: AlmacenPropuestas | None = None,
    fecha_boe: FechaBoe | None = None,
    abiertas: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, EstadoDeLey]:
    """The state of every law the parameter files read: is it stale, and what keeps its reading from being recorded.

    No model is involved. The checks of every file are run only when some law is stale (nothing else needs them).
    ``abiertas``: further reasons per law from the caller (the autopilot's open norms and unfinished groups).
    """
    estados, paginas = lecturas(texto_de, data_dir=data_dir, fecha_boe=fecha_boe)
    pendientes = [e for e in estados.values() if e.desactualizada]
    if not pendientes:
        return estados
    propuestas = almacen.listar() if almacen is not None else []
    sin_cuadrar = _sin_cuadrar(texto_de, data_dir)
    for estado in pendientes:
        pagina = paginas[estado.boe_id]
        for objetivo, tocados in grupos_de_los_bloques(estado.boe_id, pagina, estado.modificados, data_dir=data_dir):
            if releido_contra(objetivo, pagina, propuestas) is None:
                estado.pendientes_de_releer.append(f"{objetivo.etiqueta} (art. {', '.join(tocados)})")
        if estado.pendientes_de_releer:
            estado.motivos.append(
                f"{len(estado.pendientes_de_releer)} grupo(s) de artículos modificados desde la última lectura "
                f"({', '.join(estado.modificados[:12])}) sin releer contra el texto actual: "
                + "; ".join(estado.pendientes_de_releer[:5])
            )
        estado.motivos += sin_cuadrar.get(estado.boe_id, [])
        estado.motivos += [
            f"propuesta {p.id} «{p.estado}» ({p.etiqueta[:80]})"
            for p in propuestas
            if p.boe_id == estado.boe_id and p.estado in _ABIERTAS
        ]
        estado.motivos += list((abiertas or {}).get(estado.boe_id, []))
    return estados


# --------------------------------------------------------------------------- #
# Recording a new reading
# --------------------------------------------------------------------------- #


def _validador_de(nombre: str) -> Adaptador | None:
    """An adapter of the parameter file ``nombre``: it knows the model the file must still fit."""
    for adaptador in adaptadores_registrados():
        patron = re.escape(adaptador.fichero).replace(re.escape("{ejercicio}"), r"\d{4}")
        if re.fullmatch(patron, nombre):
            return adaptador
    return None


def refrescar(
    boe_id: str,
    *,
    consolidado_a: str,
    pagina: str,
    data_dir: Path = DATA_DIR,
    hoy: date,
    motivo: str,
) -> dict[str, Any]:
    """Record in EVERY parameter file that cites ``boe_id`` that its consolidated text ``consolidado_a`` was read today.

    Each source of that law that names its current consolidated text gets ``consolidado_a``, ``consultado_el``
    (``hoy``) and ``sha256_html`` (of ``pagina``); a file with a ``revisiones`` list gets an entry saying so. A source
    pinned to another text is left alone. Every file is validated by its own model before any is written, and
    nothing is written when one fails. The caller decides whether the reading may be recorded
    (:func:`diagnosticar`); this function only writes it. Returns what it did.
    """
    sha = hashlib.sha256(pagina.encode("utf-8")).hexdigest()
    nuevos = {"consolidado_a": consolidado_a, "consultado_el": hoy.isoformat(), "sha256_html": sha}
    cambios: dict[Path, tuple[dict[str, Any], int]] = {}
    sin_registro: list[str] = []
    norma = boe_id
    for path in ficheros_de_parametros(data_dir):
        contenido = path.read_text(encoding="utf-8")
        raw = json.loads(contenido)
        if not isinstance(raw, dict):
            continue
        tocado = False
        for fuente in fuentes_del_fichero(path.name, raw):
            if fuente.boe_id != boe_id or fuente.fijada:
                continue
            nodo = leer_ruta(raw, fuente.ruta)
            norma = fuente.norma
            # The hash alone does not make a new reading: the page carries parts that change on every visit.
            if (nodo.get("consolidado_a"), nodo.get("consultado_el")) != (consolidado_a, hoy.isoformat()):
                nodo.update(nuevos)
                tocado = True
        if not tocado:
            continue
        if isinstance(raw.get("revisiones"), list):
            raw["revisiones"].append(
                {
                    "norma": f"Relectura de {norma.split(' (')[0]}"[:200],
                    "boe_id": boe_id,
                    "revisado_el": hoy.isoformat(),
                    "resultado": (
                        f"Texto consolidado a {consolidado_a} leído el {hoy.isoformat()}: {motivo} Se actualiza su "
                        "procedencia (consolidado_a, consultado_el y sha256_html) sin tocar ninguna cifra."
                    ),
                }
            )
        else:
            sin_registro.append(path.name)
        validador = _validador_de(path.name)
        if validador is None:
            raise RecodificacionError(f"{path.name}: ningún adaptador sabe validarlo; no se escribe nada.")
        try:
            validador.validar_fichero(raw)
        except (ValidationError, ValueError) as exc:
            raise RecodificacionError(
                f"{path.name} dejaría de ser válido al anotar la lectura de {boe_id}: {str(exc)[:200]}"
            ) from exc
        cambios[path] = (raw, _sangria(contenido))
    # Everything validated: only now is anything written.
    for path, (raw, sangria) in cambios.items():
        _volcar(path, raw, sangria=sangria)
    if cambios:
        from hacienda_gpt.tax import fuentes

        _limpiar_caches()
        fuentes.normas_de_parametros.cache_clear()
    return {
        "boe_id": boe_id,
        "consolidado_a": consolidado_a,
        "consultado_el": hoy.isoformat(),
        "ficheros": [p.name for p in cambios],
        "sin_registro": sin_registro,
    }


MOTIVO_REFRESCO = (
    "todas las comprobaciones de los grupos que la citan coinciden, ningún artículo modificado desde la lectura "
    "anterior queda sin releer y no hay nada abierto sobre ella."
)


def refrescar_lo_que_se_pueda(
    texto_de: TextoDe,
    *,
    hoy: date,
    data_dir: Path = DATA_DIR,
    almacen: AlmacenPropuestas | None = None,
    fecha_boe: FechaBoe | None = None,
    abiertas: Mapping[str, Sequence[str]] | None = None,
    simular: bool = False,
) -> InformeProcedencia:
    """Record today's reading of every stale law nothing blocks (see the module); say why the others are blocked.

    ``simular``: diagnose only and write nothing (for texts that were not read from the BOE today).
    """
    informe = InformeProcedencia()
    estados = diagnosticar(texto_de, data_dir=data_dir, almacen=almacen, fecha_boe=fecha_boe, abiertas=abiertas)
    for boe_id, estado in sorted(estados.items()):
        informe.fijadas += estado.fijadas
        if not estado.fuentes:
            continue
        if estado.sin_saber:
            informe.sin_saber[boe_id] = estado.sin_saber
        elif estado.motivos:
            informe.bloqueadas[boe_id] = estado.motivos
        elif not estado.desactualizada:
            informe.al_dia.append(boe_id)
        elif simular:
            informe.refrescadas.append({"boe_id": boe_id, "consolidado_a": estado.consolidado_a, "simulado": True})
        else:
            pagina = texto_de(boe_id, None)
            assert pagina is not None
            informe.refrescadas.append(
                refrescar(
                    boe_id,
                    # Never backwards: a file may have recorded the date of the BOE's metadata, later than the page's.
                    consolidado_a=max(estado.consolidado_a, estado.actualizada_en_boe_el, estado.leida_consolidado_a),
                    pagina=pagina,
                    data_dir=data_dir,
                    hoy=hoy,
                    motivo=MOTIVO_REFRESCO,
                )
            )
    return informe


__all__ = [
    "EstadoDeLey",
    "FuenteLeida",
    "InformeProcedencia",
    "bloques_del_grupo",
    "diagnosticar",
    "es_texto_vigente",
    "fecha_de_consolidacion",
    "ficheros_de_parametros",
    "fuentes_de_los_parametros",
    "fuentes_del_fichero",
    "grupos_de_los_bloques",
    "lecturas",
    "refrescar",
    "refrescar_lo_que_se_pueda",
    "releido_contra",
]
