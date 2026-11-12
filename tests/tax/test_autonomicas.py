"""Data-driven regional deductions: the generic routine, on synthetic deductions."""

from decimal import Decimal
import json
import os
from pathlib import Path

import pytest

from hacienda_gpt.tax.autonomicas import Bases, aplicar, evaluar
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import DeduccionCalculable, load_params

D = Decimal


def _bases(total: str = "20000", resto: str = "0", ahorro: str = "0") -> Bases:
    b = D(total)
    return Bases(
        base_imponible_total=b,
        base_imponible_general=b,
        base_liquidable_total=b,
        base_liquidable_general=b,
        base_imponible_total_menos_minimos=b - D(5550),
        base_liquidable_total_menos_minimos=b - D(5550),
        base_imponible_total_menos_minimo_descendientes=b,
        base_imponible_ahorro=D(ahorro),
        resto_unidad_familiar=D(resto),
    )


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


def test_alternative_conditions_any_one_is_enough() -> None:
    # Rent for the under-36s OR large families OR disability of 65 %.
    d = _pct(alternativas=[{"edad_max": 35}, {"requiere": ["familia_numerosa"]}, {"discapacidad_min": 65}])
    renta = dict(alquiler_vivienda_habitual=D(3000))
    assert evaluar(d, _inp(edad=30, **renta), _bases(), conjunta=False).importe == D(450)
    assert evaluar(d, _inp(edad=50, familia_numerosa=True, **renta), _bases(), conjunta=False).importe == D(450)
    assert evaluar(d, _inp(edad=50, grado_discapacidad=65, **renta), _bases(), conjunta=False).importe == D(450)
    ninguna = evaluar(d, _inp(edad=50, **renta), _bases(), conjunta=False)
    assert ninguna.importe == 0 and "ninguna de las condiciones alternativas" in ninguna.motivo
    # The deduction's own conditions still apply on top of the alternatives.
    con_tope = _pct(limite_individual="15000", alternativas=[{"edad_max": 35}])
    assert "supera el límite" in evaluar(con_tope, _inp(edad=30, **renta), _bases(), conjunta=False).motivo


def test_variants_change_the_figures_for_those_who_meet_them() -> None:
    d = _pct(
        variantes=[
            {"descripcion": "municipio rural", "cuando": {"requiere": ["municipio_rural_o_despoblacion"]},
             "porcentaje": "0.20", "maximo": "1000"},
            {"descripcion": "dos o más hijos", "cuando": {"unidades_min": {"num_descendientes": 2}}, "maximo": "800"},
        ]
    )  # fmt: skip
    renta = dict(alquiler_vivienda_habitual=D(9000))
    general = evaluar(d, _inp(**renta), _bases(), conjunta=False)
    assert general.importe == D(600) and general.variante is None
    rural = evaluar(
        d, _inp(municipio_rural_o_despoblacion=True, num_descendientes=2, **renta), _bases(), conjunta=False
    )
    assert rural.importe == D(1000) and rural.variante == "municipio rural"  # the first matching variant wins
    hijos = evaluar(d, _inp(num_descendientes=2, **renta), _bases(), conjunta=False)
    assert hijos.importe == D(800)  # 15 % kept, cap raised


def test_variants_by_income_tier_and_filing_mode() -> None:
    d = _fija(
        importe="300",
        variantes=[
            {"descripcion": "rentas bajas", "cuando": {"ingreso_max": "22000"}, "importe": "360"},
            {"descripcion": "individual", "cuando": {"tributacion": "individual"}, "importe": "150"},
        ],
    )
    inp = _inp(hijos_nacidos_en_el_ejercicio=1)
    assert evaluar(d, inp, _bases("20000"), conjunta=False).importe == D(360)
    assert evaluar(d, inp, _bases("30000"), conjunta=False).importe == D(150)
    assert evaluar(d, inp, _bases("30000"), conjunta=True).importe == D(300)


def test_amount_by_child_order() -> None:
    d = _fija(importe=None, importes_por_orden=["500", "1000", "1200"])
    # One newborn who is the taxpayer's second child.
    assert evaluar(
        d, _inp(hijos_nacidos_en_el_ejercicio=1, num_descendientes=2), _bases(), conjunta=False
    ).importe == D(1000)
    # Twins who are the third and fourth children: the last amount repeats.
    assert evaluar(
        d, _inp(hijos_nacidos_en_el_ejercicio=2, num_descendientes=4), _bases(), conjunta=False
    ).importe == D(2400)
    # Descendants not stated: the newborns are taken as the only children.
    assert evaluar(d, _inp(hijos_nacidos_en_el_ejercicio=2), _bases(), conjunta=False).importe == D(1500)
    with pytest.raises(ValueError, match="importes_por_orden exige"):
        _fija(importe=None, importes_por_orden=["1"], unidad=None, edad_min=65)
    with pytest.raises(ValueError, match="uno de los dos"):
        _fija(importes_por_orden=["1"])


def test_count_conditions_and_empty_definitions() -> None:
    dos_hijos = _fija(
        unidad=None, unidades_min={"num_descendientes": 2}, unidades_max={"num_descendientes": 2}, titulo="Dos hijos"
    )
    assert evaluar(dos_hijos, _inp(num_descendientes=2), _bases(), conjunta=False).importe == D(200)
    assert evaluar(dos_hijos, _inp(num_descendientes=3), _bases(), conjunta=False) is None
    with pytest.raises(ValueError, match="al menos una condición"):
        _pct(variantes=[{"descripcion": "x", "cuando": {}, "maximo": "1"}])
    with pytest.raises(ValueError, match="alternativa vacía"):
        _pct(alternativas=[{}])


def test_incompatible_deductions_and_disability_ranges() -> None:
    discapacidad = _fija(unidad=None, discapacidad_min=65, titulo="Discapacidad", articulo="art. 4")
    edad = _fija(unidad=None, edad_min=75, titulo="Mayores de 75", articulo="art. 6", incompatible_con=["art. 4"])
    avisos: list[str] = []
    total = aplicar([discapacidad, edad], _inp(edad=80, grado_discapacidad=65), _bases(), conjunta=False, norma="Ley X",
                    linea=lambda *a: None, aviso=avisos.append)  # fmt: skip
    assert total == D(200) and any("incompatible con art. 4" in a for a in avisos)
    solo_edad = aplicar([discapacidad, edad], _inp(edad=80), _bases(), conjunta=False, norma="Ley X",
                        linea=lambda *a: None, aviso=avisos.append)  # fmt: skip
    assert solo_edad == D(200)
    tramo = _fija(unidad=None, discapacidad_min=33, discapacidad_max=64, titulo="Discapacidad media")
    assert evaluar(tramo, _inp(grado_discapacidad=50), _bases(), conjunta=False).importe == D(200)
    assert evaluar(tramo, _inp(grado_discapacidad=70), _bases(), conjunta=False) is None


