"""Calculators checked against the quoted rules, with the age and antiquity boundary cases.

Every expected date below was worked by hand from the quoted sentence (shown in each test and checked to be
in the BOE extracts), counting years and months "de fecha a fecha" and days excluding the first one, as art.
5.1 of the Código Civil says.
"""

from datetime import date
from pathlib import Path

import pytest

from hacienda_gpt.modulos.caducidades import calculo
from hacienda_gpt.modulos.caducidades.calculo import CalculoError, sumar_anos, sumar_meses
from hacienda_gpt.modulos.caducidades.reglas import cargar_reglas, textos_de_directorio

R = cargar_reglas()
EXTRACTOS = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "caducidades"
HOY = date(2026, 11, 13)


def _en_ley(boe_id: str, frase: str) -> bool:
    return " ".join(frase.split()) in " ".join(textos_de_directorio(EXTRACTOS)[boe_id].split())


def _citado(v: calculo.Vencimiento, frase: str) -> bool:
    return any(frase in ev.cita for ev in v.citas)


# --- Art. 5.1 Código Civil ----------------------------------------------------


def test_periods_count_date_to_date_and_end_on_the_last_day_of_a_shorter_month() -> None:
    assert _en_ley(
        "BOE-A-1889-4763",
        "si los plazos estuviesen fijados por meses o años, se computarán de fecha a fecha. Cuando en el mes del "
        "vencimiento no hubiera día equivalente al inicial del cómputo, se entenderá que el plazo expira el último del mes.",
    )
    assert sumar_anos(date(2024, 2, 29), 3) == date(2027, 2, 28)
    assert sumar_meses(date(2027, 3, 31), -1) == date(2027, 2, 28)
    assert sumar_meses(date(2026, 1, 31), 1) == date(2026, 2, 28)


# --- DNI: art. 6.1 RD 1553/2005 -------------------------------------------------

DNI_B = (
    "b) Cinco años, cuando el titular haya cumplido los cinco años de edad y no haya alcanzado los treinta al "
    "momento de la expedición o renovación."
)


def test_dni_renewed_at_29_lasts_five_years_although_the_holder_turns_30_during_validity() -> None:
    assert _en_ley("BOE-A-2005-21163", DNI_B)
    v = calculo.calcular_dni(R, fecha_expedicion=date(2020, 3, 14), fecha_nacimiento=date(1990, 3, 15))
    assert v.fecha == date(2025, 3, 14)
    assert _citado(v, DNI_B)


def test_dni_renewed_on_the_30th_birthday_lasts_ten_years() -> None:
    v = calculo.calcular_dni(R, fecha_expedicion=date(2020, 3, 15), fecha_nacimiento=date(1990, 3, 15))
    assert v.fecha == date(2030, 3, 15)
    assert _citado(v, "c) Diez años, cuando el titular haya cumplido los treinta y no haya alcanzado los setenta.")


def test_dni_renewed_at_69_lasts_ten_years_although_the_holder_turns_70_during_validity() -> None:
    v = calculo.calcular_dni(R, fecha_expedicion=date(2026, 5, 31), fecha_nacimiento=date(1956, 6, 1))
    assert v.fecha == date(2036, 5, 31)
    assert not v.permanente


def test_dni_renewed_at_70_is_permanent() -> None:
    assert _en_ley("BOE-A-2005-21163", "d) Permanente cuando el titular haya cumplido los setenta años.")
    v = calculo.calcular_dni(R, fecha_expedicion=date(2026, 6, 1), fecha_nacimiento=date(1956, 6, 1))
    assert v.permanente and v.fecha is None


def test_dni_of_a_child_under_five_lasts_two_years_and_from_five_five() -> None:
    assert _en_ley("BOE-A-2005-21163", "a) Dos años cuando el solicitante no haya cumplido los cinco años de edad.")
    nacimiento = date(2023, 1, 10)
    assert calculo.calcular_dni(R, fecha_expedicion=date(2028, 1, 9), fecha_nacimiento=nacimiento).fecha == date(
        2030, 1, 9
    )
    assert calculo.calcular_dni(R, fecha_expedicion=date(2028, 1, 10), fecha_nacimiento=nacimiento).fecha == date(
        2033, 1, 10
    )


