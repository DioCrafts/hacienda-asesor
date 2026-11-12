"""Impuesto sobre el Patrimonio and ITSGF, accrual of 31-12-2026.

Every expected figure is worked out by hand in the comments from the scales as
printed in the laws (tests/fixtures/boe_patrimonio holds the quoted articles).
"""

from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path

import pytest

from hacienda_gpt.tax.params import EjercicioNoDisponibleError
from hacienda_gpt.tax.patrimonio import ComunidadNoDisponibleError, PatrimonioInput, calcular_patrimonio
from hacienda_gpt.tax.patrimonio_params import (
    PatrimonioParams,
    comprobar_evidencias,
    comprobar_sin_regulacion,
    ejercicios_patrimonio_disponibles,
    load_patrimonio_params,
    textos_de_directorio,
)

D = Decimal
EXTRACTOS = Path(__file__).resolve().parents[1] / "fixtures" / "boe_patrimonio"
COMUNIDADES = {
    "andalucia",
    "aragon",
    "asturias",
    "baleares",
    "canarias",
    "cantabria",
    "castilla_la_mancha",
    "castilla_y_leon",
    "cataluna",
    "comunidad_valenciana",
    "extremadura",
    "galicia",
    "la_rioja",
    "madrid",
    "murcia",
}


def _calc(comunidad: str, **kwargs):
    return calcular_patrimonio(PatrimonioInput(comunidad=comunidad, **{k: D(str(v)) for k, v in kwargs.items()}))


# --- Data ---------------------------------------------------------------------


def test_the_fifteen_common_regime_communities_are_recorded() -> None:
    p = load_patrimonio_params(2026)
    assert ejercicios_patrimonio_disponibles() == [2026]
    assert set(p.comunidades) == COMUNIDADES
    assert [k for k, c in p.comunidades.items() if not c.regula] == ["castilla_la_mancha"]
    # A community that regulates nothing has no parameters of its own, but is there.
    clm = p.comunidades["castilla_la_mancha"]
    assert (clm.minimo_exento, clm.escala, clm.bonificacion, clm.no_modelado) == (None, None, None, [])
    assert p.itsgf.vigencia.vigente


def test_every_figure_is_in_its_quote_and_every_quote_is_in_the_law() -> None:
    p = load_patrimonio_params(2026)
    textos = textos_de_directorio(EXTRACTOS)
    citadas = {ev.boe_id for grupo in p.grupos().values() for ev in grupo.evidencia}
    assert citadas <= set(textos) <= set(p.fuentes_principales)
    assert comprobar_evidencias(p, textos) == []


def test_quotes_against_the_full_downloaded_pages_when_available() -> None:
    # Point HACIENDA_BOE_PAGINAS at a directory with <ID>.html straight from boe.es.
    directorio = os.environ.get("HACIENDA_BOE_PAGINAS")
    if not directorio:
        pytest.skip("HACIENDA_BOE_PAGINAS no está definido")
    p = load_patrimonio_params(2026)
    textos = textos_de_directorio(Path(directorio))
    if not set(p.fuentes_principales) <= set(textos):
        pytest.skip("el directorio no contiene las páginas del Impuesto sobre el Patrimonio")
    assert comprobar_evidencias(p, textos) == []
    assert comprobar_sin_regulacion(p, textos) == []


