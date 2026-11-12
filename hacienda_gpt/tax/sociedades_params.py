"""Parameters of the Impuesto sobre Sociedades for small companies, loaded from
versioned JSON.

Same rule as ``params.py`` and ``autonomos_params.py``: no figure lives in the
arithmetic, and each group of figures carries its ``evidencia`` — the literal
sentence of the Ley 27/2014 it was read from and how each number is spelled in
it — so the data can be re-checked mechanically against the consolidated text
on boe.es (see :func:`comprobar_evidencias`).

One file holds the rates of several years: the transitional provision 44 of the
law fixes them by the year in which the tax period STARTS, and the file lists
the years it covers in ``ejercicios_cubiertos``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import re
import sys

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.autonomos_params import Evidencia, textos_de_directorio
from hacienda_gpt.tax.params import DATA_DIR, EjercicioNoDisponibleError, Fuente, FuentePrincipal


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Grupo(_Model):
    fuente: Fuente
    evidencia: list[Evidencia] = Field(min_length=1)
    # Caveats the calculators repeat whenever they use this group.
    avisos: list[str] = Field(default_factory=list)

    def cita(self, articulo: str) -> str:
        """The literal quote stored for ``articulo`` (so that user-facing text never retypes the law)."""
        for ev in self.evidencia:
            if ev.articulo == articulo:
                return ev.cita
        raise KeyError(articulo)


class TiposAno(_Model):
    """Rates for tax periods starting within one calendar year."""

    # Companies with turnover under ``limite_microempresa``: two-bracket scale.
    micro_primer_tramo: Decimal
    micro_resto: Decimal
    # Entities meeting art. 101 (turnover under ``limite_reducida_dimension``).
    reducida_dimension: Decimal


class TiposGravamen(_Grupo):
    """Art. 29.1 LIS and its transitional provision 44."""

    general: Decimal
    nueva_creacion: Decimal
    limite_microempresa: Decimal  # exclusive: turnover of the previous period below it
    limite_primer_tramo: Decimal  # part of the base taxed at the first rate
    dias_ano: Decimal  # divisor to pro-rate the first bracket in short periods
    limite_reducida_dimension: Decimal  # exclusive
    por_ano: dict[str, TiposAno]


class ReservaCapitalizacion(_Grupo):
    """Art. 25 LIS: reduction for the increase of equity."""

    porcentaje: Decimal
    # Higher percentages by the increase of the average workforce (fractions).
    plantilla_umbral_minimo: Decimal
    plantilla_umbral_medio: Decimal
    plantilla_umbral_alto: Decimal
    porcentaje_plantilla_minimo: Decimal
    porcentaje_plantilla_medio: Decimal
    porcentaje_plantilla_alto: Decimal
    # Ceiling as a share of the positive base before the reduction and the loss relief.
    limite_base: Decimal
    limite_base_micro: Decimal
    limite_cifra_negocios_micro: Decimal  # exclusive
    anos_mantenimiento: Decimal
    anos_pendientes: Decimal

    def porcentaje_por_plantilla(self, incremento: Decimal | None) -> Decimal:
        """Percentage for a workforce increase given as a fraction (0.03 = 3 %)."""
        if incremento is None or incremento < self.plantilla_umbral_minimo:
            return self.porcentaje
        if incremento <= self.plantilla_umbral_medio:
            return self.porcentaje_plantilla_minimo
        if incremento <= self.plantilla_umbral_alto:
            return self.porcentaje_plantilla_medio
        return self.porcentaje_plantilla_alto


class CompensacionBasesNegativas(_Grupo):
    """Art. 26 LIS and additional provision 15."""

    limite_porcentaje: Decimal
    minimo_compensable: Decimal
    umbral_gran_empresa: Decimal  # inclusive: turnover of at least this
    limite_gran_empresa: Decimal
    umbral_gran_empresa_superior: Decimal  # inclusive
    limite_gran_empresa_superior: Decimal

    def limite(self, cifra_negocios_12_meses: Decimal | None) -> Decimal:
        if cifra_negocios_12_meses is not None:
            if cifra_negocios_12_meses >= self.umbral_gran_empresa_superior:
                return self.limite_gran_empresa_superior
            if cifra_negocios_12_meses >= self.umbral_gran_empresa:
                return self.limite_gran_empresa
        return self.limite_porcentaje


class ReservaNivelacion(_Grupo):
    """Art. 105 LIS: deferral of up to a share of the positive base."""

    porcentaje: Decimal  # of the positive taxable base
    limite: Decimal  # yearly ceiling in euros, pro-rated in short periods
    anos_adicion: Decimal  # the reduction is added back within these years
    incremento_incumplimiento: Decimal  # surcharge on the quota if the conditions are breached


class PagoFraccionadoIS(_Grupo):
    """Art. 40 LIS (modelo 202)."""

    porcentaje_cuota: Decimal  # art. 40.2, on the previous period's quota
    # Art. 40.3: this fraction of the tax rate, rounded down.
    base_numerador: Decimal
    base_denominador: Decimal
    # Turnover above which the art. 40.3 method is compulsory (exclusive).
    limite_modalidad_base_obligatoria: Decimal
    # Turnover from which additional provision 14 changes the art. 40.3 method (inclusive).
    umbral_especialidades: Decimal


class ComparacionIRPF(_Grupo):
    """IRPF rules the self-employed/company comparison refers to (quotes, and the dividend withholding)."""

    retencion_capital_mobiliario: Decimal  # art. 101.4 LIRPF: payment on account, not a final tax


class TributacionMinima(_Grupo):
    """Art. 30 bis LIS: only its turnover threshold, to warn that it is not computed."""

    umbral_cifra_negocios: Decimal  # inclusive


class SociedadesParams(_Model):
    ejercicio: int  # edition of the file: the year the data set was read for
    # Log of automatic readings of the source laws (tax/recodificacion/procedencia.py); empty until the first one.
    revisiones: list[dict[str, str]] = Field(default_factory=list)
    # Years (start of the tax period) for which ``tipos_gravamen.por_ano`` has rates.
    ejercicios_cubiertos: list[int] = Field(min_length=1)
    # Consolidated laws the figures are read from (watched for changes by ``tax/fuentes.py``).
    fuentes_principales: dict[str, FuentePrincipal]
    # Texts published once in the BOE and quoted as evidence (the forms approved by ministerial
    # order): there is no consolidated version of them to watch.
    fuentes_complementarias: dict[str, FuentePrincipal] = Field(default_factory=dict)
    no_modelado: list[str] = Field(default_factory=list)
    tipos_gravamen: TiposGravamen
    reserva_capitalizacion: ReservaCapitalizacion
    compensacion_bases_negativas: CompensacionBasesNegativas
    reserva_nivelacion: ReservaNivelacion
    pago_fraccionado: PagoFraccionadoIS
    comparacion_irpf: ComparacionIRPF
    tributacion_minima: TributacionMinima

    def grupos(self) -> dict[str, _Grupo]:
        return {name: value for name in type(self).model_fields if isinstance(value := getattr(self, name), _Grupo)}

    def tipos_de(self, ejercicio: int) -> TiposAno:
        try:
            return self.tipos_gravamen.por_ano[str(ejercicio)]
        except KeyError:
            raise EjercicioNoDisponibleError(
                f"No hay tipos del Impuesto sobre Sociedades cargados para períodos iniciados en {ejercicio}."
            ) from None

    @property
    def ley(self) -> FuentePrincipal:
        """The Ley 27/2014: the first source of the file."""
        return next(iter(self.fuentes_principales.values()))

    def fuente_texto(self, *boe_ids: str) -> str:
        """The Ley 27/2014 plus the other sources named (those a result actually relied on)."""
        todas = {**self.fuentes_complementarias, **self.fuentes_principales}
        fuentes = [self.ley, *(todas[boe_id] for boe_id in boe_ids)]
        return "; ".join(
            f"{fp.norma}, {fp.boe_id}, {'consolidado a' if 'consolidado' in fp.norma else 'publicada el'} "
            f"{fp.consolidado_a}"
            for fp in fuentes
        )


def _archivos() -> list[Path]:
    return sorted(DATA_DIR.glob("sociedades_*.json"), reverse=True)


@lru_cache(maxsize=8)
def _load(path: Path) -> SociedadesParams:
    params = SociedadesParams.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if {str(year) for year in params.ejercicios_cubiertos} != set(params.tipos_gravamen.por_ano):
        raise ValueError(f"{path.name}: ejercicios_cubiertos no coincide con tipos_gravamen.por_ano")
    return params


def ejercicios_sociedades_disponibles() -> list[int]:
    return sorted({year for path in _archivos() for year in _load(path).ejercicios_cubiertos})


def load_sociedades_params(ejercicio: int) -> SociedadesParams:
    """Parameters for tax periods starting in ``ejercicio``.

    The file named after the year wins; otherwise the most recent file that
    covers it (the law fixes several years of rates in advance).
    """
    exacto = DATA_DIR / f"sociedades_{ejercicio}.json"
    for path in [exacto, *_archivos()]:
        if path.exists() and ejercicio in _load(path).ejercicios_cubiertos:
            return _load(path)
    disponibles = ", ".join(str(year) for year in ejercicios_sociedades_disponibles())
    raise EjercicioNoDisponibleError(
        f"No hay parámetros del Impuesto sobre Sociedades cargados para períodos iniciados en {ejercicio} "
        f"(disponibles: {disponibles})."
    )


# --- Self-check of the data against the law ---------------------------------

_PALABRAS = {"cinco": "5", "séptimos": "7"}


def cifra_a_decimal(literal: str) -> Decimal:
    """'25 por ciento' -> 0.25; '23 %' -> 0.23; '26,5 por ciento' -> 0.265;
    '50.000 euros' -> 50000; '1 millón de euros' -> 1000000; '3 años' -> 3; 'cinco' -> 5."""
    token = literal.split()[0]
    number = Decimal(_PALABRAS.get(token.lower()) or token.replace(".", "").replace(",", "."))
    if "por ciento" in literal or "%" in literal:
        return number / 100
    if "millón" in literal or "millones" in literal:
        return number * 1_000_000
    return number


def _compacto(text: str) -> str:
    return " ".join(text.split())


def _hojas(value: object, prefix: str = "") -> dict[str, Decimal]:
    """Every Decimal inside a parameter group, by dotted path."""
    if isinstance(value, Decimal):
        return {prefix: value}
    items: list[tuple[str, object]]
    if isinstance(value, BaseModel):
        items = [
            (name, getattr(value, name))
            for name in type(value).model_fields
            if name not in {"fuente", "evidencia", "avisos"}
        ]
    elif isinstance(value, dict):
        items = [(str(key), item) for key, item in value.items()]
    elif isinstance(value, list):
        items = [(str(index), item) for index, item in enumerate(value)]
    else:
        return {}
    found: dict[str, Decimal] = {}
    for name, item in items:
        found.update(_hojas(item, f"{prefix}.{name}" if prefix else name))
    return found


def comprobar_evidencias(params: SociedadesParams, textos: dict[str, str] | None = None) -> list[str]:
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
            if ev.boe_id not in params.fuentes_principales and ev.boe_id not in params.fuentes_complementarias:
                problemas.append(f"{nombre}: {ev.boe_id} no figura entre las fuentes")
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
                # The figure as a whole token: "5 por ciento" must not match inside "25 por ciento".
                if not re.search(rf"(?<![\w.,]){re.escape(literal)}(?!\w|[.,]\d)", ev.cita):
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


def main(argv: list[str] | None = None) -> int:
    """``python -m hacienda_gpt.tax.sociedades_params <ejercicio> <dir with the BOE pages>``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("uso: python -m hacienda_gpt.tax.sociedades_params <ejercicio> <directorio con BOE-A-*.html>")
        return 2
    problemas = comprobar_evidencias(load_sociedades_params(int(args[0])), textos_de_directorio(Path(args[1])))
    for problema in problemas:
        print(problema)
    print("Parámetros coherentes con el texto." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
