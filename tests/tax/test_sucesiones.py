"""Impuesto sobre Sucesiones y Donaciones. Expected figures are worked by hand in
the comments from ``hacienda_gpt/tax/data/isd_2026.json``, whose figures are in
turn re-read here against literal extracts of the consolidated laws."""

from datetime import date
from decimal import Decimal
import os
from pathlib import Path

from pydantic import ValidationError
import pytest

from hacienda_gpt.tax.params import EjercicioNoDisponibleError
from hacienda_gpt.tax.sucesiones import BeneficiarioISD, calcular_isd, calcular_isd_varios, grupo_de_parentesco
from hacienda_gpt.tax.sucesiones_params import (
    ISDParams,
    NormativaAutonomicaISD,
    comprobar_evidencias,
    comprobar_normativa,
    ejercicios_isd_disponibles,
    escrituras,
    load_isd_params,
    normalizar_parentesco,
    texto_plano,
    textos_de_directorio,
)

D = Decimal
EXTRACTOS = Path(__file__).resolve().parents[1] / "fixtures" / "boe_sucesiones"


def _isd(**kwargs):
    return calcular_isd(BeneficiarioISD(**kwargs))


def _linea(result, prefix: str) -> D:
    return next(line.importe for line in result.lineas if line.concepto.startswith(prefix))


def _avisa(result, fragment: str) -> bool:
    return any(fragment in aviso for aviso in result.avisos)


# --- Parameters and their evidence -------------------------------------------


def test_params_load_with_the_figures_read_from_the_law() -> None:
    assert ejercicios_isd_disponibles() == [2026]
    p = load_isd_params(2026)
    parentesco = p.estatal.reducciones[0]
    assert [t.importe for t in parentesco.tramos] == [D("15956.87"), D("15956.87"), D("7993.46")]
    assert (parentesco.tramos[0].incremento_por_ano, parentesco.tramos[0].maximo) == (D("3990.72"), D("47858.59"))
    escala = p.estatal.tarifas[0].escala
    assert len(escala.tramos) == 16
    assert (escala.tramos[0].tipo, escala.tramos[-1].tipo) == (D("0.0765"), D("0.34"))
    assert p.estatal.coeficientes[0].tramos[1].coeficientes == {
        "I": D("1.05"),
        "II": D("1.05"),
        "III": D("1.6676"),
        "IV": D("2.1"),
    }
    assert {"madrid", "andalucia", "cataluna"} <= set(p.comunidades) and len(p.comunidades) == 15
    assert p.comunidad("Comunidad de Madrid") is not None and p.comunidad("Catalunya") is not None
    assert p.comunidad("Navarra") is None
    with pytest.raises(EjercicioNoDisponibleError):
        load_isd_params(1999)


def test_every_figure_is_in_its_quote_and_every_quote_is_in_the_law() -> None:
    p = load_isd_params(2026)
    textos = textos_de_directorio(EXTRACTOS)
    assert set(textos) == set(p.fuentes_principales)
    assert comprobar_evidencias(p, textos) == []


def test_quotes_against_the_full_downloaded_pages_when_available() -> None:
    # Point HACIENDA_BOE_PAGINAS at a directory with the pages (<BOE id>.html) straight from boe.es.
    directorio = os.environ.get("HACIENDA_BOE_PAGINAS")
    if not directorio or not (Path(directorio) / "BOE-A-1987-28141.html").exists():
        pytest.skip("HACIENDA_BOE_PAGINAS no tiene las páginas del ISD")
    assert comprobar_evidencias(load_isd_params(2026), textos_de_directorio(Path(directorio))) == []


def test_the_self_check_catches_a_wrong_figure_a_wrong_quote_and_a_scale_that_does_not_add_up() -> None:
    p = load_isd_params(2026)
    textos = textos_de_directorio(EXTRACTOS)
    raw = p.model_dump(mode="json")
    raw["comunidades"]["madrid"]["bonificaciones"][0]["porcentaje"] = "0.95"
    raw["comunidades"]["andalucia"]["reducciones"][0]["evidencia"][0]["cita"] += " y algo que la ley no dice"
    raw["comunidades"]["cataluna"]["tarifas"][1]["escala"]["tramos"][1]["hasta"] = "200000"
    problemas = comprobar_evidencias(ISDParams.model_validate(raw), textos)
    assert any("madrid.bonificaciones.0" in x and "porcentaje = 0.95" in x for x in problemas)
    assert any("andalucia.reducciones.0" in x and "no aparece literal" in x for x in problemas)
    assert any("cataluna.tarifas.1" in x and "no cuadra" in x for x in problemas)


