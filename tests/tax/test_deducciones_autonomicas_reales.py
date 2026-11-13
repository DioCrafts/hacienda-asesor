"""Regional deductions as loaded from each community's law (figures quoted in the articles)."""

from decimal import Decimal

import pytest

from hacienda_gpt.tax.beneficios import evaluar_beneficios
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import load_params

D = Decimal
PREFIX = "Deducción autonómica: "


def _deducciones(comunidad: str, ejercicio: int = 2026, **kw: object) -> dict[str, Decimal]:
    base = dict(rendimiento_trabajo_bruto=D(26000), cotizaciones_seguridad_social=D(1650))
    r = calcular_irpf(IRPFInput(ejercicio=ejercicio, comunidad_autonoma=comunidad, **{**base, **kw}))
    return {line.concepto.removeprefix(PREFIX): -line.importe for line in r.lineas if line.concepto.startswith(PREFIX)}


def _unica(deducciones: dict[str, Decimal], fragmento: str) -> Decimal:
    (importe,) = [v for k, v in deducciones.items() if fragmento.lower() in k.lower()]
    return importe


def test_every_other_community_has_computable_deductions() -> None:
    comunidades = load_params(2026).comunidades
    assert [k for k, c in comunidades.items() if k != "madrid" and not c.deducciones_calculables] == []
    # Madrid's extra deductions are data-driven too, on top of the six hand-coded ones.
    assert comunidades["madrid"].deducciones_calculables
    assert sum(len(c.deducciones_calculables) for c in comunidades.values()) >= 125


def test_cataluna_rent_art_612_3() -> None:
    # 10 % up to 500 € for those aged 35 or under; 1.000 € in a large family.
    joven = _deducciones("Cataluña", edad=32, alquiler_vivienda_habitual=D(8400))
    assert _unica(joven, "alquiler") == D("500.00")
    numerosa = _deducciones("Cataluña", edad=45, familia_numerosa=True, alquiler_vivienda_habitual=D(12000))
    assert _unica(numerosa, "alquiler") == D("1000.00")
    assert _deducciones("Cataluña", edad=45, alquiler_vivienda_habitual=D(8400)) == {}


def test_valencia_rent_art_4_uno_n() -> None:
    # 20 %/800 in general; 25 %/950 for those aged 35 or under. The cap is prorated by the days of the lease.
    anual = dict(dias_alquiler_vivienda_habitual=365)
    assert _unica(
        _deducciones("Comunidad Valenciana", edad=50, alquiler_vivienda_habitual=D(8400), **anual), "arrendamiento"
    ) == D(800)
    assert _unica(
        _deducciones("Comunidad Valenciana", edad=32, alquiler_vivienda_habitual=D(8400), **anual), "arrendamiento"
    ) == D(950)
    # Above 30.000 € of taxable income it is lost.
    rica = _deducciones(
        "Comunidad Valenciana", edad=32, alquiler_vivienda_habitual=D(8400), rendimiento_trabajo_bruto=D(45000), **anual
    )
    assert rica == {}


def test_castilla_y_leon_birth_by_child_order_art_4() -> None:
    # 1.010 € first child, 1.475 € second, 2.351 € third; halved between parents filing separately.
    primero = _deducciones("Castilla y León", hijos_nacidos_en_el_ejercicio=1, num_descendientes=1)
    assert _unica(primero, "nacimiento") == D("1010.00")
    segundo = _deducciones(
        "Castilla y León", hijos_nacidos_en_el_ejercicio=1, num_descendientes=2, descendientes_compartidos=True
    )
    assert _unica(segundo, "nacimiento") == D("737.50")
    rural = _deducciones(
        "Castilla y León", hijos_nacidos_en_el_ejercicio=1, num_descendientes=3, habitantes_municipio=3000
    )
    assert _unica(rural, "nacimiento") == D("3300.00")


def test_andalucia_birth_and_large_family_exclude_each_other() -> None:
    # Art. 11: 200 € per child born; art. 14: 200 € for large families; the two are incompatible.
    r = calcular_irpf(
        IRPFInput(
            ejercicio=2026,
            comunidad_autonoma="Andalucía",
            rendimiento_trabajo_bruto=D(22000),
            cotizaciones_seguridad_social=D(1400),
            hijos_nacidos_en_el_ejercicio=1,
            num_descendientes=3,
            familia_numerosa=True,
        )
    )
    aplicadas = [line.concepto for line in r.lineas if line.concepto.startswith(PREFIX)]
    assert len(aplicadas) == 1 and "nacimiento" in aplicadas[0].lower()
    assert any("incompatible" in aviso for aviso in r.avisos)


def test_unchecked_requirements_are_always_repeated() -> None:
    r = calcular_irpf(
        IRPFInput(
            ejercicio=2026,
            comunidad_autonoma="Murcia",
            edad=30,
            rendimiento_trabajo_bruto=D(24000),
            cotizaciones_seguridad_social=D(1500),
            alquiler_vivienda_habitual=D(7200),
        )
    )
    assert any(line.concepto.startswith(PREFIX) for line in r.lineas)
    assert any("sin comprobar estos requisitos" in aviso for aviso in r.avisos)


@pytest.mark.parametrize("comunidad", ["Galicia", "Extremadura", "Canarias", "Baleares"])
def test_catalogue_lists_them_with_what_is_missing(comunidad: str) -> None:
    inp = IRPFInput(ejercicio=2026, comunidad_autonoma=comunidad, rendimiento_trabajo_bruto=D(20000))
    beneficios = [
        b for b in evaluar_beneficios(inp, conocidos={"comunidad_autonoma"}) if b.id.startswith("autonomica_")
    ]
    assert beneficios and all(b.estado == "por_confirmar" for b in beneficios)
    assert any(b.datos_que_faltan for b in beneficios)


def test_2025_keeps_only_what_applied_then() -> None:
    total_2025 = sum(len(c.deducciones_calculables) for c in load_params(2025).comunidades.values())
    total_2026 = sum(len(c.deducciones_calculables) for c in load_params(2026).comunidades.values())
    assert 0 < total_2025 < total_2026
