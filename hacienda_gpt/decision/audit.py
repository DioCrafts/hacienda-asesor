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
            for f in interpretation.extracted_facts
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
