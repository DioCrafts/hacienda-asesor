"""Pluggable fact extractors for the decision engine.

The interpreter delegates intent + fact extraction to a `FactExtractor`. The
production default is `OpenAIFactExtractor`, which leverages OpenAI's structured
output to return strongly typed facts. `RegexFactExtractor` keeps the legacy
heuristic behaviour for tests, offline development, and fallback when the LLM
is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType

logger = logging.getLogger(__name__)


# Canonical fact vocabulary surfaced to the LLM. Kept aligned with
# `hacienda_gpt.decision.taxonomy` plus a few common extras the interpreter
# can use to drive richer flows.
KNOWN_FACT_NAMES = (
    "residencia_fiscal",
    "periodo_fiscal",
    "tipo_renta",
    "menciona_ingresos",
    "importe_renta_aproximado",
    "categoria_contribuyente",
    "regimen_tributario",
    "volumen_facturacion",
    "actividad_economica",
    "plantilla",
    "alta_actividad_economica",
    "fecha_inicio_actividad",
    "regimen_cotizacion",
    "periodicidad_iva",
    "tema_tributario",
)


KNOWN_INTENTS = (
    "declaracion_irpf",
    "iva",
    "autonomo",
    "generic_tributary",
    "unknown",
)


@dataclass(frozen=True)
class ExtractionPayload:
    """What an extractor returns for a single user turn."""

    intent: str
    intent_confidence: float
    extracted_facts: list[Fact] = field(default_factory=list)


class FactExtractor(Protocol):
    def extract(
        self,
        user_input: str,
        chat_history: list[dict[str, str]] | list[str],
        current_case_state: CaseState | None,
    ) -> ExtractionPayload: ...


# --------------------------------------------------------------------------- #
# Regex fallback (legacy behaviour preserved for tests and offline dev)
# --------------------------------------------------------------------------- #


class RegexFactExtractor:
    """Deterministic regex extractor; kept as a stable fallback."""

    def extract(
        self,
        user_input: str,
        chat_history: list[dict[str, str]] | list[str],
        current_case_state: CaseState | None,
    ) -> ExtractionPayload:
        del chat_history
        text = user_input.lower().strip()
        intent, confidence = self._detect_intent(text)
        facts = self._extract_facts(text)
        return ExtractionPayload(intent=intent, intent_confidence=confidence, extracted_facts=facts)

    @staticmethod
    def _detect_intent(text: str) -> tuple[str, float]:
        if re.search(r"\birpf\b|\brenta\b|declaraci[oó]n de la renta", text):
            return "declaracion_irpf", 0.84
        if re.search(r"\biva\b|modelo\s+303|modelo\s+390", text):
            return "iva", 0.84
        if re.search(r"aut[oó]nomo|autonoma|autónoma|alta en reta|cuota de autónomos", text):
            return "autonomo", 0.81
        if re.search(r"impuesto|hacienda|aeat|tribut", text):
            return "generic_tributary", 0.62
        return "unknown", 0.3

    @staticmethod
    def _extract_facts(text: str) -> list[Fact]:
        facts: list[Fact] = []

        if any(
            token in text
            for token in [
                "residente en españa",
                "residencia fiscal en españa",
                "vivo en españa",
            ]
        ):
            facts.append(
                Fact(
                    fact_id="fact_residencia_fiscal",
                    name="residencia_fiscal",
                    value="ES",
                    value_type=FactValueType.STRING,
                    source="user_input",
                    confidence=0.82,
                )
            )

        year_match = re.search(r"\b(20\d{2})\b", text)
        if year_match:
            facts.append(
                Fact(
                    fact_id="fact_tax_year",
                    name="periodo_fiscal",
                    value=year_match.group(1),
                    value_type=FactValueType.STRING,
                    source="user_input",
                    confidence=0.77,
                )
            )

        if "ingres" in text or "rendimiento" in text:
            facts.append(
                Fact(
                    fact_id="fact_income_mentioned",
                    name="menciona_ingresos",
                    value=True,
                    value_type=FactValueType.BOOLEAN,
                    source="user_input",
                    confidence=0.75,
                )
            )

        return facts


# --------------------------------------------------------------------------- #
# OpenAI structured-output extractor (production default)
# --------------------------------------------------------------------------- #


_SYSTEM_PROMPT = """Eres un extractor de hechos fiscales para España (AEAT).

