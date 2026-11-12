"""Parameters for self-employed taxpayers (estimación directa simplificada,
modelo 130, modelo 303), loaded from versioned JSON.

Same rule as ``params.py``: no figure lives in the arithmetic. Here each group
of figures also carries its ``evidencia`` — the literal sentence of the law it
was read from, and how each number is spelled in that sentence — so the data
can be re-checked mechanically against the consolidated text on boe.es
(see :func:`comprobar_evidencias`).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
import html
import json
from pathlib import Path
import re
import sys

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.params import DATA_DIR, EjercicioNoDisponibleError, Fuente, FuentePrincipal


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidencia(_Model):
    """A literal quote of the law and the figures read from it."""

    boe_id: str
    articulo: str
    cita: str
    # Path of the figure inside its group (e.g. "porcentaje", "tramos.0.hasta")
    # -> the figure exactly as the quote spells it ("5 por ciento", "2.000 euros").
    cifras: dict[str, str] = Field(default_factory=dict)


class _Grupo(_Model):
    fuente: Fuente
    evidencia: list[Evidencia] = Field(min_length=1)
    # Caveats the calculators repeat whenever they use this group.
    avisos: list[str] = Field(default_factory=list)


class EstimacionDirectaSimplificada(_Grupo):
    # Turnover of the previous year above which the simplified method is not available.
    limite_cifra_negocios: Decimal


class GastosDificilJustificacion(_Grupo):
    porcentaje: Decimal
    limite_anual: Decimal


class GastosDificilJustificacionCeuta(_Grupo):
    """Temporary higher percentage for activities carried on in Ceuta (a year-specific provision)."""

    porcentaje: Decimal
    # First quarter whose payment on account (modelo 130) may use it.
    pagos_fraccionados_desde_trimestre: int = Field(ge=1, le=4)


class ReduccionIrregulares(_Grupo):
    """Art. 32.1 LIRPF: income generated over more than two years or notoriously irregular."""

    porcentaje: Decimal
    limite_base: Decimal  # net income the percentage applies to, at most


class ReduccionInicioActividad(_Grupo):
    """Art. 32.3 LIRPF: first profitable year of a new activity and the following one."""

    porcentaje: Decimal
    limite_base: Decimal
    # Not available when more than this share of the income comes from a former employer.
    max_ingresos_antiguo_pagador: Decimal


class ReduccionDependientes(_Grupo):
    """Art. 32.2.1.º LIRPF: single-client / economically dependent self-employed."""

    importe_general: Decimal
    umbral_superior: Decimal  # exclusive: net income must be below it
    umbral_inferior: Decimal
    importe_adicional_maximo: Decimal
    pendiente: Decimal
    max_otras_rentas: Decimal
    discapacidad: Decimal
    discapacidad_ampliada: Decimal
    # Requirements of art. 32.2.2.º that can be checked with figures.
    max_gastos_sobre_ingresos: Decimal
    min_ingresos_con_retencion: Decimal
    max_prestaciones_trabajo: Decimal
    # Degree of disability (as a fraction) that makes a "persona con discapacidad" (art. 60.3
    # LIRPF) and the one that gives the higher amount of letter b).
    grado_discapacidad: Decimal
    grado_discapacidad_ampliada: Decimal


class ReduccionRentasBajas(_Grupo):
    """Art. 32.2.3.º LIRPF: taxpayers with non-exempt income under the limit."""

    limite_rentas: Decimal  # exclusive
    umbral_inferior: Decimal
    importe_maximo: Decimal
    pendiente: Decimal
    limite_conjunto_reduccion_trabajo: Decimal


class TramoMinoracion(_Model):
    hasta: Decimal  # previous year's net income up to this amount, inclusive
    importe: Decimal  # per quarter


class PagoFraccionado(_Grupo):
    porcentaje: Decimal
    # Professionals are not obliged when at least this share of last year's
    # income bore withholding.
    exencion_porcentaje_retenido: Decimal
    # Art. 110.3.c): flat amount per quarter by previous year's net income.
    minoracion_limite: Decimal
    minoracion_tramos: list[TramoMinoracion] = Field(min_length=1)
    # Art. 110.2: cut of the percentage for activities entitled to the art. 68.4 LIRPF deduction.
    reduccion_ceuta_melilla: Decimal

    def minoracion(self, rendimiento_neto_anterior: Decimal) -> Decimal:
        for tramo in self.minoracion_tramos:
            if rendimiento_neto_anterior <= tramo.hasta:
                return tramo.importe
        return Decimal(0)


class RetencionProfesionales(_Grupo):
    general: Decimal
    inicio_actividad: Decimal
    # Art. 95.1, fourth paragraph: collectors, insurance brokers, lottery delegates and letter d).
    otros_supuestos: Decimal
    # Letter d): last year's gross income from those activities must be below the limit and
    # above this share of the gross income from economic activities plus work.
    supuesto_d_limite_ingresos: Decimal
    supuesto_d_min_proporcion: Decimal
    # Last paragraph: cut of these rates for income entitled to the art. 68.4 LIRPF deduction.
    reduccion_ceuta_melilla: Decimal


class IVA(_Grupo):
    tipo_general: Decimal
    tipo_reducido: Decimal
    tipo_superreducido: Decimal
    anos_compensacion: Decimal


class AutonomosParams(_Model):
    ejercicio: int
    # Consolidated texts the figures were read from, by BOE id.
    fuentes_principales: dict[str, FuentePrincipal]
    # What was deliberately left out, repeated to whoever reads the file.
    no_modelado: list[str] = Field(default_factory=list)
    estimacion_directa_simplificada: EstimacionDirectaSimplificada
    gastos_dificil_justificacion: GastosDificilJustificacion
    # Only in the years a provision sets it (2026: disposición adicional 64.ª LIRPF).
    gastos_dificil_justificacion_ceuta: GastosDificilJustificacionCeuta | None = None
    reduccion_irregulares: ReduccionIrregulares
    reduccion_dependientes: ReduccionDependientes
    reduccion_rentas_bajas: ReduccionRentasBajas
    reduccion_inicio_actividad: ReduccionInicioActividad
    pago_fraccionado: PagoFraccionado
    retencion_profesionales: RetencionProfesionales
    iva: IVA

    def grupos(self) -> dict[str, _Grupo]:
        return {name: value for name in type(self).model_fields if isinstance(value := getattr(self, name), _Grupo)}

    def fuente_texto(self, *boe_ids: str) -> str:
        """'Norma, BOE-A-…, consolidado a …' for the given sources, for a result's ``fuente``."""
        return "; ".join(
            f"{fp.norma}, {fp.boe_id}, consolidado a {fp.consolidado_a}"
            for fp in (self.fuentes_principales[boe_id] for boe_id in boe_ids)
        )


