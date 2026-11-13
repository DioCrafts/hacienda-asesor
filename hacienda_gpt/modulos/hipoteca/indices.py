"""Official mortgage reference rates (Euríbor etc.) read from the Banco de España resolutions in the BOE.

Art. 27.2 of Orden EHA/2899/2011 says the official rates "se publicarán mensualmente por el Banco de
España en el «Boletín Oficial del Estado»". The BdE publishes two resolutions a month («Resolución de …,
del Banco de España, por la que se publican determinados tipos de interés oficiales de referencia del
mercado hipotecario»): one at the start of the month (Euríbor, €STR, IRS, Míbor of the previous month)
and one around the 18th (IRPH and the euro-area average). This module

- parses one resolution's XML (``https://www.boe.es/diario_boe/xml.php?id=<BOE id>``) into values per
  month and index, tolerating the formats used since 2004 (see :data:`_REGLAS`);
- keeps the series as versioned data (``data/indices_referencia.json``), every value with the BOE id it
  was read from, the literal figure and the publication date; a later «Corrección de erratas» wins;
- finds new resolutions with the BOE search (``/buscar/boe.php``) behind an injectable fetcher, so the
  tests never touch the network: ``python -m hacienda_gpt.modulos.hipoteca.indices actualizar``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import unicodedata
import urllib.parse
from xml.etree import ElementTree

DATA_DIR = Path(__file__).resolve().parent / "data"
SERIE_PATH = DATA_DIR / "indices_referencia.json"

BOE_XML_URL = "https://www.boe.es/diario_boe/xml.php?id={boe_id}"
BOE_DOC_URL = "https://www.boe.es/diario_boe/txt.php?id={boe_id}"

# Index keys -> Spanish name for the user.
INDICES: dict[str, str] = {
    "euribor_12m": "Euríbor a un año",
    "euribor_6m": "Euríbor a seis meses",
    "euribor_3m": "Euríbor a tres meses",
    "euribor_1m": "Euríbor a un mes",
    "euribor_1s": "Euríbor a una semana",
    "estr_12m": "Tipo de referencia basado en el €STR a un año",
    "estr_6m": "Tipo de referencia basado en el €STR a seis meses",
    "estr_3m": "Tipo de referencia basado en el €STR a tres meses",
    "estr_1m": "Tipo de referencia basado en el €STR a un mes",
    "estr_1s": "Tipo de referencia basado en el €STR a una semana",
    "irs_5a": "Permuta de intereses (IRS) a cinco años",
    "mibor_12m": "Míbor a un año",
    "irph_entidades": "IRPH (tipo medio de los préstamos hipotecarios a más de tres años, entidades en España)",
    "irph_zona_euro": "Tipo medio de los préstamos a la vivienda entre uno y cinco años en la zona euro",
    "deuda_publica_2_6a": "Rendimiento interno de la deuda pública de 2 a 6 años",
    # Published until the 2013 reform; kept for loans that still refer to them.
    "irph_bancos": "Tipo medio de los préstamos hipotecarios a más de tres años de bancos",
    "irph_cajas": "Tipo medio de los préstamos hipotecarios a más de tres años de cajas de ahorro",
    "ceca": "Tipo activo de referencia de las cajas de ahorro",
}

_MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_PLAZOS = {"una semana": "1s", "un mes": "1m", "tres meses": "3m", "seis meses": "6m", "un ano": "12m"}


# Two resolutions whose month the BOE text gets wrong and no «Corrección de erratas» fixed. The month is
# inferred from the publication pattern (the mid-month resolution gives the IRPH of the month before
# last: the one of 19-10-2026 gives «Septiembre de 2026»), and every value read from them says so.
MES_INFERIDO: dict[str, tuple[str | None, str, str]] = {
    "BOE-A-2013-531": (
        None,
        "2012-12",
        "La resolución de 18-1-2013 no imprime el mes; se infiere diciembre de 2012 (publicación de mitad de mes).",
    ),
    "BOE-A-2023-11879": (
        "2003-04",
        "2023-04",
        "El BOE imprime «Abril de 2003» en una resolución de 18-5-2023; se toma abril de 2023 (errata evidente).",
    ),
}


class FormatoResolucionError(ValueError):
    """The resolution could not be read: its format changed or it is not one of these resolutions."""


def _plano(text: str) -> str:
    """Lower case, no accents, single spaces: the form label rules match against."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("​", "")
    return " ".join(text.lower().split())


