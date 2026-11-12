"""Law change → who it affects.

``boe_watch`` notices that a consolidated law changed. This module finds which
articles changed, asks the LLM once per changed law (never once per user) for a
plain-language summary tagged with a fixed vocabulary of taxpayer groups, and
then matches those tags against each case's facts deterministically.

The safeguard against an invented effect is mechanical, as in
:mod:`hacienda_gpt.tax.documentos`: the summary must come with a literal quote
of the article text, and a summary whose quote is not in the text is discarded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from html.parser import HTMLParser
import logging
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.proactive.store import Notification, NotificationStore

logger = logging.getLogger(__name__)

MAX_TEXTO = 2500
MAX_NOTA = 1000
# A budget law can touch dozens of articles; the prompt takes the most recent ones.
MAX_ARTICULOS_PROMPT = 12
MIN_CITA = 15

Relevancia = Literal["alta", "media", "baja"]


class ArticuloModificado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # E.g. "Artículo 63. Escala general del Impuesto."
    titulo: str
    publicado: date
    en_vigor: date | None = None
    # The "Se modifica…" / "Se añade…" sentences of the block.
    nota: str = ""
    # Current wording, trimmed.
    texto: str = ""


class CambioNormativo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boe_id: str
    titulo_norma: str
    # Identifies this revision of the law (boe_watch's new ``fecha_actualizacion``).
    actualizado: str
    resumen: str
    en_vigor_desde: str
    afecta_a: list[str]
    relevancia: Relevancia
    cita_literal: str
    articulos: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parsing the consolidated HTML
# --------------------------------------------------------------------------- #

_BLOCK_TAGS = frozenset(
    "p div h1 h2 h3 h4 h5 h6 li ul ol blockquote br tr table option select form section article "
    "header footer pre dd dt hr".split()
)
_SKIP_TAGS = frozenset({"script", "style", "head", "noscript"})


class _TextLines(HTMLParser):
    """Flatten HTML into text lines, one per block-level element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            # A line break in the source is not a line break in the text: only block tags are.
            self.parts.append(re.sub(r"\s+", " ", data))


def _lineas(html: str) -> list[str]:
    parser = _TextLines()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return [line for line in lines if line]


# A block we report: an article or an additional/transitory/derogatory/final provision.
_HEADING_RE = re.compile(
    r"^(?:Art[ií]culo\s+(?:\d+(?:\s+(?:bis|ter|qu[aá]ter|quinquies|sexies|septies|octies|nonies|decies))?"
    r"|[úu]nico|primero|segundo|tercero|cuarto|quinto)\s*\."
    r"|Disposici[oó]n\s+(?:adicional|transitoria|derogatoria|final)\b)",
    re.IGNORECASE,
)
# Structural headings that end the previous block without starting a reported one.
_BOUNDARY_RE = re.compile(
    r"^(?:\[Bloque\b|T[IÍ]TULO\b|CAP[IÍ]TULO\b|Secci[oó]n\s|Subsecci[oó]n\s|LIBRO\b|ANEXO\b|PRE[AÁ]MBULO\b"
    r"|EXPOSICI[OÓ]N DE MOTIVOS\b)"
)
_FECHA = r"(\d{1,2})/(\d{1,2})/(\d{4})"
# "Última actualización, publicada el 31/12/2020, en vigor a partir del 01/01/2021."
# "Texto añadido, publicado el …", "Modificación publicada el …", "Texto original, publicado el …"
_VERSION_RE = re.compile(
    rf"publicad[oa]\s+el\s+{_FECHA}(?:\s*,?\s*en\s+vigor\s+a\s+partir\s+del?\s+{_FECHA})?", re.IGNORECASE
)
_NOTA_RE = re.compile(
    r"^Se\s+(?:modifica|modifican|a[ñn]ade|a[ñn]aden|deroga|derogan|suprime|suprimen|renumera|sustituye|declara|"
    r"deja|dejan|corrige|corrigen|actualiza|actualizan|prorroga|introduce)\b",
    re.IGNORECASE,
)
# Labels of the version selector, not part of the wording.
_UI_RE = re.compile(r"^(?:Seleccionar redacci[oó]n|Redacci[oó]n\b|Subir\b|\[Bloque\b)", re.IGNORECASE)


