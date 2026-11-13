"""The kinds of parameter group of the IRPF files the re-encoding loop maintains.

* ``deduccion_autonomica`` — one regional deduction computed from data
  (``DeduccionCalculable``), with its quotes in ``evidencia_deducciones.json``;
* ``escala`` — a progressive scale (``Escala``): the regional general scales and
  the State's general and savings scales;
* ``cifras:<grupo>`` — every other group: an object of figures with its ``fuente``
  (work reduction, minimums, donations, the Madrid deductions…). ONE class,
  :class:`GrupoDeCifras`, driven by the pydantic model of each group; a
  :class:`Cifras` entry says which model, which lines of the calculator a test
  case may compare and which fields the group's law does not print.

Nothing in an IRPF file is left without an adapter (``tests/tax/test_recodificacion_irpf.py``
checks it against the model). The adapters of the other parameter files live in
``adaptadores_otros``. Adding a kind means writing an
:class:`~hacienda_gpt.tax.recodificacion.nucleo.Adaptador` and registering it;
the loop, the proposals and the commands do not change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import copy
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hacienda_gpt.tax import params as params_mod
from hacienda_gpt.tax.params import (
    CAMPOS_EJERCICIO,
    CAMPOS_GASTO,
    CAMPOS_PERIODO,
    CAMPOS_REPARTO,
    CAMPOS_REQUISITO,
    CAMPOS_UNIDAD,
    DATA_DIR,
    AlquilerViviendaHabitual,
    BaseLimite,
    CompensacionAhorroParams,
    DeduccionCalculable,
    DeduccionesMadrid,
    DeduccionTrabajo,
    Donativos,
    Escala,
    Fuente,
    IRPFParams,
    Maternidad,
    Minimos,
    ObligacionDeclarar,
    PrevisionSocial,
    ReduccionArrendamiento,
    RepartoDeducciones,
    Trabajo,
    Tramo,
    TributacionConjunta,
    ViviendaTransitoria,
)
from hacienda_gpt.tax.recodificacion import prompts
from hacienda_gpt.tax.recodificacion.control import (
    CENT,
    Control,
    Reglas,
    _sin_acentos,
    articulo_base,
    articulos_de_referencia,
    cifras_sin_respaldo,
    normalizar_evidencia,
    notas_de_cifra_derivada,
    numeros_de_texto_sin_respaldo,
    ruta_de_etiqueta,
)
from hacienda_gpt.tax.recodificacion.nucleo import (
    Adaptador,
    Desglose,
    Objetivo,
    Propuesta,
    RecodificacionError,
    adaptadores_registrados,
    desglosar,
    ejercicios_en,
    escribir_ruta,
    ficheros_en_curso,
    leer_ruta,
    registrar,
)
from hacienda_gpt.tax.verificacion import (
    Resultado,
    TextoDe,
    _cifras_de,
    comprobaciones,
    huella_de_grupo,
    texto_plano,
    verificar,
)

FICHERO_IRPF = "irpf_{ejercicio}.json"
EVIDENCIA_DEDUCCIONES = "evidencia_deducciones.json"
EVIDENCIA_PARAMETROS = "evidencia_parametros.json"


def _errores(exc: ValidationError) -> list[str]:
    return [f"modelo: {'.'.join(map(str, e['loc'])) or 'valor'}: {e['msg']}"[:300] for e in exc.errors()[:8]]


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _guardar_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Regional deductions
# --------------------------------------------------------------------------- #

REGLAS_DEDUCCION = Reglas(
    euros=frozenset(
        {
            "importe",
            "importes_por_orden",
            "maximo",
            "maximo_incremento",
            "maximo_por_unidad",
            "base_maxima",
            "base_maxima_por_unidad",
            "limite_individual",
            "limite_conjunta",
            "limite_conjunta_incremento",
            "limite_unidad_familiar",
            "base_ahorro_max",
            "ingreso_max",
            "hasta",
            "desde_individual",
            "hasta_individual",
            "desde_conjunta",
            "hasta_conjunta",
        }
    ),
    fracciones=frozenset(
        {
            "porcentaje",
            "maximo_fraccion_cuota",
            "base_maxima_fraccion",
            "gasto_minimo_sobre_base",
            "factor_sobre_limite",
        }
    ),
    factores=frozenset({"factor"}),
    grados=frozenset({"discapacidad_min", "discapacidad_max", "conyuge_discapacidad_min"}),
    ignorar=frozenset({"recodificada_el", "en_vigor_desde"}),
)

GUIA_DEDUCCION = """\
How a regional IRPF deduction is encoded (the schema below is the authority; this explains how to use it):
- `tipo` "fija": amount = `importe` × units (`unidad`, or `unidad_descendientes` = the children matching a filter; no \
unit = once per taxpayer, and then at least one checkable condition is required). `tipo` "porcentaje": `porcentaje` \
(a fraction: 15 % → "0.15") of the expense named by `base`, under the caps.
- Inputs: use ONLY the names in the vocabularies of the schema (`CAMPOS_UNIDAD`, `CAMPOS_GASTO`, `CAMPOS_REQUISITO`, \
`CAMPOS_PERIODO`, `CAMPOS_REPARTO`, `CAMPOS_EJERCICIO`). If the AMOUNT depends on a datum that has no input, the \
deduction is not computable. A broad expense input is fed by all its parts (`GASTOS_DESGLOSADOS`): use the narrow \
part when the law covers only that part, `bases_adicionales` when it covers several.
- Conditions the calculator checks go in fields (`edad_min`/`edad_max`, `discapacidad_min`, `requiere`, \
`unidades_min`, `descendientes`, `habitantes_max`, `tributacion`, income ceilings). Every other legal condition goes, \
in Spanish, in `requisitos_no_comprobados`: the user is shown that list each time the deduction is applied. Never \
drop a condition silently. If the decisive eligibility condition cannot be checked and the trigger input does not \
imply it, the deduction is not computable.
- Bounds are INCLUSIVE and ages are those reached at 31 December: "menor de 36 años" → `edad_max` 35; "menos de 3.000 \
habitantes" → `habitantes_max` 2999; "no superior a 25.000" → `limite_individual` "25000"; "inferior a 25.000" → \
`limite_individual` "25000" with `limite_es_estricto` true.
- Income ceilings: `base_limite` names the income notion the law uses (see `BaseLimite`); `limite_individual` / \
`limite_conjunta` (or `limite_conjunta_incremento` when the law states the increment, not the sum); \
`limite_unidad_familiar`; `base_ahorro_max`. If the law uses an income notion that is not in `BaseLimite`, or income \
per family member, the deduction is not computable. `reduccion_progresiva` ONLY when the law reduces the amount \
linearly from 100 % at "desde" to 0 at "hasta".
- Different figures for some taxpayers: `variantes` (`cuando` is an AND; THE FIRST variant whose conditions hold is \
used, so the most specific or favourable goes first; for an OR repeat the variant). Either/or eligibility: \
`alternativas`. Ceilings raised or lifted for some: `ajustes_limite`. Figures per child: `ajustes_por_hijo`. Amount \
by order of the child or unit: `importes_por_orden`.
- Caps: `maximo` (euros, per return), `maximo_por_unidad`, `maximo_fraccion_cuota`; on the expense: `base_maxima`, \
`base_maxima_por_unidad`, `base_maxima_fraccion`. A law that says "15 % de las cantidades, con base máxima de 9.040 \
euros" is `base_maxima` "9040" — never a computed `maximo`. Increments are encoded as increments \
(`maximo_incremento`, `limite_conjunta_incremento`, `ajustes_limite[].factor` 1.2 for "+20 %").
- Part of the year: `prorrateo`. Cap shared between returns: `dividir_entre`. Tiered rates: `tramos_porcentaje`. \
Mutually exclusive deductions of the same law: `excluyente_con` (exact `articulo` strings of other encoded \
deductions). Shared cap across deductions: `limite_conjunto`.
- An encoding may give LESS than the law in an edge case only if `requisitos_no_comprobados` says so; it must never \
give MORE in a common case.
- `articulo`: keep exactly the label you are given. `titulo` and `resumen` (one or two sentences with the amounts and \
main conditions) in Spanish. `en_vigor_desde` is set by the program from the effects date: give any date.
- Do not write `recodificada_el`.
"""

_PERSONALES = frozenset(
    {
        "edad",
        "grado_discapacidad",
        "edad_conyuge",
        "grado_discapacidad_conyuge",
        "habitantes_municipio",
        "descendientes",
        "descendientes_compartidos",
        "miembros_unidad_familiar",
        "num_descendientes",
    }
)
# Whether a deduction depends on the taxpayer's income at all (then the reviewer's reading of the base matters).
_CON_INGRESO = (
    "limite_individual",
    "limite_conjunta",
    "limite_unidad_familiar",
    "reduccion_progresiva",
    "gasto_minimo_sobre_base",
    "factor_sobre_limite",
)


def _decimal(valor: Any, *, nombre: str, defecto: str | None = "0") -> Decimal | None:
    if valor in (None, ""):
        return Decimal(defecto) if defecto is not None else None
    try:
        return Decimal(str(valor).replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"«{nombre}» no es un importe") from exc


class DeduccionesAutonomicas(Adaptador):
    tipo = "deduccion_autonomica"
    fichero = FICHERO_IRPF
    que = (
        "one regional (autonomous community) IRPF deduction, as an entry of `deducciones_calculables` that the "
        "calculator evaluates from data alone"
    )
    permite_retirada = True
    min_casos = 2

    # -- prompts ---------------------------------------------------------- #

    def esquema(self) -> str:
        return prompts.fuente_de(
            [DeduccionCalculable], modulo=params_mod, desde="BaseLimite = Literal[", hasta="class RepartoDeducciones"
        )

    def guia(self) -> str:
        return GUIA_DEDUCCION

    def campos_de_caso(self) -> list[str]:
        """Inputs of the calculator a test taxpayer may carry."""
        from hacienda_gpt.tax.irpf import IRPFInput

        permitidos = (
            _PERSONALES | CAMPOS_UNIDAD | CAMPOS_GASTO | CAMPOS_REQUISITO | CAMPOS_PERIODO | CAMPOS_REPARTO
        ) | CAMPOS_EJERCICIO
        return sorted(permitidos & set(IRPFInput.model_fields))

    def guia_casos(self, objetivo: Objetivo) -> str:
        from hacienda_gpt.tax.irpf import Descendiente

        return (
            "`datos_json` of a case is a JSON object serialised as a string:\n"
            '{"contribuyente": {<inputs>}, "ingreso": "<euros>", "tributacion": "individual" | "conjunta", '
            '"base_ahorro": "<euros>", "resto_unidad_familiar": "<euros>", "cuota_integra_autonomica": "<euros>"}\n'
            "- `ingreso` is the amount of the income notion THE LAW names for its ceilings (it is used for every "
            "notion, so say in `base_limite_segun_ley` which one the law means: one of the `BaseLimite` values, or "
            '"" when the deduction has no income ceiling). Only `contribuyente` is mandatory; the rest default to '
            '0, "individual" and, for the regional gross tax, unknown.\n'
            f"- Inputs allowed in `contribuyente`: {', '.join(self.campos_de_caso())}.\n"
            f"- `descendientes` is a list with one object per child: {', '.join(Descendiente.model_fields)} "
            "(`edad` is the age reached at 31 December).\n"
            "- `importe_esperado`: the deduction in euros the law gives that taxpayer (0 when not entitled), "
            'decimal with a dot ("150.00").\n'
            "- Stay within what the calculator is told: conditions listed in `requisitos_no_comprobados` are assumed "
            "to hold, so do not build cases on them."
        )

    def extra_revisor(self) -> dict[str, Any]:
        return {"base_limite_segun_ley": {"type": "string", "enum": [*get_args(BaseLimite), ""]}}

    # -- gate ------------------------------------------------------------- #

    @staticmethod
    def _donde(ruta: Sequence[str]) -> tuple[str, str]:
        """(community key, article label) of a deduction's path."""
        return ruta[1], ruta[-1].partition("=")[2]

    def controlar(
        self, valor: Any, citas: list[str], objetivo: Objetivo, *, efectos_desde: date | None, datos: dict[int, Any]
    ) -> tuple[BaseModel | None, Control]:
        control = Control()
        clave, articulo = self._donde(objetivo.ruta)
        if not isinstance(valor, dict):
            control.fallos.append("el valor no es un objeto JSON")
            return None, control
        if valor.get("recodificada_el") is not None:
            control.fallos.append("«recodificada_el» lo pone el programa, no la propuesta")
        candidato = {k: v for k, v in valor.items() if k != "recodificada_el"}
        if efectos_desde is not None:
            # Dated by the quote-backed effects date, whatever the proposal says.
            candidato["en_vigor_desde"] = efectos_desde.isoformat()
        if candidato.get("articulo") != articulo:
            control.fallos.append(f"«articulo» debe ser exactamente «{articulo}»")
        try:
            modelo = DeduccionCalculable.model_validate(candidato)
        except ValidationError as exc:
            control.fallos += _errores(exc)
            return None, control
        ev = normalizar_evidencia(citas)
        cifras = cifras_sin_respaldo(modelo, ev, REGLAS_DEDUCCION)
        control.fallos += cifras.fallos
        control.revisar += cifras.revisar
        visibles = [modelo.resumen, *(v.descripcion for v in modelo.variantes)]
        for texto in visibles:
            control.fallos += [
                f"«{texto[:50]}…» menciona {numero}, que no está en ninguna cita"
                for numero in numeros_de_texto_sin_respaldo(texto, ev)
            ]
        for requisito in modelo.requisitos_no_comprobados:
            control.revisar += [
                f"el requisito «{requisito[:60]}…» menciona {numero}, que no está en ninguna cita"
                for numero in numeros_de_texto_sin_respaldo(requisito, ev)
            ]
        control.fallos += [
            f"nota de cifra derivada o aproximada: «{nota}»"
            for nota in notas_de_cifra_derivada([*visibles, *modelo.requisitos_no_comprobados])
        ]
        if modelo.limite_es_estricto and not re.search(
            r"inferior|menos de|menor (?:de|que|a)\b|por debajo|no alcan|no llegu", ev, re.IGNORECASE
        ):
            control.fallos.append("limite_es_estricto sin «inferior a» (o equivalente) en las citas")
        for ejercicio, raw in sorted(datos.items()):
            lista = leer_ruta(raw, objetivo.ruta[:-1])
            if ejercicio not in objetivo.ejercicios or not isinstance(lista, list):
                continue
            vecinos = [d.get("articulo") for d in lista if isinstance(d, dict)]
            for campo in ("incompatible_con", "excluyente_con"):
                control.fallos += [
                    f"{campo}: «{a}» no es una deducción codificada de {clave} en {ejercicio}"
                    for a in getattr(modelo, campo)
                    if a not in vecinos or a == articulo
                ]
            if articulo not in vecinos:
                # ``incompatible_con`` gives way to whatever was applied BEFORE in file order.
                tras = vecinos.index(objetivo.insertar_tras) + 1 if objetivo.insertar_tras in vecinos else len(vecinos)
                tras = 0 if objetivo.insertar_tras == "" else tras
                control.fallos += [
                    f"«{d['articulo']}» cede ante «{articulo}» (incompatible_con) y quedaría antes que él en {ejercicio}"
                    for d in lista[:tras]
                    if isinstance(d, dict) and articulo in d.get("incompatible_con", [])
                ]
        return modelo, control

    def normalizar(self, modelo: BaseModel) -> Any:
        return modelo.model_dump(mode="json", exclude_defaults=True, exclude={"recodificada_el"})

    def valor_previo(self, anterior: Any) -> Any:
        if not isinstance(anterior, dict):
            return anterior
        return {k: copy.deepcopy(v) for k, v in anterior.items() if k != "recodificada_el"}

    def evaluar_caso(self, modelo: BaseModel, datos: Any, objetivo: Objetivo) -> tuple[Decimal, str | None]:
        from hacienda_gpt.tax import autonomicas
        from hacienda_gpt.tax.irpf import IRPFInput

        assert isinstance(modelo, DeduccionCalculable)
        if not isinstance(datos, dict) or not isinstance(datos.get("contribuyente"), dict):
            raise ValueError("falta el objeto «contribuyente»")
        contribuyente = datos["contribuyente"]
        ajenos = sorted(set(contribuyente) - set(self.campos_de_caso()))
        if ajenos:
            raise ValueError(f"datos que la calculadora no usa para esta deducción: {', '.join(ajenos)}")
        if datos.get("tributacion", "individual") not in ("individual", "conjunta"):
            raise ValueError("«tributacion» debe ser individual o conjunta")
        conjunta = datos.get("tributacion") == "conjunta"
        ingreso = _decimal(datos.get("ingreso"), nombre="ingreso")
        ahorro = _decimal(datos.get("base_ahorro"), nombre="base_ahorro")
        resto = _decimal(datos.get("resto_unidad_familiar"), nombre="resto_unidad_familiar")
        cuota = _decimal(datos.get("cuota_integra_autonomica"), nombre="cuota_integra_autonomica", defecto=None)
        assert ingreso is not None and ahorro is not None and resto is not None
        inp = IRPFInput(
            ejercicio=max(objetivo.ejercicios),
            comunidad_autonoma=objetivo.ambito,
            tributacion="conjunta_biparental" if conjunta else "individual",
            **contribuyente,
        )
        bases = autonomicas.Bases(
            base_imponible_total=ingreso,
            base_imponible_general=ingreso,
            base_liquidable_total=ingreso,
            base_liquidable_general=ingreso,
            base_imponible_total_menos_minimos=ingreso,
            base_liquidable_total_menos_minimos=ingreso,
            base_imponible_total_menos_minimo_descendientes=ingreso,
            base_imponible_ahorro=ahorro,
            resto_unidad_familiar=resto,
            cuota_integra_autonomica=cuota,
        )
        try:
            resultado = autonomicas.evaluar(modelo, inp, bases, conjunta=conjunta)
        except Exception as exc:  # a case the engine cannot run is the case's problem, reported as such
            raise ValueError(f"la calculadora no pudo evaluarlo ({type(exc).__name__}: {exc})") from exc
        if resultado is None:
            return Decimal(0), "ningún dato del caso activa la deducción"
        if resultado.motivo is not None:
            return Decimal(0), f"no aplicada: {resultado.motivo}"
        notas = [f"variante «{resultado.variante}»"] if resultado.variante else []
        return resultado.importe.quantize(CENT), "; ".join([*notas, *resultado.avisos]) or None

    def contrastar(self, modelo: BaseModel, extra: dict[str, Any]) -> list[str]:
        assert isinstance(modelo, DeduccionCalculable)
        segun_ley = extra.get("base_limite_segun_ley")
        depende = any(getattr(modelo, campo) is not None for campo in _CON_INGRESO) or any(
            c.ingreso_max is not None for c in (modelo, *modelo.alternativas, *(v.cuando for v in modelo.variantes))
        )
        if depende and segun_ley != modelo.base_limite:
            return [
                f"Base de los límites de renta: la propuesta usa «{modelo.base_limite}» y el revisor lee en la ley "
                f"«{segun_ley or 'ninguna de las previstas'}»."
            ]
        return []

    # -- files ------------------------------------------------------------ #

    def json_final(self, propuesta: Propuesta, anterior: Any, *, hoy: date) -> Any:
        return {**propuesta.despues, "recodificada_el": hoy.isoformat()}

    def al_retirar(self, raw: dict[str, Any], propuesta: Propuesta) -> None:
        clave, articulo = self._donde(propuesta.ruta)
        comunidad = raw["comunidades"][clave]
        dependen = [
            d["articulo"]
            for d in comunidad.get("deducciones_calculables", [])
            if articulo in d.get("incompatible_con", []) or articulo in d.get("excluyente_con", [])
        ]
        if dependen:
            raise RecodificacionError(
                f"No se puede retirar «{articulo}» de {clave}: lo citan como incompatible {', '.join(dependen)}. "
                "Corrige antes esas deducciones."
            )
        actual = leer_ruta(raw, propuesta.ruta) or {}
        titulo = f"{articulo}: {actual.get('titulo', propuesta.etiqueta)}"
        if titulo not in comunidad.setdefault("otras_deducciones", []):
            comunidad["otras_deducciones"].append(titulo)

    def validar_fichero(self, raw: dict[str, Any]) -> None:
        IRPFParams.model_validate(raw)

    def citas_de(self, data_dir: Path, objetivo: Objetivo, ejercicio: int) -> list[str]:
        clave, articulo = self._donde(objetivo.ruta)
        evidencia = _json(data_dir / EVIDENCIA_DEDUCCIONES)
        del_ejercicio = evidencia.get(f"{clave}@{ejercicio}", {})
        citas = del_ejercicio[articulo] if articulo in del_ejercicio else evidencia.get(clave, {}).get(articulo, [])
        return list(citas)

    def guardar_citas(self, data_dir: Path, propuesta: Propuesta, ficheros: dict[int, dict[str, Any]]) -> None:
        """Keep ``verificacion`` honest for every year, touched or not.

        The quotes under the community's key are shared by every year that has no section of its own
        (``<key>@<year>``). Overwriting them would make the weekly check pass for a year this proposal does NOT
        touch, whose encoding still carries the old figures; so each of those years first gets the old quotes under
        its own section — where they keep failing, which is the truth.
        """
        clave, articulo = self._donde(propuesta.ruta)
        path = data_dir / EVIDENCIA_DEDUCCIONES
        evidencia: dict[str, dict[str, Any]] = _json(path)
        compartidas = evidencia.get(clave, {}).get(articulo)
        if propuesta.version is None:
            for ejercicio, raw in ficheros.items():
                propia = evidencia.get(f"{clave}@{ejercicio}", {})
                if ejercicio in propuesta.ejercicios:
                    if articulo in propia:
                        del propia[articulo]
                        if not set(propia) - {"_version"}:
                            del evidencia[f"{clave}@{ejercicio}"]
                elif compartidas and articulo not in propia and leer_ruta(raw, propuesta.ruta) is not None:
                    evidencia.setdefault(f"{clave}@{ejercicio}", {})[articulo] = compartidas
            if propuesta.resultado == "retirada":
                evidencia.get(clave, {}).pop(articulo, None)
            else:
                evidencia.setdefault(clave, {})[articulo] = propuesta.citas
        else:
            for ejercicio in propuesta.ejercicios:
                seccion = evidencia.setdefault(f"{clave}@{ejercicio}", {"_version": propuesta.version})
                if seccion.get("_version") != propuesta.version:
                    raise RecodificacionError(
                        f"Las citas de {clave} para {ejercicio} se comprueban contra la versión "
                        f"{seccion.get('_version') or 'vigente'} y la propuesta se leyó de la {propuesta.version}: "
                        "no pueden convivir en la misma sección de evidencia_deducciones.json."
                    )
                if propuesta.resultado == "retirada":
                    seccion.pop(articulo, None)
                else:
                    seccion[articulo] = propuesta.citas
        _guardar_json(path, evidencia)

    def grupos(self, raw: dict[str, Any]) -> list[list[str]]:
        return [
            ["comunidades", clave, "deducciones_calculables", f"articulo={d['articulo']}"]
            for clave, comunidad in raw.get("comunidades", {}).items()
            for d in comunidad.get("deducciones_calculables", [])
        ]

    def pendientes(self, raw: dict[str, Any]) -> list[tuple[list[str], str]]:
        return [
            (
                ["comunidades", clave, "deducciones_calculables", f"articulo={d['articulo']}"],
                f"{comunidad.get('nombre', clave)}: {d['articulo']} {d.get('titulo', '')}".strip(),
            )
            for clave, comunidad in raw.get("comunidades", {}).items()
            for d in comunidad.get("deducciones_calculables", [])
            if d.get("recodificada_el")
        ]

    def confirmar(self, raw: dict[str, Any], ruta: Sequence[str]) -> None:
        nodo = leer_ruta(raw, ruta)
        if isinstance(nodo, dict):
            nodo.pop("recodificada_el", None)