def _texto(element: ElementTree.Element) -> str:
    return " ".join("".join(element.itertext()).split())


# Label rules, in order: (pattern on the normalised label, index key or a section marker).
# A row whose label starts a section ("Euríbor. Plazos:") gives the context to the "– Un año." rows below it.
_REGLAS: tuple[tuple[str, str], ...] = (
    (r"short.?term rate|\bestr\b", "@estr"),
    (r"^\W*\d*\W*euribor\W*plazos", "@euribor"),
    (r"euribor a (una semana|un mes|tres meses|seis meses|un ano)", "euribor_"),
    (r"referencia interbancaria a (un|1) ano", "euribor_12m"),
    (r"^\W*(\w\W+)?(de )?bancos\W*\d?\W*$", "irph_bancos"),
    (r"^\W*(\w\W+)?(de )?cajas de ahorro\W*\d?\W*$", "irph_cajas"),
    (r"^\W*(\w\W+)?del conjunto de entidades de credito\W*\d?\W*$", "irph_entidades"),
    (r"tipo activo de referencia de las cajas de ahorro", "ceca"),
    (r"interest rate swap", "irs_5a"),
    (r"mibor", "mibor_12m"),
    (r"deuda publica", "deuda_publica_2_6a"),
    (r"tipo medio de los prestamos.*entre uno y cinco anos.*zona", "irph_zona_euro"),
    (r"tipo medio de los prestamos hipotecarios a mas de tres anos.*en espana", "irph_entidades"),
    (r"tipo medio de los prestamos hipotecarios a mas de tres anos", "irph_entidades"),
)


def _clave_de(etiqueta: str, seccion: str | None) -> tuple[str | None, str | None]:
    """(index key or None, new section) for one row label."""
    plano = _plano(etiqueta)
    sub = re.fullmatch(r"\W*(una semana|un mes|tres meses|seis meses|un ano)\W*", plano)
    if sub:
        return (f"{seccion}_{_PLAZOS[sub.group(1)]}" if seccion else None), seccion
    for patron, clave in _REGLAS:
        m = re.search(patron, plano)
        if not m:
            continue
        if clave.startswith("@"):
            return None, clave[1:]
        if clave == "euribor_":
            return f"euribor_{_PLAZOS[m.group(1)]}", None
        return clave, None
    return None, seccion


def _valor(literal: str) -> Decimal | None:
    limpio = literal.strip().replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    limpio = limpio.replace(" ", "").removesuffix("%")
    if not re.fullmatch(r"-?\d+(,\d+)?", limpio):
        return None
    try:
        return Decimal(limpio.replace(",", "."))
    except InvalidOperation:  # pragma: no cover - the regex already rules it out
        return None


def _mes_en(texto: str) -> str | None:
    """'Octubre de 2026', 'Mayo 2025', '… vivienda1.Septiembre de 2008:', 'Octubre 2004 Porcentaje' -> 'AAAA-MM'."""
    m = re.search(r"(?:^|[\s.:\d])([a-z]+),? (?:de )?(\d{4})\W*(?:porcentaje\W*)?$", _plano(texto))
    if m and m.group(1) in _MESES:
        return f"{int(m.group(2)):04d}-{_MESES[m.group(1)]:02d}"
    return None


@dataclass(frozen=True)
class ValorIndice:
    mes: str  # "AAAA-MM": the month the value refers to (not the publication month)
    indice: str
    valor: Decimal  # percent, as published (3.239 means 3,239 %)
    literal: str  # the figure exactly as the BOE prints it ("3,239")
    nota: str | None = None  # set when the month is inferred (see MES_INFERIDO)


