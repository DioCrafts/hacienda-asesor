"""Family A at its thresholds: households just inside and just outside each limit."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from hacienda_gpt.modulos.ayudas.hogar import hogar_del_perfil
from hacienda_gpt.modulos.ayudas.parametros import load_prestaciones
from hacienda_gpt.modulos.ayudas.prestaciones import (
    Evaluacion,
    evaluar_bono_social_electrico,
    evaluar_bono_social_termico,
    evaluar_capi,
    evaluar_hijo_discapacidad,
    evaluar_imv,
    evaluar_nacimiento,
    evaluar_titulo_monoparental,
    evaluar_todas,
)

HOY = date(2026, 11, 13)
P = load_prestaciones(2026)


def _imv(perfil: dict[str, Any], hoy: date = HOY) -> Evaluacion:
    return evaluar_imv(P, hogar_del_perfil(perfil, hoy))


def _solo(**extra: Any) -> dict[str, Any]:
    return {"edad": 40, "estado_civil": "soltero", "ayudas_adultos_hogar": 1, "ayudas_menores_hogar": 0, **extra}


# --- Ingreso mínimo vital ------------------------------------------------------------


def test_imv_single_adult_exactly_ten_euros_below_the_guaranteed_income() -> None:
    # 733,60 € guaranteed; 723,60 €/month of income is "inferior, al menos en 10 euros".
    e = _imv(_solo(ayudas_ingresos_hogar="8683.20", ayudas_patrimonio_neto_hogar=0))
    assert e.estado == "cumple"
    assert e.importe_mensual == Decimal("10.00")
    assert any("8.803,20 euros" in c.cita for c in e.citas)


def test_imv_single_adult_one_cent_over() -> None:
    e = _imv(_solo(ayudas_ingresos_hogar="8683.32", ayudas_patrimonio_neto_hogar=0))
    assert e.estado == "no_cumple"


def test_imv_couple_with_two_children_and_the_disability_complement() -> None:
    perfil = {
        "edad": 38,
        "estado_civil": "casado",
        "ayudas_adultos_hogar": 2,
        "ayudas_menores_hogar": 2,
        "ayudas_ingresos_hogar": 6000,
        "ayudas_patrimonio_neto_hogar": 1000,
        "descendientes": [{"edad": 6, "grado_discapacidad": 65}, {"edad": 9}],
    }
    e = _imv(perfil)
    # 733,60 × 1,9 = 1.393,84 + 22 % of 733,60 (161,39) = 1.555,23; minus 500 €/month of income.
    assert e.estado == "cumple"
    assert e.importe_mensual == Decimal("1055.23")
    assert e.lineas[-2].importe == Decimal("500.00")


def test_imv_wealth_limit_and_missing_wealth() -> None:
    limite = Decimal(3) * Decimal("733.60") * 12  # 26.409,60 €
    assert _imv(_solo(ayudas_ingresos_hogar=0, ayudas_patrimonio_neto_hogar=limite)).estado == "no_cumple"
    assert (
        _imv(_solo(ayudas_ingresos_hogar=0, ayudas_patrimonio_neto_hogar=limite - Decimal("0.01"))).estado == "cumple"
    )
    sin_patrimonio = _imv(_solo(ayudas_ingresos_hogar=0))
    assert sin_patrimonio.estado == "probable"
    assert any("26.409,60" in r for r in sin_patrimonio.requisitos_no_comprobados)


def test_imv_age_and_missing_data() -> None:
    assert _imv(_solo(edad=22, ayudas_ingresos_hogar=0, ayudas_patrimonio_neto_hogar=0)).estado == "no_cumple"
    faltan = _imv({"edad": 40, "estado_civil": "soltero", "num_descendientes": 2})
    assert faltan.estado == "faltan_datos"
    assert any("ayudas_ingresos_hogar" in f for f in faltan.faltan)


def test_imv_with_income_estimated_from_the_fiscal_profile_is_only_probable() -> None:
    e = _imv(
        {"edad": 40, "estado_civil": "soltero", "rendimiento_trabajo_bruto": 6000, "cotizaciones_seguridad_social": 381}
    )
    assert e.estado == "probable"
    assert e.importe_mensual == Decimal("733.60") - (Decimal(5619) / 12).quantize(Decimal("0.01"))


def test_already_requested_is_not_proposed() -> None:
    e = _imv(_solo(ayudas_ingresos_hogar=0, ayudas_ya_solicitadas=["imv"]))
    assert e.estado == "ya_solicitada" and not e.proponible


# --- Complemento de ayuda para la infancia -----------------------------------------------


def _capi(ingresos: Decimal) -> Evaluacion:
    perfil = {
        "estado_civil": "casado",
        "ayudas_adultos_hogar": 2,
        "ayudas_menores_hogar": 1,
        "ayudas_ingresos_hogar": ingresos,
        "ayudas_patrimonio_neto_hogar": 0,
        "descendientes": [{"fecha_nacimiento": "2024-05-01"}],
    }
    return evaluar_capi(P, hogar_del_perfil(perfil, HOY))


def test_capi_at_300_percent_of_the_scale() -> None:
    # 733,60 × 1,6 × 12 = 14.085,12; × 300 % = 42.255,36 (ingresos "inferiores").
    dentro = _capi(Decimal("42255.35"))
    assert dentro.estado == "cumple"
    assert dentro.importe_anual is None and "no se ha podido leer" in dentro.importe_texto
    assert _capi(Decimal("42255.36")).estado == "no_cumple"


def test_capi_without_minors() -> None:
    assert evaluar_capi(P, hogar_del_perfil(_solo(ayudas_ingresos_hogar=0), HOY)).estado == "no_cumple"


# --- Asignación por hijo con discapacidad ----------------------------------------------------


@pytest.mark.parametrize(
    ("hijo", "importe"),
    [
        ({"edad": 10, "grado_discapacidad": 33}, Decimal("1000.00")),
        ({"edad": 10, "grado_discapacidad": 32}, None),
        ({"edad": 20, "grado_discapacidad": 64}, None),
        ({"edad": 20, "grado_discapacidad": 65}, Decimal("5962.80")),
        ({"edad": 20, "grado_discapacidad": 75, "necesita_asistencia": True}, Decimal("8942.40")),
    ],
)
def test_child_disability_allowance(hijo: dict[str, Any], importe: Decimal | None) -> None:
    e = evaluar_hijo_discapacidad(P, hogar_del_perfil({"descendientes": [hijo]}, HOY))
    assert e.importe_anual == importe
    assert e.estado == ("cumple" if importe else "no_cumple")


def test_child_disability_counted_but_without_detail() -> None:
    e = evaluar_hijo_discapacidad(
        P, hogar_del_perfil({"num_descendientes": 1, "num_descendientes_discapacidad": 1}, HOY)
    )
    assert e.estado == "faltan_datos"


# --- Prestación por nacimiento ----------------------------------------------------------------


def _nacimiento(ingresos: Decimal, **extra: Any) -> Evaluacion:
    perfil = {
        "familia_monoparental": True,
        "ayudas_adultos_hogar": 1,
        "ayudas_menores_hogar": 1,
        "ayudas_ingresos_hogar": ingresos,
        "descendientes": [{"fecha_nacimiento": "2026-03-01"}],
        **extra,
    }
    return evaluar_nacimiento(P, hogar_del_perfil(perfil, HOY), None)


def test_birth_allowance_at_the_income_limit() -> None:
    e = _nacimiento(Decimal("15356.00"))
    assert e.estado == "cumple" and e.importe_anual == Decimal(1000)
    assert e.vence == date(2031, 3, 1)  # five years (art. 53.1 LGSS)
    assert _nacimiento(Decimal("15856.00")).importe_anual == Decimal("500.00")
    assert _nacimiento(Decimal("16346.00")).importe_anual == Decimal("10.00")
    assert _nacimiento(Decimal("16346.01")).estado == "no_cumple"  # difference under 10 €


def test_birth_allowance_large_family_limit_and_cases() -> None:
    hijos = [{"fecha_nacimiento": "2026-02-01"}, {"edad": 5}, {"edad": 8}, {"edad": 12}]
    perfil = {"familia_numerosa": True, "ayudas_ingresos_hogar": Decimal("26854.00"), "descendientes": hijos}
    e = evaluar_nacimiento(P, hogar_del_perfil(perfil, HOY), None)
    assert e.estado == "cumple" and e.importe_anual == Decimal(1000)  # 23.109 + 3.745 for the 4th child
    sin_supuesto = evaluar_nacimiento(P, hogar_del_perfil({**perfil, "familia_numerosa": False}, HOY), None)
    assert sin_supuesto.estado == "no_cumple"
    progenitor = evaluar_nacimiento(P, hogar_del_perfil({**perfil, "familia_numerosa": False}, HOY), 65)
    assert progenitor.estado == "no_cumple"  # 26.854 € is over the general limit with four children (24.569,60 €)
    antiguo = _nacimiento(Decimal(0), descendientes=[{"fecha_nacimiento": "2021-11-12"}])
    assert antiguo.estado == "no_cumple"  # prescribed


def test_birth_of_an_earlier_year_is_only_probable() -> None:
    e = _nacimiento(Decimal(9000), descendientes=[{"fecha_nacimiento": "2024-06-01"}])
    assert e.estado == "probable"
    assert any("límite de ingresos de ese año" in r for r in e.requisitos_no_comprobados)


# --- Bono social eléctrico y térmico -----------------------------------------------------------


def _bono(base: Decimal, hoy: date = HOY, **extra: Any) -> Evaluacion:
    perfil = {
        "ayudas_adultos_hogar": 1,
        "ayudas_menores_hogar": 0,
        "ayudas_base_imponible_hogar": base,
        "ayudas_contrato_pvpc": True,
        **extra,
    }
    return evaluar_bono_social_electrico(P, hogar_del_perfil(perfil, hoy))


def test_electricity_social_bonus_income_thresholds() -> None:
    # 1,5 × IPREM of 14 payments (8.400 €) = 12.600 €; severe at 50 %.
    assert _bono(Decimal("12600")).estado == "cumple"
    assert "42,5 %" in _bono(Decimal("12600")).importe_texto
    assert _bono(Decimal("12600.01")).estado == "no_cumple"
    assert "57,5 %" in _bono(Decimal("6300")).importe_texto
    assert "42,5 %" in _bono(Decimal("6300.01")).importe_texto
    assert _bono(Decimal("12600"), date(2027, 1, 15)).importe_texto.startswith("Descuento del 35 %")


def test_electricity_social_bonus_household_and_special_circumstances() -> None:
    # 1,5 + 0,3 + 0,5 = 2,3 × 8.400 = 19.320 €; +1 for a single parent with a minor.
    pareja: dict[str, Any] = {"ayudas_adultos_hogar": 2, "ayudas_menores_hogar": 1, "estado_civil": "casado"}
    assert _bono(Decimal("19320"), **pareja).estado == "cumple"
    assert _bono(Decimal("19320.01"), **pareja).estado == "no_cumple"
    sola: dict[str, Any] = {"ayudas_adultos_hogar": 1, "ayudas_menores_hogar": 1, "estado_civil": "divorciado"}
    assert _bono(Decimal("21000"), **sola).estado == "cumple"  # 3,0 × 8.400 = 25.200
    assert _bono(Decimal("25200.01"), **sola).estado == "no_cumple"


def test_electricity_social_bonus_large_family_and_pvpc() -> None:
    e = _bono(Decimal("90000"), familia_numerosa=True)
    assert e.estado == "cumple" and "familia numerosa" in e.motivo and "42,5 %" in e.importe_texto
    assert "57,5 %" in _bono(Decimal("16800"), familia_numerosa=True).importe_texto  # two times the IPREM
    sin_pvpc = _bono(Decimal("10000"), ayudas_contrato_pvpc=False)
    assert sin_pvpc.estado == "probable"
    assert any("PVPC" in r for r in sin_pvpc.requisitos_no_comprobados)


def test_thermal_social_bonus_follows_the_electric_one() -> None:
    h = hogar_del_perfil(
        {
            "ayudas_adultos_hogar": 1,
            "ayudas_menores_hogar": 0,
            "ayudas_base_imponible_hogar": 9000,
            "ayudas_contrato_pvpc": True,
        },
        HOY,
    )
    termico = evaluar_bono_social_termico(P, h, evaluar_bono_social_electrico(P, h))
    assert termico.estado == "probable" and termico.vence == date(2026, 12, 31)
    h2 = hogar_del_perfil({"ayudas_ya_solicitadas": ["bono_social_electrico"]}, HOY)
    assert evaluar_bono_social_termico(P, h2, evaluar_bono_social_electrico(P, h2)).estado == "automatica"


# --- Título de familia monoparental -----------------------------------------------------------


def test_single_parent_title_opens_in_2027() -> None:
    perfil = {"familia_monoparental": True, "descendientes": [{"edad": 4}, {"edad": 7}]}
    antes = evaluar_titulo_monoparental(P, hogar_del_perfil(perfil, HOY))
    assert antes.estado == "proximamente" and antes.vence == date(2027, 1, 1) and "especial" in antes.motivo
    despues = evaluar_titulo_monoparental(P, hogar_del_perfil(perfil, date(2027, 1, 2)))
    assert despues.estado == "probable"
    pareja = evaluar_titulo_monoparental(P, hogar_del_perfil({"estado_civil": "casado", "num_descendientes": 1}, HOY))
    assert pareja.estado == "no_cumple"


def test_a_well_off_couple_without_children_is_proposed_nothing() -> None:
    perfil = {
        "edad": 45,
        "estado_civil": "casado",
        "num_descendientes": 0,
        "rendimiento_trabajo_bruto": 48000,
        "cotizaciones_seguridad_social": 3100,
        "datos_conyuge": {"rendimiento_trabajo_bruto": 32000},
    }
    evaluaciones = evaluar_todas(P, hogar_del_perfil(perfil, HOY))
    assert [e.id for e in evaluaciones if e.proponible] == []
