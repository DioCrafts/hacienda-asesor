"""Calls for aid (family B) read from the BOE's daily summary, classified without any LLM.

Sections III (other dispositions) and V-B (announcements: the «Extracto de la Resolución … por la
que se convocan…» with the BDNS identifier) are read. A call is kept when its title announces a call
for aid, grants or places; its text (the BOE XML) gives who may apply, the amount and the deadline.
Who it is addressed to is decided by deterministic keyword rules over the title and the beneficiaries
section, each decision with the literal fragment that triggered it. An LLM classifier can be plugged
in behind a flag for the cases the rules leave undecided; its answer is only accepted when the
fragment it quotes is literally in the text.

Regional bulletins are out of scope: only calls published in the BOE are seen.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
import os
import re
from typing import Any, Literal
import unicodedata
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.modulos.ayudas.hogar import Hogar, booleano, entero
from hacienda_gpt.ops import formatos
from hacienda_gpt.ops.formatos_boe import ReglasSumario, extraer_sumario
from hacienda_gpt.proactive.novedades import ItemSumario

# BOE sections read: III (full calls of ministries) and V-B (extracts of calls, with the BDNS id).
SECCIONES = frozenset({"3", "5B"})

Destinatario = Literal["personas", "autonomos_empresas", "entidades", "investigacion", "mixto", "desconocido"]
Nivel = Literal["encaja", "posible", "no"]


def plano(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().split())


def _compacto(text: str) -> str:
    return " ".join(text.split())


# --- Which summary items are calls --------------------------------------------

_SENAL_CONVOCATORIA = ("convoca", "convocatoria")
_OBJETO_CONVOCATORIA = ("ayuda", "beca", "subvenci", "plazas", "prestacion")
# Titles that say "convoca" but are not calls for aid.
_NO_CONVOCATORIA = (
    "subasta",
    "junta general",
    "convocatoria de junta",
    "asamblea",
    "expropiaci",
    "actas previas",
    "oposicion",
    "proceso selectivo",
    "pruebas selectivas",
    "pruebas especificas",
    "concurso de traslados",
    "provision de",
    "elecciones",
    "huelga",
    "emisiones",
    "deuda del estado",
    "licitacion",
    "correccion de errores",
    "corrigen errores",
    "se corrige",
    "convenio",
    "disponibilidad de credito",
    "ampliacion de presupuesto",
    "amplia el plazo",
    "se resuelve",
    "resolucion de concesion",
    "se conceden",
    "premio",
    "concurso fotografico",
    "certamen",
)


def es_candidata(item: ItemSumario) -> bool:
    """The title announces a call for aid, grants or places (not an auction, a board meeting…)."""
    if item.seccion not in SECCIONES:
        return False
    titulo = plano(item.titulo)
    return (
        any(s in titulo for s in _SENAL_CONVOCATORIA)
        and any(o in titulo for o in _OBJETO_CONVOCATORIA)
        and not any(n in titulo for n in _NO_CONVOCATORIA)
    )


def items_del_sumario(payload: Mapping[str, Any], fecha: date) -> list[ItemSumario]:
    """Items of sections III and V-B of one day's summary (same format spec as ``proactive.novedades``)."""
    reglas = formatos.reglas_de("boe_sumario", ReglasSumario)
    return [ItemSumario(fecha=fecha, **item) for item in extraer_sumario(reglas, payload, SECCIONES)]


def candidatas(payload: Mapping[str, Any], fecha: date) -> list[ItemSumario]:
    return [item for item in items_del_sumario(payload, fecha) if es_candidata(item)]


# --- The text of one call ----------------------------------------------------


class Documento(BaseModel):
    model_config = ConfigDict(extra="forbid")

    titulo: str = ""
    tipo: str = ""  # BOE analysis: "Subvenciones (SNPS)"…
    url_pdf: str = ""
    parrafos: list[str] = Field(default_factory=list)

    @property
    def texto(self) -> str:
        return "\n".join(self.parrafos)