def test_how_figures_may_be_spelled_in_a_quote() -> None:
    assert "16.000" in escrituras(D("16000"), "importe")
    assert "15.956,87" in escrituras(D("15956.87"), "importe")
    assert "1.000.000" in escrituras(D("1000000"), "importe")
    assert {"99", "99,00"} <= set(escrituras(D("0.99"), "porcentaje"))
    assert "tres" in escrituras(D("0.03"), "porcentaje")
    assert "7,65" in escrituras(D("0.0765"), "porcentaje")
    assert {"2,1", "2,1000"} <= set(escrituras(D("2.1"), "coeficiente"))
    # Whole coefficients are printed bare too ("1", "2"), and thousands sometimes with a space.
    assert "2" in escrituras(D("2"), "coeficiente") and "150 000" in escrituras(D("150000"), "importe")
    assert {"21", "veintiuno", "veintiún"} <= set(escrituras(21, "entero"))
    assert "sesenta y cinco" in escrituras(65, "entero")
    # Inline markup does not split words; table cells are separated.
    assert texto_plano("<p>art. 20.2.<em>a</em>)</p><table><tr><td>0,00</td><td>7,65</td></tr></table>") == (
        "art. 20.2.a) 0,00 7,65"
    )


def test_a_community_file_as_phase_two_will_write_it_validates_and_is_checked() -> None:
    madrid = load_isd_params(2026).comunidades["madrid"]
    again = NormativaAutonomicaISD.model_validate(madrid.model_dump(mode="json"))
    assert comprobar_normativa("madrid", again, textos_de_directorio(EXTRACTOS)) == []


# --- Kinship groups ------------------------------------------------------------


def test_kinship_groups_of_article_20() -> None:
    assert grupo_de_parentesco("hijo", 20) == "I"
    assert grupo_de_parentesco("hijo", 21) == "II"
    assert grupo_de_parentesco("otro_descendiente", None) == "II"
    assert grupo_de_parentesco("conyuge", 40) == grupo_de_parentesco("padre", 70) == "II"
    assert grupo_de_parentesco("hermano", 40) == grupo_de_parentesco("sobrino", 10) == "III"
    assert grupo_de_parentesco("yerno_nuera", 40) == grupo_de_parentesco("padrastro", 40) == "III"
    assert grupo_de_parentesco("colateral_cuarto_grado", 40) == grupo_de_parentesco("extrano", 40) == "IV"
    assert grupo_de_parentesco("pareja_de_hecho", 40) == "IV"
    assert normalizar_parentesco("Hija") == "hijo" and normalizar_parentesco("cuñado") == "colateral_afinidad"
    assert normalizar_parentesco("Abuela") == "otro_ascendiente" and normalizar_parentesco("primo") == (
        "colateral_cuarto_grado"
    )
    with pytest.raises(ValueError, match="parentesco desconocido"):
        normalizar_parentesco("vecino")


# --- State rules only -----------------------------------------------------------


def test_state_child_over_21() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=200000)
    # Base 200.000 − 15.956,87 (grupo II) = 184.043,13.
    # Tarifa: hasta 159.634,83 → 23.063,25; resto 24.408,30 × 21,25 % = 5.186,76 → 28.250,01. Coeficiente 1.
    assert (r.grupo, r.comunidad) == ("II", None)
    assert r.base_liquidable == D("184043.13")
    assert r.cuota_integra == r.cuota_tributaria == r.cuota_a_ingresar == D("28250.01")
    assert r.bonificacion == 0
    assert _avisa(r, "No has indicado comunidad autónoma")
    assert _avisa(r, "art. 32 Ley 22/2009") and _avisa(r, "No se han acumulado donaciones anteriores")
    assert "BOE-A-1987-28141" in r.fuente


