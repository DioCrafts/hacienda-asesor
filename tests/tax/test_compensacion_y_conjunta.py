"""Savings-base offsetting (art. 49 LIRPF) and joint vs separate returns."""

from datetime import date
from decimal import Decimal

import pytest

from hacienda_gpt.tax.compensacion import compensar_ahorro
from hacienda_gpt.tax.conjunta import _conjunta, comparar_tributacion, hijos_comunes
from hacienda_gpt.tax.irpf import Descendiente, IRPFInput, IRPFResult, PrevisionSocialMiembro, calcular_irpf
from hacienda_gpt.tax.params import load_params
from hacienda_gpt.tax.profile import DatosInvalidosError

D = Decimal
L = D("0.25")


def _ss(bruto: int) -> Decimal:
    return (D(bruto) * D("0.0635")).quantize(D("0.01"))


def _asalariado(bruto: int, **extra) -> IRPFInput:
    """A salaried earner in Madrid (Social Security at 6,35 % of the gross salary)."""
    return IRPFInput(
        ejercicio=2026,
        comunidad_autonoma="Madrid",
        rendimiento_trabajo_bruto=D(bruto),
        cotizaciones_seguridad_social=_ss(bruto),
        **extra,
    )


def _linea(res: IRPFResult, concepto: str) -> Decimal:
    return sum((linea.importe for linea in res.lineas if linea.concepto == concepto), D(0))


PENSIONES = "Reducción por aportaciones a sistemas de previsión social"


def test_the_limit_is_the_laws() -> None:
    assert load_params(2026).compensacion_ahorro.limite_cruzado == L


@pytest.mark.parametrize(
    ("rend", "gan", "base", "pend_r", "pend_g"),
    [
        (1000, 2000, 3000, 0, 0),  # both positive: nothing to offset
        (4000, -3000, 3000, 0, 2000),  # losses take 25 % of 4.000 = 1.000; 2.000 carried forward
        (4000, -500, 3500, 0, 0),  # small loss fits within the limit
        (-3000, 4000, 3000, 2000, 0),  # the same the other way round
        (-1000, -2000, 0, 1000, 2000),  # both negative: all carried forward
        (0, -5000, 0, 0, 5000),
    ],
)
def test_offsetting_within_the_year(rend: int, gan: int, base: int, pend_r: int, pend_g: int) -> None:
    c = compensar_ahorro(rendimientos=D(rend), ganancias=D(gan), limite_cruzado=L)
    assert (c.base_ahorro, c.rendimientos_negativos_pendientes, c.perdidas_pendientes) == (
        D(base),
        D(pend_r),
        D(pend_g),
    )


def test_losses_from_earlier_years() -> None:
    # Own kind first, without limit: 3.000 of gains absorb 3.000 of the 5.000 of old losses.
    # The other 2.000 go against capital income, capped at 25 % of 2.000 = 500.
    c = compensar_ahorro(rendimientos=D(2000), ganancias=D(3000), perdidas_anteriores=D(5000), limite_cruzado=L)
    assert c.base_ahorro == D(1500) and c.perdidas_pendientes == D(1500) and c.compensado_de_anteriores == D(3500)
    # The year's own loss uses the limit first; old losses get what is left of it (nothing here).
    c = compensar_ahorro(rendimientos=D(4000), ganancias=D(-1000), perdidas_anteriores=D(2000), limite_cruzado=L)
    assert c.base_ahorro == D(3000) and c.perdidas_pendientes == D(2000)
    # Old negative capital income against this year's capital income.
    c = compensar_ahorro(
        rendimientos=D(800), ganancias=D(0), rendimientos_negativos_anteriores=D(300), limite_cruzado=L
    )
    assert c.base_ahorro == D(500) and c.rendimientos_negativos_pendientes == 0


