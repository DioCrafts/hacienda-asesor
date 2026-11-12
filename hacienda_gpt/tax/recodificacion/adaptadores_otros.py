"""Re-encoding adapters for the parameter files that are not the IRPF one.

Four files — self-employed (``autonomos_<year>.json``), corporate tax (``sociedades_<year>.json``), wealth tax and
ITSGF (``patrimonio_<year>.json``) and inheritance and gift tax (``isd_<year>.json``) — share one layout: a *group*
is a JSON object that carries its own ``evidencia`` (literal quotes of the law) next to its figures. This module
maintains every such group with ONE adapter class, :class:`GrupoConEvidencia`, driven by the pydantic models of each
file; a :class:`Familia` says what differs between the files (which model is the root, how a figure must be printed,
which calculator evaluates a test case, when a wording reaches a tax year).

What the gate checks for a proposal, on top of what the loop already does (quotes verbatim in the articles read,
effects date printed in a quote):

* the value validates against the group's model, and the whole file still validates with the group in place;
* every figure of the group is printed in a quote — ``control.cifras_sin_respaldo``, driven by the types of the model;
* the file's own self-check (``comprobar_evidencias`` of its ``*_params`` module) accepts the group, so the periodic
  verification that feeds the loop will not flag tomorrow what was written today;
* nothing the user reads mentions a figure that is in no quote.

Limits that are stated to the operator instead of hidden:

* the loop reads the articles and provisions of ONE law. A group may also rest on quotes of another law, of a
  preamble or of a ministerial order: those quotes are *inherited* untouched, a figure that only they print cannot
  change, and a proposal for such a group is always left to a person;
* a group no calculator turns into an amount has no test cases: it is always left to a person as well.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
import copy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import html
import json
from pathlib import Path
import re
import sys
import types
import typing
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, ValidationError

from hacienda_gpt.tax import (
    autonomos_params as autonomos_mod,
)
from hacienda_gpt.tax import (
    patrimonio_params as patrimonio_mod,
)
from hacienda_gpt.tax import (
    sociedades_params as sociedades_mod,
)
from hacienda_gpt.tax import (
    sucesiones_params as isd_mod,
)
from hacienda_gpt.tax.params import DATA_DIR
from hacienda_gpt.tax.recodificacion import prompts
from hacienda_gpt.tax.recodificacion.adaptadores import Hallazgo
from hacienda_gpt.tax.recodificacion.control import (
    CENT,
    MIN_CITA,
    Control,
    Reglas,
    articulos_de_referencia,
    campos_numericos,
    cifras_sin_respaldo,
    clave_de_disposicion,
    extraer_articulos,
    lineas_de,
    notas_de_cifra_derivada,
    numeros_de_texto_sin_respaldo,
    ruta_de_etiqueta,
)
from hacienda_gpt.tax.recodificacion.nucleo import (
    Adaptador,
    Objetivo,
    Propuesta,
    RecodificacionError,
    ejercicios_en,
    leer_ruta,
    registrar,
)
from hacienda_gpt.tax.verificacion import Comprobacion, Resultado, TextoDe, _comprimido, verificar

MARCA = "Recodificada automáticamente el "
# Appended to ``fuente.referencia`` while nobody has confirmed the group: the calculators print the reference of
# every unverified source they use, so the user reads this in each result that relies on the group.
PENDIENTE = " — recodificación automática pendiente de revisión humana"
# Fields the program writes, never the proposal.
_PROCEDENCIA = ("fuente", "evidencia", "avisos", "recodificada_el")
Estilo = Literal["cifras", "citas"]


def _errores(exc: ValidationError) -> list[str]:
    return [f"modelo: {'.'.join(map(str, e['loc'])) or 'valor'}: {e['msg']}"[:300] for e in exc.errors()[:8]]


# --------------------------------------------------------------------------- #
# What differs between the files
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Calculo:
    """A deterministic calculator a reviewer's test case can be run through."""

    # Lazy: returns (input model, function(input, params) -> result model).
    cargar: Callable[[], tuple[type[BaseModel], Callable[[Any, Any], Any]]]
    que: str


@dataclass(frozen=True)
class Familia:
    """One parameter file: its models, how its figures are printed and which calculators use it."""

    clave: str
    titulo: str
    fichero: str
    modulo: Any
    raiz: type[BaseModel]
    estilo: Estilo
    reglas: Reglas
    # Decimal fields holding a count the law may spell out ("cuatro años", "cinco séptimos"): checked by the file's
    # own self-check, which knows those spellings, instead of by the generic walker.
    cuentas: frozenset[str]
    # Fields whose notation the generic walker does not know (a coefficient "1,0000", a percentage in a table cell
    # with no sign): for them the file's own check, which finds every figure as a whole token, has the last word.
    tolerados: frozenset[str]
    # The file's own self-check: (validated file, path of the only group of interest or None for all) -> problems.
    comprobar: Callable[[Any, Sequence[str] | None], list[str]]
    # Prefix the self-check gives to the problems of the group at a path.
    prefijo: Callable[[Sequence[str]], str]
    calculos: dict[str, Calculo]
    # Names of the group models no calculator turns into an amount.
    sin_evaluador: frozenset[str]
    # "fin": the tax accrues at the end of the year (or on a day within it); "inicio": the file holds what applies
    # to periods STARTING in its year.
    devengo: Literal["fin", "inicio"] = "fin"
    # (literal, dotted path of the figure) -> the value the file's self-check reads from that spelling.
    leer_cifra: Callable[[str, str], Decimal] | None = None
    hojas: Callable[[Any], dict[str, Decimal]] | None = None
    # Quote labels the calculator looks up by name: losing one would break it.
    etiquetas_usadas: dict[str, tuple[str, ...]] = field(default_factory=dict)
    limpiar: Callable[[], None] = lambda: None
    validar_extra: Callable[[Any], None] = lambda params: None

    def modelos(self) -> list[type[BaseModel]]:
        """Every model of the file that is a group (has ``evidencia``), found by walking the root's annotations."""
        encontrados: list[type[BaseModel]] = []
        vistos: set[type] = set()

        def recorrer(modelo: type[BaseModel]) -> None:
            if modelo in vistos:
                return
            vistos.add(modelo)
            if "evidencia" in modelo.model_fields:
                encontrados.append(modelo)
                return
            for info in modelo.model_fields.values():
                for t in _tipos(info.annotation):
                    if isinstance(t, type) and issubclass(t, BaseModel):
                        recorrer(t)

        recorrer(self.raiz)
        return encontrados


def _tipos(anotacion: Any) -> Iterable[Any]:
    origen = typing.get_origin(anotacion)
    if origen is None:
        yield anotacion
    elif origen in (typing.Union, types.UnionType, list, tuple, dict, set, frozenset):
        for arg in typing.get_args(anotacion):
            yield from _tipos(arg)


def rutas_de_grupos(raw: Any, ruta: tuple[str, ...] = ()) -> list[list[str]]:
    """Path of every group of a parameter file: the objects that carry their own ``evidencia``."""
    if isinstance(raw, dict):
        if isinstance(raw.get("evidencia"), list):
            return [list(ruta)]
        return [r for clave, valor in raw.items() for r in rutas_de_grupos(valor, (*ruta, str(clave)))]
    if isinstance(raw, list):
        return [r for i, valor in enumerate(raw) for r in rutas_de_grupos(valor, (*ruta, str(i)))]
    return []


def _en_modelo(params: Any, ruta: Sequence[str]) -> Any:
    """The object of a validated parameter file at ``ruta`` (None when it is not there)."""
    nodo = params
    try:
        for paso in ruta:
            if isinstance(nodo, BaseModel):
                nodo = getattr(nodo, paso)
            elif isinstance(nodo, dict):
                nodo = nodo[paso]
            else:
                nodo = nodo[int(paso)]
    except (AttributeError, KeyError, IndexError, ValueError, TypeError):
        return None
    return nodo


# --- self-employed --------------------------------------------------------- #


def _calculo(
    modulo: str, entrada: str, funcion: str
) -> Callable[[], tuple[type[BaseModel], Callable[[Any, Any], Any]]]:
    def cargar() -> tuple[type[BaseModel], Callable[[Any, Any], Any]]:
        import importlib

        mod = importlib.import_module(f"hacienda_gpt.tax.{modulo}")
        return getattr(mod, entrada), getattr(mod, funcion)

    return cargar


