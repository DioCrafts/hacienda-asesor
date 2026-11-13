"""Self-check of the legal data: every figure is inside its quote and every quote is verbatim in the law.

The fixtures are extracts (the quoted paragraphs) of the consolidated texts on boe.es. With
``HIPOTECA_BOE_DIR`` pointing to a folder with the full pages (``<BOE-ID>.html``) the check runs on them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from hipoteca_util import FIXTURES
import pytest

from hacienda_gpt.modulos.hipoteca.normativa import (
    NORMATIVA_PATH,
    Normativa,
    cargar_normativa,
    comprobar_evidencias,
    leer_cifra,
    textos_de_directorio,
)


def test_normativa_coherente_con_los_extractos():
    textos = textos_de_directorio(FIXTURES)
    assert set(textos) >= set(cargar_normativa().fuentes_principales)
    assert comprobar_evidencias(cargar_normativa(), textos) == []


@pytest.mark.skipif(not os.environ.get("HIPOTECA_BOE_DIR"), reason="sin HIPOTECA_BOE_DIR con las páginas del BOE")
def test_normativa_coherente_con_las_paginas_completas():
    assert comprobar_evidencias(cargar_normativa(), textos_de_directorio(Path(os.environ["HIPOTECA_BOE_DIR"]))) == []


def _alterada(cambio) -> Normativa:
    raw = json.loads(NORMATIVA_PATH.read_text(encoding="utf-8"))
    cambio(raw)
    return Normativa.model_validate(raw)


def test_detecta_una_cifra_que_no_dice_la_ley():
    def cambio(raw):
        raw["comision_variable"]["opcion_b_limite"] = "0.003"

    problemas = comprobar_evidencias(_alterada(cambio), textos_de_directorio(FIXTURES))
    assert problemas == ["comision_variable.opcion_b_limite: el dato es 0.003 y la cita dice «0,25 por ciento»"]


def test_detecta_una_cita_inventada_y_una_cifra_sin_evidencia():
    def cambio(raw):
        raw["comision_fijo"]["evidencia"][1]["cita"] += " Y algo más."
        raw["comision_fijo"]["evidencia"][1]["cifras"] = {}

    problemas = comprobar_evidencias(_alterada(cambio), textos_de_directorio(FIXTURES))
    assert "comision_fijo: la cita de art. 23.7.b) Ley 5/2019 no aparece literal en BOE-A-2019-3814" in problemas
    assert "comision_fijo.limite_posterior: cifra sin evidencia" in problemas


def test_fechas_de_vigencia_leidas_de_la_ley():
    n = cargar_normativa()
    assert str(n.ley_5_2019.entrada_en_vigor) == "2019-06-16"
    assert str(n.suspension_comisiones_2022_2024.hasta) == "2024-12-31"
    assert str(n.ley_41_2007.entrada_en_vigor) == "2007-12-09"


@pytest.mark.parametrize(
    "literal, valor",
    [
        ("0,15 por ciento", "0.0015"),
        ("1 por 100", "0.01"),
        ("300.000 euros", "300000"),
        ("5 primeros años", "5"),
        ("al menos, 3 años", "3"),
        ("treinta y seis meses", "36"),
        ("un mes", "1"),
    ],
)
def test_leer_cifra(literal, valor):
    from decimal import Decimal

    assert leer_cifra(literal) == Decimal(valor)
