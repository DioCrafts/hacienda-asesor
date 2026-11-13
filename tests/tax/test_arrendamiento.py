"""Reduction for letting a dwelling (art. 23.2 LIRPF and transitional provision 38)."""

from datetime import date
from decimal import Decimal

import pytest

from hacienda_gpt.tax.arrendamiento import DatosArrendamiento, reduccion_arrendamiento
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import load_params
from hacienda_gpt.tax.profile import build_input

D = Decimal
NUEVO = date(2026, 12, 15)


def _pct(ejercicio: int = 2026, **kw: object) -> D:
    ra = load_params(ejercicio).reduccion_arrendamiento
    assert ra is not None
    return reduccion_arrendamiento(DatosArrendamiento(**kw), ra).porcentaje


def test_contracts_before_the_housing_act_get_60_percent() -> None:
    assert _pct(fecha_contrato=date(2019, 3, 1)) == D("0.60")
    assert _pct(fecha_contrato=date(2023, 5, 25), zona_tensionada=True) == D("0.60")
    assert _pct(2025, fecha_contrato=date(2019, 3, 1)) == D("0.60")


@pytest.mark.parametrize(
    ("datos", "esperado"),
    [
        ({}, "0.50"),
        ({"zona_tensionada": True, "variacion_renta": D(-6)}, "0.90"),
        ({"zona_tensionada": True, "variacion_renta": D(-5)}, "0.50"),  # must drop by MORE than 5 %
        ({"variacion_renta": D(-10)}, "0.50"),  # not in a stressed area
        ({"primer_alquiler": True, "zona_tensionada": True, "inquilino_joven": True}, "0.70"),
        ({"primer_alquiler": True, "inquilino_joven": True}, "0.50"),
        ({"alquiler_social": True}, "0.70"),
        ({"vivienda_rehabilitada": True}, "0.60"),
        ({"vivienda_rehabilitada": True, "alquiler_social": True}, "0.70"),
    ],
)
def test_contracts_between_the_housing_act_and_december_2026(datos: dict[str, object], esperado: str) -> None:
    assert _pct(fecha_contrato=date(2024, 6, 1), **datos) == D(esperado)
    assert _pct(2025, fecha_contrato=date(2024, 6, 1), **datos) == D(esperado)
    # The boundary date itself is treated as the old regime.
    assert _pct(fecha_contrato=date(2026, 12, 1), **datos) == D(esperado)


@pytest.mark.parametrize(
    ("datos", "esperado"),
    [
        # a) new contract, rent lowered by more than 5 %
        ({"variacion_renta": D(-6), "mismo_inquilino": True}, "1.00"),
        ({"variacion_renta": D(-6), "renta_bajo_indice": True}, "1.00"),
        ({"variacion_renta": D(-6), "zona_tensionada": True, "inquilino_joven": True}, "0.95"),
        ({"variacion_renta": D(-6), "zona_tensionada": True}, "0.90"),
        ({"variacion_renta": D(-6), "inquilino_joven": True}, "0.85"),
        ({"variacion_renta": D(-6)}, "0.70"),
        # a) rent not raised
        ({"variacion_renta": D(-5)}, "0.50"),
        ({"variacion_renta": D(0)}, "0.50"),
        # a) rent raised
        ({"variacion_renta": D(5)}, "0.40"),
        ({"variacion_renta": D("5.01")}, "0.30"),
        ({"variacion_renta": D(10)}, "0.30"),
        ({"variacion_renta": D(15)}, "0.25"),
        ({"variacion_renta": D(20)}, "0.20"),
        ({"variacion_renta": D(21)}, "0.15"),
        # b) first letting
        ({"primer_alquiler": True, "renta_bajo_indice": True}, "1.00"),
        ({"primer_alquiler": True, "zona_tensionada": True, "inquilino_joven": True}, "0.95"),
        ({"primer_alquiler": True, "zona_tensionada": True, "renta_bajo_indice": False}, "0.90"),
        ({"primer_alquiler": True, "inquilino_joven": True}, "0.85"),
        ({"primer_alquiler": True, "renta_bajo_indice": False}, "0.50"),
        # c) and d), and the largest wins
        ({"alquiler_social": True, "variacion_renta": D(8)}, "0.70"),
        ({"vivienda_rehabilitada": True, "variacion_renta": D(8)}, "0.60"),
        # tacit extension
        ({"variacion_renta": D(8), "prorroga_tacita": True, "renta_bajo_indice": True}, "0.80"),
        ({"variacion_renta": D(8), "prorroga_tacita": True}, "0.30"),
        # large landlords: only c) and d)
        ({"gran_tenedor": True, "variacion_renta": D(-6), "mismo_inquilino": True}, "0"),
        ({"gran_tenedor": True, "primer_alquiler": True, "renta_bajo_indice": True}, "0"),
        ({"gran_tenedor": True, "alquiler_social": True}, "0.70"),
        # no rent comparison and not a first letting: nothing from a)
        ({}, "0"),
    ],
)
def test_contracts_after_december_2026(datos: dict[str, object], esperado: str) -> None:
    assert _pct(fecha_contrato=NUEVO, **datos) == D(esperado)