def test_calculator_applies_it_and_reports_what_is_left() -> None:
    base = dict(ejercicio=2026, rendimiento_trabajo_bruto=D(30000), cotizaciones_seguridad_social=D(1900))
    r = calcular_irpf(IRPFInput(**base, rendimiento_capital_mobiliario=D(4000), ganancias_patrimoniales=D(-3000)))
    assert r.base_imponible_ahorro == D("3000.00")  # not 1.000: the loss is capped at 25 % of 4.000
    assert r.perdidas_patrimoniales_a_compensar == D("2000.00") and r.rendimientos_negativos_a_compensar == 0
    assert any("Quedan saldos negativos" in a for a in r.avisos)
    # Realising a loss before year end lowers the tax: the classic lever.
    sin_perdida = calcular_irpf(IRPFInput(**base, ganancias_patrimoniales=D(6000)))
    con_perdida = calcular_irpf(IRPFInput(**base, ganancias_patrimoniales=D(2000)))
    assert con_perdida.cuota_liquida < sin_perdida.cuota_liquida
    # Losses brought forward reduce this year's gains.
    con_pendientes = calcular_irpf(
        IRPFInput(**base, ganancias_patrimoniales=D(6000), perdidas_patrimoniales_pendientes=D(4000))
    )
    assert (
        con_pendientes.base_imponible_ahorro == D("2000.00")
        and con_pendientes.cuota_liquida == con_perdida.cuota_liquida
    )


def test_joint_filing_wins_when_one_spouse_has_no_income() -> None:
    a = IRPFInput(
        ejercicio=2026,
        rendimiento_trabajo_bruto=D(38000),
        cotizaciones_seguridad_social=D(2400),
        retenciones=D(6500),
        num_descendientes=2,
    )
    b = IRPFInput(ejercicio=2026, num_descendientes=2)
    c = comparar_tributacion(a, b)
    assert c.mejor == "conjunta" and c.ahorro > 0
    assert c.total_individual - c.total_conjunta == c.ahorro
    assert c.conjunta.tributacion == "conjunta_biparental"
    # Children are counted once in the joint return, and halved in each individual one.
    assert c.conjunta.minimo_personal_familiar > c.individual_a.minimo_personal_familiar


def test_separate_filing_wins_with_two_similar_salaries() -> None:
    a = IRPFInput(
        ejercicio=2026,
        comunidad_autonoma="Madrid",
        rendimiento_trabajo_bruto=D(40000),
        cotizaciones_seguridad_social=D(2540),
    )
    b = IRPFInput(
        ejercicio=2026,
        comunidad_autonoma="Madrid",
        rendimiento_trabajo_bruto=D(36000),
        cotizaciones_seguridad_social=D(2290),
    )
    c = comparar_tributacion(a, b)
    assert c.mejor == "individual" and c.ahorro > D(1000)
    # The joint return adds both incomes.
    assert c.conjunta.base_imponible_general > c.individual_a.base_imponible_general


def test_joint_comparison_is_available_to_the_agent() -> None:
    from hacienda_gpt.agent.evidence import EvidenceLedger
    from hacienda_gpt.agent.providers.base import ToolCall
    from hacienda_gpt.agent.tools import Toolbox
    from tests.agent.fakes import FakeSearcher

    ledger = EvidenceLedger()
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=ledger)
    assert "comparar_declaracion_conjunta" in {spec.name for spec in toolbox.specs()}
    outcome = toolbox.execute(
        ToolCall(
            id="1",
            name="comparar_declaracion_conjunta",
            arguments={
                "ejercicio": 2026,
                "conyuge_a": {
                    "rendimiento_trabajo_bruto": 38000,
                    "cotizaciones_seguridad_social": 2400,
                    "num_descendientes": 2,
                },
                "conyuge_b": {},
            },
        )
    )
    assert not outcome.is_error and "Sale mejor la declaración conjunta" in outcome.content
    assert len(ledger) == 4  # the verdict and the three returns are all citable
    assert toolbox.execute(ToolCall(id="2", name="comparar_declaracion_conjunta", arguments={})).is_error