def test_state_group_one_adds_per_year_under_21_up_to_the_cap() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=15, valor_recibido=100000)
    # 15.956,87 + 3.990,72 × (21 − 15) = 39.901,19 → base liquidable 60.098,81.
    # Hasta 55.918,17 → 5.703,50; resto 4.180,64 × 13,60 % = 568,57 → 6.272,07.
    assert r.grupo == "I"
    assert _linea(r, "Reducción por parentesco") == D("-39901.19")
    assert r.cuota_a_ingresar == D("6272.07")
    # At 10: 15.956,87 + 3.990,72 × 11 = 59.854,79, capped at 47.858,59.
    assert _linea(_isd(modo="sucesion", parentesco="hijo", edad=10, valor_recibido=100000), "Reducción por p") == D(
        "-47858.59"
    )
    sin_edad = _isd(modo="sucesion", parentesco="hijo", valor_recibido=100000)
    assert sin_edad.grupo == "II" and _avisa(sin_edad, "No has indicado la edad del descendiente")


def test_state_sibling_with_previous_wealth_and_the_excess_clause() -> None:
    r = _isd(modo="sucesion", parentesco="hermano", edad=50, valor_recibido=100000, patrimonio_preexistente=500000)
    # 100.000 − 7.993,46 = 92.006,54. Hasta 79.880,52 → 9.166,06; resto 12.126,02 × 16,15 % = 1.958,35 → 11.124,41.
    # Patrimonio 500.000 → segundo tramo, grupo III: × 1,6676 = 18.551,07.
    assert (r.grupo, r.coeficiente) == ("III", D("1.6676"))
    assert r.cuota_integra == D("11124.41")
    assert r.cuota_tributaria == D("18551.07")
    # Patrimonio 402.700: solo 21,89 € por encima del primer tramo. Con 1,5882 la cuota sería 17.667,79;
    # la subida (883,28) supera esos 21,89 €, así que la cuota queda en 17.667,79 + 21,89 = 17.689,68.
    justo = _isd(modo="sucesion", parentesco="hermano", edad=50, valor_recibido=100000, patrimonio_preexistente=402700)
    assert justo.cuota_tributaria == D("17689.68")
    assert _linea(justo, "Reducción del exceso") == D("-861.39")


def test_state_spouse_with_disability_insurance_and_main_home() -> None:
    r = _isd(
        modo="sucesion",
        parentesco="cónyuge",
        edad=70,
        grado_discapacidad=40,
        valor_recibido=300000,
        valor_vivienda_habitual=200000,
        seguro_vida=20000,
    )
    # Base imponible 320.000. Reducciones: parentesco 15.956,87 + discapacidad 47.858,59 + seguro 9.195,49 (tope)
    # + vivienda min(95 % × 200.000, 122.606,47) = 195.617,42 → base liquidable 124.382,58.
    # Hasta 119.757,67 → 15.606,22; resto 4.624,91 × 18,70 % = 864,86 → 16.471,08.
    assert r.base_imponible == D("320000.00")
    assert _linea(r, "Reducción por discapacidad") == D("-47858.59")
    assert _linea(r, "Reducción por seguro de vida") == D("-9195.49")
    assert _linea(r, "Reducción por vivienda habitual") == D("-122606.47")
    assert r.reducciones == D("195617.42")
    assert r.cuota_a_ingresar == D("16471.08")
    assert _avisa(r, "mantener la vivienda durante 10 años")
    grave = _isd(modo="sucesion", parentesco="hijo", edad=30, grado_discapacidad=65, valor_recibido=300000)
    assert _linea(grave, "Reducción por discapacidad") == D("-150253.03")


def test_state_main_home_and_insurance_are_for_close_relatives_only() -> None:
    sobrino = _isd(
        modo="sucesion", parentesco="sobrino", edad=40, valor_recibido=100000, valor_vivienda_habitual=100000
    )
    assert not any("vivienda" in line.concepto for line in sobrino.lineas)
    assert _avisa(sobrino, "ha de ser mayor de 65 años")
    hermana = _isd(
        modo="sucesion", parentesco="hermana", edad=70, valor_recibido=100000, valor_vivienda_habitual=100000
    )
    assert _linea(hermana, "Reducción por vivienda habitual") == D("-95000.00")
    assert _avisa(hermana, "haber convivido con el causante")
    amigo = _isd(modo="sucesion", parentesco="amigo", edad=40, valor_recibido=0, seguro_vida=50000)
    assert amigo.base_liquidable == D("50000.00") and _avisa(amigo, "Reducción por seguro de vida")
    # Grupo IV, sin reducción: hasta 47.930,72 → 4.685,10; resto 2.069,28 × 12,75 % = 263,83 → 4.948,93; × 2 = 9.897,87.
    assert amigo.cuota_tributaria == D("9897.87")