# --------------------------------------------------------------------------- #
# Progressive scales
# --------------------------------------------------------------------------- #


class EscalaLeida(BaseModel):
    """A progressive scale as the law prints it, bracket by bracket, in order.

    For each row of the table: ``hasta`` is the upper end of the bracket in euros (the "Base liquidable hasta" of the
    NEXT row; null for the last bracket, "en adelante"), ``cuota_hasta_inicio`` the "Cuota íntegra" the row prints
    (the tax accumulated at the start of the bracket) and ``tipo`` the "Tipo aplicable" as a fraction (11,65 % →
    "0.1165").
    """

    model_config = ConfigDict(extra="forbid")

    tramos: list[Tramo] = Field(min_length=1)


REGLAS_ESCALA = Reglas(
    euros=frozenset({"hasta", "cuota_hasta_inicio"}), porcentajes_de_tabla=frozenset({"tipo"}), ignorar=frozenset()
)
MARCA_ESCALA = "Recodificada automáticamente el "
GUIA_ESCALA = """\
How a progressive scale is encoded:
- The BOE prints it as a table with the columns "Base liquidable hasta euros", "Cuota íntegra euros", "Resto base \
liquidable hasta euros" and "Tipo aplicable porcentaje". Each row is one bracket, starting at the row's base.
- One `tramos` item per row, in order. `hasta`: where the bracket ENDS, i.e. the base printed on the next row (do \
not add base and "resto" yourself: copy the next row's base); null on the last row ("En adelante"). \
`cuota_hasta_inicio`: the "Cuota íntegra" printed on the row. `tipo`: the row's percentage as a fraction.
- Quote the table row by row, verbatim, cells in reading order ("12.985,35 1.168,68 8.083,25 11,65 %").
- If the article prints several scales (general and savings, or one per period), encode the one named in the task.
- Encode ONLY the table. Rules around it (minimums, reductions) belong to other groups.
"""
_ESCALAS_ESTATALES = ("escala_general_estatal", "escala_ahorro_estatal", "escala_ahorro_autonomica")


