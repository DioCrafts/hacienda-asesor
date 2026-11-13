"""Legal rules of the ``caducidades`` module, loaded from versioned JSON (``data/reglas.json``).

Every figure (years of validity, ITV frequencies, warranty and withdrawal periods…) is read from the
consolidated text on boe.es and stored with the literal sentence it comes from (``evidencia``) and how each
number is spelled there (``cifras``). :func:`comprobar_evidencias` re-checks the data mechanically: each
figure is spelled in one of its group's quotes and means the stored value, and — given the law texts —
each quote is verbatim in its law. Nothing here is recited from memory.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
import re
import sys

from pydantic import BaseModel, ConfigDict, Field

DATA_PATH = Path(__file__).resolve().parent / "data" / "reglas.json"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fuente(_Model):
    referencia: str
    en_vigor_desde: str
    verificado: bool
    nota: str | None = None


class FuentePrincipal(_Model):
    norma: str
    boe_id: str
    url: str
    consolidado_a: str
    consultado_el: str
    sha256_html: str


class Evidencia(_Model):
    """A literal quote of the law and the figures read from it (path in the group -> spelling)."""

    boe_id: str
    articulo: str
    cita: str
    cifras: dict[str, str] = Field(default_factory=dict)


class Tramite(_Model):
    """How to renew or act: official procedure name, body and steps (Spanish), official site roots only."""

    nombre: str
    organismo: str
    pasos: list[str] = Field(min_length=1)
    webs: list[str] = Field(default_factory=list)


class _Grupo(_Model):
    fuente: Fuente
    evidencia: list[Evidencia] = Field(min_length=1)
    avisos: list[str] = Field(default_factory=list)

    def citas(self) -> list[Evidencia]:
        return list(self.evidencia)


class TramoEdad(_Model):
    """Validity granted when the holder's age at issue is below ``hasta_edad`` (None: no upper limit)."""

    hasta_edad: int | None
    anos: int | None  # None: permanent


class ReglaDocumentoPorEdad(_Grupo):
    tramos: list[TramoEdad] = Field(min_length=1)
    tramite: Tramite
    # Passport only: children under this age without a DNI get a validity up to that age (art. 5.2).
    edad_menores_sin_dni: int | None = None

    def tramo(self, edad: int) -> TramoEdad:
        for tramo in self.tramos:
            if tramo.hasta_edad is None or edad < tramo.hasta_edad:
                return tramo
        raise ValueError("tabla de tramos sin tramo final")  # pragma: no cover - validated by the tests


class VigenciaPermiso(_Model):
    anos: int
    edad: int
    anos_desde_edad: int


class ReglaPermiso(_Grupo):
    grupo1_clases: list[str]
    grupo2_clases: list[str]
    grupo2: VigenciaPermiso
    resto: VigenciaPermiso
    antelacion_maxima_meses: int
    tramite: Tramite


class TramoITV(_Model):
    """Frequency while the vehicle is younger than ``hasta_anos`` (None: no upper limit); ``meses`` None = exempt."""

    hasta_anos: int | None
    meses: int | None


class CategoriaITV(_Model):
    descripcion: str
    tramos: list[TramoITV] = Field(min_length=1)

    def tramo(self, antiguedad: int) -> TramoITV:
        for tramo in self.tramos:
            if tramo.hasta_anos is None or antiguedad < tramo.hasta_anos:
                return tramo
        raise ValueError("tabla de tramos sin tramo final")  # pragma: no cover


class ReglaITV(_Grupo):
    categorias: dict[str, CategoriaITV]
    margen_anticipo_dias: int
    tramite: Tramite


class ReglaCertificado(_Grupo):
    vigencia_maxima_anos: int
    vigencia_maxima_dnie_anos: int
    identificacion_previa_anos: int
    tramite: Tramite


class ReglaSeguro(_Grupo):
    prorroga_maxima_anos: int
    oposicion_tomador_meses: int
    oposicion_asegurador_meses: int
    comunicacion_modificacion_meses: int
    tramite: Tramite


class ReglaGarantia(_Grupo):
    bienes_anos: int
    digital_anos: int
    segunda_mano_minimo_anos: int
    presuncion_bienes_anos: int
    presuncion_digital_anos: int
    tras_reparacion_anos: int
    prescripcion_anos: int
    en_vigor_desde: date


class ExcepcionDesistimiento(_Model):
    letra: str
    texto: str  # literal letter of art. 103 TRLGDCU


class ReglaDesistimiento(_Grupo):
    dias: int
    dias_ampliado: int
    prorroga_sin_informacion_meses: int
    excepciones: list[ExcepcionDesistimiento]
    tramite: Tramite


class ReglaComputo(_Grupo):
    """Art. 5.1 Código Civil: how days, months and years are counted."""


class Reglas(_Model):
    version: str
    fuentes_principales: dict[str, FuentePrincipal]
    no_modelado: list[str] = Field(default_factory=list)
    dni: ReglaDocumentoPorEdad
    pasaporte: ReglaDocumentoPorEdad
    permiso_conduccion: ReglaPermiso
    itv: ReglaITV
    certificado_electronico: ReglaCertificado
    seguro: ReglaSeguro
    garantia: ReglaGarantia
    desistimiento: ReglaDesistimiento
    computo: ReglaComputo

    def grupos(self) -> dict[str, _Grupo]:
        return {name: value for name in type(self).model_fields if isinstance(value := getattr(self, name), _Grupo)}

    def fuente_texto(self, boe_id: str) -> str:
        fp = self.fuentes_principales[boe_id]
        return f"{fp.norma}, {fp.boe_id}, consolidado a {fp.consolidado_a}"

    def url(self, boe_id: str) -> str:
        return self.fuentes_principales[boe_id].url


