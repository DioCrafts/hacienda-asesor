"""Proactive advisor jobs: notifications the user did not have to ask for.

Two commands, meant for cron::

    # Recordatorios de plazos y plan de fin de ejercicio para todos los casos.
    # No usa ningún LLM (no cuesta nada): cada día a las 07:00.
    0 7 * * *   cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.proactive run-daily >> logs/proactive.log 2>&1

    # Cambios normativos: primero boe_watch detecta y descarga las normas que han cambiado,
    # y después boe-impact avisa a los usuarios afectados (una llamada al LLM por norma
    # cambiada, ninguna por usuario; --yes para no pedir confirmación en cron).
    30 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.boe_watch >> logs/boe_watch.log 2>&1
    45 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.proactive boe-impact \
                    --changes-log data/html/_boe_changes.jsonl --snapshot-dir data/html --yes >> logs/proactive.log 2>&1

Both are safe to re-run: notifications are deduplicated per case, and
``boe-impact`` remembers which change events it already handled in a small
state file next to the changes log.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import logging
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
import urllib.request

import click

from hacienda_gpt import settings
from hacienda_gpt.decision.schemas import CaseStatus
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.impacto import clave_evento, parse_fecha_actualizacion, preparar_eventos, procesar_eventos
from hacienda_gpt.proactive.jobs import ejecutar_para_todos, hoy_en_espana
from hacienda_gpt.proactive.novedades import ItemSumario, notificar_novedad, revisar
from hacienda_gpt.proactive.store import NotificationStore
from hacienda_gpt.tax.fuentes import desactualizadas, normas_de_parametros

logger = logging.getLogger(__name__)

_DB_OPTION = click.option(
    "--db-path",
    default=None,
    help="Base de datos SQLite de casos y notificaciones (por defecto, DECISION_STATE_DB_PATH).",
)


def _parse_day(_ctx: click.Context, _param: click.Parameter, value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise click.BadParameter("usa el formato AAAA-MM-DD") from exc


def _state_path(changes_log: Path) -> Path:
    return changes_log.with_name(changes_log.name + ".impact_state.json")


def _load_processed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("processed", []))
    except (json.JSONDecodeError, OSError, AttributeError) as exc:
        raise click.ClickException(
            f"No se puede leer el estado {path} ({exc}). Corrígelo o bórralo: sin él se volverían a analizar "
            "todos los cambios del registro."
        ) from exc


def _save_processed(path: Path, processed: set[str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    payload = {"schema_version": 1, "updated_at": datetime.now(UTC).isoformat(), "processed": sorted(processed)}
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_events(changes_log: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for number, line in enumerate(changes_log.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line %d of %s", number, changes_log)
            continue
        if isinstance(event, dict) and event.get("id"):
            events.append(event)
    return events


def _locate_html(event: dict[str, Any], snapshot_dir: Path) -> Path | None:
    """Find the HTML boe_watch downloaded for an event.

    The event records the path as boe_watch saw it (possibly relative to its
    working directory), so fall back to searching the snapshot directory.
    """
    recorded = event.get("path")
    if isinstance(recorded, str) and recorded:
        for candidate in (Path(recorded), snapshot_dir / recorded, snapshot_dir / Path(recorded).name):
            if candidate.is_file():
                return candidate
    found = sorted(snapshot_dir.rglob(f"{event['id']}.html"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


@click.group()
def cli() -> None:
    """Trabajos proactivos del asesor: avisos que el usuario no ha tenido que pedir."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@cli.command("run-daily")
@_DB_OPTION
@click.option("--hoy", callback=_parse_day, default=None, help="Fecha de referencia AAAA-MM-DD (por defecto, hoy).")
def run_daily(db_path: str | None, hoy: date | None) -> None:
    """Recordatorios de plazos y plan de fin de ejercicio para todos los casos. No usa ningún LLM."""
    path = db_path or settings.DECISION_STATE_DB_PATH
    summary = ejecutar_para_todos(SQLiteCaseStateStore(path), NotificationStore(path), hoy=hoy or hoy_en_espana())
    click.echo(json.dumps(summary, ensure_ascii=False))