@dataclass
class Resolucion:
    boe_id: str
    titulo: str
    publicado: date
    valores: list[ValorIndice] = field(default_factory=list)
    # Rows whose label no rule recognises: the format watch reports them instead of guessing.
    desconocidas: list[str] = field(default_factory=list)
    es_correccion: bool = False
    # A «Corrección de erratas» in words: the resolution it corrects ("Resolución de 16 de junio de 2006"),
    # the month it says was missing, and the figures it replaces ("donde dice: «5,148», debe decir: «5,418»").
    corrige: str | None = None
    mes_omitido: str | None = None
    cambios: list[tuple[str, str]] = field(default_factory=list)


def _metadatos(raiz: ElementTree.Element) -> tuple[str, str, date, ElementTree.Element]:
    boe_id = (raiz.findtext("metadatos/identificador") or "").strip()
    titulo = " ".join((raiz.findtext("metadatos/titulo") or "").split())
    fecha = (raiz.findtext("metadatos/fecha_publicacion") or "").strip()
    texto = raiz.find("texto")
    if not boe_id or not re.fullmatch(r"\d{8}", fecha) or texto is None:
        raise FormatoResolucionError("Faltan el identificador, la fecha de publicación o el texto.")
    if "banco de espana" not in _plano(titulo) or "referencia" not in _plano(titulo):
        raise FormatoResolucionError(f"{boe_id} no es una resolución de tipos de referencia: «{titulo}».")
    return boe_id, titulo, date(int(fecha[:4]), int(fecha[4:6]), int(fecha[6:])), texto


def parsear_resolucion(xml: str | bytes, mes_si_falta: str | None = None) -> Resolucion:
    """Read one BdE resolution (or its «Corrección de erratas») from the BOE XML.

    ``mes_si_falta`` is the month a later correction says the resolution forgot to print.
    """
    try:
        raiz = ElementTree.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ElementTree.ParseError as exc:
        raise FormatoResolucionError(f"XML ilegible: {exc}") from exc
    boe_id, titulo, publicado, texto = _metadatos(raiz)
    res = Resolucion(boe_id=boe_id, titulo=titulo, publicado=publicado)
    impreso, inferido, nota = MES_INFERIDO.get(boe_id, (None, None, None))
    if inferido and impreso is None:
        mes_si_falta = inferido
    if "correccion de errat" in _plano(titulo):
        res.es_correccion = True
        m = re.search(r"(Resoluci[oó]n de \d+ de \w+ de \d{4})", titulo)
        res.corrige = m.group(1) if m else None
        prosa = " ".join(_texto(p) for p in texto.iter("p"))
        omitido = re.search(r"debe decir: «([^»]+)»", prosa)
        if omitido and _mes_en(omitido.group(1)):
            res.mes_omitido = _mes_en(omitido.group(1))
        res.cambios = re.findall(r"donde dice: «(-?[\d.,]+)», debe decir: «(-?[\d.,]+)»", prosa)
        if texto.find(".//tr") is None:
            return res  # a correction in words only: applied by construir_serie
    mes: str | None = None
    seccion: str | None = None
    for el in texto.iter():
        if el.tag in {"p", "caption"}:
            # Footnotes and the signature ("Madrid, 3 de noviembre de 2026.–…") never name the month.
            if (el.get("class") or "").startswith("cita") or _plano(_texto(el)).startswith("madrid"):
                continue
            if mes_parrafo := _mes_en(_texto(el)):
                mes, seccion = mes_parrafo, None
        elif el.tag == "tr":
            celdas = [_texto(td) for td in el.findall("td")] or [_texto(th) for th in el.findall("th")]
            if not celdas:
                continue
            # One cell: a section header spanning the row. Three: number, label, figure (2016).
            etiqueta = " ".join(celdas[:-1]) if len(celdas) > 1 else celdas[0]
            literal = celdas[-1] if len(celdas) > 1 else ""
            valor = _valor(literal)
            if valor is None:
                if mes_fila := _mes_en(" ".join(celdas)):
                    mes, seccion = mes_fila, None
                    continue
            clave, seccion = _clave_de(etiqueta, seccion)
            if valor is None:
                continue  # section headers and empty cells
            if clave is None:
                res.desconocidas.append(f"{etiqueta} = {literal}")
                continue
            mes = mes or mes_si_falta
            if mes is None:
                raise FormatoResolucionError(f"{boe_id}: hay cifras antes de saber a qué mes se refieren.")
            if inferido and mes in (impreso, inferido):
                res.valores.append(ValorIndice(inferido, clave, valor, literal.strip(), nota))
            else:
                res.valores.append(ValorIndice(mes=mes, indice=clave, valor=valor, literal=literal.strip()))
    if not res.valores:
        raise FormatoResolucionError(f"{boe_id}: no se ha encontrado ningún tipo en la tabla.")
    return res


