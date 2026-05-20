from __future__ import annotations

from dataclasses import dataclass

from hacienda_gpt.decision.interpreter import InterpretationResult
from hacienda_gpt.decision.planner import PlannerResult
from hacienda_gpt.decision.schemas import CaseState, ObligationCandidate


@dataclass(frozen=True)
class ExplanationComposer:
    """Compose final user-facing response with audit-friendly structure."""

    def compose(
        self,
        case_state: CaseState,
        interpretation: InterpretationResult,
        obligations: list[ObligationCandidate],
        planner_result: PlannerResult,
    ) -> str:
        sections: list[str] = []

        sections.append("1) Hechos detectados")
        if interpretation.extracted_facts:
            for fact in interpretation.extracted_facts:
                sections.append(
                    f"- {fact.name}: {fact.value} (confianza {fact.confidence:.2f}, fuente: {fact.source})"
                )
        else:
            sections.append("- No se detectaron hechos concluyentes en este turno.")

        sections.append("\n2) Incertidumbres")
        if interpretation.uncertainties:
            for uncertainty in interpretation.uncertainties:
                sections.append(f"- [{uncertainty.severity.value}] {uncertainty.message}")
        else:
            sections.append("- No se identificaron incertidumbres relevantes en este turno.")

        sections.append("\n3) Obligaciones candidatas + confianza")
        if obligations:
            for obligation in obligations:
                sections.append(
                    f"- {obligation.title} ({obligation.obligation_id}) — confianza {obligation.confidence:.2f}, riesgo {obligation.risk_level.value}"
                )
                if obligation.blocking_missing_facts:
                    sections.append(f"  - Facts faltantes para confirmar: {', '.join(obligation.blocking_missing_facts)}")
        else:
            sections.append("- No hay obligaciones candidatas confirmables con la información actual.")

        sections.append("\n4) Plan recomendado")
        if planner_result.ranked_actions:
            for planned in planner_result.ranked_actions:
                action = planned.action
                sections.append(
                    f"- [{action.priority}] {action.title} | fecha objetivo: {action.due_date} | score {planned.score:.2f}"
                )
                if planned.bloqueos:
                    sections.append(f"  - Bloqueos: {', '.join(planned.bloqueos)}")
                if planned.checklist_documental:
                    sections.append("  - Checklist documental:")
                    for item in planned.checklist_documental:
                        sections.append(f"    - {item}")
                if action.depends_on:
                    sections.append(f"  - Depende de: {', '.join(action.depends_on)}")
        else:
            sections.append("- No se pudo proponer un plan accionable con los datos actuales.")

        sections.append("\n5) Fuentes")
        sources: list[str] = []
        for obligation in obligations:
            for evidence in obligation.evidence_refs:
                sources.append(f"- {evidence.title} ({evidence.locator})")
        if sources:
            sections.extend(sorted(set(sources)))
        else:
            sections.append("- Sin fuentes normativas trazables en este turno; se recomienda recopilar evidencia.")

        sections.append("\n6) Próxima pregunta óptima")
        if interpretation.next_questions:
            sections.append(f"- {interpretation.next_questions[0].question_text}")
        elif case_state.missing_facts:
            sections.append(
                f"- Para avanzar, confirma este dato clave: {case_state.missing_facts[0].fact_name}."
            )
        else:
            sections.append("- ¿Quieres que validemos esta propuesta con más detalle y documentación adicional?")

        return "\n".join(sections)