def leer_documento(xml: str | bytes) -> Documento:
    """Title, analysis type and paragraphs of a BOE document's XML (``/diario_boe/xml.php?id=…``)."""
    try:
        raiz = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return Documento()
    texto = raiz.find("texto")
    parrafos: list[str] = []
    if texto is not None:
        for p in texto.iter("p"):
            linea = _compacto("".join(p.itertext()))
            if linea:
                parrafos.append(linea)
    return Documento(
        titulo=_compacto(raiz.findtext("metadatos/titulo") or ""),
        tipo=_compacto(raiz.findtext("analisis/tipo") or ""),
        url_pdf=(raiz.findtext("metadatos/url_pdf") or "").strip(),
        parrafos=parrafos,
    )


_ENCABEZADO = re.compile(
    r"^(art[ií]culo \d+|base \w+|primer[oa]|segund[oa]|tercer[oa]|cuart[oa]|quint[oa]|sext[oa]|s[eé]ptim[oa]|"
    r"octav[oa]|noven[oa]|d[eé]cim[oa]|anexo|[ivx]+\.)\b",
    re.IGNORECASE,
)


def _es_encabezado(parrafo: str) -> bool:
    return len(parrafo) < 160 and bool(_ENCABEZADO.match(parrafo))


def secciones(doc: Documento) -> list[tuple[str, list[str]]]:
    """(heading, paragraphs) in reading order; text before the first heading goes under ''."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for parrafo in doc.parrafos:
        if _es_encabezado(parrafo):
            out.append((parrafo, []))
        else:
            out[-1][1].append(parrafo)
    return out


def _seccion(doc: Documento, *claves: str, max_chars: int = 1500) -> str:
    for encabezado, parrafos in secciones(doc):
        if encabezado and any(c in plano(encabezado) for c in claves):
            texto = " ".join(parrafos)
            return texto[:max_chars].rsplit(" ", 1)[0] if len(texto) > max_chars else texto
    return ""


_BDNS = re.compile(r"BDNS\s*\(\s*Identif\.?\s*\)\s*:?\s*(\d+)", re.IGNORECASE)


def bdns_de(doc: Documento) -> str | None:
    match = _BDNS.search(doc.texto)
    return match.group(1) if match else None


# --- Deadlines (Ley 39/2015, art. 30) -----------------------------------------

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
_UNIDADES = {
    "un": 1,
    "una": 1,
    "uno": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciseis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
    "veintiuno": 21,
    "veintiun": 21,
    "veintidos": 22,
    "veintitres": 23,
    "veinticuatro": 24,
    "veinticinco": 25,
    "veintiseis": 26,
    "veintisiete": 27,
    "veintiocho": 28,
    "veintinueve": 29,
    "treinta": 30,
    "cuarenta": 40,
    "cincuenta": 50,
    "sesenta": 60,
    "noventa": 90,
}


def numero_texto(texto: str) -> int | None:
    """'veinte' -> 20; 'treinta y cinco' -> 35; '15' -> 15."""
    texto = plano(texto)
    if texto.isdigit():
        return int(texto)
    partes = texto.split(" y ")
    if all(p in _UNIDADES for p in partes):
        return sum(_UNIDADES[p] for p in partes)
    return None


def _habil(d: date) -> bool:
    return d.weekday() < 5


def _siguiente_habil(d: date) -> date:
    while not _habil(d):
        d += timedelta(days=1)
    return d


def sumar_dias_habiles(desde: date, n: int) -> date:
    """The n-th working day after ``desde`` counting from the next day (weekends excluded; holidays NOT)."""
    d = desde
    contados = 0
    while contados < n:
        d += timedelta(days=1)
        if _habil(d):
            contados += 1
    return d


def sumar_meses(desde: date, n: int) -> date:
    """Art. 30.4: same day number in the month of expiry, or its last day; then art. 30.5."""
    mes = desde.month - 1 + n
    anio, mes = desde.year + mes // 12, mes % 12 + 1
    dia = min(desde.day, calendar.monthrange(anio, mes)[1])
    return _siguiente_habil(date(anio, mes, dia))


TipoPlazo = Literal["fecha", "dias_habiles", "dias_naturales", "meses", "bdns"]


class Plazo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: TipoPlazo
    fin: date
    # Literal sentence of the call the deadline was read from.
    cita: str
    # True when holidays could not be counted (the real last day may be later, never earlier).
    estimado: bool = False
    # Literal lead-in paragraph the sentence hangs from ("b) Para su inclusión en la lista de espera:").
    contexto: str = ""


# "quince días (15) hábiles"; "días" alone are working days (art. 30.2 Ley 39/2015).
_RE_DIAS = re.compile(r"\b(\d+|[a-z]+(?: y [a-z]+)?) (?:\(\d+\) )?dias(?: \(\d+\))?(?: (habiles|naturales))?\b")
_ORDINALES = {
    "primer": 1,
    "segundo": 2,
    "tercer": 3,
    "cuarto": 4,
    "quinto": 5,
    "sexto": 6,
    "septimo": 7,
    "octavo": 8,
    "noveno": 9,
    "decimo": 10,
}
_RE_DIA_ORDINAL = re.compile(r"\b(" + "|".join(_ORDINALES) + r") dia habil\b")
_RE_MESES = re.compile(r"\b(\d+|un|una|dos|tres|cuatro|seis) (mes|meses)\b")
_RE_FECHA = re.compile(
    r"\b(?:hasta|finaliza(?:ra)?|termina(?:ra)?|concluye|concluira|antes)(?: el| del)?(?: dia)? (\d{1,2}) de "
    r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
    r"(?: de (\d{4}))?"
)
_DESDE_PUBLICACION = ("publicacion", "publique", "publicado")
# Sentences with a time limit that is not the one to apply.
_OTROS_PLAZOS = (
    "subsana",
    "recurso",
    "resolver",
    "notifica",
    "reclamacion",
    "justifica",
    "alegacion",
    "ejecucion",
    "devolucion",
    "renuncia",
    "contados desde la recepcion",
    "desde su recepcion",
)


_ENUMERADO = re.compile(r"^(\d+\.?º|[a-z]\)|\d+\))\s")


def _frases(parrafo: str) -> list[str]:
    return [f.strip() for f in re.split(r"(?<=[.;])\s+(?=[A-ZÁÉÍÓÚÑ0-9¿(])", parrafo) if f.strip()]


def _parrafos_de_plazo(doc: Documento) -> list[str]:
    for encabezado, parrafos in secciones(doc):
        e = plano(encabezado)
        if "plazo" in e and ("solicitud" in e or "presentacion" in e):
            return [encabezado, *parrafos] if parrafos else [encabezado]
    return [p for p in doc.parrafos if "plazo" in plano(p) and ("solicitud" in plano(p) or "presentacion" in plano(p))]


def extraer_plazos(doc: Documento, publicacion: date) -> list[Plazo]:
    """Every application deadline the call states, each with its literal sentence."""
    plazos: list[Plazo] = []
    vistos: set[tuple[str, date]] = set()
    entrada = ""
    for parrafo in _parrafos_de_plazo(doc):
        enumerado = bool(_ENUMERADO.match(parrafo))
        if parrafo.rstrip().endswith(":"):
            entrada = parrafo
        elif not enumerado:
            entrada = ""
        for frase in _frases(parrafo):
            p = plano(frase)
            if any(o in p for o in _OTROS_PLAZOS):
                continue
            encontrados: list[tuple[TipoPlazo, date, bool]] = []
            desde_publicacion = any(s in p for s in _DESDE_PUBLICACION)
            for match in _RE_FECHA.finditer(p):
                anio = int(match.group(3)) if match.group(3) else None
                mes, dia = _MESES[match.group(2)], int(match.group(1))
                if anio is None:
                    anio = publicacion.year + (1 if mes < publicacion.month else 0)
                try:
                    encontrados.append(("fecha", date(anio, mes, dia), False))
                except ValueError:
                    continue
            if desde_publicacion:
                for match in _RE_DIAS.finditer(p):
                    n = numero_texto(match.group(1))
                    if n is None or n > 365:
                        continue
                    if match.group(2) != "naturales":
                        encontrados.append(("dias_habiles", sumar_dias_habiles(publicacion, n), True))
                    else:
                        fin = _siguiente_habil(publicacion + timedelta(days=n))
                        encontrados.append(("dias_naturales", fin, True))
                for match in _RE_DIA_ORDINAL.finditer(p):
                    encontrados.append(
                        ("dias_habiles", sumar_dias_habiles(publicacion, _ORDINALES[match.group(1)]), True)
                    )
                for match in _RE_MESES.finditer(p):
                    n = numero_texto(match.group(1))
                    if n is None or n > 24:
                        continue
                    encontrados.append(("meses", sumar_meses(publicacion, n), True))
            for tipo, fin, estimado in encontrados:
                if (tipo, fin) in vistos:
                    continue
                vistos.add((tipo, fin))
                contexto = entrada if enumerado and entrada and entrada != parrafo else ""
                plazos.append(Plazo(tipo=tipo, fin=fin, cita=frase, estimado=estimado, contexto=contexto))
    return sorted(plazos, key=lambda x: x.fin)


# --- Who it is for -------------------------------------------------------------

_PERSONAS = (
    "personas fisicas",
    "pensionista",
    "jovenes",
    "estudiantes",
    "alumnado",
    "alumnos",
    "familias",
    "unidades familiares",
    "unidad familiar",
    "hogares",
    "personas mayores",
    "personas con discapacidad",
    "victimas",
    "titulados",
    "graduados",
    "licenciados",
    "personas residentes",
    "personas usuarias",
    "personas desempleadas",
    "personas solicitantes",
    "persona solicitante",
    "personas interesadas",
    "ciudadanos",
)
_ENTIDADES = (
    "personas juridicas",
    "empresas",
    "pymes",
    "sociedades mercantiles",
    "entidades sin animo de lucro",
    "entidades locales",
    "ayuntamiento",
    "diputacion",
    "comunidades autonomas",
    "universidades",
    "centros de formacion",
    "nuevos centros",
    "red estatal de centros",
    "organismos",
    "asociaciones",
    "fundaciones",
    "cooperativas",
    "agrupaciones",
    "operadores",
    "armadores",
    "consorcio",
    "entidades beneficiarias",
    "administraciones publicas",
    "organizaciones no gubernamentales",
)
_ACTIVIDAD = (
    "desarrollen actividad economica",
    "desarrollan actividad economica",
    "autonomos",
    "trabajadores por cuenta propia",
)
_INVESTIGACION = ("personal investigador", "investigadores", "predoctoral", "postdoctoral", "grupos de investigacion")


class Senal(BaseModel):
    """A classification decision and the literal fragment that triggered it."""

    model_config = ConfigDict(extra="forbid")

    regla: str
    fragmento: str


def _fragmento(textos: Iterable[str], patron: str) -> str | None:
    for texto in textos:
        for frase in _frases(texto):
            if patron in plano(frase):
                return frase[:400]
    return None


def _senales(textos: list[str], patrones: Iterable[str], etiqueta: str) -> list[Senal]:
    out = []
    for patron in patrones:
        fragmento = _fragmento(textos, patron)
        if fragmento is not None:
            out.append(Senal(regla=f"{etiqueta}: «{patron}»", fragmento=fragmento))
    return out


def clasificar_destinatario(titulo: str, beneficiarios: str) -> tuple[Destinatario, list[Senal]]:
    textos = [titulo, beneficiarios]
    personas = _senales(textos, _PERSONAS, "personas")
    entidades = _senales(textos, _ENTIDADES, "entidades")
    actividad = _senales(textos, _ACTIVIDAD, "actividad económica")
    investigacion = _senales(textos, _INVESTIGACION, "investigación")
    senales = personas + entidades + actividad + investigacion
    if investigacion:
        return "investigacion", senales
    if actividad:
        return "autonomos_empresas", senales
    if personas and entidades:
        return "mixto", senales
    if personas:
        return "personas", senales
    if entidades:
        return "entidades", senales
    return "desconocido", senales


# Themes: keywords (accent-free, lower case) a call is tagged with, for matching against the profile.
TEMAS: dict[str, tuple[str, ...]] = {
    "mayores": (
        "pensionista",
        "jubilacion",
        "personas mayores",
        "termalismo",
        "turismo social",
        "sesenta y cinco o mas",
    ),
    "familias": (
        "familias numerosas",
        "familias monoparentales",
        "hijos",
        "hijas",
        "natalidad",
        "crianza",
        "nacimiento",
    ),
    "discapacidad": ("discapacidad", "dependencia"),
    "jovenes": ("jovenes", "juventud", "menores de 35", "menores de treinta y cinco"),
    "estudios": (
        "becas",
        "beca ",
        "estudiantes",
        "alumnado",
        "titulados",
        "graduados",
        "licenciados",
        "matricula",
        "cursando estudios",
        "cursar estudios",
        "actividades formativas",
    ),
    "vivienda": ("alquiler", "vivienda habitual", "rehabilitacion de vivienda"),
    "victimas": ("violencia de genero", "terrorismo"),
    "desempleo": ("desempleadas", "desempleo", "demandantes de empleo"),
    "energia": ("eficiencia energetica", "autoconsumo", "calefaccion"),
}

# Themes that, when the profile cannot judge them, do not stop a call from being proposed.
TEMAS_SECUNDARIOS: frozenset[str] = frozenset({"desempleo", "energia"})

COMUNIDADES: dict[str, tuple[str, ...]] = {
    "Andalucía": ("andalucia",),
    "Aragón": ("aragon",),
    "Asturias": ("asturias",),
    "Illes Balears": ("illes balears", "islas baleares"),
    "Canarias": ("canarias",),
    "Cantabria": ("cantabria",),
    "Castilla y León": ("castilla y leon",),
    "Castilla-La Mancha": ("castilla-la mancha", "castilla la mancha"),
    "Cataluña": ("cataluna", "catalunya"),
    "Comunitat Valenciana": ("comunitat valenciana", "comunidad valenciana"),
    "Extremadura": ("extremadura",),
    "Galicia": ("galicia",),
    "Madrid": ("comunidad de madrid",),
    "Murcia": ("region de murcia",),
    "Navarra": ("navarra",),
    "País Vasco": ("pais vasco", "euskadi"),
    "La Rioja": ("la rioja",),
    "Ceuta": ("ceuta",),
    "Melilla": ("melilla",),
}


def temas_de(*textos: str) -> list[str]:
    planos = " ".join(plano(t) for t in textos)
    return [tema for tema, claves in TEMAS.items() if any(c in planos for c in claves)]


def ambito_de(*textos: str) -> list[str]:
    planos = " ".join(plano(t) for t in textos)
    return [nombre for nombre, claves in COMUNIDADES.items() if any(c in planos for c in claves)]


# --- Optional LLM classifier (behind a flag, gated by a literal quote) ---------

ClasificadorLLM = Callable[[str, str], Mapping[str, Any] | None]
_clasificador_llm: ClasificadorLLM | None = None
_DESTINATARIOS: frozenset[str] = frozenset(
    {"personas", "autonomos_empresas", "entidades", "investigacion", "mixto", "desconocido"}
)


def registrar_clasificador_llm(funcion: ClasificadorLLM | None) -> None:
    """Plug in an LLM classifier ``(titulo, texto) -> {"destinatario", "temas", "cita"}``.

    It is used only when ``AYUDAS_CLASIFICADOR_LLM=1`` and the rules leave the call undecided
    ("desconocido" or "mixto"). Its answer is discarded unless ``cita`` is literally in the text.
    """
    global _clasificador_llm
    _clasificador_llm = funcion


def llm_activo() -> bool:
    return _clasificador_llm is not None and os.environ.get("AYUDAS_CLASIFICADOR_LLM", "").strip() in {"1", "true"}


def aceptar_respuesta_llm(
    respuesta: Mapping[str, Any] | None, texto: str
) -> tuple[Destinatario, list[str], str] | None:
    """The deterministic gate: a known label and a quote that exists verbatim in ``texto``."""
    if not respuesta:
        return None
    destinatario = respuesta.get("destinatario")
    cita = respuesta.get("cita")
    temas = respuesta.get("temas") or []
    if destinatario not in _DESTINATARIOS or not isinstance(cita, str) or len(cita.strip()) < 15:
        return None
    if _compacto(cita) not in _compacto(texto):
        return None
    temas_validos = [t for t in temas if isinstance(t, str) and t in TEMAS]
    return destinatario, temas_validos, _compacto(cita)  # type: ignore[return-value]


# --- The detected call -----------------------------------------------------------


class Convocatoria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boe_id: str
    fecha_publicacion: date
    seccion: str
    departamento: str
    titulo: str
    url: str
    url_pdf: str = ""
    bdns: str | None = None
    destinatario: Destinatario
    temas: list[str] = Field(default_factory=list)
    ambito: list[str] = Field(default_factory=list)
    beneficiarios: str = ""
    objeto: str = ""
    cuantia: str = ""
    plazos: list[Plazo] = Field(default_factory=list)
    senales: list[Senal] = Field(default_factory=list)
    clasificador: Literal["reglas", "llm"] = "reglas"
    detectada_el: date

    def plazos_abiertos(self, hoy: date) -> list[Plazo]:
        return [p for p in self.plazos if p.fin >= hoy]

    def abierta(self, hoy: date) -> bool:
        """Open when a stated deadline has not passed, or (no deadline read) published less than 30 days ago."""
        if self.plazos:
            return bool(self.plazos_abiertos(hoy))
        return (hoy - self.fecha_publicacion).days <= 30

    def proximo_fin(self, hoy: date) -> date | None:
        abiertos = self.plazos_abiertos(hoy)
        return abiertos[0].fin if abiertos else None


def analizar(item: ItemSumario, xml: str | bytes, hoy: date) -> Convocatoria:
    """Classify one candidate from its XML; the rules first, the LLM (if enabled) only when undecided."""
    doc = leer_documento(xml)
    titulo = doc.titulo or item.titulo
    beneficiarios = _seccion(
        doc, "beneficiari", "requisitos de las personas", "destinatari", "requisitos de los solicitantes"
    )
    objeto = _seccion(doc, "objeto", "finalidad", max_chars=600)
    cuantia = _seccion(doc, "cuantia", "importe", "dotacion", "credito", max_chars=600)
    destinatario, senales = clasificar_destinatario(titulo, beneficiarios)
    temas = temas_de(titulo, beneficiarios, objeto)
    clasificador: Literal["reglas", "llm"] = "reglas"
    if destinatario in ("desconocido", "mixto") and llm_activo():
        assert _clasificador_llm is not None
        try:
            respuesta = _clasificador_llm(titulo, doc.texto)
        except Exception:  # an adapter failure must not lose the call
            respuesta = None
        aceptada = aceptar_respuesta_llm(respuesta, doc.texto)
        if aceptada is not None:
            destinatario, temas_llm, cita = aceptada
            temas = sorted(set(temas) | set(temas_llm))
            senales.append(Senal(regla="clasificador LLM (cita comprobada en el texto)", fragmento=cita[:400]))
            clasificador = "llm"
    return Convocatoria(
        boe_id=item.boe_id,
        fecha_publicacion=item.fecha,
        seccion=item.seccion,
        departamento=item.departamento,
        titulo=titulo,
        url=item.url or f"https://www.boe.es/diario_boe/txt.php?id={item.boe_id}",
        url_pdf=doc.url_pdf,
        bdns=bdns_de(doc),
        destinatario=destinatario,
        temas=temas,
        ambito=ambito_de(titulo, beneficiarios),
        beneficiarios=beneficiarios,
        objeto=objeto,
        cuantia=cuantia,
        plazos=extraer_plazos(doc, item.fecha),
        senales=senales,
        clasificador=clasificador,
        detectada_el=hoy,
    )


# --- Does it fit the household? ---------------------------------------------------


class Encaje(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nivel: Nivel
    motivos: list[str] = Field(default_factory=list)
    requisitos_no_comprobados: list[str] = Field(default_factory=list)


def _intereses(perfil: Mapping[str, Any]) -> set[str]:
    valor = perfil.get("ayudas_intereses")
    if isinstance(valor, str):
        valor = [v.strip() for v in valor.split(",")]
    return {str(v).strip() for v in valor} if isinstance(valor, list) else set()


def _predicado(tema: str, h: Hogar, perfil: Mapping[str, Any]) -> tuple[bool | None, str]:
    """(fits?, why) for one theme; None when the profile cannot tell."""
    edad = h.edad
    pensionista = booleano(perfil.get("ayudas_pensionista"))
    if tema in _intereses(perfil):
        return True, f"has indicado interés en «{tema}»"
    if tema == "mayores":
        if pensionista or (edad is not None and edad >= 65):
            return True, "tienes 65 años o más o eres pensionista" if not pensionista else "eres pensionista"
        if edad is not None and edad < 55 and pensionista is not True:
            return False, f"está dirigida a pensionistas o mayores y tienes {edad} años"
        return None, "no consta si eres pensionista"
    if tema == "familias":
        hijos = entero(perfil.get("num_descendientes")) or len(h.hijos) or (h.menores or 0)
        if hijos:
            return True, "tienes hijos a cargo"
        if "num_descendientes" in perfil or h.menores == 0:
            return False, "no consta que tengas hijos"
        return None, "no consta si tienes hijos"
    if tema == "discapacidad":
        if h.discapacidad_33:
            return True, "hay una discapacidad reconocida en el hogar"
        if h.discapacidad_33 is False:
            return False, "no consta discapacidad en el hogar"
        return None, "no consta si hay discapacidad en el hogar"
    if tema == "jovenes":
        if edad is None:
            return None, "no consta tu edad"
        return (True, f"tienes {edad} años") if edad <= 35 else (False, f"está dirigida a jóvenes y tienes {edad} años")
    if tema == "victimas":
        if h.victima_violencia_genero or h.victima_terrorismo:
            return True, "consta la condición de víctima"
        return None, "no consta la condición de víctima"
    if tema == "desempleo":
        if (entero(perfil.get("dias_desempleo")) or 0) > 0:
            return True, "has estado en desempleo"
        return None, "no consta si estás en desempleo"
    if tema == "vivienda":
        if h.alquiler:
            return True, "pagas alquiler de tu vivienda habitual"
        return None, "no consta tu situación de vivienda"
    return None, f"el perfil no permite valorar «{tema}»"


def _misma_comunidad(ambito: list[str], comunidad: str | None) -> bool | None:
    if not ambito:
        return True
    if not comunidad:
        return None
    mia = plano(comunidad)
    return any(any(c in mia or mia in c for c in COMUNIDADES[nombre]) or plano(nombre) in mia for nombre in ambito)


def encaje(conv: Convocatoria, h: Hogar, perfil: Mapping[str, Any]) -> Encaje:
    """Conservative match: "encaja" only for calls to individuals whose themes the profile supports."""
    no_comprobados = [
        "Todos los requisitos de la convocatoria (nacionalidad o residencia, estar al corriente con Hacienda y la "
        "Seguridad Social, los específicos de cada línea): léelos en el BOE antes de solicitar."
    ]
    if conv.destinatario in ("entidades", "investigacion"):
        return Encaje(nivel="no", motivos=[f"Dirigida a {conv.destinatario}, no a hogares."])
    if conv.destinatario == "autonomos_empresas":
        if booleano(perfil.get("es_autonomo")):
            return Encaje(
                nivel="posible",
                motivos=["Dirigida a quienes desarrollan una actividad económica y consta que eres autónomo."],
                requisitos_no_comprobados=no_comprobados,
            )
        return Encaje(nivel="no", motivos=["Dirigida a personas con actividad económica (empresas o autónomos)."])
    comunidad = _misma_comunidad(conv.ambito, h.comunidad)
    if comunidad is False:
        return Encaje(nivel="no", motivos=[f"Limitada a {', '.join(conv.ambito)} y resides en {h.comunidad}."])
    resultados = [(tema, *_predicado(tema, h, perfil)) for tema in conv.temas]
    negativos = [motivo for _, ok, motivo in resultados if ok is False]
    if negativos:
        return Encaje(nivel="no", motivos=negativos)
    positivos = [motivo for _, ok, motivo in resultados if ok is True]
    dudas = [motivo for _, ok, motivo in resultados if ok is None]
    if comunidad is None:
        dudas.append(f"limitada a {', '.join(conv.ambito)} y no consta tu comunidad autónoma")
    bloqueantes = [tema for tema, ok, _ in resultados if ok is None and tema not in TEMAS_SECUNDARIOS]
    if conv.destinatario == "personas" and positivos and not bloqueantes and comunidad is True:
        # A secondary theme the profile cannot judge (unemployment, energy) is usually an alternative route
        # (pensioner OR unemployed over 60): it is listed to be checked but does not block the proposal.
        return Encaje(
            nivel="encaja",
            motivos=positivos,
            requisitos_no_comprobados=no_comprobados,
        )
    motivos = positivos + dudas or ["Dirigida a personas físicas, sin un requisito que el perfil permita comprobar."]
    return Encaje(nivel="posible", motivos=motivos, requisitos_no_comprobados=no_comprobados)
