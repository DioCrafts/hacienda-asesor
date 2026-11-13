"""Minimum for disability of descendants and ascendants (art. 60.2 LIRPF), state and regional.

The figures are the ones the laws print (quoted next to each test) and the sums are done by hand:

* LIRPF art. 60.2: «El mínimo por discapacidad de ascendientes o descendientes será de 3.000 euros anuales por cada
  uno de los descendientes o ascendientes que generen derecho a la aplicación del mínimo a que se refieren los
  artículos 58 y 59 de esta Ley [...]. El mínimo será de 9.000 euros anuales, por cada uno de ellos que acrediten un
  grado de discapacidad igual o superior al 65 por ciento. Dicho mínimo se aumentará, en concepto de gastos de
  asistencia, en 3.000 euros anuales por cada ascendiente o descendiente que acredite necesitar ayuda de terceras
  personas o movilidad reducida, o un grado de discapacidad igual o superior al 65 por ciento.»
* LIRPF arts. 57.1, 58.1 and 59.1: 5.550 € (taxpayer), 2.400 € (first descendant), 1.150 € (ascendant «mayor de 65
  años o con discapacidad cualquiera que sea su edad»).
* Art. 61.1.ª: «su importe se prorrateará entre ellos por partes iguales».
"""

from __future__ import annotations

from decimal import Decimal
import json
import re
from typing import Any

from pydantic import ValidationError
import pytest

from hacienda_gpt.tax.beneficios import evaluar_beneficios
from hacienda_gpt.tax.conjunta import _conjunta, comparar_tributacion, hijos_comunes
from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult, calcular_irpf
from hacienda_gpt.tax.params import DATA_DIR, Minimos, load_params
from hacienda_gpt.tax.profile import build_input
from hacienda_gpt.tax.recodificacion.adaptadores import CIFRAS, NO_SON_GRUPOS_DE_COMUNIDAD
from hacienda_gpt.tax.recodificacion.nucleo import Objetivo
from hacienda_gpt.tax.verificacion import comprobaciones

D = Decimal

BASE: dict[str, Any] = {
    "ejercicio": 2026,
    "rendimiento_trabajo_bruto": D(30000),
    "cotizaciones_seguridad_social": D(1950),
}


def _calc(**datos: Any) -> IRPFResult:
    return calcular_irpf(IRPFInput(**{**BASE, **datos}))


def _linea(r: IRPFResult, inicio: str) -> D:
    return sum((x.importe for x in r.lineas if x.concepto.startswith(inicio)), D(0))


# --------------------------------------------------------------------------- #
# State minimum
# --------------------------------------------------------------------------- #


def test_a_disabled_descendant_adds_3000_9000_and_the_assistance_expenses() -> None:
    sin = _calc(descendientes=[{"edad": 10}])
    assert sin.minimo_personal_familiar == D("7950.00")  # 5.550 + 2.400
    del_33 = _calc(descendientes=[{"edad": 10, "grado_discapacidad": 33}])
    assert _linea(del_33, "Mínimo por discapacidad de descendientes") == D("3000.00")
    assert del_33.minimo_personal_familiar == D("10950.00")
    # 65 % or more: 9.000 plus the assistance expenses (3.000), «o un grado de discapacidad igual o superior al 65».
    del_65 = _calc(descendientes=[{"edad": 10, "grado_discapacidad": 65}])
    assert _linea(del_65, "Mínimo por discapacidad de descendientes") == D("12000.00")
    assert del_65.minimo_personal_familiar == D("19950.00")
    # 33 % to 64 % with third parties' help or reduced mobility: 3.000 + 3.000.
    asistencia = _calc(descendientes=[{"edad": 10, "grado_discapacidad": 40, "necesita_asistencia": True}])
    assert _linea(asistencia, "Mínimo por discapacidad de descendientes") == D("6000.00")
    assert asistencia.minimo_personal_familiar == D("13950.00")
    # A disabled descendant counts at any age (art. 58.1).
    adulto = _calc(descendientes=[{"edad": 40, "grado_discapacidad": 65}])
    assert adulto.minimo_personal_familiar == D("19950.00")
    assert any("art. 60.2 LIRPF" in a and "1.800 €" in a for a in del_33.avisos)
    assert not any("art. 60.2 LIRPF" in a for a in sin.avisos)


