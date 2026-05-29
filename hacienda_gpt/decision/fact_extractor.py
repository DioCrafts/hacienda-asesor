"""Pluggable fact extractors for the decision engine.

The interpreter delegates intent + fact extraction to a `FactExtractor`. The
production default is `OpenAIFactExtractor`, which leverages OpenAI's structured
output to return strongly typed facts. `RegexFactExtractor` keeps the legacy
heuristic behaviour for tests, offline development, and fallback when the LLM
is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import re
from typing import Any, Protocol

from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType

logger = logging.getLogger(__name__)


# Canonical fact vocabulary surfaced to the LLM, paired with the value type
# we expect to receive. Anything the LLM emits in a non-matching slot is
# either coerced when safe (e.g. "45000" → 45000.0) or dropped, so we never
# persist a `categoria_contribuyente = True` style of nonsense fact.
EXPECTED_FACT_TYPE: dict[str, FactValueType] = {
    "residencia_fiscal": FactValueType.STRING,
    "periodo_fiscal": FactValueType.STRING,
    "tipo_renta": FactValueType.STRING,
    "menciona_ingresos": FactValueType.BOOLEAN,
    "importe_renta_aproximado": FactValueType.NUMBER,
    "categoria_contribuyente": FactValueType.STRING,
    "regimen_tributario": FactValueType.STRING,
    "volumen_facturacion": FactValueType.NUMBER,
    "actividad_economica": FactValueType.STRING,
    "plantilla": FactValueType.NUMBER,
    "alta_actividad_economica": FactValueType.BOOLEAN,
    "fecha_inicio_actividad": FactValueType.STRING,
    "regimen_cotizacion": FactValueType.STRING,
    "periodicidad_iva": FactValueType.STRING,
    "tema_tributario": FactValueType.STRING,
}

KNOWN_FACT_NAMES: tuple[str, ...] = tuple(EXPECTED_FACT_TYPE.keys())


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

REGLA DE SLOT (crítica). Cada hecho tiene UN ÚNICO campo de valor permitido,
y el resto deben ir a `null`. Si te equivocas de slot el hecho se descarta.

  value_string  (texto/categoría/fecha):
    residencia_fiscal           → "ES" | "OTHER" | código ISO ("PT", "DE"…)
    periodo_fiscal              → año en 4 dígitos como texto, ej. "2024"
    tipo_renta                  → "trabajo" | "actividad_economica" | "capital_mobiliario" | "capital_inmobiliario" | "ganancias_patrimoniales" | "otros"
    categoria_contribuyente     → "autonomo" | "asalariado" | "sociedad" | "pensionista" | "estudiante" | "desempleado"
    regimen_tributario          → "estimacion_directa_normal" | "estimacion_directa_simplificada" | "estimacion_objetiva" | "modulos"
    actividad_economica         → texto libre breve ("consultoría informática", "bar de tapas"…)
    fecha_inicio_actividad      → fecha ISO ("2024-01-15" si día explícito, "2024-01" si solo mes y año, "2024" si solo año)
    regimen_cotizacion          → "RETA" | "general" | "asimilado" | etc.
    periodicidad_iva            → "mensual" | "trimestral"
    tema_tributario             → texto libre breve

  value_number  (numérico):
    importe_renta_aproximado    → euros, ej. 45000
    volumen_facturacion         → euros anuales facturados
    plantilla                   → número entero de empleados

  value_boolean  (sí/no):
    menciona_ingresos           → true si el usuario menciona ingresos/rendimientos
    alta_actividad_economica    → true si el usuario está dado de alta en actividad económica

EJEMPLOS de mapeo concreto (sigue el mismo patrón):

  Usuario: "soy autónomo y facturo 45.000€ haciendo consultoría informática"
    → categoria_contribuyente value_string="autonomo"
    → volumen_facturacion       value_number=45000
    → menciona_ingresos         value_boolean=true
    → actividad_economica       value_string="consultoría informática"
    → alta_actividad_economica  value_boolean=true

  Usuario: "vivo en Madrid, empecé la actividad en enero de 2024, ejercicio 2024"
    → residencia_fiscal         value_string="ES"
    → fecha_inicio_actividad    value_string="2024-01"
    → periodo_fiscal            value_string="2024"
    → alta_actividad_economica  value_boolean=true

  Usuario: "tributo en estimación directa simplificada y no tengo empleados"
    → regimen_tributario        value_string="estimacion_directa_simplificada"
    → plantilla                 value_number=0

REGLAS GENERALES:
- "vivo en Madrid", "soy de Sevilla", "estoy en Bilbao" → residencia_fiscal=ES.
- "vivo en Berlín", "soy residente en Portugal" → residencia_fiscal=OTHER (o el código ISO si claro).
- Solo emite `periodo_fiscal` cuando el usuario habla del **ejercicio fiscal** de la consulta (renta 2024, IRPF 2025, campaña 2023). No lo confundas con `fecha_inicio_actividad`.
- Si un dato es ambiguo no lo emitas. Mejor ningún hecho que un hecho incorrecto.
- No repitas hechos que ya estén en `current_case_state.facts` salvo que el usuario los corrija explícitamente.
- Si el turno es vacío, off-topic o no aporta hechos nuevos, devuelve `facts: []` e `intent` con el valor más cercano (o `unknown`)."""


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
        self._model = (
            model or os.environ.get("DECISION_EXTRACTOR_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
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
                    {"name": f.name, "value": f.value, "confidence": f.confidence} for f in current_case_state.facts
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
            if name not in EXPECTED_FACT_TYPE:
                continue

            expected = EXPECTED_FACT_TYPE[name]
            value = _coerce_value(
                expected=expected,
                value_string=raw.get("value_string"),
                value_number=raw.get("value_number"),
                value_boolean=raw.get("value_boolean"),
            )
            if value is None:
                logger.debug(
                    "Dropping fact %s: extractor did not emit a value matching %s",
                    name,
                    expected.value,
                )
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
                    value_type=expected,
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
# Value coercion helpers (module-level for unit-testability)
# --------------------------------------------------------------------------- #


# Grabs the first number-like token, separators included ("45.000,50", "12.5").
_NUMERIC_TOKEN_RE = re.compile(r"-?\d[\d.,]*\d|-?\d")
# Strict thousands-grouping shapes: "45.000", "1.234.567" / "45,000", "1,234,567".
_THOUSANDS_DOT_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")
_THOUSANDS_COMMA_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+$")


def _parse_number_from_string(value: str) -> float | None:
    """Parse a euro-style amount, disambiguating ``.`` / ``,`` separators.

    The previous implementation stripped every ``.`` and turned every ``,`` into
    a ``.``, which corrupted English-style decimals ("12.5" → 125). Here we pick
    the decimal separator deliberately:

    * both present  → the *last* one is the decimal mark, the other groups thousands
      ("45.000,50" → 45000.50, "1,234.50" → 1234.50);
    * only ``,``    → Spanish decimal comma ("12,5" → 12.5) unless it is a strict
      3-digit grouping ("45,000" → 45000);
    * only ``.``    → thousands grouping ("45.000" → 45000) unless it is a decimal
      point ("12.5" → 12.5).
    """
    match = _NUMERIC_TOKEN_RE.search(value)
    if not match:
        return None
    token = match.group(0)
    has_dot = "." in token
    has_comma = "," in token

    if has_dot and has_comma:
        if token.rfind(",") > token.rfind("."):
            normalized = token.replace(".", "").replace(",", ".")
        else:
            normalized = token.replace(",", "")
    elif has_comma:
        normalized = token.replace(",", "") if _THOUSANDS_COMMA_RE.match(token) else token.replace(",", ".")
    elif has_dot:
        normalized = token.replace(".", "") if _THOUSANDS_DOT_RE.match(token) else token
    else:
        normalized = token

    try:
        return float(normalized)
    except ValueError:
        return None


def _coerce_value(
    *,
    expected: FactValueType,
    value_string: Any,
    value_number: Any,
    value_boolean: Any,
) -> Any:
    """Return a value matching `expected`, or None if no safe coercion exists.

    This is the line of defense against the LLM emitting `value_boolean=true`
    for a fact that semantically requires a string (e.g. ``categoria_contribuyente``).
    We accept the slot that matches `expected`; if it is empty we attempt a
    narrow, *safe* coercion from a sibling slot. Anything ambiguous returns
    None so the caller drops the fact.
    """

    if expected is FactValueType.STRING:
        if isinstance(value_string, str) and value_string.strip():
            return value_string.strip()
        # Numbers can be rendered as strings safely (e.g. "2024" for periodo_fiscal).
        if isinstance(value_number, (int, float)) and not isinstance(value_number, bool):
            if float(value_number).is_integer():
                return str(int(value_number))
            return str(value_number)
        return None

    if expected is FactValueType.NUMBER:
        if isinstance(value_number, (int, float)) and not isinstance(value_number, bool):
            return float(value_number)
        if isinstance(value_string, str):
            return _parse_number_from_string(value_string)
        return None

    if expected is FactValueType.BOOLEAN:
        if isinstance(value_boolean, bool):
            return value_boolean
        if isinstance(value_string, str):
            lowered = value_string.strip().lower()
            if lowered in {"true", "si", "sí", "yes", "1"}:
                return True
            if lowered in {"false", "no", "0"}:
                return False
        if isinstance(value_number, (int, float)) and not isinstance(value_number, bool):
            return bool(value_number)
        return None

    # Other FactValueType members are not surfaced by the schema yet.
    return None


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