def test_self_employed_calculators_are_available_to_the_agent() -> None:
    from hacienda_gpt.agent.evidence import EvidenceLedger
    from hacienda_gpt.agent.providers.base import ToolCall
    from hacienda_gpt.agent.tools import Toolbox
    from tests.agent.fakes import FakeSearcher

    ledger = EvidenceLedger()
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=ledger)
    # 40.000 − 10.000 = 30.000; difficult-to-justify expenses: 5 % = 1.500 → net 28.500.
    actividad = toolbox.execute(
        ToolCall(
            id="1", name="calcular_rendimiento_actividad", arguments={"ingresos_integros": 40000, "otros_gastos": 10000}
        )
    )
    assert not actividad.is_error and "rendimiento_actividad_neto = 28500" in actividad.content
    # First quarter: 20 % of (10.000 − 2.000 − 5 %) = 1.520, less 600 of withholdings.
    pago = toolbox.execute(
        ToolCall(
            id="2",
            name="calcular_modelo_130",
            arguments={
                "trimestre": 1,
                "ingresos_acumulados": 10000,
                "gastos_acumulados": 2000,
                "retenciones_acumuladas": 600,
            },
        )
    )
    assert not pago.is_error and "Modelo 130" in pago.content
    # 21 % of 10.000 = 2.100, less 500 of input VAT.
    iva = toolbox.execute(
        ToolCall(
            id="3",
            name="calcular_modelo_303",
            arguments={"trimestre": 1, "base_tipo_general": 10000, "cuotas_soportadas_deducibles": 500},
        )
    )
    assert not iva.is_error and "resultado = 1600" in iva.content
    assert len(ledger) == 3
    mal = toolbox.execute(ToolCall(id="4", name="calcular_modelo_303", arguments={"trimestre": 9}))
    assert mal.is_error and "trimestre" in mal.content


def test_company_and_wealth_calculators_are_available_to_the_agent() -> None:
    from hacienda_gpt.agent.evidence import EvidenceLedger
    from hacienda_gpt.agent.providers.base import ToolCall
    from hacienda_gpt.agent.tools import Toolbox
    from tests.agent.fakes import FakeSearcher

    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger())
    # Micro company in 2026: 19 % on the first 50.000 and 21 % on the rest: 9.500 + 2.100 = 11.600.
    sociedad = toolbox.execute(
        ToolCall(
            id="1",
            name="calcular_impuesto_sociedades",
            arguments={"ejercicio": 2026, "resultado_contable": 60000, "cifra_negocios_anterior": 300000},
        )
    )
    assert not sociedad.is_error and "11.600" in sociedad.content
    comparacion = toolbox.execute(
        ToolCall(
            id="2",
            name="comparar_autonomo_vs_sociedad",
            arguments={"ejercicio": 2026, "beneficio_antes_retribucion": 80000, "salario": 30000},
        )
    )
    assert not comparacion.is_error and "SOLO SUMA IMPUESTOS" in comparacion.content
    patrimonio = toolbox.execute(
        ToolCall(id="3", name="calcular_patrimonio", arguments={"comunidad": "Madrid", "depositos": 1500000})
    )
    assert not patrimonio.is_error and "Impuesto sobre el Patrimonio" in patrimonio.content
    foral = toolbox.execute(
        ToolCall(id="4", name="calcular_patrimonio", arguments={"comunidad": "Navarra", "depositos": 1})
    )
    assert foral.is_error


def test_inheritance_calculator_is_available_to_the_agent() -> None:
    from hacienda_gpt.agent.evidence import EvidenceLedger
    from hacienda_gpt.agent.providers.base import ToolCall
    from hacienda_gpt.agent.tools import Toolbox
    from tests.agent.fakes import FakeSearcher

    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger())
    hijo = {"modo": "sucesion", "parentesco": "hijo", "edad": 40, "valor_recibido": 300000}
    estatal = toolbox.execute(ToolCall(id="1", name="calcular_sucesiones_donaciones", arguments=hijo))
    madrid = toolbox.execute(
        ToolCall(id="2", name="calcular_sucesiones_donaciones", arguments={**hijo, "comunidad": "Madrid"})
    )
    assert not estatal.is_error and not madrid.is_error
    assert "Impuesto sobre Sucesiones y Donaciones" in estatal.content and estatal.content != madrid.content
    assert toolbox.execute(
        ToolCall(id="3", name="calcular_sucesiones_donaciones", arguments={"modo": "regalo"})
    ).is_error


# --------------------------------------------------------------------------- #
# Rules of the joint return (art. 84 LIRPF)
# --------------------------------------------------------------------------- #


