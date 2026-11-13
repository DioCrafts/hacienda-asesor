from datetime import UTC, datetime, timedelta
import json

import pytest

from hacienda_gpt import settings
from hacienda_gpt.ops import vigia
from hacienda_gpt.ops.vigia import (
    EstadoIlegibleError,
    avisar_healthcheck,
    leer_estado,
    registrar_ejecucion,
    revisar,
    url_de_healthcheck,
    vigilado,
)

T0 = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    return tmp_path / "sub" / "_jobs_state.json"


@pytest.fixture
def pings(monkeypatch) -> list:
    """Record healthcheck pings instead of touching the network; no URL configured by default."""
    calls: list = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def read(self) -> bytes:
            return b"OK"

    def fake_urlopen(request, timeout=None):
        calls.append((request.get_method(), request.full_url, request.data))
        return _Response()

    monkeypatch.setattr(vigia.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "")
    monkeypatch.delenv("HEALTHCHECK_URL_BOE_NOVEDADES", raising=False)
    return calls


def test_registrar_keeps_last_run_success_and_error_per_job(state) -> None:
    registrar_ejecucion("run-daily", ok=True, ahora=T0, path=state)
    registrar_ejecucion("boe-watch", ok=False, detalle="código de salida 2", ahora=T0, path=state)
    entry = registrar_ejecucion("run-daily", ok=False, detalle="x" * 2000, ahora=T0 + timedelta(days=1), path=state)

    assert entry["last_ok"] is False and len(entry["last_error_detail"]) == 500
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["jobs"]["boe-watch"] == {
        "last_run": "2026-11-10T07:00:00+00:00",
        "last_ok": False,
        "last_detail": "código de salida 2",
        "last_error": "2026-11-10T07:00:00+00:00",
        "last_error_detail": "código de salida 2",
    }
    daily = leer_estado(state)["run-daily"]
    assert daily["last_success"] == "2026-11-10T07:00:00+00:00"  # survives the later failure
    assert daily["last_run"] == daily["last_error"] == "2026-11-11T07:00:00+00:00"

    # A later success keeps the last error on record.
    after = registrar_ejecucion("run-daily", ok=True, ahora=T0 + timedelta(days=2), path=state)
    assert after["last_ok"] is True and after["last_error"] == "2026-11-11T07:00:00+00:00"
    assert not list(state.parent.glob("*.tmp"))


def test_registrar_uses_the_configured_path_by_default(tmp_path, monkeypatch) -> None:
    target = tmp_path / "estado.json"
    monkeypatch.setattr(settings, "JOBS_STATE_PATH", str(target))
    registrar_ejecucion("deliver", ok=True)
    assert set(leer_estado()) == {"deliver"}
    assert target.exists()


def test_leer_estado_missing_is_empty_and_corrupt_is_an_error(state) -> None:
    assert leer_estado(state) == {}
    state.parent.mkdir(parents=True)
    for content in ("{not json", '{"jobs": []}', "[]", '{"jobs": {"a": 1}}'):
        state.write_text(content, encoding="utf-8")
        with pytest.raises(EstadoIlegibleError):
            leer_estado(state)


def test_registrar_sets_a_corrupt_state_aside_and_starts_again(state) -> None:
    state.parent.mkdir(parents=True)
    state.write_text("{not json", encoding="utf-8")

    registrar_ejecucion("run-daily", ok=True, ahora=T0, path=state)

    assert set(leer_estado(state)) == {"run-daily"}
    assert state.with_name(state.name + ".corrupt").read_text(encoding="utf-8") == "{not json"


def test_vigilado_as_decorator_records_success_and_pings(state, pings, monkeypatch) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL_BOE_NOVEDADES", "https://hc.example.test/ping/abc")

    @vigilado("boe-novedades", path=state)
    def job(value: int) -> int:
        return value * 2

    assert job(21) == 42
    entry = leer_estado(state)["boe-novedades"]
    assert entry["last_ok"] is True and entry["last_success"] == entry["last_run"]
    assert pings == [("GET", "https://hc.example.test/ping/abc", None)]


def test_vigilado_records_the_failure_pings_fail_and_reraises(state, pings, monkeypatch) -> None:
    monkeypatch.setenv("HEALTHCHECK_URL_BOE_NOVEDADES", "https://hc.example.test/ping/abc/")

    with pytest.raises(RuntimeError, match="sin red"):
        with vigilado("boe-novedades", path=state):
            raise RuntimeError("sin red")

    entry = leer_estado(state)["boe-novedades"]
    assert entry["last_ok"] is False and entry["last_error_detail"] == "RuntimeError: sin red"
    assert "last_success" not in entry
    assert pings == [("POST", "https://hc.example.test/ping/abc/fail", b"RuntimeError: sin red")]


