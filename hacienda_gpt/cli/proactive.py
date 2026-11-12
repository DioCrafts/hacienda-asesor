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

from datetime import UTC, date, datetime
import json
import logging
from pathlib import Path
from typing import Any

import click

from hacienda_gpt import settings
from hacienda_gpt.decision.schemas import CaseStatus
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.impacto import clave_evento, preparar_eventos, procesar_eventos
from hacienda_gpt.proactive.jobs import ejecutar_para_todos, hoy_en_espana
from hacienda_gpt.proactive.store import NotificationStore

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


if __name__ == "__main__":
    cli()