class _CitasDeParametros(Adaptador):
    """Quotes kept in ``evidencia_parametros.json``: file → path of the group → the quotes and where they were read."""

    def citas_de(self, data_dir: Path, objetivo: Objetivo, ejercicio: int) -> list[str]:
        fichero = _json(data_dir / EVIDENCIA_PARAMETROS).get(objetivo.fichero.format(ejercicio=ejercicio), {})
        return list(fichero.get("/".join(objetivo.ruta), {}).get("citas", []))

    def guardar_citas(self, data_dir: Path, propuesta: Propuesta, ficheros: dict[int, dict[str, Any]]) -> None:
        path = data_dir / EVIDENCIA_PARAMETROS
        evidencia: dict[str, dict[str, Any]] = _json(path)
        for ejercicio in propuesta.ejercicios:
            evidencia.setdefault(propuesta.fichero.format(ejercicio=ejercicio), {})["/".join(propuesta.ruta)] = {
                "boe_id": propuesta.boe_id,
                "version": propuesta.version,
                "efectos_desde": propuesta.efectos_desde,
                "cita_efectos": propuesta.cita_efectos,
                "citas": propuesta.citas,
                "propuesta": propuesta.id,
                # ``verificacion`` trusts these quotes only while the group holds the figures they were read for.
                "sha256_valor": huella_de_grupo(leer_ruta(ficheros[ejercicio], propuesta.ruta)),
            }
        _guardar_json(path, evidencia)


class Escalas(_CitasDeParametros):
    tipo = "escala"
    fichero = FICHERO_IRPF
    que = "a progressive tax scale (the table of brackets of an IRPF scale), as the list of its brackets"
    permite_retirada = False
    min_casos = 2

    def esquema(self) -> str:
        return prompts.fuente_de([Tramo, EscalaLeida], modulo=params_mod)

    def guia(self) -> str:
        return GUIA_ESCALA

    def guia_casos(self, objetivo: Objetivo) -> str:
        return (
            '`datos_json` of a case: {"base_liquidable": "<euros>"}. `importe_esperado`: the gross tax (cuota íntegra) '
            "the law's table gives for that base — the row's printed cuota plus the excess over the row's base at the "
            "row's rate — rounded to the cent. Choose bases exactly at a bracket boundary, just above one, inside "
            "the last bracket and below the first boundary."
        )

    def controlar(
        self, valor: Any, citas: list[str], objetivo: Objetivo, *, efectos_desde: date | None, datos: dict[int, Any]
    ) -> tuple[BaseModel | None, Control]:
        control = Control()
        try:
            modelo = EscalaLeida.model_validate(valor)
        except ValidationError as exc:
            control.fallos += _errores(exc)
            return None, control
        tramos = modelo.tramos
        topes = [t.hasta for t in tramos]
        if topes[-1] is not None or any(t is None for t in topes[:-1]):
            control.fallos.append("solo el último tramo queda sin tope («en adelante»)")
            return modelo, control
        finitos = [t for t in topes[:-1] if t is not None]
        if finitos != sorted(set(finitos)) or any(t <= 0 for t in finitos):
            control.fallos.append("los topes de los tramos deben ser positivos y crecientes")
        if any(not 0 < t.tipo < 1 for t in tramos):
            control.fallos.append("cada tipo es una fracción entre 0 y 1 (11,65 % → 0.1165)")
        if control.fallos:
            return modelo, control
        # The table must add up: a misread cell shows as a cumulative tax that does not follow from the rows above.
        acumulada, inicio = Decimal(0), Decimal(0)
        for i, tramo in enumerate(tramos):
            if tramo.cuota_hasta_inicio is not None:
                if i and abs(tramo.cuota_hasta_inicio - acumulada) > CENT:
                    control.fallos.append(
                        f"tramos[{i}].cuota_hasta_inicio = {tramo.cuota_hasta_inicio} no cuadra con los tramos "
                        f"anteriores ({acumulada.quantize(CENT)}): alguna celda está mal leída"
                    )
                acumulada = tramo.cuota_hasta_inicio
            elif i:
                control.revisar.append(f"tramos[{i}] no lleva la cuota íntegra impresa: no se puede cuadrar la tabla")
            if tramo.hasta is not None:
                acumulada += (tramo.hasta - inicio) * tramo.tipo
                inicio = tramo.hasta
        cifras = cifras_sin_respaldo(modelo, normalizar_evidencia(citas), REGLAS_ESCALA)
        control.fallos += cifras.fallos
        control.revisar += cifras.revisar
        return modelo, control

    def normalizar(self, modelo: BaseModel) -> Any:
        return modelo.model_dump(mode="json", exclude_none=False)

    def valor_previo(self, anterior: Any) -> Any:
        return {"tramos": copy.deepcopy(anterior.get("tramos"))} if isinstance(anterior, dict) else anterior

    def evaluar_caso(self, modelo: BaseModel, datos: Any, objetivo: Objetivo) -> tuple[Decimal, str | None]:
        assert isinstance(modelo, EscalaLeida)
        if not isinstance(datos, dict):
            raise ValueError("datos_json debe ser un objeto con «base_liquidable»")
        base = _decimal(datos.get("base_liquidable"), nombre="base_liquidable", defecto=None)
        if base is None or base < 0:
            raise ValueError("falta «base_liquidable»")
        escala = Escala(
            tramos=modelo.tramos, fuente=Fuente(referencia="propuesta", en_vigor_desde="", verificado=False)
        )
        return escala.cuota(base).quantize(CENT, rounding=ROUND_HALF_UP), None

    def json_final(self, propuesta: Propuesta, anterior: Any, *, hoy: date) -> Any:
        previa = anterior.get("fuente", {}) if isinstance(anterior, dict) else {}
        # The reference keeps the article and the law; the wording it named is no longer the one encoded.
        referencia = re.split(r"\s*(?:\(|,\s*redacci[oó]n)", str(previa.get("referencia") or propuesta.etiqueta))[0]
        fuente: dict[str, Any] = {
            "referencia": f"{referencia} (efectos desde {propuesta.efectos_desde})",
            "en_vigor_desde": propuesta.efectos_desde,
            # Unverified until a person confirms it: the calculator lists it in every result that uses the scale.
            "verificado": False,
            "nota": MARCA_ESCALA + f"{hoy.isoformat()} a partir del BOE; pendiente de revisión humana.",
        }
        if previa.get("condicionada_a"):
            fuente["condicionada_a"] = previa["condicionada_a"]
        if propuesta.version is not None:
            fuente["version_historica"] = propuesta.version
        tramos = [
            {k: v for k, v in t.items() if k != "cuota_hasta_inicio" or v is not None}
            for t in propuesta.despues["tramos"]
        ]
        return {"tramos": tramos, "fuente": fuente}

    def validar_fichero(self, raw: dict[str, Any]) -> None:
        IRPFParams.model_validate(raw)

    def grupos(self, raw: dict[str, Any]) -> list[list[str]]:
        rutas = [[nombre] for nombre in _ESCALAS_ESTATALES if nombre in raw]
        return rutas + [["comunidades", clave, "escala_general"] for clave in raw.get("comunidades", {})]

    def pendientes(self, raw: dict[str, Any]) -> list[tuple[list[str], str]]:
        out: list[tuple[list[str], str]] = []
        for ruta in self.grupos(raw):
            fuente = (leer_ruta(raw, ruta) or {}).get("fuente", {})
            if not fuente.get("verificado") and str(fuente.get("nota") or "").startswith(MARCA_ESCALA):
                out.append((ruta, f"{'/'.join(ruta)} ({fuente.get('referencia', '')})"))
        return out

    def confirmar(self, raw: dict[str, Any], ruta: Sequence[str]) -> None:
        fuente = (leer_ruta(raw, ruta) or {}).get("fuente")
        if isinstance(fuente, dict):
            fuente["verificado"] = True
            fuente.pop("nota", None)


# --------------------------------------------------------------------------- #
# Groups of figures
# --------------------------------------------------------------------------- #