def test_vigilado_treats_system_exit_by_its_code(state, pings) -> None:
    with pytest.raises(SystemExit):
        with vigilado("limpio", path=state):
            raise SystemExit(0)
    with pytest.raises(SystemExit):
        with vigilado("sucio", path=state):
            raise SystemExit(3)
    with pytest.raises(KeyboardInterrupt):
        with vigilado("interrumpido", path=state):
            raise KeyboardInterrupt

    estado = leer_estado(state)
    assert estado["limpio"]["last_ok"] is True
    assert estado["sucio"]["last_error_detail"] == "código de salida 3"
    assert estado["interrumpido"]["last_ok"] is False
    assert pings == []  # nothing configured, nothing pinged


def test_healthcheck_url_specific_beats_generic(pings, monkeypatch) -> None:
    assert url_de_healthcheck("boe-novedades") == ""
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "https://hc.example.test/generic")
    assert url_de_healthcheck("boe-novedades") == "https://hc.example.test/generic"
    monkeypatch.setenv("HEALTHCHECK_URL_BOE_NOVEDADES", "https://hc.example.test/specific")
    assert url_de_healthcheck("boe-novedades") == "https://hc.example.test/specific"
    assert url_de_healthcheck("run-daily") == "https://hc.example.test/generic"


def test_a_failing_ping_never_breaks_the_job(state, monkeypatch) -> None:
    def broken_urlopen(request, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr(vigia.urllib.request, "urlopen", broken_urlopen)
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "https://hc.example.test/generic")

    with vigilado("run-daily", path=state):
        pass

    assert leer_estado(state)["run-daily"]["last_ok"] is True
    assert avisar_healthcheck("run-daily", ok=False, detalle="x") is False


def test_healthcheck_only_pings_http_urls(pings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "file:///etc/passwd")
    assert avisar_healthcheck("run-daily", ok=True) is False
    assert pings == []


def test_a_state_that_cannot_be_written_does_not_hide_the_job_result(tmp_path, pings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "https://hc.example.test/generic")
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")

    @vigilado("run-daily", path=blocker / "state.json")  # the parent is a file: cannot be created
    def job() -> str:
        return "hecho"

    assert job() == "hecho"
    assert pings == [("GET", "https://hc.example.test/generic", None)]


def test_revisar_reports_never_ran_stale_and_failed() -> None:
    ahora = T0 + timedelta(hours=48)
    estado = {
        "run-daily": {"last_run": T0.isoformat(), "last_ok": True, "last_success": T0.isoformat()},
        "boe-watch": {
            "last_run": ahora.isoformat(),
            "last_ok": False,
            "last_success": (ahora - timedelta(hours=24)).isoformat(),
            "last_error": ahora.isoformat(),
            "last_error_detail": "código de salida 2",
        },
        "deliver": {"last_run": ahora.isoformat(), "last_ok": True, "last_success": ahora.isoformat()},
        "siempre-falla": {"last_run": ahora.isoformat(), "last_ok": False, "last_error_detail": "boom"},
        "extra": {"last_run": ahora.isoformat(), "last_ok": False, "last_error_detail": ""},
    }
    limites = {"run-daily": 36, "boe-watch": 36, "deliver": 36, "boe-impact": 36, "siempre-falla": 36}

    problemas = revisar(estado, ahora=ahora, max_horas=limites)

    assert problemas == [
        "boe-impact: no se ha ejecutado nunca.",
        f"boe-watch: la última ejecución ({ahora.isoformat()}) falló: código de salida 2",
        # Not in max_horas, but a failed last run is always a problem.
        f"extra: la última ejecución ({ahora.isoformat()}) falló: sin detalle",
        f"run-daily: la última ejecución correcta fue hace 48 h ({T0.isoformat()}); el máximo es 36 h.",
        f"siempre-falla: la última ejecución ({ahora.isoformat()}) falló: boom",
        "siempre-falla: no ha terminado bien ninguna vez.",
    ]


def test_revisar_all_good_and_boundaries() -> None:
    estado = {"run-daily": {"last_run": T0.isoformat(), "last_ok": True, "last_success": T0.isoformat()}}
    assert revisar(estado, ahora=T0 + timedelta(hours=36), max_horas={"run-daily": 36}) == []
    assert len(revisar(estado, ahora=T0 + timedelta(hours=36, minutes=1), max_horas={"run-daily": 36})) == 1
    # A naive "now" is read as UTC.
    assert revisar(estado, ahora=datetime(2026, 11, 10, 8, 0), max_horas={"run-daily": 36}) == []
    assert revisar({}, ahora=T0, max_horas={}) == []
