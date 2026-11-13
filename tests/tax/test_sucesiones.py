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
    assert set(textos) == set(p.fuentes_principales) | set(p.fuentes_complementarias)
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


# --- Rules that go beyond "one fixed amount, one allowance" -----------------------


def _sin(clave: str, **cambios):
    """The parameters with one community's lists replaced: what the calculator did before a rule existed."""
    p = load_isd_params(2026)
    comunidad = p.comunidades[clave].model_copy(update=cambios)
    return p.model_copy(update={"comunidades": {**p.comunidades, clave: comunidad}})


def test_partner_assimilated_only_for_the_communitys_own_rules_castilla_y_leon() -> None:
    caso = BeneficiarioISD(
        modo="sucesion", comunidad="Castilla y León", parentesco="pareja de hecho", edad=50, valor_recibido=600000
    )
    r = calcular_isd(caso)
    # Art. 22.1.a): cónyuge solo para las reducciones y bonificaciones de Castilla y León.
    # Reducciones: 60.000 (art. 13) + variable hasta completar 400.000 = 340.000 → base liquidable 200.000.
    # Tarifa estatal: 23.063,25 + 40.365,17 × 21,25 % = 8.577,60 → 31.640,85. El coeficiente es estatal y ve el
    # parentesco real, grupo IV: × 2 = 63.281,70. Bonificación 99 % (art. 17 bis) = 62.648,88 → 632,82.
    assert _linea(r, "Reducción por parentesco (Castilla y León)") == D("-60000.00")
    assert _linea(r, "Reducción variable") == D("-340000.00")
    assert (r.grupo, r.parentesco, r.coeficiente) == ("IV", "pareja_de_hecho", D("2"))
    assert r.cuota_tributaria == D("63281.70") and r.cuota_a_ingresar == D("632.82")
    assert _avisa(r, "SOLO para") and _avisa(r, "Registro de Uniones de Hecho de Castilla y León")
    # Before the assimilation was encoded: a stranger, no reduction and no allowance.
    # 80.655,08 + 201.222,46 × 29,75 % = 59.863,68 → 140.518,76; × 2 = 281.037,52.
    assert calcular_isd(caso, _sin("castilla_y_leon", equiparaciones=[])).cuota_a_ingresar == D("281037.52")
    # State reductions the community has not touched keep seeing a stranger: no life-insurance reduction.
    con_seguro = calcular_isd(caso.model_copy(update={"seguro_vida": D(20000)}))
    assert not any(line.concepto == "Reducción por seguro de vida" for line in con_seguro.lineas)
    assert _avisa(con_seguro, "Reducción por seguro de vida (art. 20.2.b) Ley 29/1987) no aplicada")


def test_partner_assimilated_in_canarias_for_its_reductions_but_not_for_the_state_coefficient() -> None:
    r = _isd(
        modo="sucesion",
        comunidad="canarias",
        parentesco="pareja_de_hecho",
        edad=50,
        valor_recibido=300000,
        seguro_vida=30000,
        valor_vivienda_habitual=150000,
        participacion_vivienda=1,
    )
    # Art. 41: como cónyuge en lo dictado por Canarias. Base imponible 330.000. Reducciones canarias: parentesco
    # 40.400 + seguro 23.150 (tope) + vivienda 99 % × 150.000 = 148.500 → 212.050; base liquidable 117.950.
    # Tarifa estatal: 9.166,06 + 38.069,48 × 16,15 % = 6.148,22 → 15.314,28; coeficiente estatal del grupo IV
    # (parentesco real): × 2 = 30.628,56. Bonificación 99,9 % = 30.597,93 → 30,63.
    assert _linea(r, "Reducción por parentesco (Canarias)") == D("-40400.00")
    assert _linea(r, "Reducción por seguro de vida") == D("-23150.00")
    assert _linea(r, "Reducción por vivienda habitual") == D("-148500.00")
    assert (r.grupo, r.cuota_tributaria, r.cuota_a_ingresar) == ("IV", D("30628.56"), D("30.63"))


