"""2.0TD periods, distributor CSV parsing and the compact consumption stored in the case."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

import pytest

from hacienda_gpt.modulos.luz.consumo import (
    DIAS_HORARIOS,
    ConsumoGuardado,
    FormatoNoReconocidoError,
    compactar,
    importar_csv,
    validar_consumo,
)
from hacienda_gpt.modulos.luz.datos import DatoNoDisponibleError
from hacienda_gpt.modulos.luz.periodos import (
    es_dia_valle,
    horas_del_dia,
    periodo_energia,
    periodo_potencia,
)

D = Decimal

# --- periods ---------------------------------------------------------------------------------------


def test_working_day_periods_in_the_peninsula() -> None:
    martes = date(2026, 11, 17)
    assert [periodo_energia(martes, h) for h in range(24)] == (
        ["p3"] * 8 + ["p2"] * 2 + ["p1"] * 4 + ["p2"] * 4 + ["p1"] * 4 + ["p2"] * 2
    )
    assert periodo_potencia(martes, 7) == "p2" and periodo_potencia(martes, 8) == "p1"


def test_ceuta_and_melilla_are_shifted_one_hour() -> None:
    martes = date(2026, 11, 17)
    assert periodo_energia(martes, 10, "ceuta") == "p2"
    assert periodo_energia(martes, 11, "melilla") == "p1"
    assert periodo_energia(martes, 23, "ceuta") == "p2"


def test_weekends_6_january_and_fixed_national_holidays_are_valley() -> None:
    assert es_dia_valle(date(2026, 11, 14))  # Saturday
    assert es_dia_valle(date(2026, 1, 6))  # Epiphany, named by the Circular
    assert es_dia_valle(date(2026, 10, 12))  # Monday, Fiesta Nacional
    assert es_dia_valle(date(2025, 12, 8))
    assert not es_dia_valle(date(2026, 4, 3))  # Good Friday: no fixed date
    assert not es_dia_valle(date(2026, 4, 2))  # Maundy Thursday: substitutable
    assert not es_dia_valle(date(2026, 12, 7))  # Monday after a Sunday holiday: substitutable
    assert all(periodo_energia(date(2026, 12, 8), h) == "p3" for h in range(24))
    with pytest.raises(DatoNoDisponibleError, match="2028"):
        es_dia_valle(date(2028, 3, 1))


def test_days_when_clocks_change_have_23_and_25_hours() -> None:
    assert horas_del_dia(date(2026, 3, 29)) == 23
    assert horas_del_dia(date(2026, 10, 25)) == 25
    assert horas_del_dia(date(2026, 10, 25), "canarias") == 25
    assert horas_del_dia(date(2026, 10, 26)) == 24


# --- CSV ---------------------------------------------------------------------------------------------


def test_datadis_year_is_read_with_both_dst_days(anio, csv_anio, sint) -> None:  # type: ignore[no-untyped-def]
    imp = importar_csv(csv_anio)
    assert imp.formato == "datadis" and imp.cups == sint.CUPS
    assert len(imp.lecturas) == len(anio)
    assert imp.avisos == []
    total = sum((x.kwh for x in imp.lecturas), D(0))
    assert total == sum((kwh for *_, kwh in anio), D(0))
    assert D(2500) < total < D(3600)  # a realistic flat
    assert sum(1 for x in imp.lecturas if x.fecha == date(2026, 3, 29)) == 23
    assert sum(1 for x in imp.lecturas if x.fecha == date(2025, 10, 26)) == 25


def test_distributor_format_with_numeric_hours_and_dst(sint) -> None:  # type: ignore[no-untyped-def]
    filas = sint.serie(date(2026, 10, 24), date(2026, 10, 26))
    imp = importar_csv(sint.csv_distribuidora(filas))
    assert imp.formato == "distribuidora"
    assert [x.indice for x in imp.lecturas if x.fecha == date(2026, 10, 25)] == list(range(25))
    assert [x.kwh for x in imp.lecturas] == [kwh for *_, kwh in filas]


def test_start_of_hour_convention_comma_separator_and_estimated_hours() -> None:
    texto = "cups,fecha,hora,consumo (kWh),metodo\n" + "\n".join(
        f'ES0031405912345678JY,2026-10-25,{h:02d}:00,"0,{h + 10}",{"Estimada" if h == 5 else "Real"}'
        for h in [0, 1, 2, 2, *range(3, 24)]
    )
    imp = importar_csv(texto)
    assert [x.indice for x in imp.lecturas] == list(range(25))
    assert imp.lecturas[3].kwh == D("0.12")
    assert any("estimadas" in a for a in imp.avisos)


def test_quarter_hour_files_are_added_per_hour() -> None:
    lineas = ["CUPS;Fecha;Hora;AE_kWh;REAL/ESTIMADO"]
    for h in range(24):
        for m in (15, 30, 45, 60):
            hh, mm = (h + 1, 0) if m == 60 else (h, m)
            lineas.append(f"ES0031405912345678JY;17/11/2026;{hh:02d}:{mm:02d};0,05;R")
    imp = importar_csv("\n".join(lineas))
    assert len(imp.lecturas) == 24 and all(x.kwh == D("0.20") for x in imp.lecturas)
    assert any("cuartohorario" in a for a in imp.avisos)


def test_incomplete_days_and_exported_energy_are_reported() -> None:
    texto = "CUPS;Fecha;Hora;Consumo_kWh;Metodo_obtencion;Energia_vertida_kWh\n" + "\n".join(
        f"ES0031405912345678JY;2026/11/17;{h:02d}:00;0,2;Real;0,1" for h in range(1, 20)
    )
    imp = importar_csv(texto)
    assert any("19 horas" in a for a in imp.avisos)
    assert any("vertida" in a for a in imp.avisos)


@pytest.mark.parametrize(
    "texto",
    ["", "CUPS;Fecha;Hora\n", "Nombre;Apellidos\nAna;López\n", "CUPS;Fecha;Hora;Consumo\nES1;2026/13/45;01:00;0,1\n"],
)
def test_unreadable_files_give_a_spanish_message(texto: str) -> None:
    with pytest.raises(FormatoNoReconocidoError):
        importar_csv(texto)


# --- compact storage ---------------------------------------------------------------------------------


def test_compact_form_keeps_daily_periods_recent_hours_and_monthly_profile(csv_anio) -> None:  # type: ignore[no-untyped-def]
    imp = importar_csv(csv_anio)
    c = compactar(imp.lecturas, importado_el=date(2026, 11, 13))
    assert len(c.dias) == 396 and len(c.horario) == DIAS_HORARIOS
    assert len(c.horario[date(2026, 10, 25)]) == 25
    assert sum(sum(v) for v in c.dias.values()) == sum((x.kwh for x in imp.lecturas), D(0))
    assert sum(c.meses["2026-03"].perfil) == c.meses["2026-03"].kwh
    # The stored fact is small compared with the hourly year and survives the round trip.
    canonico = validar_consumo(c.a_json())
    assert len(json.dumps(canonico)) < 60_000 < len(csv_anio)
    assert ConsumoGuardado.model_validate(canonico).dias == c.dias
    # Periods of a weekday add up to the day.
    martes = c.dias[date(2026, 10, 13)]
    assert sum(martes) == sum(x.kwh for x in imp.lecturas if x.fecha == date(2026, 10, 13))
    assert martes[0] > 0 and martes[1] > 0
    sabado = c.dias[date(2026, 10, 17)]
    assert sabado[0] == sabado[1] == 0


def test_new_imports_merge_over_the_stored_days(sint) -> None:  # type: ignore[no-untyped-def]
    primero = compactar(importar_csv(sint.csv_datadis(sint.serie(date(2026, 9, 1), date(2026, 9, 30)))).lecturas)
    segundo = compactar(
        importar_csv(sint.csv_datadis(sint.serie(date(2026, 10, 1), date(2026, 10, 31)))).lecturas, previo=primero
    )
    assert segundo.rango() == (date(2026, 9, 1), date(2026, 10, 31))
    assert segundo.totales(date(2026, 9, 15), date(2026, 10, 14)) is not None


def test_invalid_stored_consumption_is_rejected_in_spanish() -> None:
    with pytest.raises(ValueError, match="tres consumos"):
        validar_consumo({"dias": {"2026-10-01": ["1", "2"]}})
    with pytest.raises(ValueError, match="objeto JSON"):
        validar_consumo([1, 2])