def test_savings_base_ceiling_and_phase_out_on_the_cap() -> None:
    d = _pct(base_ahorro_max="1800")
    renta = _inp(alquiler_vivienda_habitual=D(3000))
    assert evaluar(d, renta, _bases(ahorro="1800"), conjunta=False).importe == D(450)
    assert "base del ahorro" in evaluar(d, renta, _bases(ahorro="1800.01"), conjunta=False).motivo
    # The cap (600), not the computed amount, is halved midway through the band.
    tope = _pct(reduccion_progresiva={"desde_individual": "20000", "hasta_individual": "24000", "aplica_a": "maximo"})
    assert evaluar(tope, renta, _bases("22000"), conjunta=False).importe == D(300)  # min(450, 600 × 0.5)
    assert evaluar(tope, _inp(alquiler_vivienda_habitual=D(1000)), _bases("22000"), conjunta=False).importe == D(150)
    importe = _pct(reduccion_progresiva={"desde_individual": "20000", "hasta_individual": "24000"})
    assert evaluar(importe, renta, _bases("22000"), conjunta=False).importe == D(225)


# --------------------------------------------------------------------------- #
# Schema extensions (BRIEF_DED3): each on a synthetic deduction
# --------------------------------------------------------------------------- #


def _aplicar(deducciones: list[DeduccionCalculable], inp: IRPFInput, bases: (
    Bases | None
) = None, *,conjunta: bool = False) -> tuple[D, dict[str, D], list[str]]:  # fmt: skip
    lineas: dict[str, D] = {}
    avisos: list[str] = []
    total = aplicar(deducciones, inp, bases or _bases(), conjunta=conjunta, norma="Ley X",
                    linea=lambda c, i, f: lineas.__setitem__(f.removesuffix(" Ley X"), -i), aviso=avisos.append)  # fmt: skip
    return total, lineas, avisos


def _guarderia(**kw: object) -> DeduccionCalculable:
    base = dict(base="gastos_guarderia", porcentaje="0.15", maximo=None, maximo_por_unidad="250",
                unidad="num_descendientes_menores_3", titulo="Guardería")  # fmt: skip
    return _pct(**{**base, **kw})


def test_cap_per_child_uses_each_childs_own_expenses_when_broken_down() -> None:
    d = _guarderia()
    # Aggregate (today's behaviour): 15 % × 4.000 = 600 against 250 × 2 children.
    junto = _inp(gastos_guarderia=D(4000), num_descendientes_menores_3=2)
    assert evaluar(d, junto, _bases(), conjunta=False).importe == D(500)
    # Broken down: everything was spent on one child → min(600, 250) + 0.
    uno = _inp(descendientes=[{"edad": 1, "gastos": {"gastos_guarderia": 4000}}, {"edad": 2}])
    assert uno.gastos_guarderia == D(4000) and uno.num_descendientes_menores_3 == 2  # filled in from the detail
    assert evaluar(d, uno, _bases(), conjunta=False).importe == D(250)
    # Σ min(pct × expense_i, cap): min(450, 250) + min(150, 250).
    dos = _inp(descendientes=[{"edad": 1, "gastos": {"gastos_guarderia": 3000}},
                              {"edad": 2, "gastos": {"gastos_guarderia": 1000}}])  # fmt: skip
    assert evaluar(d, dos, _bases(), conjunta=False).importe == D(400)
    # A detail without expenses keeps the aggregate rule.
    sin = _inp(gastos_guarderia=D(4000), descendientes=[{"edad": 1}, {"edad": 2}])
    assert evaluar(d, sin, _bases(), conjunta=False).importe == D(500)
    # Totals and breakdowns must agree; counts too.
    with pytest.raises(ValueError, match="no coincide con el total"):
        _inp(gastos_guarderia=D(100), descendientes=[{"edad": 1, "gastos": {"gastos_guarderia": 4000}}])
    with pytest.raises(ValueError, match="no cuadra con el detalle"):
        _inp(num_descendientes=3, descendientes=[{"edad": 1}])
    with pytest.raises(ValueError, match="gastos desconocidos"):
        _inp(descendientes=[{"edad": 1, "gastos": {"caprichos": 1}}])
    with pytest.raises(ValueError, match="edad o la fecha"):
        _inp(descendientes=[{}])


def test_zero_units_with_an_expense_is_assumed_one_or_refused() -> None:
    gasto = _inp(gastos_guarderia=D(4000))
    total, _, avisos = _aplicar([_guarderia()], gasto)
    assert total == D(250) and any("se ha supuesto 1" in a for a in avisos)
    r = evaluar(_guarderia(si_faltan_unidades="denegar"), gasto, _bases(), conjunta=False)
    assert r.importe == 0 and "no consta ninguna unidad" in r.motivo
    con_hijo = _inp(gastos_guarderia=D(4000), num_descendientes_menores_3=1)
    assert evaluar(_guarderia(si_faltan_unidades="denegar"), con_hijo, _bases(), conjunta=False).importe == D(250)


def test_children_units_and_conditions_from_their_ages() -> None:
    # 100 € per child under 6; needs the ages, not just a count.
    d = _fija(importe="100", unidad=None, unidad_descendientes={"edad_max": 5}, titulo="Menores de 6")
    assert evaluar(d, _inp(), _bases(), conjunta=False) is None  # no children at all
    sin_edades = evaluar(d, _inp(num_descendientes=2), _bases(), conjunta=False)
    assert sin_edades.importe == 0 and sin_edades.falta_detalle
    _, lineas, avisos = _aplicar([d], _inp(num_descendientes=2))
    assert not lineas and any("no se han podido comprobar: «Menores de 6»" in a for a in avisos)
    hijos = _inp(descendientes=[{"edad": 2}, {"edad": 5}, {"edad": 6}])
    assert evaluar(d, hijos, _bases(), conjunta=False).importe == D(200)
    # Filters an old count answers exactly work without the detail.
    menores_3 = _fija(importe="100", unidad=None, unidad_descendientes={"edad_max": 2})
    assert evaluar(menores_3, _inp(num_descendientes_menores_3=2), _bases(), conjunta=False).importe == D(200)
    # As a condition: household employee, only with a child under 12 and both parents working.
    hogar = _pct(base="cotizaciones_empleada_hogar", porcentaje="0.20", maximo="400",
                 descendientes=[{"edad_max": 11}], requiere=["progenitores_trabajan"])  # fmt: skip
    cuotas = dict(cotizaciones_empleada_hogar=D(1500), progenitores_trabajan=True)
    assert evaluar(hogar, _inp(descendientes=[{"edad": 11}], **cuotas), _bases(), conjunta=False).importe == D(300)
    mayor = evaluar(hogar, _inp(descendientes=[{"edad": 12}], **cuotas), _bases(), conjunta=False)
    assert "al menos 1 descendiente(s) de hasta 11 años" in mayor.motivo
    falta = evaluar(hogar, _inp(num_descendientes=1, **cuotas), _bases(), conjunta=False)
    assert "faltan los datos de tus descendientes" in falta.motivo
    no_trabajan = evaluar(hogar, _inp(descendientes=[{"edad": 3}], cotizaciones_empleada_hogar=D(1500)),
                          _bases(), conjunta=False)  # fmt: skip
    assert "«progenitores trabajan»" in no_trabajan.motivo
    # School stage, disability by degree, international adoption, multiple birth.
    material = _fija(importe="110", unidad=None, unidad_descendientes={"etapas": ["primaria", "eso"]})
    cole = _inp(descendientes=[{"edad": 4, "etapa_educativa": "infantil_segundo_ciclo"},
                               {"edad": 8, "etapa_educativa": "primaria"}, {"edad": 13, "etapa_educativa": "eso"}])  # fmt: skip
    assert evaluar(material, cole, _bases(), conjunta=False).importe == D(220)
    discapacidad = _fija(importe="300", unidad=None, unidad_descendientes={"discapacidad_min": 65})
    grados = _inp(descendientes=[{"edad": 9, "grado_discapacidad": 40}, {"edad": 12, "grado_discapacidad": 70}])
    assert evaluar(discapacidad, grados, _bases(), conjunta=False).importe == D(300)
    internacional = _fija(importe="600", unidad=None,
                          unidad_descendientes={"anios_desde_llegada_max": 0, "adopcion_internacional": True})  # fmt: skip
    adopcion = _inp(descendientes=[{"edad": 4, "ejercicio_adopcion": 2026, "adopcion_internacional": True},
                                   {"edad": 0}])  # fmt: skip
    assert adopcion.hijos_nacidos_en_el_ejercicio == 2 and adopcion.hijos_adoptados_en_el_ejercicio == 1
    assert evaluar(internacional, adopcion, _bases(), conjunta=False).importe == D(600)
    multiple = _fija(importe="246", unidad=None,
                     unidad_descendientes={"anios_desde_llegada_max": 0, "parto_multiple": True})  # fmt: skip
    gemelos = _inp(descendientes=[{"edad": 0, "parto_multiple": True}, {"edad": 0, "parto_multiple": True},
                                  {"edad": 5}])  # fmt: skip
    assert evaluar(multiple, gemelos, _bases(), conjunta=False).importe == D(492)