def test_dni_printed_date_prevails_and_a_mismatch_is_flagged() -> None:
    v = calculo.calcular_dni(
        R,
        fecha_expedicion=date(2020, 3, 15),
        fecha_nacimiento=date(1990, 3, 15),
        fecha_caducidad=date(2030, 3, 16),
    )
    assert v.fecha == date(2030, 3, 16) and v.origen == "impresa"
    assert any("no coincide" in a for a in v.avisos)


def test_dni_without_dates_is_a_readable_error() -> None:
    with pytest.raises(CalculoError, match="fecha de nacimiento"):
        calculo.calcular_dni(R, fecha_expedicion=date(2020, 1, 1))


# --- Passport: art. 5 RD 896/2003 -----------------------------------------------

PAS = (
    "el pasaporte tendrá una validez improrrogable de cinco años, si el titular tiene menos de treinta en la fecha "
    "de su expedición, y de diez años, cuando haya cumplido esa edad. Para los menores de cinco años la validez del "
    "pasaporte se limitará a dos años."
)


def test_passport_at_29_five_years_at_30_ten_years() -> None:
    assert _en_ley("BOE-A-2003-13978", PAS)
    nacimiento = date(2000, 7, 1)
    assert calculo.calcular_pasaporte(R, fecha_expedicion=date(2030, 6, 30), fecha_nacimiento=nacimiento).fecha == (
        date(2035, 6, 30)
    )
    assert calculo.calcular_pasaporte(R, fecha_expedicion=date(2030, 7, 1), fecha_nacimiento=nacimiento).fecha == (
        date(2040, 7, 1)
    )


def test_passport_of_a_child_under_14_is_capped_at_14_unless_they_had_a_dni() -> None:
    assert _en_ley(
        "BOE-A-2003-13978",
        "Los pasaportes expedidos a los menores de catorce años residentes en España que carezcan de documento "
        "nacional de identidad, tendrán validez máxima hasta que el menor alcance dicha edad",
    )
    kwargs = {"fecha_expedicion": date(2026, 1, 1), "fecha_nacimiento": date(2015, 1, 1)}
    assert calculo.calcular_pasaporte(R, **kwargs).fecha == date(2029, 1, 1)
    assert calculo.calcular_pasaporte(R, tiene_dni=True, **kwargs).fecha == date(2031, 1, 1)


# --- Driving licence: arts. 12 and 13 RGC ---------------------------------------

RGC_12_2 = (
    "2. El permiso de las clases restantes y la licencia de conducción, cualquiera que sea su clase, tendrán un "
    "período de vigencia de diez años mientras su titular no cumpla los sesenta y cinco años y de cinco años a "
    "partir de esa edad."
)
RGC_12_1 = (
    "1. El permiso de conducción de las clases C1, C1 + E, C, C + E, D1, D1 + E, D y D + E tendrá un período de "
    "vigencia de cinco años mientras su titular no cumpla los sesenta y cinco años y de tres años a partir de esa edad."
)


def test_licence_b_renewed_at_63_lasts_ten_years_and_the_next_extension_at_73_five() -> None:
    assert _en_ley("BOE-A-2009-9481", RGC_12_2)
    v = calculo.calcular_permiso(
        R, clases=["B"], fecha_inicio_vigencia=date(2025, 8, 9), fecha_nacimiento=date(1961, 8, 10)
    )
    assert v.fecha == date(2035, 8, 9)
    assert any("5 años desde 09/08/2035, hasta 09/08/2040" in ln.valor for ln in v.lineas)
    assert _citado(v, RGC_12_2)


def test_licence_b_renewed_on_the_65th_birthday_lasts_five_years() -> None:
    v = calculo.calcular_permiso(
        R, clases=["B"], fecha_inicio_vigencia=date(2025, 3, 1), fecha_nacimiento=date(1960, 3, 1)
    )
    assert v.fecha == date(2030, 3, 1)


def test_licence_b_renewed_the_day_before_65_lasts_ten_years() -> None:
    v = calculo.calcular_permiso(
        R, clases=["B"], fecha_inicio_vigencia=date(2025, 2, 28), fecha_nacimiento=date(1960, 3, 1)
    )
    assert v.fecha == date(2035, 2, 28)


