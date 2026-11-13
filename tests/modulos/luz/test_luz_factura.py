"""Bills computed by the module and checked by hand, line by line (every expected figure is worked out
in the comment above it from the BOE figures and the offer's prices)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from hacienda_gpt.modulos.luz.consumo import Lectura, compactar, importar_csv
from hacienda_gpt.modulos.luz.esios import interpretar
from hacienda_gpt.modulos.luz.factura import Contrato, FacturaError, calcular_factura
from hacienda_gpt.modulos.luz.ofertas import Oferta
from hacienda_gpt.modulos.luz.periodos import es_dia_valle, hora_local, horas_del_dia

D = Decimal

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "luz"
CONTRATO = Contrato(D("4.6"), D("4.6"))
FIJA = Oferta(
    nombre="Fija 3 periodos",
    energia_eur_kwh={"p1": "0.15", "p2": "0.12", "p3": "0.08"},
    potencia_eur_kw_dia={"p1": "0.089", "p2": "0.012"},
)


def _lineas(f) -> dict[str, D]:  # type: ignore[no-untyped-def]
    return {linea.concepto: linea.importe for linea in f.lineas}


def test_hand_checked_bill_of_a_fixed_price_offer() -> None:
    f = calcular_factura(
        FIJA, CONTRATO, date(2026, 9, 15), date(2026, 10, 14), totales={"p1": D(80), "p2": D(70), "p3": D(120)}
    )
    assert f.dias == 30
    assert _lineas(f) == {
        "Potencia P1 (punta-llano)": D("12.28"),  # 4,6 × 30 × 0,089 = 12,282
        "Potencia P2 (valle)": D("1.66"),  # 4,6 × 30 × 0,012 = 1,656
        "Energía P1": D("12.00"),  # 80 × 0,15
        "Energía P2": D("8.40"),  # 70 × 0,12
        "Energía P3": D("9.60"),  # 120 × 0,08
        "Financiación del bono social": D("0.74"),  # 9,011295 × 30 / 365 = 0,74065 (Orden TED/634/2026)
        "Impuesto sobre la electricidad": D("2.28"),  # 44,68 × 5,11269632 % = 2,28435
        "Alquiler del contador": D("0.80"),  # 0,81 × 12 × 30 / 365 = 0,79890
        "IVA 21 %": D("10.03"),  # (44,68 + 2,28 + 0,80) × 21 % = 10,0296
    }
    assert f.total == D("57.79")
    assert any("bono social" in a for a in f.avisos)
    assert any("BOE-A-2026-13759" in c for c in f.citas)


def test_hand_checked_bill_split_by_the_temporary_tax_cut_of_march_2026() -> None:
    f = calcular_factura(
        FIJA, CONTRATO, date(2026, 3, 10), date(2026, 4, 9), totales={"p1": D(80), "p2": D(70), "p3": D(120)}
    )
    lineas = _lineas(f)
    # 12 days at general rates (10-21 March), 19 days with RDL 7/2026 (from 22 March).
    assert lineas["Potencia P1 (punta-llano) [del 10-03 al 21-03]"] == D("4.91")  # 4,6 × 12 × 0,089 = 4,9128
    assert lineas["Energía P1 [del 10-03 al 21-03]"] == D("4.65")  # 80 × 12/31 = 30,968 kWh × 0,15 = 4,6452
    assert lineas["Energía P1 [del 22-03 al 09-04]"] == D("7.35")  # 49,032 kWh × 0,15 = 7,3548
    assert lineas["Financiación del bono social [del 10-03 al 21-03]"] == D("0.23")  # 6,979247 × 12 / 365
    assert lineas["Impuesto sobre la electricidad [del 10-03 al 21-03]"] == D("0.89")  # 17,42 × 5,11269632 %
    # 0,5 % × 27,57 € = 0,138 is below the minimum of 1 €/MWh × 165,483 kWh = 0,165.
    assert lineas["Impuesto sobre la electricidad [del 22-03 al 09-04]"] == D("0.17")
    assert lineas["IVA 21 %"] == D("3.91")  # (17,42 + 0,89 + 0,32) × 21 % = 3,9123
    assert lineas["IVA 10 %"] == D("2.83")  # (27,57 + 0,17 + 0,51) × 10 % = 2,825
    assert f.total == D("53.62")
    assert any("reparten" in a for a in f.avisos)


def test_hand_checked_2025_bill_with_regulated_tolls_added_discount_and_three_phase_meter() -> None:
    oferta = Oferta(
        nombre="Margen sin peajes",
        energia_eur_kwh="0.09",
        potencia_eur_kw_dia={"p1": "0.010", "p2": "0.010"},
        incluye_peajes_cargos=False,
        descuento_energia_pct=D(10),
        bono_social_eur_dia=D("0.012742"),
        contador="trifasico",
    )
    f = calcular_factura(
        oferta,
        Contrato(D("5.75"), D("3.45")),
        date(2025, 2, 1),
        date(2025, 2, 28),
        totales={"p1": D(100), "p2": D(80), "p3": D(150)},
    )
    assert _lineas(f) == {
        # (22,958932 + 3,971618) × 5,75 × 28 / 365 = 11,878955
        "Potencia P1 (punta-llano): peajes y cargos": D("11.88"),
        # (0,442165 + 0,255423) × 3,45 × 28 / 365 = 0,184622
        "Potencia P2 (valle): peajes y cargos": D("0.18"),
        "Potencia (margen de la comercializadora) P1 (punta-llano)": D("1.61"),  # 5,75 × 28 × 0,010
        "Potencia (margen de la comercializadora) P2 (valle)": D("0.97"),  # 3,45 × 28 × 0,010 = 0,966
        "Energía P1: peajes y cargos": D("9.25"),  # 100 × (0,034234 + 0,058305) = 9,2539
        "Energía P2: peajes y cargos": D("2.26"),  # 80 × (0,016540 + 0,011661) = 2,25608
        "Energía P3: peajes y cargos": D("0.45"),  # 150 × (0,000079 + 0,002915) = 0,4491
        "Energía P1 (precio de la comercializadora)": D("9.00"),
        "Energía P2 (precio de la comercializadora)": D("7.20"),
        "Energía P3 (precio de la comercializadora)": D("13.50"),
        "Financiación del bono social": D("0.36"),  # 0,012742 × 28 = 0,356776
        "Descuento 10,00 % sobre energía": D("-2.97"),  # 10 % of 29,70 (own energy price only)
        "Impuesto sobre la electricidad": D("2.75"),  # 53,69 × 5,11269632 % = 2,7450067
        "Alquiler del contador": D("1.25"),  # 1,36 × 12 × 28 / 365 = 1,251945
        "IVA 21 %": D("12.11"),  # (53,69 + 2,75 + 1,25) × 21 % = 12,1149
    }
    assert f.total == D("69.80")


def test_hand_checked_pvpc_bill_over_the_25_hour_day() -> None:
    precios = interpretar((FIXTURES / "esios_1001_2026-10-24_25.json").read_text(encoding="utf-8"))
    assert len(precios) == 49 and set(precios.values()) == {D("0.15")}  # the Canarias value is ignored
    lecturas = [
        Lectura(d, i, D("0.200")) for d in (date(2026, 10, 24), date(2026, 10, 25)) for i in range(horas_del_dia(d))
    ]
    f = calcular_factura(
        Oferta(nombre="PVPC", tipo="pvpc"),
        CONTRATO,
        date(2026, 10, 24),
        date(2026, 10, 25),
        lecturas=lecturas,
        precios_pvpc=precios,
    )
    assert f.kwh == {"p1": 0, "p2": 0, "p3": D("9.800")}  # weekend: 49 h × 0,2 kWh, all valley
    assert _lineas(f) == {
        # 4,6 × (23,324952 + 4,379461) × 2 / 365 = 0,698303
        "Potencia P1 (punta-llano): peajes y cargos": D("0.70"),
        # 4,6 × (0,443770 + 0,281653) × 2 / 365 = 0,018285
        "Potencia P2 (valle): peajes y cargos": D("0.02"),
        "Potencia P1: margen de comercialización fijo (PVPC)": D("0.08"),  # 4,6 × 3,113 × 2 / 365 = 0,078465
        "Energía consumida PVPC (precio hora a hora)": D("1.47"),  # 9,8 kWh × 150 €/MWh
        "Financiación del bono social": D("0.05"),  # 9,011295 × 2 / 365 = 0,049377
        "Impuesto sobre la electricidad": D("0.12"),  # 2,32 × 5,11269632 % = 0,118615
        "Alquiler del contador": D("0.05"),  # 0,81 × 12 × 2 / 365 = 0,053260
        "IVA 21 %": D("0.52"),  # (2,32 + 0,12 + 0,05) × 21 % = 0,5229
    }
    assert f.total == D("3.01")


def test_hourly_data_gives_the_same_bill_as_its_period_totals(csv_anio, anio) -> None:  # type: ignore[no-untyped-def]
    desde, hasta = date(2026, 9, 15), date(2026, 10, 14)
    imp = importar_csv(csv_anio)
    # Independent classification of the synthetic hours, written out from Circular 3/2020 art. 7.3.
    esperado = {"p1": D(0), "p2": D(0), "p3": D(0)}
    for dia, _, hora, kwh in anio:
        if not desde <= dia <= hasta:
            continue
        if dia.weekday() >= 5 or dia == date(2026, 10, 12):
            esperado["p3"] += kwh
        elif 10 <= hora < 14 or 18 <= hora < 22:
            esperado["p1"] += kwh
        elif 8 <= hora < 10 or 14 <= hora < 18 or hora >= 22:
            esperado["p2"] += kwh
        else:
            esperado["p3"] += kwh
    por_horas = calcular_factura(FIJA, CONTRATO, desde, hasta, lecturas=imp.lecturas)
    por_dias = calcular_factura(FIJA, CONTRATO, desde, hasta, dias_kwh=compactar(imp.lecturas).dias)
    por_totales = calcular_factura(FIJA, CONTRATO, desde, hasta, totales=esperado)
    assert por_horas.kwh == por_dias.kwh == esperado
    assert por_horas.total == por_dias.total == por_totales.total


def test_conditional_months_use_general_rates_unless_the_user_confirms() -> None:
    totales = {"p1": D(60), "p2": D(60), "p3": D(100)}
    auto = calcular_factura(FIJA, CONTRATO, date(2026, 8, 1), date(2026, 8, 31), totales=totales)
    si = calcular_factura(FIJA, CONTRATO, date(2026, 8, 1), date(2026, 8, 31), totales=totales, tipos_reducidos="si")
    assert "IVA 21 %" in _lineas(auto) and "IVA 10 %" in _lineas(si)
    assert any("condicional" in a for a in auto.avisos)
    assert si.total < auto.total
    # Above 10 kW the VAT cut does not apply, the electricity-tax cut does.
    grande = calcular_factura(FIJA, Contrato(D("11"), D("11")), date(2026, 4, 1), date(2026, 4, 30), totales=totales)
    assert "IVA 21 %" in _lineas(grande) and any("10 kW" in a for a in grande.avisos)


def test_bills_that_cannot_be_computed_explain_why() -> None:
    totales = {"p1": D(1), "p2": D(1), "p3": D(1)}
    with pytest.raises(FacturaError, match="IGIC"):
        calcular_factura(FIJA, Contrato(D(4), D(4), "canarias"), date(2026, 1, 1), date(2026, 1, 31), totales=totales)
    with pytest.raises(FacturaError, match="15 kW"):
        calcular_factura(FIJA, Contrato(D(20), D(20)), date(2026, 1, 1), date(2026, 1, 31), totales=totales)
    with pytest.raises(FacturaError, match="PVPC"):
        calcular_factura(
            Oferta(nombre="P", tipo="pvpc"), CONTRATO, date(2026, 1, 1), date(2026, 1, 31), totales=totales
        )
    with pytest.raises(FacturaError, match="Falta el consumo"):
        calcular_factura(FIJA, CONTRATO, date(2026, 1, 1), date(2026, 1, 31))


def test_offer_validation_messages() -> None:
    from hacienda_gpt.modulos.luz.ofertas import validar_oferta, validar_ofertas

    with pytest.raises(ValueError, match="potencia"):
        validar_oferta({"nombre": "X", "energia_eur_kwh": 0.1})
    with pytest.raises(ValueError, match="PVPC"):
        validar_oferta({"nombre": "X", "tipo": "pvpc", "energia_eur_kwh": 0.1})
    with pytest.raises(ValueError, match="mismo nombre"):
        validar_ofertas([{"nombre": "A", "tipo": "pvpc"}, {"nombre": "A", "tipo": "pvpc"}])
    canonica = validar_oferta(
        {"nombre": "Única", "energia_eur_kwh": "0.1", "potencia_eur_kw_dia": {"p1": 0.1, "p2": 0.1}}
    )
    assert canonica["energia_eur_kwh"] == {"p1": "0.1", "p2": "0.1", "p3": "0.1"}
    assert json.loads(json.dumps(canonica)) == canonica


def test_valley_hours_classification_matches_local_clock(anio) -> None:  # type: ignore[no-untyped-def]
    # The synthetic series stores the local start hour; the module must compute the same.
    for dia, indice, hora, _ in anio[:500]:
        assert hora_local(dia, indice) == hora
    assert es_dia_valle(date(2026, 3, 29))