@lru_cache(maxsize=1)
def cargar_reglas() -> Reglas:
    return Reglas.model_validate(json.loads(DATA_PATH.read_text(encoding="utf-8")))


# --- Self-check of the data against the law ---------------------------------

_PALABRAS = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "el": 1,  # "el año siguiente": one year
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "diez": 10,
    "doce": 12,
    "catorce": 14,
    "treinta": 30,
    "sesenta": 60,
    "setenta": 70,
}
# ITV frequencies, in months.
_FRECUENCIAS = {"bienal": 24, "anual": 12, "semestral": 6}
_UNIDADES = {"año", "años", "mes", "meses", "día", "días", "naturales", "siguiente", "posterior", "de", "edad"}


def cifra_a_numero(literal: str) -> Decimal:
    """'Dos años' -> 2; 'sesenta y cinco años' -> 65; '30 días naturales' -> 30; 'Bienal' -> 24 (months)."""
    texto = literal.strip().lower()
    if texto in _FRECUENCIAS:
        return Decimal(_FRECUENCIAS[texto])
    total = 0
    leido = False
    for token in texto.split():
        if token in _UNIDADES:
            break
        if token == "y":
            continue
        if token.isdigit():
            total += int(token)
        elif token in _PALABRAS:
            total += _PALABRAS[token]
        else:
            raise ValueError(f"no sé leer «{literal}» como cifra")
        leido = True
    if not leido:
        raise ValueError(f"no sé leer «{literal}» como cifra")
    return Decimal(total)


def _hojas(value: object, prefix: str = "") -> dict[str, Decimal]:
    """Every numeric figure inside a group, by dotted path (texts, dates and procedure steps excluded)."""
    if isinstance(value, bool):
        return {}
    if isinstance(value, int | Decimal):
        return {prefix: Decimal(value)}
    items: list[tuple[str, object]]
    if isinstance(value, BaseModel):
        items = [
            (name, getattr(value, name))
            for name in type(value).model_fields
            if name not in {"fuente", "evidencia", "avisos", "tramite"}
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


def _compacto(text: str) -> str:
    return " ".join(text.split())


def textos_literales(reglas: Reglas) -> list[tuple[str, str, str]]:
    """(group, BOE id, literal text) for every quote the data claims is verbatim in a law."""
    found: list[tuple[str, str, str]] = []
    for nombre, grupo in reglas.grupos().items():
        found.extend((nombre, ev.boe_id, ev.cita) for ev in grupo.evidencia)
    found.extend(
        ("desistimiento", "BOE-A-2007-20555", excepcion.texto) for excepcion in reglas.desistimiento.excepciones
    )
    return found


def comprobar_evidencias(reglas: Reglas, textos: dict[str, str] | None = None) -> list[str]:
    """Problems found re-reading the rules against their quotes (and, given ``textos``, against the laws).

    ``textos``: BOE id -> plain text of the consolidated page (or of extracts of the quoted articles).
    An empty list means the data is consistent with the text it claims to come from.
    """
    problemas: list[str] = []
    for nombre, grupo in reglas.grupos().items():
        hojas = _hojas(grupo)
        sin_evidencia = set(hojas)
        for ev in grupo.evidencia:
            if ev.boe_id not in reglas.fuentes_principales:
                problemas.append(f"{nombre}: {ev.boe_id} no figura en fuentes_principales")
            for ruta, literal in ev.cifras.items():
                if ruta not in hojas:
                    problemas.append(f"{nombre}: la evidencia cita una cifra inexistente ({ruta})")
                    continue
                if not re.search(rf"(?<![\w.,]){re.escape(literal)}(?![\w]|[.,]\d)", ev.cita):
                    problemas.append(f"{nombre}.{ruta}: «{literal}» no aparece en la cita de {ev.articulo}")
                    continue
                try:
                    leido = cifra_a_numero(literal)
                except ValueError as exc:
                    problemas.append(f"{nombre}.{ruta}: {exc}")
                    continue
                if leido != hojas[ruta]:
                    problemas.append(f"{nombre}.{ruta}: el dato es {hojas[ruta]} y la cita dice «{literal}»")
                sin_evidencia.discard(ruta)
        problemas.extend(f"{nombre}.{ruta}: cifra sin evidencia" for ruta in sorted(sin_evidencia))
    if textos is not None:
        for nombre, boe_id, literal in textos_literales(reglas):
            texto = textos.get(boe_id)
            if texto is None:
                problemas.append(f"{nombre}: no se ha aportado el texto de {boe_id}")
            elif _compacto(literal) not in _compacto(texto):
                problemas.append(f"{nombre}: «{literal[:80]}…» no aparece literal en {boe_id}")
    return problemas


def textos_de_directorio(directorio: Path) -> dict[str, str]:
    """BOE id -> plain text, from ``<BOE-ID>.html`` (downloaded consolidated pages) or ``<BOE-ID>.txt`` extracts."""
    from hacienda_gpt.tax.autonomos_params import texto_de_html

    textos: dict[str, str] = {}
    for path in sorted(directorio.glob("BOE-A-*.txt")):
        textos[path.stem] = path.read_text(encoding="utf-8")
    for path in sorted(directorio.glob("BOE-A-*.html")):
        textos[path.stem] = texto_de_html(path.read_text(encoding="utf-8"))
    return textos


def main(argv: list[str] | None = None) -> int:
    """``python -m hacienda_gpt.modulos.caducidades.reglas <dir with BOE-A-*.html|txt>``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("uso: python -m hacienda_gpt.modulos.caducidades.reglas <directorio con BOE-A-*.html>")
        return 2
    problemas = comprobar_evidencias(cargar_reglas(), textos_de_directorio(Path(args[0])))
    for problema in problemas:
        print(problema)
    print("Reglas coherentes con el texto." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