def _date(day: str, month: str, year: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def articulos_modificados(html: str, *, desde: date) -> list[ArticuloModificado]:
    """Articles and provisions of a BOE consolidated law whose latest wording was published after ``desde``.

    Works from the text, not the markup: a block starts at a line beginning with
    "Artículo N." or "Disposición adicional/transitoria/derogatoria/final …" and
    its latest wording is the most recent "publicada el DD/MM/YYYY" line inside it.
    """
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in _lineas(html):
        if _HEADING_RE.match(line) and not _VERSION_RE.search(line):
            current = []
            blocks.append((line, current))
        elif _BOUNDARY_RE.match(line):
            current = None
        elif current is not None:
            current.append(line)

    out: list[ArticuloModificado] = []
    for titulo, lines in blocks:
        latest: tuple[date, date | None] | None = None
        notas: list[str] = []
        texto: list[str] = []
        for line in lines:
            version = _VERSION_RE.search(line)
            if version:
                publicado = _date(*version.group(1, 2, 3))
                if publicado and (latest is None or publicado > latest[0]):
                    latest = (publicado, _date(*version.group(4, 5, 6)) if version.group(4) else None)
            elif _NOTA_RE.match(line):
                notas.append(line)
            elif not _UI_RE.match(line):
                texto.append(line)
        if latest is None or latest[0] <= desde:
            continue
        out.append(
            ArticuloModificado(
                titulo=titulo,
                publicado=latest[0],
                en_vigor=latest[1],
                nota=" ".join(notas)[:MAX_NOTA],
                texto="\n".join(texto)[:MAX_TEXTO],
            )
        )
    return out


# --------------------------------------------------------------------------- #
# One LLM call per changed law
# --------------------------------------------------------------------------- #

# tag → (who it is, shown in the prompt; why a matching user is told they are affected)
VOCABULARIO: dict[str, tuple[str, str]] = {
    "todos": ("cualquier contribuyente del IRPF", "Afecta a todos los contribuyentes."),
    "asalariados": ("trabajadores por cuenta ajena", "Tienes rendimientos del trabajo por cuenta ajena."),
    "autonomos": ("autónomos y profesionales", "Ejerces una actividad económica como autónomo."),
    "pensionistas": ("perceptores de pensiones", "Cobras una pensión."),
    "arrendadores": ("quienes alquilan inmuebles a terceros", "Tienes ingresos por alquiler de inmuebles."),
    "inquilinos": ("quienes viven de alquiler", "Pagas alquiler por tu vivienda habitual."),
    "inversores": (
        "quienes tienen intereses, dividendos o ganancias patrimoniales",
        "Tienes rendimientos del ahorro o ganancias patrimoniales.",
    ),
    "familias_con_hijos": ("contribuyentes con descendientes a cargo", "Tienes hijos a tu cargo."),
    "propietarios_vivienda": (
        "propietarios de su vivienda habitual",
        "Estás pagando la compra de tu vivienda habitual.",
    ),
    "donantes": ("quienes hacen donativos", "Haces donativos."),
    "planes_de_pensiones": ("partícipes de planes de pensiones", "Aportas a un plan de pensiones."),
    "bienes_en_el_extranjero": (
        "quienes tienen bienes o cuentas en el extranjero",
        "Tienes bienes o derechos en el extranjero.",
    ),
    "no_residentes": ("no residentes fiscales en España", "Tu residencia fiscal está fuera de España."),
    "sociedades": ("sociedades y otras personas jurídicas", "Tributas como sociedad."),
}
TAGS: tuple[str, ...] = tuple(VOCABULARIO)

SYSTEM_PROMPT = (
    """\
Eres un asesor fiscal español. Recibes los artículos de una norma consolidada del BOE cuya redacción acaba de \
cambiar, con la nota que indica qué se ha modificado y el texto vigente. Tu resumen se enviará como aviso a \
contribuyentes sin formación jurídica, y se comprobará contra el texto que recibes.

Devuelve:
- `resumen`: de 2 a 4 frases, en español llano, que digan qué ha cambiado y para quién. Sin jerga y sin citar \
números de artículo salvo que ayude.
- `en_vigor_desde`: la fecha desde la que se aplica el cambio, tal como consta en el texto o en la nota \
("1 de enero de 2027"). Si no consta, cadena vacía.
- `afecta_a`: los grupos de contribuyentes a los que el cambio afecta directamente, SOLO de esta lista:
"""
    + "\n".join(f"  - {tag}: {quien}" for tag, (quien, _) in VOCABULARIO.items())
    + """
  Usa `todos` solo si el cambio alcanza a cualquier contribuyente (por ejemplo, la escala general o el mínimo \
personal). Si no afecta directamente a ninguno de estos grupos, devuelve una lista vacía.
- `relevancia`: `alta` si cambia cuánto se paga, una obligación o un plazo para los grupos afectados; `media` si el \
efecto es limitado o depende de circunstancias poco frecuentes; `baja` si el cambio es puramente técnico, de \
redacción, de remisiones o de renumeración.
- `cita_literal`: una o dos frases copiadas TAL CUAL del texto de los artículos, sin resumir ni corregir, que \
respalden el resumen.

Reglas:
- No deduzcas efectos que el texto no diga. No calcules importes ni des cifras que no aparezcan en el texto.
- Si no puedes saber qué ha cambiado respecto a la redacción anterior, descríbelo como «nueva redacción de…» y \
limítate a lo que el texto vigente establece.
- No des consejos ni recomendaciones: solo describe el cambio.
"""
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "resumen": {"type": "string"},
        "en_vigor_desde": {"type": "string"},
        "afecta_a": {"type": "array", "items": {"type": "string", "enum": list(TAGS)}},
        "relevancia": {"type": "string", "enum": ["alta", "media", "baja"]},
        "cita_literal": {"type": "string"},
    },
    "required": ["resumen", "en_vigor_desde", "afecta_a", "relevancia", "cita_literal"],
}