def test_each_spouse_applies_their_own_pension_limits_in_a_joint_return() -> None:
    """Art. 84.2.1.º LIRPF: «Los límites máximos de reducción en la base imponible previstos en los
    artículos 52, 53 y 54 [...] serán aplicados individualmente por cada partícipe o mutualista integrado
    en la unidad familiar». Art. 52.1: the lesser of «a) El 30 por 100 de la suma de los rendimientos
    netos del trabajo y de actividades económicas percibidos individualmente en el ejercicio» and
    «b) 1.500 euros anuales». One limit on the summed contributions understated the reduction when both
    contribute, and overstated it when the contributor has no income of their own."""
    # 30.000 + 12.000, 1.500 each. Own caps: 30 % × (30.000 - 1.905 - 2.000) = 7.828,50 and
    # 30 % × (12.000 - 762 - 2.000) = 2.771,40, both above 1.500: 3.000 reduced (one limit: 1.500).
    a = _asalariado(30000, aportacion_plan_pensiones=D(1500))
    b = _asalariado(12000, aportacion_plan_pensiones=D(1500))
    conjunta = calcular_irpf(_conjunta(a, b))
    assert _linea(conjunta, PENSIONES) == D("-3000.00")
    # Base imponible 42.000 - 2.667 - 2.000 = 37.333; minus 3.400 (art. 84.2.3.º) and 3.000.
    assert conjunta.base_imponible_general == D("37333.00")
    assert conjunta.base_liquidable_general == D("30933.00")
    assert any("1.º, 1500.00 € reducibles de 1500.00 €" in aviso for aviso in conjunta.avisos)

    # 70.000 + 4.000, 1.500 + 1.000: the second spouse's own cap is 30 % × (4.000 - 254 - 2.000) = 523,80.
    a = _asalariado(70000, aportacion_plan_pensiones=D(1500))
    b = _asalariado(4000, aportacion_plan_pensiones=D(1000))
    unida = _conjunta(a, b)
    conjunta = calcular_irpf(unida)
    assert _linea(conjunta, PENSIONES) == D("-2023.80")
    assert conjunta.base_liquidable_general == D("67301.00") - D(3400) - D("2023.80")
    # The same return without each member's figures applies one limit to the sum, and says so.
    sin_desglose = calcular_irpf(unida.model_copy(update={"prevision_social_por_miembro": None}))
    assert _linea(sin_desglose, PENSIONES) == D("-1500.00")
    assert any("prevision_social_por_miembro" in aviso for aviso in sin_desglose.avisos)
    assert conjunta.resultado < sin_desglose.resultado
    # Now joint filing pays for this couple (it did not with the single limit).
    assert comparar_tributacion(a, b).mejor == "conjunta"

    # A spouse with no work or activity income has a 30 % cap of zero: nothing of their 1.500 € reduces.
    sin_rentas = IRPFInput(ejercicio=2026, comunidad_autonoma="Madrid", aportacion_plan_pensiones=D(1500))
    conjunta = calcular_irpf(_conjunta(_asalariado(70000), sin_rentas))
    assert _linea(conjunta, PENSIONES) == 0
    assert any("cinco ejercicios siguientes" in aviso for aviso in conjunta.avisos)


def test_the_members_figures_must_add_up_to_the_joint_return() -> None:
    miembros = [
        PrevisionSocialMiembro(aportacion_plan_pensiones=D(1000), rendimiento_trabajo_bruto=D(20000)),
        PrevisionSocialMiembro(aportacion_plan_pensiones=D(500)),
    ]
    # Totals left at zero are the members' sums.
    inp = IRPFInput(ejercicio=2026, tributacion="conjunta_biparental", prevision_social_por_miembro=miembros)
    assert (inp.aportacion_plan_pensiones, inp.rendimiento_trabajo_bruto) == (D(1500), D(20000))
    with pytest.raises(ValueError, match="no coincide con la suma de los miembros"):
        IRPFInput(
            ejercicio=2026,
            tributacion="conjunta_biparental",
            aportacion_plan_pensiones=D(1000),
            prevision_social_por_miembro=miembros,
        )
    with pytest.raises(ValueError, match="solo se usa en la declaración conjunta"):
        IRPFInput(ejercicio=2026, prevision_social_por_miembro=miembros)