def test_state_household_goods_are_an_option_with_a_warning() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=200000, incluir_ajuar=True)
    # Ajuar 3 % × 200.000 = 6.000 → base 206.000 − 15.956,87 = 190.043,13.
    # 23.063,25 + 30.408,30 × 21,25 % = 6.461,76 → 29.525,01.
    assert _linea(r, "Ajuar doméstico") == D("6000.00")
    assert r.cuota_a_ingresar == D("29525.01")
    assert _avisa(r, "presunción legal del 3 %") and _avisa(r, "admite prueba en contrario")


def test_state_gift_has_no_kinship_reduction_and_business_is_a_declared_amount() -> None:
    r = _isd(modo="donacion", parentesco="hijo", edad=30, valor_recibido=100000)
    # Sin reducciones: hasta 79.880,52 → 9.166,06; resto 20.119,48 × 16,15 % = 3.249,30 → 12.415,36.
    assert r.reducciones == 0 and r.cuota_a_ingresar == D("12415.36")
    empresa = _isd(modo="donacion", parentesco="hijo", edad=30, valor_recibido=100000, valor_empresa_familiar=100000)
    # 95 % × 100.000 = 95.000 → base 5.000 × 7,65 % = 382,50.
    assert empresa.cuota_a_ingresar == D("382.50")
    assert _avisa(empresa, "sin comprobar ninguno de sus requisitos")
    assert _avisa(empresa, "donante tenga 65 años o más")


def test_unknown_community_falls_back_to_state_rules_and_says_so() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=200000, comunidad="Navarra")
    assert r.comunidad is None and r.cuota_a_ingresar == D("28250.01")
    assert _avisa(r, "No hay normativa de «Navarra» cargada") and _avisa(r, "SOLO la ley estatal")


def test_input_is_validated() -> None:
    with pytest.raises(ValidationError, match="indica el parentesco"):
        BeneficiarioISD(modo="sucesion", valor_recibido=1000)
    with pytest.raises(ValidationError, match="no pueden superar el valor recibido"):
        BeneficiarioISD(modo="sucesion", parentesco="hijo", valor_recibido=1000, valor_vivienda_habitual=2000)
    with pytest.raises(ValidationError, match="solo existen en las sucesiones"):
        BeneficiarioISD(modo="donacion", parentesco="hijo", valor_recibido=1000, seguro_vida=10)
    with pytest.raises(ValidationError, match="parentesco desconocido"):
        BeneficiarioISD(modo="donacion", parentesco="vecino", valor_recibido=1000)
    solo_grupo = _isd(modo="sucesion", grupo="III", valor_recibido=50000)
    assert solo_grupo.grupo == "III" and _avisa(solo_grupo, "Solo has indicado el grupo")
    assert _linea(solo_grupo, "Reducción por parentesco") == D("-7993.46")


# --- Madrid ----------------------------------------------------------------------


def test_madrid_child_inheriting() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=200000, comunidad="Madrid")
    # 200.000 − 16.000 = 184.000. Tarifa de Madrid: hasta 159.888,45 → 23.097,51; resto 24.111,55 × 21,25 % =
    # 5.123,70 → 28.221,21. Bonificación 99 % = 27.939,00 → 282,21.
    assert r.comunidad == "madrid"
    assert _linea(r, "Reducción por parentesco (Madrid)") == D("-16000.00")
    assert r.cuota_tributaria == D("28221.21")
    assert r.bonificacion == D("27939.00")
    assert r.cuota_a_ingresar == D("282.21")
    assert "BOCM-m-2010-90068" in r.fuente
    menor = _isd(modo="sucesion", parentesco="hijo", edad=18, valor_recibido=200000, comunidad="Madrid")
    # 16.000 + 4.000 × 3 = 28.000.
    assert _linea(menor, "Reducción por parentesco (Madrid)") == D("-28000.00")


