"""Parameters of the Impuesto sobre el Patrimonio and of the Impuesto Temporal
de Solidaridad de las Grandes Fortunas (ITSGF), loaded from versioned JSON.

Same rule as ``params.py`` and ``autonomos_params.py``: no figure lives in the
arithmetic. Every group of figures carries its ``fuente`` and its ``evidencia``
(the literal sentence of the law and how each number is spelled in it), for the
State and for each of the fifteen common-regime communities, so the file can be
re-checked mechanically against the consolidated texts on boe.es
(see :func:`comprobar_evidencias`).

A community that has legislated nothing on the tax is recorded with
``regula = False``; one that has legislated only part (say, the minimum but no
scale) leaves the rest as ``None`` and the state rule applies to it.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import re
import sys
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from hacienda_gpt.tax.autonomos_params import Evidencia, _compacto, _Model, texto_de_html
from hacienda_gpt.tax.params import (
    DATA_DIR,
    EjercicioNoDisponibleError,
    Escala,
    Fuente,
    FuentePrincipal,
    Tramo,
    normalizar_comunidad,
)


class _Grupo(_Model):
    fuente: Fuente
    evidencia: list[Evidencia] = Field(min_length=1)


class ViviendaHabitual(_Grupo):
    """Art. 4.Nueve: the main home is exempt up to this amount."""

    limite_exento: Decimal


class MinimoDiscapacidad(_Model):
    grado_desde: Decimal  # degree of disability from which it applies (0.33 = 33 %)
    importe: Decimal


class MinimoExento(_Grupo):
    # None = the community only regulates the disability amounts; the general one is the State's.
    importe: Decimal | None
    # Higher minimums for taxpayers with a disability, ascending by degree.
    discapacidad: list[MinimoDiscapacidad] = Field(default_factory=list)


class EscalaPatrimonio(_Grupo):
    tramos: list[Tramo] = Field(min_length=1)

    def escala(self) -> Escala:
        return Escala(tramos=self.tramos, fuente=self.fuente)

    def cuota(self, base: Decimal) -> Decimal:
        """Tax on ``base`` using the cumulative column as the law prints it (unrounded)."""
        return self.escala().cuota(base)


class LimiteConjunto(_Grupo):
    """Art. 31 Ley 19/1991 / art. 3.Doce Ley 38/2022."""

    porcentaje_base_irpf: Decimal
    reduccion_maxima: Decimal


class ObligacionDeclarar(_Grupo):
    """Art. 37: must file when tax is due or the assets are worth more than this."""

    umbral_bienes_derechos: Decimal


class Estado(_Model):
    vivienda_habitual: ViviendaHabitual
    minimo_exento: MinimoExento
    escala: EscalaPatrimonio
    limite_conjunto: LimiteConjunto
    obligacion_declarar: ObligacionDeclarar


class VigenciaITSGF(_Grupo):
    vigente: bool


class HechoImponibleITSGF(_Grupo):
    umbral_patrimonio_neto: Decimal


class ITSGF(_Model):
    vigencia: VigenciaITSGF
    hecho_imponible: HechoImponibleITSGF
    minimo_exento: MinimoExento
    escala: EscalaPatrimonio
    limite_conjunto: LimiteConjunto
    # Art. 3.Quince: the wealth tax actually paid is deducted. No figures.
    deduccion_patrimonio: _Grupo


class BonificacionGeneral(_Grupo):
    """The community's general allowance on the quota, as it works while the ITSGF is in force."""

    # Share of the quota that is waived (1 = 100 %). None = no percentage allowance.
    porcentaje: Decimal | None
    # How the ITSGF changes it:
    #  "ninguno"                – the percentage applies regardless of the ITSGF;
    #  "diferencia"             – the allowance is the wealth-tax quota minus the ITSGF quota
    #                             (it replaces the percentage, see ``umbral_base_itsgf``);
    #  "porcentaje_menos_itsgf" – the percentage allowance is reduced by the ITSGF payable.
    regimen_itsgf: Literal["ninguno", "diferencia", "porcentaje_menos_itsgf"]
    # With "diferencia": the replacement only happens when the net wealth minus
    # ``minimo_descontado`` exceeds this amount; below it ``porcentaje`` applies. None = always replaced.
    umbral_base_itsgf: Decimal | None = None
    minimo_descontado: Decimal | None = None

    @model_validator(mode="after")
    def _coherente(self) -> BonificacionGeneral:
        if (self.umbral_base_itsgf is None) != (self.minimo_descontado is None):
            raise ValueError("umbral_base_itsgf y minimo_descontado van juntos")
        if self.regimen_itsgf != "diferencia" and self.porcentaje is None:
            raise ValueError("una bonificación sin porcentaje solo tiene sentido en el régimen 'diferencia'")
        if self.umbral_base_itsgf is not None and (self.regimen_itsgf != "diferencia" or self.porcentaje is None):
            raise ValueError("el umbral solo se usa para elegir entre el porcentaje y la diferencia")
        return self


