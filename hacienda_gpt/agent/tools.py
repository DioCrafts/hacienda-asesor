"""Tools the agent can call, and their execution against local state.

Tools are deliberately few and typed. Everything that must be *exact* — what
the law says, what the user declared, which rules fire — lives behind a tool,
so the model orchestrates and reasons but never has to recall a figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from typing import Any

from hacienda_gpt.agent.evidence import SOURCE_TYPES, EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall, ToolSpec
from hacienda_gpt.agent.search import CorpusSearcher, SearchFilters, normalize_date
from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE, KNOWN_FACT_NAMES, _coerce_value
from hacienda_gpt.decision.interpreter import _merge_facts
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import CaseState, Fact

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


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    summary: str
    is_error: bool = False


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(content=message, summary=message[:120], is_error=True)


class Toolbox:
    """Holds the per-run state the tools read and mutate."""

    def __init__(
        self,
        *,
        searcher: CorpusSearcher,
        ledger: EvidenceLedger,
        case: CaseState | None = None,
        default_k: int = 6,
    ) -> None:
        self._searcher = searcher
        self.ledger = ledger
        self.case = case
        self.case_changed = False
        self._default_k = default_k

    def specs(self) -> list[ToolSpec]:
        specs = [SEARCH_TOOL]
        if self.case is not None:
            specs.extend([READ_CASE_TOOL, RECORD_FACTS_TOOL, EVALUATE_RULES_TOOL])
        return specs

    def execute(self, call: ToolCall) -> ToolOutcome:
        if call.arguments_error:
            return _error(f"No se pudo interpretar la llamada: {call.arguments_error}. Reenvíala con JSON válido.")
        handlers = {
            SEARCH_TOOL.name: self._search,
            READ_CASE_TOOL.name: self._read_case,
            RECORD_FACTS_TOOL.name: self._record_facts,
            EVALUATE_RULES_TOOL.name: self._evaluate_rules,
        }
        handler = handlers.get(call.name)
        if handler is None or (call.name != SEARCH_TOOL.name and self.case is None):
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
            self.ledger.register(text=doc.page_content or "", metadata=dict(doc.metadata or {})) for doc in docs
        ]
        # A repeated search can return chunks already registered; show each once.
        unique = list({fragment.fragment_id: fragment for fragment in fragments}.values())
        body = "\n\n".join(fragment.render() for fragment in unique)
        return ToolOutcome(
            content=body,
            summary=f"{len(unique)} fragmentos: {', '.join(f.fragment_id for f in unique)}",
        )

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