def test_partner_assimilated_for_allowances_only_castilla_la_mancha_and_for_named_reductions_aragon() -> None:
    clm = _isd(
        modo="sucesion", comunidad="castilla_la_mancha", parentesco="pareja_de_hecho", edad=50, valor_recibido=200000
    )
    # Art. 18.1: solo «a los efectos de los artículos 17 y 17 bis». Sin reducciones (grupo IV): base 200.000 →
    # 31.640,85 × 2 = 63.281,70. Tramo de 175.000 a 225.000 de base liquidable: 95 % = 60.117,62 → 3.164,08.
    assert clm.reducciones == 0 and clm.grupo == "IV"
    assert (clm.bonificacion, clm.cuota_a_ingresar) == (D("60117.62"), D("3164.08"))
    aragon = _isd(modo="sucesion", comunidad="aragon", parentesco="pareja_de_hecho", edad=50, valor_recibido=600000)
    # Disp. adicional 1.ª: como cónyuge en las reducciones que lo nombran (art. 131-5: 500.000) → base 100.000.
    # 9.166,06 + 20.119,48 × 16,15 % = 3.249,30 → 12.415,36; × 2 (grupo IV) = 24.830,71.
    assert _linea(aragon, "Reducción aragonesa a favor del cónyuge") == D("-500000.00")
    assert aragon.cuota_a_ingresar == D("24830.71")
    donacion = _isd(modo="donacion", comunidad="aragon", parentesco="pareja_de_hecho", edad=50, valor_recibido=150000)
    # Art. 132-2: 100.000 → base 50.000 → 4.685,10 + 2.069,28 × 12,75 % = 263,83 → 4.948,93; × 2 = 9.897,87.
    # La bonificación del art. 132-6 se define por el «Grupo II» y no se extiende a la pareja.
    assert donacion.cuota_a_ingresar == D("9897.87") and donacion.bonificacion == 0


def test_reduction_of_the_whole_base_galicia_disability() -> None:
    caso = BeneficiarioISD(
        modo="sucesion", comunidad="galicia", parentesco="hijo", edad=40, grado_discapacidad=65, valor_recibido=2000000
    )
    r = calcular_isd(caso)
    # Art. 6.Tres.b): grupos I y II, discapacidad ≥ 65 %, patrimonio preexistente ≤ 3.000.000 €: 100 % de la base.
    # Tras la reducción por parentesco (1.000.000) queda 1.000.000, que es lo que se aplica.
    assert _linea(r, "Reducción por discapacidad (Galicia) (100 % de la base imponible)") == D("-1000000.00")
    assert r.base_liquidable == 0 and r.cuota_a_ingresar == 0
    # Before: only the state disability reduction. 2.000.000 − 1.000.000 − 150.253,03 = 849.746,97 →
    # tarifa gallega 78.500 + 49.746,97 × 15 % = 7.462,05 → 85.962,05.
    viejo = load_isd_params(2026).comunidades["galicia"].reducciones
    solo_33 = viejo[1].model_copy(update={"tramos": viejo[1].tramos[2:]})
    assert calcular_isd(caso, _sin("galicia", reducciones=[viejo[0], solo_33])).cuota_a_ingresar == D("85962.05")
    # Letter c): above 3.000.000 € of previous wealth, 300.000 €. 2.000.000 − 1.300.000 = 700.000 →
    # 23.500 + 400.000 × 11 % = 67.500 (coeficiente 1 en Galicia para los grupos I y II).
    rico = calcular_isd(caso.model_copy(update={"patrimonio_preexistente": D(3500000)}))
    assert _linea(rico, "Reducción por discapacidad (Galicia)") == D("-300000.00")
    assert rico.cuota_a_ingresar == D("67500.00")
    hermano = _isd(
        modo="sucesion",
        comunidad="galicia",
        parentesco="hermano",
        edad=40,
        grado_discapacidad=65,
        valor_recibido=500000,
    )
    assert _linea(hermano, "Reducción por discapacidad (Galicia)") == D("-300000.00")  # grupo III: letra c)


