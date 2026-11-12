"""Data-driven regional deductions: the generic routine, on synthetic deductions."""

from decimal import Decimal

import pytest

from hacienda_gpt.tax.autonomicas import Bases, aplicar, evaluar
from hacienda_gpt.tax.irpf import IRPFInput
from hacienda_gpt.tax.params import DeduccionCalculable

D = Decimal


def _bases(total: str = "20000", resto: str = "0") -> Bases:
    b = D(total)
    return Bases(b, b, b, b, b - D(5550), D(resto))


def _inp(**kw: object) -> IRPFInput:
    return IRPFInput(ejercicio=2026, **kw)


def _fija(**kw: object) -> DeduccionCalculable:
    base = dict(articulo="art. 1", titulo="Nacimiento", resumen="r", tipo="fija", importe="200",
                unidad="hijos_nacidos_en_el_ejercicio", en_vigor_desde="2026-01-01")  # fmt: skip
    return DeduccionCalculable(**{**base, **kw})


def _pct(**kw: object) -> DeduccionCalculable:
    base = dict(articulo="art. 2", titulo="Alquiler", resumen="r", tipo="porcentaje", porcentaje="0.15",
                base="alquiler_vivienda_habitual", maximo="600", en_vigor_desde="2026-01-01")  # fmt: skip
    return DeduccionCalculable(**{**base, **kw})


def test_fixed_amount_per_unit_and_nothing_when_not_triggered() -> None:
    d = _fija()
    assert evaluar(d, _inp(), _bases(), conjunta=False) is None
    r = evaluar(d, _inp(hijos_nacidos_en_el_ejercicio=2), _bases(), conjunta=False)
    assert r is not None and r.importe == D(400) and r.motivo is None


def test_percentage_with_caps() -> None:
    r = evaluar(_pct(), _inp(alquiler_vivienda_habitual=D(3000)), _bases(), conjunta=False)
    assert r is not None and r.importe == D(450)
    r = evaluar(_pct(), _inp(alquiler_vivienda_habitual=D(9000)), _bases(), conjunta=False)
    assert r is not None and r.importe == D(600)
    por_hijo = _pct(base="gastos_guarderia", maximo=None, maximo_por_unidad="100", unidad="num_descendientes_menores_3")
    r = evaluar(por_hijo, _inp(gastos_guarderia=D(5000), num_descendientes_menores_3=2), _bases(), conjunta=False)
    assert r is not None and r.importe == D(200)


def test_income_ceilings_strictness_and_family_unit() -> None:
    d = _fija(limite_individual="20000", limite_conjunta="30000")
    inp = _inp(hijos_nacidos_en_el_ejercicio=1)
    assert evaluar(d, inp, _bases("20000"), conjunta=False).motivo is None  # "no superior a"
    assert "supera el límite" in evaluar(d, inp, _bases("20000.01"), conjunta=False).motivo
    assert evaluar(d, inp, _bases("25000"), conjunta=True).motivo is None
    estricto = _fija(limite_individual="20000", limite_es_estricto=True)
    assert evaluar(estricto, inp, _bases("20000"), conjunta=False).motivo is not None
    familia = _fija(limite_unidad_familiar="30000")
    assert "unidad familiar" in evaluar(familia, inp, _bases("20000", resto="15000"), conjunta=False).motivo
    menos_minimos = _fija(base_limite="base_imponible_total_menos_minimos", limite_individual="15000")
    assert evaluar(menos_minimos, inp, _bases("20000"), conjunta=False).motivo is None  # 20.000 − 5.550


def test_age_disability_and_flags() -> None:
    joven = _pct(edad_max=35)
    assert "falta tu edad" in evaluar(joven, _inp(alquiler_vivienda_habitual=D(3000)), _bases(), conjunta=False).motivo
    assert evaluar(joven, _inp(alquiler_vivienda_habitual=D(3000), edad=35), _bases(), conjunta=False).motivo is None
    assert "35" in evaluar(joven, _inp(alquiler_vivienda_habitual=D(3000), edad=36), _bases(), conjunta=False).motivo
    # Triggered only by personal circumstances: silent when they are not met.
    mayor = _fija(unidad=None, edad_min=65, titulo="Edad")
    assert evaluar(mayor, _inp(edad=40), _bases(), conjunta=False) is None
    assert evaluar(mayor, _inp(edad=70), _bases(), conjunta=False).importe == D(200)
    numerosa = _fija(unidad=None, requiere=["familia_numerosa"], titulo="Familia numerosa")
    assert evaluar(numerosa, _inp(), _bases(), conjunta=False) is None
    assert evaluar(numerosa, _inp(familia_numerosa=True), _bases(), conjunta=False).importe == D(200)
    discapacidad = _fija(unidad=None, discapacidad_min=33, titulo="Discapacidad")
    assert evaluar(discapacidad, _inp(grado_discapacidad=33), _bases(), conjunta=False).importe == D(200)


def test_linear_phase_out_minimum_expense_and_sharing() -> None:
    d = _fija(reduccion_progresiva={"desde_individual": "20000", "hasta_individual": "24000"})
    inp = _inp(hijos_nacidos_en_el_ejercicio=1)
    assert evaluar(d, inp, _bases("20000"), conjunta=False).importe == D(200)
    assert evaluar(d, inp, _bases("23000"), conjunta=False).importe == D(50)
    assert "se extingue" in evaluar(d, inp, _bases("24000"), conjunta=False).motivo
    minimo = _pct(gasto_minimo_sobre_base="0.10")
    assert (
        "no supera el 10 %"
        in evaluar(minimo, _inp(alquiler_vivienda_habitual=D(2000)), _bases(), conjunta=False).motivo
    )
    compartida = _fija(reparto_descendientes=True)
    mitad = evaluar(
        compartida, _inp(hijos_nacidos_en_el_ejercicio=1, descendientes_compartidos=True), _bases(), conjunta=False
    )
    assert mitad.importe == D(100)


def test_apply_writes_lines_and_repeats_unchecked_requirements() -> None:
    lineas, avisos = [], []
    total = aplicar(
        [_fija(requisitos_no_comprobados=["Residir en la comunidad todo el año."]), _pct(edad_max=30)],
        _inp(hijos_nacidos_en_el_ejercicio=1, alquiler_vivienda_habitual=D(3000), edad=40),
        _bases(),
        conjunta=False,
        norma="Ley X",
        linea=lambda c, i, f: lineas.append((c, i, f)),
        aviso=avisos.append,
    )
    assert total == D(200)
    assert lineas == [("Deducción autonómica: Nacimiento", D("-200.00"), "art. 1 Ley X")]
    assert any("sin comprobar" in a and "Residir en la comunidad" in a for a in avisos)
    assert any("«Alquiler» no aplicada" in a for a in avisos)


def test_incoherent_definitions_are_rejected() -> None:
    with pytest.raises(ValueError, match="condición comprobable"):
        _fija(unidad=None)
    with pytest.raises(ValueError, match="unidad desconocida"):
        _fija(unidad="mascotas")
    with pytest.raises(ValueError, match="porcentual"):
        _pct(base="caprichos")
    with pytest.raises(ValueError, match="requisitos desconocidos"):
        _fija(requiere=["es_majo"])