def test_self_check_catches_wrong_figures_quotes_and_scales() -> None:
    p = load_patrimonio_params(2026)
    textos = textos_de_directorio(EXTRACTOS)
    raw = p.model_dump(mode="json")

    def con(cambio) -> list[str]:
        copia = PatrimonioParams.model_validate(raw).model_dump(mode="json")
        cambio(copia)
        return comprobar_evidencias(PatrimonioParams.model_validate(copia), textos)

    def minimo_madrid(d) -> None:
        d["comunidades"]["madrid"]["minimo_exento"]["importe"] = "800000"

    assert con(minimo_madrid) == [
        "comunidades.madrid.minimo_exento.importe: el dato es 800000 y la cita dice «700.000 euros»"
    ]

    def cita_inventada(d) -> None:
        d["estado"]["obligacion_declarar"]["evidencia"][0]["cita"] = "Declaran quienes superen 2.000.000 euros."

    assert con(cita_inventada) == [
        "estado.obligacion_declarar: la cita de art. 37 Ley 19/1991 no aparece literal en BOE-A-1991-14392"
    ]

    def sin_evidencia(d) -> None:
        del d["itsgf"]["hecho_imponible"]["evidencia"][0]["cifras"]["umbral_patrimonio_neto"]

    assert con(sin_evidencia) == ["itsgf.hecho_imponible.umbral_patrimonio_neto: cifra sin evidencia"]

    # A rate changed in data AND in its evidence still fails: the printed cumulative column no longer adds up.
    def tipo_coherente_pero_falso(d) -> None:
        escala = d["comunidades"]["cataluna"]["escala"]
        escala["tramos"][3]["tipo"] = "0.00545"
        escala["evidencia"][0]["cifras"]["tramos.3.tipo"] = "0,525"

    problemas = con(tipo_coherente_pero_falso)
    assert len(problemas) == 2
    assert problemas[0] == "comunidades.cataluna.escala.tramos.3.tipo: el dato es 0.00545 y la cita dice «0,525»"
    assert problemas[1].startswith(
        "comunidades.cataluna.escala.tramos.4: la cuota acumulada impresa (8949.54) no cuadra"
    )


def test_not_regulating_is_checked_against_the_full_text() -> None:
    p = load_patrimonio_params(2026)
    limpio = {"BOE-A-2014-1368": "CAPÍTULO I\nImpuesto sobre la Renta de las Personas Físicas\nArtículo 1. Deducción."}
    assert comprobar_sin_regulacion(p, limpio) == []
    con_capitulo = {"BOE-A-2014-1368": "CAPÍTULO II\nImpuesto sobre el Patrimonio\nArtículo 9. Mínimo exento."}
    assert comprobar_sin_regulacion(p, con_capitulo) == [
        "comunidades.castilla_la_mancha: BOE-A-2014-1368 sí contiene «Impuesto sobre el Patrimonio»"
    ]


def test_allowances_tied_to_the_itsgf_need_the_itsgf_in_force() -> None:
    raw = load_patrimonio_params(2026).model_dump(mode="json")
    raw["itsgf"]["vigencia"]["vigente"] = False
    with pytest.raises(ValueError, match="bonificación ligada al ITSGF"):
        PatrimonioParams.model_validate(raw)


# --- State rules ----------------------------------------------------------------


def test_state_scale_main_home_exemption_and_proportional_debt() -> None:
    # Castilla-La Mancha regulates nothing: state minimum and scale, no allowance.
    r = _calc("Castilla-La Mancha", vivienda_habitual=500_000, deuda_vivienda_habitual=100_000, depositos=1_500_000)
    assert not r.comunidad_regula and (r.origen_minimo_exento, r.origen_escala) == ("estatal", "estatal")
    assert r.bienes_y_derechos == D("2000000.00")
    assert r.exencion_vivienda_habitual == D("300000.00")  # art. 4.Nueve: up to 300.000
    # Home loan deductible in the non-exempt proportion: 100.000 x (500.000 - 300.000) / 500.000 = 40.000
    assert r.deudas_deducibles == D("40000.00")
    # 500.000 - 300.000 + 1.500.000 - 40.000 = 1.660.000
    assert r.base_imponible == D("1660000.00")
    assert r.minimo_exento == D("700000.00")
    assert r.base_liquidable == D("960000.00")
    # State scale, bracket from 668.499,75 (cuota 2.506,86) at 0,9 %:
    # 2.506,86 + (960.000 - 668.499,75) x 0,009 = 2.506,86 + 2.623,50225 = 5.130,36
    assert r.cuota_integra == D("5130.36")
    assert r.reduccion_limite_conjunto is None  # no IRPF figures: not computed, and said so
    assert any("No se ha aplicado el límite conjunto" in a for a in r.avisos)
    assert r.bonificacion_autonomica == 0
    assert r.cuota_a_ingresar == r.total_a_ingresar == D("5130.36")
    assert r.itsgf is not None and not r.itsgf.sujeto and r.itsgf.cuota_a_ingresar == 0
    assert r.obligado_a_declarar and "resulta a ingresar" in r.motivo_obligacion
    assert r.aplicable_2025 and r.parametros_no_verificados == []
    assert "BOE-A-1991-14392" in r.fuente and "BOE-A-2014-1368" in r.fuente
    assert {linea.concepto: linea.importe for linea in r.lineas}["Cuota íntegra (escala estatal)"] == D("5130.36")