def test_madrid_wording_in_force_after_the_date_of_death_is_flagged() -> None:
    r = _isd(
        modo="sucesion",
        parentesco="hijo",
        edad=30,
        valor_recibido=200000,
        comunidad="Madrid",
        fecha_devengo=date(2026, 3, 1),
    )
    assert _avisa(r, "en vigor desde el 2026-07-01, después de la fecha de devengo")
    tarde = _isd(
        modo="sucesion",
        parentesco="hijo",
        edad=30,
        valor_recibido=200000,
        comunidad="Madrid",
        fecha_devengo=date(2026, 9, 1),
    )
    assert not _avisa(tarde, "después de la fecha de devengo")


def test_madrid_sibling_gets_half_and_registered_partner_counts_as_spouse() -> None:
    r = _isd(modo="sucesion", parentesco="hermano", edad=50, valor_recibido=100000, comunidad="Madrid")
    # 100.000 − 8.000 = 92.000. Hasta 80.006,73 → 9.178,12; resto 11.993,27 × 16,15 % = 1.936,91 → 11.115,03.
    # × 1,5882 = 16.672,55 + 980,35 = 17.652,90. Bonificación 50 % = 8.826,45 → 8.826,45.
    assert r.cuota_tributaria == D("17652.90")
    assert r.cuota_a_ingresar == D("8826.45")
    estado = _isd(modo="sucesion", parentesco="pareja de hecho", edad=50, valor_recibido=200000)
    assert estado.grupo == "IV" and _avisa(estado, "no equipara la pareja de hecho")
    madrid = _isd(modo="sucesion", parentesco="pareja de hecho", edad=50, valor_recibido=200000, comunidad="Madrid")
    assert (madrid.grupo, madrid.parentesco, madrid.cuota_a_ingresar) == ("II", "conyuge", D("282.21"))
    assert _avisa(madrid, "Registro de Uniones de Hecho")


def test_madrid_gift_to_a_child() -> None:
    con = _isd(
        modo="donacion", parentesco="hija", edad=30, valor_recibido=100000, comunidad="Madrid", escritura_publica=True
    )
    # Sin reducción: hasta 80.006,73 → 9.178,12; resto 19.993,27 × 16,15 % = 3.228,91 → 12.407,03.
    # Bonificación 99 % = 12.282,96 → 124,07.
    assert con.cuota_tributaria == D("12407.03")
    assert con.cuota_a_ingresar == D("124.07")
    assert _avisa(con, "origen de los fondos")
    sin = _isd(modo="donacion", parentesco="hija", edad=30, valor_recibido=100000, comunidad="Madrid")
    assert sin.bonificacion == 0 and sin.cuota_a_ingresar == D("12407.03")
    assert _avisa(sin, "exige que conste en escritura pública")
    # Up to 10.000 € no deed is needed: 8.000 × 7,65 % = 612,00; 99 % = 605,88 → 6,12.
    pequena = _isd(modo="donacion", parentesco="hija", edad=30, valor_recibido=8000, comunidad="Madrid")
    assert pequena.cuota_a_ingresar == D("6.12")
    # Up to 1.000 €, 100 % whoever the donee is: 900 × 7,65 % × 2 (grupo IV) = 137,70 → 0.
    amigo = _isd(modo="donacion", parentesco="amigo", edad=30, valor_recibido=900, comunidad="Madrid")
    assert (amigo.cuota_tributaria, amigo.cuota_a_ingresar) == (D("137.70"), D("0.00"))


# --- Andalucía -------------------------------------------------------------------


def test_andalucia_child_inheriting() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=1200000, comunidad="Andalucía")
    # 1.200.000 − 1.000.000 = 200.000. Tarifa andaluza: hasta 150.000 → 21.620; resto 50.000 × 20 % = 10.000
    # → 31.620. Bonificación 99 % = 31.303,80 → 316,20.
    assert _linea(r, "Reducción por parentesco (Andalucía)") == D("-1000000.00")
    assert r.cuota_tributaria == D("31620.00")
    assert r.cuota_a_ingresar == D("316.20")
    exento = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=800000, comunidad="Andalucía")
    assert exento.base_liquidable == 0 and exento.cuota_a_ingresar == 0
    assert _avisa(exento, "Las reducciones superan la base imponible")


