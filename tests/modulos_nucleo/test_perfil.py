"""Module facts in the household profile: types, validation, canonical JSON, minimisation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType
from hacienda_gpt.modulos.perfil import (
    HechoInvalidoError,
    hecho_de_modulo,
    hechos_para_modulo,
    normalizar_valor,
    perfil_de,
    registrar_hechos_de_modulo,
    tipo_esperado,
)
from .fakes import Llamadas, agua, instalar, seguros


@pytest.fixture
def reg():
    llamadas = Llamadas()
    return instalar(agua(llamadas), seguros(llamadas))


def _hecho(reg, nombre):
    return reg.hechos()[nombre][1]


def test_vocabulary_grows_with_the_modules(reg) -> None:
    assert tipo_esperado("agua_lecturas") is FactValueType.STRING  # JSON facts are stored as text
    assert tipo_esperado("agua_personas") is FactValueType.NUMBER
    assert tipo_esperado("edad") is FactValueType.NUMBER  # the tax vocabulary is untouched
    assert tipo_esperado("desconocido") is None
    # A shared fact a module declares stays the core's.
    assert hecho_de_modulo("comunidad_autonoma") is None


def test_values_are_coerced_then_validated_by_the_module(reg) -> None:
    assert normalizar_valor(_hecho(reg, "agua_personas"), "3") == 3.0
    assert normalizar_valor(_hecho(reg, "agua_bonificada"), "sí") is True
    assert normalizar_valor(_hecho(reg, "agua_compania"), " canal ") == "CANAL"
    with pytest.raises(HechoInvalidoError, match="Indica el nombre de la compañía"):
        normalizar_valor(_hecho(reg, "agua_compania"), "x")
    with pytest.raises(HechoInvalidoError, match="se espera un número"):
        normalizar_valor(_hecho(reg, "agua_personas"), "muchas")


def test_json_facts_are_validated_and_stored_canonical(reg) -> None:
    hecho = _hecho(reg, "agua_lecturas")
    texto = normalizar_valor(hecho, '[{"mes": "2026-02", "m3": 9}, {"m3": 8, "mes": "2026-01"}]')
    assert texto == '[{"m3":8.0,"mes":"2026-01"},{"m3":9.0,"mes":"2026-02"}]'
    # The same value given parsed gives the same text.
    assert normalizar_valor(hecho, [{"m3": 9, "mes": "2026-02"}, {"m3": 8, "mes": "2026-01"}]) == texto
    with pytest.raises(HechoInvalidoError, match="JSON válido"):
        normalizar_valor(hecho, "[{")
    with pytest.raises(HechoInvalidoError, match="lista de objetos"):
        normalizar_valor(hecho, '{"m3": 1}')


def _case(*facts: tuple[str, object, FactValueType]) -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="c1",
        user_id="u",
        jurisdiction="ES",
        tax_period="2026",
        created_at=now,
        updated_at=now,
        facts=[
            Fact(fact_id=f"fact_{n}", name=n, value=v, value_type=t, source="t", confidence=1.0) for n, v, t in facts
        ],
    )


def test_profile_parses_json_facts_and_minimises_what_a_specialist_sees(reg) -> None:
    case = _case(
        ("agua_lecturas", '[{"m3":8.0,"mes":"2026-01"}]', FactValueType.STRING),
        ("descendientes", '[{"edad":4}]', FactValueType.STRING),
        ("seguros_poliza", "P-123", FactValueType.STRING),
        ("comunidad_autonoma", "Madrid", FactValueType.STRING),
    )
    perfil = perfil_de(case)
    assert perfil["agua_lecturas"] == [{"m3": 8.0, "mes": "2026-01"}] and perfil["descendientes"] == [{"edad": 4}]
    visto = hechos_para_modulo(perfil, reg.get("agua"))
    assert "seguros_poliza" not in visto and visto["comunidad_autonoma"] == "Madrid" and "agua_lecturas" in visto


def test_a_module_writes_only_its_own_facts(reg) -> None:
    case = _case()
    updated = registrar_hechos_de_modulo(case, reg.get("agua"), {"agua_personas": 4}, fuente="modulo:agua")
    assert [(f.name, f.value, f.source) for f in updated.facts] == [("agua_personas", 4.0, "modulo:agua")]
    with pytest.raises(HechoInvalidoError):
        registrar_hechos_de_modulo(case, reg.get("agua"), {"seguros_poliza": "x"}, fuente="modulo:agua")
    with pytest.raises(HechoInvalidoError):
        registrar_hechos_de_modulo(case, reg.get("agua"), {"comunidad_autonoma": "x"}, fuente="modulo:agua")
