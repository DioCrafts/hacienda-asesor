"""The sources the format watch knows: what each parser assumes, stated as checks.

One :class:`~hacienda_gpt.ops.formatos.Fuente` per external format, with:

* its expectations in plain Spanish (they go to the operator and to the
  diagnosis agent);
* its canaries: cheap invariants over what the parser extracted;
* how to trim a payload into a sample that still parses;
* how to replay the extraction on a sample with a given set of rules.

Imported lazily by :mod:`hacienda_gpt.ops.formatos` (the parsers import that
module, and this one imports the parsers). A group of sources whose parser
cannot be imported (the crawlers need scrapy) is skipped, not fatal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
import logging
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree

from hacienda_gpt.ops.formatos import (
    MAX_MUESTRA_CHARS,
    Ensayo,
    Fallo,
    Fuente,
    Lectura,
    canonico,
    registrar,
    spec_activa,
)
from hacienda_gpt.ops.formatos_boe import (
    ReglasConsolidado,
    ReglasDocumentoXml,
    ReglasMetadatos,
    ReglasSumario,
    extraer_documento_xml,
    extraer_metadatos,
    valor_en,
)
from hacienda_gpt.ops.formatos_documentos import CLASES, OTROS
from hacienda_gpt.ops.formatos_esqueleto import esqueleto_html, lista_permitida

logger = logging.getLogger(__name__)

_BOE_ID = re.compile(r"^BOE-[A-Z]-\d{4}-\d+$")
MAX_HIJOS_MUESTRA = 3
_SCRIPTS = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")


def _grave(canario: str, detalle: str) -> Fallo:
    return Fallo(canario=canario, gravedad="grave", detalle=detalle)


def _leve(canario: str, detalle: str) -> Fallo:
    return Fallo(canario=canario, gravedad="leve", detalle=detalle)


# --------------------------------------------------------------------------- #
# BOE: daily summary (JSON)
# --------------------------------------------------------------------------- #


def _podar_json(node: Any) -> Any:
    """Keep the shape of a JSON document and only the first elements of every list."""
    if isinstance(node, list):
        return [_podar_json(item) for item in node[:MAX_HIJOS_MUESTRA]]
    if isinstance(node, dict):
        return {key: _podar_json(value) for key, value in node.items()}
    return node[:400] if isinstance(node, str) else node


def _recortar_json(crudo: str) -> str:
    try:
        return json.dumps(_podar_json(json.loads(crudo)), ensure_ascii=False, indent=1)
    except ValueError:
        return crudo[:MAX_MUESTRA_CHARS]


def _canarios_sumario(items: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    items = items if isinstance(items, list) else []
    generales = [item for item in items if item.get("seccion") == "1"]
    fallos: list[Fallo] = []
    if isinstance(reglas, ReglasSumario):
        try:
            estado = valor_en(json.loads(muestra), reglas.estado)
        except ValueError:
            estado = None
        if estado != reglas.estado_ok:
            fallos.append(
                _grave(
                    "estado_api",
                    f"La API no declara éxito ({'.'.join(paso[0] for paso in reglas.estado)} = {estado!r}).",
                )
            )
    if not items:
        fallos.append(_grave("items", "El sumario existe pero no se ha leído de él ninguna disposición."))
    elif not generales:
        try:
            laborable = date.fromisoformat(contexto).weekday() < 5
        except ValueError:
            laborable = True
        detalle = "No se ha leído ninguna disposición de la sección I (disposiciones generales)."
        fallos.append(_grave("seccion_1", detalle) if laborable else _leve("seccion_1", detalle + " Es sábado."))
    raros = [item.get("boe_id") for item in items if not _BOE_ID.match(str(item.get("boe_id") or ""))]
    if raros:
        fallos.append(_grave("identificadores", f"Identificadores con forma desconocida, p. ej. {raros[0]!r}."))
    if items and sum(not item.get("url") for item in items) > len(items) * 0.1:
        fallos.append(_leve("enlaces", "Más del 10 % de las disposiciones llegan sin enlace al texto."))
    return Lectura(fallos=fallos, metricas={"items": len(items), "items_seccion_1": len(generales)})


def _canarios_dias(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    try:
        laborable = date.fromisoformat(str(datos.get("dia"))).weekday() < 5
    except ValueError:
        laborable = False
    if laborable and not datos.get("hay"):
        detalle = f"El {datos.get('dia')} (de lunes a viernes) la API no tiene sumario del BOE."
        return Lectura(fallos=[_grave("sin_sumario", detalle + " Un festivo suelto es normal; dos días seguidos, no.")])
    return Lectura()


def _registrar_boe() -> None:
    from hacienda_gpt.proactive.impacto import bloques_consolidado
    from hacienda_gpt.proactive.novedades import extraer_items

    registrar(
        Fuente(
            clave="boe_sumario",
            descripcion="sumario diario del BOE (API de datos abiertos, JSON)",
            expectativas=(
                'La respuesta es JSON con status.code = "200" y el sumario bajo data.sumario.',
                "data.sumario.diario[].seccion[] tiene «codigo» («1» = disposiciones generales, «3» = otras "
                "disposiciones) y «departamento»; donde hay un solo elemento llega un objeto en vez de una lista.",
                "Cada departamento tiene «item» directamente, o dentro de «epigrafe» (con «nombre») o de «texto».",
                "Cada item tiene «identificador» (BOE-A-AAAA-N), «titulo» y «url_html».",
                "Un día laborable hay al menos una disposición en la sección I.",
            ),
            reglas=ReglasSumario,
            extraer=lambda reglas, texto: extraer_items(reglas, json.loads(texto)),
            canarios=_canarios_sumario,
            recortar=_recortar_json,
            caidas=(("items", 0.8),),
            codigo="hacienda_gpt/proactive/novedades.py (items_del_sumario), hacienda_gpt/ops/formatos_boe.py",
        )
    )
    registrar(
        Fuente(
            clave="boe_sumario_dias",
            descripcion="disponibilidad del sumario diario del BOE",
            expectativas=(
                "La API responde 404 solo los días sin boletín (domingos y algún festivo).",
                "De lunes a viernes hay sumario; dos días laborables seguidos sin él no es un festivo.",
            ),
            canarios=_canarios_dias,
            guarda_muestras=False,
            codigo="hacienda_gpt/cli/proactive.py (_sumario, _get), hacienda_gpt/proactive/novedades.py (revisar)",
        )
    )
    registrar(
        Fuente(
            clave="boe_documento_xml",
            descripcion="ficha XML de una disposición del BOE (metadatos y análisis)",
            expectativas=(
                "XML bien formado con <metadatos> (identificador, rango, origen_legislativo, departamento, "
                "estatus_derogacion, fecha_derogacion).",
                "El análisis jurídico está en <analisis>: <materias>/<materia> y <referencias> con "
                '<anteriores>/<anterior referencia="BOE-…"> y <posteriores>/<posterior>, cada uno con <palabra> y '
                "<texto>.",
                "El análisis puede tardar días en publicarse, pero entre muchas disposiciones de las dos últimas "
                "semanas alguna lo tiene.",
                "La derogación consta como estatus_derogacion = «S» o como referencia posterior «SE DEROGA…»; la "
                "convalidación de un real decreto-ley, como referencia posterior cuya palabra contiene «convalidación».",
            ),
            reglas=ReglasDocumentoXml,
            extraer=extraer_documento_xml,
            canarios=_canarios_documento_xml,
            recortar=_recortar_xml,
            codigo=(
                "hacienda_gpt/proactive/novedades.py (leer_analisis), hacienda_gpt/tax/vigencia_normas.py "
                "(estado_desde_xml), hacienda_gpt/ops/formatos_boe.py"
            ),
        )
    )
    registrar(
        Fuente(
            clave="boe_metadatos",
            descripcion="metadatos de una norma consolidada del BOE (API de datos abiertos, JSON)",
            expectativas=(
                'La respuesta es JSON con status.code = "200" y una lista «data» con un elemento.',
                "Ese elemento tiene «identificador» (el pedido) y «fecha_actualizacion» con forma AAAAMMDDTHHMMSSZ.",
            ),
            reglas=ReglasMetadatos,
            extraer=extraer_metadatos,
            canarios=_canarios_metadatos,
            codigo="hacienda_gpt/cli/boe_watch.py (_fetch_metadata), hacienda_gpt/ops/formatos_boe.py",
        )
    )
    registrar(
        Fuente(
            clave="boe_consolidado_html",
            descripcion="página de texto consolidado del BOE (buscar/act.php)",
            expectativas=(
                "El texto de la norma se lee como líneas: cada artículo o disposición empieza por una línea "
                "«Artículo N.» o «Disposición adicional/transitoria/derogatoria/final…».",
                "Dentro de cada bloque hay al menos una línea de versión «…publicado el DD/MM/AAAA[, en vigor a "
                "partir del DD/MM/AAAA]».",
                "Las notas de cambio empiezan por «Se modifica», «Se añade», «Se deroga»…",
                'El marcado lleva id="textoxslt" y bloques class="bloque" (no se usan para extraer).',
            ),
            reglas=ReglasConsolidado,
            extraer=bloques_consolidado,
            canarios=_canarios_consolidado,
            recortar=_recortar_consolidado,
            codigo=(
                "hacienda_gpt/proactive/impacto.py (bloques_consolidado, _lineas); el texto plano que usa "
                "hacienda_gpt/tax/verificacion.py no depende de estas reglas"
            ),
        )
    )


# --------------------------------------------------------------------------- #
# BOE: one disposition (XML)
# --------------------------------------------------------------------------- #

_SIN_INTERES_XML = frozenset({"texto", "metadata-eli"})


def _podar_xml(node: ElementTree.Element) -> None:
    vistos: dict[str, int] = {}
    for child in list(node):
        vistos[child.tag] = vistos.get(child.tag, 0) + 1
        if vistos[child.tag] > MAX_HIJOS_MUESTRA * 2:
            node.remove(child)
        else:
            _podar_xml(child)


def _recortar_xml(crudo: str) -> str:
    """Drop the body of the disposition and long runs of sibling elements; keep the XML well formed."""
    try:
        root = ElementTree.fromstring(crudo)
    except ElementTree.ParseError:
        return crudo[:MAX_MUESTRA_CHARS]
    for child in list(root):
        if child.tag in _SIN_INTERES_XML:
            root.remove(child)
    _podar_xml(root)
    return ElementTree.tostring(root, encoding="unicode")


def _canarios_documento_xml(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    fallos: list[Fallo] = []
    identificador = str(datos.get("identificador") or "")
    cobertura = contexto.startswith("cobertura:")
    esperado = contexto.split(":")[1] if cobertura and contexto.count(":") >= 1 else contexto
    if not identificador:
        fallos.append(_grave("identificador", "No se encuentra el identificador de la disposición en <metadatos>."))
    elif _BOE_ID.match(esperado) and identificador != esperado:
        fallos.append(_grave("identificador", f"Se pidió {esperado} y la ficha dice {identificador}."))
    if not datos.get("rango"):
        fallos.append(_leve("rango", "La ficha no indica el rango de la disposición."))
    if not datos.get("departamento"):
        fallos.append(_leve("departamento", "La ficha no indica el departamento."))
    materias, anteriores = datos.get("materias") or [], datos.get("anteriores") or []
    if cobertura and not (materias or anteriores):
        leidas = contexto.rsplit(":", 1)[-1]
        fallos.append(
            _grave(
                "analisis",
                f"Ninguna de las {leidas} disposiciones leídas trae análisis (materias o normas que modifica): o el "
                "BOE lo ha movido de sitio o ha dejado de publicarlo.",
            )
        )
    return Lectura(fallos=fallos, metricas={"materias": len(materias), "anteriores": len(anteriores)})


# --------------------------------------------------------------------------- #
# BOE: consolidated law metadata (JSON) and page (HTML)
# --------------------------------------------------------------------------- #


def _canarios_metadatos(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    fallos: list[Fallo] = []
    identificador = datos.get("identificador")
    if not identificador or (contexto and identificador != contexto):
        fallos.append(_grave("identificador", f"Se pidió {contexto or '?'} y los metadatos dicen {identificador!r}."))
    fecha = datos.get("fecha_actualizacion")
    if not isinstance(fecha, str) or not re.fullmatch(r"\d{8}T\d{6}Z", fecha):
        fallos.append(
            _grave(
                "fecha_actualizacion", f"«fecha_actualizacion» falta o no tiene la forma AAAAMMDDTHHMMSSZ: {fecha!r}."
            )
        )
    return Lectura(fallos=fallos)


_INICIO_TEXTO = 'id="textoxslt"'
_INICIO_BLOQUE = '<div class="bloque"'
MAX_HTML_MUESTRA = 40_000
MIN_BLOQUES_VERSION = 5


def _recortar_consolidado(crudo: str) -> str:
    """The first articles of the law: from the start of its text, cut at a block boundary."""
    inicio = crudo.find(_INICIO_TEXTO)
    inicio = crudo.rfind("<", 0, inicio) if inicio > 0 else 0
    # Skip the preamble: start at the first article block when there is one.
    articulo = crudo.find('<div class="bloque" id="a1"', inicio)
    if articulo > 0:
        inicio = articulo
    trozo = crudo[inicio : inicio + MAX_HTML_MUESTRA]
    corte = trozo.rfind(_INICIO_BLOQUE)
    cuerpo = trozo[:corte] if corte > MAX_HTML_MUESTRA // 4 else trozo
    # The markers the spec expects are part of what a sample must show.
    return f"<div {_INICIO_TEXTO}>\n{cuerpo}\n</div>" if _INICIO_TEXTO in crudo else cuerpo


def _canarios_consolidado(bloques: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    bloques = bloques if isinstance(bloques, list) else []
    fallos: list[Fallo] = []
    con_version = sum(1 for bloque in bloques if bloque.get("publicado"))
    con_texto = sum(1 for bloque in bloques if bloque.get("texto"))
    if not bloques:
        fallos.append(_grave("bloques", "No se ha reconocido ningún artículo ni disposición en la página."))
    else:
        # Articles never amended carry no version line, and the page's index yields empty blocks:
        # only "none at all" is a sign that the format moved.
        if con_version == 0 and len(bloques) >= MIN_BLOQUES_VERSION:
            fallos.append(
                _grave(
                    "versiones",
                    f"Ninguno de los {len(bloques)} bloques tiene fecha de publicación reconocible: sin ella ningún "
                    "cambio de redacción se detecta.",
                )
            )
        if con_texto == 0:
            fallos.append(_grave("texto", f"Ninguno de los {len(bloques)} bloques tiene texto."))
    if isinstance(reglas, ReglasConsolidado):
        ausentes = [marca for marca in reglas.marcadores if marca not in muestra]
        if ausentes:
            fallos.append(_leve("marcadores", "El marcado ya no contiene: " + ", ".join(ausentes) + "."))
    return Lectura(fallos=fallos, metricas={"bloques": len(bloques), "con_version": con_version})


# --------------------------------------------------------------------------- #
# Crawlers
# --------------------------------------------------------------------------- #


def _canarios_boletin(filas: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    filas = filas if isinstance(filas, list) else []
    fallos: list[Fallo] = []
    if not filas:
        fallos.append(
            _grave("resultados", f"La búsqueda «{contexto}» no ha dado ningún resultado reconocible en el listado.")
        )
    else:
        if sum(not fila.get("title") for fila in filas) > len(filas) * 0.5:
            fallos.append(_grave("titulos", "Más de la mitad de los resultados llegan sin título."))
        if all(not fila.get("date_raw") for fila in filas):
            fallos.append(_leve("fechas", "Ningún resultado trae fecha."))
    return Lectura(fallos=fallos, metricas={"resultados": len(filas)})


_CAMPOS_TEAC = (
    "numero_resolucion",
    "sala",
    "fecha_resolucion",
    "sentido",
    "vinculante",
    "concepto",
    "materia",
    "norma",
    "precepto",
    "criterio",
)


def _canarios_teac(campos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    if not isinstance(campos, dict):
        return Lectura(fallos=[_grave("ficha", "La página se ha tomado por «el criterio no existe».")])
    fallos: list[Fallo] = []
    presentes = [campo for campo in _CAMPOS_TEAC if campos.get(campo) is not None]
    if not campos.get("numero_resolucion"):
        fallos.append(_grave("numero_resolucion", "No se encuentra el número de resolución."))
    if not campos.get("criterio"):
        fallos.append(_grave("criterio", "No se encuentra el texto del criterio."))
    if len(presentes) < 4:
        fallos.append(_grave("campos", f"Solo se han reconocido {len(presentes)} de {len(_CAMPOS_TEAC)} campos."))
    if not campos.get("fecha_resolucion"):
        fallos.append(_leve("fecha_resolucion", "No se reconoce la fecha de la resolución."))
    return Lectura(fallos=fallos, metricas={"campos": len(presentes)})


def _canarios_dgt(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    filas_leidas = datos.get("filas")
    filas_lista = [f for f in filas_leidas if isinstance(f, list | tuple)] if isinstance(filas_leidas, list) else []
    filas = len(filas_lista)
    con_numero = sum(1 for fila in filas_lista if len(fila) > 1 and fila[1])
    fallos: list[Fallo] = []
    if not filas and datos.get("total_paginas") is None and not datos.get("query"):
        fallos.append(
            _grave("pagina", "La página de resultados no tiene filas, ni paginación, ni consulta: no se reconoce.")
        )
    elif filas and not datos.get("query"):
        fallos.append(
            _grave("query", "Hay resultados pero falta el campo oculto «query», sin el que no se descarga ninguno.")
        )
    if filas and con_numero < filas * 0.5:
        fallos.append(_leve("numeros", f"Solo {con_numero} de {filas} filas traen el número de consulta."))
    return Lectura(fallos=fallos, metricas={"filas": filas})


_NUMERO_DGT = re.compile(r"^[VN]\d{4}-\d{2}$")
MIN_TEXTO_DGT = 300


def _extraer_documento_dgt(reglas: Any, html: str) -> dict[str, Any]:
    from hacienda_gpt.cli.dgt_seed import numero_de_documento

    visible = " ".join(re.sub(r"<[^>]+>", " ", _SCRIPTS.sub("", html)).split())
    return {"numero": numero_de_documento(reglas, html), "caracteres": len(visible)}


def _canarios_documento_dgt(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    numero, caracteres = datos.get("numero"), int(datos.get("caracteres") or 0)
    pedido = contexto.split(":", 1)[1] if ":" in contexto else ""
    fallos: list[Fallo] = []
    if caracteres < MIN_TEXTO_DGT:
        fallos.append(_grave("vacio", f"El documento apenas tiene texto ({caracteres} caracteres)."))
    if not numero:
        fallos.append(
            _grave(
                "numero",
                "El documento no trae su número de consulta reconocible: si la fila de la búsqueda tampoco lo trae, "
                "se guarda como «unknown-<id>» y no se puede deduplicar.",
            )
        )
    elif not _NUMERO_DGT.match(str(numero)):
        fallos.append(_leve("numero_raro", f"Número de consulta con forma desconocida: {numero!r}."))
    elif pedido and numero != pedido:
        fallos.append(_grave("otro_documento", f"Se pidió la consulta {pedido} y el documento es la {numero}."))
    return Lectura(fallos=fallos, metricas={"caracteres": caracteres})


def _registrar_dgt() -> None:
    from hacienda_gpt.cli.dgt_seed import ReglasBusquedaDgt, ReglasDocumentoDgt, extraer_busqueda

    registrar(
        Fuente(
            clave="dgt_busqueda",
            descripcion="resultados del buscador de consultas de la DGT (Petete/Knosys, HTML)",
            expectativas=(
                'Cada resultado es un <td id="doc_NNN"> con el número en un elemento class="NUM-CONSULTA".',
                'La página trae id="total_pages" y un campo oculto id="query" con la consulta Knosys, '
                "imprescindible para descargar cada documento.",
            ),
            reglas=ReglasBusquedaDgt,
            extraer=extraer_busqueda,
            canarios=_canarios_dgt,
            codigo="hacienda_gpt/cli/dgt_seed.py (_search_month, extraer_busqueda)",
        )
    )
    registrar(
        Fuente(
            clave="dgt_documento",
            descripcion="documento de una consulta de la DGT (Petete/Knosys, HTML); se observa uno por página",
            expectativas=(
                'El documento trae su número de consulta en un párrafo class="NUM-CONSULTA" («V0058-25»).',
                "Es la consulta que la fila de la búsqueda prometía.",
            ),
            reglas=ReglasDocumentoDgt,
            extraer=_extraer_documento_dgt,
            canarios=_canarios_documento_dgt,
            codigo="hacienda_gpt/cli/dgt_seed.py (_fetch_document, _parse_doc_num, numero_de_documento)",
        )
    )


def _canarios_teac_listado(ids: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    ids = ids if isinstance(ids, list) else []
    fallos: list[Fallo] = []
    if not ids:
        try:
            desde, hasta = (date(*reversed([int(x) for x in parte.split("/")])) for parte in contexto.split(".."))
            dias = (hasta - desde).days
        except (ValueError, TypeError):
            dias = 0
        detalle = (
            f"La primera página del listado ({contexto}) no enlaza ningún criterio: se da por terminado el rango "
            "sin descargar nada."
        )
        fallos.append(_grave("enlaces", detalle) if dias >= 60 else _leve("enlaces", detalle + " (rango corto)"))
    return Lectura(fallos=fallos, metricas={"criterios": len(ids)})


def _titulo(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return " ".join(m.group(1).split()) if m else ""


def _extraer_boe_seed(reglas: Any, html: str) -> dict[str, Any]:
    return {"titulo": _titulo(html)[:300], "consolidado": 'id="textoxslt"' in html, "caracteres": len(html)}


def _canarios_boe_seed(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    fallos: list[Fallo] = []
    titulo = str(datos.get("titulo") or "")
    if contexto and contexto not in titulo:
        fallos.append(
            _grave(
                "identificador",
                f"Se pidió {contexto} y la página guardada se titula {titulo[:120]!r}: no es la norma pedida.",
            )
        )
    return Lectura(fallos=fallos, metricas={"consolidado": float(bool(datos.get("consolidado")))})


def _registrar_seeds() -> None:
    from hacienda_gpt.cli.teac_seed import ReglasListadoTeac, ids_de_pagina

    registrar(
        Fuente(
            clave="teac_listado",
            descripcion="listado de criterios del TEAC por fechas (DYCTEA, criterios.aspx)",
            expectativas=(
                "Cada criterio del listado es un enlace href='criterio.aspx?id=<id compuesto>&amp;q=…'.",
                "Un rango de dos meses o más tiene algún criterio; una página sin enlaces se toma como fin del "
                "listado.",
            ),
            reglas=ReglasListadoTeac,
            extraer=ids_de_pagina,
            canarios=_canarios_teac_listado,
            codigo="hacienda_gpt/cli/teac_seed.py (collect_criterio_ids, ids_de_pagina)",
        )
    )


def _registrar_boe_seed() -> None:
    registrar(
        Fuente(
            clave="boe_seed",
            descripcion="descarga inicial de normas del BOE por identificador (boe_seed, buscar/act.php)",
            expectativas=(
                "act.php?id=<ID> devuelve la página de esa norma: su <title> empieza por el identificador (a "
                "veces tras «BOE.es - »). Una norma no consolidada redirige a su texto original (doc.php), que "
                "también vale.",
            ),
            canarios=_canarios_boe_seed,
            extraer=_extraer_boe_seed,
            codigo="hacienda_gpt/cli/boe_seed.py (fetch_document)",
        )
    )


def _registrar_rastreadores() -> None:
    from hacienda_gpt.crawler.boletin_autonomico import BUNDLED_SPECS, ReglasBoletin, parse_listing
    from hacienda_gpt.crawler.teac import ReglasTeac, extraer_criterio

    for spec in BUNDLED_SPECS.values():

        def extraer(reglas: Any, texto: str, spec: Any = spec) -> list[dict[str, Any]]:
            # A copy of the spec is not the bundled object, so ``parse_listing`` applies these rules as given.
            filas = parse_listing(replace(spec, **reglas.model_dump()), texto)
            return [{"title": f["title"], "date_raw": f["date_raw"], "link": f["link"]} for f in filas]

        registrar(
            Fuente(
                clave=f"boletin_{spec.key}",
                descripcion=f"listado del buscador del {spec.name}",
                expectativas=(
                    "La página de resultados es HTML servido por el servidor (sin JavaScript).",
                    "Cada resultado es un nodo seleccionado por «item_xpath»; dentro de él, «title_xpath», "
                    "«date_xpath» y «link_xpath» dan el título, la fecha y el enlace.",
                    "Una búsqueda por un término tributario habitual devuelve al menos un resultado.",
                ),
                reglas=ReglasBoletin,
                extraer=extraer,
                canarios=_canarios_boletin,
                codigo="hacienda_gpt/crawler/boletin_autonomico.py (parse_listing)",
            )
        )
    registrar(
        Fuente(
            clave="teac_criterio",
            descripcion="ficha de un criterio del TEAC (DYCTEA, HTML)",
            expectativas=(
                "Un id inexistente devuelve una página con el texto «El criterio no existe».",
                "Cada dato va rotulado (dt/dd, th/td, td/td o etiqueta seguida de su valor) con una de las "
                "etiquetas de «alias».",
                "Toda ficha tiene número de resolución (NN/NNNNN/AAAA), fecha (DD/MM/AAAA) y texto del criterio.",
            ),
            reglas=ReglasTeac,
            extraer=extraer_criterio,
            canarios=_canarios_teac,
            codigo="hacienda_gpt/crawler/teac.py (extraer_criterio, _extract_field)",
        )
    )


# --------------------------------------------------------------------------- #
# CENDOJ / Tribunal Supremo (no network of our own: only the reads the operator already asked for)
# --------------------------------------------------------------------------- #

MIN_LOTE = 3
_SIN_TEXTO = re.compile(r"(?is)<!--.*?-->|<(svg|noscript)\b.*?</\1\s*>")

# The only words of a CENDOJ page the watch keeps (see formatos_esqueleto): the labels the reader and the
# Supreme Court analysis rely on, court vocabulary, ordinals used as headings, months, and connectors.
# Nothing here names a person, a party, a place or a matter.
PALABRAS_CENDOJ = lista_permitida("""
    roj ecli es ts sts ats id cendoj organo sede seccion ponente fecha sala tribunal supremo nº º ª num
    numero recurso resolucion procedimiento tipo casacion contencioso administrativo civil penal social militar
    primera segunda tercera cuarta quinta sexta septima octava sentencia auto fundamentos derecho juridicos
    antecedentes hecho hechos fallo parte dispositiva voto particular interes casacional objetivo rec ordinario
    unificacion doctrina ley primero segundo tercero cuarto quinto sexto septimo octavo noveno decimo
    undecimo duodecimo vigesimo enero febrero marzo abril mayo junio julio agosto septiembre octubre
    noviembre diciembre de del la el lo los las y en a al para por
    """.split())


def _sin_ruido(crudo: str) -> str:
    """HTML without scripts, styles, comments or inline drawings: what a sample needs is text and links."""
    return _SIN_TEXTO.sub("", _SCRIPTS.sub("", crudo))


def esqueleto_cendoj(crudo: str) -> str:
    """What the watch keeps of a ruling, and all a model ever sees of it: its structural skeleton.

    The CENDOJ forbids building a database of rulings without the CGPJ's authorisation: not one word of
    the ruling outside :data:`PALABRAS_CENDOJ` is kept; figures keep only their shape ("STS 1111/1111").
    """
    return esqueleto_html(crudo, PALABRAS_CENDOJ)


def _recortar_html(crudo: str) -> str:
    """A rendered page without its noise, so that its text fits in a sample (a leading ``<base>`` stays)."""
    return _sin_ruido(crudo)[:MAX_MUESTRA_CHARS]


def _forma(valor: Any) -> Any:
    """The shape of an extracted value: every digit becomes ``#`` ("STS ####/####")."""
    return re.sub(r"\d", "#", valor) if isinstance(valor, str) else valor


