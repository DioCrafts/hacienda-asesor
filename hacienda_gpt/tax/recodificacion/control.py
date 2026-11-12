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

from collections.abc import Iterable
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
_UI_RE = re.compile(r"^(?:Seleccionar redacci[oó]n|Subir$|\[Bloque\b)", re.IGNORECASE)
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


def _bloques(pagina: str) -> list[tuple[str, list[str]]]:
    """(article number, lines) for every "Artículo N." block of the page, in order."""
    bloques: list[tuple[str, list[str]]] = []
    actual: list[str] | None = None
    for linea in lineas_de(pagina):
        cabecera = _CABECERA_RE.match(_sin_acentos(linea))
        if cabecera:
            actual = [linea]
            bloques.append((" ".join(cabecera.group(1).lower().split()), actual))
        elif _BLOQUE_RE.match(linea) or _FRONTERA_RE.match(linea):
            actual = None
        elif actual is not None and not _UI_RE.match(linea):
            actual.append(linea)
    return bloques


def extraer_articulos(pagina: str, bases: Iterable[str]) -> TextoLegal:
    """Text of the articles named by ``bases`` ("5", "9 quater", "110-2").

    Works on the text, not on the markup. A block starts at a line "Artículo N."
    and ends at the next article or structural heading. When a page prints the
    same number twice (the approving decree and the consolidated text it
    approves) every match is returned, each under its own heading.
    """
    buscados = list(dict.fromkeys(bases))
    bloques = _bloques(pagina)
    partes: list[str] = []
    encontrados: list[str] = []
    for base in buscados:
        suyos = ["\n".join(lineas) for numero, lineas in bloques if numero == base]
        if suyos:
            encontrados.append(base)
            partes += suyos
    texto = "\n\n".join(partes)
    return TextoLegal(
        texto=texto,
        encontrados=tuple(encontrados),
        faltan=tuple(b for b in buscados if b not in encontrados),
        sha256=hashlib.sha256(_comprimido(texto).encode("utf-8")).hexdigest(),
    )


def indice_de(pagina: str) -> list[str]:
    """Headings of the law's articles ("Artículo 13. Normas comunes…"), for the reader to ask for more text."""
    vistos: dict[str, str] = {}
    for numero, lineas in _bloques(pagina):
        if len(lineas[0]) > len(vistos.get(numero, "")):
            vistos[numero] = lineas[0][:160]
    return list(vistos.values())


def articulos_que_contienen(pagina: str, citas: Iterable[str]) -> list[str]:
    """Articles of the page in which any of ``citas`` is found literally (where a previous encoding read from)."""
    propias = [c for c in (_comprimido(cita) for cita in citas) if len(c) >= MIN_CITA]
    out: list[str] = []
    for numero, lineas in _bloques(pagina):
        if numero not in out and any(c in _comprimido("\n".join(lineas)) for c in propias):
            out.append(numero)
    return out


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