def test_andalucia_main_home_keeps_the_state_ceiling_and_says_so() -> None:
    r = _isd(
        modo="sucesion",
        parentesco="hijo",
        edad=30,
        valor_recibido=1200000,
        valor_vivienda_habitual=300000,
        comunidad="andalucia",
    )
    # 99 % × 300.000 = 297.000, limitado a 122.606,47 → base 77.393,53.
    # Hasta 70.000 → 7.820; resto 7.393,53 × 16 % = 1.182,96 → 9.002,96; 99 % = 8.912,93 → 90,03.
    assert _linea(r, "Reducción por vivienda habitual") == D("-122606.47")
    assert r.cuota_a_ingresar == D("90.03")
    assert _avisa(r, "no menciona límite; se ha mantenido el límite estatal")
    assert _avisa(r, "durante 3 años")


def test_andalucia_coefficients_ignore_previous_wealth_and_disability_is_improved() -> None:
    r = _isd(
        modo="sucesion",
        parentesco="hermano",
        edad=50,
        grado_discapacidad=33,
        valor_recibido=400000,
        patrimonio_preexistente=5000000,
        comunidad="andalucia",
    )
    # 400.000 − 10.000 − 250.000 = 140.000. Hasta 100.000 → 12.620; resto 40.000 × 18 % = 7.200 → 19.820.
    # × 1,5 = 29.730; el grupo III no tiene bonificación.
    assert _linea(r, "Reducción por discapacidad (Andalucía)") == D("-250000.00")
    assert (r.coeficiente, r.cuota_a_ingresar) == (D("1.5"), D("29730.00"))


def test_andalucia_gift_to_a_child_and_business_at_99() -> None:
    con = _isd(
        modo="donacion",
        parentesco="hijo",
        edad=30,
        valor_recibido=100000,
        comunidad="andalucia",
        escritura_publica=True,
    )
    # 100.000 → 12.620 (acumulado impreso). Bonificación 99 % = 12.493,80 → 126,20.
    assert con.cuota_tributaria == D("12620.00") and con.cuota_a_ingresar == D("126.20")
    sin = _isd(modo="donacion", parentesco="hijo", edad=30, valor_recibido=100000, comunidad="andalucia")
    assert sin.cuota_a_ingresar == D("12620.00") and _avisa(sin, "exige que conste en escritura pública")
    # Up to 5.000 € no deed is needed: 5.000 × 7 % = 350 → 3,50.
    assert _isd(
        modo="donacion", parentesco="hijo", edad=30, valor_recibido=5000, comunidad="andalucia"
    ).cuota_a_ingresar == D("3.50")
    empresa = _isd(
        modo="donacion",
        parentesco="hijo",
        edad=30,
        valor_recibido=500000,
        valor_empresa_familiar=500000,
        comunidad="andalucia",
        escritura_publica=True,
    )
    # 99 % × 500.000 = 495.000 → base 5.000 × 7 % = 350 → 3,50.
    assert _linea(empresa, "Reducción por empresa") == D("-495000.00")
    assert empresa.cuota_a_ingresar == D("3.50")
    assert _avisa(empresa, "donaciones, que no se calculan")


# --- Cataluña --------------------------------------------------------------------


def test_cataluna_child_inheriting() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=400000, comunidad="Cataluña")
    # 400.000 − 100.000 = 300.000. Tarifa catalana: hasta 150.000 → 14.500; resto 150.000 × 17 % = 25.500 → 40.000.
    # Bonificación del grupo II sobre la base imponible (400.000): (100.000 × 60 + 100.000 × 55 + 100.000 × 50 +
    # 100.000 × 45) / 400.000 = 52,50 % → 21.000 → 19.000.
    assert r.comunidad == "cataluna"
    assert r.cuota_tributaria == D("40000.00")
    assert r.bonificacion == D("21000.00")
    assert r.cuota_a_ingresar == D("19000.00")
    assert _avisa(r, "Ley 19/2010") and _avisa(r, "Se pierde si se opta")
    conyuge = _isd(modo="sucesion", parentesco="cónyuge", edad=60, valor_recibido=400000, comunidad="Cataluña")
    assert conyuge.cuota_a_ingresar == D("400.00")  # 99 % de 40.000