@cli.command("boe-impact")
@click.option(
    "--changes-log",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Registro de cambios que escribe boe_watch (_boe_changes.jsonl).",
)
@click.option(
    "--snapshot-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directorio donde boe_watch descarga el HTML consolidado.",
)
@click.option("--since", callback=_parse_day, default=None, help="Solo cambios detectados desde AAAA-MM-DD.")
@click.option("--yes", is_flag=True, help="No pedir confirmación antes de llamar al LLM.")
@_DB_OPTION
def boe_impact(changes_log: Path, snapshot_dir: Path, since: date | None, yes: bool, db_path: str | None) -> None:
    """Avisa a los usuarios afectados por las normas que han cambiado (una llamada al LLM por norma)."""
    state_path = _state_path(changes_log)
    processed = _load_processed(state_path)
    events = [event for event in _read_events(changes_log) if clave_evento(event) not in processed]
    if since is not None:
        events = [event for event in events if str(event.get("ts", ""))[:10] >= since.isoformat()]

    latest_by_id = {event["id"]: event for event in events}

    def load_html(boe_id: str) -> str | None:
        path = _locate_html(latest_by_id[boe_id], snapshot_dir)
        if path is None:
            logger.warning("No HTML found for %s under %s", boe_id, snapshot_dir)
            return None
        return path.read_bytes().decode("utf-8", errors="replace")

    pendientes = preparar_eventos(events, load_html).pendientes
    summary: dict[str, Any] = {"eventos": len(events), "llamadas_llm": 0, "notificaciones": 0}
    if pendientes and not yes:
        click.echo("Normas con artículos modificados:")
        for pendiente in pendientes:
            click.echo(f"  {pendiente.boe_id}  {len(pendiente.articulos)} artículo(s)  {pendiente.titulo_norma[:70]}")
        if not click.confirm(f"Se harán {len(pendientes)} llamada(s) al LLM. ¿Continuar?"):
            click.echo("Cancelado: no se ha analizado ni notificado nada.")
            return

    if events:
        provider: Any = None
        if pendientes:
            from hacienda_gpt.agent.providers import create_provider

            provider = create_provider()
        path = db_path or settings.DECISION_STATE_DB_PATH
        cases = [case for case in SQLiteCaseStateStore(path).list_all_cases() if case.status != CaseStatus.CLOSED]
        summary = procesar_eventos(
            provider, eventos=events, html_por_boe_id=load_html, cases=cases, store=NotificationStore(path)
        )
        _save_processed(state_path, processed | set(summary["procesados"]))
    click.echo(json.dumps(summary, ensure_ascii=False))


@cli.command("params-check")
@click.option(
    "--fail-on-stale", is_flag=True, help="Termina con código 1 si alguna norma ha cambiado (para cron o CI)."
)
def params_check(fail_on_stale: bool) -> None:
    """Comprueba si el BOE ha actualizado las normas de las que salen los parámetros de la calculadora.

    Una petición ligera de metadatos por norma (la Ley del IRPF y la ley de cada
    comunidad); no usa ningún LLM. Una norma listada no significa que sus cifras
    hayan cambiado: significa que hay que releerla.
    """
    from hacienda_gpt.cli.boe_watch import _fetch_metadata

    fechas: dict[str, date] = {}
    errores: list[str] = []
    for norma in normas_de_parametros():
        try:
            fecha = parse_fecha_actualizacion(_fetch_metadata(norma.boe_id).get("fecha_actualizacion"))
        except Exception as exc:  # network or malformed payload: report and carry on
            errores.append(f"{norma.boe_id}: {exc}")
            continue
        if fecha is not None:
            fechas[norma.boe_id] = fecha
        time.sleep(0.3)
    pendientes = [
        {
            "boe_id": norma.boe_id,
            "comunidad": norma.comunidad_nombre or "Estado",
            "norma": norma.norma,
            "parametros_leidos_el": norma.consultado_el.isoformat(),
            "actualizada_en_boe_el": fecha.isoformat(),
            "ejercicios": list(norma.ejercicios),
        }
        for norma, fecha in desactualizadas(fechas)
    ]
    click.echo(
        json.dumps(
            {"comprobadas": len(fechas), "a_revisar": pendientes, "errores": errores}, ensure_ascii=False, indent=2
        )
    )
    if fail_on_stale and pendientes:
        raise SystemExit(1)


_SUMARIO_URL = "https://www.boe.es/datosabiertos/api/boe/sumario/{dia}"
_XML_URL = "https://www.boe.es/diario_boe/xml.php?id={id}"
_USER_AGENT = "hacienda-asesor/0.1 (novedades tributarias)"


def _get(url: str, *, accept: str, timeout: int = 60, intentos: int = 3) -> bytes | None:
    """GET ``url`` with a couple of retries; None on 404 (no BOE that day)."""
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": _USER_AGENT})
    for intento in range(intentos):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return bytes(response.read())
        except HTTPError as exc:
            if exc.code == 404:
                return None
            if intento == intentos - 1:
                raise
        except (URLError, ConnectionError, TimeoutError):
            if intento == intentos - 1:
                raise
        time.sleep(2 * (intento + 1))
    return None