def test_nothing_to_pay_below_the_minimum_and_filing_threshold_of_two_million() -> None:
    # 600.000 of deposits < 700.000: no tax, and assets under 2.000.000: no return.
    r = _calc("castilla y leon", depositos=600_000)
    assert r.base_liquidable == r.cuota_a_ingresar == 0 and not r.obligado_a_declarar
    assert r.no_modelado == [
        "art. 11: Exención de los patrimonios especialmente protegidos de contribuyentes con discapacidad"
    ]
    # Exempt family company of 2.500.000 and nothing else: no tax, but assets > 2.000.000 (art. 37).
    r = _calc("aragon", bienes_exentos_empresa_familiar=2_500_000)
    assert r.base_imponible == r.cuota_a_ingresar == 0
    assert r.obligado_a_declarar and "supera 2.000.000 €" in r.motivo_obligacion
    # Exactly 2.000.000 is not "superior a".
    r = _calc("aragon", bienes_exentos_empresa_familiar=2_000_000)
    assert not r.obligado_a_declarar
    # Art. 37 does not say whether exempt assets count: the prudent reading is always announced when no tax is due.
    assert any("criterio prudente" in a and "no dice si cuentan los exentos" in a for a in r.avisos)
    # Debts are not subtracted for that threshold: 2.500.000 of assets with 2.400.000 of debts still files.
    r = _calc("aragon", depositos=2_500_000, otras_deudas=2_400_000)
    assert r.base_imponible == D("100000.00") and r.cuota_a_ingresar == 0 and r.obligado_a_declarar


def test_joint_limit_with_the_irpf() -> None:
    # 3.000.000 in deposits, state rules: base liquidable 2.300.000.
    # Bracket from 1.336.999,51 (cuota 8.523,36) at 1,3 %: 8.523,36 + 963.000,49 x 0,013 = 8.523,36 + 12.519,00637 = 21.042,37
    # IRPF: bases 120.000 -> limit 60 % = 72.000; IRPF quota 60.000.
    # 60.000 + 21.042,37 - 72.000 = 9.042,37 of excess; cap 80 % x 21.042,37 = 16.833,90 -> reduction 9.042,37
    r = _calc("castilla_la_mancha", depositos=3_000_000, irpf_base_imponible=120_000, irpf_cuota_integra=60_000)
    assert r.cuota_integra == D("21042.37")
    assert r.reduccion_limite_conjunto == D("9042.37")
    assert r.cuota_a_ingresar == D("12000.00")
    # Half of the base (1.500.000) cannot produce income: only 10.521,185 counts; 60.000 + 10.521,19 < 72.000.
    r = _calc(
        "castilla_la_mancha",
        depositos=3_000_000,
        irpf_base_imponible=120_000,
        irpf_cuota_integra=60_000,
        bienes_no_productivos=1_500_000,
    )
    assert r.reduccion_limite_conjunto == 0 and r.cuota_a_ingresar == D("21042.37")
    # Only one of the two IRPF figures: the limit is not computed.
    r = _calc("castilla_la_mancha", depositos=3_000_000, irpf_base_imponible=120_000)
    assert r.reduccion_limite_conjunto is None and any("hacen falta las dos cifras" in a for a in r.avisos)


