from datetime import UTC, datetime, timedelta
import json
import sys

from click.testing import CliRunner
import pytest

from hacienda_gpt import settings
from hacienda_gpt.cli import ops as ops_module
from hacienda_gpt.cli.ops import ASUNTO_DOCTOR, MAX_HORAS_POR_DEFECTO, cli
from hacienda_gpt.ops import vigia
from hacienda_gpt.ops.vigia import leer_estado, registrar_ejecucion
from hacienda_gpt.proactive.entrega import ConfiguracionCanalError, EntregaStore
from hacienda_gpt.proactive.store import Notification, NotificationStore


class FakeCanal:
    def __init__(self, nombre: str = "fake", *, falla: bool = False) -> None:
        self.nombre = nombre
        self.falla = falla
        self.enviados: list[tuple[str, str]] = []

    def enviar(self, asunto: str, cuerpo: str) -> None:
        if self.falla:
            raise ConnectionError("servidor caído")
        self.enviados.append((asunto, cuerpo))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated state file and database; no channels, no healthcheck, no network."""
    state = tmp_path / "_jobs_state.json"
    db = tmp_path / "state.sqlite3"
    monkeypatch.setattr(settings, "JOBS_STATE_PATH", str(state))
    monkeypatch.setattr(settings, "DECISION_STATE_DB_PATH", str(db))
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "")
    canales: list = []
    monkeypatch.setattr(ops_module, "canales_configurados", lambda: list(canales))

    def no_network(*_args, **_kwargs):
        raise AssertionError("the tests must not touch the network")

    monkeypatch.setattr(vigia.urllib.request, "urlopen", no_network)
    return type("Env", (), {"state": state, "db": str(db), "canales": canales})()


def _add(db: str, key: str) -> Notification:
    notification = Notification(
        case_id="c1", key=key, kind="plazo", title=f"Título {key}", body="**Cuerpo**", severity="aviso"
    )
    NotificationStore(db).add(notification)
    return notification


def test_deliver_without_channels_says_so_and_exits_zero(env) -> None:
    _add(env.db, "a")
    result = CliRunner().invoke(cli, ["deliver"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["configurado"] is False and "NOTIFY_" in payload["mensaje"]
    assert leer_estado(env.state)["deliver"]["last_ok"] is True


def test_deliver_sends_pending_notifications_once(env) -> None:
    canal = FakeCanal()
    env.canales.append(canal)
    _add(env.db, "a")
    _add(env.db, "b")
    runner = CliRunner()

    result = runner.invoke(cli, ["deliver"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "configurado": True,
        "enviadas": 2,
        "errores": 0,
        "canales": {"fake": {"pendientes": 2, "enviadas": 2, "errores": 0}},
    }
    assert canal.enviados == [("Título a", "Cuerpo\n\nCaso: c1\n"), ("Título b", "Cuerpo\n\nCaso: c1\n")]

    again = runner.invoke(cli, ["deliver", "--db-path", env.db])
    assert json.loads(again.output)["enviadas"] == 0 and len(canal.enviados) == 2
    assert leer_estado(env.state)["deliver"]["last_ok"] is True


def test_deliver_exits_one_and_records_the_failure_when_a_send_fails(env) -> None:
    env.canales.extend([FakeCanal("roto", falla=True), FakeCanal("sano")])
    _add(env.db, "a")

    result = CliRunner().invoke(cli, ["deliver"])

    assert result.exit_code == 1
    assert json.loads(result.output)["canales"] == {
        "roto": {"pendientes": 1, "enviadas": 0, "errores": 1},
        "sano": {"pendientes": 1, "enviadas": 1, "errores": 0},
    }
    entry = leer_estado(env.state)["deliver"]
    assert entry["last_ok"] is False and entry["last_error_detail"] == "código de salida 1"


def test_deliver_reports_a_half_configured_channel(env, monkeypatch) -> None:
    def broken() -> list:
        raise ConfiguracionCanalError("El correo está configurado a medias: falta NOTIFY_EMAIL_TO.")

    monkeypatch.setattr(ops_module, "canales_configurados", broken)
    result = CliRunner().invoke(cli, ["deliver"])

    assert result.exit_code == 1 and "NOTIFY_EMAIL_TO" in result.output
    assert leer_estado(env.state)["deliver"]["last_ok"] is False


def test_deliver_marcar_entregadas_skips_the_backlog(env) -> None:
    canal = FakeCanal()
    env.canales.append(canal)
    _add(env.db, "a")
    runner = CliRunner()

    result = runner.invoke(cli, ["deliver", "--marcar-entregadas"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"configurado": True, "marcadas_sin_enviar": {"fake": 1}}

    _add(env.db, "b")
    runner.invoke(cli, ["deliver"])
    assert [asunto for asunto, _ in canal.enviados] == ["Título b"]
    assert EntregaStore(env.db).pendientes("fake") == []


def _all_ok(state, *, ahora: datetime | None = None) -> None:
    for nombre in MAX_HORAS_POR_DEFECTO:
        registrar_ejecucion(nombre, ok=True, ahora=ahora or datetime.now(UTC), path=state)


def test_default_max_ages() -> None:
    assert MAX_HORAS_POR_DEFECTO == {
        "run-daily": 36.0,
        "boe-novedades": 36.0,
        "boe-impact": 36.0,
        "boe-watch": 36.0,
        "deliver": 36.0,
    }


def test_doctor_all_good_exits_zero_and_sends_nothing(env) -> None:
    canal = FakeCanal()
    env.canales.append(canal)
    _all_ok(env.state)

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"problemas": [], "avisado_por": [], "errores_de_envio": 0}
    assert canal.enviados == []


def test_doctor_reports_problems_through_the_channels_and_exits_one(env) -> None:
    roto, sano = FakeCanal("roto", falla=True), FakeCanal("sano")
    env.canales.extend([roto, sano])
    _all_ok(env.state)
    registrar_ejecucion("boe-watch", ok=False, detalle="código de salida 2", path=env.state)
    registrar_ejecucion("run-daily", ok=True, ahora=datetime.now(UTC) - timedelta(hours=50), path=env.state)

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert len(payload["problemas"]) == 2
    assert payload["problemas"][0].startswith("boe-watch: la última ejecución")
    assert payload["problemas"][1].startswith("run-daily: la última ejecución correcta fue hace 50 h")
    assert payload["avisado_por"] == ["sano"] and payload["errores_de_envio"] == 1
    ((asunto, cuerpo),) = sano.enviados
    assert asunto == ASUNTO_DOCTOR == "HaciendaGPT: tarea programada con problemas"
    assert cuerpo == "".join(f"- {problema}\n" for problema in payload["problemas"])


def test_doctor_with_no_state_lists_every_expected_job(env) -> None:
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert json.loads(result.output)["problemas"] == [
        f"{nombre}: no se ha ejecutado nunca." for nombre in sorted(MAX_HORAS_POR_DEFECTO)
    ]


def test_doctor_max_horas_overrides_adds_and_disables(env) -> None:
    _all_ok(env.state, ahora=datetime.now(UTC) - timedelta(hours=40))
    runner = CliRunner()

    assert len(json.loads(runner.invoke(cli, ["doctor"]).output)["problemas"]) == 5

    args = ["doctor", "--no-notify"]
    for nombre in ("run-daily", "boe-novedades", "boe-impact", "deliver"):
        args += ["--max-horas", f"{nombre}=48"]
    result = runner.invoke(cli, [*args, "--max-horas", "boe-watch=0", "--max-horas", "semanal=200"])
    assert result.exit_code == 1
    assert json.loads(result.output)["problemas"] == ["semanal: no se ha ejecutado nunca."]

    bad = runner.invoke(cli, ["doctor", "--max-horas", "boe-watch"])
    assert bad.exit_code == 2 and "nombre=horas" in bad.output
    assert runner.invoke(cli, ["doctor", "--max-horas", "boe-watch=mucho"]).exit_code == 2


def test_doctor_no_notify_and_unreadable_state(env) -> None:
    canal = FakeCanal()
    env.canales.append(canal)
    env.state.write_text("{not json", encoding="utf-8")

    result = CliRunner().invoke(cli, ["doctor", "--no-notify"])
    assert result.exit_code == 1 and canal.enviados == []
    (problema,) = json.loads(result.output)["problemas"]
    assert problema.startswith("No se puede leer el estado de las tareas")

    other = env.state.with_name("otro.json")
    _all_ok(other)
    assert CliRunner().invoke(cli, ["doctor", "--state-path", str(other)]).exit_code == 0


def test_run_executes_the_command_and_records_success(env, tmp_path) -> None:
    marker = tmp_path / "marker.txt"
    code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"

    result = CliRunner().invoke(cli, ["run", "boe-novedades", "--", sys.executable, "-c", code, str(marker), "--yes"])

    assert result.exit_code == 0, result.output
    assert marker.read_text() == "--yes"  # options after -- reach the command untouched
    entry = leer_estado(env.state)["boe-novedades"]
    assert entry["last_ok"] is True and "last_error" not in entry


def test_run_propagates_the_exit_code_and_pings_fail(env, monkeypatch) -> None:
    pings: list = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def read(self) -> bytes:
            return b"OK"

    def fake_urlopen(request, timeout=None):
        pings.append((request.full_url, request.data))
        return _Response()

    monkeypatch.setattr(vigia.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("HEALTHCHECK_URL_BOE_WATCH", "https://hc.example.test/ping/abc")
    runner = CliRunner()

    result = runner.invoke(cli, ["run", "boe-watch", "--", sys.executable, "-c", "raise SystemExit(7)"])
    assert result.exit_code == 7
    entry = leer_estado(env.state)["boe-watch"]
    assert entry["last_ok"] is False and entry["last_error_detail"] == "código de salida 7"

    assert runner.invoke(cli, ["run", "boe-watch", "--", sys.executable, "-c", "pass"]).exit_code == 0
    assert pings == [
        ("https://hc.example.test/ping/abc/fail", "código de salida 7".encode()),
        ("https://hc.example.test/ping/abc", None),
    ]


def test_run_records_a_command_that_cannot_be_started(env, tmp_path) -> None:
    result = CliRunner().invoke(cli, ["run", "run-daily", "--state-path", str(env.state), "--", str(tmp_path / "nope")])

    assert result.exit_code == 1 and "No se pudo ejecutar" in result.output
    assert leer_estado(env.state)["run-daily"]["last_ok"] is False


def test_run_requires_a_command(env) -> None:
    assert CliRunner().invoke(cli, ["run", "run-daily"]).exit_code == 2
    assert leer_estado(env.state) == {}