def test_amount_by_order_from_the_ages_and_for_other_units() -> None:
    # Birth year and the two following, by the child's order among all the children.
    d = _fija(importe=None, importes_por_orden=["600", "750", "900"], unidad=None,
              unidad_descendientes={"anios_desde_llegada_max": 2})  # fmt: skip
    hijos = _inp(descendientes=[{"edad": 9}, {"edad": 2}, {"edad": 0}])  # second and third children
    assert evaluar(d, hijos, _bases(), conjunta=False).importe == D(1650)
    assert evaluar(d, _inp(hijos_nacidos_ultimos_3_ejercicios=1, num_descendientes=2), _bases(),
                   conjunta=False).motivo.startswith("depende de datos de cada descendiente")  # fmt: skip
    # Any other unit: first, second, third… unit (618,60 / 773,25 / 927,90 per fostered minor).
    acogidos = _fija(importe=None, importes_por_orden=["618.60", "773.25", "927.90"], unidad="menores_acogidos")
    assert evaluar(acogidos, _inp(menores_acogidos=4), _bases(), conjunta=False).importe == D("3247.65")
    # A fixed deduction may carry a total cap: 240 € per fostered minor, at most 1.200 €.
    tope = _fija(importe="240", unidad="menores_acogidos", maximo="1200")
    assert evaluar(tope, _inp(menores_acogidos=7), _bases(), conjunta=False).importe == D(1200)


def test_proration_of_the_cap_by_days_of_the_lease() -> None:
    d = _pct(maximo="500", prorrateo={"dato": "dias_alquiler_vivienda_habitual", "aplica_a": "maximo"})
    renta = dict(alquiler_vivienda_habitual=D(6000))
    falta = evaluar(d, _inp(**renta), _bases(), conjunta=False)
    assert falta.importe == 0 and "«dias alquiler vivienda habitual», que no consta" in falta.motivo
    assert evaluar(d, _inp(dias_alquiler_vivienda_habitual=365, **renta), _bases(), conjunta=False).importe == D(500)
    medio = evaluar(d, _inp(dias_alquiler_vivienda_habitual=146, **renta), _bases(), conjunta=False)
    assert medio.importe == D(200)  # 500 × 146/365
    # Below the prorated cap the percentage rules: 15 % × 600 = 90.
    assert evaluar(d, _inp(dias_alquiler_vivienda_habitual=146, alquiler_vivienda_habitual=D(600)), _bases(),
                   conjunta=False).importe == D(90)  # fmt: skip
    # The law may let us assume the whole year, with a warning.
    supuesto = _pct(maximo="500", prorrateo={"dato": "dias_alquiler_vivienda_habitual", "aplica_a": "maximo",
                                              "si_falta": "completo"})  # fmt: skip
    total, _, avisos = _aplicar([supuesto], _inp(**renta))
    assert total == D(500) and any("se ha supuesto todo el año" in a for a in avisos)
    # Prorating the amount itself.
    importe = _pct(maximo=None, prorrateo={"dato": "dias_alquiler_vivienda_habitual"})
    assert evaluar(importe, _inp(dias_alquiler_vivienda_habitual=73, **renta), _bases(), conjunta=False).importe == D(
        180
    )  # 900 × 73/365


def test_proration_by_steps_and_per_period() -> None:
    # Fostering: nothing up to 90 days, half from 91 to 182, the whole from 183.
    d = _fija(importe="300", unidad="menores_acogidos",
              prorrateo={"dato": "dias_acogimiento", "tramos": [{"desde": 91, "factor": "0.5"},
                                                                {"desde": 183, "factor": "1"}]})  # fmt: skip
    assert "no alcanza el mínimo" in evaluar(d, _inp(menores_acogidos=1, dias_acogimiento=90), _bases(),
                                              conjunta=False).motivo  # fmt: skip
    assert evaluar(d, _inp(menores_acogidos=1, dias_acogimiento=91), _bases(), conjunta=False).importe == D(150)
    assert evaluar(d, _inp(menores_acogidos=1, dias_acogimiento=182), _bases(), conjunta=False).importe == D(150)
    assert evaluar(d, _inp(menores_acogidos=2, dias_acogimiento=183), _bases(), conjunta=False).importe == D(600)
    assert "que no consta" in evaluar(d, _inp(menores_acogidos=1), _bases(), conjunta=False).motivo
    # Proportional to the days of the year (2026 has 365).
    dias = _fija(importe="365", unidad="menores_acogidos", prorrateo={"dato": "dias_acogimiento"})
    assert evaluar(dias, _inp(menores_acogidos=1, dias_acogimiento=100), _bases(), conjunta=False).importe == D(100)
    # An explicit denominator, and an amount granted once per day.
    sobre_360 = _fija(importe="360", unidad=None, edad_min=18, prorrateo={"dato": "dias_desempleo", "total": 360})
    assert evaluar(sobre_360, _inp(edad=30, dias_desempleo=90), _bases(), conjunta=False).importe == D(90)
    por_dia = _fija(importe="2", unidad=None, edad_min=18,
                    prorrateo={"dato": "dias_desempleo", "por_cada_periodo": True})  # fmt: skip
    assert evaluar(por_dia, _inp(edad=30, dias_desempleo=40), _bases(), conjunta=False).importe == D(80)
    # Days as an eligibility condition: unemployed for at least 183 days.
    paro = _pct(alternativas=[{"edad_max": 32}, {"unidades_min": {"dias_desempleo": 183}}])
    renta = dict(alquiler_vivienda_habitual=D(3000), edad=50)
    assert evaluar(paro, _inp(dias_desempleo=183, **renta), _bases(), conjunta=False).importe == D(450)
    assert evaluar(paro, _inp(dias_desempleo=182, **renta), _bases(), conjunta=False).motivo is not None