def test_shared_descendants_halve_the_disability_minimum_too() -> None:
    r = _calc(descendientes=[{"edad": 10, "grado_discapacidad": 65}], descendientes_compartidos=True)
    assert _linea(r, "Mínimo por discapacidad de descendientes (50 %, compartido)") == D("6000.00")
    assert r.minimo_personal_familiar == D("12750.00")  # 5.550 + 1.200 + 6.000


def test_a_disabled_ascendant_under_65_gives_the_ascendant_minimum_and_the_disability_one() -> None:
    antes = _calc()
    madre = _calc(num_ascendientes_discapacidad=1)  # 60 years old, 33 %: not in num_ascendientes_mayores_65
    assert madre.minimo_personal_familiar - antes.minimo_personal_familiar == D("4150.00")  # 1.150 + 3.000
    assert _linea(madre, "Mínimo por ascendientes") == D("1150.00")
    assert _linea(madre, "Mínimo por discapacidad de ascendientes") == D("3000.00")
    assert any("61.5.ª" in a for a in madre.avisos)  # half a year living together, not checked
    # Over 65 and disabled: one ascendant, counted once for the ascendant minimum.
    padre = _calc(num_ascendientes_mayores_65=1, num_ascendientes_discapacidad=1, num_ascendientes_discapacidad_65=1)
    assert _linea(padre, "Mínimo por ascendientes") == D("1150.00")
    assert _linea(padre, "Mínimo por discapacidad de ascendientes") == D("12000.00")
    assert any("entre los mayores de 65" in a for a in padre.avisos)
    # Two people: an 80-year-old without disability and a 60-year-old with it.
    dos = _calc(
        num_ascendientes_mayores_65=1, num_ascendientes_discapacidad=1, num_ascendientes_discapacidad_hasta_65=1
    )
    assert _linea(dos, "Mínimo por ascendientes") == D("2300.00")


def test_the_ascendants_minimums_are_prorated_among_the_returns_that_apply_them() -> None:
    datos = dict(num_ascendientes_mayores_65=1, num_ascendientes_discapacidad=1, num_ascendientes_discapacidad_65=1)
    sola = _calc(**datos)
    assert any("se reparten" in a and "61.1.ª" in a for a in sola.avisos)
    dos = _calc(**datos, num_declaraciones_mismos_ascendientes=2)
    assert _linea(dos, "Mínimo por ascendientes (prorrateado entre 2 declaraciones)") == D("575.00")
    assert _linea(dos, "Mínimo por discapacidad de ascendientes (prorrateado entre 2 declaraciones)") == D("6000.00")
    assert dos.minimo_personal_familiar == D("12125.00")
    tres = _calc(**datos, num_declaraciones_mismos_ascendientes=3)
    assert tres.minimo_personal_familiar == D("9933.33")  # 5.550 + 383,33 + 4.000


def test_the_taxpayer_and_the_spouse_get_the_assistance_expenses_from_33_percent_when_they_need_help() -> None:
    assert _calc(grado_discapacidad=40).minimo_personal_familiar == D("8550.00")  # art. 60.1: 5.550 + 3.000
    con_ayuda = _calc(grado_discapacidad=40, necesita_asistencia=True)
    assert con_ayuda.minimo_personal_familiar == D("11550.00")  # «se aumentará [...] en 3.000 euros anuales»
    assert any("gastos de asistencia" in a for a in _calc(grado_discapacidad=40).avisos)
    sin_grado = _calc(necesita_asistencia=True)
    assert sin_grado.minimo_personal_familiar == D("5550.00")
    assert any("sin un grado de discapacidad del 33 %" in a for a in sin_grado.avisos)
    conjunta = _calc(tributacion="conjunta_biparental", grado_discapacidad_conyuge=50, necesita_asistencia_conyuge=True)
    assert _linea(conjunta, "Mínimo por discapacidad del cónyuge") == D("6000.00")


