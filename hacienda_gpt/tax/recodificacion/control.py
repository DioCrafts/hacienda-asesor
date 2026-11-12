"""Deterministic checks shared by every re-encoding adapter (no LLM here).

Three families of helpers:

* locating the article(s) of a law in a BOE consolidated page and flattening
  them to the text the reader and the reviewer are shown;
* literal-quote and "every figure is inside a quote" checks, ported from the
  self-check the human-driven encoders had to pass (``check_ded3.py``) and built
  on the same normalisation ``verificacion`` uses;
* the effects date of a wording, which decides the tax years a proposal touches.

Nothing in this module trusts the model: a proposal that does not pass these
checks is never written to a parameter file.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import re
from typing import Any
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.verificacion import _comprimido, _formas

# A quote shorter than this (ignoring whitespace) proves nothing: "300" is in every law.
MIN_CITA = 15
CENT = Decimal("0.01")


class Control(BaseModel):
    """Outcome of the deterministic gate.

    ``fallos`` block the proposal. ``revisar`` are things the gate could not
    decide by its rules: they do not block a proposal, but a proposal that has
    any is never applied without a person.
    """

    model_config = ConfigDict(extra="forbid")

    fallos: list[str] = Field(default_factory=list)
    revisar: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fallos


# --------------------------------------------------------------------------- #
# Article text
# --------------------------------------------------------------------------- #

# Latin ordinals of inserted articles: bis, ter, quater, quinquies… (laws also print "sexties", "sexdecies"…).
_SUFIJOS = r"bis|ter|quater|[a-z]{2,}ies"
_NUMERO = rf"\d+(?:-\d+)?(?:\s+(?:{_SUFIJOS})\b)?"
_ETIQUETA_RE = re.compile(rf"\bart(?:iculo|\.)?\s*({_NUMERO})", re.IGNORECASE)
_CABECERA_RE = re.compile(rf"^articulo\s+({_NUMERO})\s*(?:[.:]|$)", re.IGNORECASE)
_BLOQUE_RE = re.compile(r"^\[Bloque\b")
_FRONTERA_RE = re.compile(
    r"^(?:T[IÍ]TULO\b|CAP[IÍ]TULO\b|Secci[oó]n\s+\d|Subsecci[oó]n\s+\d|LIBRO\b|ANEXO\b|"
    r"Disposici[oó]n\s+(?:adicional|transitoria|derogatoria|final)\b)"
)
_DISPOSICION_RE = re.compile(
    r"^disposicion\s+(adicional|transitoria|derogatoria|final)\s+([a-z ]+?|\d+)\s*(?:\.[ªa]?)?\s*[.:]", re.IGNORECASE
)
_UI_RE = re.compile(r"^(?:Seleccionar redacci[oó]n|Subir$|\[Bloque\b)", re.IGNORECASE)
# An article longer than this is cut down to the apartado a group is read from (when it can be isolated).
UMBRAL_ACOTAR = 20_000
# A first-level apartado still longer than this is cut down to its sub-apartado.
UMBRAL_APARTADO = 15_000
_FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.S | re.I)
_LABEL_RE = re.compile(r"<input\b[^>]*\bchecked\b[^>]*>\s*<label\b[^>]*>(.*?)</label>", re.S | re.I)
_SALTO = frozenset({"p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"})
_OMITIR = frozenset({"script", "style", "head", "noscript"})
REDACCION = "Redacción mostrada por el BOE:"


def _sin_acentos(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", texto)
    return "".join(ch for ch in descompuesto if not unicodedata.combining(ch))


def articulo_base(etiqueta: str) -> str | None:
    """The article of the law a label refers to: "art. 5.Dos.1" → "5", "art. 9 quáter" → "9 quater"."""
    match = _ETIQUETA_RE.search(_sin_acentos(etiqueta))
    return " ".join(match.group(1).lower().split()) if match else None


class _Lineas(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._omitir = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _OMITIR:
            self._omitir += 1
        elif tag in _SALTO:
            self.partes.append("\n")
        elif tag in ("td", "th"):
            self.partes.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _OMITIR:
            self._omitir = max(0, self._omitir - 1)
        elif tag in _SALTO:
            self.partes.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._omitir:
            self.partes.append(re.sub(r"\s+", " ", data))


def _version_mostrada(form: re.Match[str]) -> str:
    """The BOE's version selector reduced to the label of the wording shown."""
    label = _LABEL_RE.search(form.group(1))
    if label is None:
        return " "
    texto = " ".join(re.sub(r"<[^>]+>", " ", label.group(1)).split())
    return f"<p>{REDACCION} {texto}</p>"


def lineas_de(pagina: str) -> list[str]:
    """Visible text of a BOE page, one line per block element; version selectors become one line."""
    parser = _Lineas()
    parser.feed(_FORM_RE.sub(_version_mostrada, pagina))
    parser.close()
    texto = unicodedata.normalize("NFKC", "".join(parser.partes))
    lineas = (" ".join(linea.split()) for linea in texto.split("\n"))
    return [linea for linea in lineas if linea]


@dataclass(frozen=True)
class TextoLegal:
    """The articles of a law a group of parameters is read from."""

    texto: str
    encontrados: tuple[str, ...]
    faltan: tuple[str, ...]
    sha256: str = field(default="")

    @property
    def comprimido(self) -> str:
        return _comprimido(self.texto)


_ORDINALES_UNIDAD = "primera segunda tercera cuarta quinta sexta septima octava novena".split()
_ORDINALES_DECENA = {
    20: "vigesima",
    30: "trigesima",
    40: "cuadragesima",
    50: "quincuagesima",
    60: "sexagesima",
    70: "septuagesima",
    80: "octogesima",
    90: "nonagesima",
    100: "centesima",
}
_CLASES_DISPOSICION = {"adicional": "da", "transitoria": "dt", "derogatoria": "dd", "final": "df"}