A partir del turno del usuario y del estado actual del caso, devuelves SIEMPRE
un objeto JSON con dos partes:

1. `intent`: la intención fiscal principal del usuario en este turno
   (declaracion_irpf | iva | autonomo | generic_tributary | unknown) y un
   `intent_confidence` en [0, 1].
2. `facts`: lista de hechos estructurados que el usuario menciona explícita
   o inequívocamente. NUNCA inventes valores. Si dudas, NO incluyas el hecho.

Reglas clave:
- "vivo en Madrid", "soy de Sevilla", "estoy en Bilbao" → residencia_fiscal=ES.
- "vivo en Berlín", "soy residente en Portugal" → residencia_fiscal=OTHER.
- "renta 2024", "IRPF 2025", "campaña de 2023" → periodo_fiscal=AÑO.
- "soy autónomo" / "autónoma" → categoria_contribuyente=autonomo.
- "soy asalariado" / "tengo nómina" → categoria_contribuyente=asalariado.
- "facturo 45.000€" / "ingreso 60k al año" → volumen_facturacion (número en euros) Y menciona_ingresos=true.
- "estimación directa" / "directa simplificada" / "módulos" / "estimación objetiva" → regimen_tributario.
- "no tengo empleados" / "trabajo solo" → plantilla=0.
- "consultoría informática", "tengo un bar" → actividad_economica (texto breve).
- "tengo ingresos del trabajo / capital / actividad económica / ganancias" → tipo_renta.
- Si el usuario inicia o ha iniciado una actividad: "alta en autónomos en enero de 2024" → fecha_inicio_actividad (YYYY-MM-DD si claro), alta_actividad_economica=true.
- No repitas hechos que ya estén en el `current_case_state.facts` salvo que el usuario los corrija.

