import json

from click.testing import CliRunner
import pytest

from hacienda_gpt.cli import proactive as cli_module
from hacienda_gpt.cli.proactive import cli
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.store import NotificationStore
from . import SALARIED, make_case
from .test_impacto import LIRPF_HTML, FakeProvider, _evento, _respuesta


def _last_json(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def test_run_daily_writes_notifications_and_prints_a_summary(tmp_path) -> None:
    db = str(tmp_path / "state.sqlite3")
    cases = SQLiteCaseStateStore(db)
    cases.save_case(make_case("a", **SALARIED))
    cases.save_case(make_case("b", alta_actividad_economica=True))
    cases.close()

    runner = CliRunner()
    result = runner.invoke(cli, ["run-daily", "--db-path", db, "--hoy", "2026-10-05"])
    assert result.exit_code == 0, result.output
    # a: plan. b: two quarterly deadlines on 20 Oct + the request for figures.
    assert _last_json(result.output) == {
        "hoy": "2026-10-05",
        "cases": 2,
        "plazo": 2,
        "fin_de_ejercicio": 2,
        "errors": 0,
    }

    again = runner.invoke(cli, ["run-daily", "--db-path", db, "--hoy", "2026-10-06"])
    assert _last_json(again.output)["plazo"] == 0 and _last_json(again.output)["fin_de_ejercicio"] == 0

    store = NotificationStore(db)
    assert [n.kind for n in store.list("a")] == ["fin_de_ejercicio"]
    assert sorted(n.kind for n in store.list("b")) == ["fin_de_ejercicio", "plazo", "plazo"]

    bad = runner.invoke(cli, ["run-daily", "--db-path", db, "--hoy", "5 de octubre"])
    assert bad.exit_code != 0 and "AAAA-MM-DD" in bad.output


@pytest.fixture
def boe(tmp_path, monkeypatch):
    """A snapshot dir with one changed law, its changes log, a case database and a fake provider."""
    snapshot = tmp_path / "html"
    (snapshot / "boe-seed").mkdir(parents=True)
    (snapshot / "boe-seed" / "BOE-A-2006-20764.html").write_text(LIRPF_HTML, encoding="utf-8")
    log = snapshot / "_boe_changes.jsonl"
    events = [
        # The recorded path is relative to wherever boe_watch ran: it must still be found.
        _evento(path="elsewhere/boe-seed/BOE-A-2006-20764.html"),
        _evento(boe_id="BOE-A-1992-28740", previous=None),
    ]
    log.write_text("".join(json.dumps(e) + "\n" for e in events) + "not json\n", encoding="utf-8")

    db = str(tmp_path / "state.sqlite3")
    cases = SQLiteCaseStateStore(db)
    cases.save_case(make_case("a", **SALARIED))
    cases.close()

    provider = FakeProvider(_respuesta())
    import hacienda_gpt.agent.providers as providers

    monkeypatch.setattr(providers, "create_provider", lambda *args, **kwargs: provider)
    args = ["boe-impact", "--changes-log", str(log), "--snapshot-dir", str(snapshot), "--db-path", db]
    return {"args": args, "provider": provider, "db": db, "log": log}


def test_boe_impact_notifies_once_and_remembers_processed_events(boe) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, [*boe["args"], "--yes"])
    assert result.exit_code == 0, result.output
    summary = _last_json(result.output)
    assert summary["llamadas_llm"] == 1 and summary["notificaciones"] == 1 and summary["eventos"] == 2
    assert len(boe["provider"].calls) == 1

    state = json.loads(cli_module._state_path(boe["log"]).read_text(encoding="utf-8"))
    assert len(state["processed"]) == 2
    (n,) = NotificationStore(boe["db"]).list("a")
    assert n.kind == "cambio_normativo"

    # Re-run: nothing pending, no LLM call, no confirmation needed.
    again = runner.invoke(cli, boe["args"])
    assert again.exit_code == 0 and _last_json(again.output) == {"eventos": 0, "llamadas_llm": 0, "notificaciones": 0}
    assert len(boe["provider"].calls) == 1


def test_boe_impact_asks_before_spending_and_honours_since(boe) -> None:
    runner = CliRunner()
    declined = runner.invoke(cli, boe["args"], input="n\n")
    assert declined.exit_code == 0
    assert "Se harán 1 llamada(s) al LLM" in declined.output and "Cancelado" in declined.output
    assert boe["provider"].calls == [] and not cli_module._state_path(boe["log"]).exists()

    # Events are dated 2022-12-22: a later --since leaves nothing to do.
    later = runner.invoke(cli, [*boe["args"], "--since", "2023-01-01"])
    assert _last_json(later.output)["eventos"] == 0 and boe["provider"].calls == []

    accepted = runner.invoke(cli, [*boe["args"], "--since", "2022-12-01"], input="y\n")
    assert accepted.exit_code == 0 and len(boe["provider"].calls) == 1
    assert _last_json(accepted.output)["notificaciones"] == 1