def _normalize(text: str) -> str:
    """Case-, accent- and whitespace-insensitive form for passage matching."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", "", stripped).lower()


def analizar_cambio(
    provider: Any,
    *,
    boe_id: str,
    titulo_norma: str,
    articulos: list[ArticuloModificado],
    actualizado: str | None = None,
) -> CambioNormativo | None:
    """Summarise a law change with one ``complete_json`` call.

    Returns None when the answer cannot be trusted or used: the quote is not
    literally in the supplied text, no known group is affected, or a field is
    malformed. Raises the provider's ``ProviderError``.
    """
    if not articulos:
        return None
    enviados = sorted(articulos, key=lambda a: a.publicado, reverse=True)[:MAX_ARTICULOS_PROMPT]
    partes = [f'<norma id="{boe_id}">{titulo_norma}</norma>']
    for a in enviados:
        vigor = f' en_vigor="{a.en_vigor.isoformat()}"' if a.en_vigor else ""
        partes.append(
            f'<articulo publicado="{a.publicado.isoformat()}"{vigor}>\n{a.titulo}\n'
            f"<nota>{a.nota}</nota>\n<texto>\n{a.texto}\n</texto>\n</articulo>"
        )
    raw = provider.complete_json(
        system=SYSTEM_PROMPT, user="\n\n".join(partes), schema=SCHEMA, schema_name="cambio_normativo"
    )

    resumen, cita, relevancia = raw.get("resumen"), raw.get("cita_literal"), raw.get("relevancia")
    vigor_desde, afecta = raw.get("en_vigor_desde"), raw.get("afecta_a")
    if not isinstance(resumen, str) or not resumen.strip() or relevancia not in ("alta", "media", "baja"):
        return None
    tags = [tag for tag in TAGS if isinstance(afecta, list) and tag in afecta]
    if not tags:
        return None
    haystack = _normalize("\n".join(f"{a.titulo}\n{a.nota}\n{a.texto}" for a in enviados))
    if not isinstance(cita, str) or len(_normalize(cita)) < MIN_CITA or _normalize(cita) not in haystack:
        # The quoted passage is not in the law: the summary cannot be trusted.
        logger.warning("Discarded the analysis of %s: its quote is not in the supplied text.", boe_id)
        return None
    return CambioNormativo(
        boe_id=boe_id,
        titulo_norma=titulo_norma,
        actualizado=actualizado or max(a.publicado for a in articulos).isoformat(),
        resumen=" ".join(resumen.split()),
        en_vigor_desde=vigor_desde.strip() if isinstance(vigor_desde, str) else "",
        afecta_a=tags,
        relevancia=relevancia,
        cita_literal=" ".join(cita.split()),
        articulos=[a.titulo for a in enviados],
    )


# --------------------------------------------------------------------------- #
# Deterministic matching against a profile
# --------------------------------------------------------------------------- #


def _hay(facts: dict[str, Any], *names: str) -> bool:
    """True when any of the numeric facts is present and non-zero."""
    for name in names:
        value = facts.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool) and value != 0:
            return True
    return False


def _categoria(facts: dict[str, Any]) -> str:
    value = facts.get("categoria_contribuyente")
    return value.strip().lower() if isinstance(value, str) else ""


# tag → does this profile belong to the group? (the reasons shown are in VOCABULARIO)
CRITERIOS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "todos": lambda f: True,
    "asalariados": lambda f: _categoria(f) == "asalariado"
    or (_hay(f, "rendimiento_trabajo_bruto") and _categoria(f) != "pensionista"),
    "autonomos": lambda f: f.get("alta_actividad_economica") is True
    or _categoria(f) == "autonomo"
    or f.get("regimen_cotizacion") == "RETA"
    or _hay(f, "rendimiento_actividad_neto"),
    "pensionistas": lambda f: _categoria(f) == "pensionista",
    "arrendadores": lambda f: _hay(f, "rendimiento_alquiler_neto"),
    "inquilinos": lambda f: _hay(f, "alquiler_vivienda_habitual"),
    "inversores": lambda f: _hay(f, "rendimiento_capital_mobiliario", "ganancias_patrimoniales"),
    "familias_con_hijos": lambda f: _hay(f, "num_descendientes", "num_descendientes_menores_3"),
    # The only ownership signal in the profile: still paying a home bought before 2013.
    "propietarios_vivienda": lambda f: _hay(f, "pagos_vivienda_habitual_pre2013"),
    "donantes": lambda f: _hay(f, "donativos", "donativos_fundaciones_madrid"),
    "planes_de_pensiones": lambda f: _hay(f, "aportacion_plan_pensiones", "contribucion_empresa_plan_pensiones"),
    "bienes_en_el_extranjero": lambda f: f.get("bienes_en_el_extranjero") is True,
    # Nobody unless a fact says so.
    "no_residentes": lambda f: isinstance(f.get("residencia_fiscal"), str)
    and f["residencia_fiscal"].strip().upper() not in ("", "ES"),
    "sociedades": lambda f: _categoria(f) == "sociedad",
}


def perfil_afectado(facts: dict[str, Any], afecta_a: list[str]) -> list[str]:
    """Spanish reasons why a profile is affected by a change tagged ``afecta_a`` (empty: not affected)."""
    return [VOCABULARIO[tag][1] for tag in TAGS if tag in afecta_a and CRITERIOS[tag](facts)]


def notificar_cambio(cambio: CambioNormativo, cases: Iterable[CaseState], store: NotificationStore) -> int:
    """One ``cambio_normativo`` notification per affected case. Returns how many were created."""
    if cambio.relevancia == "baja":
        return 0
    created = 0
    url = f"https://www.boe.es/buscar/act.php?id={cambio.boe_id}"
    for case in cases:
        razones = perfil_afectado({fact.name: fact.value for fact in case.facts}, cambio.afecta_a)
        if not razones:
            continue
        sections = [cambio.resumen, "**Por qué te lo cuento:** " + " ".join(razones)]
        if cambio.en_vigor_desde:
            sections.append(f"**En vigor desde:** {cambio.en_vigor_desde}")
        sections.append(f"**Texto de la norma:** «{cambio.cita_literal}»")
        if cambio.articulos:
            sections.append("**Preceptos modificados:** " + "; ".join(cambio.articulos))
        sections.append(
            f"**Norma:** {cambio.titulo_norma} ({cambio.boe_id}).\n\n"
            "Este resumen se ha generado automáticamente a partir del texto consolidado del BOE y no calcula el "
            "efecto en tu caso."
        )
        created += store.add(
            Notification(
                case_id=case.case_id,
                kind="cambio_normativo",
                key=f"cambio_normativo:{cambio.boe_id}:{cambio.actualizado}",
                title=f"Cambio normativo: {_titulo_corto(cambio.titulo_norma)}",
                body="\n\n".join(sections),
                severity="aviso" if cambio.relevancia == "alta" else "info",
                actions=[
                    "Pregunta al asesor cómo cambia tu situación con esta modificación.",
                    f"Consulta el texto consolidado en el BOE: {url}",
                ],
                sources=[f"{cambio.titulo_norma} ({cambio.boe_id})", url],
            )
        )
    return created


def _titulo_corto(titulo: str, limit: int = 110) -> str:
    titulo = " ".join(titulo.split())
    return titulo if len(titulo) <= limit else titulo[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Glue for boe_watch change events
# --------------------------------------------------------------------------- #


class CambioPendiente(BaseModel):
    """A changed law ready for analysis: its events collapsed and its modified articles parsed."""

    model_config = ConfigDict(extra="forbid")

    boe_id: str
    titulo_norma: str
    desde: date
    actualizado: str
    articulos: list[ArticuloModificado]
    # Keys of the boe_watch events this covers.
    eventos: list[str]


def parse_fecha_actualizacion(value: Any) -> date | None:
    """Parse boe_watch's ``fecha_actualizacion`` ("20201231T113000Z", ISO, or DD/MM/YYYY)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})(?!\d)", text)
    if match:
        return _date(match.group(3), match.group(2), match.group(1))
    match = re.match(rf"^{_FECHA}", text)
    if match:
        return _date(*match.group(1, 2, 3))
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def clave_evento(evento: Mapping[str, Any]) -> str:
    """Stable identity of a boe_watch change event (law + the revision it moved to)."""
    return f"{evento.get('id')}|{evento.get('new_actualizacion')}"


