"""Tools the agent can call, and their execution against local state.

Tools are deliberately few and typed. Everything that must be *exact* — what
the law says, what the user declared, which rules fire — lives behind a tool,
so the model orchestrates and reasons but never has to recall a figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
import logging
from typing import Any

from hacienda_gpt.agent.evidence import SOURCE_TYPES, EvidenceLedger, Fragment
from hacienda_gpt.agent.providers.base import ToolCall, ToolSpec
from hacienda_gpt.agent.search import CorpusSearcher, SearchFilters, normalize_date
from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE, KNOWN_FACT_NAMES, _coerce_value
from hacienda_gpt.decision.interpreter import _merge_facts
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import CaseState, Fact, parse_fiscal_year
from hacienda_gpt.doctrine.citas import extract_citations, identify_document
from hacienda_gpt.doctrine.grafo import DoctrineGraph
from hacienda_gpt.tax.beneficios import evaluar_beneficios
from hacienda_gpt.tax.calendario import aviso_festivos, proximos_vencimientos
from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult
from hacienda_gpt.tax.params import EjercicioNoDisponibleError, ejercicios_disponibles, load_params
from hacienda_gpt.tax.profile import DatosInvalidosError, build_input
from hacienda_gpt.tax.simulador import simular

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 10

SEARCH_TOOL = ToolSpec(
    name="buscar_fuentes",
    description=(
        "Busca en el corpus fiscal español (legislación consolidada del BOE, consultas vinculantes de la DGT, "
        "resoluciones del TEAC, jurisprudencia y contenido de la AEAT) y devuelve fragmentos literales, cada uno con "
        "un identificador citable [F#], su autoridad, fecha y si es vinculante. Úsala antes de afirmar cualquier "
        "norma, cifra, plazo o criterio. La búsqueda es semántica: escribe la consulta como una frase descriptiva "
        "con los términos técnicos de la norma (p. ej. 'exención por reinversión en vivienda habitual artículo 38 "
        "LIRPF requisitos y plazo'), no como palabras sueltas. Usa `tipo_fuente` para pedir expresamente la ley, la "
        "jurisprudencia o la doctrina administrativa sobre un punto ya localizado."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "consulta": {"type": "string", "description": "Qué buscar, en español, con terminología técnica."},
            "tipo_fuente": {
                "type": "string",
                "enum": list(SOURCE_TYPES),
                "description": (
                    "Restringe a un tipo: 'normativa' (leyes y reglamentos, BOE y boletines autonómicos), "
                    "'jurisprudencia' (TS, TC), 'doctrina_administrativa' (DGT, TEAC) o 'aeat' (manuales y páginas "
                    "informativas). Omítelo para buscar en todo."
                ),
            },
            "tributo": {
                "type": "string",
                "description": "Tributo al que acotar si procede: IRPF, IVA, ISD, ITP_AJD, IS, IP, IIVTNU…",
            },
            "ambito": {
                "type": "string",
                "description": (
                    "'estatal', o 'autonomico:<comunidad>' para normativa propia de una comunidad autónoma "
                    "(la estatal se incluye siempre)."
                ),
            },
            "solo_vinculante": {
                "type": "boolean",
                "description": "true para devolver solo doctrina marcada como vinculante.",
            },
            "desde_fecha": {
                "type": "string",
                "description": (
                    "AAAA o AAAA-MM-DD. Descarta consultas, resoluciones y sentencias anteriores a esa fecha; úsalo "
                    "cuando una reforma o una sentencia posterior haya podido dejar superada la doctrina antigua. "
                    "La legislación consolidada (sin fecha de resolución) se mantiene siempre."
                ),
            },
            "max_resultados": {"type": "integer", "description": f"Entre 1 y {MAX_SEARCH_RESULTS}. Por defecto 6."},
        },
        "required": ["consulta"],
    },
)

READ_CASE_TOOL = ToolSpec(
    name="consultar_expediente",
    description=(
        "Devuelve el expediente fiscal del usuario: ejercicio, hechos ya conocidos (residencia, tipo de renta, "
        "importes, actividad…), datos que faltan y obligaciones candidatas detectadas. Consúltalo antes de "
        "preguntar al usuario algo que quizá ya haya dicho."
    ),
    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
)

RECORD_FACTS_TOOL = ToolSpec(
    name="registrar_hechos",
    description=(
        "Guarda en el expediente hechos que el usuario ha declarado expresamente en esta conversación (o que ha "
        "corregido). No registres inferencias ni supuestos tuyos. Valores esperados: residencia_fiscal 'ES' o "
        "código ISO del país; periodo_fiscal año de 4 dígitos; importes en euros como número; booleanos true/false."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "hechos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "nombre": {"type": "string", "enum": list(KNOWN_FACT_NAMES)},
                        "valor": {"type": ["string", "number", "boolean"]},
                    },
                    "required": ["nombre", "valor"],
                },
            }
        },
        "required": ["hechos"],
    },
)

EVALUATE_RULES_TOOL = ToolSpec(
    name="evaluar_obligaciones",
    description=(
        "Ejecuta el catálogo determinista de reglas sobre el expediente y devuelve las obligaciones tributarias "
        "candidatas, los hechos que faltan para confirmarlas y las fuentes normativas de cada regla. El catálogo es "
        "parcial: que una obligación no aparezca no significa que no exista."
    ),
    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
)


_INPUT_FIELDS_DOC = (
    "comunidad_autonoma, tributacion ('individual' | 'conjunta_biparental' | 'conjunta_monoparental'), edad, "
    "grado_discapacidad, num_descendientes, num_descendientes_menores_3, descendientes_compartidos (bool), "
    "num_ascendientes_mayores_65, num_ascendientes_mayores_75, rendimiento_trabajo_bruto, "
    "cotizaciones_seguridad_social, otros_gastos_deducibles_trabajo, rendimiento_actividad_neto, "
    "rendimiento_alquiler_neto, otras_rentas_base_general, rendimiento_capital_mobiliario, ganancias_patrimoniales, "
    "aportacion_plan_pensiones, contribucion_empresa_plan_pensiones, donativos, donativos_recurrentes (bool), "
    "pagos_vivienda_habitual_pre2013, hijos_deduccion_maternidad, gastos_guarderia, retenciones. Para residentes en "
    "Madrid, además: alquiler_vivienda_habitual, gastos_escolaridad, gastos_ensenanza_idiomas, "
    "gastos_uniformes_escolares, num_hijos_gastos_educativos, num_hijos_primer_ciclo_infantil, "
    "cotizaciones_empleada_hogar, hijos_nacidos_ultimos_3_ejercicios, donativos_fundaciones_madrid, familia_numerosa "
    "(bool), base_imponible_resto_unidad_familiar, miembros_unidad_familiar"
)

CALCULATE_TOOL = ToolSpec(
    name="calcular_irpf",
    description=(
        "Calcula la declaración del IRPF de forma determinista con los parámetros legales del ejercicio (escalas, "
        "mínimos, reducciones y deducciones estatales) y devuelve el desglose línea a línea con su referencia legal, "
        "como un fragmento citable [F#]. Úsala SIEMPRE que haga falta una cuota, un ahorro o una comparación: no "
        "calcules impuestos por tu cuenta. Parte de los datos del expediente del usuario si lo hay; `datos` añade o "
        "sustituye cifras. Para comparar alternativas (individual frente a conjunta, aportar o no a un plan de "
        "pensiones, vender este año o el siguiente), pasa `escenarios`: cada uno aplica sus `cambios` sobre los "
        "datos base y el resultado indica la diferencia con el primero. Importes anuales en euros. "
        f"Campos admitidos en `datos` y `cambios`: {_INPUT_FIELDS_DOC}. Lee los avisos del resultado: dicen qué no "
        "se ha modelado (en particular, la parte autonómica) y debes trasladar al usuario los que le afecten."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año fiscal. Por defecto, el del expediente."},
            "datos": {
                "type": "object",
                "description": "Cifras del contribuyente que añadir o corregir sobre el expediente.",
            },
            "escenarios": {
                "type": "array",
                "description": "Alternativas a comparar. Omítelo para un único cálculo.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "nombre": {"type": "string", "description": "Etiqueta corta, p. ej. 'Con plan de pensiones'."},
                        "cambios": {"type": "object", "description": "Campos que cambian respecto a los datos base."},
                    },
                    "required": ["nombre", "cambios"],
                },
            },
        },
    },
)

BENEFITS_TOOL = ToolSpec(
    name="listar_beneficios_fiscales",
    description=(
        "Revisa el catálogo de reducciones, mínimos y deducciones ESTATALES del IRPF contra la situación del "
        "usuario y devuelve, para cada uno, si ya está aplicado, si está disponible (con el ahorro estimado por la "
        "calculadora), o qué dato falta para saberlo. Es el punto de partida para responder a «¿cómo pago menos?». "
        "No incluye deducciones autonómicas: el resultado indica cómo buscarlas. Admite los mismos `datos` que "
        "calcular_irpf."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año fiscal. Por defecto, el del expediente."},
            "datos": {
                "type": "object",
                "description": "Cifras del contribuyente que añadir o corregir sobre el expediente.",
            },
        },
    },
)


CALENDAR_TOOL = ToolSpec(
    name="calendario_fiscal",
    description=(
        "Devuelve los próximos plazos fiscales que aplican al usuario según su expediente (fin del ejercicio para "
        "aportaciones y donativos, declaración de la Renta, IVA y pagos fraccionados si tiene actividad económica, "
        "modelo 720 si tiene bienes en el extranjero), con su fecha, días restantes y referencia legal, como un "
        "fragmento citable. Úsala cuando pregunte por plazos o cuando una recomendación dependa de una fecha."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {"dias": {"type": "integer", "description": "Horizonte en días (por defecto 365)."}},
    },
)


VALIDITY_TOOL = ToolSpec(
    name="consultar_vigencia",
    description=(
        "Consulta el grafo de doctrina para saber si una consulta de la DGT, una resolución del TEAC o una "
        "sentencia sigue vigente: qué resoluciones posteriores la confirman, matizan, superan o contradicen. "
        "Úsala antes de apoyarte en doctrina de hace años o cuando dos fuentes parezcan discrepar. El resultado es "
        "un indicio construido a partir de las citas entre resoluciones, no una fuente: para afirmar que algo está "
        "superado, busca y cita la resolución posterior con buscar_fuentes."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "referencia": {
                "type": "string",
                "description": (
                    "La resolución tal como se cita: 'V1234-19', 'resolución TEAC 00/1234/2017', "
                    "'STS de 3 de junio de 2020 (rec. 1234/2019)', 'STC 182/2021'."
                ),
            }
        },
        "required": ["referencia"],
    },
)

_RELACION_VERBO = {
    "cita": "la cita",
    "confirma": "la confirma",
    "matiza": "la matiza",
    "supera": "la supera",
    "contradice": "la contradice",
}


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    summary: str
    is_error: bool = False


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(content=message, summary=message[:120], is_error=True)


_CASE_ONLY_TOOLS = frozenset({READ_CASE_TOOL.name, RECORD_FACTS_TOOL.name, EVALUATE_RULES_TOOL.name})


def _money(value: Decimal) -> str:
    """1234.5 → '1.234,50 €' (Spanish formatting, sign preserved)."""
    text = f"{abs(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if value < 0 else ''}{text} €"


def _render_result(nombre: str, result: IRPFResult) -> str:
    lines = [f"Cálculo del IRPF {result.ejercicio} — {nombre} (tributación {result.tributacion.replace('_', ' ')})"]
    for line in result.lineas:
        ref = f" [{line.referencia}]" if line.referencia else ""
        lines.append(f"- {line.concepto}: {_money(line.importe)}{ref}")
    lines.append(f"- Tipo medio efectivo: {result.tipo_medio_efectivo} %")
    if result.avisos:
        lines.append("Avisos (lo que este cálculo no cubre o da por supuesto):")
        lines.extend(f"  * {aviso}" for aviso in result.avisos)
    lines.append(f"Parámetros: {result.fuente}.")
    return "\n".join(lines)


class Toolbox:
    """Holds the per-run state the tools read and mutate."""

    def __init__(
        self,
        *,
        searcher: CorpusSearcher,
        ledger: EvidenceLedger,
        case: CaseState | None = None,
        default_k: int = 6,
        graph: DoctrineGraph | None = None,
        allowed: frozenset[str] | None = None,
    ) -> None:
        # Subagents get a subset of the tools (a researcher must not edit the case).
        self._allowed = allowed
        self._graph = graph
        self._searcher = searcher
        self.ledger = ledger
        self.case = case
        self.case_changed = False
        self._default_k = default_k

    def specs(self) -> list[ToolSpec]:
        specs = [SEARCH_TOOL, CALCULATE_TOOL, BENEFITS_TOOL, CALENDAR_TOOL]
        if self._graph is not None:
            specs.append(VALIDITY_TOOL)
        if self.case is not None:
            specs.extend([READ_CASE_TOOL, RECORD_FACTS_TOOL, EVALUATE_RULES_TOOL])
        if self._allowed is not None:
            specs = [spec for spec in specs if spec.name in self._allowed]
        return specs

    def execute(self, call: ToolCall) -> ToolOutcome:
        if call.arguments_error:
            return _error(f"No se pudo interpretar la llamada: {call.arguments_error}. Reenvíala con JSON válido.")
        handlers = {
            SEARCH_TOOL.name: self._search,
            CALCULATE_TOOL.name: self._calculate,
            BENEFITS_TOOL.name: self._benefits,
            CALENDAR_TOOL.name: self._calendar,
            VALIDITY_TOOL.name: self._validity,
            READ_CASE_TOOL.name: self._read_case,
            RECORD_FACTS_TOOL.name: self._record_facts,
            EVALUATE_RULES_TOOL.name: self._evaluate_rules,
        }
        handler = handlers.get(call.name)
        unavailable = (
            (call.name in _CASE_ONLY_TOOLS and self.case is None)
            or (self._allowed is not None and call.name not in self._allowed)
        ) or (call.name == VALIDITY_TOOL.name and self._graph is None)
        if handler is None or unavailable:
            return _error(f"Herramienta no disponible: {call.name}.")
        try:
            return handler(call.arguments)
        except Exception as exc:  # a tool failure is reported to the model, never crashes the turn
            logger.exception("tool %s failed", call.name)
            return _error(f"La herramienta {call.name} falló: {exc}")

    # ------------------------------------------------------------------ #

    def _search(self, arguments: dict[str, Any]) -> ToolOutcome:
        query = arguments.get("consulta")
        if not isinstance(query, str) or not query.strip():
            return _error("Falta 'consulta' (texto no vacío).")
        source_type = arguments.get("tipo_fuente")
        if source_type is not None and source_type not in SOURCE_TYPES:
            return _error(f"'tipo_fuente' debe ser uno de: {', '.join(SOURCE_TYPES)}.")
        raw_k = arguments.get("max_resultados", self._default_k)
        k = raw_k if isinstance(raw_k, int) and not isinstance(raw_k, bool) else self._default_k
        k = max(1, min(MAX_SEARCH_RESULTS, k))

        def _text(key: str) -> str | None:
            value = arguments.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else None

        date_from = _text("desde_fecha")
        if date_from is not None and normalize_date(date_from) is None:
            return _error("'desde_fecha' debe tener formato AAAA o AAAA-MM-DD.")
        filters = SearchFilters(
            date_from=date_from,
            source_type=source_type,
            tributo=_text("tributo"),
            jurisdiction_scope=_text("ambito"),
            binding_only=arguments.get("solo_vinculante") is True,
        )
        docs = self._searcher.search(query.strip(), k=k, filters=filters)
        if not docs:
            hint = " Prueba sin filtros o con otra formulación." if not filters.is_empty() else ""
            return ToolOutcome(
                content=f"Sin resultados para esta búsqueda.{hint}",
                summary="0 fragmentos",
            )
        fragments = [
            self.ledger.register(text=doc.page_content or "", metadata=self._with_validity(dict(doc.metadata or {})))
            for doc in docs
        ]
        # A repeated search can return chunks already registered; show each once.
        unique = list({fragment.fragment_id: fragment for fragment in fragments}.values())
        body = "\n\n".join(fragment.render() for fragment in unique)
        alerts = self._validity_alerts(unique)
        if alerts:
            body += "\n\n" + alerts
        return ToolOutcome(
            content=body,
            summary=f"{len(unique)} fragmentos: {', '.join(f.fragment_id for f in unique)}",
        )

    # ------------------------------------------------------------------ #
    # Tax computation
    # ------------------------------------------------------------------ #

    def _scenario_base(self, arguments: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Resolve (fiscal year, case facts, explicit data) for a computation."""
        datos = arguments.get("datos") or {}
        if not isinstance(datos, dict):
            raise DatosInvalidosError("'datos' debe ser un objeto {campo: valor}.")
        facts = {fact.name: fact.value for fact in self.case.facts} if self.case is not None else {}
        raw_year = arguments.get("ejercicio")
        if isinstance(raw_year, int) and not isinstance(raw_year, bool):
            year = raw_year
        else:
            case_year = parse_fiscal_year(self.case.tax_period) if self.case is not None else None
            year = case_year if case_year is not None else max(ejercicios_disponibles())
        return year, facts, datos

    def _calculate(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            year, facts, datos = self._scenario_base(arguments)
            simulacion = simular(
                ejercicio=year, facts=facts, datos=datos, escenarios=arguments.get("escenarios"), con_beneficios=False
            )
        except (DatosInvalidosError, EjercicioNoDisponibleError) as exc:
            return _error(str(exc))

        blocks: list[str] = []
        fragment_ids: list[str] = []
        base_name = simulacion.escenarios[0].nombre
        for escenario in simulacion.escenarios:
            nombre, result = escenario.nombre, escenario.resultado
            # Each scenario is registered as evidence: the answer cites it like
            # any other source and the verifier checks figures against it.
            fragment = self.ledger.register(
                text=_render_result(nombre, result),
                metadata={
                    "title": f"Cálculo IRPF {result.ejercicio} — {nombre}",
                    "source": f"calculadora://irpf/{result.ejercicio}/{len(self.ledger) + 1}",
                    "source_authority": "CALCULADORA",
                    "section": "Simulación determinista",
                },
            )
            fragment_ids.append(fragment.fragment_id)
            block = f"[{fragment.fragment_id}] " + _render_result(nombre, result)
            if nombre != base_name:
                diff = escenario.ahorro_frente_a_base
                verb = "ahorra" if diff > 0 else "cuesta"
                block += (
                    f"\nFrente a «{base_name}»: {verb} {_money(abs(diff))}."
                    if diff
                    else f"\nFrente a «{base_name}»: sin diferencia."
                )
            blocks.append(block)
        if simulacion.datos_no_aportados:
            blocks.append(
                "DATOS NO APORTADOS (el cálculo los ha tomado como cero o sin informar): "
                + ", ".join(simulacion.datos_no_aportados)
                + ". Pídeselos al usuario si pueden cambiar el resultado, y dile que el cálculo es provisional sin ellos."
            )
        return ToolOutcome(
            content="\n\n".join(blocks),
            summary=f"{len(simulacion.escenarios)} cálculo(s): {', '.join(fragment_ids)}",
        )

    def _calendar(self, arguments: dict[str, Any]) -> ToolOutcome:
        raw_days = arguments.get("dias", 365)
        days = raw_days if isinstance(raw_days, int) and not isinstance(raw_days, bool) else 365
        days = max(1, min(730, days))
        facts = {fact.name: fact.value for fact in self.case.facts} if self.case is not None else {}
        today = datetime.now(UTC).date()
        vencimientos = proximos_vencimientos(facts, hoy=today, dias=days)
        if not vencimientos:
            return ToolOutcome(content="No hay vencimientos en ese plazo.", summary="0 vencimientos")
        lines = [
            f"- {v.fecha.strftime('%d/%m/%Y')} (en {v.dias_restantes} días): {v.titulo} — {v.periodo}"
            + (f", modelo {v.modelo}" if v.modelo else "")
            + f". {v.detalle} [{v.referencia}{'' if v.verificado else '; fecha no contrastada'}]"
            + (f" Nota: {v.nota}" if v.nota else "")
            for v in vencimientos
        ]
        text = (
            f"Calendario fiscal del usuario a {today.strftime('%d/%m/%Y')} (próximos {days} días)\n"
            + "\n".join(lines)
            + f"\n{aviso_festivos()}\nSolo se listan las obligaciones que constan en el expediente: si el usuario "
            "tiene actividad económica o bienes en el extranjero y no lo ha dicho, no aparecen."
        )
        # Registered as evidence so dates in the answer can be cited and verified.
        fragment = self.ledger.register(
            text=text,
            metadata={
                "title": f"Calendario fiscal a {today.strftime('%d/%m/%Y')}",
                "source": f"calendario://{today.isoformat()}/{days}",
                "source_authority": "CALCULADORA",
                "section": "Plazos de presentación",
            },
        )
        return ToolOutcome(
            content=f"[{fragment.fragment_id}] {text}",
            summary=f"{len(vencimientos)} vencimientos: {fragment.fragment_id}",
        )

    def _benefits(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            year, facts, datos = self._scenario_base(arguments)
            params = load_params(year)
            inp = build_input(ejercicio=year, facts=facts, datos=datos)
        except (DatosInvalidosError, EjercicioNoDisponibleError) as exc:
            return _error(str(exc))
        conocidos = {name for name in (*facts, *datos) if name in IRPFInput.model_fields or name == "estado_civil"}
        beneficios = evaluar_beneficios(inp, conocidos=conocidos, params=params)
        payload = {
            "ejercicio": year,
            "beneficios": [
                {
                    "nombre": b.nombre,
                    "estado": b.estado,
                    "detalle": b.detalle,
                    "ahorro_estimado_eur": float(b.ahorro_estimado) if b.ahorro_estimado is not None else None,
                    "requisitos": b.requisitos,
                    "referencia": b.referencia,
                    "parametros_verificados": b.verificado,
                    "datos_que_faltan": b.datos_que_faltan,
                }
                for b in beneficios
            ],
            "nota": (
                "Los ahorros estimados salen de la calculadora con los datos disponibles; para citarlos ante el "
                "usuario, calcula el escenario con calcular_irpf y cita ese fragmento. Las condiciones de cada "
                "beneficio deben contrastarse con buscar_fuentes antes de afirmarlas."
            ),
        }
        disponibles = sum(1 for b in beneficios if b.estado == "disponible")
        return ToolOutcome(
            content=json.dumps(payload, ensure_ascii=False),
            summary=f"{len(beneficios)} beneficios revisados, {disponibles} disponibles",
        )

    # ------------------------------------------------------------------ #
    # Doctrine validity
    # ------------------------------------------------------------------ #

    def _with_validity(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Stamp a retrieved chunk with its document's validity status."""
        if self._graph is None:
            return metadata
        doc = identify_document(metadata)
        if doc is None:
            return metadata
        status = self._graph.vigencia(doc.clave)
        if status.estado != "desconocido":
            metadata["_doc_key"] = status.clave
            metadata["_vigencia_estado"] = status.estado
            metadata["_vigencia_resumen"] = status.resumen
        return metadata

    def _validity_alerts(self, fragments: list[Fragment]) -> str:
        """Warn the model about superseded/disputed doctrine among the results,
        and about results that disagree with each other."""
        if self._graph is None:
            return ""
        lines: list[str] = []
        for fragment in fragments:
            if fragment.vigencia in ("superada", "discutida", "matizada"):
                lines.append(f"- [{fragment.fragment_id}] {fragment.title}: {fragment.vigencia_resumen}")
        by_key = {fragment.doc_key: fragment for fragment in fragments if fragment.doc_key}
        for key, fragment in by_key.items():
            for arista in self._graph.salientes(key):
                other = by_key.get(arista.destino)
                if other is not None and arista.relacion in ("supera", "contradice", "matiza"):
                    lines.append(
                        f"- [{fragment.fragment_id}] {_RELACION_VERBO[arista.relacion].replace('la ', '')} a "
                        f"[{other.fragment_id}] (según el grafo de citas)."
                    )
        if not lines:
            return ""
        return (
            "ALERTAS DE VIGENCIA (indicios del grafo de citas entre resoluciones; compruébalos leyendo la resolución "
            "posterior antes de afirmarlos):\n" + "\n".join(dict.fromkeys(lines))
        )

    def _validity(self, arguments: dict[str, Any]) -> ToolOutcome:
        assert self._graph is not None
        reference = arguments.get("referencia")
        if not isinstance(reference, str) or not reference.strip():
            return _error("Falta 'referencia' (la resolución tal como se cita).")
        citas = extract_citations(reference)
        if not citas:
            return _error(
                "No reconozco esa referencia. Usa el número de consulta (V1234-19), el RG del TEAC (00/1234/2017), "
                "o 'STS … (rec. 1234/2019)' / 'STC 182/2021'."
            )
        blocks: list[str] = []
        for cita in citas[:3]:
            nodo = self._graph.get(cita.clave)
            if nodo is None:
                blocks.append(
                    f"{cita.texto}: no consta en el grafo (ni está en el corpus ni la cita ningún documento)."
                )
                continue
            status = self._graph.vigencia(nodo.clave)
            lines = [
                f"{self._graph.etiqueta(nodo)} — estado: {status.estado.upper()}. {status.resumen}"
                + (
                    ""
                    if nodo.en_corpus
                    else " (No está en el corpus: solo se conoce por las citas de otros documentos.)"
                )
            ]
            for arista in self._graph.entrantes(nodo.clave):
                otro = self._graph.get(arista.origen)
                if otro is not None:
                    evidencia = f" — «{arista.evidencia}»" if arista.evidencia and arista.relacion != "cita" else ""
                    lines.append(f"  · {self._graph.etiqueta(otro)} {_RELACION_VERBO[arista.relacion]}{evidencia}")
            for arista in self._graph.salientes(nodo.clave):
                otro = self._graph.get(arista.destino)
                if otro is not None and arista.relacion != "cita":
                    lines.append(f"  · {arista.relacion} a {self._graph.etiqueta(otro)}")
            blocks.append("\n".join(lines))
        blocks.append(
            "Esto es un indicio, no una fuente citable: para afirmar ante el usuario que un criterio está superado o "
            "discutido, localiza la resolución posterior con buscar_fuentes y cítala."
        )
        return ToolOutcome(content="\n\n".join(blocks), summary=f"vigencia de {len(citas[:3])} referencia(s)")

    def _case_snapshot(self) -> dict[str, Any]:
        case = self.case
        assert case is not None
        return {
            "ejercicio": case.tax_period,
            "jurisdiccion": case.jurisdiction,
            "hechos": {fact.name: fact.value for fact in case.facts},
            "datos_pendientes": [missing.fact_name for missing in case.missing_facts],
            "obligaciones_candidatas": [
                {
                    "id": obligation.obligation_id,
                    "titulo": obligation.title,
                    "estado": obligation.status.value,
                    "faltan_para_confirmar": obligation.blocking_missing_facts,
                }
                for obligation in case.obligation_candidates
            ],
        }

    def _read_case(self, arguments: dict[str, Any]) -> ToolOutcome:
        del arguments
        snapshot = self._case_snapshot()
        return ToolOutcome(
            content=json.dumps(snapshot, ensure_ascii=False, default=str),
            summary=f"{len(snapshot['hechos'])} hechos conocidos",
        )

    def _record_facts(self, arguments: dict[str, Any]) -> ToolOutcome:
        assert self.case is not None
        raw_facts = arguments.get("hechos")
        if not isinstance(raw_facts, list) or not raw_facts:
            return _error("Falta 'hechos' (lista no vacía de {nombre, valor}).")

        accepted: list[Fact] = []
        rejected: list[str] = []
        for item in raw_facts:
            name = item.get("nombre") if isinstance(item, dict) else None
            value = item.get("valor") if isinstance(item, dict) else None
            expected = EXPECTED_FACT_TYPE.get(name) if isinstance(name, str) else None
            if not isinstance(name, str) or expected is None:
                rejected.append(f"{name!r}: nombre de hecho desconocido")
                continue
            coerced = _coerce_value(
                expected=expected,
                value_string=value if isinstance(value, str) else None,
                value_number=value if isinstance(value, int | float) and not isinstance(value, bool) else None,
                value_boolean=value if isinstance(value, bool) else None,
            )
            if coerced is None:
                rejected.append(f"{name}: valor {value!r} no es de tipo {expected.value}")
                continue
            accepted.append(
                Fact(
                    fact_id=f"fact_{name}",
                    name=name,
                    value=coerced,
                    value_type=expected,
                    source="user_statement_via_agent",
                    confidence=0.9,
                )
            )

        if accepted:
            merged = _merge_facts(accepted, self.case)
            known = {fact.name for fact in merged}
            update: dict[str, Any] = {
                "facts": merged,
                # CaseState forbids a fact being both known and missing.
                "missing_facts": [m for m in self.case.missing_facts if m.fact_name not in known],
                "updated_at": datetime.now(UTC),
            }
            period = next((fact for fact in accepted if fact.name == "periodo_fiscal"), None)
            if period is not None:
                update["tax_period"] = str(period.value)
            self.case = self.case.model_copy(update=update)
            self.case_changed = True

        lines = [f"Registrados: {', '.join(f'{fact.name}={fact.value}' for fact in accepted) or 'ninguno'}."]
        if rejected:
            lines.append("Rechazados: " + "; ".join(rejected) + ".")
        return ToolOutcome(
            content=" ".join(lines),
            summary=f"{len(accepted)} registrados, {len(rejected)} rechazados",
            is_error=not accepted,
        )

    def _evaluate_rules(self, arguments: dict[str, Any]) -> ToolOutcome:
        del arguments
        assert self.case is not None
        result = evaluate_rules(case_state=self.case, recent_facts=[])
        if list(result.candidate_obligations) != list(self.case.obligation_candidates):
            self.case = self.case.model_copy(
                update={"obligation_candidates": result.candidate_obligations, "updated_at": datetime.now(UTC)}
            )
            self.case_changed = True
        payload = {
            "ejercicio_evaluado": result.fiscal_year,
            "obligaciones_candidatas": [
                {
                    "id": obligation.obligation_id,
                    "titulo": obligation.title,
                    "descripcion": obligation.description,
                    "riesgo": obligation.risk_level.value,
                    "fuentes_de_la_regla": [f"{ref.title} ({ref.locator})" for ref in obligation.evidence_refs],
                }
                for obligation in result.candidate_obligations
            ],
            "reglas_no_activadas": [
                {"regla": trace.rule_id, "hechos_que_faltan": trace.missing_facts}
                for trace in result.rule_traces
                if not trace.matched
            ],
            "aviso": "Catálogo de reglas parcial: la ausencia de una obligación no implica que no exista.",
        }
        return ToolOutcome(
            content=json.dumps(payload, ensure_ascii=False, default=str),
            summary=f"{len(result.candidate_obligations)} obligaciones candidatas",
        )
