from datetime import date
from decimal import Decimal

import pytest

from hacienda_gpt.decision.schemas import CaseStatus
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.jobs import (
    ejecutar_para_caso,
    ejecutar_para_todos,
    plan_fin_de_ejercicio,
    recordatorios_de_plazos,
)
from hacienda_gpt.proactive.store import NotificationStore
from hacienda_gpt.tax.optimizador import optimizar_desde_hechos
from . import SALARIED, make_case

NOV = date(2026, 11, 12)


@pytest.fixture
def store(tmp_path) -> NotificationStore:
    return NotificationStore(str(tmp_path / "db.sqlite3"))


# --------------------------------------------------------------------------- #
# Deadline reminders
# --------------------------------------------------------------------------- #


def test_reminders_only_inside_the_window_and_not_duplicated(store) -> None:
    case = make_case(alta_actividad_economica=True)
    # 12 Nov: the next quarterly deadline (1 Feb 2027) is 81 days away.
    assert recordatorios_de_plazos(case, hoy=NOV, store=store) == 0

    # 10 Jan 2027: 4T VAT and 4T instalment both fall on Monday 1 Feb (22 days).
    hoy = date(2027, 1, 10)
    assert recordatorios_de_plazos(case, hoy=hoy, store=store) == 2
    items = store.list(case.case_id)
    assert {n.key for n in items} == {"plazo:iva_trimestral:2026:01", "plazo:irpf_pago_fraccionado:2026:01"}
    assert all(n.kind == "plazo" and n.severity == "aviso" and n.due_date == date(2027, 2, 1) for n in items)
    vat = next(n for n in items if "iva" in n.key)
    assert vat.title == "Modelo 303 · IVA: autoliquidación trimestral (4T 2026): vence el 01/02/2027"
    assert "quedan 22 días" in vat.body and "fin de semana" in vat.body
    assert "no está verificada" not in vat.body
    assert vat.sources == ["art. 71.4 Reglamento del IVA, RD 1624/1992 (BOE-A-1992-28925)"]

    # Daily re-runs never duplicate, even as the date moves on.
    assert recordatorios_de_plazos(case, hoy=hoy, store=store) == 0
    assert recordatorios_de_plazos(case, hoy=date(2027, 1, 30), store=store) == 0
    assert len(store.list(case.case_id)) == 2


def test_reminder_is_urgent_within_a_week_and_flags_unverified_dates(store) -> None:
    case = make_case(categoria_contribuyente="asalariado")
    assert recordatorios_de_plazos(case, hoy=date(2027, 6, 24), store=store) == 1
    (renta,) = store.list(case.case_id)
    assert renta.severity == "urgente" and renta.due_date == date(2027, 6, 30)
    assert renta.title.startswith("Modelo 100 · Declaración de la Renta (Renta 2026)")
    assert "no está verificada" in renta.body
    assert any("Confirma la fecha" in action for action in renta.actions)

    other = make_case("c2", categoria_contribuyente="asalariado")
    recordatorios_de_plazos(other, hoy=date(2027, 6, 22), store=store)  # 8 days left
    assert store.list("c2")[0].severity == "aviso"


def test_year_end_calendar_entry_is_left_to_the_plan(store) -> None:
    case = make_case()
    # 20 Dec: the only deadline within 30 days is the generic 31 Dec closing entry.
    assert recordatorios_de_plazos(case, hoy=date(2026, 12, 20), store=store) == 0
    assert store.list(case.case_id) == []


# --------------------------------------------------------------------------- #
# Year-end plan
# --------------------------------------------------------------------------- #


