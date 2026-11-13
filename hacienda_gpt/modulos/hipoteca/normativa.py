"""Legal parameters of the mortgage module, loaded from versioned JSON (``data/normativa.json``).

Every figure (fee caps, periods, dates) is read from the consolidated text on boe.es and stored with the
literal sentence it comes from and how the figure is spelled there, so :func:`comprobar_evidencias`
can re-check it mechanically: every figure is inside its quote and means the stored value, and (given
the law texts) every quote is verbatim in its law. No figure lives in the arithmetic.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import re
import sys

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.autonomos_params import texto_de_html

DATA_DIR = Path(__file__).resolve().parent / "data"
NORMATIVA_PATH = DATA_DIR / "normativa.json"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FuentePrincipal(_Model):
    norma: str
    boe_id: str
    url: str
    consolidado_a: str
    consultado_el: str
    sha256_html: str


class Fuente(_Model):
    referencia: str
    boe_id: str
    en_vigor_desde: str
    verificado: bool
    nota: str | None = None


class Evidencia(_Model):
    boe_id: str
    articulo: str
    cita: str
    # Field of the group -> the figure exactly as the quote spells it ("0,15 por ciento", "16/06/2019").
    cifras: dict[str, str] = Field(default_factory=dict)


class _Grupo(_Model):
    fuente: Fuente
    evidencia: list[Evidencia] = Field(min_length=1)

    def cita_corta(self) -> str:
        return f"{self.fuente.referencia} ({self.fuente.boe_id})"


class Ley52019(_Grupo):
    entrada_en_vigor: date


class ReembolsoAnticipado(_Grupo):
    preaviso_maximo_meses: Decimal


class ComisionVariable(_Grupo):
    opcion_a_anos: Decimal
    opcion_a_limite: Decimal
    opcion_b_anos: Decimal
    opcion_b_limite: Decimal


class ComisionFijo(_Grupo):
    anos_iniciales: Decimal
    limite_inicial: Decimal
    limite_posterior: Decimal


class CambioAFijo(_Grupo):
    anos: Decimal
    limite: Decimal
    periodo_fijo_minimo_anos: Decimal


class TipoVariable(_Grupo):
    pass


class Ley412007(_Grupo):
    entrada_en_vigor: date
    desistimiento_anos: Decimal
    desistimiento_limite_inicial: Decimal
    desistimiento_limite_posterior: Decimal
    riesgo_tipo_revision_max_meses: Decimal


class Ley21994(_Grupo):
    limite_no_subrogatoria_variable: Decimal
    limite_subrogacion_variable: Decimal
    limite_novacion_ampliacion_plazo: Decimal
    reduccion_arancel_registral: Decimal


class Gastos(_Grupo):
    pass


class Suspension(_Grupo):
    desde: date
    hasta: date


class CodigoBuenasPracticas2022(_Grupo):
    duracion_meses: Decimal
    duracion_meses_dana: Decimal
    precio_adquisicion_maximo: Decimal
    constituidos_hasta: date
    acuerdo_publicado: date


class CodigoBuenasPracticas2012(_Grupo):
    precio_adquisicion_maximo: Decimal


class IndicesOficiales(_Grupo):
    pass


class Normativa(_Model):
    version: str
    fuentes_principales: dict[str, FuentePrincipal]
    no_modelado: list[str] = Field(default_factory=list)
    ley_5_2019: Ley52019
    reembolso_anticipado: ReembolsoAnticipado
    comision_variable: ComisionVariable
    comision_fijo: ComisionFijo
    cambio_a_fijo: CambioAFijo
    tipo_variable: TipoVariable
    ley_41_2007: Ley412007
    ley_2_1994: Ley21994
    gastos: Gastos
    suspension_comisiones_2022_2024: Suspension
    codigo_buenas_practicas_2022: CodigoBuenasPracticas2022
    codigo_buenas_practicas_2012: CodigoBuenasPracticas2012
    indices_oficiales: IndicesOficiales

    def grupos(self) -> dict[str, _Grupo]:
        return {n: v for n in type(self).model_fields if isinstance(v := getattr(self, n), _Grupo)}

    def url(self, boe_id: str) -> str:
        return self.fuentes_principales[boe_id].url


@lru_cache(maxsize=1)
def cargar_normativa() -> Normativa:
    return Normativa.model_validate(json.loads(NORMATIVA_PATH.read_text(encoding="utf-8")))


# --- Self-check ---------------------------------------------------------------------------------

_NUMEROS = {
    "un": 1,
    "una": 1,
    "tres": 3,
    "cinco": 5,
    "diez": 10,
    "doce": 12,
    "treinta y seis": 36,
    "cuarenta y dos": 42,
}
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
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def leer_cifra(literal: str) -> Decimal | date:
    """'0,15 por ciento' -> 0.0015; '1 por 100' -> 0.01; '300.000 euros' -> 300000; '5 primeros años' -> 5;
    'treinta y seis meses' -> 36; 'al menos, 3 años' -> 3; '16/06/2019' and '31 de diciembre de 2024' -> date."""
    texto = " ".join(literal.split())
    if m := re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", texto):
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if m := re.fullmatch(r"(\d{1,2}) de (\w+) de (\d{4})", texto):
        return date(int(m.group(3)), _MESES[m.group(2).lower()], int(m.group(1)))
    for palabra, valor in sorted(_NUMEROS.items(), key=lambda kv: -len(kv[0])):
        if re.match(rf"{palabra}\b", texto.lower()):
            return Decimal(valor)
    m = re.search(r"\d[\d.]*(,\d+)?", texto)
    if not m:
        raise InvalidOperation(literal)
    numero = Decimal(m.group(0).replace(".", "").replace(",", "."))
    return numero / 100 if re.search(r"por (ciento|100)\b", texto) else numero


def _compacto(text: str) -> str:
    return " ".join(text.split())


def comprobar_evidencias(normativa: Normativa, textos: dict[str, str] | None = None) -> list[str]:
    """Problems found re-reading the data against its quotes (and, with ``textos``, the quotes against the
    law texts: BOE id -> plain text). Empty list = consistent."""
    problemas: list[str] = []
    for nombre, grupo in normativa.grupos().items():
        cifras_grupo = {
            campo: getattr(grupo, campo)
            for campo in type(grupo).model_fields
            if campo not in {"fuente", "evidencia"} and isinstance(getattr(grupo, campo), Decimal | date)
        }
        sin_evidencia = set(cifras_grupo)
        if grupo.fuente.boe_id not in normativa.fuentes_principales:
            problemas.append(f"{nombre}: {grupo.fuente.boe_id} no figura en fuentes_principales")
        for ev in grupo.evidencia:
            if ev.boe_id not in normativa.fuentes_principales:
                problemas.append(f"{nombre}: {ev.boe_id} no figura en fuentes_principales")
            if textos is not None:
                texto = textos.get(ev.boe_id)
                if texto is None:
                    problemas.append(f"{nombre}: no se ha aportado el texto de {ev.boe_id}")
                elif _compacto(ev.cita) not in _compacto(texto):
                    problemas.append(f"{nombre}: la cita de {ev.articulo} no aparece literal en {ev.boe_id}")
            for campo, literal in ev.cifras.items():
                if campo not in cifras_grupo:
                    problemas.append(f"{nombre}: la evidencia cita una cifra inexistente ({campo})")
                    continue
                if not re.search(rf"(?<![\d.,]){re.escape(literal)}(?!\d|[.,]\d)", ev.cita):
                    problemas.append(f"{nombre}.{campo}: «{literal}» no aparece en la cita de {ev.articulo}")
                    continue
                try:
                    leido = leer_cifra(literal)
                except (InvalidOperation, KeyError, ValueError):
                    problemas.append(f"{nombre}.{campo}: «{literal}» no es una cifra")
                    continue
                if leido != cifras_grupo[campo]:
                    problemas.append(f"{nombre}.{campo}: el dato es {cifras_grupo[campo]} y la cita dice «{literal}»")
                sin_evidencia.discard(campo)
        problemas.extend(f"{nombre}.{campo}: cifra sin evidencia" for campo in sorted(sin_evidencia))
    return problemas


def textos_de_directorio(directorio: Path) -> dict[str, str]:
    """BOE id -> plain text, from ``<BOE-ID>.html`` (downloaded pages) or ``<BOE-ID>.txt`` (extracts)."""
    textos: dict[str, str] = {}
    for path in sorted(directorio.glob("BOE-A-*.txt")):
        textos[path.stem] = path.read_text(encoding="utf-8")
    for path in sorted(directorio.glob("BOE-A-*.html")):
        textos[path.stem] = texto_de_html(path.read_text(encoding="utf-8"))
    return textos


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - maintenance
    """``python -m hacienda_gpt.modulos.hipoteca.normativa <dir con BOE-A-*.html descargados>``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(main.__doc__)
        return 2
    problemas = comprobar_evidencias(cargar_normativa(), textos_de_directorio(Path(args[0])))
    for problema in problemas:
        print(problema)
    print("Normativa coherente con el texto." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
