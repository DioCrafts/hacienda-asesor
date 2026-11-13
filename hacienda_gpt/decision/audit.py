from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hacienda_gpt.decision.interpreter import InterpretationResult
from hacienda_gpt.decision.rules_engine import RulesEngineResult
from hacienda_gpt.decision.schemas import CaseState, ObligationCandidate

PROMPT_VERSION = "decision_prompt_v1"
MODEL_VERSION = "openai_model_config_v1"
RULES_VERSION_FALLBACK = "unknown"


def build_recommendation_audit_event(
    case_state: CaseState,
    interpretation: InterpretationResult,
    rules_result: RulesEngineResult,
    obligations: list[ObligationCandidate],
) -> dict[str, Any]:
    evidence = []
    for ob in obligations:
        for ev in ob.evidence_refs:
            evidence.append(
                {
                    "evidence_id": ev.evidence_id,
                    "title": ev.title,
                    "locator": ev.locator,
                    "confidence": ev.confidence,
                }
            )

    return {
        "event_type": "recommendation_audit",
        "timestamp": datetime.now(UTC).isoformat(),
        "case_id": case_state.case_id,
        "facts_used": [
            {
                "fact_id": f.fact_id,
                "name": f.name,
                "value": f.value,
                "confidence": f.confidence,
                "source": f.source,
            }
            # The accumulated facts the recommendation rested on (not just this
            # turn's): the audit trail must capture the full decision basis.
            for f in interpretation.all_facts
        ],
        "rules_triggered": [
            {
                "rule_id": trace.rule_id,
                "rule_version": trace.rule_version,
                "matched": trace.matched,
                "missing_facts": trace.missing_facts,
            }
            for trace in rules_result.rule_traces
            if trace.matched
        ],
        "evidences_cited": evidence,
        "versions": {
            "model": MODEL_VERSION,
            "prompt": PROMPT_VERSION,
            "ruleset": rules_result.ruleset_version or RULES_VERSION_FALLBACK,
        },
    }


# --------------------------------------------------------------------------- #
# Removing one fact from the audit trail (the user deleted it from their case)
# --------------------------------------------------------------------------- #

ELIMINADO = "[eliminado por el usuario]"

# Names under which a fact's value travels in tool arguments when they differ from the fact's own name.
ALIAS_EN_HERRAMIENTAS: dict[str, tuple[str, ...]] = {
    "datos_conyuge": ("conyuge", "conyuge_b"),
    "preferencias_optimizacion": ("restricciones",),
}


def redactar_hecho(evento: Any, nombre: str) -> tuple[Any, bool]:
    """``evento`` with every stored value of the fact ``nombre`` replaced by :data:`ELIMINADO`.

    Covers the structured places a fact's value is written to: a user edit (``fact_name``/``value``), the
    facts a recommendation rested on (``name``/``value``), the facts the advisor recorded (``nombre``/``valor``)
    and any argument of a tool keyed by the fact's name or by one of its aliases (``ALIAS_EN_HERRAMIENTAS``),
    at any depth. Free text — what the user typed, what the advisor answered, a tool's one-line summary — is
    left as it is: it is the record of the conversation, not a stored fact. Returns (event, whether it changed).
    """
    claves = {nombre, *ALIAS_EN_HERRAMIENTAS.get(nombre, ())}
    cambiado = False

    def recorrer(nodo: Any) -> Any:
        nonlocal cambiado
        if isinstance(nodo, dict):
            salida: dict[str, Any] = {}
            emparejado = nodo.get("fact_name") == nombre or nodo.get("name") == nombre or nodo.get("nombre") == nombre
            for clave, valor in nodo.items():
                if (emparejado and clave in ("value", "valor")) or clave in claves:
                    if valor != ELIMINADO and valor is not None:
                        cambiado = True
                    salida[clave] = ELIMINADO if valor is not None else None
                else:
                    salida[clave] = recorrer(valor)
            return salida
        if isinstance(nodo, list):
            return [recorrer(v) for v in nodo]
        return nodo

    return recorrer(evento), cambiado