Si el turno es vacío, off-topic o no aporta hechos nuevos, devuelve `facts: []`
e `intent` con el valor más cercano (o `unknown`)."""


_RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "ExtractedFacts",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {"type": "string", "enum": list(KNOWN_INTENTS)},
            "intent_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "enum": list(KNOWN_FACT_NAMES)},
                        "value_string": {"type": ["string", "null"]},
                        "value_number": {"type": ["number", "null"]},
                        "value_boolean": {"type": ["boolean", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": [
                        "name",
                        "value_string",
                        "value_number",
                        "value_boolean",
                        "confidence",
                    ],
                },
            },
        },
        "required": ["intent", "intent_confidence", "facts"],
    },
    "strict": True,
}


class OpenAIExtractionError(RuntimeError):
    pass


class OpenAIFactExtractor:
    """Production extractor backed by OpenAI structured output."""

    def __init__(self, client: Any | None = None, model: str | None = None) -> None:
        self._client = client
        self._model = model or os.environ.get("DECISION_EXTRACTOR_MODEL") or os.environ.get(
            "OPENAI_MODEL", "gpt-4o-mini"
        )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        self._client = OpenAI()
        return self._client

    def extract(
        self,
        user_input: str,
        chat_history: list[dict[str, str]] | list[str],
        current_case_state: CaseState | None,
    ) -> ExtractionPayload:
        client = self._ensure_client()
        messages = self._build_messages(user_input, chat_history, current_case_state)
        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_schema", "json_schema": _RESPONSE_SCHEMA},
                temperature=0,
            )
        except Exception as exc:  # network, auth, quota, etc.
            raise OpenAIExtractionError(f"OpenAI extraction failed: {exc}") from exc

        content = response.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAIExtractionError(f"Invalid JSON from extractor: {content[:200]}") from exc

        intent = data.get("intent", "unknown")
        if intent not in KNOWN_INTENTS:
            intent = "unknown"
        confidence = float(data.get("intent_confidence", 0.0))

        facts = self._materialize_facts(data.get("facts", []))
        return ExtractionPayload(intent=intent, intent_confidence=confidence, extracted_facts=facts)

    @staticmethod
    def _build_messages(
        user_input: str,
        chat_history: list[dict[str, str]] | list[str],
        current_case_state: CaseState | None,
    ) -> list[dict[str, str]]:
        case_summary: dict[str, Any] = {"facts": []}
        if current_case_state is not None:
            case_summary = {
                "case_id": current_case_state.case_id,
                "jurisdiction": current_case_state.jurisdiction,
                "tax_period": current_case_state.tax_period,
                "facts": [
                    {"name": f.name, "value": f.value, "confidence": f.confidence}
                    for f in current_case_state.facts
                ],
                "missing_facts": [m.fact_name for m in current_case_state.missing_facts],
                "gave_up_facts": list(getattr(current_case_state, "gave_up_facts", []) or []),
            }

        normalized_history: list[dict[str, str]] = []
        for entry in chat_history or []:
            if isinstance(entry, dict):
                role = entry.get("role")
                content = entry.get("content")
                if role and content:
                    normalized_history.append({"role": role, "content": content})

        user_payload = {
            "current_case_state": case_summary,
            "chat_history": normalized_history,
            "user_input": user_input,
        }

        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]

    @staticmethod
    def _materialize_facts(raw_facts: list[dict[str, Any]]) -> list[Fact]:
        facts: list[Fact] = []
        for raw in raw_facts:
            name = raw.get("name")
            if name not in KNOWN_FACT_NAMES:
                continue
            value_string = raw.get("value_string")
            value_number = raw.get("value_number")
            value_boolean = raw.get("value_boolean")

            value: Any
            if value_number is not None:
                value = value_number
                vtype = FactValueType.NUMBER
            elif value_boolean is not None:
                value = value_boolean
                vtype = FactValueType.BOOLEAN
            elif value_string is not None and value_string != "":
                value = value_string
                vtype = FactValueType.STRING
            else:
                # Skip facts with no concrete value.
                continue

            try:
                confidence = float(raw.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = max(0.0, min(1.0, confidence))

            facts.append(
                Fact(
                    fact_id=f"fact_{name}",
                    name=name,
                    value=value,
                    value_type=vtype,
                    source="llm_extractor",
                    confidence=confidence,
                )
            )

        # Deduplicate by name keeping the highest-confidence entry.
        by_name: dict[str, Fact] = {}
        for fact in facts:
            existing = by_name.get(fact.name)
            if existing is None or fact.confidence > existing.confidence:
                by_name[fact.name] = fact
        return list(by_name.values())


# --------------------------------------------------------------------------- #
# Default selection
# --------------------------------------------------------------------------- #


def default_fact_extractor() -> FactExtractor:
    """Pick the production default extractor based on environment.

    `DECISION_EXTRACTOR=regex` forces the legacy regex behaviour. Otherwise
    we use the OpenAI extractor if `OPENAI_API_KEY` is available; otherwise
    fall back to the regex extractor with a log line.
    """

    choice = os.environ.get("DECISION_EXTRACTOR", "").strip().lower()
    if choice == "regex":
        return RegexFactExtractor()
    if choice in ("openai", "llm"):
        return OpenAIFactExtractor()

    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIFactExtractor()

    logger.warning(
        "OPENAI_API_KEY missing — falling back to RegexFactExtractor. "
        "Set DECISION_EXTRACTOR=openai with a valid key for full extraction."
    )
    return RegexFactExtractor()