HtmlSource = Mapping[str, str] | Callable[[str], str | None]


class Preparacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Laws worth one LLM call each.
    pendientes: list[CambioPendiente] = Field(default_factory=list)
    # Keys of events that produce nothing: first download, no previous date, no modified article.
    sin_efecto: list[str] = Field(default_factory=list)
    # Keys of events whose HTML could not be loaded: to retry, never to drop silently.
    sin_html: list[str] = Field(default_factory=list)


def preparar_eventos(eventos: Iterable[Mapping[str, Any]], html_por_boe_id: HtmlSource) -> Preparacion:
    """Collapse events per law and parse what changed. No LLM.

    Several pending events of one law become one analysis: only the latest
    HTML exists on disk, so the earliest previous date is compared against it.
    """
    por_norma: dict[str, list[Mapping[str, Any]]] = {}
    out = Preparacion()
    for evento in eventos:
        boe_id = evento.get("id")
        if not isinstance(boe_id, str) or not boe_id:
            continue
        if parse_fecha_actualizacion(evento.get("previous_actualizacion")) is None:
            out.sin_efecto.append(clave_evento(evento))
            continue
        por_norma.setdefault(boe_id, []).append(evento)

    for boe_id, grupo in por_norma.items():
        claves = [clave_evento(evento) for evento in grupo]
        desde = min(d for e in grupo if (d := parse_fecha_actualizacion(e.get("previous_actualizacion"))) is not None)
        ultimo = grupo[-1]
        html = html_por_boe_id(boe_id) if callable(html_por_boe_id) else html_por_boe_id.get(boe_id)
        if not html:
            out.sin_html.extend(claves)
            continue
        articulos = articulos_modificados(html, desde=desde)
        if not articulos:
            out.sin_efecto.extend(claves)
            continue
        out.pendientes.append(
            CambioPendiente(
                boe_id=boe_id,
                titulo_norma=str(ultimo.get("title") or boe_id),
                desde=desde,
                actualizado=str(ultimo.get("new_actualizacion") or ""),
                articulos=articulos,
                eventos=claves,
            )
        )
    return out