def test_joint_limit_capped_at_80_percent_and_itsgf_limit() -> None:
    # 10.000.000 in deposits, state rules: base liquidable 9.300.000.
    # Patrimonio, bracket from 5.347.998,03 (cuota 71.362,33) at 2,1 %:
    #   71.362,33 + 3.952.001,97 x 0,021 = 71.362,33 + 82.992,04137 = 154.354,37
    # IRPF: bases 100.000 -> limit 60.000; quota 30.000. Excess 30.000 + 154.354,37 - 60.000 = 124.354,37,
    #   capped at 80 % x 154.354,37 = 123.483,496 -> 123.483,50. Quota left: 30.870,87 (the 20 % floor).
    r = _calc("castilla_la_mancha", depositos=10_000_000, irpf_base_imponible=100_000, irpf_cuota_integra=30_000)
    assert r.cuota_integra == D("154354.37")
    assert r.reduccion_limite_conjunto == D("123483.50")
    assert r.cuota_a_ingresar == D("30870.87")
    # ITSGF: base liquidable 10.000.000 - 700.000 = 9.300.000; bracket from 5.347.998,03 (cuota 39.915,97) at 2,1 %:
    #   39.915,97 + 82.992,04137 = 122.908,01
    # Limit: 30.000 + 30.870,87 + 122.908,01 - 60.000 = 123.778,88, capped at 80 % x 122.908,01 = 98.326,41
    #   -> 24.581,60, less the Patrimonio paid (30.870,87) -> nothing to pay.
    assert r.itsgf is not None and r.itsgf.sujeto
    assert r.itsgf.base_liquidable == D("9300000.00")
    assert r.itsgf.cuota_integra == D("122908.01")
    assert r.itsgf.reduccion_limite_conjunto == D("98326.41")
    assert r.itsgf.cuota_tras_limite == D("24581.60")
    assert r.itsgf.deduccion_patrimonio == D("24581.60")
    assert r.itsgf.cuota_a_ingresar == 0 and not r.itsgf.obligado_a_declarar
    assert r.total_a_ingresar == D("30870.87")


def test_itsgf_limit_adds_the_whole_wealth_tax_quota_left_by_its_own_limit() -> None:
    # Form 718 (annex of Orden HFP/587/2023, replaced by Orden HAC/652/2026), box 180 = IRPF quotas + box 152
    # "Cuota íntegra del Impuesto sobre el Patrimonio (casilla [40] de la declaración…)" + box 153 (limitable ITSGF part).
    # Madrid, 8.000.000 in deposits, half of them unable to produce income; IRPF bases 50.000 (limit 30.000), quota 12.000.
    # Patrimonio (state scale) on 7.300.000: 71.362,33 + 1.952.001,97 x 0,021 = 71.362,33 + 40.992,04137 = 112.354,37
    #   limitable half 56.177,185: 12.000 + 56.177,185 - 30.000 = 38.177,185 -> reduction 38.177,19 (cap 89.883,50)
    #   total cuota íntegra (box 40) = 74.177,18
    # ITSGF on 7.300.000: 39.915,97 + 40.992,04137 = 80.908,01; limitable half 40.454,005
    #   12.000 + 74.177,18 + 40.454,005 - 30.000 = 96.631,185, capped at 80 % x 80.908,01 = 64.726,41 -> 16.181,60
    # Madrid's allowance: 74.177,18 - 16.181,60 = 57.995,58 -> Patrimonio 16.181,60, ITSGF 0.
    r = _calc(
        "madrid",
        depositos=8_000_000,
        irpf_base_imponible=50_000,
        irpf_cuota_integra=12_000,
        bienes_no_productivos=4_000_000,
    )
    assert (r.cuota_integra, r.reduccion_limite_conjunto, r.cuota_tras_limite) == (
        D("112354.37"),
        D("38177.19"),
        D("74177.18"),
    )
    assert r.itsgf is not None
    assert (r.itsgf.cuota_integra, r.itsgf.reduccion_limite_conjunto, r.itsgf.cuota_tras_limite) == (
        D("80908.01"),
        D("64726.41"),
        D("16181.60"),
    )
    assert (r.bonificacion_autonomica, r.cuota_a_ingresar, r.itsgf.cuota_a_ingresar) == (
        D("57995.58"),
        D("16181.60"),
        0,
    )
    # The stricter reading (only the limitable part of the wealth-tax quota: 12.000 + 18.000 + 40.454,005 - 30.000
    # = 40.454,00 of reduction -> 40.454,01) is reported, not applied; so is the proration of art. 31.Uno.b.
    assert any("modelo 718 (casilla 152)" in a for a in r.avisos)
    assert any("40.454,01 €" in a and "16.181,60 €" in a for a in r.avisos)
    assert any("prorrateado la cuota íntegra" in a and "(50,00 % de la cuota)" in a for a in r.avisos)
    # Without unproductive assets both readings coincide and nothing extra is said.
    r = _calc("madrid", depositos=8_000_000, irpf_base_imponible=50_000, irpf_cuota_integra=12_000)
    assert r.cuota_tras_limite == D("22470.87") and r.itsgf is not None and r.itsgf.cuota_tras_limite == D("16181.60")
    assert not any("lectura estricta" in a for a in r.avisos)