def test_2025_has_no_new_regime() -> None:
    ra = load_params(2025).reduccion_arrendamiento
    assert ra is not None and ra.regimen_2026 is None


def test_calculator_applies_the_reduction() -> None:
    base = dict(ejercicio=2026, rendimiento_trabajo_bruto=D(30000), cotizaciones_seguridad_social=D(1900))
    sin = calcular_irpf(IRPFInput(**base))
    con = calcular_irpf(
        IRPFInput(
            **base, rendimiento_alquiler_vivienda_sin_reducir=D(8000), arrendamiento_fecha_contrato=date(2020, 1, 1)
        )
    )
    # 60 % of 8.000 is not taxed: the general base grows by 3.200.
    assert con.base_imponible_general - sin.base_imponible_general == D("3200.00")
    assert any(line.concepto.startswith("Reducción por arrendamiento de vivienda (60 %") for line in con.lineas)
    # The same figure given as already reduced yields the same tax.
    ya = calcular_irpf(IRPFInput(**base, rendimiento_alquiler_neto=D(3200)))
    assert ya.cuota_liquida == con.cuota_liquida


def test_no_date_means_no_reduction_and_a_warning() -> None:
    r = calcular_irpf(IRPFInput(ejercicio=2026, rendimiento_alquiler_vivienda_sin_reducir=D(8000)))
    assert r.base_imponible_general == D("8000.00")
    assert any("Falta la fecha del contrato" in aviso for aviso in r.avisos)


def test_negative_income_is_not_reduced() -> None:
    r = calcular_irpf(
        IRPFInput(
            ejercicio=2026,
            rendimiento_trabajo_bruto=D(30000),
            rendimiento_alquiler_vivienda_sin_reducir=D(-1000),
            arrendamiento_fecha_contrato=date(2020, 1, 1),
        )
    )
    assert not any(line.concepto.startswith("Reducción por arrendamiento") for line in r.lineas)
    assert any("solo se aplica al positivo" in aviso for aviso in r.avisos)


def test_profile_facts_feed_the_calculator() -> None:
    inp = build_input(
        ejercicio=2026,
        facts={
            "rendimiento_alquiler_vivienda_sin_reducir": 6000.0,
            "arrendamiento_fecha_contrato": "2024-09-01",
            "arrendamiento_zona_tensionada": True,
            "arrendamiento_variacion_renta": -7.5,
        },
    )
    assert inp.arrendamiento_fecha_contrato == date(2024, 9, 1)
    r = calcular_irpf(inp)
    assert r.base_imponible_general == D("600.00")  # 90 % reduction