def test_professional_licence_five_years_before_65_three_after() -> None:
    assert _en_ley("BOE-A-2009-9481", RGC_12_1)
    v = calculo.calcular_permiso(
        R, clases=["C"], fecha_inicio_vigencia=date(2026, 1, 19), fecha_nacimiento=date(1962, 1, 20)
    )
    assert v.fecha == date(2031, 1, 19)
    assert any("3 años desde 19/01/2031, hasta 19/01/2034" in ln.valor for ln in v.lineas)


def test_holder_over_65_with_c_and_b_expires_with_the_shorter_period() -> None:
    v = calculo.calcular_permiso(
        R, clases=["B", "C1 + E"], fecha_inicio_vigencia=date(2026, 2, 1), fecha_nacimiento=date(1960, 1, 1)
    )
    assert v.fecha == date(2029, 2, 1)  # group 2 from 65: three years (B alone would be five)
    assert any("grupo 2" in a for a in v.avisos)


def test_licence_extension_window_is_three_months_before_expiry() -> None:
    assert _en_ley(
        "BOE-A-2009-9481",
        "La solicitud de prórroga de vigencia del permiso o licencia podrá presentarse con una antelación máxima de "
        "tres meses a su fecha de caducidad, computándose desde esta última fecha el nuevo período de vigencia",
    )
    v = calculo.calcular_permiso(R, clases=["B"], fecha_caducidad=date(2027, 5, 31))
    assert any(ln.valor == "28/02/2027" for ln in v.lineas)


def test_unknown_licence_class_is_rejected() -> None:
    with pytest.raises(CalculoError, match="Clase de permiso desconocida"):
        calculo.calcular_permiso(R, clases=["Z"], fecha_caducidad=date(2030, 1, 1))


# --- ITV: art. 6 RD 920/2017 ------------------------------------------------------

M1 = "Hasta 4 años: Exento. De más de 4 años: Bienal. De más de 10 años: Anual."
ART_6_5 = (
    "si dicha fecha está comprendida en los 30 días naturales precedentes a la expiración del plazo de validez de la "
    "inspección anterior, el plazo de validez se obtendrá adicionando la frecuencia correspondiente a la citada fecha "
    "de expiración."
)


def test_car_turning_four_today_has_its_first_itv_today() -> None:
    assert _en_ley("BOE-A-2017-12841", M1)
    v = calculo.calcular_itv(R, hoy=HOY, categoria="M1", fecha_primera_matriculacion=date(2022, 11, 13))
    assert v.fecha == date(2026, 11, 13)
    assert _citado(v, M1)


def test_four_year_old_car_inspected_at_its_due_date_returns_in_two_years() -> None:
    v = calculo.calcular_itv(R, hoy=HOY, categoria="M1", fecha_primera_matriculacion=date(2022, 3, 1))
    assert v.fecha == date(2028, 3, 1)
    assert any("Sin la fecha de la última ITV" in a for a in v.avisos)
    v2 = calculo.calcular_itv(
        R, hoy=HOY, categoria="M1", fecha_primera_matriculacion=date(2022, 3, 1), fecha_ultima_itv=date(2026, 3, 1)
    )
    assert v2.fecha == date(2028, 3, 1)


def test_ten_year_old_car_goes_annual() -> None:
    v = calculo.calcular_itv(
        R, hoy=HOY, categoria="M1", fecha_primera_matriculacion=date(2016, 5, 10), fecha_ultima_itv=date(2026, 5, 10)
    )
    assert v.fecha == date(2027, 5, 10)


def test_nine_year_old_car_stays_biennial() -> None:
    v = calculo.calcular_itv(
        R, hoy=HOY, categoria="M1", fecha_primera_matriculacion=date(2016, 5, 10), fecha_ultima_itv=date(2025, 5, 10)
    )
    assert v.fecha == date(2027, 5, 10)


def test_inspection_in_the_30_days_before_expiry_counts_from_the_expiry() -> None:
    assert _en_ley("BOE-A-2017-12841", ART_6_5)
    v = calculo.calcular_itv(
        R,
        hoy=HOY,
        categoria="M1",
        fecha_primera_matriculacion=date(2016, 5, 10),
        fecha_ultima_itv=date(2026, 4, 20),
        fecha_caducidad_anterior=date(2026, 5, 10),
    )
    # Counted from the expiry (10/05/2026), when the car is ten: annual.
    assert v.fecha == date(2027, 5, 10)
    assert _citado(v, ART_6_5)


