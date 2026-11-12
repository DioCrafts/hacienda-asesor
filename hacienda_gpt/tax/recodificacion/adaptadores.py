"""The kinds of parameter group the re-encoding loop knows how to maintain.

Two adapters so far, both over the IRPF parameter files:

* ``deduccion_autonomica`` — one regional deduction computed from data
  (``DeduccionCalculable``), with its quotes in ``evidencia_deducciones.json``;
* ``escala`` — a progressive scale (``Escala``): the regional general scales and
  the State's general and savings scales.

Adding a kind means writing an :class:`~hacienda_gpt.tax.recodificacion.nucleo.Adaptador`
and registering it; the loop, the proposals and the commands do not change.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import json
from pathlib import Path
import re
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
    BaseLimite,
    DeduccionCalculable,
    Escala,
    Fuente,
    IRPFParams,
    Tramo,
)
from hacienda_gpt.tax.recodificacion import prompts
from hacienda_gpt.tax.recodificacion.control import (
    CENT,
    Control,
    Reglas,
    _sin_acentos,
    articulo_base,
    cifras_sin_respaldo,
    normalizar_evidencia,
    notas_de_cifra_derivada,
    numeros_de_texto_sin_respaldo,
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
from hacienda_gpt.tax.verificacion import Resultado, TextoDe, comprobaciones, verificar

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


class Escalas(Adaptador):
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

    @staticmethod
    def _clave(ruta: Sequence[str]) -> str:
        return "/".join(ruta)

    def citas_de(self, data_dir: Path, objetivo: Objetivo, ejercicio: int) -> list[str]:
        fichero = _json(data_dir / EVIDENCIA_PARAMETROS).get(objetivo.fichero.format(ejercicio=ejercicio), {})
        return list(fichero.get(self._clave(objetivo.ruta), {}).get("citas", []))

    def guardar_citas(self, data_dir: Path, propuesta: Propuesta, ficheros: dict[int, dict[str, Any]]) -> None:
        path = data_dir / EVIDENCIA_PARAMETROS
        evidencia: dict[str, dict[str, Any]] = _json(path)
        for ejercicio in propuesta.ejercicios:
            evidencia.setdefault(propuesta.fichero.format(ejercicio=ejercicio), {})[self._clave(propuesta.ruta)] = {
                "boe_id": propuesta.boe_id,
                "version": propuesta.version,
                "efectos_desde": propuesta.efectos_desde,
                "cita_efectos": propuesta.cita_efectos,
                "citas": propuesta.citas,
                "propuesta": propuesta.id,
            }
        _guardar_json(path, evidencia)

    def _rutas(self, raw: dict[str, Any]) -> list[list[str]]:
        rutas = [[nombre] for nombre in _ESCALAS_ESTATALES if nombre in raw]
        return rutas + [["comunidades", clave, "escala_general"] for clave in raw.get("comunidades", {})]

    def pendientes(self, raw: dict[str, Any]) -> list[tuple[list[str], str]]:
        out: list[tuple[list[str], str]] = []
        for ruta in self._rutas(raw):
            fuente = (leer_ruta(raw, ruta) or {}).get("fuente", {})
            if not fuente.get("verificado") and str(fuente.get("nota") or "").startswith(MARCA_ESCALA):
                out.append((ruta, f"{'/'.join(ruta)} ({fuente.get('referencia', '')})"))
        return out

    def confirmar(self, raw: dict[str, Any], ruta: Sequence[str]) -> None:
        fuente = (leer_ruta(raw, ruta) or {}).get("fuente")
        if isinstance(fuente, dict):
            fuente["verificado"] = True
            fuente.pop("nota", None)


DEDUCCIONES = registrar(DeduccionesAutonomicas())
ESCALAS = registrar(Escalas())

# --------------------------------------------------------------------------- #
# Finding what to re-read
# --------------------------------------------------------------------------- #


class Hallazgo(BaseModel):
    """What a look at the parameter files found to re-read, and what it could not take on."""

    model_config = ConfigDict(extra="forbid")

    objetivos: list[Objetivo] = Field(default_factory=list)
    # Failing groups of a kind no adapter maintains yet.
    sin_adaptador: list[str] = Field(default_factory=list)
    errores: list[str] = Field(default_factory=list)


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
    return None


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
        referencia = articulo
        etiqueta = f"{nombre}: {articulo} {(presente or {}).get('titulo', '')}".strip()
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
    checks = comprobaciones(IRPFParams.model_validate(raw), evidencia=_json(data_dir / EVIDENCIA_DEDUCCIONES))
    return [r for r in verificar(checks, texto_de) if not r.ok]


def objetivos_de_verificacion(
    ejercicios: Sequence[int],
    texto_de: TextoDe,
    *,
    data_dir: Path = DATA_DIR,
    ambito: str | None = None,
    articulo: str | None = None,
    tipos: Sequence[str] | None = None,
) -> Hallazgo:
    """Groups whose check against the law fails in any of ``ejercicios``, each with every year asked for.

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
            if reconocido is None:
                hallazgo.sin_adaptador.append(f"{ejercicio} {'/'.join(check.ruta)}: {check.descripcion}")
                continue
            adaptador, ruta = reconocido
            if (tipos and adaptador.tipo not in tipos) or (articulo and not _es_el_articulo(ruta, articulo)):
                continue
            encontrados.setdefault((adaptador.tipo, tuple(ruta)), (adaptador, []))[1].extend(
                f"{ejercicio}: ya no está en el texto «{falta}»" for falta in resultado.faltan[:4]
            )
    for (_, ruta_t), (adaptador, motivo) in encontrados.items():
        try:
            hallazgo.objetivos.append(
                construir_objetivo(
                    adaptador, list(ruta_t), ficheros, data_dir=data_dir, origen="verificacion", motivo=motivo
                )
            )
        except RecodificacionError as exc:
            hallazgo.errores.append(str(exc))
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
    ejercicios: Sequence[int],
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


def objetivo_de_ruta(
    ejercicios: Sequence[int],
    ruta: Sequence[str],
    *,
    data_dir: Path = DATA_DIR,
    version: str | None = None,
    anotaciones: str = "",
) -> Objetivo:
    """The group at ``ruta`` ("comunidades/galicia/escala_general"), asked for by the operator."""
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


__all__ = [
    "DEDUCCIONES",
    "ESCALAS",
    "EscalaLeida",
    "Hallazgo",
    "construir_objetivo",
    "objetivo_de_ruta",
    "objetivos_de_articulo",
    "objetivos_de_rollover",
    "objetivos_de_verificacion",
]