REGLAS_CIFRAS = Reglas(
    euros=frozenset(
        {
            "otros_gastos",
            "umbral_superior",
            "umbral_inferior",
            "umbral_medio",
            "importe_maximo",
            "importe_tramo_2",
            "max_otras_rentas",
            "reduccion_biparental",
            "reduccion_monoparental",
            "limite_general",
            "incremento_contribucion_empresarial",
            "contribuyente",
            "incremento_mayor_65",
            "incremento_mayor_75",
            "descendientes",
            "incremento_descendiente_menor_3",
            "ascendiente",
            "incremento_ascendiente_mayor_75",
            "discapacidad",
            "discapacidad_65",
            "gastos_asistencia",
            "discapacidad_descendientes",
            "discapacidad_65_descendientes",
            "gastos_asistencia_descendientes",
            "tramo_primero",
            "importe_por_hijo",
            "incremento_guarderia",
            "base_maxima",
            "limite_trabajo_un_pagador",
            "limite_trabajo_varios_pagadores",
            "limite_segundo_pagador",
            "limite_capital_y_ganancias_con_retencion",
            "limite_base_individual",
            "limite_base_conjunta",
            "limite_base_unidad_familiar",
            "limite_base_por_miembro",
            "limite_suma_bases",
            "maximo",
            "maximo_por_hijo",
            "maximo_por_hijo_con_escolaridad",
            "maximo_por_hijo_primer_ciclo_infantil",
            "maximo_familia_numerosa",
        }
    ),
    fracciones=frozenset(
        {
            "porcentaje",
            "porcentaje_rendimientos",
            "porcentaje_minimo_sobre_base",
            "porcentaje_escolaridad_idiomas",
            "porcentaje_vestuario",
            "porcentaje_familia_numerosa",
            "limite_cruzado",
            "limite_base_liquidable",
            "parte_autonomica",
            "tipo",
            "rebaja_zona_tensionada",
            "joven_o_social",
            "rehabilitada",
            "resto",
            "maxima",
            "zona_tensionada_joven",
            "zona_tensionada",
            "joven",
            "rebaja_resto",
            "renta_no_superior",
            "primer_alquiler_sobre_indice",
            "social",
            "prorroga_tacita",
        }
    ),
    # The donations scale is a table: "250 euros / 80", with no sign next to the rate.
    porcentajes_de_tabla=frozenset({"tipo_primero", "tipo_resto", "tipo_resto_recurrente"}),
    literales=frozenset({"pendiente", "pendiente_tramo_1", "pendiente_tramo_2"}),
    porcentajes_mixtos=frozenset({"rebaja_minima", "incrementos"}),
    fechas=frozenset({"contratos_posteriores_a"}),
    ignorar=frozenset({"fuente"}),
)
MARCA_CIFRAS = MARCA_ESCALA
# Separates the provenance mark from the note the group carried before, which comes back on confirmation.
_NOTA_PREVIA = " || Nota anterior: "
_HISTORICA = re.compile(r"en su redacci[oó]n a\b", re.IGNORECASE)
_BOE_ID = re.compile(
    r"\b(?:BOE|BOA|BOC|BOCL|BOCM|BOCT|BORM|DOGC|DOGV|BOJA|BOPA|BOIB|DOE|DOG|BOR|BON|BOPV)-[A-Za-z]-\d{4}-\d+\b"
)
GUIA_CIFRAS = """\
How a group of figures is encoded:
- The value is the JSON object of the model below WITHOUT `fuente` (the program writes the provenance). Every field \
is mandatory unless the model gives it a default. If the text shown does not print one of them, do not guess: ask \
for the article that does in `articulos_necesarios`, or answer "no_calculable".
- Amounts in euros as decimal strings with a dot ("5550", "17673.52"). Percentages as FRACTIONS ("0.30" for "30 por \
100") unless the field's comment says percent points. Multipliers the law prints ("multiplicar por 1,75") exactly \
as printed ("1.75"). Dates as AAAA-MM-DD.
- Copy what the law prints. Never compute a threshold, a slope, a total or an increased amount from other figures.
- Keep the MEANING of every field as the previous encoding used it (<codificacion_anterior>): the calculator reads \
the fields by name and its formula is fixed. If the new wording changes the STRUCTURE of the rule — another tier, a \
different formula, a condition the model has no field for, a figure that now depends on something else — the model \
can no longer express it: answer "no_calculable" and say exactly what changed, so that a person changes the \
calculator. Do not squeeze a new rule into the old fields.
- Quote the sentence or the table row that prints each figure, verbatim.
"""


def _modelos_de(modelo: type[BaseModel]) -> list[type[BaseModel]]:
    """``modelo`` and the models nested in it (its ``Fuente`` aside), nested first."""
    import types
    import typing

    encontrados: list[type[BaseModel]] = []

    def tipos(anotacion: Any) -> list[Any]:
        if typing.get_origin(anotacion) in (typing.Union, types.UnionType, list, tuple, dict):
            return [t for arg in typing.get_args(anotacion) for t in tipos(arg)]
        return [anotacion]

    def recorrer(m: type[BaseModel]) -> None:
        for info in m.model_fields.values():
            for t in tipos(info.annotation):
                if isinstance(t, type) and issubclass(t, BaseModel) and t is not Fuente and t not in encontrados:
                    recorrer(t)
        if m not in encontrados:
            encontrados.append(m)

    recorrer(modelo)
    return encontrados


def _modelo_del_campo(padre: type[BaseModel], campo: str) -> type[BaseModel]:
    return next(m for m in _modelos_de(padre) if m is not padre and m in _modelos_de_anotacion(padre, campo))


def _modelos_de_anotacion(padre: type[BaseModel], campo: str) -> list[type[BaseModel]]:
    import types
    import typing

    anotacion = padre.model_fields[campo].annotation
    candidatos = typing.get_args(anotacion) if typing.get_origin(anotacion) in (typing.Union, types.UnionType) else ()
    return [t for t in (anotacion, *candidatos) if isinstance(t, type) and issubclass(t, BaseModel)]


@dataclass(frozen=True)
class Cifras:
    """One group of figures of the IRPF file: where it lives, its model and how a test case is run for it."""

    nombre: str
    # Path below the root of the file, or below ``comunidades/<key>`` when ``autonomico``.
    partes: tuple[str, ...]
    modelo: type[BaseModel]
    # One sentence for the prompts.
    que: str
    autonomico: bool = False
    # Lines of ``calcular_irpf`` a reviewer's test case may compare (by the start of their concept). Empty, and no
    # ``evaluador`` either: the group has no executable test case and every proposal for it is left to a person.
    lineas: tuple[str, ...] = ()
    # A pure function of the calculator that a case runs instead, for a group whose effect shows in no line:
    # (proposed model, the case's ``datos``) -> (amount, note). ValueError: the case cannot be run.
    evaluador: Callable[[BaseModel, dict[str, Any]], tuple[Decimal, str | None]] | None = None
    # How the reviewer writes ``datos_json`` for ``evaluador``.
    guia_evaluador: str = ""
    # Why no case can be run for the group, when it has neither lines nor an evaluator (said to the operator).
    sin_caso: str = ""
    # Fields the group's law does not print (they come from another law): kept as each file holds them.
    ajenos: frozenset[str] = frozenset()
    # A regime kept for old contracts, read from a superseded wording: re-read only when the operator names it.
    congelado: bool = False
    # Path of the State group whose amounts stand for the fields a regional law does not print (a community may set
    # only its taxpayer minimum and leave the rest as the State has them).
    completa_con: tuple[str, ...] = ()


