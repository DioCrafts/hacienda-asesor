"""Find new tax-related norms in the BOE's daily issue, without any LLM.

``boe_watch`` only sees changes to laws we already track. This module reads
each day's summary instead and keeps the dispositions that touch taxes,
whoever passed them — the State or an autonomous community — judged by the
BOE's own analysis of each one: its subject index (``materias``) and the list
of norms it modifies. A housing decree-law that amends the IRPF Act has no tax
word in its title; its analysis says so.

The analysis is sometimes published days after the disposition, so items
without one are kept as pending and looked at again on later runs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
import json
import re
from typing import Any, Literal
import unicodedata
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.ops import formatos
from hacienda_gpt.ops.formatos_boe import ReglasDocumentoXml, ReglasSumario, extraer_documento_xml, extraer_sumario
from hacienda_gpt.proactive.impacto import CRITERIOS, VOCABULARIO
from hacienda_gpt.proactive.store import Notification, NotificationStore
from hacienda_gpt.tax.fuentes import comunidad_de, norma_de_parametros
from hacienda_gpt.tax.params import ejercicios_disponibles, load_params

# Sections of the BOE read in full (I: general dispositions) and by title only (III: other dispositions).
SECCION_GENERAL = "1"
SECCIONES_POR_TITULO = frozenset({"3"})
# Days an item may wait for the BOE to publish its analysis.
DIAS_ESPERA_ANALISIS = 15

Relevancia = Literal["alta", "media", "baja"]


def _plano(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().split())


# Tax → patterns looked for in the BOE's subject index and in titles (accent- and case-insensitive).
TRIBUTOS: dict[str, tuple[str, ...]] = {
    "IRPF": ("renta de las personas fisicas", "irpf"),
    "IVA": ("valor anadido",),
    "Impuesto sobre Sociedades": ("impuesto sobre sociedades",),
    "Sucesiones y Donaciones": ("sucesiones y donaciones",),
    "Patrimonio": ("impuesto sobre el patrimonio", "grandes fortunas"),
    "Transmisiones Patrimoniales y AJD": ("transmisiones patrimoniales", "actos juridicos documentados"),
    "Renta de no Residentes": ("renta de no residentes",),
    "Impuestos Especiales": ("impuestos especiales", "impuesto sobre hidrocarburos", "labores del tabaco"),
    "Tributos locales": (
        "haciendas locales",
        "impuesto sobre bienes inmuebles",
        "impuesto sobre actividades economicas",
        "incremento de valor de los terrenos",
    ),
    "Tributos cedidos": ("tributos cedidos",),
    "Cotizaciones sociales": ("cotizacion a la seguridad social", "cotizaciones sociales", "bases de cotizacion"),
}
# Signs of tax content that name no particular tax.
GENERICOS: tuple[str, ...] = (
    "impuesto",
    "tribut",
    "tasas",
    "medidas fiscales",
    "beneficios fiscales",
    "incentivos fiscales",
    "regimen fiscal",
    "inspeccion fiscal",
    "fraude fiscal",
    "deducciones fiscales",
    "exenciones fiscales",
    "sistema fiscal",
    "catastro",
    "aduanas",
    "recaudacion",
    "autoliquidacion",
    "presupuestos generales",
)
# Titles that contain a tax word but are not tax law.
RUIDO: tuple[str, ...] = ("convenio", "se publica la relacion", "nombra", "cese ", "oposicion", "subvenci")
RANGOS_ALTOS: tuple[str, ...] = (
    "ley",
    "real decreto-ley",
    "decreto-ley",
    "decreto legislativo",
    "ley foral",
    "norma foral",
)
RANGOS_MEDIOS: tuple[str, ...] = ("real decreto", "decreto", "orden", "decreto foral")
# Who is told about a State-level novelty, by tax. Anything not listed is logged but not notified.
DESTINATARIOS: dict[str, tuple[str, ...]] = {
    "IRPF": ("todos",),
    "IVA": ("autonomos", "sociedades"),
    "Impuesto sobre Sociedades": ("sociedades",),
    "Cotizaciones sociales": ("autonomos",),
    "Patrimonio": ("inversores",),
    "Renta de no Residentes": ("no_residentes",),
}


class ItemSumario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boe_id: str
    fecha: date
    seccion: str
    departamento: str
    epigrafe: str = ""
    titulo: str
    url: str = ""


class NormaModificada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boe_id: str
    accion: str  # MODIFICA, DEROGA, AÑADE…
    texto: str
    # We already watch this norm (corpus catalogue or calculator parameters).
    vigilada: bool = False
    # The calculator's parameters come from it.
    parametros: bool = False


class Analisis(BaseModel):
    """What the BOE's XML says about one disposition."""

    model_config = ConfigDict(extra="forbid")

    rango: str = ""
    origen: str = ""
    departamento: str = ""
    materias: list[str] = Field(default_factory=list)
    modifica: list[NormaModificada] = Field(default_factory=list)

    @property
    def publicado(self) -> bool:
        """The BOE has indexed the disposition (subjects or references are present)."""
        return bool(self.materias or self.modifica)