def test_five_million_in_six_communities() -> None:
    # 5.000.000 in deposits. ITSGF everywhere: (5.000.000 - 700.000 - 3.000.000) x 1,7 % = 22.100.
    # State scale on 4.300.000: 25.904,35 + 1.626.000,99 x 0,017 = 25.904,35 + 27.642,01683 = 53.546,37
    # Cataluña on 4.500.000: 27.199,58 + 1.826.000,99 x 0,01785 = 27.199,58 + 32.594,11767 = 59.793,70
    # Extremadura on 4.500.000: 38.856,53 + 1.826.000,99 x 0,0255 = 38.856,53 + 46.563,02525 = 85.419,56
    # Cantabria on 4.300.000: 64.814,82 (see its own test)
    esperado = {  # cuota íntegra, Patrimonio payable, ITSGF payable
        "madrid": ("53546.37", "22100.00", "0"),  # allowance 31.446,37: pays the ITSGF quota to the community
        "cataluna": ("59793.70", "59793.70", "0"),  # no allowance; Patrimonio covers the ITSGF
        "galicia": ("53546.37", "26773.18", "0"),  # 50 % = 26.773,185 -> 26.773,19; what is left exceeds 22.100
        "extremadura": ("85419.56", "0", "22100.00"),  # 100 % not tied to the ITSGF: the State collects it
        "cantabria": ("64814.82", "22100.00", "0"),  # 4.300.000 > 3.000.000: allowance by difference
        "castilla_la_mancha": ("53546.37", "53546.37", "0"),  # state rules, no allowance
    }
    for comunidad, (integra, patrimonio, itsgf) in esperado.items():
        r = _calc(comunidad, depositos=5_000_000)
        assert r.itsgf is not None and r.itsgf.cuota_integra == D("22100.00"), comunidad
        assert (r.cuota_integra, r.cuota_a_ingresar, r.itsgf.cuota_a_ingresar) == (
            D(integra),
            D(patrimonio),
            D(itsgf),
        ), comunidad
    extremadura = _calc("extremadura", depositos=5_000_000)
    assert any("no está ligada al ITSGF" in a for a in extremadura.avisos)
    assert any("1.336.999,01" in a for a in extremadura.avisos)


# --- Regional minimums and scales -------------------------------------------------


def test_own_minimum_and_own_scale_cataluna() -> None:
    # Minimum 500.000 (art. 621-1); scale of the transitional provision 1.ª.
    # 1.500.000 - 500.000 = 1.000.000; bracket from 668.499,75 (cuota 2.632,21) at 0,945 %:
    # 2.632,21 + 331.500,25 x 0,00945 = 2.632,21 + 3.132,67736 = 5.764,89
    r = _calc("Cataluña", depositos=1_500_000)
    assert (r.origen_minimo_exento, r.origen_escala) == ("autonomica", "autonomica")
    assert r.minimo_exento == D("500000.00") and r.cuota_integra == D("5764.89")
    assert r.bonificacion_autonomica == 0 and r.cuota_a_ingresar == D("5764.89")
    assert len(r.no_modelado) == 2 and any("propiedades forestales" in a for a in r.avisos)
    # Top bracket added while the ITSGF is in force: from 20.000.000 (cuota 448.713,93) at 3,48 %.
    # 30.500.000 - 500.000 = 30.000.000 -> 448.713,93 + 10.000.000 x 0,0348 = 796.713,93
    r = _calc("cataluna", depositos=30_500_000)
    assert r.cuota_integra == D("796713.93")
    # ITSGF: 30.500.000 - 700.000 = 29.800.000; from 10.695.996,06 (cuota 152.223,93) at 3,5 %:
    # 152.223,93 + 19.104.003,94 x 0,035 = 152.223,93 + 668.640,1379 = 820.864,07; less Patrimonio 796.713,93 = 24.150,14
    assert r.itsgf is not None and r.itsgf.cuota_integra == D("820864.07")
    assert r.itsgf.cuota_a_ingresar == D("24150.14") and r.itsgf.obligado_a_declarar
    assert r.total_a_ingresar == D("820864.07")


