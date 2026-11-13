import json

import pytest

from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.decision import fact_extractor as fx
from hacienda_gpt.decision.fact_extractor import (
    FactExtractionError,
    OpenAIFactExtractor,
    ProviderFactExtractor,
    RegexFactExtractor,
    default_fact_extractor,
)
from hacienda_gpt.decision.schemas import CaseState


class _JsonProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, response) -> None:
        self.response = response
        self.requests: list[dict] = []

    def complete_json(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_provider_extractor_coerces_string_values_and_drops_bad_ones() -> None:
    provider = _JsonProvider(
        {
            "intent": "autonomo",
            "intent_confidence": 1.7,
            "facts": [
                {"name": "volumen_facturacion", "value": "45.000", "confidence": 0.9},
                {"name": "alta_actividad_economica", "value": "true", "confidence": 0.8},
                {"name": "residencia_fiscal", "value": "ES", "confidence": 0.95},
                {"name": "plantilla", "value": 0, "confidence": 0.7},  # model ignored "always text"
                {"name": "importe_renta_aproximado", "value": "bastante", "confidence": 0.9},
                {"name": "hecho_inventado", "value": "x", "confidence": 0.9},
                "basura",
            ],
        }
    )
    case = CaseState(case_id="c1", user_id="u", jurisdiction="ES", tax_period="2025")

    payload = ProviderFactExtractor(provider).extract(
        "soy autónomo y facturo 45.000 €", [{"role": "user", "content": "hola"}], case
    )

    assert payload.intent == "autonomo" and payload.intent_confidence == 1.0
    assert {fact.name: fact.value for fact in payload.extracted_facts} == {
        "volumen_facturacion": 45000.0,
        "alta_actividad_economica": True,
        "residencia_fiscal": "ES",
        "plantilla": 0.0,
    }

    request = provider.requests[0]
    assert request["schema_name"] == "ExtractedFacts"
    assert "siempre como" in request["system"]
    user_payload = json.loads(request["user"])
    assert user_payload["user_input"] == "soy autónomo y facturo 45.000 €"
    assert user_payload["current_case_state"]["case_id"] == "c1"
    # No constructs that provider-side structured output commonly rejects.
    schema_text = json.dumps(request["schema"])
    assert "minimum" not in schema_text and '"null"' not in schema_text


def test_provider_extractor_wraps_provider_failures() -> None:
    extractor = ProviderFactExtractor(_JsonProvider(ProviderError("sin red")))
    with pytest.raises(FactExtractionError, match="sin red"):
        extractor.extract("hola", [], None)


def test_default_extractor_follows_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DECISION_EXTRACTOR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from hacienda_gpt import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "LOCAL_LLM_MODEL", "")
    assert isinstance(default_fact_extractor(), RegexFactExtractor)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(default_fact_extractor(), OpenAIFactExtractor)

    # A configured Claude wins over a stray OpenAI key: LLM_PROVIDER decides.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    extractor = default_fact_extractor()
    assert isinstance(extractor, ProviderFactExtractor)
    assert (extractor._provider.name, extractor._provider.effort) == ("anthropic", "low")

    monkeypatch.setenv("DECISION_EXTRACTOR", "regex")
    assert isinstance(default_fact_extractor(), RegexFactExtractor)

    monkeypatch.setenv("DECISION_EXTRACTOR", "provider")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "local")
    monkeypatch.setattr(settings, "LOCAL_LLM_MODEL", "qwen3:14b")
    assert default_fact_extractor()._provider.name == "local"
    assert fx.FactExtractionError is fx.OpenAIExtractionError