def test_cataluna_weighted_allowance_is_rounded_as_the_law_says() -> None:
    r = _isd(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=320000, comunidad="cataluna")
    # Base liquidable 220.000 → 14.500 + 70.000 × 17 % = 26.400.
    # Media ponderada: (6.000.000 + 5.500.000 + 5.000.000 + 20.000 × 45) / 320.000 = 54,375 %: la milésima es 5,
    # se mantiene la centésima → 54,37 % (no 54,38). 26.400 × 54,37 % = 14.353,68 → 12.046,32.
    assert r.bonificacion == D("14353.68")
    assert r.cuota_a_ingresar == D("12046.32")
    menor = _isd(modo="sucesion", parentesco="hija", edad=18, valor_recibido=600000, comunidad="cataluna")
    # Grupo I: 100.000 + 12.000 × 3 = 136.000 → base 464.000 → 57.000 + 64.000 × 24 % = 72.360.
    # Media: (99 + 97 + 95 + 2 × 90 + 80) / 6 = 91,8333 % → 91,83 %. 72.360 × 91,83 % = 66.448,19 → 5.911,81.
    assert _linea(menor, "Reducción por parentesco (Cataluña)") == D("-136000.00")
    assert menor.cuota_a_ingresar == D("5911.81")


def test_cataluna_reductions_by_relationship_age_and_main_home() -> None:
    padre = _isd(modo="sucesion", parentesco="padre", edad=80, valor_recibido=500000, comunidad="cataluna")
    # Ascendiente 30.000 + mayores de 75 años 275.000 → base 195.000 → 14.500 + 45.000 × 17 % = 22.150.
    # Bonificación sobre 500.000: (60 + 55 + 50 + 2 × 45) / 5 = 51 % → 11.296,50 → 10.853,50.
    assert _linea(padre, "Reducción por parentesco (Cataluña)") == D("-30000.00")
    assert _linea(padre, "Reducción para personas mayores") == D("-275000.00")
    assert padre.cuota_a_ingresar == D("10853.50")
    con_discapacidad = _isd(
        modo="sucesion", parentesco="padre", edad=80, grado_discapacidad=40, valor_recibido=500000, comunidad="cataluna"
    )
    assert con_discapacidad.reducciones == D("305000.00")  # 30.000 + 275.000 por discapacidad, no las dos
    assert _avisa(con_discapacidad, "es incompatible con «Reducción por discapacidad (Cataluña)»")
    nieto = _isd(modo="sucesion", parentesco="nieto", edad=30, valor_recibido=500000, comunidad="cataluna")
    assert _linea(nieto, "Reducción por parentesco (Cataluña)") == D("-50000.00")
    hijastra = _isd(modo="sucesion", parentesco="hijastra", edad=30, valor_recibido=400000, comunidad="cataluna")
    assert (hijastra.parentesco, hijastra.cuota_a_ingresar) == ("hijo", D("19000.00"))
    casa = dict(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=700000, valor_vivienda_habitual=400000)
    # 95 % × 400.000 = 380.000; con el 50 % de la vivienda el límite es 500.000 × 0,5 = 250.000.
    mitad = _isd(**casa, participacion_vivienda="0.5", comunidad="cataluna")
    assert _linea(mitad, "Reducción por vivienda habitual") == D("-250000.00")
    # Sin la participación se toma el mínimo individual de 180.000 y se avisa.
    sin_dato = _isd(**casa, comunidad="cataluna")
    assert _linea(sin_dato, "Reducción por vivienda habitual") == D("-180000.00")
    assert _avisa(sin_dato, "no has indicado la tuya")
    solo_grupo = _isd(modo="sucesion", grupo="II", valor_recibido=400000, comunidad="cataluna")
    # Without the exact relationship the Catalan amounts cannot be chosen: state reduction, no allowance.
    assert _linea(solo_grupo, "Reducción por parentesco") == D("-15956.87") and solo_grupo.bonificacion == 0
    assert _avisa(solo_grupo, "falta indicar el parentesco exacto")


