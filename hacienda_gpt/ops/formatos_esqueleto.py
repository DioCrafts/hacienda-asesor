"""Structural skeletons of HTML pages whose text must not be kept or sent anywhere.

The CENDOJ's reuse conditions forbid mass downloading and building a database
of rulings without the CGPJ's authorisation. The format watch therefore never
keeps, and never sends to a model, the text of a ruling: what it keeps of a
CENDOJ page is a SKELETON, built here:

* the markup: tags in order, attribute names, and the values of ``class`` /
  ``id`` / ``name`` when they are plain identifiers (layout vocabulary, not
  text); any other attribute value becomes ``_``; scripts, styles, comments
  and inline drawings are dropped;
* the text: each word is kept only if it is in a closed list of labels and
  court vocabulary the parser relies on («ROJ», «ECLI», «Ponente», «Fecha»,
  «Sala», «Sección», «Nº de Recurso»…); every other word becomes ``_``;
* figures keep their shape: every digit becomes ``1`` (so a date stays a valid
  date and a ROJ still reads "STS 1111/1111"), except a single digit marking
  an ordinal ("Sala 3ª"), which is layout;
* the result is capped.

A name, a party, an address or a sentence of the reasoning cannot survive the
filter unless it is itself one of those generic words. Same rule as the user
documents' skeletons (:mod:`hacienda_gpt.ops.formatos_documentos`): an
allow-list, never a block-list.
"""

from __future__ import annotations

from collections.abc import Iterable
import html as html_lib
import re
import unicodedata

HUECO = "_"
MAX_ESQUELETO = 20_000

_RUIDO = re.compile(r"(?is)<(script|style|svg|noscript)\b.*?</\1\s*>|<!--.*?-->|<![^>]*>|<\?.*?\?>")
_ETIQUETA = re.compile(r"<(/?)([A-Za-z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATRIBUTO = re.compile(r"""([^\s=/>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+)))?""")
_IDENTIFICADOR = re.compile(r"[A-Za-z_][\w-]*(?: [A-Za-z_][\w-]*)*")
_ATRIBUTOS_DE_MAQUETA = frozenset({"class", "id", "name"})
# A run of letters (º and ª count as letters, as in "Nº" and "3ª").
_PALABRA = re.compile(r"[^\W\d_]+")
_CIFRA = re.compile(r"\d(?![ºª])")


def plano(palabra: str) -> str:
    """Lower case without accents (``ñ`` kept), for comparing against the allow-list."""
    descompuesto = unicodedata.normalize("NFD", palabra.lower().replace("ñ", "\0"))
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn").replace("\0", "ñ")


def lista_permitida(palabras: Iterable[str]) -> frozenset[str]:
    return frozenset(plano(p) for p in palabras)


def _se_queda(palabra: str, permitidas: frozenset[str]) -> bool:
    # Ordinal indicators ("3ª", "2º") are layout, whatever the list says.
    return plano(palabra) in permitidas or set(palabra) <= {"º", "ª"}


def _texto(segmento: str, permitidas: frozenset[str]) -> str:
    texto = _CIFRA.sub("1", html_lib.unescape(segmento))
    texto = _PALABRA.sub(lambda m: m.group(0) if _se_queda(m.group(0), permitidas) else HUECO, texto)
    texto = re.sub(r"[ \t\r\f\v]+", " ", texto)
    texto = re.sub(r"\s*\n\s*", "\n", texto)
    return html_lib.escape(texto, quote=False)


def _etiqueta(cierre: str, nombre: str, atributos: str) -> str:
    partes = [f"<{cierre}{nombre.lower()}"]
    for m in _ATRIBUTO.finditer(atributos):
        clave = m.group(1).lower()
        if m.group(2) is None and m.group(3) is None and m.group(4) is None:
            partes.append(clave)
            continue
        valor = next(v for v in m.group(2, 3, 4) if v is not None)
        if clave in _ATRIBUTOS_DE_MAQUETA and len(valor) <= 60 and _IDENTIFICADOR.fullmatch(valor.strip()):
            valor = _CIFRA.sub("1", valor.strip())
        else:
            valor = HUECO
        partes.append(f'{clave}="{valor}"')
    return " ".join(partes) + ">"


def esqueleto_html(html: str, permitidas: frozenset[str], max_chars: int = MAX_ESQUELETO) -> str:
    """The structural skeleton of ``html``: markup kept, text reduced to allowed words and digit shapes."""
    limpio = _RUIDO.sub("", html)
    partes: list[str] = []
    posicion = 0
    for m in _ETIQUETA.finditer(limpio):
        partes.append(_texto(limpio[posicion : m.start()], permitidas))
        partes.append(_etiqueta(m.group(1), m.group(2), m.group(3)))
        posicion = m.end()
    partes.append(_texto(limpio[posicion:], permitidas))
    esqueleto = "".join(partes)
    if len(esqueleto) <= max_chars:
        return esqueleto
    corte = esqueleto.rfind("<", 0, max_chars)
    return esqueleto[: corte if corte > max_chars // 2 else max_chars]