def test_inspection_more_than_30_days_early_counts_from_the_inspection() -> None:
    v = calculo.calcular_itv(
        R,
        hoy=HOY,
        categoria="M1",
        fecha_primera_matriculacion=date(2016, 5, 10),
        fecha_ultima_itv=date(2026, 4, 9),
        fecha_caducidad_anterior=date(2026, 5, 10),
    )
    assert v.fecha == date(2028, 4, 9)  # the car is nine on 09/04/2026: biennial


def test_van_moped_and_taxi_tables() -> None:
    assert _en_ley("BOE-A-2017-12841", "Hasta 2 años: Exento. De 2 a 6 años: Bienal. De 6 a 10 años: Anual.")
    van = calculo.calcular_itv(
        R, hoy=HOY, categoria="N1", fecha_primera_matriculacion=date(2020, 1, 15), fecha_ultima_itv=date(2026, 1, 15)
    )
    assert van.fecha == date(2027, 1, 15)
    moped = calculo.calcular_itv(
        R, hoy=date(2026, 1, 1), categoria="L1e", fecha_primera_matriculacion=date(2024, 2, 29)
    )
    assert moped.fecha == date(2027, 2, 28)
    taxi = calculo.calcular_itv(
        R, hoy=HOY, categoria="M1_taxi_ambulancia_escolar", fecha_primera_matriculacion=date(2024, 6, 1)
    )
    assert taxi.fecha == date(2027, 6, 1)


def test_itv_card_date_prevails() -> None:
    v = calculo.calcular_itv(
        R,
        hoy=HOY,
        categoria="M1",
        fecha_primera_matriculacion=date(2016, 5, 10),
        fecha_ultima_itv=date(2026, 5, 10),
        fecha_proxima_itv=date(2027, 5, 9),
    )
    assert v.fecha == date(2027, 5, 9) and v.origen == "impresa"


def test_unknown_vehicle_category_is_rejected() -> None:
    with pytest.raises(CalculoError, match="Categoría"):
        calculo.calcular_itv(R, hoy=HOY, categoria="X", fecha_primera_matriculacion=date(2020, 1, 1))


# --- Certificates, insurance, contracts -------------------------------------------


def test_certificate_needs_its_own_date_and_checks_the_legal_maximum() -> None:
    assert _en_ley(
        "BOE-A-2020-14046", "2. El período de vigencia de los certificados cualificados no será superior a cinco años."
    )
    sin = calculo.calcular_certificado(R, fecha_expedicion=date(2024, 3, 1))
    assert sin.fecha is None
    assert any("no después del 01/03/2029" in ln.valor for ln in sin.lineas)
    mal = calculo.calcular_certificado(R, fecha_expedicion=date(2024, 3, 1), fecha_caducidad=date(2030, 3, 1))
    assert any("supera la vigencia máxima" in a for a in mal.avisos)


def test_insurance_opposition_deadline_is_one_month_before_renewal() -> None:
    assert _en_ley(
        "BOE-A-1980-22501",
        "efectuada con un plazo de, al menos, un mes de anticipación a la conclusión del período del seguro en curso "
        "cuando quien se oponga a la prórroga sea el tomador",
    )
    v = calculo.calcular_seguro(R, hoy=HOY, fecha_vencimiento=date(2027, 3, 31))
    assert v.fecha == date(2027, 3, 31) and v.accion_hasta == date(2027, 2, 28)


def test_insurance_date_in_the_past_rolls_forward() -> None:
    v = calculo.calcular_seguro(R, hoy=HOY, fecha_vencimiento=date(2025, 1, 5))
    assert v.fecha == date(2027, 1, 5) and v.accion_hasta == date(2026, 12, 5)
    assert any("se ha prorrogado" in a for a in v.avisos)


def test_life_insurance_has_no_opposition_deadline() -> None:
    v = calculo.calcular_seguro(R, hoy=HOY, fecha_vencimiento=date(2027, 3, 31), es_vida=True)
    assert v.accion_hasta is None and any("art. 22.5" in a for a in v.avisos)


def test_generic_contract_uses_its_own_notice_period() -> None:
    v = calculo.calcular_contrato(hoy=HOY, fecha_vencimiento=date(2027, 1, 31), preaviso_dias=15)
    assert v.accion_hasta == date(2027, 1, 16)