def test_a_joint_return_counts_each_spouses_age_and_disability_in_the_minimum() -> None:
    """Art. 84.2.2.º LIRPF: one minimum of art. 57.1 per return («con independencia del número de miembros»),
    but «Para la cuantificación del mínimo a que se refiere el apartado 2 del artículo 57 y el apartado 1
    del artículo 60 [...] se tendrán en cuenta las circunstancias personales de cada uno de los cónyuges».
    Art. 57: 5.550 €, plus 1.150 € over 65 and 1.400 € more over 75. Art. 60.1: 3.000 € with a disability,
    9.000 € from 65 %, plus 3.000 € of assistance expenses from 65 %."""

    def minimo(a: IRPFInput, b: IRPFInput) -> Decimal:
        return calcular_irpf(_conjunta(a, b)).minimo_personal_familiar

    titular = _asalariado(70000, edad=50)
    conyuge = IRPFInput(ejercicio=2026, comunidad_autonoma="Madrid", edad=70, grado_discapacidad=33)
    resultado = calcular_irpf(_conjunta(titular, conyuge))
    assert resultado.minimo_personal_familiar == D(5550) + D(1150) + D(3000)  # it was 5.550
    assert _linea(resultado, "Incremento del mínimo por la edad del cónyuge (conjunta, art. 84.2.2.º LIRPF)") == D(1150)
    assert _linea(resultado, "Mínimo por discapacidad del cónyuge (conjunta, art. 84.2.2.º LIRPF)") == D(3000)
    mayor = conyuge.model_copy(update={"edad": 80, "grado_discapacidad": 65})
    assert minimo(titular, mayor) == D(5550) + D(1150) + D(1400) + D(9000) + D(3000)
    assert minimo(titular.model_copy(update={"edad": 70}), conyuge.model_copy(update={"edad": 68})) == D(5550) + D(
        1150
    ) + D(1150) + D(3000)
    # The joint return gets it from the second spouse's own figures: no other spouse is counted twice.
    assert _conjunta(titular, conyuge).edad_conyuge == 70
    # In a separate return the spouse's circumstances do not count (they only feed regional deductions).
    solo = calcular_irpf(titular.model_copy(update={"edad_conyuge": 70, "grado_discapacidad_conyuge": 33}))
    assert solo.minimo_personal_familiar == D(5550)


def test_children_given_by_one_spouse_count_by_halves_in_both_separate_returns() -> None:
    """Art. 58.1 LIRPF: the minimum for each descendant «que conviva con el contribuyente» is 2.400 € for
    the first and 2.700 € for the second; art. 58.2 adds 2.800 € under three. Art. 61.1.ª: «Cuando dos o
    más contribuyentes tengan derecho a la aplicación del mínimo por descendientes [...] respecto de los
    mismos [...] descendientes, su importe se prorrateará entre ellos por partes iguales». Two parents
    filing separately apply (2.400 + 2.700 + 2.800) / 2 = 3.950 € each; the joint return, 7.900 €."""
    hijos = [Descendiente(edad=1, gastos={"gastos_guarderia": D(1000)}), Descendiente(edad=6)]
    a = _asalariado(30000, descendientes=hijos)
    b = _asalariado(25000)
    c = comparar_tributacion(a, b)
    compartido = "Mínimo por descendientes (50 %, compartido)"
    assert _linea(c.individual_a, compartido) == _linea(c.individual_b, compartido) == D(3950)
    assert c.individual_b.minimo_personal_familiar == D(5550) + D(3950)  # it was 5.550: no children
    assert _linea(c.conjunta, "Mínimo por descendientes") == D(7900)
    assert any("El cónyuge B no ha indicado hijos" in aviso for aviso in c.avisos)
    # The second spouse gets the children, not their expenses.
    comunes = hijos_comunes(a, b)
    assert comunes.copiados_b and not comunes.copiados_a
    assert comunes.b.descendientes is not None and all(not h.gastos for h in comunes.b.descendientes)
    assert comunes.b.gastos_guarderia == 0 and comunes.a.descendientes_compartidos

    # A spouse who states having no children is not their parent: no minimum, and the parent applies the
    # whole minimum unless they say it is shared with someone else.
    c = comparar_tributacion(a, b, sin_hijos_b=True)
    assert not any(linea.concepto.startswith("Mínimo por descendientes") for linea in c.individual_b.lineas)
    assert _linea(c.individual_a, "Mínimo por descendientes") == D(7900)
    compartido_fuera = a.model_copy(update={"descendientes_compartidos": True})
    c = comparar_tributacion(compartido_fuera, b, sin_hijos_b=True)
    assert _linea(c.individual_a, compartido) == D(3950)


