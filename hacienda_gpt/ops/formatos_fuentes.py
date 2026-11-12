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

logger = logging.getLogger(__name__)

_BOE_ID = re.compile(r"^BOE-[A-Z]-\d{4}-\d+$")
MAX_HIJOS_MUESTRA = 3


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
    filas, con_numero = int(datos.get("filas") or 0), int(datos.get("con_numero") or 0)
    fallos: list[Fallo] = []
    if not filas and not datos.get("total_paginas") and not datos.get("query"):
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


def _registrar_dgt() -> None:
    registrar(
        Fuente(
            clave="dgt_busqueda",
            descripcion="resultados del buscador de consultas de la DGT (Petete/Knosys, HTML)",
            expectativas=(
                'Cada resultado es un <td id="doc_NNN"> con el número en un elemento class="NUM-CONSULTA".',
                'La página trae id="total_pages" y un campo oculto id="query" con la consulta Knosys, '
                "imprescindible para descargar cada documento.",
            ),
            canarios=_canarios_dgt,
            codigo="hacienda_gpt/cli/dgt_seed.py (_search_month y expresiones _SEARCH_ROW_RE, _DOC_ID_RE…)",
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


for _grupo in (_registrar_boe, _registrar_dgt, _registrar_rastreadores, _registrar_documentos):
    try:
        _grupo()
    except ImportError as exc:  # e.g. scrapy missing on an API-only install
        logger.warning("Format watch: sources of %s not registered: %s", _grupo.__name__, exc)