def forma_documento(reglas: Any, html: str) -> dict[str, Any]:
    """What the gate compares for ``cendoj_documento``: which fields are found, and the shape of their values.

    Run on skeletons, never on literal values: a proposed spec is accepted only if it finds the same fields
    with the same shapes in the known-good skeleton and satisfies the canaries on the new one. Free text
    (the reporting judge) counts only as present or absent.
    """
    from hacienda_gpt.crawler.cendoj import MIN_PALABRAS_DOCUMENTO, extraer_documento

    campos = extraer_documento(reglas, html)
    return {
        "source_authority": campos["source_authority"],
        "sala": campos["sala"],
        "roj": _forma(campos["roj"]),
        "ecli": _forma(campos["ecli"]),
        "fecha_resolucion": _forma(campos["fecha_resolucion"]),
        "ponente": campos["ponente"] is not None,
        "is_tributario": campos["is_tributario"],
        "tributos_detectados": campos["tributos_detectados"],
        "con_texto": campos["palabras"] >= MIN_PALABRAS_DOCUMENTO,
    }


def _canarios_cendoj(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    fallos: list[Fallo] = []
    con_texto = bool(datos.get("con_texto"))
    if not con_texto:
        fallos.append(
            _grave(
                "pagina_vacia",
                "La página apenas tiene texto: es el armazón del portal o una página de error, no una resolución.",
            )
        )
    elif not datos.get("roj") and not datos.get("ecli"):
        fallos.append(_grave("identificadores", "No se reconoce ni el ROJ ni el ECLI de la resolución."))
    elif not datos.get("roj"):
        fallos.append(_leve("roj", "Se reconoce el ECLI pero no el ROJ: la sentencia se guarda con otro nombre."))
    if con_texto and not datos.get("fecha_resolucion"):
        fallos.append(
            _grave(
                "fecha",
                "No se reconoce ninguna fecha DD/MM/AAAA: sin ella una casación no se sitúa antes o después del "
                "22/07/2016 y pierde prioridad.",
            )
        )
    if con_texto and not datos.get("sala"):
        fallos.append(_leve("sala", "No se reconoce la sala."))
    if con_texto and not datos.get("ponente"):
        fallos.append(_leve("ponente", "No se reconoce el ponente."))
    return Lectura(fallos=fallos)


def _canarios_supremo(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    n = {clave: int(datos.get(clave) or 0) for clave in datos if isinstance(datos.get(clave), int | float)}
    fallos: list[Fallo] = []
    analizadas = n.get("analizadas", 0)
    if analizadas >= MIN_LOTE and n.get("con_fecha", 0) == 0:
        fallos.append(_grave("fechas", f"Ninguna de las {analizadas} resoluciones leídas tiene fecha reconocible."))
    if n.get("sala_tercera", 0) >= MIN_LOTE and n.get("sala_tercera_con_seccion", 0) == 0:
        fallos.append(
            _grave(
                "seccion",
                f"Ninguna de las {n['sala_tercera']} sentencias de la Sala Tercera tiene sección reconocible: sin ella "
                "ninguna se conserva (la cabecera habrá cambiado).",
            )
        )
    if analizadas >= MIN_LOTE and n.get("con_tipo_recurso", 0) == 0:
        fallos.append(
            _grave("tipo_recurso", f"En ninguna de las {analizadas} resoluciones se reconoce el tipo de recurso.")
        )
    if n.get("casacion_nueva", 0) >= 5 and n.get("fijan_doctrina", 0) == 0:
        fallos.append(
            _leve(
                "doctrina",
                f"Ninguna de las {n['casacion_nueva']} casaciones posteriores al 22/07/2016 fija doctrina según el "
                "lector: ¿ha cambiado la redacción de los fundamentos?",
            )
        )
    return Lectura(fallos=fallos, metricas={k: float(v) for k, v in n.items()})


def _registrar_cendoj() -> None:
    from hacienda_gpt.crawler.cendoj import ReglasCendoj

    registrar(
        Fuente(
            clave="cendoj_documento",
            descripcion="resolución del CENDOJ (poderjudicial.es, HTML), leída una a una",
            expectativas=(
                "La página trae el texto de la resolución (no el armazón de JavaScript del buscador).",
                "En el texto aparecen «ROJ: STS n/aaaa» y/o «ECLI:ES:TS:aaaa:n», una fecha DD/MM/AAAA, «Ponente:» "
                "y la sala («Sala Tercera», «Sala de lo Contencioso…»).",
                "Solo se observan las resoluciones que el operador ya ha pedido (lista de URL o ficheros locales): "
                "la vigilancia no descarga nada del CENDOJ.",
                "Por las condiciones del CENDOJ, de una resolución solo se guarda (y solo ves) su ESQUELETO: las "
                "etiquetas y nombres de atributo del marcado, los rótulos de los que depende el lector («ROJ», "
                "«ECLI», «Ponente», «Fecha», «Sala», «Sección», «Nº de Recurso»…), «_» en lugar de cualquier otra "
                "palabra y «1» en lugar de cada cifra. Lo extraído se compara por su forma («STS ####/####»), "
                "no por su valor.",
            ),
            reglas=ReglasCendoj,
            extraer=forma_documento,
            canarios=_canarios_cendoj,
            recortar=esqueleto_cendoj,
            codigo="hacienda_gpt/crawler/cendoj.py (extraer_documento, parse_cendoj_document)",
        )
    )
    registrar(
        Fuente(
            clave="supremo_lote",
            descripcion="cabecera de las sentencias del Supremo en una ejecución de supremo_seed (recuentos)",
            expectativas=(
                "Entre varias sentencias leídas, alguna tiene fecha y tipo de recurso reconocibles.",
                "Entre varias de la Sala Tercera, alguna tiene sección («Sección: 2», «Sección Segunda» o el código "
                "del Id Cendoj).",
                "Entre varias casaciones del régimen de la LO 7/2015, alguna fija doctrina.",
            ),
            canarios=_canarios_supremo,
            guarda_muestras=False,
            codigo="hacienda_gpt/doctrine/supremo.py (expresiones de cabecera, _detect_seccion, _detect_tipo_recurso, "
            "_extract_doctrina), hacienda_gpt/cli/supremo_seed.py",
        )
    )


# --------------------------------------------------------------------------- #
# AEAT sede (HTML and PDF crawlers)
# --------------------------------------------------------------------------- #

MIN_TEXTO_SEDE = 300


def _canarios_aeat_sede(enlaces: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    enlaces = enlaces if isinstance(enlaces, list) else []
    fallos: list[Fallo] = []
    visible = " ".join(re.sub(r"<[^>]+>", " ", _SCRIPTS.sub("", muestra)).split())
    if len(visible) < MIN_TEXTO_SEDE:
        fallos.append(
            _grave(
                "pagina_vacia", f"La página de entrada {contexto or ''} apenas tiene texto ({len(visible)} caracteres)."
            )
        )
    elif not enlaces:
        fallos.append(
            _grave(
                "enlaces",
                f"Ningún enlace de la página de entrada {contexto or ''} casa con las rutas que sigue el rastreador: "
                "la sede se habrá reorganizado y no se guardará nada más de esa sección.",
            )
        )
    return Lectura(fallos=fallos, metricas={"enlaces": len(enlaces)})


def _canarios_aeat_rastreo(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    paginas, semillas = int(datos.get("paginas") or 0), int(datos.get("semillas") or 0)
    fallidas = [f for f in datos.get("semillas_fallidas") or [] if isinstance(f, dict)]
    http = [f for f in fallidas if f.get("http")]
    fallos: list[Fallo] = []
    if paginas == 0 and len(fallidas) < semillas:
        fallos.append(_grave("paginas", "El rastreo de la sede ha terminado sin guardar ninguna página."))
    if http:
        detalle = "; ".join(f"{f.get('url')} (HTTP {f.get('http')})" for f in http[:5])
        if semillas and len(http) * 2 > semillas:
            fallos.append(_grave("semillas", f"Más de la mitad de las páginas de entrada ya no existen: {detalle}."))
        else:
            fallos.append(_leve("semillas", f"Páginas de entrada que ya no existen: {detalle}."))
    return Lectura(fallos=fallos, metricas={"paginas": paginas, "semillas_fallidas_http": len(http)})


def _canarios_aeat_pdf(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    claves = ("respuestas_pdf", "no_pdf", "guardados", "duplicados", "paginas")
    n = {clave: int(datos.get(clave) or 0) for clave in claves}
    fallos: list[Fallo] = []
    if n["paginas"] >= 20 and n["respuestas_pdf"] == 0:
        fallos.append(
            _leve("sin_pdf", f"Se han recorrido {n['paginas']} páginas de la sede sin encontrar ningún enlace .pdf.")
        )
    if n["respuestas_pdf"] >= MIN_LOTE and n["guardados"] + n["duplicados"] == 0:
        fallos.append(
            _grave(
                "no_pdf",
                f"Ninguna de las {n['respuestas_pdf']} respuestas a enlaces .pdf era un PDF (¿llegan como visor HTML?): "
                "no se ha guardado ninguno.",
            )
        )
    elif n["respuestas_pdf"] and n["no_pdf"] * 2 > n["respuestas_pdf"]:
        fallos.append(
            _leve("no_pdf", f"{n['no_pdf']} de {n['respuestas_pdf']} respuestas a enlaces .pdf no eran un PDF.")
        )
    return Lectura(fallos=fallos, metricas={k: float(v) for k, v in n.items()})


def _registrar_aeat() -> None:
    from hacienda_gpt.crawler.crawlers import ReglasSedeAeat, extraer_enlaces

    registrar(
        Fuente(
            clave="aeat_sede",
            descripcion="páginas de entrada de la sede de la AEAT (HTML renderizado)",
            expectativas=(
                "Cada página de entrada (/Sede/irpf.html, /Sede/iva.html…) existe y tiene texto.",
                "Algunos de sus enlaces casan con las «rutas_permitidas» del rastreador y están en sus «dominios»: "
                "por ellos sigue el rastreo.",
                "Una reorganización de la sede cambia las rutas: se añaden las nuevas junto a las antiguas.",
            ),
            reglas=ReglasSedeAeat,
            extraer=extraer_enlaces,
            canarios=_canarios_aeat_sede,
            recortar=_recortar_html,
            codigo="hacienda_gpt/crawler/crawlers.py (AgenciaTributariaWebCrawler.urls, extraer_enlaces)",
        )
    )
    registrar(
        Fuente(
            clave="aeat_rastreo",
            descripcion="ejecución del rastreador web de la sede de la AEAT (recuentos)",
            expectativas=(
                "Las páginas de entrada responden (no 404).",
                "Una ejecución guarda páginas, en número parecido a las anteriores.",
            ),
            canarios=_canarios_aeat_rastreo,
            caidas=(("paginas", 0.5),),
            guarda_muestras=False,
            codigo="hacienda_gpt/crawler/crawlers.py (AgenciaTributariaWebCrawler.urls)",
        )
    )
    registrar(
        Fuente(
            clave="aeat_pdf",
            descripcion="ejecución del rastreador de PDF de la sede de la AEAT (recuentos)",
            expectativas=(
                "Un enlace .pdf de la sede devuelve los bytes del PDF (empiezan por «%PDF»), no un visor HTML.",
                "Una ejecución guarda PDF, en número parecido a las anteriores.",
            ),
            canarios=_canarios_aeat_pdf,
            caidas=(("guardados", 0.5),),
            guarda_muestras=False,
            codigo="hacienda_gpt/crawler/crawlers.py (AgenciaTributariaPDFCrawler.parse_pdf)",
        )
    )


# --------------------------------------------------------------------------- #
# The BOE's autonomic codes (PDF) and the corpus conversion (Docling)
# --------------------------------------------------------------------------- #


def _canarios_ccaa_pdf(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    fallos: list[Fallo] = []
    no_pdf = [f for f in datos.get("no_pdf") or [] if isinstance(f, dict)]
    http = [f for f in datos.get("http") or [] if isinstance(f, dict)]
    guardados = list(datos.get("guardados") or [])
    if no_pdf:
        fallos.append(
            _grave(
                "no_pdf",
                "Respuestas que no son un PDF: "
                + "; ".join(
                    f"{f.get('ccaa')} (HTTP {f.get('status')}, empieza por {f.get('inicio', '')[:30]!r})"
                    for f in no_pdf[:5]
                )
                + ".",
            )
        )
    conocidos = [f for f in http if f.get("conocido")]
    if conocidos:
        fallos.append(
            _grave(
                "fichero",
                "El PDF de estos códigos ya no está donde se espera (abrir_pdf.php?fich=…): "
                + ", ".join(f"{f.get('ccaa')} (HTTP {f.get('status')})" for f in conocidos)
                + ".",
            )
        )
    if len(http) > len(conocidos):
        fallos.append(
            _leve(
                "fichero_no_verificado",
                "Códigos sin nombre de fichero verificado que no responden: "
                + ", ".join(f"{f.get('ccaa')} (HTTP {f.get('status')})" for f in http if not f.get("conocido"))
                + ".",
            )
        )
    if not guardados and int(datos.get("pedidos") or 0):
        fallos.append(_grave("guardados", "No se ha guardado ningún código autonómico."))
    return Lectura(fallos=fallos, metricas={"guardados": len(guardados)})


def _canarios_ccaa_texto(codigos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    from hacienda_gpt.ops.formatos_corpus import rendimiento

    codigos = [c for c in codigos if isinstance(c, dict)] if isinstance(codigos, list) else []
    fallos: list[Fallo] = []
    sin_texto = [c["fichero"] for c in codigos if not c.get("fragmentos") or not c.get("caracteres")]
    if sin_texto:
        fallos.append(
            _grave(
                "sin_texto",
                "PDF descargado pero sin texto extraído (¿escaneado, o Docling ya no lo entiende?): "
                + ", ".join(sin_texto[:10])
                + ". Se indexa como vacío.",
            )
        )
    sin_encabezados = [
        c["fichero"] for c in codigos if c.get("caracteres") and c.get("fragmentos") and not c.get("con_encabezado")
    ]
    if sin_encabezados:
        fallos.append(
            _grave(
                "sin_encabezados",
                "Texto sin ningún encabezado «Artículo N» ni «Disposición …»: la conversión ha perdido la estructura "
                "de la norma en " + ", ".join(sin_encabezados[:10]) + ".",
            )
        )
    return Lectura(fallos=fallos, metricas={"codigos": len(codigos), "caracteres_por_kb": rendimiento(codigos)})


def _canarios_corpus(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    ficheros, fragmentos = int(datos.get("ficheros") or 0), int(datos.get("fragmentos") or 0)
    vacios = int(datos.get("n_sin_fragmentos") or 0)
    fallos: list[Fallo] = []
    if ficheros and fragmentos == 0:
        fallos.append(_grave("fragmentos", f"Ninguno de los {ficheros} ficheros convertidos ha dado un fragmento."))
    elif ficheros >= 4 and vacios * 4 > ficheros:
        ejemplos = ", ".join(str(f) for f in (datos.get("sin_fragmentos") or [])[:5])
        fallos.append(
            _leve("sin_fragmentos", f"{vacios} de {ficheros} ficheros no han dado ningún fragmento ({ejemplos}).")
        )
    por_fichero = fragmentos / ficheros if ficheros else 0.0
    return Lectura(fallos=fallos, metricas={"ficheros": ficheros, "fragmentos_por_fichero": por_fichero})


def _registrar_ccaa() -> None:
    registrar(
        Fuente(
            clave="boe_ccaa_pdf",
            descripcion="PDF de los códigos electrónicos autonómicos del BOE (biblioteca_juridica/codigos)",
            expectativas=(
                "abrir_pdf.php?fich=<código>.pdf devuelve un PDF (empieza por «%PDF-») con estado 200.",
                "El nombre de fichero de cada código del catálogo sigue siendo válido.",
            ),
            canarios=_canarios_ccaa_pdf,
            codigo="hacienda_gpt/crawler/boe_consolidado.py (DEFAULT_CCAA_CODES, parse_pdf)",
        )
    )
    registrar(
        Fuente(
            clave="boe_ccaa_texto",
            descripcion="texto extraído (Docling) de los PDF de los códigos autonómicos",
            expectativas=(
                "Cada PDF de código produce fragmentos con texto.",
                "El texto conserva encabezados «Artículo N» o «Disposición adicional/transitoria/derogatoria/final».",
                "Los caracteres de texto por KB de PDF se parecen a los de ingestas anteriores.",
            ),
            canarios=_canarios_ccaa_texto,
            caidas=(("caracteres_por_kb", 0.5),),
            codigo="hacienda_gpt/processor/document_loader.py (Docling), hacienda_gpt/ops/formatos_corpus.py",
        )
    )
    registrar(
        Fuente(
            clave="corpus_docling",
            descripcion="conversión del corpus con Docling (recuentos por ingesta)",
            expectativas=(
                "Cada fichero HTML o PDF del corpus produce al menos un fragmento.",
                "Los fragmentos por fichero se parecen a los de ingestas anteriores.",
            ),
            canarios=_canarios_corpus,
            caidas=(("fragmentos_por_fichero", 0.5),),
            guarda_muestras=False,
            codigo="hacienda_gpt/processor/document_loader.py (process_documents, _process_one_file)",
        )
    )


def _registrar_calendario() -> None:
    from hacienda_gpt.ops.formatos_calendario import registrar_fuentes

    registrar_fuentes()


# --------------------------------------------------------------------------- #
# The user's documents: counters only
# --------------------------------------------------------------------------- #


def _canarios_documento_usuario(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    datos = datos if isinstance(datos, dict) else {}
    propuestos = int(datos.get("propuestos") or 0)
    descartados = list(datos.get("descartados") or [])
    fallos: list[Fallo] = []
    if propuestos == 0 and descartados:
        fallos.append(
            _leve(
                "todo_descartado",
                "La comprobación de cita literal ha descartado todos los datos leídos: " + ", ".join(descartados) + ".",
            )
        )
    elif propuestos == 0:
        fallos.append(_leve("sin_datos", "No se ha encontrado ningún dato del perfil en el documento."))
    elif len(descartados) > propuestos:
        fallos.append(
            _leve(
                "mas_descartes_que_datos",
                "Se han descartado más datos de los que se han propuesto: " + ", ".join(descartados) + ".",
            )
        )
    return Lectura(fallos=fallos, metricas={"propuestos": propuestos, "descartados": len(descartados)})


def _registrar_documentos() -> None:
    from hacienda_gpt.tax.documentos import ReglasDocumentos

    for clase in (*CLASES, OTROS):
        registrar(
            Fuente(
                clave=f"doc_{clase}",
                descripcion=f"lector de documentos del usuario ({clase.replace('_', ' ')})",
                expectativas=(
                    "El lector localiza al menos un dato del perfil fiscal en un documento de este tipo.",
                    "La cita literal de cada dato aparece en el texto del documento.",
                ),
                canarios=_canarios_documento_usuario,
                politica="tasa",
                guarda_muestras=False,
                codigo="hacienda_gpt/tax/documentos.py (SYSTEM_PROMPT, _hechos_validos)",
            )
        )

    def ensayo(reglas_propuestas: dict[str, Any], directorio: str | Path | None) -> Ensayo:
        """Gate of the vocabulary spec: valid, additive, and only generic fiscal words (enforced by the model)."""
        try:
            nuevas = ReglasDocumentos(**reglas_propuestas)
        except (ValueError, TypeError) as exc:
            return Ensayo(ok=False, motivos=[f"El vocabulario no es válido: {exc}"])
        vigentes = ReglasDocumentos(**spec_activa("documentos", directorio).reglas)
        previas = {canonico(item.model_dump())["campo"] + "|" + item.etiqueta for item in vigentes.vocabulario}
        propuestas = {item.campo + "|" + item.etiqueta for item in nuevas.vocabulario}
        if not previas <= propuestas:
            return Ensayo(ok=False, motivos=["La propuesta quita etiquetas vigentes: solo se admiten añadidos."])
        if propuestas == previas:
            return Ensayo(ok=False, motivos=["La propuesta no añade ninguna etiqueta."])
        return Ensayo(ok=True)

    registrar(
        Fuente(
            clave="documentos",
            descripcion="vocabulario adicional del lector de documentos del usuario",
            expectativas=(
                "Las etiquetas añadidas son rótulos genéricos de documentos fiscales, nunca datos de nadie.",
            ),
            canarios=lambda datos, muestra, contexto, reglas: Lectura(),
            reglas=ReglasDocumentos,
            guarda_muestras=False,
            ensayo=ensayo,
            auto_aplicable=False,
            codigo="hacienda_gpt/tax/documentos.py (anexo_vocabulario)",
        )
    )


for _grupo in (
    _registrar_boe,
    _registrar_dgt,
    _registrar_rastreadores,
    _registrar_seeds,
    _registrar_boe_seed,
    _registrar_cendoj,
    _registrar_aeat,
    _registrar_ccaa,
    _registrar_calendario,
    _registrar_documentos,
):
    try:
        _grupo()
    except ImportError as exc:  # e.g. scrapy missing on an API-only install
        logger.warning("Format watch: sources of %s not registered: %s", _grupo.__name__, exc)