def _clave_ordinal(texto: str) -> str:
    """Spelling-insensitive key of an ordinal: "vigésimo sexta", "vigesimosexta" and "vigésima sexta" are one."""
    plano = re.sub(r"[^a-z0-9]", "", _sin_acentos(texto).lower())
    return re.sub(r"(decim|esim)[oa]", r"\1a", plano)


def _ordinales() -> dict[str, int]:
    """Feminine ordinals in words, as the BOE heads its provisions, by their spelling-insensitive key."""
    out: dict[str, int] = {}
    for n in range(1, 200):
        centena, resto = divmod(n, 100)
        prefijo = "centesima" if centena else ""
        if resto == 0:
            formas = [prefijo]
        elif resto < 10:
            formas = [prefijo + _ORDINALES_UNIDAD[resto - 1]]
        elif resto == 10:
            formas = [prefijo + "decima"]
        elif resto < 20:
            formas = [prefijo + "decima" + _ORDINALES_UNIDAD[resto - 11]]
            if resto == 11:
                formas.append(prefijo + "undecima")
            if resto == 12:
                formas.append(prefijo + "duodecima")
        else:
            decena, unidad = resto - resto % 10, resto % 10
            formas = [prefijo + _ORDINALES_DECENA[decena] + (_ORDINALES_UNIDAD[unidad - 1] if unidad else "")]
        if resto % 10 == 8 and resto > 10:
            # "decimoctava", "vigesimoctava": the vowel is elided.
            formas.append(formas[0][: -len("aoctava")] + "octava")
        for forma in formas:
            out[_clave_ordinal(forma)] = n
    return out


_ORDINAL_A_NUMERO = _ordinales()


def clave_de_disposicion(linea: str) -> str | None:
    """Key of the provision a heading opens: "Disposición adicional sexagésima primera. …" → "da 61"."""
    match = _DISPOSICION_RE.match(_sin_acentos(linea))
    if match is None:
        return None
    clase, ordinal = _CLASES_DISPOSICION[match.group(1).lower()], match.group(2).strip().lower()
    if ordinal.isdigit():
        return f"{clase} {int(ordinal)}"
    if ordinal == "unica":
        return f"{clase} unica"
    numero = _ORDINAL_A_NUMERO.get(_clave_ordinal(ordinal))
    return f"{clase} {numero}" if numero is not None else None


def _bloques(pagina: str) -> list[tuple[str, list[str]]]:
    """(key, lines) for every "Artículo N." block and every provision ("da 61") of the page, in order."""
    bloques: list[tuple[str, list[str]]] = []
    actual: list[str] | None = None
    for linea in lineas_de(pagina):
        cabecera = _CABECERA_RE.match(_sin_acentos(linea))
        if cabecera:
            actual = [linea]
            bloques.append((" ".join(cabecera.group(1).lower().split()), actual))
        elif _BLOQUE_RE.match(linea):
            actual = None
        elif _FRONTERA_RE.match(linea):
            disposicion = clave_de_disposicion(linea)
            actual = [linea] if disposicion is not None else None
            if actual is not None and disposicion is not None:
                bloques.append((disposicion, actual))
        elif actual is not None and not _UI_RE.match(linea):
            actual.append(linea)
    return bloques


# --------------------------------------------------------------------------- #
# References: which blocks of the law a group is read from
# --------------------------------------------------------------------------- #

_SUFIJOS_EN_ORDEN = (
    "",
    "bis",
    "ter",
    "quater",
    "quinquies",
    "sexies",
    "septies",
    "octies",
    "nonies",
    "decies",
    "undecies",
    "duodecies",
)
_CORTE_REFERENCIA = re.compile(r"\s*(?:\(|;|,\s*redacci|,\s*anadid|\ben\s+(?:su|la)\s+redacci)", re.IGNORECASE)
_ARTICULOS_RE = re.compile(r"\bart(?:iculo)?(s)?\.?\s*(?=\d)", re.IGNORECASE)
_ITEM_RE = re.compile(rf"({_NUMERO})((?:\.(?:[a-z]\)|\d+\.?[ºª]|\d+|[a-z]+))*\)?)", re.IGNORECASE)
_SEPARADOR_RE = re.compile(r"\s*(,|\by\b|\ba\b)\s*(?=\d)", re.IGNORECASE)
_DISPOSICION_REF_RE = re.compile(
    r"\bdisp(?:osicion|\.)\s+(adicional|transitoria|derogatoria|final)\s+(\d+|unica)", re.IGNORECASE
)


def _rango(desde: str, hasta: str) -> list[str]:
    """Articles between two bases: "57"…"60", or "2 bis"…"2 quinquies"."""
    n1, _, s1 = desde.partition(" ")
    n2, _, s2 = hasta.partition(" ")
    if n1 == n2 and s1 in _SUFIJOS_EN_ORDEN and s2 in _SUFIJOS_EN_ORDEN:
        i, j = _SUFIJOS_EN_ORDEN.index(s1), _SUFIJOS_EN_ORDEN.index(s2)
        return [f"{n1} {s}".strip() for s in _SUFIJOS_EN_ORDEN[i : j + 1]]
    if n1.isdigit() and n2.isdigit() and not s1 and not s2 and 0 < int(n2) - int(n1) <= 40:
        return [str(n) for n in range(int(n1), int(n2) + 1)]
    return [desde, hasta]


