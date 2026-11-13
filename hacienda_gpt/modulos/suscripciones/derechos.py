"""Consumer-rights data read from the BOE (``data/derechos_consumidor.json``) and its self-check.

Each right carries literal quotes of the consolidated law (``evidencia``) and the figures read from them
(``valores``, with the literal spelling in ``cifras``). :func:`comprobar` re-reads the data: every figure
must be spelled in one of its quotes and mean the stored value, and, given the law texts, every quote must
appear verbatim in its law. ``python -m hacienda_gpt.modulos.suscripciones.derechos <dir>`` runs the check
against freshly downloaded pages (``<BOE-ID>.html`` or ``.txt``).
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

from hacienda_gpt.tax.autonomos_params import textos_de_directorio
from hacienda_gpt.tax.params import Fuente, FuentePrincipal

DATA = Path(__file__).parent / "data" / "derechos_consumidor.json"


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidencia(_Modelo):
    boe_id: str
    articulo: str
    cita: str
    cifras: dict[str, str] = Field(default_factory=dict)


class Derecho(_Modelo):
    titulo: str
    resumen: str
    sectores: list[str]
    fuente: Fuente
    valores: dict[str, Decimal] = Field(default_factory=dict)
    evidencia: list[Evidencia] = Field(min_length=1)


class DerechosConsumidor(_Modelo):
    version: str
    descripcion: str
    fuentes_principales: dict[str, FuentePrincipal]
    no_modelado: list[str]
    derechos: dict[str, Derecho]

    def derecho(self, clave: str) -> Derecho:
        return self.derechos[clave]

    def valor(self, clave: str, campo: str) -> Decimal:
        return self.derechos[clave].valores[campo]

    def entero(self, clave: str, campo: str) -> int:
        return int(self.valor(clave, campo))

    def url(self, boe_id: str) -> str:
        return self.fuentes_principales[boe_id].url

    def citas(self, *claves: str) -> tuple[str, ...]:
        """Sources of the given rights, as 'BOE-A-… (url)' for the tool's ``citas``."""
        vistos: list[str] = []
        for clave in claves:
            for ev in self.derechos[clave].evidencia:
                texto = f"{ev.boe_id} {self.url(ev.boe_id)}"
                if texto not in vistos:
                    vistos.append(texto)
        return tuple(vistos)

    def norma(self, boe_id: str) -> str:
        return self.fuentes_principales[boe_id].norma.removesuffix(" (texto consolidado)")


@lru_cache(maxsize=1)
def cargar() -> DerechosConsumidor:
    return DerechosConsumidor.model_validate(json.loads(DATA.read_text(encoding="utf-8")))


_PALABRAS = {
    "un": 1,
    "una": 1,
    "dos": 2,
    "cinco": 5,
    "ocho": 8,
    "diez": 10,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "veinticuatro": 24,
}


def cifra_a_decimal(literal: str) -> Decimal:
    """'catorce días naturales' -> 14; 'un mes' -> 1; '250 personas' -> 250."""
    token = literal.split()[0].lower()
    if token in _PALABRAS:
        return Decimal(_PALABRAS[token])
    return Decimal(token.replace(".", "").replace(",", "."))


def _compacto(texto: str) -> str:
    return " ".join(texto.split())


def comprobar(datos: DerechosConsumidor, textos: dict[str, str] | None = None) -> list[str]:
    """Problems found re-reading the data against its quotes (and against the laws, given ``textos``)."""
    problemas: list[str] = []
    for clave, derecho in datos.derechos.items():
        sin_evidencia = set(derecho.valores)
        for ev in derecho.evidencia:
            if ev.boe_id not in datos.fuentes_principales:
                problemas.append(f"{clave}: {ev.boe_id} no figura en fuentes_principales")
            if textos is not None:
                texto = textos.get(ev.boe_id)
                if texto is None:
                    problemas.append(f"{clave}: no se ha aportado el texto de {ev.boe_id}")
                elif _compacto(ev.cita) not in _compacto(texto):
                    problemas.append(f"{clave}: la cita de {ev.articulo} no aparece literal en {ev.boe_id}")
            for campo, literal in ev.cifras.items():
                if campo not in derecho.valores:
                    problemas.append(f"{clave}: la evidencia cita una cifra inexistente ({campo})")
                    continue
                if not re.search(rf"(?<![\w.,]){re.escape(literal)}(?![\w]|[.,]\d)", ev.cita):
                    problemas.append(f"{clave}.{campo}: «{literal}» no aparece en la cita de {ev.articulo}")
                    continue
                if cifra_a_decimal(literal) != derecho.valores[campo]:
                    problemas.append(f"{clave}.{campo}: el dato es {derecho.valores[campo]} y la cita dice «{literal}»")
                sin_evidencia.discard(campo)
        problemas.extend(f"{clave}.{campo}: cifra sin evidencia" for campo in sorted(sin_evidencia))
        try:
            date.fromisoformat(derecho.fuente.en_vigor_desde)
        except ValueError:
            problemas.append(f"{clave}: en_vigor_desde no es una fecha")
    return problemas


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("uso: python -m hacienda_gpt.modulos.suscripciones.derechos <directorio con BOE-A-*.html|txt>")
        return 2
    problemas = comprobar(cargar(), textos_de_directorio(Path(args[0])))
    for problema in problemas:
        print(problema)
    print("Datos coherentes con el texto de la ley." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