def test_cataluna_business_reduction_forfeits_the_allowance() -> None:
    r = _isd(
        modo="sucesion",
        parentesco="hijo",
        edad=30,
        valor_recibido=400000,
        valor_empresa_familiar=100000,
        comunidad="cataluna",
    )
    # 400.000 − 100.000 − 95.000 = 205.000 → 14.500 + 55.000 × 17 % = 23.850, sin bonificación.
    assert r.bonificacion == 0 and r.cuota_a_ingresar == D("23850.00")
    assert _avisa(r, "es incompatible con optar por las reducciones que has declarado")


def test_cataluna_gift_to_a_child_uses_the_reduced_scale_only_with_a_public_deed() -> None:
    con = _isd(
        modo="donacion", parentesco="hijo", edad=30, valor_recibido=100000, comunidad="cataluna", escritura_publica=True
    )
    assert con.cuota_a_ingresar == D("5000.00")  # 100.000 × 5 %
    sin = _isd(modo="donacion", parentesco="hijo", edad=30, valor_recibido=100000, comunidad="cataluna")
    assert sin.cuota_a_ingresar == D("9000.00")  # tarifa general: 3.500 + 50.000 × 11 %
    assert _avisa(sin, "exige que conste en escritura pública")
    grande = _isd(
        modo="donacion", parentesco="hijo", edad=30, valor_recibido=700000, comunidad="cataluna", escritura_publica=True
    )
    assert grande.cuota_a_ingresar == D("47000.00")  # 38.000 + 100.000 × 9 %
    rico = _isd(
        modo="donacion",
        parentesco="hermano",
        edad=30,
        valor_recibido=100000,
        patrimonio_preexistente=3000000,
        comunidad="cataluna",
        escritura_publica=True,
    )
    assert rico.cuota_a_ingresar == D("14293.80")  # tarifa general 9.000 × 1,5882 (grupo III, sin tarifa reducida)


def test_one_result_per_beneficiary() -> None:
    resultados = calcular_isd_varios(
        [
            BeneficiarioISD(modo="sucesion", parentesco="cónyuge", valor_recibido=100000, comunidad="madrid"),
            BeneficiarioISD(modo="sucesion", parentesco="hijo", edad=12, valor_recibido=100000, comunidad="madrid"),
        ]
    )
    assert [r.grupo for r in resultados] == ["II", "I"]
    # Hijo de 12: 16.000 + 4.000 × 9 = 52.000, limitado a 48.000.
    assert _linea(resultados[1], "Reducción por parentesco (Madrid)") == D("-48000.00")


def test_all_fifteen_communities_compute_and_differ() -> None:
    """A child of 30 inheriting 400.000 €, and a 100.000 € gift with a public deed, in every community."""
    herencias, donaciones = {}, {}
    for clave in load_isd_params(2026).comunidades:
        herencia = calcular_isd(
            BeneficiarioISD(modo="sucesion", comunidad=clave, parentesco="hijo", edad=30, valor_recibido=D(400000))
        )
        donacion = calcular_isd(
            BeneficiarioISD(
                modo="donacion",
                comunidad=clave,
                parentesco="hijo",
                edad=30,
                valor_recibido=D(100000),
                escritura_publica=True,
            )
        )
        herencias[clave], donaciones[clave] = herencia.cuota_a_ingresar, donacion.cuota_a_ingresar
    # Figures hand-checked from each law's article by the encoder of that community.
    assert herencias["cantabria"] == herencias["galicia"] == herencias["extremadura"] == 0
    assert herencias["canarias"] == D("75.07") and herencias["castilla_la_mancha"] == D("15379.56")
    assert herencias["asturias"] == D("23120.00") and herencias["comunidad_valenciana"] == D("558.78")
    assert donaciones["asturias"] == D("2000.00")  # the reduced gift scale: 2 % up to 150.000 €
    assert donaciones["galicia"] == D("5000.00") and donaciones["castilla_la_mancha"] == D("620.77")
    assert donaciones["baleares"] == donaciones["cantabria"] == donaciones["aragon"] == 0
    # Without a community only state law applies, and it is the most expensive of all.
    estatal = calcular_isd(BeneficiarioISD(modo="sucesion", parentesco="hijo", edad=30, valor_recibido=D(400000)))
    assert estatal.cuota_a_ingresar > max(herencias.values())