# --- Purchases: TRLGDCU ------------------------------------------------------------

ART_120 = (
    "se manifiesten en un plazo de tres años desde la entrega en el caso de bienes o de dos años en el caso de "
    "contenidos o servicios digitales"
)
ART_121 = (
    "se presumirá que las faltas de conformidad que se manifiesten en los dos años siguientes a la entrega del bien "
    "o en el año siguiente al suministro del contenido o servicio digital"
)


def test_goods_bought_in_a_shop_three_years_guarantee_two_of_presumption_no_withdrawal() -> None:
    assert _en_ley("BOE-A-2007-20555", ART_120) and _en_ley("BOE-A-2007-20555", ART_121)
    r = calculo.calcular_compra(R, hoy=HOY, descripcion="Lavadora", fecha_compra=date(2026, 11, 8))
    assert r.fin_garantia == date(2029, 11, 8)
    assert r.fin_presuncion == date(2028, 11, 8)
    assert r.fin_desistimiento is None
    assert {v.tipo for v in r.vencimientos} == {"garantia", "presuncion"}


def test_online_purchase_withdrawal_counts_from_delivery_excluding_that_day() -> None:
    assert _en_ley(
        "BOE-A-2007-20555",
        "el consumidor o usuario tendrá derecho a desistir del contrato durante un periodo de catorce días naturales",
    )
    r = calculo.calcular_compra(
        R,
        hoy=HOY,
        descripcion="Portátil",
        fecha_compra=date(2026, 11, 2),
        fecha_entrega=date(2026, 11, 5),
        modalidad="distancia",
    )
    assert r.fin_desistimiento == date(2026, 11, 19)
    assert r.fin_garantia == date(2029, 11, 5)


def test_digital_content_two_years_one_of_presumption_withdrawal_from_the_contract() -> None:
    r = calculo.calcular_compra(
        R,
        hoy=HOY,
        descripcion="Programa",
        fecha_compra=date(2026, 11, 2),
        fecha_entrega=date(2026, 11, 4),
        tipo_bien="digital",
        modalidad="distancia",
    )
    assert r.fin_garantia == date(2028, 11, 4)
    assert r.fin_presuncion == date(2027, 11, 4)
    assert r.fin_desistimiento == date(2026, 11, 16)


def test_second_hand_with_one_year_agreed() -> None:
    r = calculo.calcular_compra(
        R,
        hoy=HOY,
        descripcion="Bici usada",
        fecha_compra=date(2026, 6, 1),
        tipo_bien="segunda_mano",
        garantia_pactada_anos=1,
    )
    assert r.fin_garantia == date(2027, 6, 1) and r.fin_presuncion == date(2027, 6, 1)
    menos = calculo.calcular_compra(
        R,
        hoy=HOY,
        descripcion="Bici usada",
        fecha_compra=date(2026, 6, 1),
        tipo_bien="segunda_mano",
        garantia_pactada_anos=0,
    )
    assert menos.fin_garantia == date(2027, 6, 1)
    assert any("no puede ser inferior" in a for a in menos.avisos)


def test_unsolicited_home_visit_thirty_days() -> None:
    r = calculo.calcular_compra(
        R, hoy=HOY, descripcion="Aspiradora", fecha_compra=date(2026, 11, 1), modalidad="visita_no_solicitada"
    )
    assert r.fin_desistimiento == date(2026, 12, 1)


def test_leap_day_delivery() -> None:
    r = calculo.calcular_compra(R, hoy=HOY, descripcion="Tele", fecha_compra=date(2024, 2, 29))
    assert r.fin_garantia == date(2027, 2, 28)


def test_purchase_before_2022_is_not_modelled() -> None:
    r = calculo.calcular_compra(R, hoy=HOY, descripcion="Móvil", fecha_compra=date(2021, 12, 31))
    assert r.fin_garantia is None
    assert any("redacción anterior" in a for a in r.avisos)
    assert any("ya habría terminado" in a for a in r.avisos)


def test_purchase_from_a_private_seller_has_no_consumer_guarantee() -> None:
    r = calculo.calcular_compra(R, hoy=HOY, descripcion="Coche", fecha_compra=date(2026, 1, 1), vendedor_empresa=False)
    assert r.fin_garantia is None and r.vencimientos == []