class RetencionInput(BaseModel):
    """Who issues the invoice, for the withholding rate of art. 95.1 RIRPF."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    inicio_actividad: bool = False
    supuesto: (
        Literal["recaudador_municipal", "mediador_seguros", "delegado_loterias", "actividad_ingresos_reducidos"] | None
    ) = None
    ceuta_melilla: bool = False


class ResultadoRetencion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The rate in percentage points (15 % -> 15), so that it survives the rounding of a test case to the cent.
    tipo_porcentaje: Decimal


def _retencion() -> tuple[type[BaseModel], Callable[[Any, Any], Any]]:
    from hacienda_gpt.tax.actividad import tipo_retencion_profesional

    def calcular(inp: RetencionInput, params: Any) -> ResultadoRetencion:
        tipo = tipo_retencion_profesional(
            params, inp.inicio_actividad, supuesto=inp.supuesto, ceuta_melilla=inp.ceuta_melilla
        )
        return ResultadoRetencion(tipo_porcentaje=tipo * 100)

    return RetencionInput, calcular


def _problemas(comprobar: Callable[..., list[str]]) -> Callable[[Any, Sequence[str] | None], list[str]]:
    return lambda params, ruta: comprobar(params, None)


AUTONOMOS = Familia(
    clave="autonomos",
    titulo="Autónomos",
    fichero="autonomos_{ejercicio}.json",
    modulo=autonomos_mod,
    raiz=autonomos_mod.AutonomosParams,
    estilo="cifras",
    reglas=Reglas(
        euros=frozenset(
            {
                "limite_cifra_negocios",
                "limite_anual",
                "limite_base",
                "importe_general",
                "umbral_superior",
                "umbral_inferior",
                "importe_adicional_maximo",
                "pendiente",
                "max_otras_rentas",
                "discapacidad",
                "discapacidad_ampliada",
                "max_prestaciones_trabajo",
                "limite_rentas",
                "importe_maximo",
                "limite_conjunto_reduccion_trabajo",
                "hasta",
                "importe",
                "minoracion_limite",
                "supuesto_d_limite_ingresos",
            }
        ),
        fracciones=frozenset(
            {
                "porcentaje",
                "max_ingresos_antiguo_pagador",
                "max_gastos_sobre_ingresos",
                "min_ingresos_con_retencion",
                "grado_discapacidad",
                "grado_discapacidad_ampliada",
                "exencion_porcentaje_retenido",
                "reduccion_ceuta_melilla",
                "general",
                "inicio_actividad",
                "otros_supuestos",
                "supuesto_d_min_proporcion",
                "tipo_general",
                "tipo_reducido",
                "tipo_superreducido",
            }
        ),
        ignorar=frozenset({*_PROCEDENCIA, "anos_compensacion"}),
    ),
    cuentas=frozenset({"anos_compensacion"}),
    tolerados=frozenset(),
    comprobar=_problemas(autonomos_mod.comprobar_evidencias),
    prefijo=lambda ruta: ruta[0],
    calculos={
        "rendimiento_actividad": Calculo(
            _calculo("actividad", "ActividadInput", "calcular_rendimiento_actividad"),
            "net income of the activity for the year (estimación directa simplificada): hard-to-justify expenses and "
            "the reductions of art. 32 LIRPF",
        ),
        "modelo_130": Calculo(
            _calculo("pagos_fraccionados", "Modelo130Input", "calcular_modelo_130"),
            "quarterly payment on account (modelo 130)",
        ),
        "modelo_303": Calculo(_calculo("iva", "Modelo303Input", "calcular_modelo_303"), "quarterly VAT return"),
        "retencion_profesional": Calculo(_retencion, "withholding rate a professional puts on an invoice"),
    },
    sin_evaluador=frozenset({"EstimacionDirectaSimplificada"}),
    leer_cifra=lambda literal, ruta: autonomos_mod.cifra_a_decimal(literal),
    hojas=autonomos_mod._hojas,
    limpiar=autonomos_mod.load_autonomos_params.cache_clear,
)


# --- corporate tax --------------------------------------------------------- #


def _sociedades_coherente(params: Any) -> None:
    if {str(year) for year in params.ejercicios_cubiertos} != set(params.tipos_gravamen.por_ano):
        raise ValueError("ejercicios_cubiertos no coincide con tipos_gravamen.por_ano")


SOCIEDADES = Familia(
    clave="sociedades",
    titulo="Impuesto sobre Sociedades",
    fichero="sociedades_{ejercicio}.json",
    modulo=sociedades_mod,
    raiz=sociedades_mod.SociedadesParams,
    estilo="cifras",
    reglas=Reglas(
        euros=frozenset(
            {
                "limite_microempresa",
                "limite_primer_tramo",
                "dias_ano",
                "limite_reducida_dimension",
                "limite_cifra_negocios_micro",
                "minimo_compensable",
                "umbral_gran_empresa",
                "umbral_gran_empresa_superior",
                "limite",
                "limite_modalidad_base_obligatoria",
                "umbral_especialidades",
                "umbral_cifra_negocios",
            }
        ),
        fracciones=frozenset(
            {
                "general",
                "nueva_creacion",
                "micro_primer_tramo",
                "micro_resto",
                "reducida_dimension",
                "porcentaje",
                "plantilla_umbral_minimo",
                "plantilla_umbral_medio",
                "plantilla_umbral_alto",
                "porcentaje_plantilla_minimo",
                "porcentaje_plantilla_medio",
                "porcentaje_plantilla_alto",
                "limite_base",
                "limite_base_micro",
                "limite_porcentaje",
                "limite_gran_empresa",
                "limite_gran_empresa_superior",
                "porcentaje_cuota",
                "incremento_incumplimiento",
                "retencion_capital_mobiliario",
            }
        ),
        ignorar=frozenset(
            {
                *_PROCEDENCIA,
                "anos_adicion",
                "anos_mantenimiento",
                "anos_pendientes",
                "base_numerador",
                "base_denominador",
            }
        ),
    ),
    cuentas=frozenset({"anos_adicion", "anos_mantenimiento", "anos_pendientes", "base_numerador", "base_denominador"}),
    tolerados=frozenset(),
    comprobar=_problemas(sociedades_mod.comprobar_evidencias),
    prefijo=lambda ruta: ruta[0],
    calculos={
        "impuesto_sociedades": Calculo(
            _calculo("sociedades", "SociedadesInput", "calcular_sociedades"),
            "corporate tax of a period: reductions of the base, rate by kind of company, quota",
        ),
        "pago_fraccionado": Calculo(
            _calculo("sociedades", "PagoFraccionadoSociedadesInput", "calcular_pago_fraccionado_sociedades"),
            "instalment payment (modelo 202)",
        ),
    },
    sin_evaluador=frozenset({"ComparacionIRPF", "TributacionMinima"}),
    devengo="inicio",
    leer_cifra=lambda literal, ruta: sociedades_mod.cifra_a_decimal(literal),
    hojas=sociedades_mod._hojas,
    etiquetas_usadas={
        "tipos_gravamen": ("art. 29.1 LIS (cuándo no hay actividad nueva)",),
        "comparacion_irpf": ("art. 27.1 LIRPF",),
    },
    limpiar=sociedades_mod._load.cache_clear,
    validar_extra=_sociedades_coherente,
)


# --- wealth tax ------------------------------------------------------------ #


def _patrimonio() -> tuple[type[BaseModel], Callable[[Any, Any], Any]]:
    from hacienda_gpt.tax.patrimonio import PatrimonioInput, calcular_patrimonio

    return PatrimonioInput, calcular_patrimonio


PATRIMONIO = Familia(
    clave="patrimonio",
    titulo="Impuesto sobre el Patrimonio",
    fichero="patrimonio_{ejercicio}.json",
    modulo=patrimonio_mod,
    raiz=patrimonio_mod.PatrimonioParams,
    estilo="cifras",
    reglas=Reglas(
        euros=frozenset(
            {
                "limite_exento",
                "importe",
                "hasta",
                "cuota_hasta_inicio",
                "umbral_bienes_derechos",
                "umbral_patrimonio_neto",
                "umbral_base_itsgf",
                "minimo_descontado",
            }
        ),
        fracciones=frozenset({"porcentaje", "porcentaje_base_irpf", "reduccion_maxima", "grado_desde"}),
        porcentajes_de_tabla=frozenset({"tipo"}),
        ignorar=frozenset(_PROCEDENCIA),
    ),
    cuentas=frozenset(),
    tolerados=frozenset({"grado_desde"}),
    comprobar=_problemas(patrimonio_mod.comprobar_evidencias),
    prefijo=lambda ruta: ".".join(ruta),
    calculos={
        "patrimonio": Calculo(
            _patrimonio,
            "wealth tax and ITSGF of one taxpayer in a community (`comunidad` defaults to the group's community)",
        )
    },
    sin_evaluador=frozenset({"VigenciaITSGF", "_Grupo"}),
    leer_cifra=patrimonio_mod.cifra_a_decimal,
    hojas=patrimonio_mod._hojas,
    limpiar=patrimonio_mod.load_patrimonio_params.cache_clear,
)


# --- inheritance and gift tax ---------------------------------------------- #


def _isd() -> tuple[type[BaseModel], Callable[[Any, Any], Any]]:
    from hacienda_gpt.tax.sucesiones import BeneficiarioISD, calcular_isd

    return BeneficiarioISD, calcular_isd


def _problemas_isd(params: Any, ruta: Sequence[str] | None) -> list[str]:
    """The ISD self-check; for one group, only over the rules of its legislator (the whole file takes far longer)."""
    if ruta is None:
        return isd_mod.comprobar_evidencias(params)
    nombre = "estatal" if ruta[0] == "estatal" else ruta[1]
    fuentes = set(params.fuentes_principales) | set(params.fuentes_complementarias)
    return isd_mod.comprobar_normativa(nombre, params.normativas()[nombre], None, fuentes)


def _prefijo_isd(ruta: Sequence[str]) -> str:
    resto = ruta[1:] if ruta[0] == "estatal" else ruta[2:]
    return f"{ruta[0] if ruta[0] == 'estatal' else ruta[1]}.{'.'.join(resto)} ("


ISD = Familia(
    clave="isd",
    titulo="Impuesto sobre Sucesiones y Donaciones",
    fichero="isd_{ejercicio}.json",
    modulo=isd_mod,
    raiz=isd_mod.ISDParams,
    estilo="citas",
    reglas=Reglas(
        euros=frozenset(
            {
                "base_imponible_hasta",
                "base_imponible_menor_de",
                "base_liquidable_hasta",
                "base_liquidable_menor_de",
                "patrimonio_preexistente_hasta",
                "patrimonio_preexistente_menor_de",
                "patrimonio_hasta",
                "importe",
                "importe_maximo",
                "incremento_por_ano",
                "limite",
                "limite_conjunto",
                "limite_individual_minimo",
                "limite_por_sujeto",
                "maximo",
                "valor_hasta",
                "hasta",
                "cuota_hasta_inicio",
                "coeficientes",
            }
        ),
        fracciones=frozenset({"porcentaje", "porcentaje_base"}),
        porcentajes_de_tabla=frozenset({"tipo"}),
        grados=frozenset({"discapacidad_desde", "discapacidad_menor_de"}),
        ignorar=frozenset({*_PROCEDENCIA, "en_vigor_desde"}),
    ),
    cuentas=frozenset(),
    tolerados=frozenset(
        {
            "coeficientes",
            "porcentaje",
            "porcentaje_base",
            "hasta",
            "cuota_hasta_inicio",
            "valor_hasta",
            "patrimonio_hasta",
            "edad_desde",
            "edad_hasta",
            "edad_menor_de",
            "edad_mayor_de",
            "edad_referencia",
            "anos_mantenimiento",
            "colateral_mayor_de",
            "colateral_anos_convivencia",
            "discapacidad_desde",
            "discapacidad_menor_de",
        }
    ),
    comprobar=_problemas_isd,
    prefijo=_prefijo_isd,
    calculos={
        "isd": Calculo(
            _isd,
            "inheritance or gift tax of one beneficiary (`comunidad` defaults to the group's community; give "
            "`parentesco` or `grupo`, `edad`, `valor_recibido`…)",
        )
    },
    sin_evaluador=frozenset({"_Regla"}),
    limpiar=isd_mod.load_isd_params.cache_clear,
)

FAMILIAS: tuple[Familia, ...] = (AUTONOMOS, SOCIEDADES, PATRIMONIO, ISD)


def familia_de(clave: str) -> Familia:
    try:
        return next(f for f in FAMILIAS if f.clave == clave)
    except StopIteration:
        raise RecodificacionError(f"No hay ninguna familia de parámetros «{clave}».") from None


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

_NO_ARTICULO = re.compile(r"\s*(?:anexo|pre[aá]mbulo|modelo|nota)", re.IGNORECASE)
_MILLONES = re.compile(r"(?<![\d.,])(\d{1,3})\s+mill(?:ón|ones)\b")


_DISPOSICION_EN_LETRAS = re.compile(
    r"\s*disp(?:osici[oó]n|\.)\s+(adicional|transitoria|derogatoria|final)\s+([a-záéíóúñ]+(?:\s+[a-záéíóúñ]+)?)",
    re.IGNORECASE,
)


def bloques_de(etiqueta: str) -> list[str]:
    """Blocks of its law a quote labelled ``etiqueta`` is read from, as ``control.extraer_articulos`` names them.

    "art. 4.Nueve Ley 19/1991" -> ["4.nueve"] (the apartado matters only in a very long article), "disposición
    transitoria 44.ª.1 LIS" -> ["dt 44"]; an annex, a preamble or a form -> [] (nothing the loop can re-read).
    """
    if _NO_ARTICULO.match(etiqueta):
        return []
    claves = articulos_de_referencia(etiqueta)
    if not claves:
        # "disp. adicional primera DLeg 1/2005": the ordinal in words, as the BOE heads the provision.
        en_letras = _DISPOSICION_EN_LETRAS.match(etiqueta)
        for ordinal in (en_letras.group(2), en_letras.group(2).split()[0]) if en_letras else ():
            clave = clave_de_disposicion(f"Disposición {en_letras.group(1).lower()} {ordinal}.")  # type: ignore[union-attr]
            if clave is not None:
                return [clave]
    ruta = ruta_de_etiqueta(etiqueta)
    if len(claves) == 1 and ruta is not None and ruta.split(".")[0] == claves[0]:
        return [ruta]
    return claves


def es_legible(etiqueta: str) -> bool:
    """Is a quote labelled ``etiqueta`` read from an article or a provision (what the loop can re-read)?"""
    return bool(bloques_de(etiqueta))


_MILES_CON_ESPACIO = re.compile(r"(?<![\d.,])\d{1,3}(?: \d{3})+(?![\d.,])")
_NORMA_CITADA = re.compile(
    r"\b\d+/(?:19|20)\d{2}\b|\b(?:arts?\.|art[ií]culos?)\s*\d+(?:-\d+)?(?:\s*(?:,|y|a)\s*\d+(?:-\d+)?)*"
)


def _evidencia_normalizada(citas: Iterable[str]) -> str:
    """The quotes as one text, plus two readings added at the end: "35 000" as "35.000" and "1 millón" as "1.000.000".

    ``control.normalizar_evidencia`` is not used: it joins ANY digit, space and three digits, which glues the cells
    of a table quoted in reading order ("0,00 167.129,45" becomes "0,00.167.129,45") and hides its figures.
    """
    texto = unicodedata.normalize("NFKC", " ".join(citas))
    extras = [m.group(0).replace(" ", ".") for m in _MILES_CON_ESPACIO.finditer(texto)]
    extras += [f"{m.group(1)}.000.000" for m in _MILLONES.finditer(texto)]
    return texto + (" | " + " | ".join(extras) if extras else "")


def _sin_normas_citadas(texto: str) -> str:
    """A user-facing sentence without the numbers that only name a norm or its articles ("Ley 22/2009", "arts. 131-5 y 132-2")."""
    return _NORMA_CITADA.sub("", texto)


def _parecida(cita: str, candidatas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The previous quote ``cita`` is a new wording of: the same passage, or one that reads almost the same."""
    propia = _comprimido(cita)
    mejor: tuple[float, dict[str, Any] | None] = (0.0, None)
    for previa in candidatas:
        otra = _comprimido(str(previa.get("cita", "")))
        if otra == propia:
            return previa
        parecido = 1.0 if (propia in otra or otra in propia) else SequenceMatcher(None, propia, otra).ratio()
        if parecido > mejor[0]:
            mejor = (parecido, previa)
    return mejor[1] if mejor[0] >= 0.6 else None