def test_nursery_in_the_year_the_child_turns_three() -> None:
    # (a) A lower cap for the child who turned three in the year (aged 3 at 31 December).
    d = _guarderia(unidad=None, unidad_descendientes={"edad_max": 3},
                   ajustes_por_hijo=[{"filtro": {"edad_min": 3, "edad_max": 3}, "maximo_por_unidad": "125"}])  # fmt: skip
    hijos = [{"edad": 3, "gastos": {"gastos_guarderia": 2000}}, {"edad": 1, "gastos": {"gastos_guarderia": 2000}}]
    assert evaluar(d, _inp(descendientes=hijos), _bases(), conjunta=False).importe == D(375)  # 125 + 250
    # Without the breakdown, the total against the sum of each child's own cap.
    assert evaluar(d, _inp(gastos_guarderia=D(4000), descendientes=[{"edad": 3}, {"edad": 1}]), _bases(),
                   conjunta=False).importe == D(375)  # fmt: skip
    sin_detalle = evaluar(d, _inp(gastos_guarderia=D(4000), num_descendientes=2), _bases(), conjunta=False)
    assert sin_detalle.importe == 0 and "descendientes" in sin_detalle.motivo
    # (b) The cap in proportion to the days of the year on which the child is under three.
    dias = _guarderia(unidad=None, unidad_descendientes={"edad_max": 3}, maximo_por_unidad="365",
                      prorrateo={"dato": "dias_hijo_en_edad", "edad_max": 2, "aplica_a": "maximo"})  # fmt: skip
    cumple_3 = {"fecha_nacimiento": "2023-04-11", "gastos": {"gastos_guarderia": 5000}}  # under three until 10 April
    assert evaluar(dias, _inp(descendientes=[cumple_3]), _bases(), conjunta=False).importe == D(100)
    nacido = {"fecha_nacimiento": "2026-12-02", "gastos": {"gastos_guarderia": 5000}}  # 30 days in the year
    assert evaluar(dias, _inp(descendientes=[nacido]), _bases(), conjunta=False).importe == D(30)
    todo_el_ano = {"edad": 2, "gastos": {"gastos_guarderia": 5000}}  # no date needed: under three all year
    assert evaluar(dias, _inp(descendientes=[todo_el_ano]), _bases(), conjunta=False).importe == D(365)
    sin_fecha = evaluar(dias, _inp(descendientes=[{"edad": 3, "gastos": {"gastos_guarderia": 5000}}]), _bases(),
                        conjunta=False)  # fmt: skip
    assert sin_fecha.importe == 0 and any("falta la fecha de nacimiento" in a for a in sin_fecha.avisos)
    # (c) The cap in proportion to the months at nursery declared for each child.
    meses = _guarderia(maximo_por_unidad="1200",
                       prorrateo={"dato": "meses_guarderia_hijo", "aplica_a": "maximo"})  # fmt: skip
    siete = {"edad": 2, "meses_guarderia": 7, "gastos": {"gastos_guarderia": 9000}}
    assert evaluar(meses, _inp(descendientes=[siete]), _bases(), conjunta=False).importe == D(700)
    # (d) A fixed amount per child and month at nursery.
    al_mes = _fija(importe="100", unidad="num_descendientes_menores_3",
                   prorrateo={"dato": "meses_guarderia_hijo", "por_cada_periodo": True})  # fmt: skip
    assert evaluar(al_mes, _inp(descendientes=[siete, {"edad": 1, "meses_guarderia": 2}]), _bases(),
                   conjunta=False).importe == D(900)  # fmt: skip


def test_expense_caps_and_caps_relative_to_the_tax_or_the_taxable_base() -> None:
    renta = _inp(alquiler_vivienda_habitual=D(12000))
    # base_maxima instead of a derived maximum: 15 % of at most 9.040 €.
    base_maxima = _pct(maximo=None, base_maxima="9040")
    assert evaluar(base_maxima, renta, _bases(), conjunta=False).importe == D("1356.00")
    # Expense capped at 10 % of the taxable base (20.000): 15 % × 2.000.
    donativos = _pct(base="donativos_autonomicos", maximo=None, base_maxima_fraccion="0.10")
    assert evaluar(donativos, _inp(donativos_autonomicos=D(5000)), _bases(), conjunta=False).importe == D(300)
    general = _pct(base="donativos_autonomicos", maximo=None, base_maxima_fraccion="0.10",
                   base_maxima_fraccion_de="base_imponible_total_menos_minimos")  # fmt: skip
    assert evaluar(general, _inp(donativos_autonomicos=D(5000)), _bases(), conjunta=False).importe == D("216.750")
    # Deduction capped at 10 % of the regional gross tax.
    cuota = Bases(**{**_bases().__dict__, "cuota_integra_autonomica": D(1200)})
    sobre_cuota = _pct(base="donativos_autonomicos", porcentaje="0.20", maximo=None, maximo_fraccion_cuota="0.10")
    assert evaluar(sobre_cuota, _inp(donativos_autonomicos=D(5000)), cuota, conjunta=False).importe == D(120)
    assert "no consta la cuota" in evaluar(sobre_cuota, _inp(donativos_autonomicos=D(5000)), _bases(),
                                           conjunta=False).motivo  # fmt: skip
    # A deduction that IS a share of the regional gross tax, for those who meet a condition.
    rural = _pct(base="cuota_integra_autonomica", porcentaje="0.15", maximo="500", habitantes_max=2999, titulo="Rural")
    assert evaluar(rural, _inp(habitantes_municipio=800), cuota, conjunta=False).importe == D(180)
    assert evaluar(rural, _inp(habitantes_municipio=3000), cuota, conjunta=False) is None
    assert evaluar(rural, _inp(), cuota, conjunta=False) is None  # inhabitants not stated: not the user's case
    # Expense cap per unit, and variants overriding the caps.
    por_hijo = _guarderia(maximo_por_unidad=None, base_maxima_por_unidad="1000",
                          variantes=[{"descripcion": "numerosa", "cuando": {"requiere": ["familia_numerosa"]},
                                      "base_maxima_por_unidad": "2000"}])  # fmt: skip
    gasto = dict(gastos_guarderia=D(5000), num_descendientes_menores_3=2)
    assert evaluar(por_hijo, _inp(**gasto), _bases(), conjunta=False).importe == D(300)
    assert evaluar(por_hijo, _inp(familia_numerosa=True, **gasto), _bases(), conjunta=False).importe == D(600)
    with pytest.raises(ValueError, match="fracción fuera de rango"):
        _pct(maximo_fraccion_cuota="10")
    with pytest.raises(ValueError, match="no lleva tramos, bases ni máximo por unidad"):
        _fija(base_maxima="100")


