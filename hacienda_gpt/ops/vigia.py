"""Watchdog for the scheduled jobs: did each one run, and did it succeed?

Three pieces:

* :func:`registrar_ejecucion` keeps a small JSON state file with the last run,
  the last success and the last error of every job.
* :class:`vigilado` (decorator or context manager) wraps a job: it records the
  outcome and pings a dead-man's-switch URL following the healthchecks.io
  convention (``<url>`` on success, ``<url>/fail`` on failure).
* :func:`revisar` reads the state and lists what is wrong.

Why both: the state file tells *what* failed, but it is read by another cron job
(``ops doctor``), and when cron itself is dead (machine off, crontab lost)
nothing runs to notice. Only an external service that expects a ping and alerts
when it does not arrive covers that case.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ContextDecorator, contextmanager
from datetime import UTC, datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit
import urllib.request

from hacienda_gpt import settings

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_MAX_DETAIL_CHARS = 500
_PING_TIMEOUT_SECONDS = 10


class EstadoIlegibleError(RuntimeError):
    """The state file exists but cannot be parsed."""


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _state_path(path: str | Path | None) -> Path:
    return Path(path) if path is not None else Path(settings.JOBS_STATE_PATH)


def leer_estado(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Per-job state (``{}`` when no job has run yet).

    Raises :class:`EstadoIlegibleError` when the file exists but is not valid:
    a watchdog that reads garbage as "nothing to report" would hide the problem.
    """
    target = _state_path(path)
    if not target.exists():
        return {}
    try:
        jobs = json.loads(target.read_text(encoding="utf-8"))["jobs"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EstadoIlegibleError(f"{target}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(jobs, dict) or not all(isinstance(entry, dict) for entry in jobs.values()):
        raise EstadoIlegibleError(f"{target}: unexpected structure")
    return jobs


@contextmanager
def _locked(target: Path) -> Iterator[None]:
    """Serialise read-modify-write across processes (several cron jobs may finish at once)."""
    with open(target.with_name(target.name + ".lock"), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def registrar_ejecucion(
    nombre: str,
    *,
    ok: bool,
    detalle: str = "",
    ahora: datetime | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Record one run of job ``nombre`` in the state file and return its entry.

    Each entry holds ``last_run``, ``last_ok``, ``last_detail`` (of the last
    run), ``last_success`` and ``last_error`` / ``last_error_detail`` (kept
    across later successes, so the last failure stays visible). Timestamps are
    ISO 8601 in UTC. An unreadable state file is set aside as ``<name>.corrupt``
    and a new one is started: recording a run must not fail because of it.
    """
    moment = _aware(ahora or datetime.now(UTC)).astimezone(UTC).isoformat()
    target = _state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _locked(target):
        try:
            jobs = leer_estado(target)
        except EstadoIlegibleError as exc:
            logger.warning("Unreadable jobs state, starting a new one: %s", exc)
            target.replace(target.with_name(target.name + ".corrupt"))
            jobs = {}
        entry = dict(jobs.get(nombre, {}))
        entry["last_run"] = moment
        entry["last_ok"] = ok
        entry["last_detail"] = detalle[:_MAX_DETAIL_CHARS]
        if ok:
            entry["last_success"] = moment
        else:
            entry["last_error"] = moment
            entry["last_error_detail"] = detalle[:_MAX_DETAIL_CHARS]
        jobs[nombre] = entry
        tmp = target.with_name(target.name + ".tmp")
        payload = {"schema_version": _SCHEMA_VERSION, "updated_at": moment, "jobs": jobs}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(target)
    return entry


def url_de_healthcheck(nombre: str) -> str:
    """``HEALTHCHECK_URL_<NAME>`` (name in upper case, other characters as ``_``), else ``HEALTHCHECK_URL``."""
    variable = "HEALTHCHECK_URL_" + re.sub(r"[^A-Z0-9]", "_", nombre.upper())
    return os.environ.get(variable, "").strip() or settings.HEALTHCHECK_URL.strip()


def avisar_healthcheck(nombre: str, *, ok: bool, detalle: str = "") -> bool:
    """Ping the job's dead-man's switch, if one is configured. Never raises.

    Success pings the URL; failure pings ``<url>/fail`` with the detail as the
    request body (healthchecks.io stores it with the event). Returns True when
    a ping was sent and accepted.
    """
    url = url_de_healthcheck(nombre)
    if not url:
        return False
    try:
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("the healthcheck URL must be http(s)")
        if not ok:
            url = url.rstrip("/") + "/fail"
        data = detalle[:_MAX_DETAIL_CHARS].encode("utf-8") if detalle and not ok else None
        request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        with urllib.request.urlopen(request, timeout=_PING_TIMEOUT_SECONDS) as response:
            response.read()
    except Exception as exc:  # the ping is best effort: it must never break the job
        # The URL is a secret of sorts (anyone holding it can fake a ping): do not log it.
        logger.warning("Healthcheck ping for %s failed: %s: %s", nombre, type(exc).__name__, exc)
        return False
    return True


def informar(
    nombre: str,
    *,
    ok: bool,
    detalle: str = "",
    ahora: datetime | None = None,
    path: str | Path | None = None,
) -> None:
    """Record the outcome of a run and ping its healthcheck. Never raises.

    A full disk or an unreachable monitoring service must not turn a job that
    worked into one that failed, nor hide the job's own exception.
    """
    try:
        registrar_ejecucion(nombre, ok=ok, detalle=detalle, ahora=ahora, path=path)
    except Exception:
        logger.exception("Could not record the run of %s in the jobs state", nombre)
    avisar_healthcheck(nombre, ok=ok, detalle=detalle)


class vigilado(ContextDecorator):  # noqa: N801 - used as a decorator, like contextlib's own helpers
    """Run a job under watch: ``@vigilado("name")`` or ``with vigilado("name"):``.

    Records success or failure (see :func:`informar`) and lets the exception
    through unchanged. ``SystemExit`` with a zero/None code counts as success,
    because that is how CLI commands end; any other exit code, exception or
    interruption counts as a failure.
    """

    def __init__(self, nombre: str, *, path: str | Path | None = None) -> None:
        self.nombre = nombre
        self.path = path

    def __enter__(self) -> vigilado:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if exc is None or (isinstance(exc, SystemExit) and exc.code in (None, 0)):
            informar(self.nombre, ok=True, path=self.path)
        elif isinstance(exc, SystemExit):
            informar(self.nombre, ok=False, detalle=f"código de salida {exc.code}", path=self.path)
        else:
            informar(self.nombre, ok=False, detalle=f"{type(exc).__name__}: {exc}", path=self.path)


def _parse(value: Any) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def revisar(estado: dict[str, dict[str, Any]], *, ahora: datetime, max_horas: dict[str, float]) -> list[str]:
    """Problems found in the jobs state, as sentences in Spanish (empty list: all good).

    For every job in ``max_horas``: it never ran, or its last success is older
    than the given number of hours. For every job in the state, expected or
    not: its last run failed.
    """
    ahora = _aware(ahora)
    problemas: list[str] = []
    for nombre in sorted(set(max_horas) | set(estado)):
        entry = estado.get(nombre)
        if entry is None:
            problemas.append(f"{nombre}: no se ha ejecutado nunca.")
            continue
        if entry.get("last_ok") is False:
            detalle = entry.get("last_error_detail") or "sin detalle"
            problemas.append(f"{nombre}: la última ejecución ({entry.get('last_run', '?')}) falló: {detalle}")
        if nombre not in max_horas:
            continue
        exito = _parse(entry.get("last_success"))
        if exito is None:
            problemas.append(f"{nombre}: no ha terminado bien ninguna vez.")
            continue
        horas = (ahora - exito).total_seconds() / 3600
        if horas > max_horas[nombre]:
            problemas.append(
                f"{nombre}: la última ejecución correcta fue hace {horas:.0f} h "
                f"({exito.isoformat()}); el máximo es {max_horas[nombre]:g} h."
            )
    return problemas
