"""The deterministic optimiser: combinations beat lever-by-lever, constraints hold, same input same plan."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall
from hacienda_gpt.agent.tools import OPTIMIZE_TOOL, Toolbox
from hacienda_gpt.api.api import app
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType
from hacienda_gpt.tax.conjunta import _conjunta
from hacienda_gpt.tax.irpf import Descendiente, IRPFInput, calcular_irpf
from hacienda_gpt.tax.optimizador import (
    MAX_EVALUACIONES,
    TEXTOS_LEGALES,
    PlanOptimo,
    PosicionLatente,
    Restricciones,
    optimizar,
    optimizar_desde_hechos,
)
from hacienda_gpt.tax.params import load_params
from hacienda_gpt.tax.profile import DatosInvalidosError

D = Decimal
P = load_params(2026)
NOV = date(2026, 11, 13)
ENERO = date(2027, 1, 15)
SI = Restricciones(acepta_iliquidez=True)


def _ss(bruto: int) -> Decimal:
    return (D(bruto) * D("0.0635")).quantize(D("0.01"))


def _asalariado(bruto: int, comunidad: str = "Madrid", **extra) -> IRPFInput:
    return IRPFInput(
        ejercicio=2026,
        comunidad_autonoma=comunidad,
        rendimiento_trabajo_bruto=D(bruto),
        cotizaciones_seguridad_social=_ss(bruto),
        **extra,
    )


def _palanca(plan: PlanOptimo, lever_id: str):
    return next(p for p in plan.palancas if p.id == lever_id)


# --------------------------------------------------------------------------- #
# Households of the tests
# --------------------------------------------------------------------------- #

# Aragón, one child at nursery: the guardería deduction (art. 110-17) needs a base liquidable below
# 35.000 €. Base 36.500,79 €: the 1.000 € the user can put in a pension plan, or the 1.000 € of loss
# they can realise, leave it above the ceiling; both together bring it under.
ARAGON = IRPFInput(
    ejercicio=2026,
    comunidad_autonoma="Aragón",
    rendimiento_trabajo_bruto=D(39830),
    cotizaciones_seguridad_social=D("2529.21"),
    ganancias_patrimoniales=D(1200),
    descendientes=[Descendiente(edad=1, gastos={"gastos_guarderia": D(2000)})],
    edad=35,
    retenciones=D(6000),
)
ARAGON_RESTRICCIONES = Restricciones(
    aportacion_maxima_pensiones=D(1000),
    acepta_iliquidez=True,
    perdidas_latentes=[PosicionLatente(descripcion="Fondo indexado", importe=D(1000))],
)

# A high earner and a spouse who earns little: filing separately the spouse pays nothing, so a pension
# contribution of theirs is worthless; filing jointly it lowers the common base.
ALTO = _asalariado(70000, retenciones=D(15000))
BAJO = _asalariado(4000)


def test_the_combination_beats_the_sum_of_standalone_savings_through_a_regional_ceiling() -> None:
    base = calcular_irpf(ARAGON)
    assert base.base_liquidable_general + base.base_liquidable_ahorro == D("36500.79")

    plan = optimizar(ARAGON, fecha=NOV, restricciones=ARAGON_RESTRICCIONES)

    pension = _palanca(plan, "plan_pensiones_contribuyente")
    perdida = _palanca(plan, "perdidas_contribuyente")
    assert (pension.importe, perdida.importe) == (D(1000), D(1000))
    guarderia = "Deducción autonómica: Gastos de guardería de hijos menores de 3 años"
    optimo = plan.optimo.declaraciones[0].resultado
    assert optimo.base_liquidable_general + optimo.base_liquidable_ahorro < D(35000)
    assert any(line.concepto == guarderia and line.importe == D(-250) for line in optimo.lineas)

    # One change at a time (what the benefit catalogue does) never crosses the ceiling: each lever alone
    # leaves the deduction out, so adding up their savings misses it.
    solos = [
        calcular_irpf(ARAGON.model_copy(update={"aportacion_plan_pensiones": D(1000)})),
        calcular_irpf(ARAGON.model_copy(update={"ganancias_patrimoniales": D(200)})),
    ]
    assert all(guarderia not in [line.concepto for line in r.lineas] for r in solos)
    suma_separada = sum((base.resultado - r.resultado for r in solos), D(0))
    assert suma_separada == plan.suma_ahorros_aislados == pension.ahorro_aislado + perdida.ahorro_aislado
    assert plan.ahorro_total == base.resultado - optimo.resultado == suma_separada + D(250)
    assert plan.interaccion == D(250)
    # Inside the plan each lever is worth more than alone, by the deduction it helps unlock.
    assert pension.ahorro_marginal == pension.ahorro_aislado + D(250)
    assert perdida.ahorro_marginal == perdida.ahorro_aislado + D(250)
    # Why, from the trace: the deduction only the combination applies.
    assert any(
        guarderia in motivo and "solo se aplica con la combinación" in motivo for motivo in plan.explicacion_interaccion
    )
    assert "Juntas ahorran 250,00 € más que la suma de cada una por separado" in plan.explicacion
    assert any(c.concepto == guarderia and c.antes == 0 and c.despues == D(-250) for c in plan.cambios_traza)


def test_joint_filing_flips_the_best_pension_allocation_and_a_greedy_search_misses_it() -> None:
    plan = optimizar(ALTO, conyuge=BAJO, fecha=NOV, restricciones=SI)
    assert plan.base.tributacion == "individual" and plan.optimo.tributacion == "conjunta_biparental"
    conjunta = _palanca(plan, "tributacion")
    propia = _palanca(plan, "plan_pensiones_contribuyente")
    del_conyuge = _palanca(plan, "plan_pensiones_conyuge")
    # Each spouse's own limit (art. 84.2.1.º): 30 % of the spouse's net work income.
    neto_conyuge = D(4000) - _ss(4000) - P.trabajo.otros_gastos
    assert del_conyuge.importe == (P.prevision_social.porcentaje_rendimientos * neto_conyuge).quantize(D("0.01"))
    assert propia.importe == P.prevision_social.limite_general
    # Filing separately the spouse pays no tax: the best allocation puts nothing in their plan...
    assert del_conyuge.ahorro_aislado == 0 and del_conyuge.accion_aislada.startswith("Aportar 0,00 €")
    # ...and joint filing alone costs money.
    assert conjunta.ahorro_aislado < 0
    # A lever-by-lever search keeps separate returns and only the user's contribution.
    voraz = sum((p.ahorro_aislado for p in plan.palancas if p.ahorro_aislado > 0), D(0))
    assert voraz == propia.ahorro_aislado < plan.ahorro_total
    assert conjunta.ahorro_marginal > 0 and del_conyuge.ahorro_marginal > 0
    assert sum(1 for m in plan.explicacion_interaccion if "una búsqueda palanca a palanca no la elegiría" in m) == 2
    # The joint return reduces both contributions, beyond the single 1.500 € a summed return would allow.
    (decl,) = plan.optimo.declaraciones
    reduccion = next(
        linea for linea in decl.resultado.lineas if linea.concepto.startswith("Reducción por aportaciones")
    )
    assert -reduccion.importe == propia.importe + del_conyuge.importe > P.prevision_social.limite_general

    # After 31 December the contributions are gone, and joint filing alone does not pay.
    enero = optimizar(ALTO, conyuge=BAJO, fecha=ENERO, restricciones=SI)
    assert enero.optimo.tributacion == "individual" and enero.palancas == []
    # With a little more income the joint return stops paying even with the spouse's plan: back to separate
    # returns, and the spouse's allocation flips back to nothing.
    otro = optimizar(ALTO, conyuge=_asalariado(4500), fecha=NOV, restricciones=SI)
    assert otro.optimo.tributacion == "individual"
    assert [p.id for p in otro.palancas] == ["plan_pensiones_contribuyente"]


def test_joint_return_with_per_member_limits_matches_the_calculator_where_one_limit_suffices() -> None:
    """The joint return hands the calculator the sum of each member's limit (art. 84.2.1.º); when only
    one member contributes within its own limit, that is exactly what the plain joint return computes."""
    sin_rentas = IRPFInput(ejercicio=2026, comunidad_autonoma="Madrid")
    plan = optimizar(ALTO, conyuge=sin_rentas, fecha=NOV, restricciones=SI)
    assert plan.optimo.tributacion == "conjunta_biparental"
    assert [(p.id, p.importe) for p in plan.palancas] == [
        ("tributacion", None),
        ("plan_pensiones_contribuyente", D(1500)),
    ]
    (decl,) = plan.optimo.declaraciones
    directo = calcular_irpf(_conjunta(ALTO.model_copy(update={"aportacion_plan_pensiones": D(1500)}), sin_rentas))
    assert decl.resultado.resultado == directo.resultado
    assert [(linea.concepto, linea.importe) for linea in decl.resultado.lineas] == [
        (linea.concepto, linea.importe) for linea in directo.lineas
    ]


def test_the_25_percent_cap_of_art_49_limits_loss_harvesting() -> None:
    con_intereses = _asalariado(30000, rendimiento_capital_mobiliario=D(8000))
    restricciones = Restricciones(acepta_iliquidez=False, perdidas_latentes=[PosicionLatente(importe=D(5000))])
    plan = optimizar(con_intereses, fecha=NOV, restricciones=restricciones)

    limite = P.compensacion_ahorro.limite_cruzado
    (perdida,) = plan.palancas
    # No gains to offset: the losses only absorb 25 % of the capital income; the rest would be carried forward.
    assert perdida.importe == limite * D(8000) == D(2000)
    optimo = plan.optimo.declaraciones[0].resultado
    assert optimo.base_imponible_ahorro == D(6000)
    assert any(
        linea.concepto.startswith("Compensación entre rendimientos y ganancias") and linea.importe == D(-2000)
        for linea in optimo.lineas
    )
    # Realising all 5.000 € would save no more this year.
    todo = calcular_irpf(con_intereses.model_copy(update={"ganancias_patrimoniales": D(-5000)}))
    assert todo.resultado == optimo.resultado
    assert perdida.naturaleza == "diferimiento"
    assert any(TEXTOS_LEGALES["art. 33.5.f) LIRPF"].texto in r for r in perdida.requisitos)
    assert "dos meses" in " ".join(perdida.requisitos)
    assert any("art. 49" in r for r in perdida.requisitos)


def test_after_31_december_only_the_filing_choices_remain() -> None:
    sin_rentas = _asalariado(0).model_copy(
        update={"rendimiento_trabajo_bruto": D(0), "cotizaciones_seguridad_social": D(0)}
    )
    restricciones = Restricciones(
        acepta_iliquidez=True,
        donativo_previsto=D(300),
        perdidas_latentes=[PosicionLatente(importe=D(1000))],
    )
    con = _asalariado(40000, ganancias_patrimoniales=D(3000))
    antes = optimizar(con, conyuge=sin_rentas, fecha=date(2026, 12, 31), restricciones=restricciones)
    assert {p.id for p in antes.palancas} >= {
        "tributacion",
        "plan_pensiones_contribuyente",
        "perdidas_contribuyente",
        "donativo",
    }

    plan = optimizar(con, conyuge=sin_rentas, fecha=ENERO, restricciones=restricciones)
    assert plan.fase == "solo_opciones_de_declaracion"
    assert [p.id for p in plan.palancas] == ["tributacion"]
    assert plan.optimo.tributacion == "conjunta_biparental"
    causas = {nd.id: nd.causa for nd in plan.no_disponibles}
    assert causas["plan_pensiones_contribuyente"] == causas["perdidas_contribuyente"] == causas["donativo"] == "fecha"


@pytest.mark.parametrize(
    ("inp", "conyuge", "restricciones"),
    [
        (ARAGON, None, ARAGON_RESTRICCIONES),
        (ALTO, BAJO, Restricciones(acepta_iliquidez=True, aportacion_maxima_pensiones=D(700))),
        (
            _asalariado(52000, comunidad="Comunidad Valenciana", ganancias_patrimoniales=D(900)),
            _asalariado(21000, comunidad="Comunidad Valenciana"),
            Restricciones(
                aportacion_maxima_pensiones=D(1200),
                aportacion_maxima_pensiones_conyuge=D(5000),
                donativo_previsto=D(150),
                presupuesto_donativos=D(100000),
                perdidas_latentes=[
                    PosicionLatente(importe=D(400)),
                    PosicionLatente(importe=D(300), titular="conyuge", tipo="valores_no_cotizados"),
                ],
            ),
        ),
    ],
)
def test_constraints_and_legal_limits_are_never_exceeded(inp, conyuge, restricciones) -> None:
    plan = optimizar(inp, conyuge=conyuge, fecha=NOV, restricciones=restricciones)
    ps = P.prevision_social
    for p in plan.palancas:
        if p.id == "plan_pensiones_contribuyente":
            assert p.importe <= (restricciones.aportacion_maxima_pensiones or ps.limite_general)
            assert inp.aportacion_plan_pensiones + p.importe <= ps.limite_general
        if p.id == "plan_pensiones_conyuge":
            assert p.importe <= ps.limite_general
            if restricciones.aportacion_maxima_pensiones_conyuge is not None:
                assert p.importe <= restricciones.aportacion_maxima_pensiones_conyuge
        if p.id == "donativo":
            assert restricciones.donativo_previsto <= p.importe <= restricciones.tope_donativos
        if p.id.startswith("perdidas_"):
            titular = p.id.removeprefix("perdidas_")
            assert p.importe <= sum(x.importe for x in restricciones.perdidas_latentes if x.titular == titular)
    assert plan.ahorro_total >= 0
    assert plan.busqueda.evaluaciones <= MAX_EVALUACIONES + 400  # the per-lever analysis runs after the cap


def test_a_large_donation_budget_is_trimmed_to_what_still_saves() -> None:
    inp = _asalariado(52000, comunidad="Madrid")
    presupuesto = Restricciones(acepta_iliquidez=False, donativo_previsto=D(150), presupuesto_donativos=D(100000))
    plan = optimizar(inp, fecha=NOV, restricciones=presupuesto)
    donativo = _palanca(plan, "donativo")
    # Never all of the budget: past the caps (10 % of the base liquidable, for the state and the Madrid
    # deductions) a euro more saves nothing, so the plan stops there.
    resultado = plan.optimo.declaraciones[0].resultado
    tope = P.donativos.limite_base_liquidable * (resultado.base_liquidable_general + resultado.base_liquidable_ahorro)
    assert D(150) <= donativo.importe <= tope + 1 < D(100000)
    menos = optimizar(
        inp, fecha=NOV, restricciones=presupuesto.model_copy(update={"presupuesto_donativos": donativo.importe - 1})
    )
    assert menos.optimo.resultado_total > plan.optimo.resultado_total
    assert donativo.naturaleza == "requiere_gastar"
    assert donativo.coste_neto == donativo.importe - donativo.ahorro_marginal > 0
    assert donativo.ahorro_por_euro == (donativo.ahorro_marginal / donativo.importe).quantize(D("0.0001"))


def test_the_recipient_of_a_planned_donation_is_chosen_from_the_regional_deductions() -> None:
    inp = _asalariado(40000, comunidad="Comunidad Valenciana")
    plan = optimizar(inp, fecha=NOV, restricciones=Restricciones(donativo_previsto=D(600)))
    donativo = _palanca(plan, "donativo")
    assert donativo.importe == D(600)  # what they will donate anyway, not a euro more
    assert donativo.opcion is not None and donativo.opcion.startswith("autonomica:")
    assert "más que donarlo a cualquier entidad de la Ley 49/2002" in donativo.explicacion
    assert any("no se ha comprobado" in r for r in donativo.requisitos)
    resultado = plan.optimo.declaraciones[0].resultado
    assert any(linea.concepto.startswith("Deducción autonómica: Donaciones") for linea in resultado.lineas)
    assert any(linea.concepto == "Deducción por donativos" for linea in resultado.lineas)


def test_regional_donations_whose_base_crosses_the_state_one_are_not_offered() -> None:
    plan = optimizar(
        _asalariado(40000, comunidad="Murcia"), fecha=NOV, restricciones=Restricciones(donativo_previsto=D(600))
    )
    assert _palanca(plan, "donativo").opcion == "ley_49_2002"
    assert any(d.id.startswith("donativo_autonomico:") for d in plan.descartadas)


def test_the_recurring_donation_rate_needs_at_least_last_years_amount() -> None:
    # Asturias has no regional donation deduction in the data: only the state recipients compete.
    inp = _asalariado(40000, comunidad="Asturias")
    restricciones = Restricciones(
        donativo_previsto=D(500), presupuesto_donativos=D(700), donativo_recurrente_importe_anterior=D(600)
    )
    donativo = _palanca(optimizar(inp, fecha=NOV, restricciones=restricciones), "donativo")
    assert donativo.opcion == "ley_49_2002_recurrente" and donativo.importe >= D(600)
    assert any(TEXTOS_LEGALES["art. 19.1 Ley 49/2002"].texto in r for r in donativo.requisitos)
    # Last year's amount is beyond the budget: the recurring rate is out of reach.
    sin = optimizar(
        inp, fecha=NOV, restricciones=restricciones.model_copy(update={"donativo_recurrente_importe_anterior": D(800)})
    )
    assert _palanca(sin, "donativo").opcion == "ley_49_2002"


def test_the_donor_spouse_is_the_one_whose_return_can_use_the_deduction() -> None:
    plan = optimizar(
        _asalariado(9000),  # the user pays no tax
        conyuge=_asalariado(45000),
        fecha=NOV,
        restricciones=Restricciones(acepta_iliquidez=False, donativo_previsto=D(250)),
    )
    donativo = _palanca(plan, "donativo")
    assert plan.optimo.tributacion == "individual"
    assert "lo paga tu cónyuge" in donativo.accion


def test_latent_gains_absorbed_by_pending_losses_are_flagged_as_free_this_year() -> None:
    inp = _asalariado(35000, perdidas_patrimoniales_pendientes=D(3000))
    plan = optimizar(
        inp,
        fecha=NOV,
        restricciones=Restricciones(acepta_iliquidez=False, ganancias_latentes=[PosicionLatente(importe=D(5000))]),
    )
    assert plan.palancas == []
    (oportunidad,) = plan.oportunidades
    assert "hasta 3.000,00 € de plusvalías latentes" in oportunidad and "sin pagar más este año" in oportunidad


def test_two_runs_give_the_same_plan() -> None:
    restricciones = Restricciones(
        acepta_iliquidez=True,
        donativo_previsto=D(300),
        presupuesto_donativos=D(1200),
        perdidas_latentes=[PosicionLatente(importe=D(1800)), PosicionLatente(importe=D(900), titular="conyuge")],
    )
    a = _asalariado(
        36000,
        comunidad="Comunidad Valenciana",
        ganancias_patrimoniales=D(2500),
        alquiler_vivienda_habitual=D(7200),
        edad=34,
    )
    b = _asalariado(24000, comunidad="Comunidad Valenciana", ganancias_patrimoniales=D(600))
    primero = optimizar(a, conyuge=b, fecha=NOV, restricciones=restricciones)
    segundo = optimizar(a, conyuge=b, fecha=NOV, restricciones=restricciones)
    assert primero.model_dump_json() == segundo.model_dump_json()


def test_the_plan_does_not_depend_on_the_hash_seed() -> None:
    script = (
        "from datetime import date; from decimal import Decimal as D; import hashlib;"
        "from hacienda_gpt.tax.irpf import IRPFInput;"
        "from hacienda_gpt.tax.optimizador import optimizar, Restricciones, PosicionLatente;"
        "a = IRPFInput(ejercicio=2026, comunidad_autonoma='Cataluña', rendimiento_trabajo_bruto=D(48000),"
        " cotizaciones_seguridad_social=D(3048), ganancias_patrimoniales=D(1500));"
        "b = IRPFInput(ejercicio=2026, comunidad_autonoma='Cataluña', rendimiento_trabajo_bruto=D(12000));"
        "r = Restricciones(donativo_previsto=D(400), perdidas_latentes=[PosicionLatente(importe=D(800))]);"
        "print(hashlib.sha256(optimizar(a, conyuge=b, fecha=date(2026, 11, 13), restricciones=r)"
        ".model_dump_json().encode()).hexdigest())"
    )
    raiz = Path(__file__).resolve().parents[2]
    salidas = {
        subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": semilla},
            cwd=raiz,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for semilla in ("1", "4242")
    }
    assert len(salidas) == 1


def test_brute_force_on_small_grids_never_beats_the_optimum() -> None:
    plan = optimizar(ARAGON, fecha=NOV, restricciones=ARAGON_RESTRICCIONES)
    assert plan.busqueda.metodo == "exhaustiva"
    rejilla = [D(v) for v in range(0, 1001, 50)]
    totales = {
        (x, perdida): calcular_irpf(
            ARAGON.model_copy(update={"aportacion_plan_pensiones": x, "ganancias_patrimoniales": D(1200) - perdida})
        ).resultado
        for x, perdida in itertools.product(rejilla, rejilla)
    }
    mejor = min(totales.values())
    assert plan.optimo.resultado_total <= mejor
    # The optimum lies on this grid (both amounts at their maximum): exhaustive search found exactly it.
    assert plan.optimo.resultado_total == mejor == totales[(D(1000), D(1000))]


def test_brute_force_for_a_couple_within_what_the_plain_calculator_can_check() -> None:
    plan = optimizar(ALTO, conyuge=BAJO, fecha=NOV, restricciones=SI)
    hijos = False
    individuales = [x.model_copy(update={"descendientes_compartidos": hijos}) for x in (ALTO, BAJO)]
    rejilla = [D(v) for v in range(0, 1501, 100)]
    puntos = []
    for xa, xb in itertools.product(rejilla, rejilla):
        a = individuales[0].model_copy(update={"aportacion_plan_pensiones": xa})
        b = individuales[1].model_copy(update={"aportacion_plan_pensiones": xb})
        # Separate returns (each with the other's base as the rest of the family unit, as the optimiser does).
        bi = lambda r: r.base_imponible_general + r.base_imponible_ahorro  # noqa: E731
        ra, rb = calcular_irpf(a), calcular_irpf(b)
        a = a.model_copy(update={"base_imponible_resto_unidad_familiar": bi(rb)})
        b = b.model_copy(update={"base_imponible_resto_unidad_familiar": bi(ra)})
        puntos.append(calcular_irpf(a).resultado + calcular_irpf(b).resultado)
        # Joint return, only where one 1.500 € limit on the sum is what the law gives (each within its own cap).
        if xa + xb <= 1500 and xb <= D("523.80"):
            puntos.append(calcular_irpf(_conjunta(a, b)).resultado)
    assert plan.optimo.resultado_total <= min(puntos)


def test_every_chosen_lever_is_explained_and_cited() -> None:
    planes = [
        optimizar(ARAGON, fecha=NOV, restricciones=ARAGON_RESTRICCIONES),
        optimizar(ALTO, conyuge=BAJO, fecha=NOV, restricciones=SI),
        optimizar(
            _asalariado(40000, comunidad="Comunidad Valenciana"),
            fecha=NOV,
            restricciones=Restricciones(donativo_previsto=D(600)),
        ),
    ]
    for plan in planes:
        assert plan.palancas
        for p in plan.palancas:
            assert p.referencia and p.referencia in plan.citas
            assert p.requisitos and p.explicacion and p.accion in plan.explicacion
            assert p.naturaleza in ("ahorro_definitivo", "diferimiento", "requiere_gastar")
        for texto in plan.textos_legales:
            assert texto.boe_id.startswith("BOE-A-")
            assert any(texto.texto in r for p in plan.palancas for r in p.requisitos) or texto.referencia.startswith(
                "art. 83"
            )
        assert plan.cambios_traza and plan.explicacion.startswith("Con los datos actuales")
        assert all(d.referencia and d.motivo for d in plan.descartadas)
        assert plan.busqueda.nota in plan.explicacion


def test_levers_that_need_missing_data_or_were_ruled_out_are_listed() -> None:
    plan = optimizar(
        _asalariado(30000), fecha=NOV, estado_civil="casado", restricciones=Restricciones(acepta_iliquidez=False)
    )
    causas = {nd.id: (nd.causa, nd.datos_que_faltan) for nd in plan.no_disponibles}
    assert causas["tributacion"] == ("faltan_datos", ["conyuge"])
    assert causas["plan_pensiones_contribuyente"][0] == "restricciones"
    assert causas["donativo"][0] == "restricciones"
    assert causas["perdidas_contribuyente"] == ("faltan_datos", ["perdidas_latentes"])
    assert {d.id for d in plan.descartadas} >= {"plan_pensiones_del_conyuge", "reparto_minimo_descendientes"}
    assert "retenciones" in plan.datos_que_faltan
    assert plan.palancas == [] and "No hay ninguna combinación" in plan.explicacion


def test_a_single_parent_can_compare_the_joint_return() -> None:
    inp = _asalariado(45000, num_descendientes=1, familia_monoparental=True)
    plan = optimizar(inp, fecha=NOV, restricciones=SI)
    assert plan.optimo.tributacion == "conjunta_monoparental"
    assert _palanca(plan, "tributacion").naturaleza == "ahorro_definitivo"


def test_spouse_positions_without_spouse_are_refused() -> None:
    with pytest.raises(DatosInvalidosError, match="cónyuge"):
        optimizar(
            _asalariado(30000), fecha=NOV, restricciones={"perdidas_latentes": [{"importe": 10, "titular": "conyuge"}]}
        )
    with pytest.raises(DatosInvalidosError, match="presupuesto"):
        optimizar(_asalariado(30000), fecha=NOV, restricciones={"donativo_previsto": 100, "presupuesto_donativos": 10})


def test_from_case_facts_like_the_simulator() -> None:
    plan = optimizar_desde_hechos(
        ejercicio=2026,
        fecha=NOV,
        facts={
            "rendimiento_trabajo_bruto": 70000.0,
            "cotizaciones_seguridad_social": 4445.0,
            "comunidad_autonoma": "Madrid",
            "estado_civil": "casado",
        },
        conyuge={"rendimiento_trabajo_bruto": 4000, "cotizaciones_seguridad_social": 254},
        restricciones={"acepta_iliquidez": True},
    )
    assert plan.con_conyuge and plan.optimo.tributacion == "conjunta_biparental"
    assert "retenciones" in plan.datos_que_faltan
    with pytest.raises(DatosInvalidosError):
        optimizar_desde_hechos(ejercicio=2026, fecha=NOV, datos={"campo_raro": 1})


# --------------------------------------------------------------------------- #
# Agent tool
# --------------------------------------------------------------------------- #


class _NoSearch:
    def search(self, *args, **kwargs):  # pragma: no cover - never called
        return []


def _case(**facts) -> CaseState:
    return CaseState(
        case_id="c-opt",
        user_id="u",
        jurisdiction="ES",
        tax_period="2026",
        facts=[
            Fact(
                fact_id=f"f_{name}",
                name=name,
                value=value,
                value_type=FactValueType.STRING if isinstance(value, str) else FactValueType.NUMBER,
                source="test",
                confidence=1.0,
            )
            for name, value in facts.items()
        ],
    )


def test_the_tool_reads_the_case_and_registers_citable_fragments(monkeypatch) -> None:
    import hacienda_gpt.agent.tools as tools

    monkeypatch.setattr(tools, "hoy_en_espana", lambda: NOV)
    ledger = EvidenceLedger()
    toolbox = Toolbox(
        searcher=_NoSearch(),
        ledger=ledger,
        case=_case(
            rendimiento_trabajo_bruto=70000.0, cotizaciones_seguridad_social=4445.0, comunidad_autonoma="Madrid"
        ),
    )
    assert OPTIMIZE_TOOL in toolbox.specs()
    outcome = toolbox.execute(
        ToolCall(
            id="1",
            name="optimizar_impuestos",
            arguments={
                "conyuge": {"rendimiento_trabajo_bruto": 4000, "cotizaciones_seguridad_social": 254},
                "acepta_iliquidez": True,
                "perdidas_latentes": [{"importe": 500, "descripcion": "ETF"}],
            },
        )
    )
    assert not outcome.is_error, outcome.content
    assert outcome.content.startswith("[F1] Plan óptimo del IRPF 2026")
    assert outcome.summary.startswith("ahorro 721,46 € con 3 medida(s): F1, F2, F3, F4")
    # The plan summary, then the current returns (two) and the optimal one (joint).
    assert len(ledger) == 4
    assert (
        "Presentar declaración conjunta" in outcome.content
        and "no se suman los ahorros por separado" in outcome.content
    )
    assert "721,46 €" in outcome.content
    assert "Situación actual (cónyuge)" in outcome.content and "Plan óptimo (conjunta)" in outcome.content

    malo = toolbox.execute(ToolCall(id="2", name="optimizar_impuestos", arguments={"conyuge": 3}))
    assert malo.is_error
    invalido = toolbox.execute(ToolCall(id="3", name="optimizar_impuestos", arguments={"presupuesto_donativos": -5}))
    assert invalido.is_error and "presupuesto_donativos: debe ser mayor o igual que 0" in invalido.content


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

client = TestClient(app)
DATOS = {"rendimiento_trabajo_bruto": 70000, "cotizaciones_seguridad_social": 4445, "comunidad_autonoma": "Madrid"}


def test_optimize_endpoint_returns_the_plan() -> None:
    respuesta = client.post(
        "/optimize",
        json={
            "ejercicio": 2026,
            "fecha": "2026-11-13",
            "datos": DATOS,
            "conyuge": {"rendimiento_trabajo_bruto": 4000, "cotizaciones_seguridad_social": 254},
            "restricciones": {"acepta_iliquidez": True},
        },
    )
    assert respuesta.status_code == 200, respuesta.text
    plan = PlanOptimo.model_validate(respuesta.json())
    assert plan.ahorro_total == D("721.46") and plan.optimo.tributacion == "conjunta_biparental"
    assert [p.id for p in plan.palancas] == ["tributacion", "plan_pensiones_contribuyente", "plan_pensiones_conyuge"]

    # From a case, like /simulate.
    case_id = client.post("/cases", json={"user_id": "u-opt", "jurisdiction": "ES", "tax_period": "2026"}).json()[
        "case_id"
    ]
    for nombre, valor in DATOS.items():
        assert client.put(f"/cases/{case_id}/facts/{nombre}", json={"value": valor}).status_code == 200
    con_caso = client.post("/optimize", json={"case_id": case_id, "fecha": "2026-11-13"})
    assert con_caso.status_code == 200
    assert D(con_caso.json()["base"]["resultado_total"]) == calcular_irpf(_asalariado(70000)).resultado


def test_optimize_endpoint_answers_422_with_a_readable_message() -> None:
    def detalle(cuerpo: dict) -> str:
        respuesta = client.post("/optimize", json=cuerpo)
        assert respuesta.status_code == 422, respuesta.text
        assert isinstance(respuesta.json()["detail"], str)
        return respuesta.json()["detail"]

    assert "edad_conyuge: debe ser menor o igual que 130" in detalle({"datos": {"edad_conyuge": 200}})
    assert "restricciones.donativo_previsto" in detalle({"restricciones": {"donativo_previsto": -1}})
    assert "presupuesto de donativos" in detalle(
        {"restricciones": {"donativo_previsto": 100, "presupuesto_donativos": 5}}
    )
    assert "No hay parámetros del IRPF" in detalle({"ejercicio": 2030})
    assert "cónyuge" in detalle({"restricciones": {"perdidas_latentes": [{"importe": 5, "titular": "conyuge"}]}})
    assert "campo desconocido" in detalle({"otra_cosa": 1})
    assert client.post("/optimize", json={"case_id": "case_nope"}).status_code == 404


def test_optimize_endpoint_example_of_the_readme_is_valid() -> None:
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    seccion = readme.split("### Optimizador fiscal", 1)[1]
    ejemplo = seccion.split("```json", 1)[1].split("```", 1)[0]
    cuerpo = json.loads(ejemplo)
    respuesta = client.post("/optimize", json=cuerpo)
    assert respuesta.status_code == 200, respuesta.text
    assert (
        hashlib.sha256(respuesta.content).hexdigest()
        == hashlib.sha256(client.post("/optimize", json=cuerpo).content).hexdigest()
    )


def test_children_given_by_one_spouse_are_common_to_both_separate_returns() -> None:
    hijos = [Descendiente(edad=2, gastos={"gastos_guarderia": D(1500)}), Descendiente(edad=6)]
    plan = optimizar(_asalariado(40000, descendientes=hijos), conyuge=_asalariado(25000), fecha=NOV, restricciones=SI)
    individuales = {d.titular: d.resultado for d in plan.base.declaraciones}
    for resultado in individuales.values():
        assert any(linea.concepto == "Mínimo por descendientes (50 %, compartido)" for linea in resultado.lineas)
    assert any("se han tomado los tuyos como comunes" in s for s in plan.supuestos)
    # A spouse who says they have no children keeps none.
    sin = optimizar_desde_hechos(
        ejercicio=2026,
        fecha=NOV,
        datos={"rendimiento_trabajo_bruto": 40000, "num_descendientes": 2, "comunidad_autonoma": "Madrid"},
        conyuge={"rendimiento_trabajo_bruto": 25000, "num_descendientes": 0},
    )
    conyuge = next(d.resultado for d in sin.base.declaraciones if d.titular == "conyuge")
    assert not any(linea.concepto.startswith("Mínimo por descendientes") for linea in conyuge.lineas)


def test_a_search_that_runs_out_of_budget_says_so_and_still_respects_the_constraints() -> None:
    restricciones = Restricciones(
        acepta_iliquidez=True,
        donativo_previsto=D(300),
        presupuesto_donativos=D(3000),
        perdidas_latentes=[PosicionLatente(importe=D(v)) for v in (700, 900, 1300)]
        + [PosicionLatente(importe=D(v), titular="conyuge") for v in (500, 2600)],
    )
    a = _asalariado(33500, comunidad="Comunidad Valenciana", ganancias_patrimoniales=D(2500))
    b = _asalariado(29000, comunidad="Comunidad Valenciana", rendimiento_capital_mobiliario=D(3000))
    completo = optimizar(a, conyuge=b, fecha=NOV, restricciones=restricciones)
    corto = optimizar(a, conyuge=b, fecha=NOV, restricciones=restricciones, max_evaluaciones=60)
    assert completo.busqueda.metodo == "exhaustiva" and corto.busqueda.metodo == "heuristica"
    assert "no está garantizado que sea el mejor posible" in corto.explicacion
    assert 0 <= corto.ahorro_total <= completo.ahorro_total
    for p in corto.palancas:
        if p.id == "donativo":
            assert D(300) <= p.importe <= D(3000)
        if p.id == "perdidas_conyuge":
            assert p.importe <= D(3100)
