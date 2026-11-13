"""Measuring the loop against encodings a person already trusts.

Before letting the loop write on its own, its owner needs a number: how often does it reach, from the law alone,
the encoding a careful person reached? This module takes groups that are shipped and trusted, HIDES their encoding
and their quotes from the reader, runs the normal loop — same prompts, same gate, same reviewer — and compares the
proposal with the human encoding field by field. Nothing is written to any parameter file and no proposal is stored.

What the comparison can and cannot say:

* figures and structural fields are compared exactly (a number is a number however it is spelled);
* free text (titles, summaries, the list of unchecked requirements) is reported apart and never counted: two correct
  encodings word it differently;
* a disagreement is not always the loop's mistake — the human encoding may be the one that is wrong or dated. The
  report is a list to read, not a score to trust blindly.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import random
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.tax.params import DATA_DIR
from hacienda_gpt.tax.recodificacion.adaptadores import es_del_tipo
from hacienda_gpt.tax.recodificacion.nucleo import (
    MAX_ITER,
    Objetivo,
    RecodificacionError,
    adaptador_de,
    ejercicios_en,
    recodificar,
)
from hacienda_gpt.tax.verificacion import TextoDe

# Never compared: written by the program, or dated by it.
_PROCEDENCIA = frozenset({"fuente", "evidencia", "avisos", "recodificada_el", "en_vigor_desde"})
# Reported apart: wording, not law.
_TEXTO = frozenset({"titulo", "resumen", "descripcion", "requisitos_no_comprobados", "nota", "notas"})
# Spanish legal prose and JSON run at roughly 3–4 characters per token.
CARACTERES_POR_TOKEN = 3.5


class _Registro(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GrupoEvaluado(_Registro):
    grupo: str
    tipo: str
    ruta: str
    ejercicio: int
    # "propuesta" (the loop converged), "sin_converger", or "error".
    estado: str
    resultado: str | None = None
    iteraciones: int = 0
    llamadas_llm: int = 0
    caracteres_enviados: int = 0
    campos_iguales: int = 0
    # {"campo", "humano", "bucle"} for every field that differs; a field only one side has shows None on the other.
    campos_distintos: list[dict[str, Any]] = Field(default_factory=list)
    textos_distintos: list[str] = Field(default_factory=list)
    # Share of compared fields that agree; None when the loop proposed no value.
    acuerdo: float | None = None
    por_revisar: list[str] = Field(default_factory=list)
    objeciones_abiertas: list[str] = Field(default_factory=list)
    error: str | None = None


class Evaluacion(_Registro):
    grupos: list[GrupoEvaluado] = Field(default_factory=list)
    evaluados: int = 0
    convergieron: int = 0
    # Converged AND every compared field equal to the human encoding.
    identicos: int = 0
    # Would have been applied with nobody looking (gate clean, reviewer agreed) — and of those, how many differ.
    aplicables_sin_persona: int = 0
    aplicables_sin_persona_con_diferencias: int = 0
    acuerdo_medio: float | None = None
    llamadas_llm: int = 0
    caracteres_enviados: int = 0
    tokens_entrada_aprox: int = 0
    coste_entrada_aprox_usd: float | None = None
    interrumpida: str | None = None
    nota: str = (
        "El acuerdo compara campo a campo con la codificación humana, que se ocultó al lector. Una diferencia no "
        "siempre es un error del bucle: léelas. El coste real, con salida incluida, está en «ops consumo» "
        "(propósitos recodificacion y recodificacion-revisor)."
    )


def _canon(valor: Any) -> Any:
    if isinstance(valor, bool) or valor is None:
        return valor
    if isinstance(valor, int | float | Decimal | str):
        try:
            return format(Decimal(str(valor)).normalize(), "f")
        except InvalidOperation:
            return " ".join(str(valor).split())
    return valor


def aplanar(valor: Any, prefijo: str = "") -> dict[str, Any]:
    """Leaves of a JSON value by dotted path, numbers in one spelling; provenance fields left out."""
    out: dict[str, Any] = {}
    if isinstance(valor, dict):
        for clave, item in valor.items():
            if clave not in _PROCEDENCIA and item is not None:
                out.update(aplanar(item, f"{prefijo}.{clave}" if prefijo else str(clave)))
    elif isinstance(valor, list | tuple):
        for i, item in enumerate(valor):
            out.update(aplanar(item, f"{prefijo}[{i}]"))
    else:
        out[prefijo] = _canon(valor)
    return out


def _es_texto(campo: str) -> bool:
    return any(parte.split("[")[0] in _TEXTO for parte in campo.split("."))


def comparar(humano: Any, bucle: Any) -> tuple[int, list[dict[str, Any]], list[str]]:
    """(fields that agree, fields that differ, text fields that differ) between two encodings of one group."""
    h, b = aplanar(humano), aplanar(bucle)
    iguales = 0
    distintos: list[dict[str, Any]] = []
    textos: list[str] = []
    for campo in sorted(set(h) | set(b)):
        if _es_texto(campo):
            if h.get(campo) != b.get(campo):
                textos.append(campo)
        elif h.get(campo) == b.get(campo):
            iguales += 1
        else:
            distintos.append({"campo": campo, "humano": h.get(campo), "bucle": b.get(campo)})
    return iguales, distintos, textos


def seleccionar(
    objetivos: Sequence[Objetivo],
    n: int,
    *,
    semilla: int = 0,
    tipos: Sequence[str] | None = None,
    ambito: str | None = None,
) -> list[Objetivo]:
    """``n`` groups drawn at random (the same ones for the same seed), spread over the kinds asked for."""
    candidatos = [
        o
        for o in objetivos
        if es_del_tipo(o.tipo, tipos) and (ambito is None or o.ambito == ambito) and o.version is None
    ]
    random.Random(semilla).shuffle(candidatos)
    return candidatos[: max(0, n)]


def _normalizado(objetivo: Objetivo, valor: Any, citas: list[str], ficheros: dict[int, Any]) -> Any:
    """The human encoding as the adapter would keep it in a proposal (defaults dropped, provenance out)."""
    adaptador = adaptador_de(objetivo.tipo)
    sin_procedencia = (
        {k: v for k, v in valor.items() if k not in ("fuente", "recodificada_el", "evidencia", "avisos")}
        if isinstance(valor, dict)
        else valor
    )
    try:
        modelo, _ = adaptador.controlar(sin_procedencia, citas, objetivo, efectos_desde=None, datos=ficheros)
    except Exception:  # an adapter that cannot take the shipped value as a proposal: compare it as it is
        modelo = None
    return adaptador.normalizar(modelo) if modelo is not None else sin_procedencia


def evaluar(
    objetivos: Sequence[Objetivo],
    *,
    lector: Any,
    revisor: Any,
    texto_de: TextoDe,
    data_dir: Path = DATA_DIR,
    max_iter: int = MAX_ITER,
    usd_por_millon_de_tokens: float | None = None,
) -> Evaluacion:
    """Re-encode each group from the law with its shipped encoding hidden, and compare. Writes nothing."""
    from hacienda_gpt.ops.consumo import PresupuestoAgotadoError

    evaluacion = Evaluacion()
    acuerdos: list[float] = []
    for objetivo in objetivos:
        con_grupo = [e for e in sorted(objetivo.antes, reverse=True) if objetivo.antes[e] is not None]
        if not con_grupo:
            continue
        ejercicio = con_grupo[0]
        humano = objetivo.antes[ejercicio]
        ficheros = {
            e: json.loads((data_dir / objetivo.fichero.format(ejercicio=e)).read_text(encoding="utf-8"))
            for e in ejercicios_en(data_dir, objetivo.fichero)
        }
        fila = GrupoEvaluado(
            grupo=objetivo.etiqueta,
            tipo=objetivo.tipo,
            ruta="/".join(objetivo.ruta),
            ejercicio=ejercicio,
            estado="error",
        )
        evaluacion.grupos.append(fila)
        # What the reader is NOT shown: the encoding, its quotes and why it would be revisited.
        oculto = objetivo.model_copy(
            update={
                "ejercicios": [ejercicio],
                "antes": {ejercicio: None},
                "citas_antes": [],
                "referencia_anterior": None,
                "origen": "evaluacion",
                "motivo": [],
            }
        )
        try:
            propuesta = recodificar(
                oculto, lector=lector, revisor=revisor, texto_de=texto_de, datos=ficheros, max_iter=max_iter
            )
        except PresupuestoAgotadoError as exc:
            evaluacion.grupos.pop()
            evaluacion.interrumpida = str(exc)
            break
        except (ProviderError, RecodificacionError) as exc:
            fila.error = str(exc)[:300]
            evaluacion.evaluados += 1
            continue
        evaluacion.evaluados += 1
        fila.estado, fila.resultado = propuesta.estado, propuesta.resultado
        fila.iteraciones, fila.llamadas_llm = len(propuesta.iteraciones), propuesta.llamadas_llm
        fila.caracteres_enviados = propuesta.caracteres_enviados
        fila.por_revisar = propuesta.control.revisar if propuesta.control else []
        fila.objeciones_abiertas = propuesta.objeciones_abiertas
        evaluacion.llamadas_llm += propuesta.llamadas_llm
        evaluacion.caracteres_enviados += propuesta.caracteres_enviados
        # The last value the loop reached, converged or not: a near miss is worth seeing too.
        valor = propuesta.despues if propuesta.despues is not None else None
        if valor is None and propuesta.iteraciones:
            valor = propuesta.iteraciones[-1].valor
        if valor is None:
            continue
        referencia = _normalizado(objetivo, humano, objetivo.citas_antes, ficheros)
        fila.campos_iguales, fila.campos_distintos, fila.textos_distintos = comparar(referencia, valor)
        total = fila.campos_iguales + len(fila.campos_distintos)
        fila.acuerdo = round(fila.campos_iguales / total, 4) if total else None
        if fila.acuerdo is not None:
            acuerdos.append(fila.acuerdo)
        if propuesta.estado == "propuesta":
            evaluacion.convergieron += 1
            evaluacion.identicos += not fila.campos_distintos
            if propuesta.aplicable_sin_persona:
                evaluacion.aplicables_sin_persona += 1
                evaluacion.aplicables_sin_persona_con_diferencias += bool(fila.campos_distintos)
    evaluacion.acuerdo_medio = round(sum(acuerdos) / len(acuerdos), 4) if acuerdos else None
    evaluacion.tokens_entrada_aprox = round(evaluacion.caracteres_enviados / CARACTERES_POR_TOKEN)
    if usd_por_millon_de_tokens is not None:
        evaluacion.coste_entrada_aprox_usd = round(
            evaluacion.tokens_entrada_aprox * usd_por_millon_de_tokens / 1_000_000, 4
        )
    return evaluacion


__all__ = ["Evaluacion", "GrupoEvaluado", "aplanar", "comparar", "evaluar", "seleccionar"]