def test_separate_returns_count_the_other_spouses_base_in_family_income_ceilings() -> None:
    """Madrid's birth deduction (art. 4 DLeg. 1/2010) needs the sum of the bases of the family unit under
    61.860 € (art. 18.1). Filing separately, the unit is still both spouses: each return carries the
    other's base imponible as the rest of the unit's."""
    hijo = [Descendiente(edad=0)]
    nacimiento = "Deducción autonómica por nacimiento o adopción de hijos"
    # 26.095 + 40.142,50 = 66.237,50 > 61.860: not for the spouse whose own base is under 30.930 €.
    c = comparar_tributacion(_asalariado(30000, descendientes=hijo), _asalariado(45000))
    assert c.individual_a.base_imponible_general == D("26095.00")
    assert c.individual_b.base_imponible_general == D("40142.50")
    assert _linea(c.individual_a, nacimiento) == 0 and _linea(c.individual_b, nacimiento) == 0
    # 26.095 + 15.570,05 = 41.665,05: both parents deduct half of the 721,70 €.
    c = comparar_tributacion(_asalariado(30000, descendientes=hijo), _asalariado(20000))
    assert _linea(c.individual_a, nacimiento) == _linea(c.individual_b, nacimiento) == D("-360.85")


def test_the_joint_return_merges_the_childrens_detail_given_by_both_spouses() -> None:
    """In the joint return the children are the unit's (art. 82.1.1.ª LIRPF) and the regional caps apply
    child by child: each child's expenses are what both spouses paid for that child."""
    a = _asalariado(
        30000,
        descendientes=[
            Descendiente(fecha_nacimiento=date(2025, 3, 1), gastos={"gastos_guarderia": D(1000)}),
            Descendiente(edad=7),
        ],
    )
    b = _asalariado(
        20000,
        descendientes=[
            Descendiente(edad=7, gastos={"gastos_libros_texto": D(120)}),
            Descendiente(edad=1, etapa_educativa="infantil_primer_ciclo", gastos={"gastos_guarderia": D(500)}),
        ],
    )
    unida = _conjunta(a, b)
    assert unida.descendientes == [
        Descendiente(
            edad=1,
            fecha_nacimiento=date(2025, 3, 1),
            etapa_educativa="infantil_primer_ciclo",
            gastos={"gastos_guarderia": D(1500)},
        ),
        Descendiente(edad=7, gastos={"gastos_libros_texto": D(120)}),
    ]
    assert (unida.gastos_guarderia, unida.gastos_libros_texto, unida.num_descendientes) == (D(1500), D(120), 2)
    # The same joint return as if one spouse had given the whole detail.
    todo_en_a = a.model_copy(
        update={"descendientes": unida.descendientes, "gastos_guarderia": D(1500), "gastos_libros_texto": D(120)}
    )
    sin_hijos_b = b.model_copy(update={"descendientes": None, "gastos_guarderia": D(0), "gastos_libros_texto": D(0)})
    assert calcular_irpf(unida) == calcular_irpf(_conjunta(todo_en_a, sin_hijos_b))

    # Different children, contradicting facts, or expenses that cannot be told apart: refused, not guessed.
    otros = b.model_copy(update={"descendientes": [Descendiente(edad=1), Descendiente(edad=9)]})
    with pytest.raises(DatosInvalidosError, match="no describen los mismos hijos"):
        comparar_tributacion(a, otros)
    distinta_etapa = b.model_copy(
        update={"descendientes": [Descendiente(edad=1, etapa_educativa="infantil_segundo_ciclo"), Descendiente(edad=7)]}
    )
    with pytest.raises(DatosInvalidosError, match="no coinciden entre los dos cónyuges"):
        _conjunta(a.model_copy(update={"descendientes": unida.descendientes}), distinta_etapa)
    gemelos_a = _asalariado(
        30000, descendientes=[Descendiente(edad=3, gastos={"gastos_guarderia": D(1000)}), Descendiente(edad=3)]
    )
    gemelos_b = _asalariado(
        20000, descendientes=[Descendiente(edad=3), Descendiente(edad=3, gastos={"gastos_guarderia": D(1000)})]
    )
    with pytest.raises(DatosInvalidosError, match="no se puede saber a cuál"):
        _conjunta(gemelos_a, gemelos_b)
    # Twins whose expenses only one spouse gives are no problem.
    solo_a = _conjunta(
        gemelos_a, gemelos_b.model_copy(update={"descendientes": [Descendiente(edad=3)] * 2, "gastos_guarderia": D(0)})
    )
    assert solo_a.descendientes == gemelos_a.descendientes