# --------------------------------------------------------------------------- #
# Regional minimums
# --------------------------------------------------------------------------- #


def test_madrid_applies_its_own_amounts_for_relatives() -> None:
    # Art. 2 quater c) and d) DLeg. 1/2010: «3.219,81 euros anuales [...] 9.659,44 euros anuales [...] se aumentará,
    # en concepto de gastos de asistencia, en 3.219,81 euros».
    r = _calc(comunidad_autonoma="Madrid", descendientes=[{"edad": 10, "grado_discapacidad": 65}])
    assert r.minimo_autonomico == D("21411.75")  # 5.956,65 + 2.575,85 + 9.659,44 + 3.219,81
    r = _calc(
        comunidad_autonoma="Madrid", descendientes=[{"edad": 10, "grado_discapacidad": 40, "necesita_asistencia": True}]
    )
    assert r.minimo_autonomico == D("14972.12")  # 5.956,65 + 2.575,85 + 3.219,81 + 3.219,81


def test_andalucia_applies_its_own_amounts_to_a_disabled_mother_under_65() -> None:
    # Art. 23 bis.3 and 4.b) Ley 5/2021: 1.200 € per ascendant, «3.130 euros anuales por cada uno de los ascendientes
    # o descendientes con discapacidad».
    r = _calc(comunidad_autonoma="Andalucía", rendimiento_trabajo_bruto=D(25000), num_ascendientes_discapacidad=1)
    assert r.minimo_personal_familiar == D("9700.00") and r.minimo_autonomico == D("10120.00")
    # The relative now counts for the regional deduction that requires the minimum for relatives' disability.
    assert _linea(r, "Deducción autonómica: Asistencia a personas con discapacidad") == D("-100.00")


def test_la_rioja_raises_only_the_descendants_minimum_and_not_the_assistance_expenses() -> None:
    # Art. 31 bis Ley 10/2017: «el mínimo por discapacidad de descendientes será de 3.300 euros anuales. Dicho importe
    # será de 9.900 euros anuales cuando se acredite un grado de discapacidad igual o superior al 65 %. No se modifica
    # la cuantía del concepto 'gastos de asistencia'».
    rioja = load_params(2026).comunidades["la_rioja"].minimos
    assert rioja is not None and rioja.discapacidad_familiar(descendiente=True) == (D(3300), D(9900), D(3000))
    assert rioja.discapacidad_familiar(descendiente=False) == (D(3000), D(9000), D(3000))
    r = _calc(comunidad_autonoma="La Rioja", descendientes=[{"edad": 30, "grado_discapacidad": 65}])
    assert r.minimo_personal_familiar == D("19950.00")
    assert r.minimo_autonomico == D("20850.00")  # 5.550 + 2.400 + 9.900 + 3.000
    r = _calc(
        comunidad_autonoma="La Rioja",
        descendientes=[{"edad": 30, "grado_discapacidad": 40, "necesita_asistencia": True}],
    )
    assert r.minimo_autonomico == D("14250.00")  # 5.550 + 2.400 + 3.300 + 3.000
    # Ascendants keep the State's amounts.
    r = _calc(comunidad_autonoma="La Rioja", num_ascendientes_discapacidad=1, num_ascendientes_discapacidad_65=1)
    assert r.minimo_autonomico == r.minimo_personal_familiar == D("18700.00")  # 5.550 + 1.150 + 9.000 + 3.000
    # Without disabled descendants nothing changes against the State's amounts.
    assert _calc(comunidad_autonoma="La Rioja").minimo_autonomico == D("5550.00")