class ComunidadPatrimonio(_Model):
    nombre: str
    alias: list[str] = Field(default_factory=list)
    # Consolidated text read for this community (key of ``fuentes_principales``).
    boe_id: str
    # False = that text says nothing about the wealth tax: state rules apply in full.
    regula: bool
    minimo_exento: MinimoExento | None = None
    escala: EscalaPatrimonio | None = None
    bonificacion: BonificacionGeneral | None = None
    # Regional rules the calculator does not apply, by article and title.
    no_modelado: list[str] = Field(default_factory=list)
    # Caveats repeated as warnings whenever this community is used.
    notas: list[str] = Field(default_factory=list)
    # Whether these same figures applied to the accrual of 31 December 2025.
    aplicable_2025: bool
    diferencias_2025: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherente(self) -> ComunidadPatrimonio:
        if not self.regula and (self.minimo_exento or self.escala or self.bonificacion or self.no_modelado):
            raise ValueError("una comunidad que no regula el impuesto no puede tener parámetros propios")
        if self.aplicable_2025 == bool(self.diferencias_2025):
            raise ValueError("diferencias_2025 debe explicar por qué aplicable_2025 es falso, y solo entonces")
        return self


class PatrimonioParams(_Model):
    ejercicio: int
    devengo: str
    fuentes_principales: dict[str, FuentePrincipal]
    # What was deliberately left out of the state rules.
    no_modelado: list[str] = Field(default_factory=list)
    # Whether the state and ITSGF figures also applied to the accrual of 31 December 2025.
    aplicable_2025: bool
    estado: Estado
    itsgf: ITSGF
    comunidades: dict[str, ComunidadPatrimonio]

    @model_validator(mode="after")
    def _fuentes(self) -> PatrimonioParams:
        for key, comunidad in self.comunidades.items():
            if comunidad.boe_id not in self.fuentes_principales:
                raise ValueError(f"{key}: {comunidad.boe_id} no figura en fuentes_principales")
            ligada = comunidad.bonificacion is not None and comunidad.bonificacion.regimen_itsgf != "ninguno"
            if ligada and not self.itsgf.vigencia.vigente:
                # Those allowances are transitional rules "while the ITSGF is in force".
                raise ValueError(f"{key}: bonificación ligada al ITSGF en un ejercicio en que no está vigente")
        return self

    def comunidad(self, nombre: str) -> tuple[str, ComunidadPatrimonio] | None:
        """Look a community up by key or alias (case/accent-insensitive)."""
        wanted = normalizar_comunidad(nombre)
        for key, comunidad in self.comunidades.items():
            nombres = {key, key.replace("_", " "), comunidad.nombre, *comunidad.alias}
            if wanted in {normalizar_comunidad(n) for n in nombres}:
                return key, comunidad
        return None

    def grupos(self) -> dict[str, _Grupo]:
        """Every group of figures, by dotted name ('estado.escala', 'comunidades.madrid.bonificacion'…)."""
        found: dict[str, _Grupo] = {}

        def collect(prefix: str, model: BaseModel) -> None:
            for name in type(model).model_fields:
                value = getattr(model, name)
                if isinstance(value, _Grupo):
                    found[f"{prefix}.{name}"] = value

        collect("estado", self.estado)
        collect("itsgf", self.itsgf)
        for key, comunidad in self.comunidades.items():
            collect(f"comunidades.{key}", comunidad)
        return found

    def fuente_texto(self, *boe_ids: str) -> str:
        """'Norma, BOE-A-…, consolidado a …' for the given sources, for a result's ``fuente``."""
        return "; ".join(
            f"{fp.norma}, {fp.boe_id}, consolidado a {fp.consolidado_a}"
            for fp in (self.fuentes_principales[boe_id] for boe_id in dict.fromkeys(boe_ids))
        )