class GrupoDeCifras(_CitasDeParametros):
    """Any object of figures with its ``fuente``: the model says what there is to read, the gate that it was read."""

    permite_retirada = False

    def __init__(self, cifras: Cifras) -> None:
        self.cifras = cifras
        self.tipo = f"cifras:{cifras.nombre}"
        self.fichero = FICHERO_IRPF
        self.que = cifras.que
        self.min_casos = 2 if cifras.lineas or cifras.evaluador is not None else 0

    # -- prompts ---------------------------------------------------------- #

    def esquema(self) -> str:
        return prompts.fuente_de(_modelos_de(self.cifras.modelo), modulo=params_mod)

    def guia(self) -> str:
        if self.cifras.completa_con:
            return GUIA_CIFRAS + (
                "- A community may set only SOME of these amounts (often just the taxpayer minimum). The fields its "
                "law does not print keep the State's amount: copy them unchanged from <codificacion_anterior> and do "
                "not look for a quote for them. A program checks that they equal the State's.\n"
            )
        ajenos = ""
        if self.cifras.ajenos:
            ajenos = (
                f"- This law does NOT print these fields (they come from another law): "
                f"{', '.join(sorted(self.cifras.ajenos))}. Leave them out of your value; the program keeps them.\n"
            )
        return GUIA_CIFRAS + ajenos

    def campos_de_caso(self) -> list[str]:
        from hacienda_gpt.tax.irpf import IRPFInput

        return sorted(set(IRPFInput.model_fields) - {"ejercicio"})

    def guia_casos(self, objetivo: Objetivo) -> str:
        if self.cifras.evaluador is not None:
            return self.cifras.guia_evaluador
        if not self.cifras.lineas:
            return (
                "This group has no executable test case (no line of the calculator isolates it): return `casos` as an "
                "empty list and put all your effort in `discrepancias`, checking every figure and what it is compared "
                "with against the text."
            )
        return (
            "`datos_json` of a case is a JSON object serialised as a string:\n"
            '{"contribuyente": {<inputs of the calculator>}, "linea": "<one of the lines below>"}\n'
            "The program runs the whole IRPF calculator for that taxpayer with the proposed figures and compares the "
            "amount of that line of the result (in absolute value) with `importe_esperado`, the amount THE LAW gives "
            'for that line, positive, decimal with a dot ("2000.00"; 0 when the line does not apply).\n'
            f"- Lines you may compare: {' | '.join(self.cifras.lineas)}.\n"
            f"- Inputs allowed in `contribuyente` (annual amounts in euros; omit what is zero): "
            f"{', '.join(self.campos_de_caso())}.\n"
            "- Build cases that isolate the group: exactly at each threshold, just below and just above it, at a cap, "
            "and one taxpayer the rule does not reach. Keep every other input at zero unless the rule needs it."
        )

    # -- gate ------------------------------------------------------------- #

    def ruta_de(self, ambito: str) -> list[str]:
        return [*(["comunidades", ambito] if self.cifras.autonomico else []), *self.cifras.partes]

    def _con_ajenos(self, valor: dict[str, Any], antes: dict[int, Any]) -> tuple[dict[str, Any], list[str]]:
        """The proposed value with the fields this law does not print taken from the latest file that has the group."""
        candidato = dict(valor)
        fallos: list[str] = []
        previo = next((antes[e] for e in sorted(antes, reverse=True) if isinstance(antes[e], dict)), None)
        for campo in sorted(self.cifras.ajenos):
            if previo is None or campo not in previo:
                fallos.append(f"«{campo}» no lo imprime esta ley y no hay codificación anterior de la que conservarlo")
            elif campo in valor and Decimal(str(valor[campo])) != Decimal(str(previo[campo])):
                fallos.append(f"«{campo}» no lo imprime esta ley: se conserva el anterior ({previo[campo]})")
            else:
                candidato[campo] = previo[campo]
        return candidato, fallos

    def controlar(
        self, valor: Any, citas: list[str], objetivo: Objetivo, *, efectos_desde: date | None, datos: dict[int, Any]
    ) -> tuple[BaseModel | None, Control]:
        control = Control()
        if not isinstance(valor, dict):
            control.fallos.append("el valor no es un objeto JSON")
            return None, control
        if "fuente" in valor:
            control.fallos.append("«fuente» la escribe el programa, no la propuesta")
        try:
            candidato, fallos = self._con_ajenos({k: v for k, v in valor.items() if k != "fuente"}, objetivo.antes)
        except (InvalidOperation, ValueError):
            control.fallos.append("un campo que esta ley no imprime no es un número")
            return None, control
        control.fallos += fallos
        marcador = {"referencia": "propuesta", "en_vigor_desde": "", "verificado": False}
        try:
            modelo = self.cifras.modelo.model_validate({**candidato, "fuente": marcador})
        except ValidationError as exc:
            control.fallos += _errores(exc)
            return None, control
        reglas = Reglas(
            euros=REGLAS_CIFRAS.euros,
            fracciones=REGLAS_CIFRAS.fracciones,
            porcentajes_de_tabla=REGLAS_CIFRAS.porcentajes_de_tabla,
            literales=REGLAS_CIFRAS.literales,
            porcentajes_mixtos=REGLAS_CIFRAS.porcentajes_mixtos,
            fechas=REGLAS_CIFRAS.fechas,
            ignorar=REGLAS_CIFRAS.ignorar | self.cifras.ajenos,
        )
        cifras = cifras_sin_respaldo(modelo, normalizar_evidencia(citas), reglas)
        estatal = self._estatal(datos, objetivo)
        for fallo in cifras.fallos:
            campo = re.split(r"[\[ =.]", fallo, maxsplit=1)[0]
            if estatal is not None and campo in estatal and _iguales(getattr(modelo, campo, None), estatal[campo]):
                control.revisar.append(
                    f"{campo}: la ley autonómica no lo imprime; coincide con el importe estatal, que es el que rige"
                )
            else:
                control.fallos.append(fallo)
        control.revisar += cifras.revisar
        if not self.cifras.lineas and self.cifras.evaluador is None:
            control.revisar.append(
                "este grupo no tiene casos de prueba ejecutables"
                + (f" ({self.cifras.sin_caso})" if self.cifras.sin_caso else "")
                + ": la propuesta solo ha pasado el control de cifras y la lectura del revisor; confírmela a mano"
            )
        if self.cifras.ajenos:
            control.revisar.append(
                f"{', '.join(sorted(self.cifras.ajenos))}: no se lee de esta ley; se conserva el valor de cada fichero"
            )
        return modelo, control

    def _estatal(self, datos: dict[int, Any], objetivo: Objetivo) -> dict[str, Any] | None:
        """The State group that fills what the regional law leaves unsaid, from the latest file asked for."""
        if not self.cifras.completa_con:
            return None
        ficheros = datos or ficheros_en_curso()
        presentes = [e for e in sorted(objetivo.ejercicios, reverse=True) if e in ficheros]
        grupo = leer_ruta(ficheros[presentes[0]], self.cifras.completa_con) if presentes else None
        return grupo if isinstance(grupo, dict) else None

    def normalizar(self, modelo: BaseModel) -> Any:
        return modelo.model_dump(mode="json", exclude={"fuente", *self.cifras.ajenos})

    def valor_previo(self, anterior: Any) -> Any:
        if not isinstance(anterior, dict):
            return anterior
        return {k: copy.deepcopy(v) for k, v in anterior.items() if k != "fuente" and k not in self.cifras.ajenos}

    def evaluar_caso(self, modelo: BaseModel, datos: Any, objetivo: Objetivo) -> tuple[Decimal, str | None]:
        from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf

        if self.cifras.evaluador is not None:
            if not isinstance(datos, dict):
                raise ValueError("datos_json debe ser un objeto")
            return self.cifras.evaluador(modelo, datos)
        if not self.cifras.lineas:
            raise ValueError("este grupo no tiene ninguna línea de la calculadora con la que comparar")
        if not isinstance(datos, dict) or not isinstance(datos.get("contribuyente"), dict):
            raise ValueError("falta el objeto «contribuyente»")
        pedida = _plano(str(datos.get("linea") or self.cifras.lineas[0]))
        permitida = next((linea for linea in self.cifras.lineas if _plano(linea).startswith(pedida[:40])), None)
        if permitida is None or not pedida:
            raise ValueError(f"«linea» debe ser una de: {' | '.join(self.cifras.lineas)}")
        contribuyente = dict(datos["contribuyente"])
        ajenos = sorted(set(contribuyente) - set(self.campos_de_caso()))
        if ajenos:
            raise ValueError(f"datos que la calculadora no conoce: {', '.join(ajenos)}")
        ejercicio = max(objetivo.ejercicios)
        base = ficheros_en_curso().get(ejercicio)
        raw = (
            copy.deepcopy(base) if isinstance(base, dict) else params_mod.load_params(ejercicio).model_dump(mode="json")
        )
        if self.cifras.autonomico:
            contribuyente.setdefault("comunidad_autonoma", objetivo.ambito)
        try:
            # The group under test with a verified source, so that the result is not littered with warnings.
            propuesto = {
                **modelo.model_dump(mode="json"),
                "fuente": {"referencia": "propuesta", "en_vigor_desde": "", "verificado": True},
            }
            escribir_ruta(raw, objetivo.ruta, propuesto)
            params = IRPFParams.model_validate(raw)
            entrada = IRPFInput(ejercicio=ejercicio, **contribuyente)
            resultado = calcular_irpf(entrada, params)
        except (ValidationError, KeyError, TypeError) as exc:
            raise ValueError(f"caso no válido ({type(exc).__name__}: {str(exc)[:160]})") from exc
        except Exception as exc:  # a case the calculator cannot run is the case's problem, reported as such
            raise ValueError(f"la calculadora no pudo evaluarlo ({type(exc).__name__}: {exc})") from exc
        suyas = [linea for linea in resultado.lineas if _plano(linea.concepto).startswith(_plano(permitida))]
        if not suyas:
            return Decimal(0), f"la línea «{permitida}» no aparece en el resultado (la regla no se aplica)"
        return abs(sum((linea.importe for linea in suyas), Decimal(0))).quantize(CENT), suyas[0].concepto

    # -- files ------------------------------------------------------------ #

    def json_final(self, propuesta: Propuesta, anterior: Any, *, hoy: date) -> Any:
        previa = anterior.get("fuente", {}) if isinstance(anterior, dict) else {}
        referencia = re.split(r"\s*(?:\(|,\s*redacci[oó]n)", str(previa.get("referencia") or propuesta.etiqueta))[0]
        nota = MARCA_CIFRAS + f"{hoy.isoformat()} a partir del BOE; pendiente de revisión humana."
        anterior_nota = str(previa.get("nota") or "")
        if anterior_nota and not anterior_nota.startswith(MARCA_CIFRAS):
            nota += _NOTA_PREVIA + anterior_nota
        fuente: dict[str, Any] = {
            "referencia": f"{referencia} (efectos desde {propuesta.efectos_desde})",
            "en_vigor_desde": propuesta.efectos_desde,
            # Unverified until a person confirms it: the calculator names it in every result that uses the group.
            "verificado": False,
            "nota": nota,
        }
        if previa.get("condicionada_a"):
            # Still hangs from the decree-law it was born with: if Congress repeals it, the group goes too.
            fuente["condicionada_a"] = previa["condicionada_a"]
        if propuesta.version is not None:
            fuente["version_historica"] = propuesta.version
        final = dict(propuesta.despues)
        for campo in sorted(self.cifras.ajenos):
            if not isinstance(anterior, dict) or campo not in anterior:
                raise RecodificacionError(
                    f"{propuesta.etiqueta}: «{campo}» no se lee de esta ley y el fichero no tiene un valor que conservar."
                )
            final[campo] = anterior[campo]
        return {**final, "fuente": fuente}

    def validar_fichero(self, raw: dict[str, Any]) -> None:
        IRPFParams.model_validate(raw)

    def grupos(self, raw: dict[str, Any]) -> list[list[str]]:
        ambitos = list(raw.get("comunidades", {})) if self.cifras.autonomico else ["estado"]
        return [ruta for ruta in (self.ruta_de(a) for a in ambitos) if isinstance(leer_ruta(raw, ruta), dict)]

    def reconocer(self, ruta: Sequence[str], raw: dict[str, Any]) -> list[str] | None:
        partes = tuple(ruta[2:]) if self.cifras.autonomico and len(ruta) > 2 and ruta[0] == "comunidades" else None
        if not self.cifras.autonomico:
            partes = tuple(ruta)
        return list(ruta) if partes == self.cifras.partes else None

    def pendientes(self, raw: dict[str, Any]) -> list[tuple[list[str], str]]:
        out: list[tuple[list[str], str]] = []
        for ruta in self.grupos(raw):
            fuente = (leer_ruta(raw, ruta) or {}).get("fuente", {})
            if not fuente.get("verificado") and str(fuente.get("nota") or "").startswith(MARCA_CIFRAS):
                out.append((ruta, f"{'/'.join(ruta)} ({fuente.get('referencia', '')})"))
        return out

    def confirmar(self, raw: dict[str, Any], ruta: Sequence[str]) -> None:
        fuente = (leer_ruta(raw, ruta) or {}).get("fuente")
        if isinstance(fuente, dict):
            fuente["verificado"] = True
            _, hay, previa = str(fuente.get("nota") or "").partition(_NOTA_PREVIA)
            if hay:
                fuente["nota"] = previa
            else:
                fuente.pop("nota", None)

    def objetivo(
        self,
        ruta: list[str],
        ficheros: dict[int, dict[str, Any]],
        *,
        data_dir: Path = DATA_DIR,
        origen: str,
        motivo: Sequence[str] = (),
        version: str | None = None,
        anotaciones: str = "",
    ) -> Objetivo:
        if not ficheros:
            raise RecodificacionError("No hay ningún fichero de parámetros para los ejercicios pedidos.")
        ultimo = max(ficheros)
        antes = {ejercicio: leer_ruta(raw, ruta) for ejercicio, raw in ficheros.items()}
        con_grupo = [e for e in sorted(antes, reverse=True) if isinstance(antes[e], dict)]
        nombre_ruta = "/".join(ruta)
        if not con_grupo:
            raise RecodificacionError(
                f"{nombre_ruta}: ningún fichero contiene este grupo, así que no se sabe de qué artículo leerlo."
            )
        fuente = antes[con_grupo[0]].get("fuente", {})
        referencia = str(fuente.get("referencia") or "")
        if ruta[0] == "comunidades":
            comunidad = ficheros[ultimo]["comunidades"].get(ruta[1])
            if comunidad is None:
                raise RecodificacionError(f"No hay ninguna comunidad «{ruta[1]}» en los parámetros de {ultimo}.")
            ambito, nombre, boe_id = ruta[1], comunidad["nombre"], comunidad["fuente_principal"]["boe_id"]
        else:
            ambito, nombre, boe_id = "estado", "Estado", ficheros[ultimo]["fuente_principal"]["boe_id"]
        etiqueta = (
            f"{nombre}: {' / '.join(p.replace('_', ' ') for p in self.cifras.partes)} ({referencia.split(' (')[0]})"
        )
        if re.search(r"\bcalculad[oa]s?\b", str(fuente.get("nota") or ""), re.IGNORECASE):
            raise RecodificacionError(
                f"{etiqueta}: sus importes están calculados a partir de otros (la ley no los imprime); el bucle no "
                "codifica cifras derivadas. Revísalo a mano."
            )
        historica = (
            self.cifras.congelado or bool(_HISTORICA.search(referencia)) or bool(fuente.get("version_historica"))
        )
        if historica and version is None:
            raise RecodificacionError(
                f"{etiqueta}: se leyó de una redacción que ya no es la vigente. El bucle nunca elige por su cuenta una "
                "versión histórica: indica la aplicable con --version AAAAMMDD."
            )
        # A group read from another law names it in the first part of its reference ("art. 19 Ley 49/2002 (BOE-A-…)").
        otra_ley = _BOE_ID.search(referencia.split(";")[0])
        if (
            otra_ley is not None
            and "añadid" not in referencia.split(";")[0]
            and "redacci" not in referencia.split(";")[0]
        ):
            boe_id = otra_ley.group(0)
        articulos = articulos_de_referencia(referencia)
        if not articulos:
            raise RecodificacionError(f"{etiqueta}: no se sabe qué artículo de la ley leer («{referencia}»).")
        objetivo = Objetivo(
            tipo=self.tipo,
            fichero=self.fichero,
            ruta=list(ruta),
            etiqueta=etiqueta,
            ambito=ambito,
            boe_id=boe_id,
            version=version,
            articulos_ley=articulos,
            ejercicios=sorted(ficheros),
            antes=antes,
            origen=origen,
            motivo=list(dict.fromkeys(motivo)),
            anotaciones=anotaciones,
        )
        objetivo.citas_antes = self.citas_de(data_dir, objetivo, con_grupo[0])
        return objetivo


def _iguales(a: Any, b: Any) -> bool:
    """Do a model's value and a file's JSON hold the same figures?"""
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(_iguales(x, y) for x, y in zip(a, b, strict=True))
    try:
        return a is not None and b is not None and Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return False


def _plano(texto: str) -> str:
    return " ".join(_sin_acentos(texto).lower().split())


GUIA_CASOS_REPARTO = (
    '`datos_json` of a case: {"deducciones_generales": "<euros>", "parte": "autonomica" | "estatal"}. '
    "`deducciones_generales` is a total amount of the deductions that the text shown splits between the State and "
    "the regional tax; `parte` (default "
    '"autonomica") says which share `importe_esperado` is: the amount THE LAW assigns to that tax, in euros with a '
    "dot, rounded to the cent. The program runs the calculator's own split with the proposed figures. Choose round "
    "and odd totals (one cent, an odd number of cents, a large amount)."
)