def test_amounts_and_limits_apply_once_to_the_summed_income_of_a_joint_return() -> None:
    """Art. 84.2 LIRPF: «Los importes y límites cuantitativos establecidos a efectos de la tributación
    individual se aplicarán en idéntica cuantía en la tributación conjunta, sin que proceda su elevación
    o multiplicación en función del número de miembros de la unidad familiar». Only arts. 52-54 (84.2.1.º)
    and the minimums of arts. 57.2 and 60.1 (84.2.2.º) go member by member, so the 2.000 € of art. 19.2.f),
    the art. 20 reduction (net work income under 19.747,5 €), the deduction of DA 61.ª (gross work income
    under 20.048,45 €) and the art. 68.6 rent deduction (base imponible under 33.007,20 €) are applied once,
    to the unit's summed income."""
    # 30.000 + 12.000: one 2.000 € and no art. 20 reduction on 42.000 - 2.667 = 39.333 €, while the 12.000 €
    # salary alone reduces 7.302 € (net 11.238 € ≤ 14.852 €).
    c = comparar_tributacion(_asalariado(30000), _asalariado(12000))
    assert _linea(c.conjunta, "Otros gastos deducibles") == D(-2000)
    assert _linea(c.conjunta, "Reducción por obtención de rendimientos del trabajo") == 0
    assert _linea(c.individual_b, "Reducción por obtención de rendimientos del trabajo") == D(-7302)
    assert c.conjunta.base_imponible_general == D("37333.00")
    assert c.mejor == "individual"
    # 70.000 + 4.000: 74.000 - 4.699 - 2.000.
    c = comparar_tributacion(_asalariado(70000), _asalariado(4000))
    assert _linea(c.conjunta, "Otros gastos deducibles") == D(-2000)
    assert c.conjunta.base_imponible_general == D("67301.00")

    # DA 61.ª: each 17.000 € salary alone gets the deduction; 34.000 € together do not.
    c = comparar_tributacion(_asalariado(17000), _asalariado(17000))
    deduccion = "Deducción por obtención de rendimientos del trabajo"
    assert _linea(c.individual_a, deduccion) < 0 and _linea(c.individual_b, deduccion) < 0
    assert _linea(c.conjunta, deduccion) == 0

    # Art. 68.6: the spouse who pays 6.000 € of rent deducts 600 € with a base imponible of 15.570,05 €;
    # the joint base imponible, 40.000 - 2.540 - 2.000 = 35.460 €, is above 33.007,20 €.
    c = comparar_tributacion(_asalariado(20000, alquiler_vivienda_habitual=D(6000)), _asalariado(20000))
    alquiler = "Deducción estatal por alquiler de vivienda habitual"
    assert c.individual_a.base_imponible_general == D("15570.05")
    assert _linea(c.individual_a, alquiler) == D(-600)
    assert c.conjunta.base_imponible_general == D("35460.00") and _linea(c.conjunta, alquiler) == 0


def test_a_single_parent_joint_return_says_the_reduction_needs_not_living_with_the_other_parent() -> None:
    """Art. 84.2.4.º LIRPF: the 2.150 € reduction «No se aplicará [...] cuando el contribuyente conviva con el
    padre o la madre de alguno de los hijos que forman parte de su unidad familiar»; it is not checked."""
    resultado = calcular_irpf(_asalariado(30000, tributacion="conjunta_monoparental", num_descendientes=1))
    assert _linea(resultado, "Reducción por tributación conjunta") == D(-2150)
    assert any("art. 84.2.4.º LIRPF" in aviso for aviso in resultado.avisos)


def test_shared_facts_of_the_household_are_not_added_up_in_the_joint_return() -> None:
    """Amounts add up in the joint return (art. 84.5 LIRPF: rents «serán gravadas acumuladamente»), but a
    year, a population, days of the year or the household's counts are one fact both spouses share: adding
    them used to give 4.040 as a year or 730 days (an invalid return)."""
    comun = {
        "ejercicio_adquisicion_vivienda": 2020,
        "dias_alquiler_vivienda_habitual": 365,
        "habitantes_municipio": 3000,
        "miembros_unidad_familiar": 4,
    }
    unida = _conjunta(
        _asalariado(30000, edad=40, edad_conyuge=38, **comun), _asalariado(20000, edad=38, edad_conyuge=40, **comun)
    )
    assert {campo: getattr(unida, campo) for campo in comun} == comun
    assert (unida.edad, unida.edad_conyuge) == (40, 38)
    assert unida.rendimiento_trabajo_bruto == D(50000)
