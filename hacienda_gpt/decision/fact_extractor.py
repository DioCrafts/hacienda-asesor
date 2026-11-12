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

# Fiscal-profile facts (family, income breakdown, deductions) that feed the IRPF
# calculator. Declared next to the calculator input they map onto.
from hacienda_gpt.tax.profile import PROFILE_FACT_TYPES  # noqa: E402

EXPECTED_FACT_TYPE.update(PROFILE_FACT_TYPES)

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

PERFIL FISCAL (mismos slots; emítelos solo si el usuario da el dato):

  value_string:
    comunidad_autonoma                  → nombre de la comunidad ("Madrid", "Cataluña", "Andalucía"…)
    estado_civil                        → "soltero" | "casado" | "pareja_de_hecho" | "separado" | "divorciado" | "viudo"

  value_number (importes anuales en euros; recuentos como enteros):
    edad                                → años cumplidos
    grado_discapacidad                  → porcentaje reconocido (33, 65…)
    num_descendientes                   → hijos a cargo (0 si dice que no tiene)
    num_descendientes_menores_3         → de ellos, menores de tres años
    num_ascendientes_mayores_65         → padres/abuelos mayores de 65 que conviven y dependen de él
    num_ascendientes_mayores_75         → de ellos, mayores de 75
    rendimiento_trabajo_bruto           → salario bruto anual
    cotizaciones_seguridad_social       → cotizaciones a la Seguridad Social pagadas por el trabajador
    retenciones                         → IRPF retenido en el año
    rendimiento_actividad_neto          → beneficio neto anual de la actividad económica
    rendimiento_alquiler_neto           → rendimiento neto de inmuebles alquilados
    rendimiento_capital_mobiliario      → intereses y dividendos
    ganancias_patrimoniales             → ganancias (o pérdidas, en negativo) por ventas de acciones, fondos, inmuebles…
    aportacion_plan_pensiones           → aportado por él a planes de pensiones en el año
    contribucion_empresa_plan_pensiones → aportado por su empresa
    donativos                           → donado a ONG/fundaciones en el año
    pagos_vivienda_habitual_pre2013     → pagado en el año por la hipoteca de una vivienda habitual comprada antes de 2013
    gastos_guarderia                    → pagado a guarderías en el año

  "gano 38.000 brutos al año, vivo en Valencia, tengo dos hijos, uno de 2 años"
    → rendimiento_trabajo_bruto value_number=38000 · comunidad_autonoma value_string="Comunidad Valenciana"
    → residencia_fiscal value_string="ES" · num_descendientes value_number=2 · num_descendientes_menores_3 value_number=1

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
        # A single hung request used to stall a whole turn indefinitely (no
        # timeout) and a transient 5xx/rate-limit tumbled it straight to a 500.
        # Bound both: the OpenAI SDK applies exponential backoff that honours
        # Retry-After headers, so we lean on it rather than a naive loop.
        self._timeout = float(os.environ.get("DECISION_EXTRACTOR_TIMEOUT", "30"))
        self._max_retries = int(os.environ.get("DECISION_EXTRACTOR_MAX_RETRIES", "2"))

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        self._client = OpenAI(timeout=self._timeout, max_retries=self._max_retries)
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
        # Defend against a malformed `intent_confidence` (null / non-numeric /
        # out of range) even though the JSON schema asks for [0,1]: a bad value
        # used to raise straight out of the extractor (unhandled `float(None)`)
        # or push InterpretationResult.confidence past its `le=1.0` validator,
        # aborting the whole turn instead of degrading. Mirror the per-fact
        # clamp in `_materialize_facts`.
        try:
            confidence = float(data.get("intent_confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

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
# Provider-neutral extractor (Claude by default; OpenAI / local via LLM_PROVIDER)
# --------------------------------------------------------------------------- #

# Kept as an alias so callers can catch one error type regardless of backend.
FactExtractionError = OpenAIExtractionError

_PROVIDER_PROMPT_SUFFIX = """

FORMATO DE SALIDA (sustituye a la regla de slots anterior): cada hecho lleva un único campo `value`, siempre como
TEXTO: los importes en euros solo con dígitos ("45000"), los booleanos como "true" o "false", y el resto tal como se
describe arriba para `value_string`."""

# One string-typed `value` per fact, coerced client-side. This avoids nullable
# unions and numeric bounds, which not every provider's structured-output
# implementation (nor most local servers) accepts.
_PROVIDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": list(KNOWN_INTENTS)},
        "intent_confidence": {"type": "number"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "enum": list(KNOWN_FACT_NAMES)},
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "value", "confidence"],
            },
        },
    },
    "required": ["intent", "intent_confidence", "facts"],
}