def _caso_de_reparto(modelo: BaseModel, datos: dict[str, Any]) -> tuple[Decimal, str | None]:
    """A reviewer's case for the State/regional split, run through the calculator's own function."""
    from hacienda_gpt.tax.irpf import repartir_deducciones_estatales

    assert isinstance(modelo, RepartoDeducciones)
    deducciones = _decimal(datos.get("deducciones_generales"), nombre="deducciones_generales", defecto=None)
    if deducciones is None or deducciones < 0:
        raise ValueError("falta «deducciones_generales» (un importe no negativo)")
    parte = str(datos.get("parte") or "autonomica").strip().lower()
    if parte not in ("autonomica", "estatal"):
        raise ValueError("«parte» debe ser «autonomica» o «estatal»")
    estatal, autonomica = repartir_deducciones_estatales(deducciones, modelo)
    nota = f"parte estatal {estatal} €, parte autonómica {autonomica} €"
    return (autonomica if parte == "autonomica" else estatal), nota


def _cifras_del_irpf() -> list[Cifras]:
    arrendamiento = {
        campo: _modelo_del_campo(ReduccionArrendamiento, campo)
        for campo in ("regimen_2021", "regimen_2023", "regimen_2026")
    }
    reduccion_alquiler = ("Reducción por arrendamiento de vivienda",)
    grupos = [
        Cifras(
            "trabajo",
            ("trabajo",),
            Trabajo,
            "the deductible expenses and the reduction for work income of the State IRPF (arts. 19.2.f and 20 LIRPF)",
            lineas=("Otros gastos deducibles", "Reducción por obtención de rendimientos del trabajo"),
        ),
        Cifras(
            "tributacion_conjunta",
            ("tributacion_conjunta",),
            TributacionConjunta,
            "the reductions for joint taxation (art. 84.2 LIRPF)",
            lineas=("Reducción por tributación conjunta",),
        ),
        Cifras(
            "prevision_social",
            ("prevision_social",),
            PrevisionSocial,
            "the limits of the reduction for contributions to pension schemes (art. 52.1 LIRPF)",
            lineas=("Reducción por aportaciones a sistemas de previsión social",),
        ),
        Cifras(
            "minimos",
            ("minimos",),
            Minimos,
            "the State personal and family minimums (arts. 57 to 60 LIRPF)",
            lineas=(
                "Mínimo del contribuyente",
                "Mínimo por descendientes",
                "Mínimo por ascendientes",
                "Mínimo por discapacidad del contribuyente",
                "Mínimo por discapacidad de descendientes",
                "Mínimo por discapacidad de ascendientes",
            ),
        ),
        Cifras(
            "reparto_deducciones_estatales",
            ("reparto_deducciones_estatales",),
            RepartoDeducciones,
            "the share of the State deductions borne by the regional tax (arts. 67 and 77 LIRPF)",
            evaluador=_caso_de_reparto,
            guia_evaluador=GUIA_CASOS_REPARTO,
        ),
        Cifras(
            "deduccion_rendimientos_trabajo",
            ("deduccion_rendimientos_trabajo",),
            DeduccionTrabajo,
            "the deduction for obtaining work income (disposición adicional 61.ª LIRPF)",
            lineas=("Deducción por obtención de rendimientos del trabajo",),
        ),
        Cifras(
            "donativos",
            ("donativos",),
            Donativos,
            "the scale of the deduction for donations (art. 19 of Ley 49/2002)",
            lineas=("Deducción por donativos",),
            ajenos=frozenset({"limite_base_liquidable"}),
        ),
        Cifras(
            "maternidad",
            ("maternidad",),
            Maternidad,
            "the maternity deduction and its childcare increase (art. 81 LIRPF)",
            lineas=("Deducción por maternidad",),
        ),
        Cifras(
            "vivienda_habitual_transitoria",
            ("vivienda_habitual_transitoria",),
            ViviendaTransitoria,
            "the transitional deduction for buying the main home before 2013 (disposición transitoria 18.ª LIRPF)",
            lineas=("Deducción por inversión en vivienda habitual (régimen transitorio)",),
        ),
        Cifras(
            "alquiler_vivienda_habitual",
            ("alquiler_vivienda_habitual",),
            AlquilerViviendaHabitual,
            "the State deduction for renting the main home (art. 68.6 LIRPF)",
            lineas=("Deducción estatal por alquiler de vivienda habitual",),
        ),
        Cifras(
            "obligacion_declarar",
            ("obligacion_declarar",),
            ObligacionDeclarar,
            "the income limits below which there is no obligation to file (art. 96 LIRPF)",
            sin_caso="ninguna calculadora usa estas cifras: el programa no decide si hay obligación de declarar",
        ),
        Cifras(
            "compensacion_ahorro",
            ("compensacion_ahorro",),
            CompensacionAhorroParams,
            "the limit for offsetting negative savings balances of one kind against the other (art. 49.1 LIRPF)",
            lineas=(
                "Compensación entre rendimientos y ganancias del ahorro",
                "Compensación de saldos negativos de ejercicios anteriores",
                "Base imponible del ahorro",
            ),
        ),
        Cifras(
            "arrendamiento_2021",
            ("reduccion_arrendamiento", "regimen_2021"),
            arrendamiento["regimen_2021"],
            "the reduction of net income from letting dwellings for contracts made before Ley 12/2023",
            lineas=reduccion_alquiler,
            congelado=True,
        ),
        Cifras(
            "arrendamiento_2023",
            ("reduccion_arrendamiento", "regimen_2023"),
            arrendamiento["regimen_2023"],
            "the reductions of net income from letting dwellings in the wording of Ley 12/2023",
            lineas=reduccion_alquiler,
            congelado=True,
        ),
        Cifras(
            "arrendamiento_2026",
            ("reduccion_arrendamiento", "regimen_2026"),
            arrendamiento["regimen_2026"],
            "the reductions of net income from letting dwellings in the wording in force (art. 23.2 LIRPF)",
            lineas=reduccion_alquiler,
        ),
        Cifras(
            "minimos_autonomicos",
            ("minimos",),
            Minimos,
            "the personal and family minimums an autonomous community sets for its share of the IRPF",
            autonomico=True,
            lineas=("Mínimo personal y familiar para el gravamen autonómico",),
            completa_con=("minimos",),
        ),
    ]
    lineas_madrid = {
        "nacimiento": "Deducción autonómica por nacimiento o adopción de hijos",
        "alquiler": "Deducción autonómica por alquiler de vivienda habitual",
        "gastos_educativos": "Deducción autonómica por gastos educativos",
        "cuidado": "Deducción autonómica por cuidado de menores o dependientes",
        "donativos": "Deducción autonómica por donativos a fundaciones y clubes deportivos",
        "familias_ingresos_reducidos": "Deducción autonómica para familias con dos o más descendientes e ingresos reducidos",
    }
    for campo in DeduccionesMadrid.model_fields:
        grupos.append(
            Cifras(
                f"madrid_{campo}",
                ("deducciones_madrid", campo),
                _modelo_del_campo(DeduccionesMadrid, campo),
                f"the figures of a regional deduction the calculator computes with its own formula ({campo.replace('_', ' ')})",
                autonomico=True,
                lineas=(lineas_madrid[campo],) if campo in lineas_madrid else (),
            )
        )
    return grupos


DEDUCCIONES = registrar(DeduccionesAutonomicas())
ESCALAS = registrar(Escalas())
CIFRAS: dict[str, GrupoDeCifras] = {c.nombre: GrupoDeCifras(c) for c in _cifras_del_irpf()}
for _adaptador in CIFRAS.values():
    registrar(_adaptador)
# The adapters of the IRPF file, in the order a path is offered to them.
_DEL_IRPF: tuple[Adaptador, ...] = (DEDUCCIONES, ESCALAS, *CIFRAS.values())
# Paths of the IRPF file that are groups of legal figures. Anything else at the root of the file, or of a community,
# is bookkeeping (names, sources, notes) or a catalogue of text; a NEW field must be put in one list or the other.
NO_SON_GRUPOS = frozenset(
    {"ejercicio", "fuente_principal", "nota_ejercicio", "revisiones", "comunidades", "reduccion_arrendamiento"}
)
NO_SON_GRUPOS_DE_COMUNIDAD = frozenset(
    {"nombre", "alias", "fuente_principal", "deducciones", "otras_deducciones", "notas", "deducciones_calculables"}
)

# --------------------------------------------------------------------------- #
# Finding what to re-read
# --------------------------------------------------------------------------- #


class Hallazgo(BaseModel):
    """What a look at the parameter files found to re-read, and what it could not take on."""

    model_config = ConfigDict(extra="forbid")

    objetivos: list[Objetivo] = Field(default_factory=list)
    # Failing groups nothing can re-read (no adapter, or not an article of a law).
    sin_adaptador: list[str] = Field(default_factory=list)
    errores: list[str] = Field(default_factory=list)
    # Groups an adapter maintains but the loop does not take on by itself, with the reason (a superseded wording
    # only the operator may name, figures computed from others).
    fuera_de_alcance: list[str] = Field(default_factory=list)


def _ficheros(data_dir: Path, ejercicios: Sequence[int] | None = None) -> dict[int, dict[str, Any]]:
    disponibles = ejercicios_en(data_dir, FICHERO_IRPF)
    return {
        ejercicio: _json(data_dir / FICHERO_IRPF.format(ejercicio=ejercicio))
        for ejercicio in (disponibles if ejercicios is None else [e for e in ejercicios if e in disponibles])
    }


def _reconocer(ruta: Sequence[str], raw: dict[str, Any]) -> tuple[Adaptador, list[str]] | None:
    """The adapter that maintains the group at ``ruta`` and its path in stable form (lists by key, not by index)."""
    if len(ruta) == 4 and ruta[0] == "comunidades" and ruta[2] == "deducciones_calculables":
        if "=" in ruta[3]:
            return DEDUCCIONES, list(ruta)
        nodo = leer_ruta(raw, ruta)
        return (DEDUCCIONES, [*ruta[:3], f"articulo={nodo['articulo']}"]) if isinstance(nodo, dict) else None
    if (len(ruta) == 1 and ruta[0] in _ESCALAS_ESTATALES) or (
        len(ruta) == 3 and ruta[0] == "comunidades" and ruta[2] == "escala_general"
    ):
        return ESCALAS, list(ruta)
    for adaptador in CIFRAS.values():
        reconocida = adaptador.reconocer(ruta, raw)
        if reconocida is not None:
            return adaptador, reconocida
    return None


def es_del_tipo(tipo: str, tipos: Sequence[str] | None) -> bool:
    """``tipos`` may name a kind ("cifras:minimos") or a whole family of kinds ("cifras")."""
    return not tipos or tipo in tipos or tipo.partition(":")[0] in tipos