def test_own_scale_asturias_and_baleares() -> None:
    # Asturias: state minimum (700.000), own scale (art. 15). First bracket at 0,22 %: 100.000 x 0,0022 = 220
    r = _calc("asturias", depositos=800_000)
    assert (r.origen_minimo_exento, r.origen_escala) == ("estatal", "autonomica")
    assert r.cuota_a_ingresar == D("220.00")
    # 1.200.000 - 700.000 = 500.000; from 334.252,88 (cuota 919,19) at 0,56 %: 919,19 + 165.747,12 x 0,0056 = 919,19 + 928,18387 = 1.847,37
    assert _calc("Principado de Asturias", depositos=1_200_000).cuota_a_ingresar == D("1847.37")
    # Baleares: minimum 3.000.000 (art. 8); 4.000.000 -> 1.000.000; from 681.869,75 (cuota 3.528,67) at 1,24 %:
    # 3.528,67 + 318.130,25 x 0,0124 = 3.528,67 + 3.944,8151 = 7.473,49
    r = _calc("Illes Balears", depositos=4_000_000)
    assert r.minimo_exento == D("3000000.00") and r.cuota_a_ingresar == D("7473.49")
    # ITSGF: 4.000.000 - 700.000 = 3.300.000 -> 300.000 x 0,017 = 5.100, less the 7.473,49 paid -> 0
    assert r.itsgf is not None and r.itsgf.cuota_integra == D("5100.00") and r.itsgf.cuota_a_ingresar == 0


def test_comunidad_valenciana_minimum_changed_between_2025_and_2026() -> None:
    # Minimum 2.000.000 (art. 8, Ley 5/2026); 3.000.000 -> 1.000.000; from 668.499,75 (cuota 3.108,51) at 1,12 %:
    # 3.108,51 + 331.500,25 x 0,0112 = 3.108,51 + 3.712,8028 = 6.821,31
    r = _calc("Comunitat Valenciana", depositos=3_000_000)
    assert r.minimo_exento == D("2000000.00") and r.cuota_a_ingresar == D("6821.31")
    assert not r.aplicable_2025 and "1.000.000 euros" in r.diferencias_2025[0]


def test_disability_minimums() -> None:
    # Andalucía, 65 %: minimum 1.500.000 (art. 24); state scale (its own is suspended while the ITSGF lasts).
    # 2.000.000 - 1.500.000 = 500.000; from 334.252,88 (cuota 835,63) at 0,5 %: 835,63 + 165.747,12 x 0,005 = 1.664,37
    r = _calc("andalucia", depositos=2_000_000, grado_discapacidad=65)
    assert r.minimo_exento == D("1500000.00") and r.origen_escala == "estatal"
    assert r.cuota_integra == D("1664.37")
    assert _calc("andalucia", depositos=2_000_000, grado_discapacidad=33).minimo_exento == D("1250000.00")
    # Without disability Andalucía has no minimum of its own: the State's.
    r = _calc("andalucia", depositos=2_000_000)
    assert r.minimo_exento == D("700000.00") and r.origen_minimo_exento == "estatal"
    # Extremadura (art. 14): 500.000 general; 600.000 / 700.000 / 800.000 from 33 % / 50 % / 65 %.
    minimos = [
        _calc("extremadura", depositos=900_000, grado_discapacidad=g).minimo_exento for g in (0, 32, 33, 50, 64, 65)
    ]
    assert minimos == [D(x) for x in ("500000.00", "500000.00", "600000.00", "700000.00", "700000.00", "800000.00")]
    # A community without disability amounts says so.
    r = _calc("madrid", depositos=900_000, grado_discapacidad=65)
    assert r.minimo_exento == D("700000.00") and any("no establece un mínimo exento distinto" in a for a in r.avisos)


# --- General allowances and the ITSGF ---------------------------------------------