def test_tiered_rates_and_several_expenses_under_one_cap() -> None:
    # 20 % on the first 250 € and 25 % on the rest.
    d = _pct(base="donativos_autonomicos", porcentaje=None, maximo=None,
             tramos_porcentaje=[{"hasta": "250", "porcentaje": "0.20"}, {"porcentaje": "0.25"}])  # fmt: skip
    assert evaluar(d, _inp(donativos_autonomicos=D(200)), _bases(), conjunta=False).importe == D(40)
    assert evaluar(d, _inp(donativos_autonomicos=D(1000)), _bases(), conjunta=False).importe == D("237.50")
    # With a cap on the expense the tiers stop there: 50 + 25 % × 350.
    con_base = d.model_copy(update={"base_maxima": D(600)})
    assert evaluar(con_base, _inp(donativos_autonomicos=D(1000)), _bases(), conjunta=False).importe == D("137.50")
    # A variant may replace the tiers by a flat rate.
    plano = _pct(base="donativos_autonomicos", porcentaje=None, maximo=None,
                 tramos_porcentaje=[{"hasta": "250", "porcentaje": "0.20"}, {"porcentaje": "0.25"}],
                 variantes=[{"descripcion": "mayores", "cuando": {"edad_min": 65}, "porcentaje": "0.30"}])  # fmt: skip
    assert evaluar(plano, _inp(edad=70, donativos_autonomicos=D(1000)), _bases(), conjunta=False).importe == D(300)
    with pytest.raises(ValueError, match="uno de los dos"):
        _pct(tramos_porcentaje=[{"porcentaje": "0.25"}])
    with pytest.raises(ValueError, match="topes crecientes"):
        _pct(porcentaje=None, tramos_porcentaje=[{"hasta": "250", "porcentaje": "0.20"}])
    # Books and languages share one cap of 200 €; uniforms count at their own 5 %.
    educacion = _pct(base="gastos_libros_texto", porcentaje="0.15", maximo="200",
                     bases_adicionales=[{"base": "gastos_ensenanza_idiomas"},
                                        {"base": "gastos_uniformes_escolares", "porcentaje": "0.05"}])  # fmt: skip
    assert evaluar(educacion, _inp(gastos_ensenanza_idiomas=D(400)), _bases(), conjunta=False).importe == D(60)
    varios = _inp(gastos_libros_texto=D(300), gastos_ensenanza_idiomas=D(400), gastos_uniformes_escolares=D(200))
    assert evaluar(educacion, varios, _bases(), conjunta=False).importe == D(115)
    mucho = _inp(gastos_libros_texto=D(1000), gastos_ensenanza_idiomas=D(1000))
    assert evaluar(educacion, mucho, _bases(), conjunta=False).importe == D(200)
    assert evaluar(educacion, _inp(), _bases(), conjunta=False) is None
    # Child by child with several expenses: each child's sum against the per-child cap.
    por_hijo = _pct(base="gastos_libros_texto", porcentaje="0.15", maximo=None, maximo_por_unidad="100",
                    unidad="num_descendientes", bases_adicionales=[{"base": "gastos_ensenanza_idiomas"}])  # fmt: skip
    hijos = _inp(descendientes=[{"edad": 8, "gastos": {"gastos_libros_texto": 400, "gastos_ensenanza_idiomas": 600}},
                                {"edad": 10, "gastos": {"gastos_libros_texto": 200}}])  # fmt: skip
    assert evaluar(por_hijo, hijos, _bases(), conjunta=False).importe == D(130)  # min(150, 100) + 30
    with pytest.raises(ValueError, match="no admiten base máxima ni tramos"):
        _pct(base_maxima="100", bases_adicionales=[{"base": "gastos_deporte", "porcentaje": "0.05"}])
    with pytest.raises(ValueError, match="sin repetir"):
        _pct(bases_adicionales=[{"base": "alquiler_vivienda_habitual"}])


def test_mutually_exclusive_deductions_keep_the_most_favourable() -> None:
    nacimiento = _fija(articulo="art. 11", titulo="Nacimiento", importe="200", excluyente_con=["art. 14"])
    numerosa = _fija(articulo="art. 14", titulo="Familia numerosa", importe="400", unidad=None,
                     requiere=["familia_numerosa"], excluyente_con=["art. 11"])  # fmt: skip
    # File order no longer decides: the second one deducts more.
    total, lineas, avisos = _aplicar(
        [nacimiento, numerosa], _inp(hijos_nacidos_en_el_ejercicio=1, familia_numerosa=True)
    )
    assert total == D(400) and lineas == {"art. 14": D("400.00")}
    assert any("«Nacimiento» (art. 11, 200.00 €) no aplicada: es incompatible con «Familia numerosa» (art. 14, "
               "400.00 €), que te resulta más favorable" in a for a in avisos)  # fmt: skip
    # Three children: now the birth one wins. One-sided declarations are enough.
    un_lado = numerosa.model_copy(update={"excluyente_con": []})
    total, lineas, _ = _aplicar([nacimiento, un_lado], _inp(hijos_nacidos_en_el_ejercicio=3, familia_numerosa=True))
    assert total == D(600) and lineas == {"art. 11": D("600.00")}
    # Alone, each one applies; a tie goes to the first in the file.
    assert _aplicar([nacimiento, numerosa], _inp(familia_numerosa=True))[0] == D(400)
    empate = _aplicar([nacimiento, numerosa], _inp(hijos_nacidos_en_el_ejercicio=2, familia_numerosa=True))
    assert empate[1] == {"art. 11": D("400.00")}
    # B excludes both A and C, which are compatible with each other: A + C (350) beats B (300).
    a = _fija(articulo="A", titulo="A", importe="200", unidad=None, edad_min=18)
    b = _fija(articulo="B", titulo="B", importe="300", unidad=None, edad_min=18, excluyente_con=["A", "C"])
    c = _fija(articulo="C", titulo="C", importe="150", unidad=None, edad_min=18)
    assert _aplicar([a, b, c], _inp(edad=40))[1] == {"A": D("200.00"), "C": D("150.00")}
    b_mayor = b.model_copy(update={"importe": D(400)})
    assert _aplicar([a, b_mayor, c], _inp(edad=40))[1] == {"B": D("400.00")}
    # A refused deduction does not knock out the other one.
    con_limite = numerosa.model_copy(update={"limite_individual": D(10000)})
    total, lineas, _ = _aplicar([nacimiento, con_limite], _inp(hijos_nacidos_en_el_ejercicio=1, familia_numerosa=True))
    assert lineas == {"art. 11": D("200.00")}