def articulos_de_referencia(referencia: str) -> list[str]:
    """Blocks of the law a source reference names, as keys of :func:`extraer_articulos`.

    "arts. 57 a 60 LIRPF" → 57, 58, 59, 60; "arts. 19.2.f) y 20" → 19, 20; "art. 96.2 y 3" → 96 (after a singular
    "art." what follows "y" is a subdivision); "arts. 2 a 2 quater" → 2, 2 bis, 2 ter, 2 quater; "disposición
    adicional 61.ª" → "da 61". Only the part before a parenthesis or "redacción de…" is read: what follows names
    the amending norms, not the law.
    """
    principal = _CORTE_REFERENCIA.split(_sin_acentos(referencia), maxsplit=1)[0]
    encontrados: list[tuple[int, str]] = []
    for match in _DISPOSICION_REF_RE.finditer(principal):
        ordinal = match.group(2).lower()
        clave = f"{_CLASES_DISPOSICION[match.group(1).lower()]} {int(ordinal) if ordinal.isdigit() else ordinal}"
        encontrados.append((match.start(), clave))
    for match in _ARTICULOS_RE.finditer(principal):
        plural, posicion, anterior = bool(match.group(1)), match.end(), None
        while True:
            item = _ITEM_RE.match(principal, posicion)
            if item is None:
                break
            base = " ".join(item.group(1).lower().split())
            if anterior is not None:
                encontrados += [(match.start(), b) for b in _rango(anterior, base)[1:]]
            else:
                encontrados.append((match.start(), base))
            separador = _SEPARADOR_RE.match(principal, item.end())
            if separador is None or not plural:
                break
            anterior = base if separador.group(1).lower() == "a" else None
            posicion = separador.end()
    return list(dict.fromkeys(clave for _, clave in sorted(encontrados, key=lambda par: par[0])))


def extraer_articulos(pagina: str, bases: Iterable[str]) -> TextoLegal:
    """Text of the blocks named by ``bases`` ("5", "9 quater", "110-2", "da 61", "5.dos.1").

    Works on the text, not on the markup. A block starts at a line "Artículo N." (or the heading of a provision)
    and ends at the next article or structural heading. When a page prints the same number twice (the approving
    decree and the consolidated text it approves) every match is returned, each under its own heading.

    A base with an apartado ("5.dos.1": apartado Dos, number 1, of article 5) asks for that part only. It is
    honoured when the article is very long (:data:`UMBRAL_ACOTAR`) and the apartado can be isolated without
    doubt; otherwise the whole article is returned. ``encontrados`` says what was actually returned ("5.dos",
    or "5"), so asking again with those names gives the same text.
    """
    buscados = list(dict.fromkeys(" ".join(b.lower().split()) for b in bases))
    bloques = _bloques(pagina)
    partes: list[tuple[str, str]] = []
    faltan: list[str] = []
    for pedido in buscados:
        base, apartados = partir_base(pedido)
        suyos = [lineas for numero, lineas in bloques if numero == base]
        if not suyos:
            faltan.append(pedido)
            continue
        nombre = base
        textos: list[str] = []
        for lineas in suyos:
            acotado = acotar(lineas, apartados) if apartados else None
            if acotado is not None:
                nombre = ".".join([base, *acotado[0]])
                textos.append("\n".join(acotado[1]))
            else:
                textos.append("\n".join(lineas))
        partes += [(nombre, texto) for texto in textos]
    # A whole article makes its apartados redundant; the same apartado asked for twice is shown once.
    enteros = {nombre for nombre, _ in partes if "." not in nombre}
    vistos: set[tuple[str, str]] = set()
    finales: list[tuple[str, str]] = []
    for nombre, texto in partes:
        if (nombre.split(".")[0] in enteros and "." in nombre) or (nombre, texto) in vistos:
            continue
        vistos.add((nombre, texto))
        finales.append((nombre, texto))
    texto = "\n\n".join(t for _, t in finales)
    return TextoLegal(
        texto=texto,
        encontrados=tuple(dict.fromkeys(nombre for nombre, _ in finales)),
        faltan=tuple(faltan),
        sha256=hashlib.sha256(_comprimido(texto).encode("utf-8")).hexdigest(),
    )


def indice_de(pagina: str) -> list[str]:
    """Headings of the law's articles ("Artículo 13. Normas comunes…"), for the reader to ask for more text."""
    vistos: dict[str, str] = {}
    for numero, lineas in _bloques(pagina):
        if len(lineas[0]) > len(vistos.get(numero, "")):
            vistos[numero] = lineas[0][:160]
    return list(vistos.values())


def articulos_que_contienen(pagina: str, citas: Iterable[str], *, junto_a: Iterable[str] = ()) -> list[str]:
    """Blocks of the page in which any of ``citas`` is found literally (where a previous encoding read from).

    ``junto_a`` are the names already going to be read ("5.dos.1"): in a very long article they tell how its
    apartados are numbered, so that a quote found in ANOTHER apartado of the same article (its common rules)
    adds that apartado ("5.uno") and not the whole article.
    """
    propias = [c for c in (_comprimido(cita) for cita in citas) if len(c) >= MIN_CITA]
    pistas: dict[str, list[str]] = {}
    for nombre in junto_a:
        base, apartados = partir_base(" ".join(nombre.lower().split()))
        if apartados:
            pistas.setdefault(base, apartados)
    out: list[str] = []
    for numero, lineas in _bloques(pagina):
        texto = "\n".join(lineas)
        if not any(c in _comprimido(texto) for c in propias):
            continue
        if numero in pistas and len(texto) > UMBRAL_ACOTAR:
            segmentos = _segmentos(lineas[1:], _familia(pistas[numero][0]))
            for etiqueta, inicio, fin in segmentos or []:
                trozo = _comprimido("\n".join(lineas[1:][inicio:fin]))
                nombre = f"{numero}.{etiqueta}"
                if nombre not in out and any(c in trozo for c in propias):
                    out.append(nombre)
            continue
        if numero not in out:
            out.append(numero)
    return out