def test_full_allowance_not_tied_to_the_itsgf_extremadura() -> None:
    # Minimum 500.000; 6.000.000 -> 5.500.000; own scale from 5.347.998,03 (cuota 107.043,51) at 3,15 %:
    # 107.043,51 + 152.001,97 x 0,0315 = 107.043,51 + 4.788,06206 = 111.831,57; 100 % allowance (art. 15 bis).
    r = _calc("extremadura", depositos=6_000_000)
    assert r.cuota_integra == r.bonificacion_autonomica == D("111831.57")
    assert r.cuota_a_ingresar == 0
    assert r.obligado_a_declarar and "supera 2.000.000 €" in r.motivo_obligacion
    # ITSGF in full: (6.000.000 - 700.000 - 3.000.000) x 1,7 % = 39.100, nothing to deduct.
    assert r.itsgf is not None
    assert (r.itsgf.cuota_integra, r.itsgf.deduccion_patrimonio, r.itsgf.cuota_a_ingresar) == (
        D("39100.00"),
        0,
        D("39100.00"),
    )
    assert r.itsgf.obligado_a_declarar and r.total_a_ingresar == D("39100.00")


def test_allowance_by_difference_with_the_itsgf_madrid() -> None:
    # 2.000.000: state scale on 1.300.000, from 668.499,75 (cuota 2.506,86) at 0,9 %:
    # 2.506,86 + 631.500,25 x 0,009 = 2.506,86 + 5.683,50225 = 8.190,36. No ITSGF quota -> the difference is the whole quota.
    r = _calc("Comunidad de Madrid", depositos=2_000_000)
    assert r.cuota_integra == r.bonificacion_autonomica == D("8190.36")
    assert r.cuota_a_ingresar == r.total_a_ingresar == 0
    assert not r.obligado_a_declarar  # 2.000.000 is not above the threshold
    assert _calc("madrid", depositos="2000000.01").obligado_a_declarar
    # 6.000.000: Patrimonio on 5.300.000, from 2.673.999,01 (cuota 25.904,35) at 1,7 %:
    # 25.904,35 + 2.626.000,99 x 0,017 = 25.904,35 + 44.642,01683 = 70.546,37
    # ITSGF: (5.300.000 - 3.000.000) x 1,7 % = 39.100. Allowance = 70.546,37 - 39.100 = 31.446,37 (disp. transitoria 7.ª)
    r = _calc("madrid", depositos=6_000_000)
    assert r.cuota_integra == D("70546.37") and r.bonificacion_autonomica == D("31446.37")
    assert r.cuota_a_ingresar == D("39100.00")  # paid to the community...
    assert r.itsgf is not None and r.itsgf.cuota_integra == D("39100.00")
    assert r.itsgf.deduccion_patrimonio == D("39100.00") and r.itsgf.cuota_a_ingresar == 0  # ...instead of to the State
    assert r.total_a_ingresar == D("39100.00")
    # Same mechanism in La Rioja, Andalucía and Murcia (Murcia with its own scale, equal to the State's).
    for comunidad in ("la rioja", "andalucia", "murcia"):
        otro = _calc(comunidad, depositos=6_000_000)
        assert (otro.cuota_integra, otro.cuota_a_ingresar, otro.total_a_ingresar) == (
            D("70546.37"),
            D("39100.00"),
            D("39100.00"),
        )


def test_cantabria_full_allowance_up_to_the_itsgf_threshold() -> None:
    # 3.000.000: own scale on 2.300.000, from 1.336.999,51 (cuota 10.328,31) at 1,57 %:
    # 10.328,31 + 963.000,49 x 0,0157 = 10.328,31 + 15.119,10769 = 25.447,42; 3.000.000 - 700.000 <= 3.000.000 -> 100 %
    r = _calc("cantabria", depositos=3_000_000)
    assert r.cuota_integra == r.bonificacion_autonomica == D("25447.42") and r.cuota_a_ingresar == 0
    assert _calc("cantabria", depositos=3_700_000).cuota_a_ingresar == 0  # exactly at the threshold
    # 5.000.000: 4.300.000 > 3.000.000 -> allowance by difference (disp. adicional 3.ª).
    # Patrimonio from 2.673.999,01 (cuota 31.319,20) at 2,06 %: 31.319,20 + 1.626.000,99 x 0,0206 = 31.319,20 + 33.495,62039 = 64.814,82
    # ITSGF: 1.300.000 x 1,7 % = 22.100 -> allowance 42.714,82, Patrimonio payable 22.100, ITSGF 0.
    r = _calc("cantabria", depositos=5_000_000)
    assert (r.cuota_integra, r.bonificacion_autonomica, r.cuota_a_ingresar) == (
        D("64814.82"),
        D("42714.82"),
        D("22100.00"),
    )
    assert r.itsgf is not None and r.itsgf.cuota_a_ingresar == 0