def test_joint_cap_shared_by_several_deductions() -> None:
    cuota = Bases(**{**_bases().__dict__, "cuota_integra_autonomica": D(2000)})
    grupo = {"grupo": "vivienda", "maximo_fraccion_cuota": "0.15"}
    compra = _pct(articulo="art. 14", titulo="Compra", base="inversion_vivienda_habitual", porcentaje="0.05",
                  maximo=None, limite_conjunto=grupo)  # fmt: skip
    obras = _pct(articulo="art. 14 bis", titulo="Obras", base="gastos_eficiencia_energetica", porcentaje="0.12",
                 maximo=None, limite_conjunto=grupo)  # fmt: skip
    inp = _inp(inversion_vivienda_habitual=D(4000), gastos_eficiencia_energetica=D(2000))
    total, lineas, avisos = _aplicar([compra, obras], inp, cuota)
    assert lineas == {"art. 14": D("200.00"), "art. 14 bis": D("100.00")} and total == D(300)  # 15 % of 2.000
    assert any("«Obras»: reducida de 240.00 € a 100.00 € por el límite conjunto de 300.00 €" in a for a in avisos)
    fijo = {"grupo": "empleo del hogar", "maximo": "800"}
    hijos = _pct(articulo="1", titulo="Hijos", base="cotizaciones_empleada_hogar", porcentaje="0.5", maximo=None,
                 limite_conjunto=fijo)  # fmt: skip
    mayores = _pct(articulo="2", titulo="Mayores", base="gastos_cuidado_familiares", porcentaje="0.5", maximo=None,
                   limite_conjunto=fijo)  # fmt: skip
    assert _aplicar([hijos, mayores], _inp(cotizaciones_empleada_hogar=D(2000), gastos_cuidado_familiares=D(900)))[
        1
    ] == {"1": D("800.00")}


def test_either_spouse_may_meet_the_age_or_disability_condition() -> None:
    renta = dict(alquiler_vivienda_habitual=D(3000), edad=40)
    solo_declarante = _pct(edad_max=35)
    en_conjunta = _pct(edad_max=35, conyuge="en_conjunta")
    siempre = _pct(edad_max=35, conyuge="siempre")
    joven = _inp(edad_conyuge=33, **renta)
    assert evaluar(solo_declarante, joven, _bases(), conjunta=True).motivo is not None
    assert evaluar(en_conjunta, joven, _bases(), conjunta=True).importe == D(450)
    assert evaluar(en_conjunta, joven, _bases(), conjunta=False).motivo is not None  # individual return
    assert evaluar(siempre, joven, _bases(), conjunta=False).importe == D(450)
    # Nothing known about the spouse: only the taxpayer counts.
    assert "tú o tu cónyuge" in evaluar(en_conjunta, _inp(**renta), _bases(), conjunta=True).motivo
    # One person must meet every bound: a young spouse and a disabled taxpayer do not add up.
    ambas = _pct(edad_max=35, discapacidad_min=33, conyuge="en_conjunta")
    mezcla = _inp(grado_discapacidad=40, edad_conyuge=30, **renta)
    assert evaluar(ambas, mezcla, _bases(), conjunta=True).motivo is not None
    assert evaluar(ambas, _inp(edad_conyuge=30, grado_discapacidad_conyuge=33, **renta), _bases(),
                   conjunta=True).importe == D(450)  # fmt: skip
    # In alternatives and variants too.
    alt = _pct(alternativas=[{"edad_max": 35, "conyuge": "en_conjunta"}, {"requiere": ["familia_numerosa"]}])
    assert evaluar(alt, joven, _bases(), conjunta=True).importe == D(450)
    # A deduction for the taxpayer whose spouse has a disability of 65 %.
    conyuge = _fija(importe="100", unidad=None, conyuge_discapacidad_min=65, titulo="Cónyuge con discapacidad")
    assert evaluar(conyuge, _inp(grado_discapacidad_conyuge=65), _bases(), conjunta=False).importe == D(100)
    assert evaluar(conyuge, _inp(grado_discapacidad_conyuge=64), _bases(), conjunta=False) is None
    # Once per spouse who qualifies, in a joint return.
    cada_uno = _fija(importe="120", unidad=None, edad_min=65, conyuge="en_conjunta", por_contribuyente=True)
    assert evaluar(cada_uno, _inp(edad=70, edad_conyuge=68), _bases(), conjunta=True).importe == D(240)
    assert evaluar(cada_uno, _inp(edad=60, edad_conyuge=68), _bases(), conjunta=True).importe == D(120)
    assert evaluar(cada_uno, _inp(edad=70, edad_conyuge=68), _bases(), conjunta=False).importe == D(120)
    with pytest.raises(ValueError, match="solo tiene sentido"):
        _pct(conyuge="siempre")
    with pytest.raises(ValueError, match="por_contribuyente exige"):
        _fija(unidad=None, edad_min=65, por_contribuyente=True)


def test_income_ceilings_raised_lifted_or_softened() -> None:
    d = _fija(limite_individual="30000", limite_conjunta="48000",
              ajustes_limite=[{"cuando": {"requiere": ["familia_numerosa_especial"]}, "factor": "1.3"},
                              {"cuando": {"requiere": ["familia_numerosa"]}, "factor": "1.2"},
                              {"cuando": {"habitantes_max": 2999}, "sin_limite": True}])  # fmt: skip
    hijo = dict(hijos_nacidos_en_el_ejercicio=1)
    assert evaluar(d, _inp(**hijo), _bases("30001"), conjunta=False).motivo is not None
    assert evaluar(d, _inp(familia_numerosa=True, **hijo), _bases("36000"), conjunta=False).importe == D(200)
    excede = evaluar(d, _inp(familia_numerosa=True, **hijo), _bases("36000.01"), conjunta=False)
    assert "supera el límite de 36000.00 €" in excede.motivo
    especial = _inp(familia_numerosa=True, familia_numerosa_especial=True, **hijo)
    assert evaluar(d, especial, _bases("39000"), conjunta=False).importe == D(200)  # the first match wins
    assert evaluar(d, _inp(familia_numerosa=True, **hijo), _bases("57600"), conjunta=True).importe == D(200)
    assert evaluar(d, _inp(habitantes_municipio=1200, **hijo), _bases("90000"), conjunta=False).importe == D(200)
    # Half the deduction above the ceiling instead of nothing.
    mitad = _fija(limite_individual="30000", factor_sobre_limite="0.5",
                  ajustes_limite=[{"cuando": {"requiere": ["familia_monoparental"]}, "factor": "1.2"}])  # fmt: skip
    total, lineas, avisos = _aplicar([mitad], _inp(**hijo), _bases("31000"))
    assert total == D(100) and any("se aplica el 50 % de la deducción" in a for a in avisos)
    assert evaluar(mitad, _inp(familia_monoparental=True, **hijo), _bases("31000"), conjunta=False).importe == D(200)
    assert evaluar(mitad, _inp(familia_monoparental=True, **hijo), _bases("40000"), conjunta=False).importe == D(100)
    with pytest.raises(ValueError, match="factor o sin_limite"):
        _fija(ajustes_limite=[{"cuando": {"requiere": ["familia_numerosa"]}}])