def test_every_community_with_its_own_minimums_uses_the_taxpayer_amounts_for_relatives_except_la_rioja() -> None:
    for ejercicio in (2025, 2026):
        p = load_params(ejercicio)
        for clave, comunidad in p.comunidades.items():
            m = comunidad.minimos
            if m is None:
                continue
            propios = (m.discapacidad, m.discapacidad_65, m.gastos_asistencia)
            assert m.discapacidad_familiar(descendiente=False) == propios, clave
            assert (m.discapacidad_familiar(descendiente=True) == propios) is (clave != "la_rioja"), clave


# --------------------------------------------------------------------------- #
# Inputs: the children's detail, the counts and their consistency
# --------------------------------------------------------------------------- #


def test_the_childrens_detail_and_the_counts_give_the_same_result() -> None:
    detalle = _calc(
        comunidad_autonoma="Madrid",
        descendientes=[
            {"edad": 10, "grado_discapacidad": 40, "necesita_asistencia": True},
            {"edad": 6, "grado_discapacidad": 70},
            {"edad": 4},
        ],
    )
    recuentos = _calc(
        comunidad_autonoma="Madrid",
        num_descendientes=3,
        num_descendientes_discapacidad=2,
        num_descendientes_discapacidad_65=1,
        num_descendientes_asistencia=1,
    )
    assert detalle.minimo_personal_familiar == recuentos.minimo_personal_familiar
    assert detalle.minimo_autonomico == recuentos.minimo_autonomico
    assert detalle.resultado == recuentos.resultado
    # 2.400 + 2.700 + 4.000 + (3.000 + 3.000) + (9.000 + 3.000)
    assert detalle.minimo_personal_familiar == D("32650.00")
    inp = IRPFInput(ejercicio=2026, descendientes=[{"edad": 10, "grado_discapacidad": 65}])
    assert (inp.num_descendientes_discapacidad, inp.num_descendientes_discapacidad_65) == (1, 1)
    assert (inp.num_familiares_discapacidad_a_cargo, inp.num_familiares_discapacidad_65_a_cargo) == (1, 1)


@pytest.mark.parametrize(
    ("datos", "mensaje"),
    [
        (dict(num_descendientes_discapacidad=1, num_descendientes_discapacidad_65=2), "num_descendientes_discapacidad"),
        (dict(num_descendientes=1, num_descendientes_discapacidad=2), "«num_descendientes» (1)"),
        (
            dict(num_descendientes_discapacidad=1, num_descendientes_discapacidad_65=1, num_descendientes_asistencia=1),
            "los del 33 % al 64 %",
        ),
        (dict(num_ascendientes_discapacidad=1, num_ascendientes_discapacidad_65=2), "num_ascendientes_discapacidad"),
        (dict(num_ascendientes_discapacidad=1, num_ascendientes_discapacidad_hasta_65=2), "65 años o menos"),
        (
            dict(
                num_ascendientes_mayores_65=0, num_ascendientes_discapacidad=2, num_ascendientes_discapacidad_hasta_65=1
            ),
            "tendrían más de 65 años",
        ),
        (
            dict(
                num_familiares_discapacidad_a_cargo=1, num_descendientes_discapacidad=1, num_ascendientes_discapacidad=1
            ),
            "num_familiares_discapacidad_a_cargo",
        ),
        (
            dict(num_familiares_discapacidad_65_a_cargo=1, num_ascendientes_discapacidad_65=2),
            "num_familiares_discapacidad_65_a_cargo",
        ),
        (dict(descendientes=[{"edad": 3, "necesita_asistencia": True}]), "33 % o más"),
        (dict(descendientes=[{"edad": 3, "grado_discapacidad": 65}], num_descendientes_discapacidad_65=2), "detalle"),
    ],
)
def test_inconsistent_counts_are_rejected_in_spanish(datos: dict[str, Any], mensaje: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(mensaje)):
        IRPFInput(ejercicio=2026, **datos)