def test_reduction_of_the_whole_base_aragon_minor_children_and_disability() -> None:
    menor = BeneficiarioISD(modo="sucesion", comunidad="aragon", parentesco="hijo", edad=15, valor_recibido=800000)
    r = calcular_isd(menor)
    # Art. 131-1: 100 % de la base para hijos menores de edad (18, art. 12 de la Constitución), máximo 3.000.000.
    # Reducción estatal de grupo I a los 15 años: 15.956,87 + 3.990,72 × 6 = 39.901,19; el resto, 760.098,81.
    assert _linea(r, "Reducción por parentesco") == D("-39901.19")
    assert _linea(r, "Reducción aragonesa para hijos del fallecido menores de edad") == D("-760098.81")
    assert _linea(r, "Reducción aragonesa a favor del cónyuge") == D("0.00")  # las demás ya superan 500.000
    assert r.cuota_a_ingresar == 0 and _avisa(r, "art. 12 de la Constitución")
    # Before (and still at 18): 800.000 − 500.000 = 300.000 → 40.011,04 + 60.610,87 × 25,5 % = 15.455,77 →
    # 55.466,81; bonificación 99 % del grupo I (art. 131-12) = 54.912,14 → 554,67.
    sin_131_1 = [
        x for x in load_isd_params(2026).comunidades["aragon"].reducciones if x.clave != "aragon_hijos_menores"
    ]
    assert calcular_isd(menor, _sin("aragon", reducciones=sin_131_1)).cuota_a_ingresar == D("554.67")
    assert calcular_isd(menor.model_copy(update={"edad": 18})).cuota_a_ingresar == D("554.67")
    assert calcular_isd(menor.model_copy(update={"edad": 17})).cuota_a_ingresar == 0
    # The 3.000.000 € ceiling. Child of 10 inheriting 3.500.000: state 47.858,59 + 3.000.000 → base 452.141,41.
    # 80.655,08 + 53.363,87 × 29,75 % = 15.875,75 → 96.530,83; 99 % = 95.565,52 → 965,31.
    grande = calcular_isd(menor.model_copy(update={"edad": 10, "valor_recibido": D(3500000)}))
    assert _linea(grande, "Reducción aragonesa para hijos del fallecido menores de edad") == D("-3000000.00")
    assert grande.cuota_a_ingresar == D("965.31")
    # Art. 131-2: disability of 65 % or more, 100 % whatever the relationship (before: 55.466,81, see above).
    discapacidad = _isd(
        modo="sucesion", comunidad="aragon", parentesco="sobrino", edad=40, grado_discapacidad=65, valor_recibido=800000
    )
    assert discapacidad.base_liquidable == 0 and discapacidad.cuota_a_ingresar == 0


def test_a_figure_the_regional_text_does_not_print_needs_a_quote_from_the_norm_that_defines_it() -> None:
    p = load_isd_params(2026)
    # Declared apart from the laws of the tax, so that it is not mistaken for one of them.
    assert "BOE-A-1978-31229" in p.fuentes_complementarias and "BOE-A-1978-31229" not in p.fuentes_principales
    assert [f.boe_id for f in p.comunidades["aragon"].fuentes_complementarias] == ["BOE-A-1978-31229"]
    raw = p.model_dump(mode="json")
    menores = next(x for x in raw["comunidades"]["aragon"]["reducciones"] if x["clave"] == "aragon_hijos_menores")
    assert menores["tramos"][0]["cuando"]["edad_menor_de"] == 18
    menores["evidencia"] = [e for e in menores["evidencia"] if e["boe_id"] != "BOE-A-1978-31229"]
    problemas = comprobar_evidencias(ISDParams.model_validate(raw), textos_de_directorio(EXTRACTOS))
    assert any("edad_menor_de = 18 no aparece en ninguna cita" in x for x in problemas)
    del raw["fuentes_complementarias"]["BOE-A-1978-31229"]
    problemas = comprobar_evidencias(ISDParams.model_validate(raw), textos_de_directorio(EXTRACTOS))
    assert any("fuente complementaria BOE-A-1978-31229 no figura" in x for x in problemas)


def test_extremadura_gift_allowance_by_slices_of_the_taxable_base() -> None:
    def donacion(valor: int, params=None):
        caso = BeneficiarioISD(
            modo="donacion",
            comunidad="extremadura",
            parentesco="hijo",
            edad=30,
            valor_recibido=valor,
            escritura_publica=True,
        )
        return calcular_isd(caso, params)

    # Art. 30.1: 99 % hasta 300.000 € de base liquidable; 50 % para la parte entre 300.000 y 600.000; nada después.
    # 300.000: 40.011,04 + 60.610,87 × 25,5 % = 15.455,77 → 55.466,81; 99 % = 54.912,14 → 554,67.
    assert donacion(300000).cuota_a_ingresar == D("554.67")
    # 500.000: 80.655,08 + 101.222,46 × 29,75 % = 30.113,68 → 110.768,76.
    # Media: (300.000 × 99 % + 200.000 × 50 %) / 500.000 = 79,4 % → 87.950,40 → 22.818,36.
    medio = donacion(500000)
    assert (medio.cuota_tributaria, medio.bonificacion, medio.cuota_a_ingresar) == (
        D("110768.76"),
        D("87950.40"),
        D("22818.36"),
    )
    assert _avisa(medio, "admite una lectura más estricta") and _avisa(medio, "450.000 € y 750.000 €")
    # 800.000: 199.291,40 + 2.444,92 × 34 % = 831,27 → 200.122,67.
    # Media: (297.000 + 150.000 + 0) / 800.000 = 55,875 % → 111.818,54 → 88.304,13.
    assert donacion(800000).cuota_a_ingresar == D("88304.13")
    # Before: nothing was computed above 300.000 €.
    reglas = load_isd_params(2026).comunidades["extremadura"].bonificaciones
    plana = reglas[2].model_copy(
        update={
            "porcentaje": D("0.99"),
            "tramos": [],
            "cuando": reglas[2].cuando.model_copy(update={"base_liquidable_hasta": D(300000)}),
        }
    )
    antes = _sin("extremadura", bonificaciones=[*reglas[:2], plana])
    assert donacion(500000, antes).cuota_a_ingresar == D("110768.76")
    assert donacion(300000, antes).cuota_a_ingresar == D("554.67")