def ejercicios_patrimonio_disponibles() -> list[int]:
    return sorted(int(path.stem.rsplit("_", 1)[1]) for path in DATA_DIR.glob("patrimonio_*.json"))


@lru_cache(maxsize=16)
def load_patrimonio_params(ejercicio: int) -> PatrimonioParams:
    path = DATA_DIR / f"patrimonio_{ejercicio}.json"
    if not path.exists():
        disponibles = ", ".join(str(year) for year in ejercicios_patrimonio_disponibles())
        raise EjercicioNoDisponibleError(
            f"No hay parámetros del Impuesto sobre el Patrimonio cargados para {ejercicio} (disponibles: {disponibles})."
        )
    return PatrimonioParams.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --- Self-check of the data against the law ---------------------------------

# Figures that are rates: the law prints them as percentages even when the cell has no sign.
_PORCENTAJES = {"tipo", "porcentaje", "porcentaje_base_irpf", "reduccion_maxima", "grado_desde"}
# Tolerance when re-adding a scale's printed cumulative column (the laws round each row to the cent).
_TOLERANCIA_ESCALA = Decimal("0.06")


def cifra_a_decimal(literal: str, ruta: str) -> Decimal:
    """'700.000 euros' -> 700000; '0,210' at a ``tipo`` -> 0.0021; '100 por 100', '50 %', '33%' -> 1, 0.5, 0.33."""
    token = literal.split()[0].rstrip("%")
    number = Decimal(token.replace(".", "").replace(",", "."))
    return number / 100 if ruta.rsplit(".", 1)[-1] in _PORCENTAJES else number


def _hojas(value: object, prefix: str = "") -> dict[str, Decimal]:
    """Every Decimal inside a parameter group, by dotted path."""
    if isinstance(value, Decimal):
        return {prefix: value}
    if isinstance(value, BaseModel):
        items: list[tuple[str, object]] = [
            (name, getattr(value, name)) for name in type(value).model_fields if name not in {"fuente", "evidencia"}
        ]
    elif isinstance(value, list):
        items = [(str(index), item) for index, item in enumerate(value)]
    else:
        return {}
    found: dict[str, Decimal] = {}
    for name, item in items:
        found.update(_hojas(item, f"{prefix}.{name}" if prefix else name))
    return found


def _comprobar_escala(nombre: str, escala: EscalaPatrimonio) -> list[str]:
    """The printed cumulative column must follow from the brackets and rates next to it."""
    problemas: list[str] = []
    inicio, acumulado = Decimal(0), Decimal(0)
    for indice, tramo in enumerate(escala.tramos):
        ultimo = indice == len(escala.tramos) - 1
        if (tramo.hasta is None) != ultimo:
            problemas.append(f"{nombre}.tramos.{indice}: solo el último tramo puede no tener límite")
            break
        if tramo.cuota_hasta_inicio is not None:
            if abs(tramo.cuota_hasta_inicio - acumulado) > _TOLERANCIA_ESCALA:
                problemas.append(
                    f"{nombre}.tramos.{indice}: la cuota acumulada impresa ({tramo.cuota_hasta_inicio}) no cuadra "
                    f"con los tramos anteriores ({acumulado.quantize(Decimal('0.01'))})"
                )
            acumulado = tramo.cuota_hasta_inicio
        elif indice:
            problemas.append(f"{nombre}.tramos.{indice}: falta la cuota acumulada")
        if tramo.hasta is not None:
            if tramo.hasta <= inicio:
                problemas.append(f"{nombre}.tramos.{indice}: los tramos no son crecientes")
            acumulado += (tramo.hasta - inicio) * tramo.tipo
            inicio = tramo.hasta
    return problemas