def test_counts_left_at_zero_are_filled_from_their_parts() -> None:
    inp = IRPFInput(ejercicio=2026, num_descendientes_discapacidad_65=1, num_ascendientes_asistencia=1)
    assert inp.num_descendientes_discapacidad == 1 and inp.num_descendientes == 1
    assert inp.num_ascendientes_discapacidad == 1 and inp.num_ascendientes_discapacidad_hasta_65 == 1
    assert (inp.num_familiares_discapacidad_a_cargo, inp.num_familiares_discapacidad_65_a_cargo) == (2, 1)
    # A larger total than the parts is kept: the rest are relatives nobody said are children or ascendants.
    r = _calc(num_familiares_discapacidad_a_cargo=2, num_descendientes=1, num_descendientes_discapacidad=1)
    assert _linea(r, "Mínimo por discapacidad de descendientes") == D("3000.00")
    assert any("solo 1 consta" in a for a in r.avisos)


def test_relatives_given_only_as_a_total_get_no_minimum_but_a_warning() -> None:
    r = _calc(num_familiares_discapacidad_a_cargo=1, num_familiares_discapacidad_65_a_cargo=1)
    assert r.minimo_personal_familiar == D("5550.00")
    assert sum("solo 0 constan" in a for a in r.avisos) == 2


# --------------------------------------------------------------------------- #
# Joint and separate returns
# --------------------------------------------------------------------------- #


def test_joint_return_counts_the_children_once_and_each_spouses_own_ascendants() -> None:
    a = IRPFInput(
        **BASE,
        descendientes=[{"edad": 10, "grado_discapacidad": 65}],
        num_ascendientes_mayores_65=1,
        num_ascendientes_discapacidad=1,  # her mother, 70, 33 %
    )
    b = IRPFInput(
        ejercicio=2026,
        rendimiento_trabajo_bruto=D(10000),
        num_ascendientes_discapacidad=1,  # his father, 60, 65 %
        num_ascendientes_discapacidad_65=1,
    )
    unida = _conjunta(a, b)
    assert (unida.num_descendientes_discapacidad, unida.num_ascendientes_discapacidad) == (1, 2)
    assert (unida.num_familiares_discapacidad_a_cargo, unida.num_familiares_discapacidad_65_a_cargo) == (3, 2)
    r = calcular_irpf(unida)
    assert _linea(r, "Mínimo por discapacidad de descendientes") == D("12000.00")
    assert _linea(r, "Mínimo por ascendientes") == D("2300.00")
    assert _linea(r, "Mínimo por discapacidad de ascendientes") == D("15000.00")  # 3.000 + 9.000 + 3.000
    assert r.minimo_personal_familiar == D("37250.00")
    # Separate returns: the children are common (half each), each spouse keeps their own ascendants.
    comunes = hijos_comunes(a, b)
    assert comunes.b.num_descendientes_discapacidad == 1 and comunes.b.num_familiares_discapacidad_a_cargo == 2
    ra, rb = calcular_irpf(comunes.a), calcular_irpf(comunes.b)
    assert _linea(ra, "Mínimo por discapacidad de descendientes") == D("6000.00")
    assert _linea(rb, "Mínimo por discapacidad de descendientes") == D("6000.00")
    assert _linea(rb, "Mínimo por discapacidad de ascendientes") == D("12000.00")
    comparacion = comparar_tributacion(a, b)
    assert any("del mínimo por su discapacidad" in x for x in comparacion.avisos)
    assert any("ascendientes de cada cónyuge" in x for x in comparacion.avisos)