def test_a_second_allowance_on_what_the_first_one_leaves_castilla_la_mancha() -> None:
    caso = BeneficiarioISD(
        modo="sucesion",
        comunidad="castilla_la_mancha",
        parentesco="hijo",
        edad=30,
        grado_discapacidad=65,
        valor_recibido=600000,
    )
    r = calcular_isd(caso)
    # 600.000 − 15.956,87 − 225.000 (art. 15) = 359.043,13 → 40.011,04 + 119.654,00 × 25,5 % = 30.511,77 →
    # 70.522,81. Art. 17.1.e): base liquidable ≥ 300.000 → 80 % = 56.418,25; quedan 14.104,56.
    # Art. 17.2: 95 % «con posterioridad» = 13.399,33 → 705,23.
    assert r.cuota_tributaria == D("70522.81")
    assert _linea(r, "Bonificación de Castilla-La Mancha en herencias, grupos I y II") == D("-56418.25")
    assert _linea(r, "Bonificación de Castilla-La Mancha por discapacidad") == D("-13399.33")
    assert (r.bonificacion, r.cuota_a_ingresar) == (D("69817.58"), D("705.23"))
    # Before, only one of the two could be applied (the 95 %): 70.522,81 × 5 % = 3.526,14.
    clm = load_isd_params(2026).comunidades["castilla_la_mancha"]
    una_sola = _sin(
        "castilla_la_mancha",
        bonificaciones=[clm.bonificaciones[0], *clm.bonificaciones_sucesivas, *clm.bonificaciones[1:]],
        bonificaciones_sucesivas=[],
    )
    assert calcular_isd(caso, una_sola).cuota_a_ingresar == D("3526.14")
    # Outside groups I and II the disability allowance stands alone: sibling, 200.000 − 7.993,46 − 225.000 < 0.
    regalo = _isd(
        modo="donacion",
        comunidad="castilla_la_mancha",
        parentesco="hijo",
        edad=30,
        grado_discapacidad=65,
        valor_recibido=300000,
        escritura_publica=True,
    )
    # 55.466,81; art. 17 bis.1.c) ≥ 240.000 → 85 % = 47.146,79; quedan 8.320,02 × 95 % = 7.904,02 → 416,00.
    assert regalo.cuota_a_ingresar == D("416.00")
    sobrino = _isd(
        modo="sucesion",
        comunidad="castilla_la_mancha",
        parentesco="sobrino",
        edad=30,
        grado_discapacidad=65,
        valor_recibido=400000,
    )
    # 400.000 − 7.993,46 − 225.000 = 167.006,54 → 23.063,25 + 7.371,71 × 21,25 % = 1.566,49 → 24.629,74;
    # × 1,5882 = 39.116,95. Solo el 95 % del art. 17.2 = 37.161,10 → 1.955,85.
    assert (sobrino.cuota_tributaria, sobrino.cuota_a_ingresar) == (D("39116.95"), D("1955.85"))