def construir_objetivo(
    adaptador: Adaptador,
    ruta: list[str],
    ficheros: dict[int, dict[str, Any]],
    *,
    data_dir: Path = DATA_DIR,
    origen: str,
    motivo: Sequence[str] = (),
    version: str | None = None,
    anotaciones: str = "",
) -> Objetivo:
    """The group at ``ruta`` in every year of ``ficheros``. Raises RecodificacionError when it cannot be located in the law."""
    if not ficheros:
        raise RecodificacionError("No hay ningún fichero de parámetros para los ejercicios pedidos.")
    if adaptador is not DEDUCCIONES and adaptador is not ESCALAS:
        return adaptador.objetivo(
            ruta, ficheros, data_dir=data_dir, origen=origen, motivo=motivo, version=version, anotaciones=anotaciones
        )
    ultimo = max(ficheros)
    antes = {ejercicio: leer_ruta(raw, ruta) for ejercicio, raw in ficheros.items()}
    presente = next((antes[e] for e in sorted(antes, reverse=True) if antes[e] is not None), None)
    if ruta[0] == "comunidades":
        comunidad = ficheros[ultimo]["comunidades"].get(ruta[1])
        if comunidad is None:
            raise RecodificacionError(f"No hay ninguna comunidad «{ruta[1]}» en los parámetros de {ultimo}.")
        ambito, nombre, boe_id = ruta[1], comunidad["nombre"], comunidad["fuente_principal"]["boe_id"]
    else:
        ambito, nombre, boe_id = "estado", "Estado", ficheros[ultimo]["fuente_principal"]["boe_id"]
    if adaptador is DEDUCCIONES:
        articulo = ruta[-1].partition("=")[2]
        etiqueta = f"{nombre}: {articulo} {(presente or {}).get('titulo', '')}".strip()
        # In a very long article only the deduction's own apartado is read (see control.acotar).
        base = ruta_de_etiqueta(articulo)
        referencia = articulo
    else:
        referencia = str((presente or {}).get("fuente", {}).get("referencia", ""))
        etiqueta = f"{nombre}: {ruta[-1].replace('_', ' ')} ({referencia.split(' (')[0]})"
        base = articulo_base(referencia)
    if base is None:
        raise RecodificacionError(f"{etiqueta}: no se sabe qué artículo de la ley leer («{referencia}»).")
    objetivo = Objetivo(
        tipo=adaptador.tipo,
        fichero=adaptador.fichero,
        ruta=ruta,
        etiqueta=etiqueta,
        ambito=ambito,
        boe_id=boe_id,
        version=version,
        articulos_ley=[base],
        ejercicios=sorted(ficheros),
        antes=antes,
        origen=origen,
        motivo=list(dict.fromkeys(motivo)),
        anotaciones=anotaciones,
    )
    con_grupo = [e for e in sorted(antes, reverse=True) if antes[e] is not None]
    if con_grupo:
        objetivo.citas_antes = adaptador.citas_de(data_dir, objetivo, con_grupo[0])
    return objetivo


def _fallidas(raw: dict[str, Any], data_dir: Path, texto_de: TextoDe) -> list[Resultado]:
    checks = comprobaciones(
        IRPFParams.model_validate(raw),
        evidencia=_json(data_dir / EVIDENCIA_DEDUCCIONES),
        evidencia_parametros=_json(data_dir / EVIDENCIA_PARAMETROS),
    )
    return [r for r in verificar(checks, texto_de) if not r.ok]


def _subgrupos_que_fallan(raw: dict[str, Any], resultado: Resultado) -> list[tuple[Adaptador, list[str], list[str]]]:
    """A failing check over a block of several groups (the Madrid deductions): the groups whose own figures are gone."""
    ruta = resultado.comprobacion.ruta
    bloque = leer_ruta(raw, ruta)
    out: list[tuple[Adaptador, list[str], list[str]]] = []
    for campo in bloque if isinstance(bloque, dict) else []:
        reconocido = _reconocer([*ruta, campo], raw)
        if reconocido is None:
            continue
        adaptador = reconocido[0]
        assert isinstance(adaptador, GrupoDeCifras)
        try:
            modelo = adaptador.cifras.modelo.model_validate(bloque[campo])
        except ValidationError:
            continue
        ausentes = [f for f in resultado.faltan if f in {str(c) for c in _cifras_de(modelo)}]
        if ausentes:
            out.append((adaptador, reconocido[1], ausentes))
    return out


def _otros_modulos() -> list[Any]:
    """Modules that register adapters for other parameter files and know how to find their own targets."""
    modulos: list[Any] = []
    for adaptador in adaptadores_registrados():
        modulo = sys.modules.get(type(adaptador).__module__)
        if adaptador.fichero != FICHERO_IRPF and modulo is not None and modulo not in modulos:
            modulos.append(modulo)
    return modulos


def _de_adaptadores_genericos(
    ejercicios: Sequence[int] | None,
    texto_de: TextoDe,
    *,
    data_dir: Path,
    ambito: str | None,
    tipos: Sequence[str] | None,
) -> Hallazgo:
    """Failing groups of adapters that describe themselves (``grupos``/``objetivo``) and have no module-level finder."""
    from hacienda_gpt.tax.verificacion import _comprimido

    hallazgo = Hallazgo()
    for adaptador in adaptadores_registrados():
        modulo = sys.modules.get(type(adaptador).__module__)
        if adaptador.fichero == FICHERO_IRPF or callable(getattr(modulo, "objetivos_de_verificacion", None)):
            continue
        if not es_del_tipo(adaptador.tipo, tipos):
            continue
        disponibles = ejercicios_en(data_dir, adaptador.fichero)
        ficheros = {
            e: _json(data_dir / adaptador.fichero.format(ejercicio=e))
            for e in disponibles
            if ejercicios is None or e in ejercicios
        }
        rutas = list(dict.fromkeys(tuple(r) for raw in ficheros.values() for r in adaptador.grupos(raw)))
        for ruta in rutas:
            try:
                objetivo = adaptador.objetivo(list(ruta), ficheros, data_dir=data_dir, origen="verificacion")
            except RecodificacionError as exc:
                hallazgo.fuera_de_alcance.append(str(exc))
                continue
            if ambito and objetivo.ambito != ambito:
                continue
            motivo: list[str] = []
            for ejercicio in sorted(e for e, valor in objetivo.antes.items() if valor is not None):
                propios = adaptador.desajustes(objetivo, ejercicio, texto_de, data_dir=data_dir)
                if propios is None:
                    pagina = texto_de(objetivo.boe_id, objetivo.version)
                    if not pagina:
                        hallazgo.errores.append(f"{ejercicio} {objetivo.etiqueta}: no se pudo leer {objetivo.boe_id}")
                        continue
                    comprimido = _comprimido(texto_plano(pagina))
                    citas = adaptador.citas_de(data_dir, objetivo, ejercicio)
                    if not citas:
                        hallazgo.fuera_de_alcance.append(f"{ejercicio} {objetivo.etiqueta}: sin citas que comprobar")
                    propios = [c[:120] for c in citas if _comprimido(c) not in comprimido]
                motivo += [f"{ejercicio}: ya no está en el texto «{falta}»" for falta in propios[:4]]
            if motivo:
                objetivo.motivo = list(dict.fromkeys(motivo))
                hallazgo.objetivos.append(objetivo)
    return hallazgo


def _sumar(hallazgo: Hallazgo, otro: Any) -> None:
    hallazgo.objetivos += list(getattr(otro, "objetivos", []))
    hallazgo.sin_adaptador += list(getattr(otro, "sin_adaptador", []))
    hallazgo.errores += list(getattr(otro, "errores", []))
    hallazgo.fuera_de_alcance += list(getattr(otro, "fuera_de_alcance", []))


def objetivos_de_verificacion(
    ejercicios: Sequence[int] | None,
    texto_de: TextoDe,
    *,
    data_dir: Path = DATA_DIR,
    ambito: str | None = None,
    articulo: str | None = None,
    tipos: Sequence[str] | None = None,
    solo_irpf: bool = False,
) -> Hallazgo:
    """Groups whose check against the law fails in any of ``ejercicios``, each with every year asked for.

    Every parameter file with a registered adapter is looked at: the IRPF one here, the others through the module
    that registers their adapters (its own ``objetivos_de_verificacion``) or, failing that, through what each
    adapter says about itself (``Adaptador.grupos`` / ``objetivo`` / ``desajustes``).

    A group that fails in one year is offered with ALL the years: which of them the new wording reaches is decided
    later, from its effects date, never from which check happened to fail.
    """
    hallazgo = Hallazgo()
    ficheros = _ficheros(data_dir, ejercicios)
    encontrados: dict[tuple[str, tuple[str, ...]], tuple[Adaptador, list[str]]] = {}
    for ejercicio, raw in ficheros.items():
        for resultado in _fallidas(raw, data_dir, texto_de):
            check = resultado.comprobacion
            ambito_del_grupo = check.ruta[1] if check.ruta[0] == "comunidades" else "estado"
            if ambito and ambito_del_grupo != ambito:
                continue
            if resultado.faltan == ["sin texto"]:
                hallazgo.errores.append(f"{ejercicio} {check.descripcion}: no se pudo leer {check.boe_id}")
                continue
            reconocido = _reconocer(check.ruta, raw)
            if reconocido is not None:
                suyos = [(reconocido[0], reconocido[1], resultado.faltan)]
            else:
                suyos = _subgrupos_que_fallan(raw, resultado)
                if not suyos:
                    hallazgo.sin_adaptador.append(f"{ejercicio} {'/'.join(check.ruta)}: {check.descripcion}")
            for adaptador, ruta, faltan in suyos:
                if not es_del_tipo(adaptador.tipo, tipos) or (articulo and not _es_el_articulo(ruta, articulo)):
                    continue
                encontrados.setdefault((adaptador.tipo, tuple(ruta)), (adaptador, []))[1].extend(
                    f"{ejercicio}: ya no está en el texto «{falta}»" for falta in faltan[:4]
                )
    for (_, ruta_t), (adaptador, motivo) in encontrados.items():
        try:
            hallazgo.objetivos.append(
                construir_objetivo(
                    adaptador, list(ruta_t), ficheros, data_dir=data_dir, origen="verificacion", motivo=motivo
                )
            )
        except RecodificacionError as exc:
            hallazgo.fuera_de_alcance.append(str(exc))
    if solo_irpf or articulo:
        return hallazgo
    for modulo in _otros_modulos():
        buscar = getattr(modulo, "objetivos_de_verificacion", None)
        if callable(buscar):
            _sumar(hallazgo, buscar(ejercicios, texto_de, data_dir=data_dir, ambito=ambito, tipos=tipos))
    _sumar(hallazgo, _de_adaptadores_genericos(ejercicios, texto_de, data_dir=data_dir, ambito=ambito, tipos=tipos))
    return hallazgo


def _es_el_articulo(ruta: Sequence[str], articulo: str) -> bool:
    """Is the deduction at ``ruta`` the one asked for? "art. 5" asks for every deduction of article 5."""
    if "=" not in ruta[-1]:
        return False
    propio = ruta[-1].partition("=")[2]
    pedido = " ".join(articulo.split())
    if propio == pedido:
        return True
    base = articulo_base(pedido)
    desnudo = re.sub(r"^art(?:iculo|\.)?\s*", "", " ".join(_sin_acentos(pedido).lower().split()))
    return base is not None and desnudo == base and articulo_base(propio) == base


def objetivos_de_articulo(
    ejercicios: Sequence[int] | None,
    *,
    comunidad: str,
    articulo: str,
    data_dir: Path = DATA_DIR,
    version: str | None = None,
    anotaciones: str = "",
) -> Hallazgo:
    """Every encoded deduction of ``articulo`` in ``comunidad`` whether or not its check fails; if none, a new one."""
    hallazgo = Hallazgo()
    ficheros = _ficheros(data_dir, ejercicios)
    rutas: list[list[str]] = []
    for raw in ficheros.values():
        for d in raw.get("comunidades", {}).get(comunidad, {}).get("deducciones_calculables", []):
            ruta = ["comunidades", comunidad, "deducciones_calculables", f"articulo={d['articulo']}"]
            if _es_el_articulo(ruta, articulo) and ruta not in rutas:
                rutas.append(ruta)
    nueva = not rutas
    if nueva:
        rutas = [["comunidades", comunidad, "deducciones_calculables", f"articulo={' '.join(articulo.split())}"]]
    for ruta in rutas:
        try:
            objetivo = construir_objetivo(
                DEDUCCIONES,
                ruta,
                ficheros,
                data_dir=data_dir,
                origen="articulo",
                motivo=["deducción nueva: no estaba codificada"] if nueva else ["revisión pedida por el operador"],
                version=version,
                anotaciones=anotaciones,
            )
        except RecodificacionError as exc:
            hallazgo.errores.append(str(exc))
            continue
        hallazgo.objetivos.append(objetivo)
    return hallazgo


