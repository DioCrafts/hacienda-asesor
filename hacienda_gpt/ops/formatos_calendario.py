"""The fiscal calendar under the format watch: coherence of the data, and its contrast with the BOE.

``hacienda_gpt/tax/data/calendario.json`` is maintained by hand: recurring
rules ("the 20th of the month after each quarter") with the regulation that
sets them. Two silent failures are possible, and each is a source here:

* ``calendario_fiscal`` — the data stops making sense: a deadline that does
  not exist in some year (``calendario.py`` skips it without a word), a year
  with no deadline for an obligation, a past deadline listed as upcoming, a
  model number the app does not know, an unverified deadline about to fall
  due, data not reviewed in a year. Checked deterministically on every
  ``ops formatos check`` and ``probe``; no spec: a failure is a report for
  whoever maintains the data.
* ``calendario_boe`` — the regulation moved under the data. For each
  obligation whose source is a consolidated BOE text, the spec holds literal
  quotes of the article that sets the deadline (read from www.boe.es, never
  recited); ``ops formatos probe`` downloads the article and checks every
  quote is still there and that every day in ``calendario.json`` is stated in
  them. When only the wording changed, the diagnosis agent may propose new
  quotes (same gate, reflection and manual apply as every spec; never
  automatic). When the deadline itself changed no quote can pass the gate, so
  the outcome is a report: the legal data is corrected by a person, with its
  source, in ``calendario.json``.

The Renta campaign deadline is set every year by a ministerial order that the
data does not identify: there is no machine-readable source to contrast it
with, and the coherence check reports it as unverified when it is close.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
import json
import logging
import re
from typing import Any

import parsel
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hacienda_gpt.ops import formatos
from hacienda_gpt.ops.formatos import Fallo, Fuente, Lectura

logger = logging.getLogger(__name__)

BOE = "https://www.boe.es"
_BOE_ID = re.compile(r"BOE-[A-Z]-\d{4}-\d+")

# AEAT model numbers the app deals with somewhere (calculators, document readers, texts). A model in the
# calendar outside this list is a typo or something the app cannot help with.
MODELOS_CONOCIDOS = frozenset(
    {"036", "100", "130", "131", "190", "200", "202", "210", "303", "390", "714", "720", "721"}
)
# Facts under which every obligation of the calendar applies.
HECHOS_TODO: dict[str, Any] = {"alta_actividad_economica": True, "bienes_en_el_extranjero": True}
DIAS_AVISO_SIN_VERIFICAR = 90
MAX_DIAS_SIN_REVISAR = 365

_MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_PALABRAS_DIA = (
    "uno",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
    "once",
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
    "veinte",
    "veintiuno",
    "veintidós",
    "veintitrés",
    "veinticuatro",
    "veinticinco",
    "veintiséis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
    "treinta",
    "treinta y uno",
)
_FIN_DE_BLOQUE = re.compile(r"(?i)</(?:p|h[1-6]|li|td|th|div|blockquote)>|<br\s*/?>")
MAX_ARTICULO = 60_000


def _grave(canario: str, detalle: str) -> Fallo:
    return Fallo(canario=canario, gravedad="grave", detalle=detalle)


def _leve(canario: str, detalle: str) -> Fallo:
    return Fallo(canario=canario, gravedad="leve", detalle=detalle)


def normalizar(texto: str) -> str:
    return " ".join(texto.replace("\xa0", " ").split())


def menciona_dia(texto: str, dia: int) -> bool:
    """Whether ``texto`` states the day ``dia`` (1-31), in figures or in words."""
    bajo = texto.lower()
    if re.search(rf"(?<!\d){dia}(?!\d)", bajo):
        return True
    return re.search(rf"(?<![\wáéíóú]){_PALABRAS_DIA[dia - 1]}(?![\wáéíóú])", bajo) is not None


def menciona_mes(texto: str, mes: int) -> bool:
    """Whether ``texto`` names the month, or places the deadline in "the following month"."""
    bajo = texto.lower()
    return _MESES[mes - 1] in bajo or "mes siguiente" in bajo


# --------------------------------------------------------------------------- #
# calendario_boe: the spec and its pure extraction
# --------------------------------------------------------------------------- #


class Contraste(BaseModel):
    """Quotes of one article of a consolidated BOE text that state an obligation's deadline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The obligation id in calendario.json ("regla_dia_inhabil" for the rule on non-working days).
    id: str = Field(min_length=1, max_length=80)
    boe_id: str
    # Start of the article's heading as the page prints it, e.g. "Artículo 71.".
    articulo: str = Field(min_length=3, max_length=60)
    citas: tuple[str, ...] = Field(min_length=1)

    @field_validator("boe_id")
    @classmethod
    def _boe(cls, value: str) -> str:
        if not _BOE_ID.fullmatch(value):
            raise ValueError("el identificador debe tener la forma BOE-A-AAAA-N")
        return value

    @field_validator("citas")
    @classmethod
    def _citas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not 10 <= len(normalizar(cita)) <= 600 for cita in value):
            raise ValueError("cada cita debe tener entre 10 y 600 caracteres")
        return value