# --- The stored series ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dato:
    """One published value as stored: what, from where."""

    indice: str
    mes: str
    valor: Decimal
    literal: str
    boe_id: str
    publicado: date

    @property
    def cita(self) -> str:
        return f"{INDICES.get(self.indice, self.indice)} de {_mes_texto(self.mes)}: {self.literal} % ({self.boe_id})"


def _mes_texto(mes: str) -> str:
    nombres = {v: k for k, v in _MESES.items() if k != "setiembre"}
    year, month = mes.split("-")
    return f"{nombres[int(month)]} de {year}"


@dataclass(frozen=True)
class SerieIndices:
    consultado_el: str
    datos: dict[str, dict[str, Dato]]  # index -> month -> value
    documentos: dict[str, dict[str, str]]

    def valor(self, indice: str, mes: str) -> Dato | None:
        return self.datos.get(indice, {}).get(mes)

    def ultimo(self, indice: str, publicado_hasta: date | None = None) -> Dato | None:
        """Latest value of ``indice`` already published on ``publicado_hasta`` (inclusive)."""
        candidatos = [
            d for d in self.datos.get(indice, {}).values() if publicado_hasta is None or d.publicado <= publicado_hasta
        ]
        return max(candidatos, key=lambda d: (d.mes, d.publicado)) if candidatos else None

    def anterior(self, dato: Dato) -> Dato | None:
        meses = sorted(m for m in self.datos.get(dato.indice, {}) if m < dato.mes)
        return self.datos[dato.indice][meses[-1]] if meses else None


def serie_desde_json(raw: dict) -> SerieIndices:
    datos: dict[str, dict[str, Dato]] = {}
    for indice, meses in raw["series"].items():
        datos[indice] = {
            mes: Dato(
                indice=indice,
                mes=mes,
                valor=Decimal(v["valor"]),
                literal=v["literal"],
                boe_id=v["boe_id"],
                publicado=date.fromisoformat(v["publicado"]),
            )
            for mes, v in meses.items()
        }
    return SerieIndices(consultado_el=raw["consultado_el"], datos=datos, documentos=raw.get("documentos", {}))


@lru_cache(maxsize=1)
def cargar_serie() -> SerieIndices:
    return serie_desde_json(json.loads(SERIE_PATH.read_text(encoding="utf-8")))


def leer_resoluciones(xmls: dict[str, bytes]) -> tuple[list[Resolucion], list[str]]:
    """Parse every XML, applying the corrections in words («Corrección de erratas»): the month a resolution
    forgot to print and the figures it misprinted. Returns the resolutions and the problems found."""
    problemas: list[str] = []
    correcciones: list[Resolucion] = []
    originales: dict[str, bytes] = {}
    for boe_id, xml in xmls.items():
        try:
            res = parsear_resolucion(xml)
        except FormatoResolucionError as exc:
            if "mes se refieren" in str(exc):
                originales[boe_id] = xml  # may be fixed by a correction below
            else:
                problemas.append(str(exc))
            continue
        if res.es_correccion and not res.valores:
            correcciones.append(res)
        else:
            originales[boe_id] = xml
            problemas += [f"{boe_id}: fila no reconocida: {fila}" for fila in res.desconocidas]
    resoluciones: list[Resolucion] = []
    for boe_id, xml in originales.items():
        titulo = " ".join((ElementTree.fromstring(xml).findtext("metadatos/titulo") or "").split())
        suyas = [c for c in correcciones if c.corrige and titulo.startswith(c.corrige + ",")]
        mes_omitido = next((c.mes_omitido for c in suyas if c.mes_omitido), None)
        try:
            res = parsear_resolucion(xml, mes_si_falta=mes_omitido)
        except FormatoResolucionError as exc:
            problemas.append(str(exc))
            continue
        for corr in suyas:
            for dice, debe_decir in corr.cambios:
                afectados = [i for i, v in enumerate(res.valores) if v.literal == dice]
                if len(afectados) != 1:
                    problemas.append(f"{corr.boe_id}: «{dice}» aparece {len(afectados)} veces en {boe_id}")
                    continue
                v = res.valores[afectados[0]]
                nuevo = _valor(debe_decir)
                if nuevo is None:
                    problemas.append(f"{corr.boe_id}: «{debe_decir}» no es una cifra")
                    continue
                res.valores[afectados[0]] = ValorIndice(mes=v.mes, indice=v.indice, valor=nuevo, literal=debe_decir)
                res.titulo += f" [corregida por {corr.boe_id}]"
        resoluciones.append(res)
    for corr in correcciones:
        if not any(corr.corrige and r.titulo.startswith(corr.corrige + ",") for r in resoluciones):
            problemas.append(f"{corr.boe_id}: no se encuentra la resolución que corrige ({corr.corrige})")
    return resoluciones, problemas