def test_cap_shared_between_co_tenants() -> None:
    renta = dict(alquiler_vivienda_habitual=D(6000))
    tope = _pct(maximo="600", dividir_entre="num_declaraciones_mismo_alquiler")
    assert evaluar(tope, _inp(**renta), _bases(), conjunta=False).importe == D(600)  # not stated: one return
    assert evaluar(tope, _inp(num_declaraciones_mismo_alquiler=2, **renta), _bases(), conjunta=False).importe == D(300)
    poco = _inp(num_declaraciones_mismo_alquiler=2, alquiler_vivienda_habitual=D(1000))
    assert evaluar(tope, poco, _bases(), conjunta=False).importe == D(150)  # below the halved cap
    importe = _pct(maximo="600", dividir_entre="num_declaraciones_mismo_alquiler", dividir="importe")
    assert evaluar(importe, poco, _bases(), conjunta=False).importe == D(75)
    base = _pct(base="inversion_vivienda_habitual", porcentaje="0.05", maximo=None, base_maxima="10000",
                dividir_entre="num_declaraciones_misma_vivienda", dividir="base_maxima")  # fmt: skip
    compra = _inp(inversion_vivienda_habitual=D(9000), num_declaraciones_misma_vivienda=2)
    assert evaluar(base, compra, _bases(), conjunta=False).importe == D(250)
    with pytest.raises(ValueError, match="reparto desconocido"):
        _pct(dividir_entre="num_descendientes")


def test_decisive_facts_years_and_inhabitants() -> None:
    # New protected first home bought from 2021 on, in a municipality of fewer than 3.000 inhabitants.
    d = _pct(base="pagos_adquisicion_vivienda", porcentaje="0.10", maximo=None, base_maxima="9040",
             requiere=["vivienda_protegida", "vivienda_nueva_construccion", "primera_vivienda"],
             habitantes_max=2999, ejercicio_min={"ejercicio_adquisicion_vivienda": 2021})  # fmt: skip
    datos = dict(pagos_adquisicion_vivienda=D(10000), vivienda_protegida=True, vivienda_nueva_construccion=True,
                 primera_vivienda=True, habitantes_municipio=2999, ejercicio_adquisicion_vivienda=2021)  # fmt: skip
    assert evaluar(d, _inp(**datos), _bases(), conjunta=False).importe == D(904)
    for cambio, motivo in [({"primera_vivienda": False}, "«primera vivienda»"),
                           ({"habitantes_municipio": 3000}, "un municipio de hasta 2999 habitantes"),
                           ({"habitantes_municipio": None}, "falta el número de habitantes"),
                           ({"ejercicio_adquisicion_vivienda": 2020}, "no anterior a 2021"),
                           ({"ejercicio_adquisicion_vivienda": None}, "falta «ejercicio adquisicion vivienda»")]:  # fmt: skip
        assert motivo in evaluar(d, _inp(**{**datos, **cambio}), _bases(), conjunta=False).motivo
    # Widowed this year or in the two before.
    viudo = _fija(importe="150", unidad=None, requiere=["viudedad"], anios_desde_max={"ejercicio_viudedad": 2})
    assert evaluar(viudo, _inp(viudedad=True, ejercicio_viudedad=2024), _bases(), conjunta=False).importe == D(150)
    assert evaluar(viudo, _inp(viudedad=True, ejercicio_viudedad=2023), _bases(), conjunta=False) is None
    assert evaluar(viudo, _inp(viudedad=True), _bases(), conjunta=False) is None
    # Victims of gender violence as one more alternative.
    alquiler = _pct(alternativas=[{"edad_max": 35}, {"requiere": ["victima_violencia_genero"]}])
    victima = _inp(edad=50, victima_violencia_genero=True, alquiler_vivienda_habitual=D(3000))
    assert evaluar(alquiler, victima, _bases(), conjunta=False).importe == D(450)
    # Dependants with a disability of 65 % or more, counted apart.
    grado = _fija(importe="300", unidad="num_familiares_discapacidad_65_a_cargo")
    assert evaluar(grado, _inp(num_familiares_discapacidad_a_cargo=2, num_familiares_discapacidad_65_a_cargo=1),
                   _bases(), conjunta=False).importe == D(300)  # fmt: skip
    with pytest.raises(ValueError, match="ejercicios desconocidos"):
        _fija(anios_desde_max={"ejercicio_boda": 1})


def test_narrow_expenses_are_parts_of_the_broad_ones() -> None:
    # The broad inputs keep their meaning: the total. Left at zero, they are the sum of their parts.
    inp = _inp(gastos_deporte=D(300), gastos_cultura=D(200), gastos_optica=D(150), gastos_salud_bucodental=D(400),
               gastos_instalaciones_renovables=D(5000), intereses_prestamo_vivienda=D(1200),
               pagos_adquisicion_vivienda=D(6000), donativos_investigacion=D(100), donativos_medio_ambiente=D(50))  # fmt: skip
    assert inp.gastos_deporte_cultura == D(500) and inp.gastos_salud == D(550)
    assert inp.gastos_eficiencia_energetica == D(5000) and inp.inversion_vivienda_habitual == D(7200)
    assert inp.donativos_autonomicos == D(150)
    # A total may be larger than its parts (the rest is unclassified), never smaller.
    assert _inp(gastos_deporte_cultura=D(900), gastos_deporte=D(300)).gastos_deporte_cultura == D(900)
    with pytest.raises(ValueError, match="es el total de"):
        _inp(gastos_salud=D(100), gastos_optica=D(150))
    # Deductions on the narrow inputs, old ones on the broad input.
    deporte = _pct(base="gastos_deporte", porcentaje="0.30", maximo="150", titulo="Deporte")
    cultura = _pct(base="gastos_cultura", porcentaje="0.21", maximo=None, base_maxima="165", titulo="Cultura",
                   articulo="art. 3")  # fmt: skip
    ambos = _pct(base="gastos_deporte_cultura", porcentaje="0.10", maximo=None, titulo="Ambos", articulo="art. 4")
    total, lineas, _ = _aplicar([deporte, cultura, ambos], inp)
    assert lineas == {"art. 2": D("90.00"), "art. 3": D("34.65"), "art. 4": D("50.00")}
    assert evaluar(deporte, _inp(gastos_deporte_cultura=D(900)), _bases(), conjunta=False) is None  # not sport for sure
    # Interest only; fees plus dental under one cap.
    intereses = _pct(base="intereses_prestamo_vivienda", porcentaje="0.25", maximo=None, base_maxima="1000")
    assert evaluar(intereses, inp, _bases(), conjunta=False).importe == D(250)
    salud = _pct(base="gastos_honorarios_sanitarios", porcentaje="0.10", maximo="500",
                 bases_adicionales=[{"base": "gastos_salud_bucodental"}, {"base": "gastos_optica"}])  # fmt: skip
    assert evaluar(salud, inp, _bases(), conjunta=False).importe == D(55)
    for nuevo in ("gastos_veterinarios", "adquisicion_vehiculo_electrico", "inversion_economia_social",
                  "gastos_enfermedades_raras", "gastos_ela"):  # fmt: skip
        assert (
            evaluar(_pct(base=nuevo, maximo="100"), _inp(**{nuevo: D(1000)}), _bases(), conjunta=False).importe == 100
        )


