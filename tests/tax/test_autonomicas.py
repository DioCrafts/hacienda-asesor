"""Data-driven regional deductions: the generic routine, on synthetic deductions."""

import json
import os
from decimal import Decimal
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
        _fija(importe=None, importes_por_orden=["1"], unidad="num_descendientes")
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
