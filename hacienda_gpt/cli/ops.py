"""Operations commands: deliver notifications and watch over the scheduled jobs.

Meant for cron::

    # Envuelve cada tarea con «ops run» para que quede registrada y avise si falla.
    0 7 * * *   cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops run run-daily -- \
                    uv run python -m hacienda_gpt.cli.proactive run-daily >> logs/proactive.log 2>&1

    # Envía por correo o webhook los avisos que aún no han salido.
    15 7 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops deliver >> logs/ops.log 2>&1

    # Revisa que todas las tareas han corrido y avisa de las que no.
    0 9 * * *   cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops doctor >> logs/ops.log 2>&1

See ``docs/operacion.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import subprocess

import click

from hacienda_gpt import settings
from hacienda_gpt.ops.vigia import EstadoIlegibleError, informar, leer_estado, revisar, vigilado
from hacienda_gpt.proactive.entrega import (
    Canal,
    ConfiguracionCanalError,
    EntregaStore,
    canales_configurados,
    entregar_pendientes,
)

logger = logging.getLogger(__name__)

ASUNTO_DOCTOR = "HaciendaGPT: tarea programada con problemas"

# Daily jobs: 36 h tolerates a late or slow run without missing a skipped day.
MAX_HORAS_POR_DEFECTO: dict[str, float] = {
    "run-daily": 36.0,
    "boe-novedades": 36.0,
    "boe-impact": 36.0,
    "boe-watch": 36.0,
    "deliver": 36.0,
}


def _canales() -> list[Canal]:
    try:
        return canales_configurados()
    except ConfiguracionCanalError as exc:
        raise click.ClickException(str(exc)) from exc


def _parse_max_horas(_ctx: click.Context, _param: click.Parameter, values: tuple[str, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for value in values:
        nombre, separador, horas = value.partition("=")
        try:
            if not (nombre.strip() and separador):
                raise ValueError
            out[nombre.strip()] = float(horas)
        except ValueError as exc:
            raise click.BadParameter("usa el formato nombre=horas (por ejemplo, boe-watch=48)") from exc
    return out


@click.group()
def cli() -> None:
    """Operación: entrega de avisos fuera de la aplicación y vigilancia de las tareas programadas."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@cli.command("deliver")
@click.option(
    "--db-path",
    default=None,
    help="Base de datos SQLite de casos y notificaciones (por defecto, DECISION_STATE_DB_PATH).",
)
@click.option(
    "--marcar-entregadas",
    is_flag=True,
    help="No envía nada: da por entregados los avisos pendientes (para activar la entrega sin recibir los antiguos).",
)
def deliver(db_path: str | None, marcar_entregadas: bool) -> None:
    """Envía por los canales configurados (webhook, correo) los avisos que aún no han salido.

    Nunca envía dos veces el mismo aviso por el mismo canal. Termina con código 1
    si algún envío ha fallado; los fallidos se reintentan en la siguiente ejecución.
    """
    with vigilado("deliver"):
        canales = _canales()
        if not canales:
            click.echo(
                json.dumps(
                    {"configurado": False, "mensaje": "No hay ningún canal configurado (NOTIFY_*): no se envía nada."},
                    ensure_ascii=False,
                )
            )
            return
        ahora = datetime.now(UTC)
        store = EntregaStore(db_path or settings.DECISION_STATE_DB_PATH)
        try:
            if marcar_entregadas:
                marcadas = {canal.nombre: store.marcar_todas(canal.nombre, ahora=ahora) for canal in canales}
                click.echo(json.dumps({"configurado": True, "marcadas_sin_enviar": marcadas}, ensure_ascii=False))
                return
            resumen = entregar_pendientes(store, canales, ahora=ahora)
        finally:
            store.close()
        click.echo(json.dumps({"configurado": True, **resumen}, ensure_ascii=False))
        if resumen["errores"]:
            raise SystemExit(1)


@cli.command("doctor")
@click.option(
    "--max-horas",
    multiple=True,
    callback=_parse_max_horas,
    metavar="NOMBRE=HORAS",
    help="Antigüedad máxima de la última ejecución correcta de una tarea. Repetible. NOMBRE=0 deja de exigirla.",
)
@click.option("--state-path", default=None, help="Fichero de estado de las tareas (por defecto, JOBS_STATE_PATH).")
@click.option("--notify/--no-notify", default=True, show_default=True, help="Avisar de los problemas por los canales.")
def doctor(max_horas: dict[str, float], state_path: str | None, notify: bool) -> None:
    """Comprueba que las tareas programadas han corrido y han terminado bien.

    Imprime los problemas en JSON, los envía por los canales configurados y
    termina con código 1 si hay alguno.
    """
    limites = {**MAX_HORAS_POR_DEFECTO, **max_horas}
    limites = {nombre: horas for nombre, horas in limites.items() if horas > 0}
    try:
        problemas = revisar(leer_estado(state_path), ahora=datetime.now(UTC), max_horas=limites)
    except EstadoIlegibleError as exc:
        problemas = [f"No se puede leer el estado de las tareas ({exc})."]

    avisados: list[str] = []
    errores_de_envio = 0
    if problemas and notify:
        cuerpo = "\n".join(f"- {problema}" for problema in problemas) + "\n"
        try:
            canales = canales_configurados()
        except ConfiguracionCanalError as exc:
            canales = []
            errores_de_envio += 1
            logger.warning("Channels are misconfigured: %s", exc)
        for canal in canales:
            try:
                canal.enviar(ASUNTO_DOCTOR, cuerpo)
            except Exception as exc:  # report through the remaining channels and the exit code
                errores_de_envio += 1
                logger.warning("Could not send the report on %s: %s: %s", canal.nombre, type(exc).__name__, exc)
            else:
                avisados.append(canal.nombre)
    click.echo(
        json.dumps(
            {"problemas": problemas, "avisado_por": avisados, "errores_de_envio": errores_de_envio},
            ensure_ascii=False,
            indent=2,
        )
    )
    if problemas:
        raise SystemExit(1)


@cli.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("nombre")
@click.argument("comando", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option("--state-path", default=None, help="Fichero de estado de las tareas (por defecto, JOBS_STATE_PATH).")
def run(nombre: str, comando: tuple[str, ...], state_path: str | None) -> None:
    """Ejecuta COMANDO bajo vigilancia: registra el resultado y avisa al healthcheck.

    \b
    ops run boe-novedades -- uv run python -m hacienda_gpt.cli.proactive boe-novedades

    La salida del comando no se toca y este proceso termina con su mismo código.
    """
    try:
        # No shell: the command is executed exactly as given, argument by argument.
        codigo = subprocess.run(list(comando), check=False).returncode
    except OSError as exc:
        informar(nombre, ok=False, detalle=f"no se pudo ejecutar {comando[0]}: {exc}", path=state_path)
        raise click.ClickException(f"No se pudo ejecutar {comando[0]}: {exc}") from exc
    if codigo == 0:
        informar(nombre, ok=True, path=state_path)
        return
    detalle = f"terminó por la señal {-codigo}" if codigo < 0 else f"código de salida {codigo}"
    informar(nombre, ok=False, detalle=detalle, path=state_path)
    raise SystemExit(128 - codigo if codigo < 0 else codigo)


if __name__ == "__main__":
    cli()