def test_galicia_half_allowance_reduced_by_the_itsgf() -> None:
    # 2.000.000: scale of the disp. transitoria 3.ª (equal to the State's here) -> 8.190,36; 50 % -> 4.095,18
    r = _calc("galicia", depositos=2_000_000)
    assert (r.cuota_integra, r.bonificacion_autonomica, r.cuota_a_ingresar) == (
        D("8190.36"),
        D("4095.18"),
        D("4095.18"),
    )
    # 6.000.000: quota 70.546,37; full allowance 35.273,185 would leave 35.273,185 of Patrimonio and
    # 39.100 - 35.273,185 = 3.826,815 of ITSGF; the allowance is cut by that: 31.446,37 -> Patrimonio 39.100, ITSGF 0.
    r = _calc("Galicia", depositos=6_000_000)
    assert (r.cuota_integra, r.bonificacion_autonomica, r.cuota_a_ingresar) == (
        D("70546.37"),
        D("31446.37"),
        D("39100.00"),
    )
    assert r.itsgf is not None and r.itsgf.cuota_a_ingresar == 0
    assert any("no lo precisa" in a and "el total a pagar entre los dos impuestos es el mismo" in a for a in r.avisos)
    assert len(r.no_modelado) == 7
    # 30.000.000: quota 183.670,29 + 18.604.003,94 x 0,035 = 183.670,29 + 651.140,1379 = 834.810,43
    # ITSGF 152.223,93 + 651.140,1379 = 803.364,07. With the full 50 % (417.405,215) the ITSGF payable would be
    # 803.364,07 - 417.405,215 = 385.958,855 -> allowance 31.446,36; Patrimonio 803.364,07, ITSGF 0.
    # (The simultaneous reading would give Patrimonio 610.384,64 + ITSGF 192.979,43: the same 803.364,07.)
    r = _calc("galicia", depositos=30_000_000)
    assert (r.cuota_integra, r.bonificacion_autonomica, r.cuota_a_ingresar) == (
        D("834810.43"),
        D("31446.36"),
        D("803364.07"),
    )
    assert r.itsgf is not None and r.itsgf.cuota_integra == D("803364.07") and r.total_a_ingresar == D("803364.07")
    # With the art. 31 limit biting, the base of the 50 % is ambiguous and the calculator says so.
    r = _calc("galicia", depositos=3_000_000, irpf_base_imponible=120_000, irpf_cuota_integra=60_000)
    assert r.cuota_tras_limite == D("12000.00") and r.cuota_a_ingresar == D("6000.00")
    assert any("anterior o" in a and "lectura prudente" in a for a in r.avisos)


def test_every_community_computes_and_never_pays_less_than_the_itsgf() -> None:
    # 8.000.000 everywhere: Patrimonio + ITSGF is at least the ITSGF quota (the ITSGF is a floor).
    # ITSGF: 7.300.000 -> 39.915,97 + (7.300.000 - 5.347.998,03) x 0,021 = 39.915,97 + 40.992,04137 = 80.908,01
    for key in sorted(COMUNIDADES):
        r = _calc(key, depositos=8_000_000)
        assert r.comunidad == key and r.itsgf is not None
        assert r.itsgf.cuota_integra == D("80908.01")
        assert r.total_a_ingresar >= D("80908.01"), key
        assert r.total_a_ingresar == r.cuota_a_ingresar + r.itsgf.cuota_a_ingresar
        assert sum(1 for a in r.avisos if "Grandes Fortunas" in a) == 1


# --- Errors ---------------------------------------------------------------------


def test_unknown_or_foral_community_and_missing_year() -> None:
    with pytest.raises(ComunidadNoDisponibleError, match="régimen foral"):
        _calc("Navarra", depositos=1)
    with pytest.raises(ComunidadNoDisponibleError, match="no reconocida"):
        _calc("Lusitania", depositos=1)
    with pytest.raises(EjercicioNoDisponibleError, match="2026"):
        calcular_patrimonio(PatrimonioInput(ejercicio=2025, comunidad="madrid"))
    with pytest.raises(ValueError):
        PatrimonioInput(comunidad="madrid", depositos=D(-1))
