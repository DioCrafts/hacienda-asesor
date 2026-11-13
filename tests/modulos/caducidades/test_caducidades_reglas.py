"""Self-check of the module's legal data: every figure is in its quote and every quote is in the law.

The extracts under ``tests/fixtures/modulos/caducidades`` are the lines of the consolidated texts on boe.es
that hold the quoted passages (downloaded on 2026-11-13; the full pages were checked with
``python -m hacienda_gpt.modulos.caducidades.reglas <dir>`` and their SHA-256 is in ``reglas.json``).
"""

from decimal import Decimal
from pathlib import Path

import pytest

from hacienda_gpt.modulos.caducidades.reglas import (
    Reglas,
    cargar_reglas,
    cifra_a_numero,
    comprobar_evidencias,
    textos_de_directorio,
    textos_literales,
)

EXTRACTOS = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "caducidades"


def _textos() -> dict[str, str]:
    return textos_de_directorio(EXTRACTOS)


def test_every_figure_is_in_its_quote_and_every_quote_in_the_law() -> None:
    assert comprobar_evidencias(cargar_reglas(), _textos()) == []


def test_every_source_has_an_extract() -> None:
    reglas = cargar_reglas()
    assert set(reglas.fuentes_principales) == set(_textos())
    for boe_id, fuente in reglas.fuentes_principales.items():
        assert fuente.url == f"https://www.boe.es/buscar/act.php?id={boe_id}"
        assert len(fuente.sha256_html) == 64


def test_a_changed_figure_is_caught() -> None:
    datos = cargar_reglas().model_dump(mode="json")
    datos["dni"]["tramos"][2]["anos"] = 15
    problemas = comprobar_evidencias(Reglas.model_validate(datos))
    assert any("dni.tramos.2.anos" in p for p in problemas)


def test_a_changed_itv_frequency_is_caught() -> None:
    datos = cargar_reglas().model_dump(mode="json")
    datos["itv"]["categorias"]["M1"]["tramos"][2]["meses"] = 24
    problemas = comprobar_evidencias(Reglas.model_validate(datos))
    assert any("categorias.M1.tramos.2.meses" in p for p in problemas)


def test_a_figure_without_evidence_is_caught() -> None:
    datos = cargar_reglas().model_dump(mode="json")
    datos["garantia"]["evidencia"][0]["cifras"].pop("bienes_anos")
    problemas = comprobar_evidencias(Reglas.model_validate(datos))
    assert "garantia.bienes_anos: cifra sin evidencia" in problemas


def test_a_quote_not_in_the_law_is_caught() -> None:
    datos = cargar_reglas().model_dump(mode="json")
    datos["permiso_conduccion"]["evidencia"][0]["cita"] = datos["permiso_conduccion"]["evidencia"][0]["cita"].replace(
        "tres años", "cuatro años"
    )
    datos["permiso_conduccion"]["evidencia"][0]["cifras"]["grupo2.anos_desde_edad"] = "cuatro años"
    datos["permiso_conduccion"]["grupo2"]["anos_desde_edad"] = 4
    problemas = comprobar_evidencias(Reglas.model_validate(datos), _textos())
    assert any("no aparece literal en BOE-A-2009-9481" in p for p in problemas)


def test_the_withdrawal_exceptions_are_the_literal_letters_of_article_103() -> None:
    reglas = cargar_reglas()
    assert [e.letra for e in reglas.desistimiento.excepciones] == list("abcdefghijklm")
    literales = [texto for grupo, _, texto in textos_literales(reglas) if grupo == "desistimiento"]
    assert all(e.texto in literales for e in reglas.desistimiento.excepciones)


@pytest.mark.parametrize(
    ("literal", "valor"),
    [
        ("Dos años", 2),
        ("sesenta y cinco años", 65),
        ("30 días naturales", 30),
        ("Bienal", 24),
        ("Semestral", 6),
        ("el año siguiente", 1),
        ("un mes", 1),
        ("catorce días naturales", 14),
    ],
)
def test_figures_are_read_from_their_spelling(literal: str, valor: int) -> None:
    assert cifra_a_numero(literal) == Decimal(valor)


def test_unknown_spelling_is_rejected() -> None:
    with pytest.raises(ValueError):
        cifra_a_numero("varios años")


def test_every_age_table_ends_open() -> None:
    reglas = cargar_reglas()
    assert reglas.dni.tramos[-1].hasta_edad is None
    assert reglas.pasaporte.tramos[-1].hasta_edad is None
    for categoria in reglas.itv.categorias.values():
        assert categoria.tramos[-1].hasta_anos is None
        limites = [t.hasta_anos for t in categoria.tramos[:-1]]
        assert limites == sorted(limites)


def test_official_sites_are_roots_only() -> None:
    reglas = cargar_reglas()
    for grupo in (reglas.dni, reglas.pasaporte, reglas.permiso_conduccion, reglas.itv, reglas.certificado_electronico):
        for web in grupo.tramite.webs:
            assert web.startswith("https://") and web.count("/") == 3 and web.endswith("/")