def _literales(cita: str) -> Iterable[str]:
    """Candidate spellings of a figure inside a quote: every run of one to four words, shortest first."""
    palabras = cita.split()
    for ancho in (1, 2, 3, 4):
        for i in range(len(palabras) - ancho + 1):
            candidato = " ".join(palabras[i : i + ancho]).strip(".,;:()«»\"'")
            if candidato:
                yield candidato


def _es_token(literal: str, cita: str) -> bool:
    return re.search(rf"(?<![\w.,]){re.escape(literal)}(?!\w|[.,]\d)", cita) is not None


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #


class Candidato(BaseModel):
    """A proposal that validated: the group, and the whole file with the group in place (for the test cases)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    grupo: BaseModel
    params: BaseModel
    ejercicio: int
    valor: Any


class GrupoConEvidencia(Adaptador):
    """One kind of group (one pydantic model) of one of the four files."""

    permite_retirada = False

    def __init__(self, familia: Familia, modelo: type[BaseModel]) -> None:
        self.familia = familia
        self.modelo = modelo
        # "<family>:<model>": ``--tipo sociedades`` asks for the whole family, as with the IRPF's "cifras:…" kinds.
        self.tipo = f"{familia.clave}:{modelo.__name__.lstrip('_')}"
        self.fichero = familia.fichero
        self.evaluable = modelo.__name__ not in familia.sin_evaluador
        self.min_casos = 2 if self.evaluable else 0
        self.que = (
            f"one group of legal parameters of the Spanish {familia.titulo} calculator: the pydantic model "
            f"`{modelo.__name__}` (the task names the group and the article it is read from)"
        )

    # -- prompts ---------------------------------------------------------- #

    def _anidados(self) -> list[type[BaseModel]]:
        vistos: list[type[BaseModel]] = []

        def recorrer(modelo: type[BaseModel]) -> None:
            if modelo in vistos or modelo.__name__ in ("Fuente", "Evidencia", "Cita"):
                return
            vistos.append(modelo)
            for base in modelo.__mro__[1:]:
                if isinstance(base, type) and issubclass(base, BaseModel) and base.__name__.startswith("_"):
                    if base.__name__ != "_Model" and base not in vistos:
                        vistos.append(base)
            for info in modelo.model_fields.values():
                for t in _tipos(info.annotation):
                    if isinstance(t, type) and issubclass(t, BaseModel):
                        recorrer(t)

        recorrer(self.modelo)
        return vistos

    def esquema(self) -> str:
        return prompts.fuente_de(list(reversed(self._anidados())), modulo=self.familia.modulo)

    def guia(self) -> str:
        programa = ", ".join(f"`{campo}`" for campo in _PROCEDENCIA if campo in self.modelo.model_fields)
        lineas = [
            f"How a `{self.modelo.__name__}` group is encoded (the schema below is the authority):",
            f"- `valor_json` is the object WITHOUT {programa}"
            + (" and without the `fuente` of a nested scale" if self._con_fuente_anidada() else "")
            + ": the program writes those. Keep every other field of the previous encoding that the text still "
            "supports; change only what the text changed.",
            '- Fractions for percentages (15 % -> "0.15"; a table cell "0,21" of a rate column -> "0.0021"), '
            "amounts in euros as the law prints them, without rounding.",
            "- Quotes: repeat VERBATIM every passage the previous encoding relied on that is still in the text and "
            "still supports the group (they are listed under «Citas en las que se apoyaba»), including those that "
            "carry no figure; a previous quote of this law you do not repeat is dropped. Add the passages that print "
            "any figure you change. A table is quoted row by row or as one passage, cells in reading order.",
            "- The previous encoding may also rest on quotes of OTHER laws or of annexes that are not shown to "
            "you: they are kept as they are, and a figure that only they support must keep its value.",
        ]
        if "tramos" in self.modelo.model_fields or self._con_fuente_anidada():
            lineas.append(
                "- A scale: one `tramos` item per printed row, in order. `hasta` is where the bracket ENDS (the base "
                "printed on the next row; null on the last), `cuota_hasta_inicio` the cumulative tax printed on the "
                "row, `tipo` the row's percentage as a fraction."
            )
        if "articulo" in self.modelo.model_fields:
            lineas.append(
                "- Keep `articulo`"
                + (" and `clave`" if "clave" in self.modelo.model_fields else "")
                + " exactly as given. `en_vigor_desde` is set by the program from the effects date: give any date. "
                "`resumen` (and `titulo`) in Spanish, with only figures that are in your quotes. Legal conditions "
                "the schema cannot express go, in Spanish, in `requisitos_no_comprobados`: never drop one silently."
            )
        lineas.append(
            "- If the text no longer fits the schema (a new condition with no field, a figure that became a "
            'formula), answer "no_calculable": a person will look at it.'
        )
        return "\n".join(lineas) + "\n"

    def guia_casos(self, objetivo: Objetivo) -> str:
        if not self.evaluable:
            return (
                "No calculator turns this group into an amount: return `casos` as an empty list and put your whole "
                "effort into `discrepancias`. A person confirms this group in any case."
            )
        partes = [
            '`datos_json` of a case: {"calculo": "<name>", "entrada": {<inputs>}, "resultado": "<result line>"}. '
            "`importe_esperado` is the value THE LAW gives for that result line (euros with a dot; a yes/no line is "
            "1 or 0). `ejercicio` is filled in by the program"
            + ("" if objetivo.ambito == "estado" else f" and `comunidad` defaults to «{objetivo.ambito}»")
            + ". Choose inputs that make the figures of THIS group decide the result: exactly at each threshold, "
            "just below and just above it, each bracket, each ceiling being reached.",
            "Calculators available (`calculo`), their inputs and their result lines:",
        ]
        for nombre, calculo in self.familia.calculos.items():
            entrada, _ = calculo.cargar()
            partes.append(
                f"- `{nombre}` — {calculo.que}. Inputs: {', '.join(c for c in entrada.model_fields if c != 'ejercicio')}."
            )
        partes.append(
            "A result line is a numeric field of the calculator's result (for example `cuota_integra`, "
            "`cuota_a_ingresar`, `gastos_dificil_justificacion`, `a_ingresar`, `itsgf.cuota_a_ingresar`) or "
            '"linea:<concepto>" for a line of its trace.'
        )
        return "\n".join(partes)

    # -- composing the group --------------------------------------------- #

    def _con_fuente_anidada(self) -> list[str]:
        """Fields holding a nested model that carries its own ``fuente`` (the scale of an ISD tariff rule)."""
        return [
            nombre
            for nombre, info in self.modelo.model_fields.items()
            if nombre not in _PROCEDENCIA
            and any(
                isinstance(t, type) and issubclass(t, BaseModel) and "fuente" in t.model_fields
                for t in _tipos(info.annotation)
            )
        ]

    def _sin_procedencia(self, grupo: Any) -> Any:
        """The group's JSON as a proposal carries it: no source, no quotes, no marks."""
        if not isinstance(grupo, dict):
            return grupo
        valor = {k: copy.deepcopy(v) for k, v in grupo.items() if k not in _PROCEDENCIA}
        for nombre in self._con_fuente_anidada():
            if isinstance(valor.get(nombre), dict):
                valor[nombre].pop("fuente", None)
        return valor

    def _fuente(self, previa: Any, *, efectos: str, version: str | None, hoy: date | None) -> dict[str, Any]:
        previa = previa if isinstance(previa, dict) else {}
        if hoy is None:
            # Only to validate a candidate: provenance is written by ``json_final``.
            return dict(previa) or {"referencia": "propuesta", "en_vigor_desde": efectos, "verificado": False}
        referencia = str(previa.get("referencia") or "").replace(PENDIENTE, "")
        fuente: dict[str, Any] = {
            "referencia": (referencia or self.tipo) + PENDIENTE,
            "en_vigor_desde": efectos,
            "verificado": False,
            "nota": f"{MARCA}{hoy.isoformat()} a partir del BOE (efectos desde {efectos}); pendiente de revisión humana.",
        }
        if previa.get("condicionada_a"):
            fuente["condicionada_a"] = previa["condicionada_a"]
        if version is not None:
            fuente["version_historica"] = version
        return fuente

    def _evidencia(
        self, citas: Sequence[str], anterior: Any, boe_id: str, articulos: Sequence[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """(quotes re-read now, quotes inherited untouched, labels of the previous quotes that are dropped)."""
        previas = [dict(e) for e in (anterior or {}).get("evidencia", []) if isinstance(e, dict)]
        heredadas = [e for e in previas if e.get("boe_id") != boe_id or not es_legible(str(e.get("articulo", "")))]
        libres = [e for e in previas if e not in heredadas]
        generica = (
            f"art. {articulos[0]} ({boe_id})" if len(articulos) == 1 else f"arts. {', '.join(articulos)} ({boe_id})"
        )
        nuevas: list[dict[str, Any]] = []
        for cita in citas:
            previa = _parecida(cita, libres)
            if previa is not None:
                libres.remove(previa)
            entrada: dict[str, Any] = {
                "boe_id": boe_id,
                "articulo": previa["articulo"] if previa is not None else generica,
                "cita": cita,
            }
            if previa is not None and previa.get("cifras") and _comprimido(previa["cita"]) == _comprimido(cita):
                entrada["cifras"] = dict(previa["cifras"])
            nuevas.append(entrada)
        return nuevas, heredadas, [str(e.get("articulo", "")) for e in libres]

    def _cifras(self, grupo: BaseModel, evidencia: list[dict[str, Any]]) -> list[str]:
        """Fill the ``cifras`` map of each quote the way the file's self-check reads it; returns what has no spelling."""
        familia = self.familia
        assert familia.hojas is not None and familia.leer_cifra is not None
        hojas = familia.hojas(grupo)

        def vale(literal: str, ruta: str, cita: str) -> bool:
            try:
                return _es_token(literal, cita) and familia.leer_cifra(literal, ruta) == hojas[ruta]  # type: ignore[misc]
            except (InvalidOperation, IndexError, ValueError, KeyError, ArithmeticError):
                return False

        cubiertas: set[str] = set()
        for entrada in evidencia:
            buenas = {
                ruta: literal
                for ruta, literal in (entrada.get("cifras") or {}).items()
                if ruta in hojas and vale(str(literal), ruta, entrada["cita"])
            }
            entrada.pop("cifras", None)
            if buenas:
                entrada["cifras"] = buenas
                cubiertas |= set(buenas)
        sin_respaldo: list[str] = []
        for ruta in hojas:
            if ruta in cubiertas:
                continue
            for entrada in evidencia:
                literal = next((c for c in _literales(entrada["cita"]) if vale(c, ruta, entrada["cita"])), None)
                if literal is not None:
                    entrada.setdefault("cifras", {})[ruta] = literal
                    break
            else:
                sin_respaldo.append(ruta)
        return sin_respaldo

    def componer(
        self,
        valor: dict[str, Any],
        citas: Sequence[str],
        anterior: Any,
        *,
        ruta: Sequence[str],
        boe_id: str,
        articulos: Sequence[str],
        efectos: str | None,
        version: str | None = None,
        hoy: date | None = None,
    ) -> tuple[dict[str, Any], Control, list[dict[str, Any]], list[dict[str, Any]]]:
        """The group as it would be written: the proposed value plus its source and quotes.

        Returns (group JSON, problems found while composing it, quotes re-read, quotes inherited). With ``hoy`` the
        source carries the mark "re-encoded, pending human review"; without it the previous source is kept, which
        is all a validation needs.
        """
        control = Control()
        previo = anterior if isinstance(anterior, dict) else {}
        grupo: dict[str, Any] = {k: copy.deepcopy(v) for k, v in valor.items() if k not in _PROCEDENCIA}
        campos = self.modelo.model_fields
        efectos_o_previo = efectos or str(previo.get("fuente", {}).get("en_vigor_desde") or "")
        if "en_vigor_desde" in campos and efectos:
            # Dated by the quote-backed effects date, whatever the proposal says.
            grupo["en_vigor_desde"] = efectos
        if "fuente" in campos:
            grupo["fuente"] = self._fuente(previo.get("fuente"), efectos=efectos_o_previo, version=version, hoy=hoy)
        for nombre in self._con_fuente_anidada():
            if isinstance(grupo.get(nombre), dict):
                previa = previo.get(nombre, {}).get("fuente") if isinstance(previo.get(nombre), dict) else None
                fecha = efectos or str((previa or {}).get("en_vigor_desde") or grupo.get("en_vigor_desde") or "")
                fuente = self._fuente(previa, efectos=fecha, version=version, hoy=hoy)
                if hoy is not None:
                    fuente["referencia"] = fuente["referencia"].replace(PENDIENTE, "")
                grupo[nombre]["fuente"] = fuente
        if "avisos" in campos and "avisos" in previo:
            grupo["avisos"] = copy.deepcopy(previo["avisos"])
        if "recodificada_el" in campos and hoy is not None:
            grupo["recodificada_el"] = hoy.isoformat()
        nuevas, heredadas, perdidas = self._evidencia(citas, anterior, boe_id, articulos)
        primero_propio = not previo.get("evidencia") or previo["evidencia"][0] not in heredadas
        evidencia = [*nuevas, *heredadas] if primero_propio else [*heredadas, *nuevas]
        if self.familia.estilo == "citas":
            evidencia = [{k: e[k] for k in ("boe_id", "articulo", "cita")} for e in evidencia]
        grupo["evidencia"] = evidencia
        usadas = self.familia.etiquetas_usadas.get("/".join(ruta), ())
        control.fallos += [
            f"la cita «{etiqueta}» la usa la calculadora por su nombre y ha desaparecido: cítela de nuevo tal como "
            "la imprime hoy la ley"
            for etiqueta in usadas
            if etiqueta in perdidas
        ]
        if not evidencia:
            control.fallos.append("el grupo se queda sin ninguna cita")
            return grupo, control, nuevas, heredadas
        if self.familia.estilo == "cifras":
            try:
                modelo = self.modelo.model_validate(grupo)
            except ValidationError as exc:
                control.fallos += _errores(exc)
                return grupo, control, nuevas, heredadas
            # A figure left without a spelling is reported by the file's own check, run on the whole candidate.
            self._cifras(modelo, evidencia)
        return grupo, control, nuevas, heredadas

    # -- what the generic commands ask an adapter about itself ------------- #

    def grupos(self, raw: dict[str, Any]) -> list[list[str]]:
        rutas = rutas_de_grupos(raw)
        if not rutas:
            return []
        try:
            params = self.familia.raiz.model_validate(raw)
        except ValidationError:
            return []
        return [ruta for ruta in rutas if type(_en_modelo(params, ruta)) is self.modelo]

    def objetivo(
        self,
        ruta: list[str],
        ficheros: dict[int, dict[str, Any]],
        *,
        data_dir: Path,
        origen: str,
        motivo: Sequence[str] = (),
        version: str | None = None,
        anotaciones: str = "",
    ) -> Objetivo:
        return construir_objetivo(
            self.familia, ruta, ficheros, origen=origen, motivo=motivo, version=version, anotaciones=anotaciones
        )

    # -- gate ------------------------------------------------------------- #

    def _fichero_de(self, ejercicio: int, datos: dict[int, Any]) -> dict[str, Any] | None:
        propio = datos.get(ejercicio)
        if isinstance(propio, dict):
            return propio
        path = DATA_DIR / self.fichero.format(ejercicio=ejercicio)
        leido: dict[str, Any] | None = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        return leido

    def controlar(
        self, valor: Any, citas: list[str], objetivo: Objetivo, *, efectos_desde: date | None, datos: dict[int, Any]
    ) -> tuple[BaseModel | None, Control]:
        control = Control()
        if not isinstance(valor, dict):
            control.fallos.append("el valor no es un objeto JSON")
            return None, control
        control.fallos += [
            f"«{campo}» lo pone el programa, no la propuesta" for campo in _PROCEDENCIA if valor.get(campo) is not None
        ]
        control.fallos += [
            f"«{nombre}.fuente» lo pone el programa, no la propuesta"
            for nombre in self._con_fuente_anidada()
            if isinstance(valor.get(nombre), dict) and valor[nombre].get("fuente") is not None
        ]
        if control.fallos:
            return None, control
        ejercicios = sorted(e for e in objetivo.ejercicios if objetivo.antes.get(e) is not None)
        if efectos_desde is not None:
            ejercicios = self.ejercicios_afectados(efectos_desde, None, ejercicios)[0]
        if not ejercicios:
            # No year to check against: the loop reports that no year is reached; nothing to validate here.
            ejercicios = sorted(e for e in objetivo.ejercicios if objetivo.antes.get(e) is not None)[-1:]
        candidato: Candidato | None = None
        for ejercicio in ejercicios:
            parcial, modelo = self._controlar_ejercicio(valor, citas, objetivo, ejercicio, efectos_desde, datos)
            prefijo = f"{ejercicio}: " if len(ejercicios) > 1 else ""
            control.fallos += [prefijo + f for f in parcial.fallos if prefijo + f not in control.fallos]
            control.revisar += [prefijo + r for r in parcial.revisar if prefijo + r not in control.revisar]
            candidato = modelo if modelo is not None else candidato
        if not self.evaluable:
            control.revisar.append(
                "ninguna calculadora convierte este grupo en un importe: no hay casos de prueba que lo contrasten y "
                "debe confirmarlo una persona"
            )
        return candidato, control

    def _controlar_ejercicio(
        self,
        valor: dict[str, Any],
        citas: list[str],
        objetivo: Objetivo,
        ejercicio: int,
        efectos_desde: date | None,
        datos: dict[int, Any],
    ) -> tuple[Control, Candidato | None]:
        familia = self.familia
        anterior = objetivo.antes.get(ejercicio)
        grupo, control, nuevas, heredadas = self.componer(
            valor,
            citas,
            anterior,
            ruta=objetivo.ruta,
            boe_id=objetivo.boe_id,
            articulos=objetivo.articulos_ley,
            efectos=efectos_desde.isoformat() if efectos_desde else None,
            version=objetivo.version,
        )
        if control.fallos:
            return control, None
        if isinstance(anterior, dict):
            control.fallos += [
                f"«{campo}» debe seguir siendo exactamente «{anterior[campo]}»"
                for campo in ("articulo", "clave")
                if campo in anterior and grupo.get(campo) != anterior[campo]
            ]
        raw = self._fichero_de(ejercicio, datos)
        if raw is None:
            control.fallos.append(f"no existe {self.fichero.format(ejercicio=ejercicio)}")
            return control, None
        raw = copy.deepcopy(raw)
        padre = leer_ruta(raw, objetivo.ruta[:-1])
        if leer_ruta(raw, objetivo.ruta) is None or padre is None:
            control.fallos.append(f"el grupo {'/'.join(objetivo.ruta)} no está en el fichero de {ejercicio}")
            return control, None
        padre[int(objetivo.ruta[-1]) if isinstance(padre, list) else objetivo.ruta[-1]] = grupo
        try:
            params = familia.raiz.model_validate(raw)
            familia.validar_extra(params)
        except ValidationError as exc:
            control.fallos += _errores(exc)
            return control, None
        except ValueError as exc:
            control.fallos.append(str(exc))
            return control, None
        modelo = _en_modelo(params, objetivo.ruta)
        if type(modelo) is not self.modelo:
            control.fallos.append(f"en {'/'.join(objetivo.ruta)} no hay un grupo «{self.modelo.__name__}»")
            return control, None

        # 1. Every figure inside a quote: the type-driven walker, over the quotes re-read now and over all of them.
        del_fichero = [
            p for p in familia.comprobar(params, objetivo.ruta) if _es_del_grupo(p, familia.prefijo(objetivo.ruta))
        ]
        todas = _evidencia_normalizada(e["cita"] for e in [*nuevas, *heredadas])
        solo_nuevas = _evidencia_normalizada(e["cita"] for e in nuevas)
        con_todas = cifras_sin_respaldo(modelo, todas, familia.reglas)
        con_nuevas = cifras_sin_respaldo(modelo, solo_nuevas, familia.reglas) if heredadas else con_todas
        numericos = campos_numericos(self.modelo)

        def tolerado(nota: str) -> bool:
            # A notation the generic walker does not know (a coefficient "1,0000", a percentage in a table cell
            # with no sign) and the file's own check, which matches whole tokens, does find.
            return _campo_de(nota, numericos) in familia.tolerados and not del_fichero

        control.fallos += [fallo for fallo in con_todas.fallos if not tolerado(fallo)]
        control.revisar += [nota for nota in con_todas.revisar if not tolerado(nota)]
        control.revisar += [
            f"{fallo.split(':')[0]}: la cifra solo figura en una cita heredada que no se ha releído"
            for fallo in con_nuevas.fallos
            if fallo not in con_todas.fallos and not tolerado(fallo)
        ]
        if heredadas:
            etiquetas = "; ".join(dict.fromkeys(f"{e['articulo']} [{e['boe_id']}]" for e in heredadas))
            control.revisar.append(
                f"el grupo se apoya también en {len(heredadas)} cita(s) de otra norma, de un anexo o de un preámbulo que este "
                f"bucle no relee ({etiquetas[:400]}): confirme que siguen vigentes"
            )
        # 2. The file's own self-check must accept the group as it would be written.
        control.fallos += [f"autocontrol del fichero: {p}" for p in del_fichero]
        # 3. Nothing the user reads carries a figure no quote prints.
        visibles = [str(grupo[c]) for c in ("resumen", "titulo") if isinstance(grupo.get(c), str)]
        for texto in visibles:
            control.fallos += [
                f"«{texto[:50]}…» menciona {numero}, que no está en ninguna cita"
                for numero in numeros_de_texto_sin_respaldo(_sin_normas_citadas(texto), todas)
            ]
        requisitos = [r for r in grupo.get("requisitos_no_comprobados", []) if isinstance(r, str)]
        for requisito in requisitos:
            control.revisar += [
                f"el requisito «{requisito[:60]}…» menciona {numero}, que no está en ninguna cita"
                for numero in numeros_de_texto_sin_respaldo(_sin_normas_citadas(requisito), todas)
            ]
        control.fallos += [
            f"nota de cifra derivada o aproximada: «{nota}»"
            for nota in notas_de_cifra_derivada([*visibles, *requisitos])
        ]
        candidato = Candidato(grupo=modelo, params=params, ejercicio=ejercicio, valor=self._sin_procedencia(grupo))
        return control, candidato

    def normalizar(self, modelo: BaseModel) -> Any:
        assert isinstance(modelo, Candidato)
        return modelo.valor

    # -- test cases ------------------------------------------------------- #

    def evaluar_caso(self, modelo: BaseModel, datos: Any, objetivo: Objetivo) -> tuple[Decimal, str | None]:
        assert isinstance(modelo, Candidato)
        if not self.evaluable:
            raise ValueError("ninguna calculadora evalúa este grupo")
        if not isinstance(datos, dict) or not isinstance(datos.get("entrada"), dict):
            raise ValueError("datos_json debe ser {«calculo», «entrada», «resultado»}")
        ajenos = sorted(set(datos) - {"calculo", "entrada", "resultado"})
        if ajenos:
            raise ValueError(f"claves desconocidas: {', '.join(ajenos)}")
        calculos = self.familia.calculos
        nombre = datos.get("calculo") or (next(iter(calculos)) if len(calculos) == 1 else None)
        if nombre not in calculos:
            raise ValueError(f"«calculo» debe ser uno de: {', '.join(calculos)}")
        entrada_modelo, funcion = calculos[nombre].cargar()
        entrada = dict(datos["entrada"])
        entrada["ejercicio"] = modelo.ejercicio
        if "comunidad" in entrada_modelo.model_fields and "comunidad" not in entrada and objetivo.ambito != "estado":
            entrada["comunidad"] = objetivo.ambito
        try:
            resultado = funcion(entrada_modelo.model_validate(entrada), modelo.params)
        except ValidationError as exc:
            raise ValueError("; ".join(_errores(exc))) from exc
        except Exception as exc:  # a case the engine cannot run is the case's problem, reported as such
            raise ValueError(f"la calculadora no pudo evaluarlo ({type(exc).__name__}: {exc})") from exc
        return _linea(resultado, str(datos.get("resultado") or "")), None

    # -- years ------------------------------------------------------------ #

    def ejercicios_afectados(
        self, desde: date, hasta: date | None, candidatos: Iterable[int]
    ) -> tuple[list[int], dict[int, str]]:
        if self.familia.devengo == "fin":
            return super().ejercicios_afectados(desde, hasta, candidatos)
        # The file of year E holds what applies to tax periods STARTING in E.
        dentro: list[int] = []
        fuera: dict[int, str] = {}
        for ejercicio in sorted(set(candidatos)):
            if date(ejercicio, 1, 1) < desde:
                fuera[ejercicio] = (
                    f"la redacción leída produce efectos desde {desde.isoformat()}: no alcanza a todos los períodos "
                    f"iniciados en {ejercicio} (indique la redacción aplicable con --version o revíselo a mano)"
                )
            elif hasta is not None and date(ejercicio, 1, 1) > hasta:
                fuera[ejercicio] = f"la redacción leída deja de producir efectos el {hasta.isoformat()}"
            else:
                dentro.append(ejercicio)
        return dentro, fuera

    # -- files ------------------------------------------------------------ #

    def json_final(self, propuesta: Propuesta, anterior: Any, *, hoy: date) -> Any:
        # ``articulos_ley`` of a proposal may include the articles the reader asked for: the label of a new quote
        # only names them when there is exactly one.
        grupo, control, _, _ = self.componer(
            propuesta.despues,
            propuesta.citas,
            anterior,
            ruta=propuesta.ruta,
            boe_id=propuesta.boe_id,
            articulos=propuesta.articulos_ley,
            efectos=propuesta.efectos_desde,
            version=propuesta.version,
            hoy=hoy,
        )
        if control.fallos:
            raise RecodificacionError(f"No se puede escribir {propuesta.etiqueta}: " + "; ".join(control.fallos[:4]))
        self._recien_compuesto = (list(propuesta.ruta), grupo)
        return grupo

    # The last group handed to the loop for writing (see ``_recolocar``).
    _recien_compuesto: tuple[list[str], Any] | None = None

    def _recolocar(self, raw: dict[str, Any]) -> None:
        """Put a rule the loop has just APPENDED to its list back at the index its path names.

        ``nucleo.escribir_ruta`` only knows list items by "campo=valor"; the rules of the ISD file have no field
        that is unique in their list, so their paths end in an index, which it takes for a missing item and
        appends. Inert as soon as ``escribir_ruta`` writes at an index itself.
        """
        if self._recien_compuesto is None:
            return
        ruta, grupo = self._recien_compuesto
        lista = leer_ruta(raw, ruta[:-1])
        if isinstance(lista, list) and ruta[-1].isdigit() and int(ruta[-1]) < len(lista) - 1 and lista[-1] is grupo:
            lista[int(ruta[-1])] = lista.pop()

    def validar_fichero(self, raw: dict[str, Any]) -> None:
        self._recolocar(raw)
        params = self.familia.raiz.model_validate(raw)
        self.familia.validar_extra(params)
        problemas = self.familia.comprobar(params, None)
        if problemas:
            raise RecodificacionError(
                f"{self.fichero}: el fichero dejaría de superar su autocontrol: " + "; ".join(problemas[:4])
            )

    def citas_de(self, data_dir: Path, objetivo: Objetivo, ejercicio: int) -> list[str]:
        path = data_dir / self.fichero.format(ejercicio=ejercicio)
        grupo = leer_ruta(json.loads(path.read_text(encoding="utf-8")), objetivo.ruta) if path.exists() else None
        return _citas_legibles(grupo, objetivo.boe_id)

    def guardar_citas(self, data_dir: Path, propuesta: Propuesta, ficheros: dict[int, dict[str, Any]]) -> None:
        """Nothing to store apart: the quotes live inside the group. The loaders' caches are dropped here."""
        self.familia.limpiar()

    def _marcado(self, grupo: Any) -> bool:
        if not isinstance(grupo, dict):
            return False
        if "recodificada_el" in self.modelo.model_fields:
            return bool(grupo.get("recodificada_el"))
        fuente = grupo.get("fuente") or {}
        return not fuente.get("verificado") and str(fuente.get("nota") or "").startswith(MARCA)

    def pendientes(self, raw: dict[str, Any]) -> list[tuple[list[str], str]]:
        marcados = [ruta for ruta in rutas_de_grupos(raw) if self._marcado(leer_ruta(raw, ruta))]
        if not marcados:
            return []
        try:
            params = self.familia.raiz.model_validate(raw)
        except ValidationError:
            return []
        return [
            (ruta, _etiqueta(self.familia, raw, ruta, self.modelo))
            for ruta in marcados
            if type(_en_modelo(params, ruta)) is self.modelo
        ]

    def confirmar(self, raw: dict[str, Any], ruta: Sequence[str]) -> None:
        grupo = leer_ruta(raw, ruta)
        if not isinstance(grupo, dict):
            return
        grupo.pop("recodificada_el", None)
        fuentes = [grupo.get("fuente"), *(grupo.get(n, {}).get("fuente") for n in self._con_fuente_anidada())]
        for fuente in fuentes:
            if isinstance(fuente, dict) and str(fuente.get("nota") or "").startswith(MARCA):
                fuente["verificado"] = True
                fuente.pop("nota", None)
                fuente["referencia"] = str(fuente.get("referencia", "")).replace(PENDIENTE, "")
        self.familia.limpiar()


def _campo_de(fallo: str, numericos: set[str]) -> str:
    """The numeric field a walker's message is about ("tramos[0].coeficientes.I = 1.5: …" -> "coeficientes")."""
    donde = re.split(r" = |: ", fallo, maxsplit=1)[0]
    pasos = [re.sub(r"\[\d+\]", "", paso) for paso in donde.split(".")]
    return next((paso for paso in reversed(pasos) if paso in numericos), "")


def _es_del_grupo(problema: str, prefijo: str) -> bool:
    if prefijo.endswith("("):
        return problema.startswith(prefijo)
    return problema.startswith((prefijo + ":", prefijo + ".", prefijo + " "))


def _linea(resultado: Any, nombre: str) -> Decimal:
    """The value of a named line of a calculator's result."""
    if not nombre:
        raise ValueError("falta «resultado»: el nombre de la línea del resultado que se compara")
    if nombre.startswith("linea:"):
        concepto = nombre.partition(":")[2].strip()
        lineas = [linea for linea in getattr(resultado, "lineas", []) if linea.concepto == concepto] or [
            linea for linea in getattr(resultado, "lineas", []) if linea.concepto.startswith(concepto)
        ]
        if len(lineas) != 1:
            conceptos = "; ".join(linea.concepto for linea in getattr(resultado, "lineas", []))
            raise ValueError(f"no hay una única línea «{concepto}» en la traza ({conceptos[:400]})")
        return Decimal(lineas[0].importe)
    valor: Any = resultado
    for paso in nombre.split("."):
        if not isinstance(valor, BaseModel) or paso not in type(valor).model_fields:
            numericos = sorted(
                campo
                for campo in (type(valor).model_fields if isinstance(valor, BaseModel) else ())
                if isinstance(getattr(valor, campo), Decimal | int)
            )
            raise ValueError(f"«{nombre}» no es una línea del resultado (hay: {', '.join(numericos)})")
        valor = getattr(valor, paso)
    if isinstance(valor, bool):
        return Decimal(int(valor))
    if isinstance(valor, Decimal | int):
        return Decimal(valor).quantize(CENT)
    raise ValueError(f"«{nombre}» no es un importe en este caso ({valor!r})")


ADAPTADORES: dict[tuple[str, str], GrupoConEvidencia] = {}
for _familia in FAMILIAS:
    for _modelo in _familia.modelos():
        _adaptador = GrupoConEvidencia(_familia, _modelo)
        registrar(_adaptador)
        ADAPTADORES[(_familia.clave, _modelo.__name__)] = _adaptador


def adaptador_del_grupo(familia: Familia, params: Any, ruta: Sequence[str]) -> GrupoConEvidencia | None:
    """The adapter that maintains the group at ``ruta`` of a validated file."""
    return ADAPTADORES.get((familia.clave, type(_en_modelo(params, ruta)).__name__))


# --------------------------------------------------------------------------- #
# Verification and target discovery
# --------------------------------------------------------------------------- #


def _citas_legibles(grupo: Any, boe_id: str) -> list[str]:
    evidencia = grupo.get("evidencia", []) if isinstance(grupo, dict) else []
    return [
        str(e["cita"])
        for e in evidencia
        if isinstance(e, dict) and e.get("boe_id") == boe_id and es_legible(str(e.get("articulo", "")))
    ]


def _referencia(grupo: Any) -> str:
    if not isinstance(grupo, dict):
        return ""
    fuente = grupo.get("fuente")
    if isinstance(fuente, dict) and fuente.get("referencia"):
        return str(fuente["referencia"]).replace(PENDIENTE, "")
    return str(grupo.get("articulo") or "")


def _etiqueta(familia: Familia, raw: dict[str, Any], ruta: Sequence[str], modelo: type[BaseModel]) -> str:
    grupo = leer_ruta(raw, ruta)
    nombre = ""
    if ruta[0] == "comunidades" and len(ruta) > 1:
        nombre = f"{raw['comunidades'][ruta[1]].get('nombre', ruta[1])}, "
    return f"{familia.titulo}: {nombre}{'/'.join(ruta)} ({modelo.__name__}; {_referencia(grupo)})"


def _ficheros(familia: Familia, data_dir: Path, ejercicios: Sequence[int] | None = None) -> dict[int, dict[str, Any]]:
    disponibles = ejercicios_en(data_dir, familia.fichero)
    return {
        ejercicio: json.loads((data_dir / familia.fichero.format(ejercicio=ejercicio)).read_text(encoding="utf-8"))
        for ejercicio in disponibles
        if ejercicios is None or ejercicio in ejercicios
    }


def comprobaciones_de(familia: Familia, raw: dict[str, Any]) -> list[Comprobacion]:
    """One literal-quote check per group and law of a parameter file, for ``verificacion.verificar``."""
    out: list[Comprobacion] = []
    for ruta in rutas_de_grupos(raw):
        grupo = leer_ruta(raw, ruta)
        fuente = grupo.get("fuente") if isinstance(grupo.get("fuente"), dict) else {}
        por_ley: dict[str, list[str]] = {}
        for e in grupo["evidencia"]:
            por_ley.setdefault(str(e["boe_id"]), []).append(str(e["cita"]))
        for boe_id, citas in por_ley.items():
            out.append(
                Comprobacion(
                    boe_id=boe_id,
                    version=fuente.get("version_historica"),
                    ruta=ruta,
                    descripcion=f"{familia.titulo}: {'/'.join(ruta)} ({_referencia(grupo)})",
                    tipo="cita",
                    citas=citas,
                )
            )
    return out


def verificar_ficheros(
    texto_de: TextoDe,
    *,
    data_dir: Path = DATA_DIR,
    ejercicios: Sequence[int] | None = None,
    familias: Sequence[Familia] = FAMILIAS,
) -> dict[str, list[Resultado]]:
    """Re-verify the four files against the current text of their laws: file name -> one result per group and law.

    The same deterministic check ``params-verify`` runs for the IRPF file (every stored quote must still be in the
    law, whitespace aside); no model is involved.
    """
    out: dict[str, list[Resultado]] = {}
    for familia in familias:
        for ejercicio, raw in _ficheros(familia, data_dir, ejercicios).items():
            out[familia.fichero.format(ejercicio=ejercicio)] = verificar(comprobaciones_de(familia, raw), texto_de)
    return out


def construir_objetivo(
    familia: Familia,
    ruta: Sequence[str],
    ficheros: dict[int, dict[str, Any]],
    *,
    boe_id: str | None = None,
    origen: str,
    motivo: Sequence[str] = (),
    version: str | None = None,
    anotaciones: str = "",
) -> Objetivo:
    """The group at ``ruta`` in every year that has it, to be re-read from the articles of ``boe_id``.

    ``boe_id`` defaults to the law most of the group's article quotes come from. Raises RecodificacionError when
    no quote of the group comes from an article of that law: the loop has nothing it can re-read.
    """
    con_grupo = {e: raw for e, raw in ficheros.items() if isinstance(leer_ruta(raw, ruta), dict)}
    if not con_grupo:
        raise RecodificacionError(f"{familia.titulo}: no hay ningún grupo en «{'/'.join(ruta)}».")
    ultimo = max(con_grupo)
    raw = con_grupo[ultimo]
    grupo = leer_ruta(raw, ruta)
    legibles = [e for e in grupo["evidencia"] if es_legible(str(e.get("articulo", "")))]
    if boe_id is None and legibles:
        leyes = [str(e["boe_id"]) for e in legibles]
        boe_id = max(dict.fromkeys(leyes), key=leyes.count)
    propias = [e for e in legibles if e["boe_id"] == boe_id]
    if boe_id is None or not propias:
        raise RecodificacionError(
            f"{familia.titulo}: {'/'.join(ruta)} no tiene ninguna cita de un artículo o disposición"
            + (f" de {boe_id}" if boe_id else "")
            + " que el bucle sepa localizar (se apoya en anexos, preámbulos u otras normas): revísalo a mano."
        )
    params = familia.raiz.model_validate(raw)
    adaptador = adaptador_del_grupo(familia, params, ruta)
    if adaptador is None:
        raise RecodificacionError(f"Ningún adaptador de recodificación mantiene «{'/'.join(ruta)}».")
    articulos = list(dict.fromkeys(a for e in propias for a in bloques_de(str(e["articulo"]))))
    return Objetivo(
        tipo=adaptador.tipo,
        fichero=familia.fichero,
        ruta=list(ruta),
        etiqueta=_etiqueta(familia, raw, ruta, adaptador.modelo),
        ambito=ruta[1] if ruta[0] == "comunidades" and len(ruta) > 1 else "estado",
        boe_id=boe_id,
        version=version,
        articulos_ley=articulos,
        ejercicios=sorted(con_grupo),
        antes={e: leer_ruta(r, ruta) for e, r in con_grupo.items()},
        citas_antes=[str(e["cita"]) for e in propias],
        origen=origen,
        motivo=list(dict.fromkeys(motivo)),
        anotaciones=anotaciones,
    )


def objetivos_de_verificacion(
    ejercicios: Sequence[int] | None,
    texto_de: TextoDe,
    *,
    data_dir: Path = DATA_DIR,
    ambito: str | None = None,
    tipos: Sequence[str] | None = None,
    familias: Sequence[Familia] = FAMILIAS,
) -> Hallazgo:
    """Groups of the four files whose quotes are no longer in the law, as targets for the loop.

    A group is offered with every year that has it (which years the new wording reaches is decided from its effects
    date). It is read from the law whose quote failed; a group whose failing quote is neither in an article nor in
    a provision (an annex, a preamble, a ministerial order) is listed in ``sin_adaptador`` for a person.
    """
    hallazgo = Hallazgo()
    for familia in familias:
        ficheros = _ficheros(familia, data_dir, ejercicios)
        fallidos: dict[tuple[str, ...], dict[str, list[str]]] = {}
        ilegibles: dict[tuple[str, ...], list[str]] = {}
        for ejercicio, raw in ficheros.items():
            for resultado in verificar(comprobaciones_de(familia, raw), texto_de):
                check = resultado.comprobacion
                if resultado.ok or (
                    ambito and (check.ruta[1] if check.ruta[0] == "comunidades" else "estado") != ambito
                ):
                    continue
                if resultado.faltan == ["sin texto"]:
                    hallazgo.errores.append(f"{ejercicio} {check.descripcion}: no se pudo leer {check.boe_id}")
                    continue
                grupo = leer_ruta(raw, check.ruta)
                for e in grupo["evidencia"]:
                    if e["boe_id"] != check.boe_id or str(e["cita"])[:120] not in resultado.faltan:
                        continue
                    nota = f"{ejercicio}: ya no está en {check.boe_id} «{str(e['cita'])[:300]}» ({e['articulo']})"
                    if es_legible(str(e.get("articulo", ""))):
                        fallidos.setdefault(tuple(check.ruta), {}).setdefault(check.boe_id, []).append(nota)
                    else:
                        ilegibles.setdefault(tuple(check.ruta), []).append(nota)
        for ruta_t, notas in ilegibles.items():
            hallazgo.sin_adaptador.append(
                f"{familia.titulo} {'/'.join(ruta_t)}: {'; '.join(notas[:3])} — no es un artículo ni una disposición "
                "que el bucle sepa localizar: revísalo a mano"
            )
        for ruta_t, por_ley in fallidos.items():
            boe_id, motivo = next(iter(por_ley.items()))
            try:
                objetivo = construir_objetivo(
                    familia, ruta_t, ficheros, boe_id=boe_id, origen="verificacion", motivo=motivo[:8]
                )
            except (RecodificacionError, ValidationError) as exc:
                hallazgo.errores.append(str(exc)[:400])
                continue
            if tipos and objetivo.tipo not in tipos and familia.clave not in tipos:
                continue
            hallazgo.objetivos.append(objetivo)
            hallazgo.errores += [
                f"{objetivo.etiqueta}: también fallan citas de {otra}; vuelve a lanzar la verificación cuando se haya "
                f"resuelto la propuesta sobre {boe_id}"
                for otra in list(por_ley)[1:]
            ]
    return hallazgo


def objetivo_de_ruta(
    familia: str | Familia,
    ejercicios: Sequence[int] | None,
    ruta: Sequence[str],
    *,
    data_dir: Path = DATA_DIR,
    boe_id: str | None = None,
    version: str | None = None,
    anotaciones: str = "",
) -> Objetivo:
    """A group asked for by the operator ("sociedades", ["tipos_gravamen"]), whether or not its check fails."""
    familia = familia_de(familia) if isinstance(familia, str) else familia
    return construir_objetivo(
        familia,
        ruta,
        _ficheros(familia, data_dir, ejercicios),
        boe_id=boe_id,
        origen="articulo",
        motivo=["revisión pedida por el operador"],
        version=version,
        anotaciones=anotaciones,
    )


# --------------------------------------------------------------------------- #
# Sanity check of the shipped groups (no model)
# --------------------------------------------------------------------------- #


class GrupoRevisado(BaseModel):
    """What the gate says today about one shipped group, read with its own quotes."""

    model_config = ConfigDict(extra="forbid")

    fichero: str
    ruta: list[str]
    tipo: str = ""
    boe_id: str = ""
    # "ok": the loop could re-encode it and apply it without a person if the reviewer agreed; "persona": it passes
    # the gate but a person always confirms it; "falla": its own quotes do not pass the gate; "ilegible": no quote
    # of an article or provision of a law, the loop cannot read it at all.
    estado: Literal["ok", "persona", "falla", "ilegible"]
    fallos: list[str] = []
    revisar: list[str] = []
    # Whether the quotes were found inside the articles the loop would extract (None: the text given has no
    # article structure to extract from).
    en_articulos: bool | None = None


def pagina_de_texto(texto: str) -> str:
    """A plain-text extract of a law as the minimal HTML the article extractor understands."""
    if re.search(r"<(?:p|div|h\d)\b", texto, re.IGNORECASE):
        return texto
    return "".join(f"<p>{html.escape(linea)}</p>\n" for linea in texto.splitlines() if linea.strip())


def revisar_grupos_vigentes(
    familia: Familia, raw: dict[str, Any], textos: dict[str, str], *, ejercicio: int | None = None
) -> list[GrupoRevisado]:
    """Run every group of ``raw`` through the gate with its existing quotes. ``textos``: BOE id -> page or extract."""
    ejercicio = int(raw["ejercicio"]) if ejercicio is None else ejercicio
    nombre = familia.fichero.format(ejercicio=ejercicio)
    planos = {boe_id: _comprimido(" ".join(lineas_de(pagina_de_texto(t)))) for boe_id, t in textos.items()}
    out: list[GrupoRevisado] = []
    for ruta in rutas_de_grupos(raw):
        try:
            objetivo = construir_objetivo(familia, ruta, {ejercicio: raw}, origen="articulo")
        except RecodificacionError as exc:
            out.append(GrupoRevisado(fichero=nombre, ruta=ruta, estado="ilegible", fallos=[str(exc)]))
            continue
        adaptador = next(a for a in ADAPTADORES.values() if a.tipo == objetivo.tipo)
        citas = objetivo.citas_antes
        revisado = GrupoRevisado(fichero=nombre, ruta=ruta, tipo=objetivo.tipo, boe_id=objetivo.boe_id, estado="ok")
        plano = planos.get(objetivo.boe_id)
        if plano is None:
            revisado.fallos.append(f"no se ha aportado el texto de {objetivo.boe_id}")
        else:
            for cita in citas:
                propia = _comprimido(cita)
                if len(propia) < MIN_CITA:
                    revisado.fallos.append(f"cita demasiado corta para probar nada: «{cita[:60]}»")
                elif propia not in plano:
                    revisado.fallos.append(f"cita no literal (no está en el texto legal): «{cita[:90]}»")
            extraido = extraer_articulos(pagina_de_texto(textos[objetivo.boe_id]), objetivo.articulos_ley)
            if extraido.encontrados:
                revisado.en_articulos = all(_comprimido(c) in extraido.comprimido for c in citas)
        valor = adaptador._sin_procedencia(objetivo.antes[ejercicio])
        _, control = adaptador.controlar(valor, citas, objetivo, efectos_desde=None, datos={ejercicio: raw})
        revisado.fallos += control.fallos
        revisado.revisar += control.revisar
        revisado.estado = "falla" if revisado.fallos else "persona" if revisado.revisar else "ok"
        out.append(revisado)
    return out


def main(argv: list[str] | None = None) -> int:
    """``python -m hacienda_gpt.tax.recodificacion.adaptadores_otros <familia> <ejercicio> <dir with the laws>``.

    Says, without any model, what the gate thinks of each shipped group of a file read with its own quotes.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] not in {f.clave for f in FAMILIAS}:
        print(
            "uso: python -m hacienda_gpt.tax.recodificacion.adaptadores_otros "
            f"<{'|'.join(f.clave for f in FAMILIAS)}> <ejercicio> <directorio con <ID>.html o <ID>.txt>"
        )
        return 2
    familia = familia_de(args[0])
    raw = json.loads((DATA_DIR / familia.fichero.format(ejercicio=int(args[1]))).read_text(encoding="utf-8"))
    textos = {
        path.stem: path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(Path(args[2]).iterdir())
        if path.suffix in (".txt", ".html")
    }
    revisados = revisar_grupos_vigentes(familia, raw, textos)
    for r in revisados:
        if r.estado != "ok":
            print(f"[{r.estado}] {'/'.join(r.ruta)} ({r.tipo or 'sin adaptador'})")
            for linea in [*r.fallos, *(f"revisar: {x}" for x in r.revisar)]:
                print(f"    {linea}")
    cuenta = {
        estado: sum(1 for r in revisados if r.estado == estado) for estado in ("ok", "persona", "falla", "ilegible")
    }
    print(json.dumps({"fichero": revisados[0].fichero if revisados else "", "grupos": len(revisados), **cuenta}))
    return 1 if cuenta["falla"] else 0


__all__ = [
    "ADAPTADORES",
    "AUTONOMOS",
    "FAMILIAS",
    "ISD",
    "MARCA",
    "PATRIMONIO",
    "PENDIENTE",
    "SOCIEDADES",
    "Candidato",
    "Familia",
    "GrupoConEvidencia",
    "GrupoRevisado",
    "Hallazgo",
    "adaptador_del_grupo",
    "bloques_de",
    "comprobaciones_de",
    "construir_objetivo",
    "es_legible",
    "familia_de",
    "objetivo_de_ruta",
    "objetivos_de_verificacion",
    "revisar_grupos_vigentes",
    "rutas_de_grupos",
    "verificar_ficheros",
]

if __name__ == "__main__":
    raise SystemExit(main())