def extraer_de_modificadora(pagina: str, bases: Iterable[str], *, ley: str = "") -> TextoLegal:
    """What an AMENDING norm says about the articles ``bases`` of the law it amends, from the norm's own text.

    For the days between the publication of an amending norm and the BOE consolidating it into the law. The
    blocks of the norm (its own articles and provisions) that name any of those articles are returned, preferring
    the ones that also name the amended law by its number (``ley``: "35/2006"), plus the provisions on entry into
    force and effects. ``encontrados`` are the bases of the AMENDED law that some block names.
    """
    buscados = list(dict.fromkeys(partir_base(" ".join(b.lower().split()))[0] for b in bases))
    bloques = [(numero, "\n".join(lineas)) for numero, lineas in _bloques(pagina) if len(lineas) > 1]

    def nombra(texto: str, base: str) -> bool:
        plano = _sin_acentos(texto).lower()
        if base.startswith(("da ", "dt ", "dd ", "df ")):
            clase = {v: k for k, v in _CLASES_DISPOSICION.items()}[base[:2]]
            ordinal = base[3:]
            for match in re.finditer(rf"disposicion\s+{clase}\s+([a-z ]+?|\d+)\s*[.,:ªa]", plano):
                valor = match.group(1).strip()
                numero = int(valor) if valor.isdigit() else _ORDINAL_A_NUMERO.get(_clave_ordinal(valor))
                if str(numero) == ordinal or valor == ordinal:
                    return True
            return False
        return any(" ".join(m.group(1).split()) == base for m in _ETIQUETA_RE.finditer(plano))

    encontrados: list[str] = []
    elegidos: list[int] = []
    for base in buscados:
        suyos = [i for i, (_, texto) in enumerate(bloques) if nombra(texto, base)]
        con_ley = [i for i in suyos if ley and ley in bloques[i][1]]
        if suyos:
            encontrados.append(base)
            elegidos += con_ley or suyos
    if elegidos:
        elegidos += [
            i
            for i, (numero, texto) in enumerate(bloques)
            if numero.startswith(("df ", "dt ")) and _CON_EFECTOS_O_VIGOR.search(texto)
        ]
    texto = "\n\n".join(bloques[i][1] for i in sorted(set(elegidos)))
    return TextoLegal(
        texto=texto,
        encontrados=tuple(encontrados),
        faltan=tuple(b for b in buscados if b not in encontrados),
        sha256=hashlib.sha256(_comprimido(texto).encode("utf-8")).hexdigest(),
    )


