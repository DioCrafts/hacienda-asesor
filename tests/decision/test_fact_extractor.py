"""Unit coverage for the LLM extractor's value coercion logic.

These tests pin the contract between the JSON-schema (which lets the model
return any of `value_string`, `value_number`, `value_boolean`) and the
typed `Fact` model the rest of the engine consumes. They are what makes
the per-fact slot rule in the prompt enforceable: if the LLM picks the
wrong slot, the offending fact is dropped instead of contaminating the
decision pipeline.
"""

from __future__ import annotations

import pytest

from hacienda_gpt.decision.fact_extractor import (
    EXPECTED_FACT_TYPE,
    OpenAIFactExtractor,
    _coerce_value,
)
from hacienda_gpt.decision.schemas import FactValueType

# --------------------------------------------------------------------------- #
# _coerce_value — pure unit tests
# --------------------------------------------------------------------------- #


def test_coerce_string_accepts_string_slot() -> None:
    assert (
        _coerce_value(
            expected=FactValueType.STRING,
            value_string="autonomo",
            value_number=None,
            value_boolean=None,
        )
        == "autonomo"
    )


def test_coerce_string_drops_boolean_slot() -> None:
    # The original bug: model returned categoria_contribuyente=value_boolean=true
    # instead of value_string="autonomo". Now that fact must be dropped, not
    # persisted as `True`.
    assert (
        _coerce_value(
            expected=FactValueType.STRING,
            value_string=None,
            value_number=None,
            value_boolean=True,
        )
        is None
    )


def test_coerce_string_accepts_integer_year_as_text() -> None:
    # periodo_fiscal=2024 must survive even if the LLM emits the year as
    # value_number.
    assert (
        _coerce_value(
            expected=FactValueType.STRING,
            value_string=None,
            value_number=2024,
            value_boolean=None,
        )
        == "2024"
    )


def test_coerce_number_accepts_numeric_slot() -> None:
    assert (
        _coerce_value(
            expected=FactValueType.NUMBER,
            value_string=None,
            value_number=45000,
            value_boolean=None,
        )
        == 45000.0
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("45000", 45000.0),
        ("45.000", 45000.0),
        ("45.000,50", 45000.50),
        ("12,5", 12.5),
    ],
)
def test_coerce_number_parses_localized_strings(raw: str, expected: float) -> None:
    got = _coerce_value(
        expected=FactValueType.NUMBER,
        value_string=raw,
        value_number=None,
        value_boolean=None,
    )
    assert got == pytest.approx(expected)


def test_coerce_number_drops_boolean_slot() -> None:
    assert (
        _coerce_value(
            expected=FactValueType.NUMBER,
            value_string=None,
            value_number=None,
            value_boolean=True,
        )
        is None
    )


def test_coerce_boolean_accepts_boolean_slot() -> None:
    assert (
        _coerce_value(
            expected=FactValueType.BOOLEAN,
            value_string=None,
            value_number=None,
            value_boolean=True,
        )
        is True
    )


@pytest.mark.parametrize("raw, expected", [("sí", True), ("si", True), ("no", False)])
def test_coerce_boolean_parses_spanish_yes_no(raw: str, expected: bool) -> None:
    assert (
        _coerce_value(
            expected=FactValueType.BOOLEAN,
            value_string=raw,
            value_number=None,
            value_boolean=None,
        )
        is expected
    )


# --------------------------------------------------------------------------- #
# OpenAIFactExtractor._materialize_facts — end-to-end of the post-processing
# --------------------------------------------------------------------------- #


def test_materialize_drops_facts_with_wrong_slot() -> None:
    raw = [
        {
            "name": "categoria_contribuyente",
            "value_string": None,
            "value_number": None,
            "value_boolean": True,  # wrong slot — must be dropped
            "confidence": 0.9,
        },
        {
            "name": "volumen_facturacion",
            "value_string": None,
            "value_number": 45000,
            "value_boolean": None,
            "confidence": 0.95,
        },
    ]
    facts = OpenAIFactExtractor._materialize_facts(raw)
    names = {f.name for f in facts}
    assert names == {"volumen_facturacion"}
    assert facts[0].value == 45000.0
    assert facts[0].value_type is FactValueType.NUMBER


def test_materialize_uses_expected_type_even_when_extractor_disagrees() -> None:
    # The LLM emits periodo_fiscal as value_number=2024, but periodo_fiscal is
    # typed as STRING — we must coerce to "2024".
    raw = [
        {
            "name": "periodo_fiscal",
            "value_string": None,
            "value_number": 2024,
            "value_boolean": None,
            "confidence": 0.9,
        }
    ]
    facts = OpenAIFactExtractor._materialize_facts(raw)
    assert len(facts) == 1
    assert facts[0].name == "periodo_fiscal"
    assert facts[0].value == "2024"
    assert facts[0].value_type is FactValueType.STRING


def test_materialize_dedupes_by_name_keeping_highest_confidence() -> None:
    raw = [
        {
            "name": "residencia_fiscal",
            "value_string": "ES",
            "value_number": None,
            "value_boolean": None,
            "confidence": 0.7,
        },
        {
            "name": "residencia_fiscal",
            "value_string": "ES",
            "value_number": None,
            "value_boolean": None,
            "confidence": 0.95,
        },
    ]
    facts = OpenAIFactExtractor._materialize_facts(raw)
    assert len(facts) == 1
    assert facts[0].confidence == pytest.approx(0.95)


def test_materialize_skips_unknown_fact_names() -> None:
    raw = [
        {
            "name": "intencion_oculta",
            "value_string": "lo_que_sea",
            "value_number": None,
            "value_boolean": None,
            "confidence": 0.9,
        }
    ]
    assert OpenAIFactExtractor._materialize_facts(raw) == []


def test_expected_fact_type_covers_all_known_facts() -> None:
    # Regression guard: anyone adding a new fact to KNOWN_FACT_NAMES must also
    # declare its expected value type, otherwise the extractor will silently
    # drop every emission of that fact.
    from hacienda_gpt.decision.fact_extractor import KNOWN_FACT_NAMES

    assert set(KNOWN_FACT_NAMES) == set(EXPECTED_FACT_TYPE.keys())