def test_the_more_favourable_of_the_state_and_the_regional_reduction_canarias() -> None:
    caso = BeneficiarioISD(
        modo="sucesion",
        comunidad="canarias",
        parentesco="hijo",
        edad=30,
        valor_recibido=200000,
        valor_vivienda_habitual=100000,
        participacion_vivienda="0.25",
    )
    r = calcular_isd(caso)
    # A quarter of a 400.000 € home. Canarias (art. 22 ter): 99 % × 100.000 = 99.000, pero el límite conjunto de
    # 200.000 se prorratea: 50.000. Estado: 95 % × 100.000 = 95.000 (límite 122.606,47 por heredero). Art. 19.5:
    # se aplica la estatal. 200.000 − 23.125 − 95.000 = 81.875 → 9.166,06 + 1.994,48 × 16,15 % = 322,11 →
    # 9.488,17; 99,9 % = 9.478,68 → 9,49.
    linea = next(line for line in r.lineas if line.concepto.startswith("Reducción por vivienda habitual"))
    assert (linea.importe, linea.referencia) == (D("-95000.00"), "art. 20.2.c) Ley 29/1987")
    assert _avisa(r, "se ha aplicado la estatal (art. 20.2.c) Ley 29/1987): 95.000,00 € frente a 50.000,00 €")
    assert _avisa(r, "durante 10 años")
    assert r.cuota_a_ingresar == D("9.49")
    # Before, the Canarian rule was applied without comparing: base 126.875 → 15.606,22 + 7.117,33 × 18,7 % =
    # 1.330,94 → 16.937,16 → 16,94.
    canarias = load_isd_params(2026).comunidades["canarias"]
    sin_comparar = [v.model_copy(update={"estatal_si_mas_favorable": False}) for v in canarias.vivienda_habitual]
    assert calcular_isd(caso, _sin("canarias", vivienda_habitual=sin_comparar)).cuota_a_ingresar == D("16.94")
    # With the whole home the Canarian one wins (148.500 against 122.606,47) and the trace says so.
    entera = calcular_isd(
        caso.model_copy(update={"valor_vivienda_habitual": D(150000), "participacion_vivienda": D(1)})
    )
    linea = next(line for line in entera.lineas if line.concepto.startswith("Reducción por vivienda habitual"))
    assert (linea.importe, linea.referencia) == (D("-148500.00"), "art. 22 ter DLeg 1/2009 (Canarias)")
    assert _avisa(entera, "se ha aplicado la autonómica (art. 22 ter DLeg 1/2009 (Canarias))")


def test_topping_up_reduction_leaves_life_insurance_out_and_shows_what_is_applied_aragon() -> None:
    caso = BeneficiarioISD(
        modo="sucesion", comunidad="aragon", parentesco="cónyuge", edad=60, valor_recibido=700000, seguro_vida=100000
    )
    r = calcular_isd(caso)
    # Art. 131-5.1.b): el límite de 500.000 cuenta las demás reducciones «excluida la relativa a los beneficiarios
    # de pólizas de seguros de vida». Base imponible 800.000. Parentesco 15.956,87; seguro 9.195,49 (no cuenta);
    # art. 131-5: 500.000 − 15.956,87 = 484.043,13. Total 509.195,49 → base liquidable 290.804,51.
    # 40.011,04 + 51.415,38 × 25,5 % = 13.110,92 → 53.121,96.
    assert _linea(r, "Reducción aragonesa a favor del cónyuge") == D("-484043.13")
    assert (r.reducciones, r.cuota_a_ingresar) == (D("509195.49"), D("53121.96"))
    # Before, the insurance reduction ate into the 500.000: base 300.000 → 55.466,81.
    aragon = load_isd_params(2026).comunidades["aragon"]
    contando = [
        x.model_copy(update={"tramos": [t.model_copy(update={"hasta_completar_excluye": []}) for t in x.tramos]})
        for x in aragon.reducciones
    ]
    assert calcular_isd(caso, _sin("aragon", reducciones=contando)).cuota_a_ingresar == D("55466.81")
    # The line shows what is really applied, never more than what is left of the base:
    # 300.000 − 15.956,87 = 284.043,13 (not the 484.043,13 that would complete 500.000).
    pequena = _isd(modo="sucesion", comunidad="aragon", parentesco="hijo", edad=30, valor_recibido=300000)
    assert _linea(pequena, "Reducción aragonesa a favor del cónyuge") == D("-284043.13")
    assert pequena.reducciones == D("300000.00") and not _avisa(pequena, "Las reducciones superan")