def test_plan_shows_the_combined_plan_and_each_lever_contribution(store) -> None:
    case = make_case(**SALARIED)
    assert plan_fin_de_ejercicio(case, hoy=NOV, store=store) == 1
    (plan,) = store.list(case.case_id)

    optimo = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts={f.name: f.value for f in case.facts})
    (pensiones,) = optimo.palancas
    assert pensiones.id == "plan_pensiones_contribuyente" and pensiones.importe == Decimal("1500")
    # The donation is not proposed: nobody said they would donate, and it requires spending.
    assert all(p.id != "donativo" for p in optimo.palancas)

    assert plan.kind == "fin_de_ejercicio" and plan.severity == "aviso" and plan.due_date == date(2026, 12, 31)
    assert plan.amount == optimo.ahorro_total == pensiones.ahorro_marginal == Decimal("538.50")
    assert plan.title == "Plan de fin de ejercicio 2026: 538,50 € de ahorro estimado"
    assert plan.key.startswith("fin_de_ejercicio:2026:")

    body = plan.body
    assert "Quedan 49 días hasta el 31 de diciembre" in body
    assert "a devolver 109,57 €" in body and "cuota líquida estimada: 8.890,43 €" in body
    assert "tu declaración quedaría a devolver 648,07 €: 538,50 € menos" in body
    assert (
        "1. **Aportación a tu plan de pensiones** — Aportar 1.500,00 € a tu plan de pensiones antes del 31/12/2026. "
        "Ahorro dentro del plan: 538,50 € (diferimiento" in body
    )
    # The requirements travel with the lever, the illiquidity and the tax on redemption among them.
    assert all(requisito in body for requisito in pensiones.requisitos)
    assert "inmovilizado hasta la jubilación" in body and "rendimiento del trabajo" in body
    assert "no se pueden sumar" in body
    # Donations only as information, with the rates read from the data.
    assert "**Si pensabas donar**" in body and "80 %" in body
    # What the plan assumed, and levers that only need a datum.
    assert "no has indicado cuánto puedes aportar" in body
    assert "Tributación conjunta: indícame tu estado civil" in body
    assert "estimaciones de una calculadora" in body
    assert plan.actions[0].startswith("Aportar 1.500,00 € a tu plan de pensiones") and "538,50 €" in plan.actions[0]
    assert pensiones.referencia in plan.sources


def test_plan_with_several_levers_shows_each_marginal_contribution(store) -> None:
    case = make_case(**SALARIED, familia_monoparental=True, num_descendientes=1)
    assert plan_fin_de_ejercicio(case, hoy=NOV, store=store) == 1
    (plan,) = store.list(case.case_id)
    optimo = optimizar_desde_hechos(ejercicio=2026, fecha=NOV, facts={f.name: f.value for f in case.facts})
    assert [p.id for p in optimo.palancas] == ["tributacion", "plan_pensiones_contribuyente"]
    assert plan.amount == optimo.ahorro_total > 0
    for n, palanca in enumerate(optimo.palancas, start=1):
        eur = f"{palanca.ahorro_marginal:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        assert f"{n}. **{palanca.nombre}** — {palanca.accion}. Ahorro dentro del plan: {eur} €" in plan.body
    assert "Presentar declaración conjunta (monoparental)" in plan.body


def test_plan_passes_through_the_regional_caveat_and_missing_core_data(store) -> None:
    case = make_case(rendimiento_trabajo_bruto=45000, rendimiento_actividad_neto=5000, comunidad_autonoma="Navarra")
    assert plan_fin_de_ejercicio(case, hoy=NOV, store=store) == 1
    body = store.list(case.case_id)[0].body
    limits = body.split("**Límites de esta estimación**")[1]
    assert limits.strip().startswith("- La normativa autonómica de «Navarra» no está cargada")
    assert "se ha aproximado" in limits
    assert limits.count("\n- ") <= 3  # at most 3 calculator caveats + the generic one
    assert "No me consta" in body and "las retenciones de IRPF" in body
    assert "a pagar" in body


def test_plan_only_between_october_and_december_of_the_fiscal_year(store) -> None:
    case = make_case(**SALARIED)
    assert plan_fin_de_ejercicio(case, hoy=date(2026, 9, 30), store=store) == 0
    assert plan_fin_de_ejercicio(case, hoy=date(2027, 1, 1), store=store) == 0
    assert plan_fin_de_ejercicio(make_case("c25", tax_period="2025", **SALARIED), hoy=NOV, store=store) == 0
    # A year without calculator parameters is skipped, not an error.
    assert (
        plan_fin_de_ejercicio(make_case("c30", tax_period="2030", **SALARIED), hoy=date(2030, 11, 1), store=store) == 0
    )
    assert store.list("c1") == store.list("c25") == store.list("c30") == []

    assert plan_fin_de_ejercicio(case, hoy=date(2026, 10, 1), store=store) == 1
    last_day = make_case("c-last", **SALARIED)
    assert plan_fin_de_ejercicio(last_day, hoy=date(2026, 12, 31), store=store) == 1
    final = store.list("c-last")[0]
    assert final.severity == "urgente" and "Hoy, 31 de diciembre, es el último día" in final.body