def procesar_eventos(
    provider: Any,
    *,
    eventos: Iterable[Mapping[str, Any]],
    html_por_boe_id: HtmlSource,
    cases: Iterable[CaseState],
    store: NotificationStore,
) -> dict[str, Any]:
    """Analyse a batch of boe_watch change events and notify the affected cases.

    ``html_por_boe_id`` is a mapping ``boe id → HTML`` or a loader called with
    the boe id. The summary lists in ``procesados`` the event keys that are
    done (including those that produce nothing) and in ``pendientes`` the ones
    whose HTML was missing or whose LLM call failed, to be retried on a later run.
    """
    all_cases = list(cases)
    preparacion = preparar_eventos(eventos, html_por_boe_id)
    pendientes, procesados = preparacion.pendientes, list(preparacion.sin_efecto)
    summary: dict[str, Any] = {
        "eventos": len(procesados) + len(preparacion.sin_html) + sum(len(p.eventos) for p in pendientes),
        "llamadas_llm": 0,
        "analizados": 0,
        "descartados": 0,
        "errores": len(preparacion.sin_html),
        "notificaciones": 0,
        "procesados": procesados,
        "pendientes": list(preparacion.sin_html),
    }
    for pendiente in pendientes:
        summary["llamadas_llm"] += 1
        try:
            cambio = analizar_cambio(
                provider,
                boe_id=pendiente.boe_id,
                titulo_norma=pendiente.titulo_norma,
                articulos=pendiente.articulos,
                actualizado=pendiente.actualizado or None,
            )
        except ProviderError as exc:
            logger.warning("Law-change analysis failed for %s: %s", pendiente.boe_id, exc)
            summary["errores"] += 1
            summary["pendientes"].extend(pendiente.eventos)
            continue
        procesados.extend(pendiente.eventos)
        if cambio is None:
            summary["descartados"] += 1
            continue
        summary["analizados"] += 1
        summary["notificaciones"] += notificar_cambio(cambio, all_cases, store)
    return summary