def construir_serie(
    resoluciones: Iterable[Resolucion],
    consultado_el: date,
    xml_sha256: dict[str, str],
    base: dict | None = None,
) -> dict:
    """The JSON of the series from parsed resolutions, added to ``base`` (an existing series) if given.
    Same month and index: the latest publication wins (a «Corrección de erratas» that reprints the table
    replaces the table it corrects)."""
    series: dict[str, dict[str, dict[str, str]]] = {k: dict(v) for k, v in (base or {}).get("series", {}).items()}
    documentos: dict[str, dict[str, str]] = dict((base or {}).get("documentos", {}))
    for res in sorted(resoluciones, key=lambda r: (r.publicado, r.boe_id)):
        documentos[res.boe_id] = {"publicado": res.publicado.isoformat(), "sha256_xml": xml_sha256.get(res.boe_id, "")}
        if res.es_correccion or "[corregida por" in res.titulo:
            documentos[res.boe_id]["titulo"] = res.titulo
        for v in res.valores:
            anterior = series.setdefault(v.indice, {}).get(v.mes)
            nuevo = {"valor": str(v.valor), "literal": v.literal, "boe_id": res.boe_id, "publicado": res.publicado.isoformat()}
            if v.nota:
                nuevo["nota"] = v.nota
            if anterior and anterior["valor"] != nuevo["valor"]:
                nuevo["sustituye"] = f"{anterior['boe_id']}: {anterior['literal']}"
            series[v.indice][v.mes] = nuevo
    ordenadas = {k: dict(sorted(series[k].items())) for k in INDICES if k in series}
    return {
        "descripcion": (
            "Tipos de interés oficiales de referencia del mercado hipotecario publicados mensualmente en el BOE "
            "por el Banco de España (art. 27 Orden EHA/2899/2011). Cada valor lleva el BOE del que se leyó; "
            "'mes' es el mes al que se refiere el tipo, no el de publicación."
        ),
        "fuente": {
            "referencia": "Resoluciones mensuales del Banco de España en el BOE (sección III)",
            "url_busqueda": "https://www.boe.es/buscar/boe.php",
            "url_documento": BOE_XML_URL,
            "base_legal": "art. 27.2 Orden EHA/2899/2011 (BOE-A-2011-17015)",
        },
        "consultado_el": consultado_el.isoformat(),
        "unidad": "porcentaje",
        "series": ordenadas,
        "documentos": dict(sorted(documentos.items())),
    }


# --- Finding and downloading resolutions (network only from the CLI) -----------------------------

Fetcher = Callable[[str], bytes]