def test_plan_asks_for_figures_when_there_is_no_income(store) -> None:
    case = make_case(comunidad_autonoma="Madrid", num_descendientes=2)
    assert plan_fin_de_ejercicio(case, hoy=NOV, store=store) == 1
    (ask,) = store.list(case.case_id)
    assert ask.kind == "fin_de_ejercicio" and ask.severity == "info" and ask.amount is None
    assert ask.title == "Fin de ejercicio 2026: faltan tus cifras para preparar el plan"
    assert "salario o pensión bruta anual" in ask.body and "retenciones" in ask.body
    assert "comunidad autónoma" not in ask.body  # already known
    # Asked once, however the rest of the profile changes.
    assert plan_fin_de_ejercicio(case, hoy=date(2026, 11, 13), store=store) == 0
    assert plan_fin_de_ejercicio(make_case(comunidad_autonoma="Madrid"), hoy=NOV, store=store) == 0
    assert len(store.list(case.case_id)) == 1


def test_plan_is_regenerated_only_when_the_facts_change(store) -> None:
    case = make_case(**SALARIED)
    assert plan_fin_de_ejercicio(case, hoy=NOV, store=store) == 1
    first = store.list(case.case_id)[0]
    # Identical profile on later days: nothing new.
    assert plan_fin_de_ejercicio(case, hoy=date(2026, 11, 13), store=store) == 0
    assert plan_fin_de_ejercicio(make_case(**SALARIED), hoy=date(2026, 12, 1), store=store) == 0

    changed = make_case(**{**SALARIED, "aportacion_plan_pensiones": 1500})
    assert plan_fin_de_ejercicio(changed, hoy=date(2026, 11, 14), store=store) == 1
    (second,) = store.list(case.case_id)  # the stale plan was replaced
    assert second.key != first.key and second.id != first.id
    # The pension limit is used up: nothing left to propose.
    assert second.amount is None and "**Aportación a tu plan de pensiones**" not in second.body
    assert second.title == "Plan de fin de ejercicio 2026: tu situación antes del 31 de diciembre"


def test_plan_replaces_the_request_for_figures(store) -> None:
    assert plan_fin_de_ejercicio(make_case(), hoy=NOV, store=store) == 1
    assert plan_fin_de_ejercicio(make_case(**SALARIED), hoy=NOV, store=store) == 1
    (only,) = store.list("c1")
    assert only.amount == Decimal("538.50")


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #


def test_ejecutar_para_caso_runs_both_jobs(store) -> None:
    case = make_case(alta_actividad_economica=True, rendimiento_actividad_neto=30000)
    assert ejecutar_para_caso(case, hoy=date(2026, 10, 5), store=store) == {"plazo": 2, "fin_de_ejercicio": 1}
    assert ejecutar_para_caso(case, hoy=date(2026, 10, 5), store=store) == {"plazo": 0, "fin_de_ejercicio": 0}


def test_ejecutar_para_todos_covers_every_open_case(tmp_path, store) -> None:
    cases = SQLiteCaseStateStore(str(tmp_path / "cases.sqlite3"))
    cases.save_case(make_case("a", **SALARIED))
    cases.save_case(make_case("b", tax_period="2025", **SALARIED))
    cases.save_case(make_case("closed", **SALARIED).model_copy(update={"status": CaseStatus.CLOSED}))
    assert {c.case_id for c in cases.list_all_cases()} == {"a", "b", "closed"}

    summary = ejecutar_para_todos(cases, store, hoy=NOV)
    assert summary == {"hoy": "2026-11-12", "cases": 2, "plazo": 0, "fin_de_ejercicio": 1, "errors": 0}
    assert len(store.list("a")) == 1 and store.list("b") == [] and store.list("closed") == []
    assert ejecutar_para_todos(cases, store, hoy=NOV)["fin_de_ejercicio"] == 0