def objetivos_de_desglose(
    ejercicios: Sequence[int] | None,
    *,
    comunidad: str,
    articulo: str,
    lector: Any,
    texto_de: TextoDe,
    data_dir: Path = DATA_DIR,
    version: str | None = None,
    anotaciones: str = "",
    norma_texto: str | None = None,
    ley_citada: str = "",
) -> tuple[Hallazgo, Desglose | None]:
    """Split an article into the deductions it holds (one reader call) and offer the ones NOT encoded yet as new.

    For an article that creates deductions nobody has encoded: the reader only lists them, each with a literal
    quote; every item the program accepts becomes a target of the normal loop under its own label ("art. 5.Dos").
    Deductions already encoded under that label are left to the normal verification.
    """
    hallazgo = Hallazgo()
    ficheros = _ficheros(data_dir, ejercicios)
    etiqueta_articulo = " ".join(articulo.split())
    raiz = ["comunidades", comunidad, "deducciones_calculables", f"articulo={etiqueta_articulo}"]
    try:
        entero = construir_objetivo(
            DEDUCCIONES, raiz, ficheros, data_dir=data_dir, origen="desglose", version=version, anotaciones=anotaciones
        )
    except RecodificacionError as exc:
        hallazgo.errores.append(str(exc))
        return hallazgo, None
    # The whole article is read to list what it holds, however long it is.
    base = articulo_base(etiqueta_articulo)
    assert base is not None
    entero.articulos_ley, entero.norma_texto, entero.ley_citada = [base], norma_texto, ley_citada
    desglose = desglosar(entero, lector=lector, texto_de=texto_de, etiqueta_articulo=etiqueta_articulo)
    hallazgo.errores += [f"{entero.etiqueta}: {d}" for d in desglose.descartados]
    codificadas = {
        d["articulo"]
        for raw in ficheros.values()
        for d in raw.get("comunidades", {}).get(comunidad, {}).get("deducciones_calculables", [])
    }
    for elemento in desglose.elementos:
        if elemento.etiqueta in codificadas:
            continue
        ruta = [*raiz[:3], f"articulo={elemento.etiqueta}"]
        try:
            objetivo = construir_objetivo(
                DEDUCCIONES,
                ruta,
                ficheros,
                data_dir=data_dir,
                origen="desglose",
                motivo=[f"deducción nueva: no estaba codificada («{elemento.cita[:120]}»)"],
                version=version,
                anotaciones=anotaciones,
            )
        except RecodificacionError as exc:
            hallazgo.errores.append(str(exc))
            continue
        objetivo.etiqueta = f"{objetivo.etiqueta} {elemento.titulo}".strip()
        objetivo.norma_texto, objetivo.ley_citada = norma_texto, ley_citada
        hallazgo.objetivos.append(objetivo)
    return hallazgo, desglose


def objetivo_de_ruta(
    ejercicios: Sequence[int] | None,
    ruta: Sequence[str],
    *,
    data_dir: Path = DATA_DIR,
    version: str | None = None,
    anotaciones: str = "",
) -> Objetivo:
    """The group at ``ruta``, asked for by the operator.

    "comunidades/galicia/escala_general" or "minimos" name a group of the IRPF file; a group of another parameter
    file carries the name of its family before a colon: "sociedades:tipos_gravamen".
    """
    familia, hay_familia, primero = ruta[0].partition(":") if ruta else ("", "", "")
    if hay_familia and familia != "irpf":
        for modulo in _otros_modulos():
            buscar = getattr(modulo, "objetivo_de_ruta", None)
            if callable(buscar):
                objetivo: Objetivo = buscar(
                    familia,
                    ejercicios,
                    [primero, *ruta[1:]],
                    data_dir=data_dir,
                    version=version,
                    anotaciones=anotaciones,
                )
                return objetivo
        raise RecodificacionError(f"Ningún adaptador de recodificación mantiene los ficheros de «{familia}».")
    ruta = [primero, *ruta[1:]] if hay_familia else list(ruta)
    ficheros = _ficheros(data_dir, ejercicios)
    if not ficheros:
        raise RecodificacionError("No hay ningún fichero de parámetros para los ejercicios pedidos.")
    reconocido = _reconocer(ruta, ficheros[max(ficheros)])
    if reconocido is None:
        raise RecodificacionError(f"Ningún adaptador de recodificación mantiene «{'/'.join(ruta)}».")
    return construir_objetivo(
        reconocido[0],
        reconocido[1],
        ficheros,
        data_dir=data_dir,
        origen="articulo",
        motivo=["revisión pedida por el operador"],
        version=version,
        anotaciones=anotaciones,
    )


def todos_los_objetivos(
    ejercicios: Sequence[int] | None = None,
    *,
    data_dir: Path = DATA_DIR,
    boe_id: str | None = None,
    solo_irpf: bool = False,
) -> Hallazgo:
    """Every group an adapter maintains, as a target, whether or not it matches its law (optionally of one law).

    For asking "which groups hang from this law, and from which of its articles?" when a norm amends it, and for
    measuring the loop against the shipped encodings.
    """
    hallazgo = Hallazgo()
    ficheros = _ficheros(data_dir, ejercicios)
    vistas: set[tuple[str, ...]] = set()
    for raw in ficheros.values():
        for adaptador in _DEL_IRPF:
            for ruta in adaptador.grupos(raw):
                if tuple(ruta) in vistas:
                    continue
                vistas.add(tuple(ruta))
                try:
                    objetivo = construir_objetivo(adaptador, ruta, ficheros, data_dir=data_dir, origen="norma")
                except RecodificacionError as exc:
                    hallazgo.fuera_de_alcance.append(str(exc))
                    continue
                if boe_id is None or objetivo.boe_id == boe_id:
                    hallazgo.objetivos.append(objetivo)
    if solo_irpf:
        return hallazgo
    for modulo in _otros_modulos():
        _sumar(hallazgo, _todos_los_de(modulo, ejercicios, data_dir=data_dir, boe_id=boe_id))
    return hallazgo


def _todos_los_de(modulo: Any, ejercicios: Sequence[int] | None, *, data_dir: Path, boe_id: str | None) -> Hallazgo:
    """The same for a module of other parameter files: its own ``todos_los_objetivos`` or, failing that, its pieces."""
    propio = getattr(modulo, "todos_los_objetivos", None)
    if callable(propio):
        resultado: Hallazgo = propio(ejercicios, data_dir=data_dir, boe_id=boe_id)
        return resultado
    hallazgo = Hallazgo()
    familias, rutas_de, construir = (
        getattr(modulo, nombre, None) for nombre in ("FAMILIAS", "rutas_de_grupos", "construir_objetivo")
    )
    if familias is None or not callable(rutas_de) or not callable(construir):
        return hallazgo
    for familia in familias:
        disponibles = ejercicios_en(data_dir, familia.fichero)
        ficheros = {
            e: _json(data_dir / familia.fichero.format(ejercicio=e))
            for e in disponibles
            if ejercicios is None or e in ejercicios
        }
        for ruta in list(dict.fromkeys(tuple(r) for raw in ficheros.values() for r in rutas_de(raw))):
            leyes: list[str | None] = [boe_id]
            if boe_id is not None:
                # Only when some quote of the group comes from that law.
                grupo = next((leer_ruta(raw, ruta) for raw in ficheros.values() if leer_ruta(raw, ruta)), None)
                citas = grupo.get("evidencia", []) if isinstance(grupo, dict) else []
                if not any(isinstance(c, dict) and c.get("boe_id") == boe_id for c in citas):
                    leyes = []
            for ley in leyes:
                try:
                    hallazgo.objetivos.append(construir(familia, list(ruta), ficheros, boe_id=ley, origen="norma"))
                except (RecodificacionError, ValidationError) as exc:
                    hallazgo.fuera_de_alcance.append(str(exc)[:300])
    return hallazgo


def objetivos_de_rollover(origen: int, destino: int, *, data_dir: Path = DATA_DIR) -> Hallazgo:
    """Deductions the rollover left out of ``destino``: each may be re-encoded from the current text for that year."""
    hallazgo = Hallazgo()
    ficheros = _ficheros(data_dir, [origen, destino])
    if set(ficheros) != {origen, destino}:
        hallazgo.errores.append(f"Hacen falta los ficheros de {origen} y de {destino}.")
        return hallazgo
    for clave, comunidad in ficheros[origen].get("comunidades", {}).items():
        quedan = [
            d["articulo"]
            for d in ficheros[destino].get("comunidades", {}).get(clave, {}).get("deducciones_calculables", [])
        ]
        anterior = ""
        for d in comunidad.get("deducciones_calculables", []):
            if d["articulo"] in quedan:
                anterior = d["articulo"]
                continue
            ruta = ["comunidades", clave, "deducciones_calculables", f"articulo={d['articulo']}"]
            try:
                objetivo = construir_objetivo(
                    DEDUCCIONES,
                    ruta,
                    {destino: ficheros[destino]},
                    data_dir=data_dir,
                    origen="rollover",
                    motivo=[f"retirada al abrir {destino}: ya no coincidía con el texto vigente"],
                )
            except RecodificacionError as exc:
                hallazgo.errores.append(str(exc))
                continue
            objetivo.etiqueta = f"{comunidad['nombre']}: {d['articulo']} {d.get('titulo', '')}".strip()
            objetivo.citas_antes = DEDUCCIONES.citas_de(data_dir, objetivo, origen)
            objetivo.referencia_anterior = d
            objetivo.insertar_tras = anterior
            hallazgo.objetivos.append(objetivo)
    return hallazgo


def control_de_lo_publicado(
    texto_de: TextoDe | None = None, *, data_dir: Path = DATA_DIR, ejercicios: Sequence[int] | None = None
) -> list[dict[str, Any]]:
    """Run the gate over every shipped regional deduction with its stored quotes; returns the ones that fail.

    Without ``texto_de`` only what needs no law is checked (the model, every figure inside a quote, the figures of
    the texts the user reads). With it, the quotes must also be literal, each in one piece, in the articles the loop
    would read — on the version of the law the year's quotes were taken from.
    """
    from hacienda_gpt.tax.recodificacion.control import articulos_que_contienen, citas_no_literales, extraer_articulos

    evidencia = _json(data_dir / EVIDENCIA_DEDUCCIONES)
    fallidas: list[dict[str, Any]] = []
    for ejercicio, raw in _ficheros(data_dir, ejercicios).items():
        for ruta in DEDUCCIONES.grupos(raw):
            clave, articulo = DeduccionesAutonomicas._donde(ruta)
            objetivo = construir_objetivo(DEDUCCIONES, ruta, {ejercicio: raw}, data_dir=data_dir, origen="control")
            _, control = DEDUCCIONES.controlar(
                objetivo.antes[ejercicio], objetivo.citas_antes, objetivo, efectos_desde=None, datos={ejercicio: raw}
            )
            fallos = list(control.fallos)
            if texto_de is not None:
                propia = evidencia.get(f"{clave}@{ejercicio}", {})
                version = propia.get("_version") if articulo in propia else None
                pagina = texto_de(objetivo.boe_id, version)
                if not pagina:
                    fallos.append(f"no se pudo leer {objetivo.boe_id} (versión {version or 'vigente'})")
                else:
                    propios = [" ".join(a.lower().split()) for a in objetivo.articulos_ley]
                    otros = articulos_que_contienen(pagina, objetivo.citas_antes, junto_a=propios)
                    texto = extraer_articulos(pagina, [*propios, *otros])
                    fallos += citas_no_literales(objetivo.citas_antes, texto.comprimido)
            if fallos:
                fallidas.append({"ejercicio": ejercicio, "comunidad": clave, "articulo": articulo, "fallos": fallos})
    return fallidas


__all__ = [
    "CIFRAS",
    "DEDUCCIONES",
    "ESCALAS",
    "Cifras",
    "EscalaLeida",
    "GrupoDeCifras",
    "Hallazgo",
    "construir_objetivo",
    "control_de_lo_publicado",
    "es_del_tipo",
    "objetivo_de_ruta",
    "objetivos_de_articulo",
    "objetivos_de_desglose",
    "objetivos_de_rollover",
    "objetivos_de_verificacion",
    "todos_los_objetivos",
]