def test_relatives_given_only_as_totals_are_not_counted_twice_in_the_household() -> None:
    a = IRPFInput(**BASE, num_familiares_discapacidad_a_cargo=1)
    b = IRPFInput(ejercicio=2026, num_familiares_discapacidad_a_cargo=1)
    # As before: the household's figure is the larger one, not the sum.
    assert _conjunta(a, b).num_familiares_discapacidad_a_cargo == 1
    # The spouse who gave no children takes the other's; their own total may be that same child.
    con_hijo = IRPFInput(**BASE, descendientes=[{"edad": 9, "grado_discapacidad": 50}])
    assert hijos_comunes(con_hijo, b).b.num_familiares_discapacidad_a_cargo == 1
    padre = IRPFInput(ejercicio=2026, num_ascendientes_discapacidad=1)
    assert hijos_comunes(con_hijo, padre).b.num_familiares_discapacidad_a_cargo == 2


def test_the_spouse_in_a_joint_return_keeps_their_need_of_assistance() -> None:
    a = IRPFInput(**BASE, grado_discapacidad=40)
    b = IRPFInput(ejercicio=2026, grado_discapacidad=50, necesita_asistencia=True)
    unida = _conjunta(a, b)
    assert not unida.necesita_asistencia and unida.necesita_asistencia_conyuge
    r = calcular_irpf(unida)
    assert _linea(r, "Mínimo por discapacidad del contribuyente") == D("3000.00")
    assert _linea(r, "Mínimo por discapacidad del cónyuge") == D("6000.00")


# --------------------------------------------------------------------------- #
# Profile, benefits catalogue
# --------------------------------------------------------------------------- #


def test_profile_facts_reach_the_calculator_and_an_old_total_gives_way_to_its_parts() -> None:
    inp = build_input(
        ejercicio=2026,
        facts={"num_ascendientes_discapacidad": 1.0, "necesita_asistencia": True, "grado_discapacidad": 45.0},
    )
    assert inp.num_ascendientes_discapacidad == 1 and inp.necesita_asistencia
    # A total stored months ago, smaller than the parts given now: the calculator takes the parts' sum.
    inp = build_input(
        ejercicio=2026,
        facts={"num_familiares_discapacidad_a_cargo": 1.0, "num_ascendientes_discapacidad": 1.0},
        datos={"descendientes": [{"edad": 8, "grado_discapacidad": 33}]},
    )
    assert inp.num_familiares_discapacidad_a_cargo == 2 and inp.num_descendientes_discapacidad == 1


def test_the_benefits_catalogue_shows_the_minimum_for_relatives() -> None:
    inp = IRPFInput(**BASE, num_ascendientes_discapacidad=1)
    por_id = {b.id: b for b in evaluar_beneficios(inp, conocidos=set(IRPFInput.model_fields))}
    assert por_id["minimo_discapacidad_familiares"].estado == "aplicado"
    assert "3000 €" in por_id["minimo_discapacidad_familiares"].detalle
    assert "1 ascendiente" in por_id["minimo_ascendientes"].detalle
    solo_total = IRPFInput(**BASE, num_familiares_discapacidad_a_cargo=1)
    por_id = {b.id: b for b in evaluar_beneficios(solo_total, conocidos=set(IRPFInput.model_fields))}
    assert por_id["minimo_discapacidad_familiares"].estado == "por_confirmar"


# --------------------------------------------------------------------------- #
# Data: quotes, verification and the re-encoding gate
# --------------------------------------------------------------------------- #


def _evidencia() -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = json.loads((DATA_DIR / "evidencia_parametros.json").read_text(encoding="utf-8"))
    return data