def test_through_the_calculator_with_the_regional_tax() -> None:
    """The call site hands the engine the regional gross tax and the new inputs reach it."""
    comunidad = next(iter(load_params(2026).comunidades.values()))
    params = load_params(2026)
    d = _pct(base="cuota_integra_autonomica", porcentaje="0.10", maximo=None, habitantes_max=2999, titulo="Rural")
    propia = comunidad.model_copy(update={"deducciones_calculables": [d], "deducciones_madrid": None})
    clave = next(k for k, c in params.comunidades.items() if c is comunidad)
    params = params.model_copy(update={"comunidades": {**params.comunidades, clave: propia}})
    datos = IRPFInput(ejercicio=2026, comunidad_autonoma=comunidad.nombre, rendimiento_trabajo_bruto=D(30000),
                      cotizaciones_seguridad_social=D(1900), habitantes_municipio=500)  # fmt: skip
    r = calcular_irpf(datos, params)
    linea = next(x for x in r.lineas if x.concepto == "Deducción autonómica: Rural")
    sin = calcular_irpf(datos.model_copy(update={"habitantes_municipio": 5000}), params)
    assert linea.importe < 0 and sin.cuota_liquida - r.cuota_liquida == -linea.importe


# --------------------------------------------------------------------------- #
# Regression: the encoded deductions of every community, through the calculator
# --------------------------------------------------------------------------- #

SNAPSHOT = Path(__file__).with_name("autonomicas_snapshot.json")

# Taxpayers chosen to trigger as many encoded deductions as possible. Only inputs
# that existed before the schema was extended, so the snapshot pins the old behaviour.
ESCENARIOS: dict[str, dict[str, object]] = {
    "joven_alquiler": dict(edad=28, rendimiento_trabajo_bruto=D(19000), cotizaciones_seguridad_social=D(1200),
                           alquiler_vivienda_habitual=D(7200), gastos_deporte_cultura=D(600), gastos_salud=D(900)),
    "joven_rural_vivienda": dict(edad=33, rendimiento_trabajo_bruto=D(24000), cotizaciones_seguridad_social=D(1500),
                                 municipio_rural_o_despoblacion=True, vivienda_en_municipio_rural=True,
                                 inversion_vivienda_habitual=D(8000), alquiler_vivienda_habitual=D(3000),
                                 gastos_eficiencia_energetica=D(5000)),
    "familia_nacimiento": dict(edad=36, rendimiento_trabajo_bruto=D(26000), cotizaciones_seguridad_social=D(1650),
                               num_descendientes=2, num_descendientes_menores_3=1, hijos_nacidos_en_el_ejercicio=1,
                               hijos_nacidos_ultimos_3_ejercicios=1, gastos_guarderia=D(2400),
                               gastos_libros_texto=D(300), gastos_ensenanza_idiomas=D(800),
                               num_hijos_gastos_educativos=1, cotizaciones_empleada_hogar=D(1800),
                               gastos_cuidado_familiares=D(2000)),
    "numerosa_conjunta": dict(edad=45, tributacion="conjunta_biparental", rendimiento_trabajo_bruto=D(34000),
                              cotizaciones_seguridad_social=D(2150), num_descendientes=3, familia_numerosa=True,
                              gastos_libros_texto=D(500), gastos_ensenanza_idiomas=D(1200),
                              num_hijos_gastos_educativos=3, alquiler_vivienda_habitual=D(8400),
                              donativos_autonomicos=D(400), menores_acogidos=1),
    "numerosa_especial_compartida": dict(edad=41, rendimiento_trabajo_bruto=D(21000),
                                         cotizaciones_seguridad_social=D(1330), num_descendientes=5,
                                         num_descendientes_menores_3=2, hijos_nacidos_en_el_ejercicio=2,
                                         familia_numerosa=True, familia_numerosa_especial=True,
                                         descendientes_compartidos=True, gastos_guarderia=D(5000),
                                         hijos_adoptados_en_el_ejercicio=1),
    "monoparental": dict(edad=39, rendimiento_trabajo_bruto=D(17500), cotizaciones_seguridad_social=D(1110),
                         tributacion="conjunta_monoparental", familia_monoparental=True, num_descendientes=1,
                         num_descendientes_menores_3=1, gastos_guarderia=D(3000), alquiler_vivienda_habitual=D(6000),
                         cotizaciones_empleada_hogar=D(1500)),
    "mayor_discapacidad": dict(edad=78, grado_discapacidad=65, rendimiento_trabajo_bruto=D(15000), viudedad=True,
                               num_ascendientes_mayores_65=1, num_ascendientes_mayores_75=1,
                               num_familiares_discapacidad_a_cargo=1, gastos_cuidado_familiares=D(4000),
                               cotizaciones_empleada_hogar=D(2200), gastos_salud=D(2500),
                               alquiler_vivienda_habitual=D(5400)),
    "inversor_renta_alta": dict(edad=50, rendimiento_trabajo_bruto=D(70000), cotizaciones_seguridad_social=D(4400),
                                rendimiento_capital_mobiliario=D(2500), inversion_empresas_nueva_creacion=D(30000),
                                donativos_autonomicos=D(3000), gastos_eficiencia_energetica=D(12000),
                                gastos_deporte_cultura=D(1500), inversion_vivienda_habitual=D(15000)),
    "renta_baja_dos_hijos": dict(edad=30, rendimiento_trabajo_bruto=D(13000), cotizaciones_seguridad_social=D(825),
                                 num_descendientes=2, gastos_libros_texto=D(250), num_hijos_gastos_educativos=2,
                                 alquiler_vivienda_habitual=D(4800), grado_discapacidad=33,
                                 base_imponible_resto_unidad_familiar=D(9000)),
}  # fmt: skip


def _deducciones_autonomicas_de(comunidad: str, datos: dict[str, object]) -> dict[str, str]:
    r = calcular_irpf(IRPFInput(ejercicio=2026, comunidad_autonoma=comunidad, **datos))
    return {x.concepto: str(-x.importe) for x in r.lineas if x.concepto.startswith("Deducción autonómica")}


def _todas() -> dict[str, dict[str, dict[str, str]]]:
    return {
        clave: {nombre: _deducciones_autonomicas_de(c.nombre, datos) for nombre, datos in ESCENARIOS.items()}
        for clave, c in sorted(load_params(2026).comunidades.items())
    }


def test_encoded_deductions_of_every_community_compute_as_in_the_snapshot() -> None:
    """Pins what the 131 deductions encoded before the schema extension give for fixed taxpayers.

    After a deliberate change of the DATA, regenerate with
    ``REGENERAR_SNAPSHOT_AUTONOMICAS=1 uv run pytest tests/tax/test_autonomicas.py -k snapshot`` and review the diff.
    """
    actual = _todas()
    if os.environ.get("REGENERAR_SNAPSHOT_AUTONOMICAS"):
        SNAPSHOT.write_text(json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    esperado = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert actual == esperado