def url_busqueda(titulo: str, desde: date, hasta: date) -> str:
    """BOE search (whole BOE, by title words and publication dates), 100 hits per page."""
    params: list[tuple[str, str]] = [("campo[0]", "ORIS")]
    params += [(f"dato[0][{k}]", k) for k in "12345T"]
    params += [("operador[0]", "and"), ("campo[1]", "TITULOS"), ("dato[1]", titulo), ("operador[1]", "and")]
    for i, campo in ((2, "DEM"), (3, "DOC"), (4, "NBOS"), (5, "NOF")):
        params += [(f"campo[{i}]", campo), (f"dato[{i}]", ""), (f"operador[{i}]", "and")]
    params += [("operador[6]", "and"), ("campo[6]", "FPU"), ("dato[6][0]", desde.isoformat())]
    params += [("dato[6][1]", hasta.isoformat()), ("page_hits", "100"), ("sort_field[0]", "FPU")]
    params += [("sort_order[0]", "desc"), ("sort_field[1]", "ORI"), ("sort_order[1]", "asc")]
    params += [("sort_field[2]", "REF"), ("sort_order[2]", "asc"), ("accion", "Buscar")]
    return "https://www.boe.es/buscar/boe.php?" + urllib.parse.urlencode(params)


# Title words that find every format since 2004 (the title changed in 2012 and 2020).
BUSQUEDAS = (
    "Banco de España tipos referencia oficiales",
    "Banco de España referencia mercado hipotecario",
    "Banco de España índices de referencia oficiales préstamos hipotecarios",
)


def ids_en_resultados(pagina_html: str) -> list[str]:
    """BOE ids of the hits of a search results page that are BdE reference-rate resolutions."""
    ids: list[str] = []
    for item in re.findall(r'<li class="resultado-busqueda[^"]*">(.*?)</li>', pagina_html, re.S):
        texto = _plano(html.unescape(re.sub(r"<[^>]+>", " ", item)))
        if "banco de espana" not in texto or "referencia" not in texto or "deuda publica" in texto:
            continue
        found = re.search(r"BOE-A-\d{4}-\d+", item)
        if found and found.group(0) not in ids:
            ids.append(found.group(0))
    return ids


def actualizar(fetch: Fetcher, desde: date, hasta: date, existentes: set[str]) -> dict[str, bytes]:
    """XML of every resolution published in [desde, hasta] that is not in ``existentes``."""
    nuevos: list[str] = []
    for titulo in BUSQUEDAS:
        pagina = fetch(url_busqueda(titulo, desde, hasta)).decode("utf-8", errors="replace")
        nuevos += [i for i in ids_en_resultados(pagina) if i not in existentes and i not in nuevos]
    return {boe_id: fetch(BOE_XML_URL.format(boe_id=boe_id)) for boe_id in nuevos}


def _fetch_urllib(url: str) -> bytes:  # pragma: no cover - network
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "hacienda-gpt/hipoteca"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data: bytes = resp.read()
        return data


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - network / maintenance
    """``actualizar [AAAA-MM-DD]``: download the resolutions published since that date (default: the last
    stored publication) and add them to the series. ``reconstruir <dir>``: rebuild the whole series from
    the ``BOE-A-*.xml`` files of a directory."""
    args = sys.argv[1:] if argv is None else argv
    hoy = date.today()
    base: dict | None = None
    if args[:1] == ["reconstruir"] and len(args) == 2:
        xmls = {p.stem: p.read_bytes() for p in sorted(Path(args[1]).glob("BOE-A-*.xml"))}
    elif args[:1] == ["actualizar"]:
        base = json.loads(SERIE_PATH.read_text(encoding="utf-8"))
        ultimo = max(date.fromisoformat(d["publicado"]) for d in base["documentos"].values())
        desde = date.fromisoformat(args[1]) if len(args) > 1 else ultimo
        xmls = actualizar(_fetch_urllib, desde, hoy, set(base["documentos"]))
        if not xmls:
            print("Sin resoluciones nuevas.")
            return 0
    else:
        print(main.__doc__)
        return 2
    resoluciones, problemas = leer_resoluciones(xmls)
    for problema in problemas:
        print(f"AVISO {problema}")
    sha = {boe_id: hashlib.sha256(xml).hexdigest() for boe_id, xml in xmls.items()}
    serie = construir_serie(resoluciones, hoy, sha, base)
    SERIE_PATH.write_text(json.dumps(serie, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Serie escrita en {SERIE_PATH}: {len(resoluciones)} resolución(es) leída(s), {len(problemas)} aviso(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