_CON_EFECTOS_O_VIGOR = re.compile(r"entrada\s+en\s+vigor|entrar[aá]\s+en\s+vigor|con\s+efectos|surtir[aá]", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Quotes
# --------------------------------------------------------------------------- #


def citas_no_literales(citas: Iterable[str], comprimido: str) -> list[str]:
    """Problems of ``citas`` against a text already compressed with ``verificacion._comprimido``.

    Stricter than the weekly re-verification: a quote must be ONE contiguous
    passage (no "[...]" stitching, which would let a figure travel between
    unrelated sentences) and long enough to mean something.
    """
    problemas: list[str] = []
    vistas = 0
    for cita in citas:
        vistas += 1
        propia = _comprimido(cita)
        if len(propia) < MIN_CITA:
            problemas.append(f"cita demasiado corta para probar nada: «{cita[:60]}»")
        elif propia not in comprimido:
            problemas.append(f"cita no literal (no está en el texto legal): «{cita[:90]}»")
    if not vistas:
        problemas.append("no hay ninguna cita literal")
    return problemas


def es_literal(cita: str, comprimido: str) -> bool:
    propia = _comprimido(cita)
    return len(propia) >= MIN_CITA and propia in comprimido


# --------------------------------------------------------------------------- #
# Figures inside quotes
# --------------------------------------------------------------------------- #

_UNIDADES = (
    "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciseis diecisiete "
    "dieciocho diecinueve veinte veintiuno veintidos veintitres veinticuatro veinticinco veintiseis veintisiete "
    "veintiocho veintinueve"
).split()
_DECENAS = {30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta", 70: "setenta", 80: "ochenta", 90: "noventa"}
_CENTENAS = {
    100: "ciento",
    200: "doscientos",
    300: "trescientos",
    400: "cuatrocientos",
    500: "quinientos",
    600: "seiscientos",
    700: "setecientos",
    800: "ochocientos",
    900: "novecientos",
}
_FACTORES_EN_PALABRAS = {
    Decimal("0.5"): ("la mitad", "50 por 100", "50 por cien", "50 %", "50%"),
    Decimal("2"): ("el doble", "duplic", "dupli"),
    Decimal("3"): ("el triple", "triplic"),
}


def en_palabras(n: int) -> list[str]:
    """Spanish spellings of 0…999 without accents ("ciento ochenta y tres"); empty outside that range."""
    if not 0 <= n <= 999:
        return []
    if n < 30:
        formas = [_UNIDADES[n]]
        if n == 1:
            formas += ["un", "una"]
        if n == 21:
            formas += ["veintiun", "veintiuna"]
        return formas
    if n < 100:
        decena, resto = n - n % 10, n % 10
        if not resto:
            return [_DECENAS[decena]]
        return [f"{_DECENAS[decena]} y {u}" for u in en_palabras(resto)]
    if n == 100:
        return ["cien"]
    centena, resto = n - n % 100, n % 100
    if not resto:
        return [_CENTENAS[centena]]
    return [f"{_CENTENAS[centena]} {u}" for u in en_palabras(resto)]


# --------------------------------------------------------------------------- #
# Apartados: cutting a very long article down to the part a group is read from
# --------------------------------------------------------------------------- #

_PALABRAS_APARTADO = {forma: n for n in range(1, 100) for forma in en_palabras(n)[:1]}
_LETRAS = "abcdefghijklmnñopqrstuvwxyz"
_BASE_CON_APARTADOS = re.compile(rf"^((?:d[atdf] \S+)|{_NUMERO})((?:\.(?:[a-zñ]+\)?|\d+\)?))*)$")
_NOTA_FINAL_RE = re.compile(
    r"^(?:Se\s+(?:modifica|anade|deroga|renumera|reenumera|suprime|declara|sustituye|corrige|deja|actualiza|introduce)|"
    r"Redaccion mostrada|Tengase en cuenta|Vease\b|Texto (?:anadido|original)|Su anterior numeracion)",
    re.IGNORECASE,
)


def partir_base(nombre: str) -> tuple[str, list[str]]:
    """("5", ["dos", "1"]) for "5.dos.1"; a plain base has no apartados."""
    match = _BASE_CON_APARTADOS.match(_sin_acentos_salvo_enye(nombre))
    if match is None:
        return nombre, []
    return " ".join(match.group(1).split()), [a.rstrip(")") for a in match.group(2).split(".") if a]


def ruta_de_etiqueta(etiqueta: str) -> str | None:
    """Where a label points inside its law: "art. 5.Dos.1 (jóvenes)" → "5.dos.1", "art. 9 quáter" → "9 quater"."""
    plano = _sin_acentos(etiqueta.replace("ñ", "\0")).replace("\0", "ñ")
    match = _ETIQUETA_RE.search(plano)
    if match is None:
        return None
    base = " ".join(match.group(1).lower().split())
    resto = re.match(r"((?:\.(?:[A-Za-zñ]+\)?|\d+\)?))*)", plano[match.end() :])
    apartados = [a.rstrip(")").lower() for a in (resto.group(1) if resto else "").split(".") if a]
    return ".".join([base, *apartados])


def _familia(apartado: str) -> str:
    if apartado.isdigit():
        return "numero"
    if _sin_acentos(apartado) in _PALABRAS_APARTADO:
        return "palabra"
    return "letra" if 1 <= len(apartado) <= 2 else "otra"


def _siguiente_letra(a: str, b: str) -> bool:
    if len(a) == 1 and len(b) == 1:
        i, j = _LETRAS.find(a), _LETRAS.find(b)
        # Not every law uses the "ñ".
        return i >= 0 and (j == i + 1 or (a == "n" and b == "o"))
    if a == "z" and b == "aa":
        return True
    return len(a) == 2 and len(b) == 2 and a[0] == b[0] and _siguiente_letra(a[1], b[1])


def _cabezas(lineas: Sequence[str], familia: str) -> list[tuple[int, str]]:
    """(line, label) of every line that opens an apartado of ``familia``, at any depth."""
    out: list[tuple[int, str]] = []
    for i, linea in enumerate(lineas):
        plano = _sin_acentos(linea.replace("ñ", "\0")).replace("\0", "ñ").lower()
        if familia == "numero":
            match = re.match(r"^(\d+)\.(?=\s|$)", plano)
        elif familia == "letra":
            match = re.match(r"^([a-zñ]{1,2})\)(?=\s|$)", plano)
        else:
            match = re.match(r"^([a-z]+(?: y [a-z]+)?)\.(?=\s|$)", plano)
            if match is not None and match.group(1) not in _PALABRAS_APARTADO:
                match = None
        if match is not None:
            out.append((i, match.group(1)))
    return out


def _segmentos(lineas: Sequence[str], familia: str) -> list[tuple[str, int, int]] | None:
    """(label, first line, end) of the TOP-LEVEL apartados of ``familia`` in ``lineas``; None when in doubt.

    Numbered and lettered lists nest ("7." holds its own "1.", "2."…), so the labels are followed with a stack. A
    label that could continue the top level and a nested one at once, or no level at all, means the structure
    cannot be told from the text: the caller then reads the whole article, which is always right.
    """
    if familia == "otra":
        return None
    cabezas = _cabezas(lineas, familia)
    arriba: list[tuple[int, str]] = []
    if familia == "palabra":
        if len({etiqueta for _, etiqueta in cabezas}) != len(cabezas):
            return None
        arriba = cabezas
    else:
        if familia == "numero":

            def sigue(a: str, b: str) -> bool:
                return int(b) == int(a) + 1

            primero, segundo = "1", "2"
        else:
            sigue, primero, segundo = _siguiente_letra, "a", "b"
        pila: list[str] = []
        for i, etiqueta in cabezas:
            if etiqueta == primero:
                if not pila:
                    arriba.append((i, etiqueta))
                pila.append(etiqueta)
                continue
            niveles = [k for k in range(len(pila)) if sigue(pila[k], etiqueta)]
            if (len(niveles) > 1 and 0 in niveles) or not pila:
                return None
            if not niveles:
                if etiqueta != segundo:
                    return None
                # A list whose first item is the unnumbered opening sentence ("12. … / 2. Límite conjunto").
                pila.append(etiqueta)
                continue
            # Two nested levels that could both go on: which one does not change where the top level is cut.
            pila = [*pila[: niveles[-1]], etiqueta]
            if niveles[-1] == 0:
                arriba.append((i, etiqueta))
    if not arriba:
        return None
    finales = [i for i, _ in arriba[1:]] + [len(lineas)]
    return [(etiqueta, inicio, fin) for (inicio, etiqueta), fin in zip(arriba, finales, strict=True)]


def acotar(lineas: Sequence[str], apartados: Sequence[str]) -> tuple[list[str], list[str]] | None:
    """The lines of one block cut down to ``apartados``; None when the block is short or it cannot be done safely.

    Returns (the apartados actually used, the lines). Kept besides the apartado itself: the heading of the
    article, whatever precedes its first apartado, the BOE's closing notes (they date the wording) and a bracketed
    line listing the other apartados, so that the reader can ask for the one holding the common rules.
    """
    if len("\n".join(lineas)) <= UMBRAL_ACOTAR or not apartados:
        return None
    cuerpo = list(lineas[1:])
    corte = len(cuerpo)
    while corte and _NOTA_FINAL_RE.match(_sin_acentos(cuerpo[corte - 1])):
        corte -= 1
    cuerpo, notas = cuerpo[:corte], cuerpo[corte:]
    usados: list[str] = []
    antes: list[str] = []
    indice: list[str] = []
    for nivel, apartado in enumerate(apartados):
        if nivel and len("\n".join(cuerpo)) <= UMBRAL_APARTADO:
            break
        segmentos = _segmentos(cuerpo[1:] if nivel else cuerpo, _familia(apartado))
        desplazamiento = 1 if nivel else 0
        elegido = next((s for s in segmentos or [] if s[0] == _sin_acentos_salvo_enye(apartado)), None)
        if elegido is None:
            break
        _, inicio, fin = elegido
        assert segmentos is not None
        antes += cuerpo[: desplazamiento + segmentos[0][1]]
        prefijo = ".".join(usados)
        indice += [
            f"{prefijo + '.' if prefijo else ''}{etiqueta}: {cuerpo[desplazamiento + i][:70]}"
            for etiqueta, i, _ in segmentos
            if etiqueta != elegido[0]
        ]
        cuerpo = cuerpo[desplazamiento + inicio : desplazamiento + fin]
        usados.append(elegido[0])
    if not usados:
        return None
    aviso = (
        f"[Se muestra solo el apartado {'.'.join(usados)} de este artículo. Sus otros apartados no se muestran; "
        "si necesitas alguno (normas comunes, límites, definiciones), pídelo como «<artículo>.<apartado>»: "
        + " | ".join(indice)
        + "]"
    )
    return usados, [lineas[0], *antes, aviso, *cuerpo, *notas]


def _sin_acentos_salvo_enye(texto: str) -> str:
    return _sin_acentos(texto.replace("ñ", "\0")).replace("\0", "ñ").lower()


def normalizar_evidencia(citas: Iterable[str]) -> str:
    """The quotes as one text, with "35 000" read as "35.000"."""
    texto = unicodedata.normalize("NFKC", " ".join(citas))
    return re.sub(r"(?<=\d)[  ](?=\d{3}(?!\d))", ".", texto)


def formas(n: Decimal) -> set[str]:
    """Ways a Spanish law prints ``n``: those ``verificacion`` knows plus "1.200,00" for whole amounts."""
    out = set(_formas(n))
    if n == n.to_integral_value():
        out |= {f + ",00" for f in out if "," not in f}
    elif n == n.quantize(Decimal("0.1")):
        out |= {f + "0" for f in out if "," in f}
    return out


def _patron(forma: str, *, cola: bool = True) -> str:
    return r"(?<![\d.,])" + re.escape(forma) + (r"(?![\d]|[.,]\d)" if cola else "")


def hay_numero(ev: str, n: Decimal) -> bool:
    return any(re.search(_patron(f), ev) for f in formas(n))


_PALABRAS_DE_NUMERO = "|".join(
    sorted({*_UNIDADES[1:], "un", "una", *_DECENAS.values(), *_CENTENAS.values(), "cien", "mil"}, key=len, reverse=True)
)
_SIGUE_NUMERO = re.compile(rf"\s+(?:y\s+)?(?:{_PALABRAS_DE_NUMERO})(?![a-z])")
_PRECEDE_NUMERO = re.compile(rf"(?<![a-z])(?:{_PALABRAS_DE_NUMERO})\s+(?:y\s+)?$")


def _hay_palabra(ev: str, n: int) -> bool:
    """``n`` written in words, as a whole number: "sesenta" is not found inside "sesenta y cinco"."""
    plano = _sin_acentos(ev).lower()
    for palabra in en_palabras(n):
        for match in re.finditer(r"(?<![a-z])" + re.escape(palabra) + r"(?![a-z])", plano):
            if not _SIGUE_NUMERO.match(plano, match.end()) and not _PRECEDE_NUMERO.search(plano[: match.start()]):
                return True
    return False


def hay_porcentaje(ev: str, fraccion: Decimal) -> bool:
    """Is ``fraccion`` printed as a percentage ("15 %", "15 por 100", "quince por ciento")?"""
    cien = fraccion * 100
    signo = r"\s*(?:%|por\s+100\b|por\s+cien)"
    if any(re.search(_patron(f, cola=False) + signo, ev) for f in formas(cien)):
        return True
    if cien == cien.to_integral_value():
        plano = _sin_acentos(ev).lower()
        return any(re.search(re.escape(p) + r"\s+por\s+cien", plano) for p in en_palabras(int(cien)))
    return False


def hay_entero(ev: str, n: int, *, margen: int = 1) -> bool:
    """``n`` in figures or words; with ``margen`` 1 also n ± 1 ("menores de 36" is encoded as 35)."""
    candidatos = [n] + [m for k in range(1, margen + 1) for m in (n + k, n - k)]
    return any(hay_numero(ev, Decimal(m)) or _hay_palabra(ev, m) for m in candidatos if m >= 0)


_MAYORIA_DE_EDAD = re.compile(r"\b(?:menor(?:es)?|mayor(?:es)?|mayoria)\s+de\s+edad\b", re.IGNORECASE)


@dataclass(frozen=True)
class Reglas:
    """How each numeric field of a model must appear in the quotes."""

    # Decimal fields in euros: the figure itself, or one cent away ("inferior a 35.000" → 34999.99).
    euros: frozenset[str]
    # Decimal fractions printed as a percentage next to "%" / "por 100" / "por ciento".
    fracciones: frozenset[str] = frozenset()
    # Decimal fractions printed in a table column, with no sign next to them.
    porcentajes_de_tabla: frozenset[str] = frozenset()
    # Multipliers: 1.2 is "+20 %", 0.5 "la mitad".
    factores: frozenset[str] = frozenset()
    # Integer degrees printed as a percentage ("33 por 100"), n or n ± 1.
    grados: frozenset[str] = frozenset()
    # Fields that are not legal figures (provenance, sources).
    ignorar: frozenset[str] = frozenset()
    # Plain numbers the law prints as they are stored ("multiplicar por 1,75").
    literales: frozenset[str] = frozenset()
    # Percentages stored either as points (5 = "5 por 100") or, below 1, as fractions (0.40 = "40 por 100").
    porcentajes_mixtos: frozenset[str] = frozenset()
    # Date fields that must be printed in a quote ("1 de diciembre de 2026"); other dates are not looked at.
    fechas: frozenset[str] = frozenset()


def cifras_sin_respaldo(valor: BaseModel, ev: str, reglas: Reglas) -> Control:
    """Every figure of ``valor``, wherever it is nested, must be printed in the quotes ``ev``.

    The walk is driven by the TYPES of the model, not by a list of known keys:
    a numeric field nobody wrote a rule for is a failure, so a schema that grows
    cannot silently escape the check.
    """
    control = Control()

    def decimal(donde: str, nombre: str, x: Decimal) -> None:
        if x == 0:
            return
        if nombre in reglas.euros:
            if not any(hay_numero(ev, x + d) for d in (Decimal(0), CENT, -CENT)):
                control.fallos.append(f"{donde} = {x}: la cifra no está en ninguna cita")
        elif nombre in reglas.fracciones:
            if hay_porcentaje(ev, x):
                return
            if x == 1 or x in _FACTORES_EN_PALABRAS:
                if not _factor_en_palabras(ev, x):
                    control.revisar.append(f"{donde} = {x}: no figura como porcentaje en las citas")
            else:
                control.fallos.append(f"{donde} = {x}: el porcentaje no está en ninguna cita")
        elif nombre in reglas.porcentajes_de_tabla:
            if not hay_numero(ev, x * 100):
                control.fallos.append(f"{donde} = {x}: el tipo no está en ninguna cita")
        elif nombre in reglas.literales:
            if not hay_numero(ev, x):
                control.fallos.append(f"{donde} = {x}: el número no está en ninguna cita")
        elif nombre in reglas.porcentajes_mixtos:
            if not hay_porcentaje(ev, x if x < 1 else x / 100):
                control.fallos.append(f"{donde} = {x}: el porcentaje no está en ninguna cita")
        elif nombre in reglas.factores:
            if x == 1 or hay_porcentaje(ev, abs(x - 1)) or hay_porcentaje(ev, x) or _factor_en_palabras(ev, x):
                return
            if any(hay_numero(ev, (importe * x).normalize()) for importe in importes):
                return  # the law prints the resulting amount ("500 euros… 250 euros si convive menos de 183 días")
            control.fallos.append(f"{donde} = {x}: el factor no se deduce de ninguna cita")
        else:
            control.fallos.append(f"{donde}: campo numérico sin regla de comprobación")

    def entero(donde: str, nombre: str, n: int) -> None:
        if n <= 1:
            return
        if nombre in reglas.grados:
            if not any(hay_porcentaje(ev, Decimal(n + k) / 100) for k in (0, 1, -1)):
                control.fallos.append(f"{donde} = {n}: el grado no está en ninguna cita")
        elif n in (17, 18) and _MAYORIA_DE_EDAD.search(_sin_acentos(ev)):
            return  # "hijos menores de edad" is the law's way of printing 18
        elif not hay_entero(ev, n):
            control.fallos.append(f"{donde} = {n}: el número no está en ninguna cita (ni en cifras ni en letras)")

    def recorrer(donde: str, nombre: str, x: Any) -> None:
        if x is None or isinstance(x, bool | str):
            return
        if isinstance(x, date):
            if nombre in reglas.fechas and not fecha_en_cita(ev, x):
                control.fallos.append(f"{donde} = {x.isoformat()}: la fecha no está en ninguna cita")
            return
        if isinstance(x, Decimal):
            decimal(donde, nombre, x)
        elif isinstance(x, int):
            entero(donde, nombre, x)
        elif isinstance(x, float):
            control.fallos.append(f"{donde}: número en coma flotante")
        elif isinstance(x, BaseModel):
            for campo in type(x).model_fields:
                if campo not in reglas.ignorar:
                    recorrer(f"{donde}.{campo}" if donde else campo, campo, getattr(x, campo))
        elif isinstance(x, dict):
            for clave, item in x.items():
                recorrer(f"{donde}.{clave}", nombre, item)
        elif isinstance(x, list | tuple):
            for i, item in enumerate(x):
                recorrer(f"{donde}[{i}]", nombre, item)

    importes: list[Decimal] = []

    def reunir(x: Any, nombre: str) -> None:
        if isinstance(x, Decimal) and nombre in reglas.euros and x:
            importes.append(x)
        elif isinstance(x, BaseModel):
            for campo in type(x).model_fields:
                reunir(getattr(x, campo), campo)
        elif isinstance(x, list | tuple):
            for item in x:
                reunir(item, nombre)

    reunir(valor, "")
    recorrer("", "", valor)
    return control


def _factor_en_palabras(ev: str, x: Decimal) -> bool:
    plano = _sin_acentos(ev).lower()
    if x == 1:
        return bool(re.search(r"100\s*(?:%|por\s+100\b|por\s+cien)|totalidad|integr[ao]", plano))
    return any(p in plano for p in _FACTORES_EN_PALABRAS.get(x, ()))


def campos_numericos(modelo: type[BaseModel]) -> set[str]:
    """Names of every field that can hold a number in ``modelo`` and the models nested in it."""
    import types
    import typing

    nombres: set[str] = set()
    vistos: set[type] = set()

    def tipos(anotacion: Any) -> Iterable[Any]:
        origen = typing.get_origin(anotacion)
        if origen is None:
            yield anotacion
            return
        if origen is typing.Literal:
            return
        if origen in (typing.Union, types.UnionType, list, tuple, dict, set, frozenset):
            for arg in typing.get_args(anotacion):
                yield from tipos(arg)

    def recorrer(m: type[BaseModel]) -> None:
        if m in vistos:
            return
        vistos.add(m)
        for nombre, info in m.model_fields.items():
            for t in tipos(info.annotation):
                if isinstance(t, type) and issubclass(t, BaseModel):
                    recorrer(t)
                elif t in (Decimal, int, float):
                    nombres.add(nombre)

    recorrer(modelo)
    return nombres


_NUMERO_EN_TEXTO = re.compile(r"(?<![\w.,])\d[\d.]*(?:,\d+)?")
_REFERENCIA = re.compile(r"(?:art(?:ículo|iculo|\.)?|apartado|letra|ley|decreto|n\.º|núm\.|número)\s*$", re.IGNORECASE)


def numeros_de_texto_sin_respaldo(texto: str, ev: str) -> list[str]:
    """Figures written in a free-text field (a summary the user reads) that no quote prints."""
    faltan: list[str] = []
    for match in _NUMERO_EN_TEXTO.finditer(unicodedata.normalize("NFKC", texto)):
        token = match.group(0).rstrip(".")
        if _REFERENCIA.search(texto[: match.start()]) or token in faltan:
            continue
        try:
            valor = Decimal(token.replace(".", "").replace(",", "."))
        except InvalidOperation:
            continue
        if re.search(_patron(token), ev) or hay_numero(ev, valor):
            continue
        if valor == valor.to_integral_value() and _hay_palabra(ev, int(valor)):
            continue
        faltan.append(token)
    return faltan


_DERIVADA = re.compile(
    r"no figura (?:literalmente )?en la ley|resulta de (?:aplicar|sumar|multiplicar|restar)|cifra (?:derivada|calculada)|"
    r"aproximad|redondead|se ha calculado|equivale a \d",
    re.IGNORECASE,
)


def notas_de_cifra_derivada(textos: Iterable[str]) -> list[str]:
    """Notes that confess a derived or approximated figure: the rule is to leave it out instead."""
    return [t[:90] for t in textos if _DERIVADA.search(t)]


# --------------------------------------------------------------------------- #
# Effects date
# --------------------------------------------------------------------------- #

_MESES = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()
_CON_EFECTOS = re.compile(
    r"\bcon\s+efectos?\b|\bsurtir[aá]n?\s+efectos?\b|"
    r"\bper[ií]odos\s+impositivos\s+(?:que\s+se\s+inicien|iniciados|que\s+comiencen|que\s+concluyan|concluidos)\b",
    re.IGNORECASE,
)


def fecha_iso(texto: str) -> date | None:
    try:
        return date.fromisoformat(texto.strip())
    except ValueError:
        return None


def fecha_en_cita(cita: str, dia: date) -> bool:
    """Is ``dia`` printed in ``cita``? ("01/01/2026", "1-1-2026", "1 de enero de 2026"; for a 1 January, the year alone
    as in "períodos impositivos iniciados a partir de 2026")."""
    plano = _sin_acentos(unicodedata.normalize("NFKC", cita)).lower()
    numericas = rf"(?<!\d)0?{dia.day}[/.-]0?{dia.month}[/.-]{dia.year}(?!\d)"
    letras = (
        rf"(?<!\d)(?:{dia.day}|uno|primero)\s+(?:de\s+)?{_MESES[dia.month - 1]}\s+(?:de\s+|del\s+)?{dia.year}(?!\d)"
    )
    if re.search(numericas, plano) or re.search(letras, plano):
        return True
    if dia.month == 1 and dia.day == 1:
        return bool(re.search(rf"(?<![\d/.-]){dia.year}(?![\d/.-])", plano))
    return False


def controlar_efectos(
    desde: str, hasta: str, cita: str, *, comprimido: str, texto: str, candidatos: Iterable[int]
) -> tuple[Control, date | None, date | None]:
    """The date from which a wording produces effects must be printed in a literal quote.

    The tax years a proposal touches are derived from this date and from nothing
    else: neither the day the BOE published the wording nor the day it entered
    into force says which tax years it applies to.
    """
    control = Control()
    inicio, fin = fecha_iso(desde), fecha_iso(hasta) if hasta.strip() else None
    if inicio is None:
        control.fallos.append("efectos_desde no es una fecha AAAA-MM-DD")
        return control, None, None
    if hasta.strip() and fin is None:
        control.fallos.append("efectos_hasta no es una fecha AAAA-MM-DD")
    if not es_literal(cita, comprimido):
        control.fallos.append("la cita de efectos no es literal (no está en el texto legal ni en sus anotaciones)")
        return control, inicio, fin
    if not fecha_en_cita(cita, inicio):
        control.fallos.append(f"la cita de efectos no contiene la fecha {inicio.isoformat()}")
    if fin is not None and not fecha_en_cita(cita, fin):
        control.fallos.append(f"la cita de efectos no contiene la fecha final {fin.isoformat()}")
    if _CON_EFECTOS.search(texto) and not _CON_EFECTOS.search(cita):
        control.revisar.append(
            "el texto contiene cláusulas de efectos («con efectos…», «períodos impositivos…») y la cita de efectos "
            "se apoya en otra cosa: compruebe a qué ejercicios se aplica la redacción"
        )
    for ejercicio in candidatos:
        if date(ejercicio, 1, 1) < inicio <= date(ejercicio, 12, 31):
            control.revisar.append(
                f"la redacción produce efectos desde {inicio.isoformat()}, a mitad del ejercicio {ejercicio}: se "
                "aplica al ejercicio entero solo si el impuesto se devenga al final del período"
            )
    return control, inicio, fin