def _sumario(dia: date) -> dict[str, Any] | None:
    raw = _get(_SUMARIO_URL.format(dia=dia.strftime("%Y%m%d")), accept="application/json")
    time.sleep(0.3)
    return json.loads(raw) if raw else None


def _xml(boe_id: str) -> bytes | None:
    raw = _get(_XML_URL.format(id=boe_id), accept="application/xml")
    time.sleep(0.3)
    return raw


def _vigiladas() -> frozenset[str]:
    """Ids already watched: the corpus catalogue plus the calculator's source laws."""
    catalogo = Path(__file__).resolve().parent / "boe_seed_catalog.json"
    ids = {str(doc["id"]) for doc in json.loads(catalogo.read_text(encoding="utf-8")).get("documents", [])}
    return frozenset(ids | {norma.boe_id for norma in normas_de_parametros()})


@cli.command("boe-novedades")
@click.option(
    "--desde",
    callback=_parse_day,
    default=None,
    help="Primer día a revisar (por defecto, el siguiente al último revisado; la primera vez, hace 7 días).",
)
@click.option("--hasta", callback=_parse_day, default=None, help="Último día a revisar (por defecto, hoy).")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./data/html"),
    show_default=True,
    help="Dónde se guardan el estado y el registro de novedades.",
)
@click.option(
    "--notify/--no-notify",
    default=True,
    show_default=True,
    help="Avisar a los usuarios afectados por las de relevancia alta.",
)
@_DB_OPTION
def boe_novedades(desde: date | None, hasta: date | None, output_dir: Path, notify: bool, db_path: str | None) -> None:
    """Busca en el BOE de cada día normas nuevas, estatales o autonómicas, que toquen impuestos.

    No usa ningún LLM: se guía por la clasificación por materias del propio BOE
    y por las normas que cada disposición modifica. Pensado para cron diario.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "_boe_novedades_state.json"
    registro_path = output_dir / "_boe_novedades.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    registro: dict[str, Any] = json.loads(registro_path.read_text(encoding="utf-8")) if registro_path.exists() else {}

    hoy = hoy_en_espana()
    hasta = hasta or hoy
    if desde is None:
        ultimo = state.get("ultimo_dia")
        desde = date.fromisoformat(ultimo) + timedelta(days=1) if ultimo else hasta - timedelta(days=7)
    dias = [desde + timedelta(days=n) for n in range(max(0, (hasta - desde).days + 1))]

    revision = revisar(
        dias,
        sumario_de=_sumario,
        xml_de=_xml,
        vigiladas=_vigiladas(),
        pendientes=[ItemSumario(**item) for item in state.get("pendientes", [])],
        hoy=hoy,
    )

    nuevas = 0
    for novedad in revision.novedades:
        nuevas += novedad.boe_id not in registro
        registro[novedad.boe_id] = novedad.model_dump(mode="json")
    notificaciones = 0
    if notify and revision.novedades:
        path = db_path or settings.DECISION_STATE_DB_PATH
        cases = [case for case in SQLiteCaseStateStore(path).list_all_cases() if case.status != CaseStatus.CLOSED]
        store = NotificationStore(path)
        notificaciones = sum(notificar_novedad(novedad, cases, store) for novedad in revision.novedades)

    registro_path.write_text(json.dumps(registro, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if dias and not revision.errores:
        state["ultimo_dia"] = max(max(dias).isoformat(), state.get("ultimo_dia", ""))
    state["pendientes"] = [item.model_dump(mode="json") for item in revision.pendientes]
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    click.echo(
        json.dumps(
            {
                "dias_revisados": len(dias) - len(revision.dias_sin_boletin),
                "novedades": len(revision.novedades),
                "nuevas_en_el_registro": nuevas,
                "pendientes_de_analisis": len(revision.pendientes),
                "notificaciones": notificaciones,
                "errores": revision.errores,
                "destacadas": [
                    {
                        "boe_id": n.boe_id,
                        "fecha": n.fecha.isoformat(),
                        "ambito": n.comunidad_nombre or "Estado",
                        "tributos": n.tributos,
                        "modifica_vigiladas": [m.boe_id for m in n.modifica],
                        "en_catalogo": n.en_catalogo,
                        "titulo": n.titulo[:140],
                    }
                    for n in revision.novedades
                    if n.relevancia == "alta"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    cli()