def test_every_minimums_group_with_quotes_is_checked_by_them_and_every_figure_is_in_them() -> None:
    evidencia = _evidencia()
    for ejercicio in (2025, 2026):
        fichero = f"irpf_{ejercicio}.json"
        raw = json.loads((DATA_DIR / fichero).read_text(encoding="utf-8"))
        rutas = set(evidencia[fichero])
        assert {"minimos", "comunidades/madrid/minimos", "comunidades/la_rioja/minimos"} <= rutas
        # The quotes vouch for the group (its fingerprint matches): verification checks the quotes literally.
        tipos = {"/".join(c.ruta): c.tipo for c in comprobaciones(load_params(ejercicio))}
        assert all(tipos[ruta] == "cita" for ruta in rutas), tipos
        for ruta in rutas:
            partes = ruta.split("/")
            nombre, ambito = ("minimos", "estado") if partes == ["minimos"] else ("minimos_autonomicos", partes[1])
            adaptador = CIFRAS[nombre]
            grupo = raw["minimos"] if ambito == "estado" else raw["comunidades"][ambito]["minimos"]
            objetivo = Objetivo(
                tipo=adaptador.tipo,
                fichero=adaptador.fichero,
                ruta=adaptador.ruta_de(ambito),
                etiqueta=ruta,
                ambito=ambito,
                boe_id=evidencia[fichero][ruta]["boe_id"],
                articulos_ley=["1"],
                ejercicios=[ejercicio],
                antes={ejercicio: grupo},
            )
            valor = {k: v for k, v in grupo.items() if k != "fuente"}
            modelo, control = adaptador.controlar(
                valor, evidencia[fichero][ruta]["citas"], objetivo, efectos_desde=None, datos={ejercicio: raw}
            )
            # Every figure is in a quote; a regional law may leave amounts to the State (said, not failed).
            assert modelo is not None and control.ok, (ruta, control.fallos)
            assert all("coincide con el importe estatal" in r for r in control.revisar), (ruta, control.revisar)


def test_the_gate_rejects_a_relatives_amount_no_quote_prints_and_cases_reach_the_new_lines() -> None:
    raw = json.loads((DATA_DIR / "irpf_2026.json").read_text(encoding="utf-8"))
    adaptador = CIFRAS["minimos_autonomicos"]
    grupo = raw["comunidades"]["la_rioja"]["minimos"]
    objetivo = Objetivo(
        tipo=adaptador.tipo,
        fichero=adaptador.fichero,
        ruta=adaptador.ruta_de("la_rioja"),
        etiqueta="la_rioja",
        ambito="la_rioja",
        boe_id="BOE-A-2017-13750",
        articulos_ley=["31 bis"],
        ejercicios=[2026],
        antes={2026: grupo},
    )
    citas = _evidencia()["irpf_2026.json"]["comunidades/la_rioja/minimos"]["citas"]
    valor = {k: v for k, v in grupo.items() if k != "fuente"}
    _, control = adaptador.controlar(
        {**valor, "discapacidad_descendientes": "3400"}, citas, objetivo, efectos_desde=None, datos={2026: raw}
    )
    assert any("discapacidad_descendientes = 3400" in f for f in control.fallos)
    # A reviewer's case for the State group compares the new lines of the calculator.
    estatal = CIFRAS["minimos"]
    assert "Mínimo por discapacidad de descendientes" in estatal.cifras.lineas
    modelo = Minimos.model_validate(raw["minimos"])
    objetivo_estatal = Objetivo(
        tipo=estatal.tipo,
        fichero=estatal.fichero,
        ruta=["minimos"],
        etiqueta="minimos",
        ambito="estado",
        boe_id="BOE-A-2006-20764",
        articulos_ley=["60"],
        ejercicios=[2026],
        antes={2026: raw["minimos"]},
    )
    caso = {
        "contribuyente": {"num_ascendientes_discapacidad": 1, "num_ascendientes_asistencia": 1},
        "linea": "Mínimo por discapacidad de ascendientes",
    }
    importe, concepto = estatal.evaluar_caso(modelo, caso, objetivo_estatal)
    assert importe == D("6000.00") and concepto == "Mínimo por discapacidad de ascendientes"
    assert "minimos" not in NO_SON_GRUPOS_DE_COMUNIDAD