def ejercicios_autonomos_disponibles() -> list[int]:
    return sorted(int(path.stem.rsplit("_", 1)[1]) for path in DATA_DIR.glob("autonomos_*.json"))


@lru_cache(maxsize=16)
def load_autonomos_params(ejercicio: int) -> AutonomosParams:
    path = DATA_DIR / f"autonomos_{ejercicio}.json"
    if not path.exists():
        disponibles = ", ".join(str(year) for year in ejercicios_autonomos_disponibles())
        raise EjercicioNoDisponibleError(
            f"No hay parámetros de autónomos cargados para {ejercicio} (disponibles: {disponibles})."
        )
    return AutonomosParams.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --- Self-check of the data against the law ---------------------------------


def texto_de_html(raw: str) -> str:
    """Plain text of a boe.es consolidated page, one paragraph per line."""
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = " ".join(raw.split())  # line breaks in the source are not paragraph breaks
    raw = re.sub(r"(?i)</(p|div|h\d|li|tr|blockquote)>|<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw).replace("\xa0", " ")
    return "\n".join(" ".join(line.split()) for line in raw.splitlines() if line.strip())


def _compacto(text: str) -> str:
    return " ".join(text.split())


def cifra_a_decimal(literal: str) -> Decimal:
    """'5 por ciento' -> 0.05; '14.047,5 euros' -> 14047.5; '1,14' -> 1.14; 'cuatro años' -> 4."""
    palabras = {"cuatro": "4"}
    token = literal.split()[0]
    number = Decimal(palabras.get(token.lower()) or token.replace(".", "").replace(",", "."))
    return number / 100 if "por ciento" in literal else number


def _hojas(value: object, prefix: str = "") -> dict[str, Decimal]:
    """Every Decimal inside a parameter group, by dotted path."""
    if isinstance(value, Decimal):
        return {prefix: value}
    if isinstance(value, BaseModel):
        items: list[tuple[str, object]] = [
            (name, getattr(value, name))
            for name in type(value).model_fields
            if name not in {"fuente", "evidencia", "avisos"}
        ]
    elif isinstance(value, list):
        items = [(str(index), item) for index, item in enumerate(value)]
    else:
        return {}
    found: dict[str, Decimal] = {}
    for name, item in items:
        found.update(_hojas(item, f"{prefix}.{name}" if prefix else name))
    return found


def comprobar_evidencias(params: AutonomosParams, textos: dict[str, str] | None = None) -> list[str]:
    """Problems found when re-reading the parameters against their quotes.

    Always checked: every figure of every group is spelled in one of the
    group's quotes, and that spelling means the stored value. With ``textos``
    (BOE id -> plain text of the consolidated page, or of the quoted articles)
    each quote must also appear verbatim in its law. An empty list means the
    data is consistent with the text it claims to come from.
    """
    problemas: list[str] = []
    for nombre, grupo in params.grupos().items():
        hojas = _hojas(grupo)
        sin_evidencia = set(hojas)
        for ev in grupo.evidencia:
            if ev.boe_id not in params.fuentes_principales:
                problemas.append(f"{nombre}: {ev.boe_id} no figura en fuentes_principales")
            if textos is not None:
                texto = textos.get(ev.boe_id)
                if texto is None:
                    problemas.append(f"{nombre}: no se ha aportado el texto de {ev.boe_id}")
                elif _compacto(ev.cita) not in _compacto(texto):
                    problemas.append(f"{nombre}: la cita de {ev.articulo} no aparece literal en {ev.boe_id}")
            for ruta, literal in ev.cifras.items():
                if ruta not in hojas:
                    problemas.append(f"{nombre}: la evidencia cita una cifra inexistente ({ruta})")
                    continue
                # The figure as a whole token: "100" must not match inside "10.000".
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
    return problemas


def textos_de_directorio(directorio: Path) -> dict[str, str]:
    """BOE id -> plain text, from ``<BOE-ID>.html`` (downloaded pages) or ``<BOE-ID>.txt`` files."""
    textos: dict[str, str] = {}
    for path in sorted(directorio.glob("BOE-A-*.txt")):
        textos[path.stem] = path.read_text(encoding="utf-8")
    for path in sorted(directorio.glob("BOE-A-*.html")):
        textos[path.stem] = texto_de_html(path.read_text(encoding="utf-8"))
    return textos


def main(argv: list[str] | None = None) -> int:
    """``python -m hacienda_gpt.tax.autonomos_params <ejercicio> <dir with the BOE pages>``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("uso: python -m hacienda_gpt.tax.autonomos_params <ejercicio> <directorio con BOE-A-*.html>")
        return 2
    problemas = comprobar_evidencias(load_autonomos_params(int(args[0])), textos_de_directorio(Path(args[1])))
    for problema in problemas:
        print(problema)
    print("Parámetros coherentes con el texto." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
