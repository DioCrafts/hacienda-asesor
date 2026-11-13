"""Regulated figures of a Spanish household electricity bill (2.0TD), loaded from versioned JSON.

Every figure was read on www.boe.es and is stored with the literal sentence (or table row) it comes from,
like ``hacienda_gpt/tax/autonomos_params.py``. :func:`comprobar_evidencias` re-checks the data: each figure
is spelled in one of its quotes and, given the law texts, each quote is verbatim in its law.

Figures that change during the year are stored as dated items (``desde``/``hasta``, both inclusive); the
calculators split a billing period wherever one of them changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import re
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.autonomos_params import cifra_a_decimal, texto_de_html

DATA = Path(__file__).resolve().parent / "data" / "regulados.json"
Periodo3 = Literal["p1", "p2", "p3"]
PERIODOS: tuple[Periodo3, ...] = ("p1", "p2", "p3")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FuentePrincipal(_Model):
    norma: str
    boe_id: str
    url: str
    # Consolidated texts: date of the last update shown by the BOE; diary texts: publication date.
    fecha: str
    tipo_texto: Literal["consolidado", "diario"]
    consultado_el: str
    sha256_html: str


class Fuente(_Model):
    referencia: str
    en_vigor_desde: str
    verificado: bool
    nota: str | None = None


class Evidencia(_Model):
    boe_id: str
    articulo: str
    cita: str
    # Dotted path of a figure in its group -> the figure exactly as the quote spells it.
    cifras: dict[str, str] = Field(default_factory=dict)


class _Grupo(_Model):
    fuente: Fuente
    evidencia: list[Evidencia] = Field(min_length=1)


class _Vigencia(_Grupo):
    desde: date
    hasta: date

    def cubre(self, dia: date) -> bool:
        return self.desde <= dia <= self.hasta


class PrecioRegulado(_Vigencia):
    """Tolls (peajes) or charges (cargos) of 2.0TD: power in €/kW and year, energy in €/kWh."""

    potencia_eur_kw_anio: dict[Literal["p1", "p2"], Decimal]
    energia_eur_kwh: dict[Periodo3, Decimal]


class BonoSocial(_Vigencia):
    eur_cups_anio: Decimal


class AlquilerContador(_Grupo):
    monofasico_eur_mes: Decimal
    trifasico_eur_mes: Decimal


class PVPC(_Grupo):
    ccf_eur_kw_anio: Decimal


class IEEGeneral(_Grupo):
    tipo: Decimal
    minimo_eur_mwh: Decimal


class IVAGeneral(_Grupo):
    tipo: Decimal


class TramoFiscal(_Vigencia):
    """A temporary tax measure for some days: applied, not applied, or conditional on a CPI figure."""

    estado: Literal["aplicada", "no_aplicada", "condicional"]
    iee_tipo: Decimal | None = None
    iee_minimo_eur_mwh: Decimal | None = None
    iva_tipo: Decimal | None = None
    iva_potencia_max_kw: Decimal | None = None
    condicion: str | None = None


class Impuestos(_Model):
    iee_general: IEEGeneral
    iva_general: IVAGeneral
    tramos_temporales: list[TramoFiscal]


class HorasPeriodo(_Model):
    p1: list[tuple[int, int]]
    p2: list[tuple[int, int]]
    p3: list[tuple[int, int]]


class Periodos2TD(_Grupo):
    peninsula_baleares_canarias: HorasPeriodo
    ceuta_melilla: HorasPeriodo


class Festivo(_Model):
    fecha: date
    boe_id: str
    cita: str


class Regulados(_Model):
    consultado_el: str
    fuentes_principales: dict[str, FuentePrincipal]
    no_modelado: list[str]
    periodos_2_0td: Periodos2TD
    festivos: list[Festivo]
    festivos_nota: str
    peajes: list[PrecioRegulado]
    cargos: list[PrecioRegulado]
    bono_social: list[BonoSocial]
    alquiler_contador: AlquilerContador
    pvpc: PVPC
    impuestos: Impuestos

    # --- lookups ------------------------------------------------------------------------------------

    def _vigente[T: _Vigencia](self, items: Sequence[T], dia: date, que: str) -> T:
        for item in items:
            if item.cubre(dia):
                return item
        raise DatoNoDisponibleError(
            f"No hay {que} cargados para el {dia:%d-%m-%Y} (los datos cubren "
            f"{min(i.desde for i in items):%d-%m-%Y} a {max(i.hasta for i in items):%d-%m-%Y}). "
            "Hay que leer en el BOE la norma de ese año y añadirla a los datos del módulo."
        )

    def peaje(self, dia: date) -> PrecioRegulado:
        return self._vigente(self.peajes, dia, "peajes de transporte y distribución")

    def cargo(self, dia: date) -> PrecioRegulado:
        return self._vigente(self.cargos, dia, "cargos del sistema eléctrico")

    def bono(self, dia: date) -> BonoSocial:
        return self._vigente(self.bono_social, dia, "valores de financiación del bono social")

    def tramo_fiscal(self, dia: date) -> TramoFiscal | None:
        return next((t for t in self.impuestos.tramos_temporales if t.cubre(dia)), None)

    def anios_festivos(self) -> set[int]:
        return {f.fecha.year for f in self.festivos}

    def citas(self, *boe_ids: str) -> tuple[str, ...]:
        return tuple(f"{self.fuentes_principales[b].norma} ({b}, {self.fuentes_principales[b].url})" for b in boe_ids)

    def grupos(self) -> dict[str, _Grupo]:
        found: dict[str, _Grupo] = {
            "periodos_2_0td": self.periodos_2_0td,
            "alquiler_contador": self.alquiler_contador,
            "pvpc": self.pvpc,
            "iee_general": self.impuestos.iee_general,
            "iva_general": self.impuestos.iva_general,
        }
        for nombre, items in (
            ("peajes", self.peajes),
            ("cargos", self.cargos),
            ("bono_social", self.bono_social),
            ("tramos_temporales", self.impuestos.tramos_temporales),
        ):
            for item in items:
                found[f"{nombre}[{item.desde}]"] = item
        return found


class DatoNoDisponibleError(LookupError):
    """A regulated figure is not loaded for that date. The message is Spanish, for the user."""


@lru_cache(maxsize=1)
def cargar_regulados() -> Regulados:
    return Regulados.model_validate(json.loads(DATA.read_text(encoding="utf-8")))


# --- Self-check against the law -------------------------------------------------------------------


def _compacto(text: str) -> str:
    return " ".join(text.split())


def _hojas(value: object, prefix: str = "") -> dict[str, Decimal]:
    if isinstance(value, Decimal):
        return {prefix: value}
    items: list[tuple[str, object]]
    if isinstance(value, BaseModel):
        items = [
            (name, getattr(value, name))
            for name in type(value).model_fields
            if name not in {"fuente", "evidencia", "desde", "hasta", "estado", "condicion"}
        ]
    elif isinstance(value, dict):
        items = [(str(k), v) for k, v in value.items()]
    else:
        return {}
    found: dict[str, Decimal] = {}
    for name, item in items:
        found.update(_hojas(item, f"{prefix}.{name}" if prefix else name))
    return found


_MESES = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)


def _mes_de_la_fila(texto: str, cita: str) -> int | None:
    """Month heading (a line 'Enero', 'Febrero'...) under which a holiday row is printed."""
    lineas = texto.splitlines()
    mes = None
    for linea in lineas:
        limpia = linea.strip()
        if limpia in _MESES:
            mes = _MESES.index(limpia) + 1
        elif _compacto(limpia) == _compacto(cita):
            return mes
    return None


def comprobar_evidencias(datos: Regulados, textos: dict[str, str] | None = None) -> list[str]:
    """Problems found re-reading the data against its quotes (and, with ``textos``, against the law).

    ``textos`` maps BOE id -> plain text of the page (or an extract with the quoted paragraphs).
    """
    problemas: list[str] = []
    compactos = {k: _compacto(v) for k, v in (textos or {}).items()}

    def _cita_en_ley(nombre: str, boe_id: str, articulo: str, cita: str) -> None:
        if boe_id not in datos.fuentes_principales:
            problemas.append(f"{nombre}: {boe_id} no figura en fuentes_principales")
        if textos is None:
            return
        if boe_id not in compactos:
            problemas.append(f"{nombre}: no se ha aportado el texto de {boe_id}")
        elif _compacto(cita) not in compactos[boe_id]:
            problemas.append(f"{nombre}: la cita de {articulo} no aparece literal en {boe_id}")

    for nombre, grupo in datos.grupos().items():
        hojas = _hojas(grupo)
        sin_evidencia = set(hojas)
        for ev in grupo.evidencia:
            _cita_en_ley(nombre, ev.boe_id, ev.articulo, ev.cita)
            for ruta, literal in ev.cifras.items():
                if ruta not in hojas:
                    problemas.append(f"{nombre}: la evidencia cita una cifra inexistente ({ruta})")
                    continue
                if not re.search(rf"(?<![\d.,]){re.escape(literal)}(?!\d|[.,]\d)", ev.cita):
                    problemas.append(f"{nombre}.{ruta}: «{literal}» no aparece en la cita de {ev.articulo}")
                    continue
                try:
                    leido = cifra_a_decimal(literal)
                except (InvalidOperation, IndexError):
                    problemas.append(f"{nombre}.{ruta}: «{literal}» no es una cifra")
                    continue
                if leido != hojas[ruta]:
                    problemas.append(f"{nombre}.{ruta}: el dato es {hojas[ruta]} y la cita dice «{literal}»")
                sin_evidencia.discard(ruta)
        problemas.extend(f"{nombre}.{ruta}: cifra sin evidencia" for ruta in sorted(sin_evidencia))

    # Hour ranges of the periods: every "a h-b h" must be printed in the table quote, in this order.
    tabla = _compacto(datos.periodos_2_0td.evidencia[0].cita)
    esperado: list[str] = []
    for horas in (datos.periodos_2_0td.peninsula_baleares_canarias, datos.periodos_2_0td.ceuta_melilla):
        for periodo in PERIODOS:
            esperado.extend(f"{a} h-{b} h" for a, b in getattr(horas, periodo))
    if " ".join(esperado) not in tabla:
        problemas.append("periodos_2_0td: las franjas horarias no coinciden con la tabla citada")

    # Holidays: the quote is the row of the BOE table, with the day number first and under its month.
    for festivo in datos.festivos:
        nombre = f"festivos[{festivo.fecha}]"
        _cita_en_ley(nombre, festivo.boe_id, "anexo", festivo.cita)
        if not festivo.cita.startswith(f"{festivo.fecha.day} "):
            problemas.append(f"{nombre}: la fila citada no empieza por el día {festivo.fecha.day}")
        if festivo.cita.count("*") != 19 or "**" in festivo.cita:
            problemas.append(f"{nombre}: la fila no es de fiesta nacional no sustituible en todas las comunidades")
        if textos is not None and festivo.boe_id in textos:
            mes = _mes_de_la_fila(textos[festivo.boe_id], festivo.cita)
            if mes != festivo.fecha.month:
                problemas.append(f"{nombre}: la fila está bajo el mes {mes}, no {festivo.fecha.month}")
    return problemas


def textos_de_directorio(directorio: Path) -> dict[str, str]:
    """BOE id -> plain text, from ``<BOE-ID>.html`` (pages from boe.es) or ``<BOE-ID>.txt`` (extracts)."""
    textos: dict[str, str] = {}
    for path in sorted(directorio.glob("BOE-*.txt")):
        textos[path.stem] = path.read_text(encoding="utf-8")
    for path in sorted(directorio.glob("BOE-*.html")):
        textos[path.stem] = texto_de_html(path.read_text(encoding="utf-8"))
    return textos


def main(argv: list[str] | None = None) -> int:
    """``python -m hacienda_gpt.modulos.luz.datos <dir with BOE-*.html>``: re-check against the law."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("uso: python -m hacienda_gpt.modulos.luz.datos <directorio con BOE-*.html>")
        return 2
    problemas = comprobar_evidencias(cargar_regulados(), textos_de_directorio(Path(args[0])))
    for problema in problemas:
        print(problema)
    print("Datos coherentes con el texto." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