def comprobar_evidencias(params: PatrimonioParams, textos: dict[str, str] | None = None) -> list[str]:
    """Problems found when re-reading the parameters against their quotes.

    Always checked: every figure of every group is spelled in one of the
    group's quotes and that spelling means the stored value; and every scale's
    printed cumulative column is consistent with its brackets and rates. With
    ``textos`` (BOE id -> plain text of the consolidated page, or of the quoted
    articles) each quote must also appear verbatim in its law. An empty list
    means the data is consistent with the text it claims to come from.
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
                    leido = cifra_a_decimal(literal, ruta)
                except (InvalidOperation, IndexError):
                    problemas.append(f"{nombre}.{ruta}: «{literal}» no es una cifra")
                    continue
                if leido != hojas[ruta]:
                    problemas.append(f"{nombre}.{ruta}: el dato es {hojas[ruta]} y la cita dice «{literal}»")
                sin_evidencia.discard(ruta)
        problemas.extend(f"{nombre}.{ruta}: cifra sin evidencia" for ruta in sorted(sin_evidencia))
        if isinstance(grupo, EscalaPatrimonio):
            problemas.extend(_comprobar_escala(nombre, grupo))
    return problemas


def comprobar_sin_regulacion(params: PatrimonioParams, textos: dict[str, str]) -> list[str]:
    """Communities recorded as not regulating the tax: their FULL text must have no chapter or article on it.

    Only meaningful with the complete consolidated pages (not with extracts).
    """
    problemas: list[str] = []
    for key, comunidad in params.comunidades.items():
        if comunidad.regula:
            continue
        texto = textos.get(comunidad.boe_id)
        if texto is None:
            problemas.append(f"comunidades.{key}: no se ha aportado el texto de {comunidad.boe_id}")
            continue
        for linea in texto.splitlines():
            plano = normalizar_comunidad(linea)
            if plano == "impuesto sobre el patrimonio" or (
                plano.startswith(("articulo", "disposicion")) and "impuesto sobre el patrimonio" in plano
            ):
                problemas.append(f"comunidades.{key}: {comunidad.boe_id} sí contiene «{linea[:80]}»")
    return problemas


def textos_de_directorio(directorio: Path) -> dict[str, str]:
    """BOE id -> plain text, from ``<ID>.html`` (downloaded pages) or ``<ID>.txt`` (extracts) files."""
    textos: dict[str, str] = {}
    for path in sorted(directorio.glob("*.txt")):
        textos[path.stem] = path.read_text(encoding="utf-8")
    for path in sorted(directorio.glob("*.html")):
        textos[path.stem] = texto_de_html(path.read_text(encoding="utf-8"))
    return textos


def main(argv: list[str] | None = None) -> int:
    """``python -m hacienda_gpt.tax.patrimonio_params <ejercicio> <dir with the BOE pages>``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("uso: python -m hacienda_gpt.tax.patrimonio_params <ejercicio> <directorio con <ID>.html>")
        return 2
    params = load_patrimonio_params(int(args[0]))
    textos = textos_de_directorio(Path(args[1]))
    problemas = comprobar_evidencias(params, textos) + comprobar_sin_regulacion(params, textos)
    for problema in problemas:
        print(problema)
    print("Parámetros coherentes con el texto." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