def test_main_home_percentage_by_the_value_of_the_property_asturias_and_galicia() -> None:
    caso = BeneficiarioISD(
        modo="sucesion",
        comunidad="asturias",
        parentesco="hijo",
        edad=30,
        valor_recibido=500000,
        valor_vivienda_habitual=100000,
        valor_total_vivienda=100000,
    )
    r = calcular_isd(caso)
    # Art. 17 bis: inmueble de 90.000,01 a 120.000 € → 98 %: 98.000 (bajo el límite estatal, que se mantiene).
    # 500.000 − 300.000 − 98.000 = 102.000 → tarifa asturiana de los grupos I y II: 11.900 + 46.000 × 25,5 % =
    # 11.730 → 23.630.
    assert _linea(r, "Reducción por vivienda habitual del causante (98 %)") == D("-98000.00")
    assert r.cuota_a_ingresar == D("23630.00") and _avisa(r, "durante 3 años")
    # Before (state rule, 95 %): base 105.000 → 11.900 + 49.000 × 25,5 % = 12.495 → 24.395.
    assert calcular_isd(caso, _sin("asturias", vivienda_habitual=[])).cuota_a_ingresar == D("24395.00")
    # Without the value of the whole property, the lowest percentage of the scale, with a warning...
    sin_valor = calcular_isd(caso.model_copy(update={"valor_total_vivienda": None}))
    assert sin_valor.cuota_a_ingresar == D("24395.00") and _avisa(sin_valor, "el más bajo de la escala (95 %)")
    # ...unless the share lets it be estimated: half of a 200.000 € home → 96 %.
    mitad = calcular_isd(caso.model_copy(update={"valor_total_vivienda": None, "participacion_vivienda": D("0.5")}))
    assert _linea(mitad, "Reducción por vivienda habitual del causante (96 %)") == D("-96000.00")
    # A big share of an expensive home: 95 % × 300.000 = 285.000, capped at the state's 122.606,47.
    cara = calcular_isd(
        caso.model_copy(update={"valor_vivienda_habitual": D(300000), "valor_total_vivienda": D(300000)})
    )
    assert _linea(cara, "Reducción por vivienda habitual") == D("-122606.47")
    assert _avisa(cara, "mantiene los demás límites estatales") and not _avisa(cara, "Confírmalo")

    galicia = BeneficiarioISD(
        modo="sucesion",
        comunidad="galicia",
        parentesco="hijo",
        edad=30,
        valor_recibido=1400000,
        valor_vivienda_habitual=200000,
        valor_total_vivienda=200000,
    )
    g = calcular_isd(galicia)
    # Art. 7.Tres: inmueble de 150.000,01 a 300.000 € → 97 %: 194.000 (límite 600.000).
    # 1.400.000 − 1.000.000 − 194.000 = 206.000 → 7.750 + 81.000 × 9 % = 7.290 → 15.040.
    assert _linea(g, "Reducción por vivienda habitual del causante (97 %)") == D("-194000.00")
    assert g.cuota_a_ingresar == D("15040.00")
    # Before, descendants got the state rule (122.606,47): base 277.393,53 → 7.750 + 152.393,53 × 9 % → 21.465,42.
    solo_conyuge = load_isd_params(2026).comunidades["galicia"].vivienda_habitual[:1]
    assert calcular_isd(galicia, _sin("galicia", vivienda_habitual=solo_conyuge)).cuota_a_ingresar == D("21465.42")
    viuda = calcular_isd(galicia.model_copy(update={"parentesco": "conyuge"}))
    assert _linea(viuda, "Reducción por vivienda habitual del causante (100 %)") == D("-200000.00")
    with pytest.raises(ValidationError, match="valor_total_vivienda no puede ser menor"):
        BeneficiarioISD(
            modo="sucesion", parentesco="hijo", valor_recibido=10, valor_vivienda_habitual=10, valor_total_vivienda=5
        )


def test_the_new_schema_fields_reject_incoherent_data() -> None:
    from hacienda_gpt.tax.sucesiones_params import Equiparacion, ReduccionVivienda, TramoReduccion

    with pytest.raises(ValidationError, match="importe o porcentaje_base"):
        TramoReduccion()
    with pytest.raises(ValidationError, match="importe o porcentaje_base"):
        TramoReduccion(importe=D(1), porcentaje_base=D(1))
    with pytest.raises(ValidationError, match="hasta_completar_excluye exige hasta_completar"):
        TramoReduccion(importe=D(1), hasta_completar_excluye=["seguro_vida"])
    cita = [{"boe_id": "X", "articulo": "a", "cita": "c"}]
    comun = {"articulo": "a", "resumen": "r", "en_vigor_desde": "2026-01-01", "evidencia": cita}
    with pytest.raises(ValidationError, match="solo_en exige alcance"):
        Equiparacion(parentesco="pareja_de_hecho", equivale_a="conyuge", solo_en=["bonificaciones"], **comun)
    with pytest.raises(ValidationError, match="porcentaje o tramos_valor"):
        ReduccionVivienda(anos_mantenimiento=5, **comun)
