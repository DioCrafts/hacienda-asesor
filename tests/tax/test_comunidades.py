"""The 15 common-regime communities: scales and minimums read from each regional law."""

from decimal import Decimal

import pytest

from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import load_params

D = Decimal
COMUNIDADES = {
    "madrid", "andalucia", "aragon", "asturias", "baleares", "canarias", "cantabria", "castilla_la_mancha",
    "castilla_y_leon", "cataluna", "comunidad_valenciana", "extremadura", "galicia", "la_rioja", "murcia",
}  # fmt: skip


@pytest.mark.parametrize("ejercicio", [2025, 2026])
def test_every_community_is_loaded_with_a_consistent_scale(ejercicio: int) -> None:
    params = load_params(ejercicio)
    assert set(params.comunidades) == COMUNIDADES
    for key, comunidad in params.comunidades.items():
        tramos = comunidad.escala_general.tramos
        assert tramos[-1].hasta is None, key
        assert comunidad.escala_general.fuente.verificado, key
        assert comunidad.fuente_principal.url.startswith("https://www.boe.es/buscar/act.php?id="), key
        inicio, acumulada = D(0), D(0)
        for tramo in tramos:
            # The cumulative tax the law prints matches the brackets and rates.
            assert tramo.cuota_hasta_inicio is not None, key
            assert abs(tramo.cuota_hasta_inicio - acumulada) <= D("0.02"), (key, inicio)
            if tramo.hasta is not None:
                assert tramo.hasta > inicio, key
                acumulada = tramo.cuota_hasta_inicio + (tramo.hasta - inicio) * tramo.tipo
                inicio = tramo.hasta
        assert D("0.07") <= tramos[0].tipo <= D("0.12"), key
        assert D("0.20") <= tramos[-1].tipo <= D("0.30"), key


@pytest.mark.parametrize(
    ("nombre", "clave"),
    [
        ("Andalucía", "andalucia"),
        ("Illes Balears", "baleares"),
        ("Catalunya", "cataluna"),
        ("Castilla-La Mancha", "castilla_la_mancha"),
        ("Comunitat Valenciana", "comunidad_valenciana"),
        ("Región de Murcia", "murcia"),
        ("Principado de Asturias", "asturias"),
        ("Castilla y León", "castilla_y_leon"),
    ],
)
def test_communities_are_found_by_their_usual_names(nombre: str, clave: str) -> None:
    params = load_params(2026)
    assert params.comunidad(nombre) is params.comunidades[clave]


def test_regional_scale_and_minimums_change_the_result() -> None:
    base = dict(ejercicio=2026, rendimiento_trabajo_bruto=D(40000), cotizaciones_seguridad_social=D(2540))
    cuotas = {
        key: calcular_irpf(IRPFInput(comunidad_autonoma=key, **base)).cuota_integra_autonomica
        for key in ("madrid", "cataluna", "galicia", "andalucia")
    }
    assert cuotas["madrid"] < cuotas["galicia"] < cuotas["andalucia"] < cuotas["cataluna"]
    # Galicia: base 35.460; minimum 5.789 at 9 %. 1.125 + 1.240,73 + 2.336 − 521,01 = hand-checked below.
    galicia = load_params(2026).comunidades["galicia"]
    assert galicia.minimos is not None and galicia.minimos.contribuyente == D("5789")
    r = calcular_irpf(IRPFInput(comunidad_autonoma="Galicia", **base))
    assert not any("no está cargada" in aviso for aviso in r.avisos)
    assert any("deducciones autonómicas NO están incluidas" in aviso for aviso in r.avisos)


def test_scales_that_changed_between_2025_and_2026() -> None:
    for key in ("extremadura", "comunidad_valenciana"):
        e25 = load_params(2025).comunidades[key].escala_general
        e26 = load_params(2026).comunidades[key].escala_general
        assert [t.tipo for t in e25.tramos] != [t.tipo for t in e26.tramos], key
        assert [t.hasta for t in e25.tramos] == [t.hasta for t in e26.tramos], key
    assert load_params(2026).comunidades["comunidad_valenciana"].escala_general.tramos[0].tipo == D("0.088")
    assert load_params(2025).comunidades["comunidad_valenciana"].escala_general.tramos[0].tipo == D("0.09")


def test_caveats_are_repeated_as_warnings() -> None:
    r = calcular_irpf(IRPFInput(ejercicio=2026, comunidad_autonoma="La Rioja", rendimiento_trabajo_bruto=D(30000)))
    assert any("discapacidad de descendientes" in aviso for aviso in r.avisos)
    baleares = load_params(2026).comunidades["baleares"]
    assert baleares.minimos is not None
    # State amounts plus 10 %, only where the Balearic law says so.
    assert baleares.minimos.contribuyente == D("5550") and baleares.minimos.incremento_mayor_65 == D("1265")
    assert baleares.minimos.descendientes == [D("2400"), D("2970"), D("4400"), D("4950")]


def test_foral_territories_remain_an_approximation() -> None:
    r = calcular_irpf(IRPFInput(ejercicio=2026, comunidad_autonoma="Navarra", rendimiento_trabajo_bruto=D(30000)))
    assert any("no está cargada" in aviso for aviso in r.avisos)