class Novedad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boe_id: str
    fecha: date
    titulo: str
    rango: str
    ambito: Literal["estatal", "autonomico"]
    # Key of the community when its law is loaded in the calculator; its plain name otherwise.
    comunidad: str | None = None
    comunidad_nombre: str | None = None
    tributos: list[str]
    materias: list[str] = Field(default_factory=list)
    modifica: list[NormaModificada] = Field(default_factory=list)
    relevancia: Relevancia
    # False when only the title could be judged (no analysis published yet, or section III).
    analizada: bool
    # Already in the watch catalogue.
    en_catalogo: bool = False
    url: str


def _secciones_leidas() -> frozenset[str]:
    return frozenset({SECCION_GENERAL}) | SECCIONES_POR_TITULO


def extraer_items(reglas: ReglasSumario, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Items of the sections the app reads, as plain data (see ``formatos_boe.extraer_sumario``)."""
    return extraer_sumario(reglas, payload, _secciones_leidas())


def items_del_sumario(payload: Mapping[str, Any], fecha: date) -> list[ItemSumario]:
    """Flatten one day's summary (the JSON of ``/datosabiertos/api/boe/sumario/<día>``).

    Which keys are read is data: the ``boe_sumario`` format spec.
    """
    reglas = formatos.reglas_de("boe_sumario", ReglasSumario)
    return [ItemSumario(fecha=fecha, **item) for item in extraer_items(reglas, payload)]


def analisis_desde(datos: Mapping[str, Any], vigiladas: frozenset[str] = frozenset()) -> Analisis:
    """Build an :class:`Analisis` from the plain extraction of a disposition's XML."""
    return Analisis(
        rango=datos["rango"],
        origen=datos["origen"],
        departamento=datos["departamento"],
        materias=list(datos["materias"]),
        modifica=[
            NormaModificada(
                boe_id=anterior["referencia"],
                accion=anterior["palabra"],
                texto=anterior["texto"],
                vigilada=anterior["referencia"] in vigiladas,
                parametros=norma_de_parametros(anterior["referencia"]) is not None,
            )
            for anterior in datos["anteriores"]
            if anterior["referencia"]
        ],
    )


def leer_analisis(xml: str | bytes, vigiladas: frozenset[str] = frozenset()) -> Analisis:
    """Parse the BOE's XML of a disposition (``/diario_boe/xml.php?id=…``).

    Which paths are read is data: the ``boe_documento_xml`` format spec.
    """
    try:
        datos = extraer_documento_xml(formatos.reglas_de("boe_documento_xml", ReglasDocumentoXml), xml)
    except ElementTree.ParseError:
        return Analisis()
    return analisis_desde(datos, vigiladas)


def _tributos_en(textos: Iterable[str]) -> tuple[list[str], bool]:
    """Named taxes found in ``textos`` and whether any generic tax sign appears."""
    planos = [_plano(t) for t in textos]
    planos = [p for p in planos if "ministerio fiscal" not in p and "fiscalia" not in p]
    tributos = [nombre for nombre, patrones in TRIBUTOS.items() if any(pat in p for p in planos for pat in patrones)]
    generico = any(pat in p for p in planos for pat in GENERICOS)
    return tributos, generico


def _rango_del_titulo(titulo: str) -> str:
    plano = _plano(titulo)
    for rango in ("real decreto-ley", "decreto-ley", "real decreto legislativo", "decreto legislativo", "ley organica",
                  "ley foral", "norma foral", "ley", "real decreto", "decreto foral", "decreto", "orden", "resolucion",
                  "correccion de errores"):  # fmt: skip
        if plano.startswith(rango):
            return rango.capitalize()
    return ""


def _relevancia(rango: str, modifica_parametros: bool) -> Relevancia:
    plano = _plano(rango)
    if "correccion" in plano:
        return "media" if modifica_parametros else "baja"
    if (
        modifica_parametros
        or any(plano == r or plano.startswith(r + " ") for r in RANGOS_ALTOS)
        or "ley organica" in plano
    ):
        return "alta"
    if any(plano == r or plano.startswith(r + " ") for r in RANGOS_MEDIOS):
        return "media"
    return "baja"


_AUTONOMICO = re.compile(
    r"^(comunidad autonoma (de |del |de las |de la )?|comunidad foral de |comunidad de |comunitat |region de |"
    r"ciudad (autonoma )?de |principado de )"
)


def _comunidad(departamento: str, origen: str) -> tuple[str | None, str | None]:
    """(key or plain name, display name) of the community behind a department; (None, None) for the State."""
    plano = _plano(departamento)
    if "autonomic" not in _plano(origen) and not _AUTONOMICO.match(plano):
        return None, None
    nombre = _AUTONOMICO.sub("", plano).strip() or plano
    clave = comunidad_de(nombre) or comunidad_de(plano)
    if clave is not None:
        return clave, load_params(max(ejercicios_disponibles())).comunidades[clave].nombre
    return nombre, departamento.strip().title()


def clasificar(
    item: ItemSumario, analisis: Analisis | None, *, vigiladas: frozenset[str] = frozenset()
) -> Novedad | None:
    """A ``Novedad`` when the disposition touches taxes; None otherwise."""
    titulo_plano = _plano(item.titulo)
    if any(r in titulo_plano for r in RUIDO):
        return None
    por_titulo, generico_titulo = _tributos_en([item.titulo, item.epigrafe])
    materias: list[str] = []
    modifica: list[NormaModificada] = []
    por_materia: list[str] = []
    generico_materia = False
    if analisis is not None and analisis.publicado:
        por_materia, generico_materia = _tributos_en(analisis.materias)
        materias = [m for m in analisis.materias if _tributos_en([m]) != ([], False)]
        modifica = [m for m in analisis.modifica if m.vigilada or m.parametros]
    tributos = sorted(set(por_titulo) | set(por_materia))
    if not (tributos or generico_titulo or generico_materia or modifica):
        return None
    rango = (analisis.rango if analisis is not None else "") or _rango_del_titulo(item.titulo)
    comunidad, nombre = _comunidad(
        (analisis.departamento if analisis is not None else "") or item.departamento,
        analisis.origen if analisis is not None else "",
    )
    relevancia = _relevancia(rango, any(m.parametros for m in modifica))
    if relevancia == "alta" and not tributos and not modifica:
        # Only a generic tax sign and no watched law touched: worth logging, not an alert.
        relevancia = "media"
    return Novedad(
        boe_id=item.boe_id,
        fecha=item.fecha,
        titulo=item.titulo,
        rango=rango,
        ambito="autonomico" if comunidad is not None else "estatal",
        comunidad=comunidad,
        comunidad_nombre=nombre,
        tributos=tributos or ["Otros tributos"],
        materias=materias,
        modifica=modifica,
        relevancia=relevancia,
        analizada=analisis is not None and analisis.publicado,
        en_catalogo=item.boe_id in vigiladas,
        url=item.url or f"https://www.boe.es/diario_boe/txt.php?id={item.boe_id}",
    )


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    novedades: list[Novedad] = Field(default_factory=list)
    # Section I items still waiting for the BOE's analysis: {"boe_id", "fecha", "titulo", "departamento", "epigrafe", "url"}.
    pendientes: list[ItemSumario] = Field(default_factory=list)
    dias_sin_boletin: list[date] = Field(default_factory=list)
    errores: list[str] = Field(default_factory=list)


# Dispositions read in one run (the queue spans two weeks) with not one analysed: not plausible.
MIN_LEIDOS_COBERTURA = 15


def _json_o_vacio(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def revisar(
    dias: Iterable[date],
    *,
    sumario_de: Callable[[date], Mapping[str, Any] | None],
    xml_de: Callable[[str], str | bytes | None],
    vigiladas: frozenset[str] = frozenset(),
    pendientes: Iterable[ItemSumario] = (),
    hoy: date | None = None,
) -> Revision:
    """Read the given days (and re-read pending items) and classify what touches taxes.

    ``sumario_de`` returns a day's summary JSON, or None when there was no
    issue (Sundays); ``xml_de`` returns a disposition's XML, or None on failure.
    """
    hoy = hoy or date.today()
    out = Revision()
    cola: list[ItemSumario] = list(pendientes)
    for dia in dias:
        try:
            payload = sumario_de(dia)
        except Exception as exc:  # one bad day must not lose the others
            out.errores.append(f"sumario {dia.isoformat()}: {exc}")
            continue
        if dia < hoy:
            # Format watch: a weekday without a summary is a holiday… or an API that moved.
            formatos.observar("boe_sumario_dias", None, extraccion={"dia": dia.isoformat(), "hay": payload is not None})
        if payload is None:
            out.dias_sin_boletin.append(dia)
            continue
        del_dia = items_del_sumario(payload, dia)
        # Format watch: a summary that yields nothing looks exactly like a quiet day.
        formatos.observar(
            "boe_sumario",
            _json_o_vacio(payload),
            extraccion=[item.model_dump(mode="json", exclude={"fecha"}) for item in del_dia],
            contexto=dia.isoformat(),
        )
        cola.extend(del_dia)

    vistos: set[str] = set()
    leidos = con_analisis = 0
    sin_analisis: tuple[str, str | bytes] | None = None
    for item in cola:
        if item.boe_id in vistos:
            continue
        vistos.add(item.boe_id)
        if item.seccion != SECCION_GENERAL:
            novedad = clasificar(item, None, vigiladas=vigiladas)
            if novedad is not None:
                out.novedades.append(novedad)
            continue
        analisis: Analisis | None = None
        try:
            xml = xml_de(item.boe_id)
            analisis = leer_analisis(xml, vigiladas) if xml else None
            if xml:
                formatos.observar("boe_documento_xml", xml, contexto=item.boe_id)
                leidos += 1
                con_analisis += analisis is not None and analisis.publicado
                if analisis is not None and not analisis.publicado:
                    sin_analisis = (item.boe_id, xml)
        except Exception as exc:
            out.errores.append(f"xml {item.boe_id}: {exc}")
        if (analisis is None or not analisis.publicado) and (hoy - item.fecha).days <= DIAS_ESPERA_ANALISIS:
            # Report what the title already shows, and look again when the analysis is out.
            out.pendientes.append(item)
        novedad = clasificar(item, analisis, vigiladas=vigiladas)
        if novedad is not None:
            out.novedades.append(novedad)
    if leidos >= MIN_LEIDOS_COBERTURA and con_analisis == 0 and sin_analisis is not None:
        # Every XML parsed, yet none carries an analysis: either the BOE moved it or it stopped publishing it.
        formatos.observar("boe_documento_xml", sin_analisis[1], contexto=f"cobertura:{sin_analisis[0]}:{leidos}")
    return out


def _destinatarios(novedad: Novedad, facts: dict[str, Any]) -> list[str]:
    """Why this profile is told about the novelty (empty: it is not)."""
    if novedad.ambito == "autonomico":
        residencia = facts.get("comunidad_autonoma")
        if not isinstance(residencia, str) or not residencia.strip():
            return []
        mia = comunidad_de(residencia) or _plano(residencia)
        suya = novedad.comunidad or ""
        if mia != suya and suya not in mia:
            return []
        return [f"Resides en {novedad.comunidad_nombre}."]
    tags = {tag for tributo in novedad.tributos for tag in DESTINATARIOS.get(tributo, ())}
    if any(m.parametros for m in novedad.modifica):
        tags.add("todos")
    return [VOCABULARIO[tag][1] for tag in VOCABULARIO if tag in tags and CRITERIOS[tag](facts)]


def notificar_novedad(novedad: Novedad, cases: Iterable[CaseState], store: NotificationStore) -> int:
    """Tell the profiles concerned that a new tax norm is out. Only high-relevance ones are notified."""
    if novedad.relevancia != "alta":
        return 0
    toca_calculadora = [m for m in novedad.modifica if m.parametros]
    created = 0
    for case in cases:
        razones = _destinatarios(novedad, {fact.name: fact.value for fact in case.facts})
        if not razones:
            continue
        sections = [
            f"El BOE del {novedad.fecha.strftime('%d/%m/%Y')} publica una norma que toca: {', '.join(novedad.tributos)}.",
            "**Por qué te lo cuento:** " + " ".join(razones),
        ]
        if novedad.modifica:
            sections.append(
                "**Normas que modifica:** " + "; ".join(f"{m.accion} {m.texto}".strip() for m in novedad.modifica[:6])
            )
        if toca_calculadora:
            sections.append(
                "**Calculadora:** modifica una norma de la que salen las cifras de la calculadora; hasta que se "
                "revisen pueden no recoger este cambio."
            )
        sections.append(
            "Este aviso sale de la clasificación del propio BOE: todavía no se ha analizado el contenido de la norma "
            "ni su efecto en tu caso, y el asesor no podrá citarla hasta que se incorpore a sus fuentes."
        )
        created += store.add(
            Notification(
                case_id=case.case_id,
                kind="cambio_normativo",
                key=f"novedad:{novedad.boe_id}",
                title=f"Norma nueva: {novedad.titulo[:110].rstrip()}{'…' if len(novedad.titulo) > 110 else ''}",
                body="\n\n".join(sections),
                severity="aviso" if toca_calculadora else "info",
                actions=[f"Lee la norma en el BOE: {novedad.url}"],
                sources=[f"{novedad.titulo} ({novedad.boe_id})", novedad.url],
            )
        )
    return created
