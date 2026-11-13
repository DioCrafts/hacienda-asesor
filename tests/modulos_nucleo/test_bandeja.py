"""Unified inbox: module notices, the open notification kind and the weekly digest."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

from click.testing import CliRunner
from pydantic import ValidationError
import pytest

from hacienda_gpt import settings
from hacienda_gpt.cli.proactive import cli
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.modulos.acciones import AccionPendiente, AccionStore
from hacienda_gpt.modulos.fiscal import MODULO as FISCAL
from hacienda_gpt.proactive.bandeja import avisos_de_modulos, clave_semana, resumen_semanal
from hacienda_gpt.proactive.jobs import ejecutar_para_todos
from hacienda_gpt.proactive.store import Notification, NotificationStore, modulo_de
from tests.proactive import SALARIED, make_case
from .fakes import Llamadas, agua, aviso, instalar, seguros

HOY = date(2026, 11, 16)  # a Monday


@pytest.fixture
def store(tmp_path) -> NotificationStore:
    return NotificationStore(str(tmp_path / "n.db"))


def test_the_kind_is_open_to_modules_and_still_checked() -> None:
    base = {"case_id": "c", "key": "k", "title": "t", "body": "", "severity": "info"}
    for kind in ("plazo", "fin_de_ejercicio", "cambio_normativo", "resumen_semanal", "modulo:agua"):
        assert Notification(kind=kind, **base).kind == kind
    for kind in ("otra_cosa", "modulo:", "modulo:Agua", "plazo "):
        with pytest.raises(ValidationError):
            Notification(kind=kind, **base)
    assert [modulo_de(k) for k in ("plazo", "modulo:agua", "resumen_semanal")] == ["fiscal", "agua", None]


def test_module_notices_are_stored_once_namespaced_and_replaced(store) -> None:
    modulo = agua(
        Llamadas(),
        avisos_fijos=[
            aviso("factura:2026-11", gravedad="aviso", importe=Decimal("31.20"), vence=date(2026, 11, 30)),
            aviso("estimacion:v2", sustituye_prefijo="estimacion:"),
        ],
    )
    case = make_case("c1", agua_personas=3)
    store.add(
        Notification(
            case_id="c1", kind="modulo:agua", key="modulo:agua:estimacion:v1", title="vieja", body="", severity="info"
        )
    )

    creados, fallidos = avisos_de_modulos(case, hoy=HOY, store=store, modulos=[modulo])

    assert (creados, fallidos) == ({"agua": 2}, [])
    notas = {n.key: n for n in store.list("c1")}
    assert set(notas) == {"modulo:agua:factura:2026-11", "modulo:agua:estimacion:v2"}
    factura = notas["modulo:agua:factura:2026-11"]
    assert (
        factura.kind == "modulo:agua" and factura.amount == Decimal("31.20") and factura.due_date == date(2026, 11, 30)
    )
    # The module saw the whole profile, JSON parsed, and the case id.
    contexto = modulo.contextos[0]
    assert contexto.perfil == {"agua_personas": 3.0} and contexto.case_id == "c1" and contexto.hoy == HOY
    # Idempotent.
    assert avisos_de_modulos(case, hoy=HOY, store=store, modulos=[modulo]) == ({"agua": 0}, [])


def test_one_failing_module_never_stops_the_others(store) -> None:
    llamadas = Llamadas()
    roto = agua(llamadas, error=RuntimeError("se rompió"))
    raro = seguros(llamadas, avisos_fijos=["no es un aviso", aviso("ok"), aviso("", titulo="sin clave")])
    creados, fallidos = avisos_de_modulos(make_case("c1"), hoy=HOY, store=store, modulos=[FISCAL, roto, raro])
    assert creados == {"seguros": 1} and fallidos == ["agua", "seguros"]
    assert [n.key for n in store.list("c1")] == ["modulo:seguros:ok"]


def test_daily_runner_adds_module_notices_only_when_a_module_is_on(tmp_path, store) -> None:
    cases = SQLiteCaseStateStore(str(tmp_path / "c.db"))
    cases.save_case(make_case("a", **SALARIED))
    modulo = agua(Llamadas(), avisos_fijos=[aviso("x")])
    instalar(modulo)
    # Only the tax module on (the default): exactly the summary there always was.
    tax_only = ejecutar_para_todos(cases, store, hoy=date(2026, 11, 12))
    assert tax_only == {"hoy": "2026-11-12", "cases": 1, "plazo": 0, "fin_de_ejercicio": 1, "errors": 0}
    con_agua = ejecutar_para_todos(cases, store, hoy=date(2026, 11, 12), modulos_de=lambda c: [FISCAL, modulo])
    assert con_agua["modulos"] == {"agua": 1} and con_agua["errores_modulos"] == 0
    assert con_agua["fin_de_ejercicio"] == 0  # the tax jobs still ran (idempotent)


def test_daily_runner_survives_a_broken_module_choice(tmp_path, store) -> None:
    cases = SQLiteCaseStateStore(str(tmp_path / "c.db"))
    cases.save_case(make_case("a", **SALARIED))

    def roto(_case):
        raise RuntimeError("sin registro")

    summary = ejecutar_para_todos(cases, store, hoy=date(2026, 11, 12), modulos_de=roto)
    assert summary["fin_de_ejercicio"] == 1 and summary["errores_modulos"] == 1


def _n(store, key, kind="modulo:agua", **kw) -> None:
    datos = {"title": key, "body": "", "severity": "info"}
    datos.update(kw)
    store.add(Notification(case_id="c1", kind=kind, key=key, **datos))


def test_weekly_digest_orders_by_urgency_and_never_adds_up(store, tmp_path) -> None:
    acciones = AccionStore(str(tmp_path / "n.db"))
    acciones.add(
        AccionPendiente(
            case_id="c1", modulo="agua", herramienta="agua_cambiar_tarifa", resumen="Agua — Cambiar tarifa."
        )
    )
    _n(store, "plazo:130", kind="plazo", title="Modelo 130", severity="urgente", due_date=date(2026, 11, 20))
    _n(store, "modulo:agua:f", title="Factura del agua", due_date=date(2026, 12, 1), amount=Decimal("31.20"))
    _n(store, "modulo:seguros:r", kind="modulo:seguros", title="Renovación del seguro", amount=Decimal("210"))
    _n(store, "modulo:agua:info", title="Consejo de ahorro")
    _n(store, "modulo:agua:pasado", title="Ya vencido", due_date=date(2026, 11, 1))
    nombres = {"agua": "Agua", "seguros": "Seguros"}

    assert resumen_semanal(make_case("c1"), hoy=HOY, store=store, acciones=acciones, nombres=nombres) == 1
    (digest,) = [n for n in store.list("c1") if n.kind == "resumen_semanal"]
    assert digest.key == clave_semana(HOY) == "resumen_semanal:2026-W47"
    assert digest.title == "Resumen semanal: 4 avisos abiertos y 1 acción por decidir"
    body = digest.body
    orden = [
        "Pendiente de tu decisión",
        "[Agua] Agua — Cambiar tarifa.",
        "Lo que vence",
        "[Impuestos] Modelo 130 — vence el 20/11/2026 (en 4 días) — urgente",
        "[Agua] Factura del agua — vence el 01/12/2026 (en 15 días) — importe indicado: 31,20 €",
        "Importes previstos",
        "[Seguros] Renovación del seguro — importe indicado: 210,00 €",
        "Otros avisos sin leer",
        "[Agua] Consejo de ahorro",
        "no se suman entre sí",
    ]
    posiciones = [body.index(texto) for texto in orden]
    assert posiciones == sorted(posiciones)
    assert "Ya vencido" not in body and "241" not in body  # no total across modules
    assert digest.severity == "aviso" and digest.due_date == date(2026, 11, 20)
    # Once per week.
    assert resumen_semanal(make_case("c1"), hoy=HOY, store=store, acciones=acciones, nombres=nombres) == 0
    assert resumen_semanal(make_case("c1"), hoy=date(2026, 11, 22), store=store, acciones=acciones) == 0


def test_no_digest_when_nothing_is_open(store) -> None:
    _n(store, "modulo:agua:leido", title="Leído")
    store.mark_read("c1", store.list("c1")[0].id)
    assert resumen_semanal(make_case("c1"), hoy=HOY, store=store) == 0


def test_run_weekly_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUTH_MODE", "off")
    db = str(tmp_path / "state.sqlite3")
    cases = SQLiteCaseStateStore(db)
    cases.save_case(make_case("a"))
    cases.save_case(make_case("b"))
    NotificationStore(db).add(
        Notification(
            case_id="a",
            kind="plazo",
            key="plazo:x",
            title="Modelo 303",
            body="",
            severity="aviso",
            due_date=date(2026, 11, 20),
        )
    )
    result = CliRunner().invoke(cli, ["run-weekly", "--db-path", db, "--hoy", "2026-11-16"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip().splitlines()[-1]) == {
        "hoy": "2026-11-16",
        "semana": "resumen_semanal:2026-W47",
        "cases": 2,
        "resumen_semanal": 1,
        "errors": 0,
    }