class ReglasCalendarioBoe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contrastes: tuple[Contraste, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unicos(self) -> ReglasCalendarioBoe:
        claves = [(c.id, c.boe_id, c.articulo) for c in self.contrastes]
        if len(claves) != len(set(claves)):
            raise ValueError("hay contrastes repetidos (mismo id, norma y artículo)")
        return self


def texto_articulo(html: str, articulo: str) -> str | None:
    """Visible text of the block of a consolidated BOE page that starts with ``articulo`` (None if absent).

    The block runs from the ``<div class="bloque"`` that holds the heading to
    the next one; one line per paragraph. Spaces in the heading match any
    whitespace (the BOE writes "Artículo\u00a030.").
    """
    titulo = r"\s+".join(re.escape(parte) for parte in articulo.split())
    # The heading element first; any element whose text starts that way otherwise.
    encabezado = re.search(r'class="articulo">\s*' + titulo, html) or re.search(">" + titulo, html)
    if encabezado is None:
        return None
    posicion = encabezado.start()
    inicio = html.rfind('<div class="bloque"', 0, posicion)
    inicio = inicio if inicio >= 0 else html.rfind("<", 0, posicion)
    fin = html.find('<div class="bloque"', posicion)
    fin = len(html) if fin < 0 else fin
    trozo = _FIN_DE_BLOQUE.sub(lambda m: m.group(0) + "\n", html[inicio : min(fin, posicion + MAX_ARTICULO)])
    crudo = "".join(parsel.Selector(text=trozo).xpath("//text()").getall())
    return "\n".join(linea for linea in (normalizar(parte) for parte in crudo.split("\n")) if linea)


def carga_contraste(reglas: ReglasCalendarioBoe, paginas: dict[str, str]) -> dict[str, dict[str, str]]:
    """The payload the watch keeps: for each law read, the text of each article the rules quote."""
    out: dict[str, dict[str, str]] = {}
    for contraste in reglas.contrastes:
        html = paginas.get(contraste.boe_id)
        if html is None:
            continue
        texto = texto_articulo(html, contraste.articulo)
        if texto is not None:
            out.setdefault(contraste.boe_id, {})[contraste.articulo] = texto
    return out


def extraer_contraste(reglas: ReglasCalendarioBoe, texto: str) -> list[dict[str, Any]]:
    """For each quote of the rules: is the article there, and is the quote in it (whitespace-insensitive)."""
    carga = json.loads(texto)
    out: list[dict[str, Any]] = []
    for contraste in reglas.contrastes:
        articulo = (carga.get(contraste.boe_id) or {}).get(contraste.articulo)
        plano = normalizar(articulo or "")
        out.append(
            {
                "id": contraste.id,
                "boe_id": contraste.boe_id,
                "articulo": contraste.articulo,
                "encontrado": articulo is not None,
                "citas": [normalizar(cita) in plano for cita in contraste.citas],
            }
        )
    return out


def _canarios_contraste(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    from hacienda_gpt.tax import calendario

    datos = datos if isinstance(datos, list) else []
    fallos: list[Fallo] = []
    citas = {(c.id, c.boe_id, c.articulo): c.citas for c in reglas.contrastes} if reglas else {}
    for item in datos:
        donde = f"{item.get('articulo')} de {item.get('boe_id')} ({item.get('id')})"
        if not item.get("encontrado"):
            fallos.append(
                _grave(
                    "articulo",
                    f"No se encuentra el {donde} en el texto consolidado: ¿se ha renumerado o derogado? Revisa "
                    "esa obligación en tax/data/calendario.json.",
                )
            )
            continue
        for cita, presente in zip(
            citas.get((item.get("id"), item.get("boe_id"), item.get("articulo")), ()), item.get("citas") or []
        ):
            if not presente:
                fallos.append(
                    _grave(
                        "cita",
                        f"El {donde} ya no dice «{cita}». Si el plazo ha cambiado, hay que corregir "
                        "tax/data/calendario.json (dato legal, con su fuente); si solo ha cambiado la redacción, "
                        "basta con una cita nueva.",
                    )
                )
    if isinstance(reglas, ReglasCalendarioBoe):
        fallos += _coherencia_con_los_datos(reglas, calendario._load())
    encontradas = sum(sum(bool(c) for c in item.get("citas") or []) for item in datos)
    return Lectura(fallos=fallos, metricas={"contrastes": len(datos), "citas_encontradas": encontradas})


def _coherencia_con_los_datos(reglas: ReglasCalendarioBoe, cal: Any) -> list[Fallo]:
    """Every day and month of calendario.json must be stated in the quotes of its obligation."""
    fallos: list[Fallo] = []
    referencias = [(o.id, o.fuente.referencia) for o in cal.obligaciones]
    referencias.append(("regla_dia_inhabil", cal.regla_dia_inhabil.referencia))
    for ident, referencia in referencias:
        if _BOE_ID.search(referencia) and not any(c.id == ident for c in reglas.contrastes):
            fallos.append(
                _leve("sin_contraste", f"«{ident}» cita una norma del BOE ({referencia}) pero no tiene citas que contrastar.")
            )
    for obligacion in cal.obligaciones:
        texto = " ".join(cita for c in reglas.contrastes if c.id == obligacion.id for cita in c.citas)
        if not texto:
            continue
        for plazo in obligacion.plazos:
            if not menciona_dia(texto, plazo.dia) or not menciona_mes(texto, plazo.mes):
                fallos.append(
                    _grave(
                        "plazo",
                        f"tax/data/calendario.json fija «{obligacion.id}» ({plazo.periodo}) el día {plazo.dia} del mes "
                        f"{plazo.mes}, pero sus citas del BOE no dicen ese día o ese mes.",
                    )
                )
    return fallos


# --------------------------------------------------------------------------- #
# calendario_fiscal: coherence and freshness of the data
# --------------------------------------------------------------------------- #


def resumen_calendario(reglas: Any, texto: str) -> dict[str, Any]:
    """What the coherence check looks at: the obligations and their rules. Raises on invalid data."""
    from hacienda_gpt.tax import calendario

    cal = calendario.leer_calendario(texto)
    return {
        "consultado_el": cal.consultado_el,
        "obligaciones": [
            {
                "id": o.id,
                "modelo": o.modelo,
                "verificado": o.fuente.verificado,
                "plazos": [p.model_dump() for p in o.plazos],
            }
            for o in cal.obligaciones
        ],
    }


def _hoy_de(contexto: str) -> date:
    try:
        return date.fromisoformat(contexto[:10])
    except ValueError:
        return date.today()


def _canarios_calendario(datos: Any, muestra: str, contexto: str, reglas: Any) -> Lectura:
    from hacienda_gpt.tax import calendario

    hoy = _hoy_de(contexto)
    cal = calendario.leer_calendario(muestra)
    fallos: list[Fallo] = []

    consultado = date.fromisoformat(cal.consultado_el)
    if consultado > hoy:
        fallos.append(_grave("consultado_el", f"«consultado_el» ({consultado}) es posterior a hoy ({hoy})."))
    elif (hoy - consultado).days > MAX_DIAS_SIN_REVISAR:
        fallos.append(
            _leve(
                "antiguedad",
                f"El calendario se contrastó con sus fuentes el {consultado} (hace más de {MAX_DIAS_SIN_REVISAR} "
                "días): revisa que ninguna norma u orden haya movido un plazo.",
            )
        )
    ids = [o.id for o in cal.obligaciones]
    if len(ids) != len(set(ids)):
        fallos.append(_grave("duplicados", "Hay obligaciones con el mismo «id»."))

    for obligacion in cal.obligaciones:
        if obligacion.modelo is not None and obligacion.modelo not in MODELOS_CONOCIDOS:
            fallos.append(
                _grave(
                    "modelo_desconocido",
                    f"«{obligacion.id}» remite al modelo {obligacion.modelo}, que no es ninguno de los que conoce la "
                    "aplicación: ¿errata?",
                )
            )
        for plazo in obligacion.plazos:
            if "{anio}" not in plazo.periodo:
                fallos.append(_leve("periodo", f"El periodo «{plazo.periodo}» de «{obligacion.id}» no lleva {{anio}}."))
            for anio in (hoy.year, hoy.year + 1):
                try:
                    legal = date(anio, plazo.mes, plazo.dia)
                except ValueError:
                    fallos.append(
                        _grave(
                            "fecha_imposible",
                            f"«{obligacion.id}» ({plazo.periodo}): el {plazo.dia}/{plazo.mes} no existe en {anio}; "
                            "hoy ese vencimiento se omite sin aviso.",
                        )
                    )
                    continue
                ajustada = legal
                while plazo.ajustar_inhabil and ajustada.weekday() >= 5:
                    ajustada += timedelta(days=1)
                if ajustada.year != legal.year:
                    fallos.append(
                        _leve(
                            "fuera_del_anio",
                            f"«{obligacion.id}»: el {legal} es fin de semana y el plazo pasa a {ajustada}, ya en otro año.",
                        )
                    )

    # Every obligation has a deadline in this year and in the next one.
    for anio in (hoy.year, hoy.year + 1):
        desde = date(anio, 1, 1)
        hasta = date(anio, 12, 31)
        en_el_anio = {
            v.id.split(":", 1)[0]
            for v in calendario.proximos_vencimientos(
                HECHOS_TODO, hoy=desde, dias=(hasta - desde).days, calendario=cal
            )
            if v.fecha.year == anio
        }
        for ident in ids:
            if ident not in en_el_anio:
                fallos.append(_grave("anio_sin_plazo", f"«{ident}» no tiene ningún vencimiento en {anio}."))

    # What the user is shown from today: never a past date, and the countdown agrees with the date.
    proximos = calendario.proximos_vencimientos(HECHOS_TODO, hoy=hoy, calendario=cal)
    for v in proximos:
        if v.fecha < hoy or v.dias_restantes != (v.fecha - hoy).days:
            fallos.append(
                _grave(
                    "vencido_como_proximo",
                    f"«{v.id}» se presenta como próximo con fecha {v.fecha} y {v.dias_restantes} días restantes.",
                )
            )
    primero: dict[str, Any] = {}
    for v in proximos:
        primero.setdefault(v.id.split(":", 1)[0], v)
    for obligacion in cal.obligaciones:
        siguiente = primero.get(obligacion.id)
        if siguiente is None:
            fallos.append(_grave("sin_proximo", f"«{obligacion.id}» no tiene ningún vencimiento en los próximos 365 días."))
        elif not obligacion.fuente.verificado and siguiente.dias_restantes <= DIAS_AVISO_SIN_VERIFICAR:
            fallos.append(
                _leve(
                    "sin_verificar",
                    f"«{obligacion.titulo}» vence el {siguiente.fecha.strftime('%d/%m/%Y')} y ese plazo no está "
                    f"contrastado ({(obligacion.fuente.nota or obligacion.fuente.referencia).rstrip('.')}). Compruébalo en "
                    f"«{obligacion.fuente.referencia}» y actualiza tax/data/calendario.json.",
                )
            )
    return Lectura(
        fallos=fallos,
        metricas={"obligaciones": len(cal.obligaciones), "proximos_365": len(proximos)},
    )


# --------------------------------------------------------------------------- #
# Reading them
# --------------------------------------------------------------------------- #


def observar_calendario(hoy: date) -> formatos.Observacion | None:
    """Check the bundled calendar data as of ``hoy`` and record the outcome (never raises)."""
    try:
        from hacienda_gpt.tax.calendario import RUTA_CALENDARIO

        crudo = RUTA_CALENDARIO.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read the calendar data: %s", exc)
        crudo = ""
    return formatos.observar("calendario_fiscal", crudo, contexto=hoy.isoformat())


Descargar = Callable[[str, str], "str | None"]


def sondear_calendario(hoy: date, descargar: Descargar) -> list[str]:
    """Check the calendar data and contrast it with the BOE articles its spec quotes; return the errors met.

    Only www.boe.es is read. If a law cannot be read the contrast is not
    recorded at all: "could not read" must not look like "the law changed".
    """
    observar_calendario(hoy)
    errores: list[str] = []
    reglas = formatos.reglas_de("calendario_boe", ReglasCalendarioBoe)
    paginas: dict[str, str] = {}
    for boe_id in dict.fromkeys(c.boe_id for c in reglas.contrastes):
        try:
            html = descargar(BOE + f"/buscar/act.php?id={boe_id}", "text/html")
        except Exception as exc:
            errores.append(f"calendario {boe_id}: {type(exc).__name__}: {exc}")
            continue
        if html is None:
            errores.append(f"calendario {boe_id}: la página del texto consolidado no existe (404)")
            continue
        paginas[boe_id] = html
    if errores:
        return errores
    carga = carga_contraste(reglas, paginas)
    formatos.observar("calendario_boe", json.dumps(carga, ensure_ascii=False, indent=1), contexto=hoy.isoformat())
    return errores


def registrar_fuentes() -> None:
    formatos.registrar(
        Fuente(
            clave="calendario_fiscal",
            descripcion="calendario fiscal del contribuyente (datos propios, tax/data/calendario.json)",
            expectativas=(
                "Cada obligación tiene al menos un vencimiento en el año en curso y en el siguiente, y cada fecha "
                "existe en el año (un 30 de febrero se omitiría sin aviso).",
                "Lo que se muestra como próximo nunca es una fecha pasada.",
                "Cada modelo citado es uno de los que conoce la aplicación.",
                "Un plazo sin contrastar no vence en los próximos 90 días; el conjunto se revisó hace menos de un año.",
            ),
            canarios=_canarios_calendario,
            extraer=resumen_calendario,
            codigo="hacienda_gpt/tax/data/calendario.json, hacienda_gpt/tax/calendario.py",
        )
    )
    formatos.registrar(
        Fuente(
            clave="calendario_boe",
            descripcion="plazos del calendario fiscal contrastados con el artículo del BOE que los fija",
            expectativas=(
                "Para cada obligación cuya fuente es un texto consolidado del BOE, las «citas» de la spec aparecen "
                "literalmente (salvo espacios) en el artículo indicado de buscar/act.php?id=<boe_id>.",
                "Cada día y cada mes de tax/data/calendario.json para esa obligación aparece en sus citas, en cifra "
                "o en letra («veinte»), o la cita sitúa el plazo en «el mes siguiente».",
                "Si el BOE solo ha cambiado la redacción, basta con citas nuevas que sigan diciendo los mismos días. "
                "Si ha cambiado el plazo, NO es adaptable con datos: hay que corregir calendario.json.",
            ),
            reglas=ReglasCalendarioBoe,
            extraer=extraer_contraste,
            canarios=_canarios_contraste,
            # A change here decides which deadline wording counts as "the law": always a person.
            auto_aplicable=False,
            codigo="hacienda_gpt/ops/formatos_calendario.py (texto_articulo, extraer_contraste), "
            "hacienda_gpt/tax/data/calendario.json",
        )
    )