class ProviderFactExtractor:
    """Fact extractor on top of the agent's provider layer.

    Runs on whatever ``LLM_PROVIDER`` selects (Claude, OpenAI or a local
    OpenAI-compatible server) through ``LLMProvider.complete_json``, reusing the
    same prompt vocabulary and the same client-side coercion/validation as
    :class:`OpenAIFactExtractor`.
    """

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def extract(
        self,
        user_input: str,
        chat_history: list[dict[str, str]] | list[str],
        current_case_state: CaseState | None,
    ) -> ExtractionPayload:
        from hacienda_gpt.agent.providers.base import ProviderError

        messages = OpenAIFactExtractor._build_messages(user_input, chat_history, current_case_state)
        try:
            data = self._provider.complete_json(
                system=_SYSTEM_PROMPT + _PROVIDER_PROMPT_SUFFIX,
                user=messages[1]["content"],
                schema=_PROVIDER_SCHEMA,
                schema_name="ExtractedFacts",
            )
        except ProviderError as exc:
            raise FactExtractionError(f"Fact extraction failed: {exc}") from exc

        intent = data.get("intent", "unknown")
        if intent not in KNOWN_INTENTS:
            intent = "unknown"
        try:
            confidence = float(data.get("intent_confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        raw_facts = [
            {
                "name": item.get("name"),
                # A model that ignores "always text" and emits 45000 / true is
                # still usable: route the value to the slot of its real type.
                "value_string": item.get("value") if isinstance(item.get("value"), str) else None,
                "value_number": (
                    item.get("value")
                    if isinstance(item.get("value"), int | float) and not isinstance(item.get("value"), bool)
                    else None
                ),
                "value_boolean": item.get("value") if isinstance(item.get("value"), bool) else None,
                "confidence": item.get("confidence", 0.5),
            }
            for item in data.get("facts") or []
            if isinstance(item, dict)
        ]
        facts = OpenAIFactExtractor._materialize_facts(raw_facts)
        return ExtractionPayload(intent=intent, intent_confidence=confidence, extracted_facts=facts)


# --------------------------------------------------------------------------- #
# Value coercion helpers (module-level for unit-testability)
# --------------------------------------------------------------------------- #


# Grabs number-like tokens, separators included ("45.000,50", "12.5").
_NUMERIC_TOKEN_RE = re.compile(r"-?\d[\d.,]*\d|-?\d")
# Strict thousands-grouping shapes: "45.000", "1.234.567" / "45,000", "1,234,567".
_THOUSANDS_DOT_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")
_THOUSANDS_COMMA_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+$")


def _select_amount_token(tokens: list[str]) -> str | None:
    """Pick the token most likely to be the monetary amount.

    The previous code took the *first* numeric token, which silently mis-parsed
    "año 2024 facturé 45.000€" as 2024 — a wrong fiscal figure feeding the
    rules engine. Heuristic instead, two stages:

    1. Prefer tokens that carry a thousands/decimal separator ("45.000,00",
       "45.000", "12,5") over bare digit runs like a year ("2024"); a euro
       amount is far more likely to be written with one.
    2. Among the remaining candidates, take the one with the most digits
       (largest written magnitude), so "facturé 45000 en 2024" still resolves
       to 45000 rather than the year.

    The LLM extractor's ``value_number`` remains authoritative; this string
    fallback only runs for the regex extractor or when the model puts the
    amount in ``value_string``.
    """
    if not tokens:
        return None
    separated = [t for t in tokens if "." in t or "," in t]
    pool = separated or tokens
    return max(pool, key=lambda t: len(re.sub(r"\D", "", t)))


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

    Worded negatives ("menos 3.000") are NOT inferred — only an explicit leading
    ``-`` sign yields a negative; rely on the LLM's signed ``value_number`` for
    losses described in prose.
    """
    token = _select_amount_token(_NUMERIC_TOKEN_RE.findall(value))
    if token is None:
        return None
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


def _provider_extractor() -> FactExtractor:
    from hacienda_gpt import settings
    from hacienda_gpt.agent.providers import create_provider

    return ProviderFactExtractor(
        create_provider(
            model=os.environ.get("DECISION_EXTRACTOR_MODEL") or None,
            # Extraction is a constrained, low-ambiguity task: it does not need
            # the reasoning depth the advisor runs at.
            effort=settings.DECISION_EXTRACTOR_EFFORT,
        )
    )


def _provider_credentials_available() -> bool:
    from hacienda_gpt import settings

    provider = settings.LLM_PROVIDER.strip().lower()
    if provider in ("anthropic", "claude"):
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if provider == "local":
        return bool(settings.LOCAL_LLM_MODEL)
    return False  # "openai" keeps using the dedicated OpenAIFactExtractor below


def default_fact_extractor() -> FactExtractor:
    """Pick the production default extractor based on environment.

    ``DECISION_EXTRACTOR`` forces a backend: ``regex`` (offline heuristic),
    ``openai`` (OpenAI structured output) or ``provider`` (whatever
    ``LLM_PROVIDER`` selects: Claude, OpenAI or a local model).

    Unset, the extractor follows ``LLM_PROVIDER`` when that provider is
    configured (Claude key present, or a local model named), else uses OpenAI
    if ``OPENAI_API_KEY`` is set, else falls back to regex with a log line.
    """

    choice = os.environ.get("DECISION_EXTRACTOR", "").strip().lower()
    if choice == "regex":
        return RegexFactExtractor()
    if choice in ("openai", "llm"):
        return OpenAIFactExtractor()
    if choice == "provider":
        return _provider_extractor()

    if _provider_credentials_available():
        return _provider_extractor()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIFactExtractor()

    logger.warning(
        "No LLM credentials found (ANTHROPIC_API_KEY / OPENAI_API_KEY / LOCAL_LLM_MODEL) — falling back to "
        "RegexFactExtractor, which only detects a handful of facts."
    )
    return RegexFactExtractor()
