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
from hacienda_gpt.tax.params import DATA_DIR, ejercicios_disponibles, load_params
from hacienda_gpt.tax.recodificacion import (
    AlmacenPropuestas,
    Hallazgo,
    Objetivo,
    Pendiente,
    Propuesta,
    RecodificacionError,
    adaptador_de,
    aplicar_propuesta,
    avisar_operador,
    confirmar,
    control_de_lo_publicado,
    ejecutar,
    objetivo_de_ruta,
    objetivos_de_articulo,
    objetivos_de_desglose,
    objetivos_de_rollover,
    objetivos_de_verificacion,
    pendientes_de_confirmar,
    rechazar_propuesta,
    todos_los_objetivos,
)
from hacienda_gpt.tax.recodificacion.nucleo import leer_texto, resumen_de
from hacienda_gpt.tax.rollover import preparar_ejercicio
from hacienda_gpt.tax.verificacion import cargar_y_verificar, leer_snapshot, url_de
from hacienda_gpt.tax.vigencia_normas import estado_desde_xml, guardar_estados, leer_estados

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


REGISTRO_NOVEDADES = "_boe_novedades.json"
ESTADO_PILOTO = "_params_autopilot.json"
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
    if not raw:
        return None
    try:
        payload: dict[str, Any] = json.loads(raw)
    except ValueError:
        # An HTML error page served with 200 never reaches the parser: tell the format watch before failing.
        from hacienda_gpt.ops import formatos

        formatos.observar("boe_sumario", raw.decode("utf-8", errors="replace"), contexto=dia.isoformat())
        raise
    return payload


def _xml(boe_id: str) -> bytes | None:
    raw = _get(_XML_URL.format(id=boe_id), accept="application/xml")
    time.sleep(0.3)
    return raw


def _vigiladas() -> frozenset[str]:
    """Ids already watched: the corpus catalogue plus the calculator's source laws."""
    catalogo = Path(__file__).resolve().parent / "boe_seed_catalog.json"
    ids = {str(doc["id"]) for doc in json.loads(catalogo.read_text(encoding="utf-8")).get("documents", [])}
    return frozenset(ids | {norma.boe_id for norma in normas_de_parametros()})


def _texto_del_boe() -> Any:
    """A ``texto_de`` that downloads each law's consolidated page once."""
    cache: dict[tuple[str, str | None], str | None] = {}

    def texto_de(boe_id: str, version: str | None = None) -> str | None:
        clave = (boe_id, version)
        if clave not in cache:
            try:
                raw = _get(url_de(boe_id, version), accept="text/html")
            except Exception as exc:
                logger.warning("Could not download %s: %s", boe_id, exc)
                raw = None
            cache[clave] = raw.decode("utf-8", errors="replace") if raw else None
            time.sleep(0.5)
        return cache[clave]

    return texto_de


@cli.command("params-verify")
@click.option("--ejercicio", "ejercicios", type=int, multiple=True, help="Ejercicio a verificar (por defecto, todos).")
@click.option(
    "--snapshot-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Leer las leyes de esta carpeta (<id>.html) en vez de descargarlas.",
)
@click.option("--fail-on-mismatch", is_flag=True, help="Termina con código 1 si alguna comprobación falla.")
def params_verify(ejercicios: tuple[int, ...], snapshot_dir: Path | None, fail_on_mismatch: bool) -> None:
    """Reverifica cada parámetro de la calculadora contra el texto vigente de su ley.

    Dice qué grupos de parámetros (escalas, mínimos, deducciones) ya no
    coinciden con lo que hoy publica el BOE. Sin LLM: son citas literales y
    cifras buscadas en el texto.
    """
    texto_de = leer_snapshot(snapshot_dir) if snapshot_dir else _texto_del_boe()
    informe: dict[str, Any] = {}
    fallos = 0
    for ejercicio in ejercicios or tuple(ejercicios_disponibles()):
        resultados = cargar_y_verificar(ejercicio, texto_de)
        malos = [r for r in resultados if not r.ok]
        fallos += len(malos)
        informe[str(ejercicio)] = {
            "comprobaciones": len(resultados),
            "conformes": len(resultados) - len(malos),
            "no_coinciden": [
                {
                    "boe_id": r.comprobacion.boe_id,
                    "parametros": "/".join(r.comprobacion.ruta),
                    "descripcion": r.comprobacion.descripcion,
                    "tipo": r.comprobacion.tipo,
                    "falta": r.faltan[:8],
                }
                for r in malos
            ],
        }
    click.echo(json.dumps(informe, ensure_ascii=False, indent=2))
    if fail_on_mismatch and fallos:
        raise SystemExit(1)


@cli.command("params-rollover")
@click.argument("destino", type=int)
@click.option(
    "--desde", "origen", type=int, default=None, help="Ejercicio de partida (por defecto, el último disponible)."
)
@click.option("--force", is_flag=True, help="Sobrescribir el fichero del ejercicio si ya existe.")
@click.option("--dry-run", is_flag=True, help="Mostrar el informe sin escribir nada.")
@click.option(
    "--recode",
    is_flag=True,
    help="Intentar recodificar con el LLM las deducciones retiradas (deja propuestas; ver params-recode).",
)
def params_rollover(destino: int, origen: int | None, force: bool, dry_run: bool, recode: bool) -> None:
    """Crea el fichero de parámetros de un ejercicio nuevo a partir del anterior.

    Copia el último ejercicio, reverifica cada grupo contra el texto vigente
    de su ley y deja marcado lo que ya no coincide, para revisar solo eso.
    """
    origen = origen or max(ejercicios_disponibles())
    path = DATA_DIR / f"irpf_{destino}.json"
    if path.exists() and not force and not dry_run:
        raise click.ClickException(f"{path.name} ya existe; usa --force para sobrescribirlo.")
    texto_de = _texto_del_boe()
    data, informe = preparar_ejercicio(origen, destino, texto_de, hoy=hoy_en_espana())
    if not dry_run:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        load_params.cache_clear()
    salida: dict[str, Any] = {"fichero": None if dry_run else str(path), **informe.model_dump()}
    if recode and not dry_run and informe.deducciones_retiradas:
        # What the rollover left out is re-read from the current text: proposals, not data.
        salida["recodificacion"] = _recodificar(
            objetivos_de_rollover(origen, destino), texto_de=texto_de, data_dir=DATA_DIR
        )
    click.echo(json.dumps(salida, ensure_ascii=False, indent=2))


@cli.command("normas-check")
@click.option("--fail-on-change", is_flag=True, help="Termina con código 1 si alguna norma ha cambiado de estado.")
def normas_check(fail_on_change: bool) -> None:
    """Comprueba si los reales decretos-ley de los que dependen parámetros se han convalidado o derogado.

    Lee del BOE la ficha de cada uno y actualiza el fichero de estado que
    consulta la calculadora: si uno se deroga, lo que introdujo deja de aplicarse.
    """
    ids: set[str] = set()
    for ejercicio in ejercicios_disponibles():
        for check_fuente in _fuentes_condicionadas(load_params(ejercicio).model_dump()):
            ids.add(check_fuente)
    previos = leer_estados()
    estados = dict(previos)
    cambios: list[dict[str, str]] = []
    errores: list[str] = []
    for boe_id in sorted(ids):
        try:
            raw = _xml(boe_id)
            if not raw:
                raise RuntimeError("sin respuesta del BOE")
            nuevo = estado_desde_xml(boe_id, raw, hoy=hoy_en_espana())
        except Exception as exc:
            errores.append(f"{boe_id}: {exc}")
            continue
        anterior = previos.get(boe_id)
        if anterior is None or anterior.estado != nuevo.estado:
            cambios.append(
                {
                    "boe_id": boe_id,
                    "antes": anterior.estado if anterior else "sin comprobar",
                    "ahora": nuevo.estado,
                    "detalle": nuevo.detalle,
                }
            )
        estados[boe_id] = nuevo
    if estados != previos:
        guardar_estados(estados)
    click.echo(
        json.dumps(
            {
                "normas": {i: estados[i].estado for i in sorted(ids) if i in estados},
                "cambios": cambios,
                "errores": errores,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if fail_on_change and any(c["antes"] != "sin comprobar" for c in cambios):
        raise SystemExit(1)


def _fuentes_condicionadas(node: Any) -> set[str]:
    """Every ``condicionada_a`` id found in a parameter tree."""
    found: set[str] = set()
    if isinstance(node, dict):
        value = node.get("condicionada_a")
        if isinstance(value, str) and value:
            found.add(value)
        for child in node.values():
            found |= _fuentes_condicionadas(child)
    elif isinstance(node, list):
        for child in node:
            found |= _fuentes_condicionadas(child)
    return found


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
@click.option(
    "--autopilot/--no-autopilot",
    default=None,
    help="Tras revisar el día, releer los parámetros que toquen las normas detectadas (ver params-autopilot). "
    "Por defecto, lo que diga RECODE_AUTOPILOT.",
)
@_DB_OPTION
def boe_novedades(
    desde: date | None, hasta: date | None, output_dir: Path, notify: bool, autopilot: bool | None, db_path: str | None
) -> None:
    """Busca en el BOE de cada día normas nuevas, estatales o autonómicas, que toquen impuestos.

    No usa ningún LLM: se guía por la clasificación por materias del propio BOE
    y por las normas que cada disposición modifica. Pensado para cron diario.
    Con --autopilot (o RECODE_AUTOPILOT=true), si alguna modifica una ley de la
    que salen parámetros de la calculadora, lanza a continuación el piloto de
    parámetros, que sí puede usar el LLM.
    """
    salida = _revisar_novedades(desde, hasta, output_dir, notify=notify, db_path=db_path)
    if _env_bool("RECODE_AUTOPILOT") if autopilot is None else autopilot:
        salida["piloto"] = _pilotar(output_dir)
    click.echo(json.dumps(salida, ensure_ascii=False, indent=2, default=str))


def _env_bool(nombre: str) -> bool:
    import os

    return os.environ.get(nombre, "").strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _revisar_novedades(
    desde: date | None, hasta: date | None, output_dir: Path, *, notify: bool, db_path: str | None
) -> dict[str, Any]:
    """One pass of the daily watch: read the days not seen yet, keep the registry and tell the users concerned."""
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "_boe_novedades_state.json"
    registro_path = output_dir / REGISTRO_NOVEDADES
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

    return {
        "dias_revisados": len(dias) - len(revision.dias_sin_boletin),
        "novedades": len(revision.novedades),
        "nuevas_en_el_registro": nuevas,
        "pendientes_de_analisis": len(revision.pendientes),
        "notificaciones": notificaciones,
        "errores": revision.errores,
        "modifican_parametros": sorted({n.boe_id for n in revision.novedades if any(m.parametros for m in n.modifica)}),
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
    }


# --------------------------------------------------------------------------- #
# Automatic re-encoding of parameters (hacienda_gpt/tax/recodificacion)
# --------------------------------------------------------------------------- #

_PROPOSALS_OPTION = click.option(
    "--proposals-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Carpeta de las propuestas (por defecto, RECODE_PROPOSALS_PATH).",
)
_SNAPSHOT_OPTION = click.option(
    "--snapshot-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Leer las leyes de esta carpeta (<id>.html) en vez de descargarlas.",
)
_DATA_OPTION = click.option(
    "--data-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    hidden=True,
    help="Carpeta de los ficheros de parámetros (para pruebas; por defecto, la del paquete).",
)


def _almacen(proposals_dir: Path | None) -> AlmacenPropuestas:
    return AlmacenPropuestas(proposals_dir or settings.RECODE_PROPOSALS_PATH)


def hay_credenciales_llm() -> bool:
    """Whether the configured provider can be called at all (so a scheduled run can skip cleanly)."""
    import os

    proveedor = settings.LLM_PROVIDER.strip().lower()
    if proveedor in ("anthropic", "claude"):
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if proveedor == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(settings.LOCAL_LLM_MODEL)


def _proveedores_de_recodificacion() -> tuple[Any, Any]:
    """Reader and reviewer, both metered: every call counts against the daily budget."""
    from hacienda_gpt.agent.providers import create_provider
    from hacienda_gpt.ops.consumo import medir

    lector = medir(create_provider(model=settings.RECODE_MODEL or None), "recodificacion")
    if settings.RECODE_REVIEWER_MODEL and settings.RECODE_REVIEWER_MODEL != settings.RECODE_MODEL:
        revisor = medir(create_provider(model=settings.RECODE_REVIEWER_MODEL), "recodificacion-revisor")
    else:
        revisor = lector.con_proposito("recodificacion-revisor")
    return lector, revisor


def _canales_del_operador() -> list[Any]:
    from hacienda_gpt.proactive.entrega import ConfiguracionCanalError, canales_configurados

    try:
        return list(canales_configurados())
    except ConfiguracionCanalError as exc:
        logger.warning("Channels are misconfigured: %s", exc)
        return []


def _anotaciones_de(norma: str, boe_id: str) -> str:
    """What the BOE's analysis of the amending ``norma`` says it does to the law ``boe_id``."""
    from hacienda_gpt.proactive.novedades import leer_analisis

    raw = _xml(norma)
    if not raw:
        raise click.ClickException(f"No se pudo leer el análisis de {norma} en el BOE.")
    lineas = [
        f"{norma}: {m.accion} {m.texto}".strip()
        for m in leer_analisis(raw).modifica
        if m.boe_id == boe_id and (m.accion or m.texto)
    ]
    if not lineas:
        raise click.ClickException(f"El análisis de {norma} no dice que modifique {boe_id}.")
    return "\n".join(lineas)


def _estimacion(objetivo: Objetivo, texto_de: Any) -> dict[str, Any]:
    """Size of what one reader call and one reviewer call would send for ``objetivo`` (no LLM involved)."""
    from hacienda_gpt.tax.recodificacion import prompts

    fila: dict[str, Any] = {
        "tipo": objetivo.tipo,
        "grupo": objetivo.etiqueta,
        "ruta": "/".join(objetivo.ruta),
        "norma": objetivo.boe_id,
        "articulos": objetivo.articulos_ley,
        "ejercicios_pedidos": objetivo.ejercicios,
        "motivo": objetivo.motivo[:4],
    }
    try:
        _, texto = leer_texto(objetivo, texto_de)
    except RecodificacionError as exc:
        return {**fila, "error": str(exc)}
    adaptador = adaptador_de(objetivo.tipo)
    fijo = len(prompts.REGLAS_COMUNES) + len(adaptador.guia()) + len(adaptador.esquema()) + len(prompts.SISTEMA_LECTOR)
    por_pasada = 2 * (fijo + len(texto.texto)) + len(json.dumps(objetivo.antes, ensure_ascii=False, default=str))
    return {
        **fila,
        "caracteres_texto_legal": len(texto.texto),
        "caracteres_por_pasada": por_pasada,
        # Spanish legal prose and JSON run at roughly 3–4 characters per token.
        "tokens_entrada_por_pasada_aprox": round(por_pasada / 3.5),
    }


def _recodificar(
    hallazgo: Hallazgo,
    *,
    texto_de: Any,
    data_dir: Path,
    proposals_dir: Path | None = None,
    max_iter: int | None = None,
    max_grupos: int | None = None,
    auto_aplicar: bool | None = None,
    forzar: bool = False,
    notify: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    salida: dict[str, Any] = {
        "grupos_a_revisar": len(hallazgo.objetivos),
        "sin_adaptador": hallazgo.sin_adaptador,
        "fuera_de_alcance": hallazgo.fuera_de_alcance,
        "errores": list(hallazgo.errores),
    }
    if dry_run:
        salida["simulacion"] = [_estimacion(objetivo, texto_de) for objetivo in hallazgo.objetivos]
        return salida
    if not hallazgo.objetivos:
        return salida
    if not hay_credenciales_llm():
        salida["omitido"] = "No hay credenciales del proveedor de LLM configurado: no se ha recodificado nada."
        return salida
    lector, revisor = _proveedores_de_recodificacion()
    almacen = _almacen(proposals_dir)
    informe, nuevas = ejecutar(
        hallazgo.objetivos,
        lector=lector,
        revisor=revisor,
        texto_de=texto_de,
        almacen=almacen,
        data_dir=data_dir,
        max_iter=max_iter or settings.RECODE_MAX_ITER,
        max_objetivos=max_grupos or settings.RECODE_MAX_PER_RUN,
        auto_aplicar=settings.RECODE_AUTO_APPLY if auto_aplicar is None else auto_aplicar,
        forzar=forzar,
        hoy=hoy_en_espana(),
    )
    salida["errores"] += informe.errores
    salida.update(informe.model_dump(exclude={"errores", "objetivos"}))
    salida["avisado_por"] = avisar_operador(nuevas, _canales_del_operador(), almacen=almacen) if notify else []
    return salida


@cli.command("params-recode")
@click.option("--ejercicio", "ejercicios", type=int, multiple=True, help="Ejercicio a revisar (por defecto, todos).")
@click.option("--comunidad", default=None, help="Solo esta comunidad (su clave: galicia, madrid…) o «estado».")
@click.option(
    "--articulo",
    default=None,
    help="Solo las deducciones de este artículo («art. 5» o «art. 5.Dos.1»). Con --comunidad las revisa aunque su "
    "comprobación no falle y, si no hay ninguna codificada, la propone como nueva.",
)
@click.option(
    "--ruta", default=None, help="Revisar este grupo aunque no falle (p. ej. comunidades/galicia/escala_general)."
)
@click.option(
    "--tipo",
    "tipos",
    multiple=True,
    help="Solo grupos de este tipo: deduccion_autonomica, escala, cifras (todos los grupos de cifras) o uno concreto "
    "(cifras:minimos). Los de otros ficheros llevan el nombre que les da su adaptador.",
)
@click.option(
    "--desglosar",
    is_flag=True,
    help="Con --comunidad y --articulo: el lector lista las deducciones que contiene el artículo y cada una que aún "
    "no esté codificada se propone como nueva (para artículos que crean deducciones).",
)
@click.option(
    "--sin-consolidar",
    is_flag=True,
    help="Con --norma: leer la nueva redacción del texto de la propia norma modificadora, porque el BOE aún no la ha "
    "consolidado. La propuesta nunca se auto-aplica.",
)
@click.option(
    "--version",
    default=None,
    help="Leer esta redacción histórica (AAAAMMDD) en vez del texto vigente. Solo con --articulo o --ruta: el bucle "
    "nunca elige por su cuenta una versión histórica.",
)
@click.option(
    "--norma",
    default=None,
    help="Identificador BOE de la norma modificadora: su análisis («MODIFICA…, con efectos de…») se da a leer.",
)
@_SNAPSHOT_OPTION
@click.option("--max-iter", type=int, default=None, help="Pasadas del lector por grupo (por defecto, RECODE_MAX_ITER).")
@click.option("--max-grupos", type=int, default=None, help="Grupos por ejecución (por defecto, RECODE_MAX_PER_RUN).")
@click.option("--auto-apply/--no-auto-apply", default=None, help="Aplicar sin esperar lo que no deje nada por revisar.")
@click.option("--force", is_flag=True, help="Volver a intentar grupos que ya tienen propuesta para este mismo texto.")
@click.option("--notify/--no-notify", default=True, show_default=True, help="Avisar al operador por sus canales.")
@click.option("--dry-run", is_flag=True, help="Decir qué se revisaría y cuánto texto se enviaría, sin llamar al LLM.")
@_PROPOSALS_OPTION
@_DATA_OPTION
def params_recode(
    ejercicios: tuple[int, ...],
    comunidad: str | None,
    articulo: str | None,
    ruta: str | None,
    tipos: tuple[str, ...],
    desglosar: bool,
    sin_consolidar: bool,
    version: str | None,
    norma: str | None,
    snapshot_dir: Path | None,
    max_iter: int | None,
    max_grupos: int | None,
    auto_apply: bool | None,
    force: bool,
    notify: bool,
    dry_run: bool,
    proposals_dir: Path | None,
    data_dir: Path | None,
) -> None:
    """Relee con un LLM los parámetros que ya no coinciden con su ley y deja PROPUESTAS de nuevos datos.

    No escribe en los ficheros de parámetros (salvo con auto-aplicación): cada
    propuesta pasa un control determinista (citas literales, cada cifra dentro
    de una cita, fecha de efectos) y la revisión de un segundo modelo con casos
    límite. Los ejercicios que toca una propuesta salen de la fecha de efectos
    de la redacción, que puede ser retroactiva.
    """
    data_dir = data_dir or DATA_DIR
    texto_de = leer_snapshot(snapshot_dir) if snapshot_dir else _texto_del_boe()
    # None: every year each parameter file has (the files of different taxes do not cover the same years).
    pedidos = list(ejercicios) or None
    explicito = ruta is not None or (articulo is not None and comunidad not in (None, "estado"))
    if version is not None and not explicito:
        raise click.ClickException("--version solo se admite con --ruta, o con --comunidad y --articulo.")
    if sin_consolidar and (norma is None or not explicito):
        raise click.ClickException(
            "--sin-consolidar necesita --norma y un grupo concreto (--ruta, o --comunidad y --articulo)."
        )
    if desglosar and (articulo is None or comunidad in (None, "estado")):
        raise click.ClickException("--desglosar necesita --comunidad y --articulo.")
    desglose: dict[str, Any] | None = None
    try:
        if ruta is not None:
            partes = ruta.strip("/").split("/")
            hallazgo = Hallazgo(objetivos=[objetivo_de_ruta(pedidos, partes, data_dir=data_dir, version=version)])
        elif desglosar:
            assert comunidad is not None and articulo is not None
            if dry_run:
                hallazgo = objetivos_de_articulo(
                    pedidos, comunidad=comunidad, articulo=articulo, data_dir=data_dir, version=version
                )
            elif not hay_credenciales_llm():
                hallazgo = Hallazgo(errores=["No hay credenciales del proveedor de LLM: no se puede desglosar."])
            else:
                hallazgo, hecho = objetivos_de_desglose(
                    pedidos,
                    comunidad=comunidad,
                    articulo=articulo,
                    lector=_proveedores_de_recodificacion()[0],
                    texto_de=texto_de,
                    data_dir=data_dir,
                    version=version,
                    norma_texto=norma if sin_consolidar else None,
                    ley_citada=_ley_citada_de(comunidad, data_dir) if sin_consolidar else "",
                )
                desglose = hecho.model_dump() if hecho is not None else None
        elif articulo is not None and comunidad not in (None, "estado"):
            assert comunidad is not None
            hallazgo = objetivos_de_articulo(
                pedidos, comunidad=comunidad, articulo=articulo, data_dir=data_dir, version=version
            )
        else:
            hallazgo = objetivos_de_verificacion(
                pedidos, texto_de, data_dir=data_dir, ambito=comunidad, articulo=articulo, tipos=tipos or None
            )
    except RecodificacionError as exc:
        raise click.ClickException(str(exc)) from exc
    if norma is not None:
        from hacienda_gpt.tax.fuentes import norma_de_parametros

        for objetivo in hallazgo.objetivos:
            objetivo.anotaciones = _anotaciones_de(norma, objetivo.boe_id)
            if sin_consolidar:
                fuente = norma_de_parametros(objetivo.boe_id)
                objetivo.norma_texto = norma
                objetivo.ley_citada = _numero_de_ley(fuente.norma) if fuente is not None else ""
    salida = _recodificar(
        hallazgo,
        texto_de=texto_de,
        data_dir=data_dir,
        proposals_dir=proposals_dir,
        max_iter=max_iter,
        max_grupos=max_grupos,
        auto_aplicar=auto_apply,
        forzar=force,
        notify=notify,
        dry_run=dry_run,
    )
    if desglose is not None:
        salida["desglose"] = desglose
    click.echo(json.dumps(salida, ensure_ascii=False, indent=2, default=str))
    if salida.get("presupuesto_agotado") or salida["errores"]:
        raise SystemExit(1)


def _numero_de_ley(nombre: str) -> str:
    """ "35/2006" for "Ley 35/2006, de 28 de noviembre…": how an amending norm cites the law it amends."""
    import re

    match = re.search(r"\b\d+/\d{4}\b", nombre)
    return match.group(0) if match else ""


def _ley_citada_de(comunidad: str, data_dir: Path) -> str:
    return _numero_de_ley(next((nombre for clave, _, nombre in _leyes_del_irpf(data_dir) if clave == comunidad), ""))


def _leyes_del_irpf(data_dir: Path) -> list[tuple[str | None, str, str]]:
    """(community key or None, BOE id, name) of every source law of the IRPF files under ``data_dir``."""
    out: dict[str, tuple[str | None, str, str]] = {}
    for path in sorted(data_dir.glob("irpf_*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        fuentes = [(None, raw["fuente_principal"])]
        fuentes += [(clave, c["fuente_principal"]) for clave, c in raw.get("comunidades", {}).items()]
        for clave, fuente in fuentes:
            out[fuente["boe_id"]] = (clave, fuente["boe_id"], fuente.get("norma", ""))
    return list(out.values())


def _pilotar(
    output_dir: Path,
    *,
    texto_de: Any = None,
    data_dir: Path | None = None,
    proposals_dir: Path | None = None,
    max_iter: int | None = None,
    max_grupos: int | None = None,
    auto_aplicar: bool | None = None,
    notify: bool = True,
    usar_llm: bool = True,
) -> dict[str, Any]:
    """One run of the parameters autopilot over the novelties registry kept under ``output_dir``."""
    from hacienda_gpt.tax.recodificacion import piloto

    data_dir = data_dir or DATA_DIR
    registro_path = output_dir / REGISTRO_NOVEDADES
    registro = json.loads(registro_path.read_text(encoding="utf-8")) if registro_path.exists() else {}
    del_irpf = _leyes_del_irpf(data_dir)
    leyes = {norma.boe_id: norma.norma for norma in normas_de_parametros()} if data_dir == DATA_DIR else {}
    leyes.update({boe_id: nombre for _, boe_id, nombre in del_irpf})
    comunidades = {boe_id: clave for clave, boe_id, _ in del_irpf if clave is not None}
    lector = revisor = None
    motivo = "No hay credenciales del proveedor de LLM configurado: no se ha recodificado nada."
    if not usar_llm:
        motivo = "Ejecución sin modelo (--no-llm): no se ha recodificado nada."
    elif hay_credenciales_llm():
        lector, revisor = _proveedores_de_recodificacion()
    almacen = _almacen(proposals_dir)
    estado_path = output_dir / ESTADO_PILOTO
    try:
        informe, nuevas = piloto.pilotar(
            registro,
            leyes=leyes,
            comunidades=comunidades,
            texto_de=texto_de or _texto_del_boe(),
            estado_path=estado_path,
            almacen=almacen,
            data_dir=data_dir,
            lector=lector,
            revisor=revisor,
            max_iter=max_iter or settings.RECODE_MAX_ITER,
            max_grupos=max_grupos or settings.RECODE_MAX_PER_RUN,
            auto_aplicar=settings.RECODE_AUTO_APPLY if auto_aplicar is None else auto_aplicar,
            hoy=hoy_en_espana(),
            motivo_sin_modelo=motivo,
        )
    except RecodificacionError as exc:
        raise click.ClickException(str(exc)) from exc
    # The same unchanged situation is announced once, not every day.
    estado = piloto.leer_estado(estado_path)
    huella = piloto.huella_del_parte(informe)
    if notify and informe.hay_algo and huella != estado.ultimo_parte:
        cuerpo = piloto.texto_del_parte(informe, nuevas)
        for canal in _canales_del_operador():
            try:
                canal.enviar(piloto.ASUNTO_PARTE, cuerpo)
            except Exception as exc:  # the report is in the output and the proposals on disk either way
                logger.warning(
                    "Could not send the autopilot report on %s: %s: %s", canal.nombre, type(exc).__name__, exc
                )
            else:
                informe.avisado_por.append(canal.nombre)
        if informe.avisado_por:
            for propuesta in nuevas:
                almacen.guardar(
                    propuesta.model_copy(update={"avisada_por": sorted({*propuesta.avisada_por, *informe.avisado_por})})
                )
            estado.ultimo_parte = huella
            piloto.guardar_estado(estado_path, estado)
    return informe.model_dump()


@cli.command("params-autopilot")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./data/html"),
    show_default=True,
    help="Donde boe-novedades guarda su registro; aquí queda también el estado del piloto.",
)
@click.option(
    "--detect/--no-detect",
    default=True,
    show_default=True,
    help="Revisar antes el BOE de los días pendientes (lo mismo que boe-novedades).",
)
@click.option("--llm/--no-llm", "usar_llm", default=True, show_default=True, help="Permitir llamadas al LLM.")
@click.option("--max-iter", type=int, default=None, help="Pasadas del lector por grupo (por defecto, RECODE_MAX_ITER).")
@click.option("--max-grupos", type=int, default=None, help="Grupos por ejecución (por defecto, RECODE_MAX_PER_RUN).")
@click.option("--auto-apply/--no-auto-apply", default=None, help="Aplicar sin esperar lo que no deje nada por revisar.")
@click.option("--notify/--no-notify", default=True, show_default=True, help="Avisar al operador y a los usuarios.")
@click.option("--fail-on-error", is_flag=True, help="Terminar con código 1 si hubo errores o se agotó el presupuesto.")
@_SNAPSHOT_OPTION
@_PROPOSALS_OPTION
@_DATA_OPTION
@_DB_OPTION
def params_autopilot(
    output_dir: Path,
    detect: bool,
    usar_llm: bool,
    max_iter: int | None,
    max_grupos: int | None,
    auto_apply: bool | None,
    notify: bool,
    fail_on_error: bool,
    snapshot_dir: Path | None,
    proposals_dir: Path | None,
    data_dir: Path | None,
    db_path: str | None,
) -> None:
    """Mantiene los parámetros al día sin que nadie lo pida: detectar → verificar → recodificar → avisar.

    1) Lee el BOE de los días pendientes (como boe-novedades). 2) Para cada norma
    que modifica una ley de la que salen parámetros, localiza los grupos de los
    artículos que nombra: si el BOE ya la ha consolidado los relee del texto
    consolidado; si no, del texto de la propia norma (propuesta que nunca se
    auto-aplica) y los relee otra vez cuando llega la consolidación. 3) Verifica
    todos los ficheros de parámetros contra el texto vigente. 4) Lanza el bucle
    de recodificación sobre lo anterior, con los topes de RECODE_MAX_PER_RUN y
    del presupuesto diario, y aplica lo que no necesita a nadie si
    RECODE_AUTO_APPLY está activo. 5) Envía un parte al operador.

    Sin credenciales de LLM hace igualmente 1, 2 y 3 y dice qué hay que releer.
    Se puede repetir sin coste: no vuelve a pagar por un grupo ya propuesto
    para el mismo texto ni repite un parte que no ha cambiado.
    """
    salida: dict[str, Any] = {}
    if detect:
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            salida["novedades"] = _revisar_novedades(None, None, output_dir, notify=notify, db_path=db_path)
        except Exception as exc:  # the BOE being down must not stop the verification of what is already known
            salida["novedades"] = {"errores": [f"no se pudo revisar el BOE del día: {type(exc).__name__}: {exc}"]}
    salida["piloto"] = _pilotar(
        output_dir,
        texto_de=leer_snapshot(snapshot_dir) if snapshot_dir else None,
        data_dir=data_dir,
        proposals_dir=proposals_dir,
        max_iter=max_iter,
        max_grupos=max_grupos,
        auto_aplicar=auto_apply,
        notify=notify,
        usar_llm=usar_llm,
    )
    click.echo(json.dumps(salida, ensure_ascii=False, indent=2, default=str))
    piloto_ = salida["piloto"]
    agotado = (piloto_.get("recodificacion") or {}).get("presupuesto_agotado")
    if fail_on_error and (piloto_["errores"] or agotado or salida.get("novedades", {}).get("errores")):
        raise SystemExit(1)


@cli.command("params-gate")
@click.option("--ejercicio", "ejercicios", type=int, multiple=True, help="Ejercicio a comprobar (por defecto, todos).")
@_SNAPSHOT_OPTION
@click.option("--offline", is_flag=True, help="Solo lo que no necesita la ley (modelo y cifras dentro de las citas).")
@_DATA_OPTION
def params_gate(ejercicios: tuple[int, ...], snapshot_dir: Path | None, offline: bool, data_dir: Path | None) -> None:
    """Pasa por el control determinista del bucle todas las deducciones autonómicas publicadas, con sus citas.

    Es el mismo control que debe superar una propuesta: modelo válido, cada cifra
    dentro de una cita, ningún número en lo que lee el usuario que la ley no
    imprima y —salvo con --offline— cada cita literal y de una pieza en los
    artículos que el bucle leería. Sin LLM. Termina con código 1 si alguna falla.
    """
    texto_de = None if offline else (leer_snapshot(snapshot_dir) if snapshot_dir else _texto_del_boe())
    fallidas = control_de_lo_publicado(texto_de, data_dir=data_dir or DATA_DIR, ejercicios=list(ejercicios) or None)
    click.echo(json.dumps({"fallan": len(fallidas), "deducciones": fallidas}, ensure_ascii=False, indent=2))
    if fallidas:
        raise SystemExit(1)


@cli.command("params-eval")
@click.option("-n", "cuantos", type=int, default=10, show_default=True, help="Cuántos grupos publicados evaluar.")
@click.option(
    "--semilla", type=int, default=0, show_default=True, help="Semilla del sorteo (misma semilla, mismos grupos)."
)
@click.option("--tipo", "tipos", multiple=True, help="Solo grupos de este tipo (por defecto, deduccion_autonomica).")
@click.option("--comunidad", default=None, help="Solo esta comunidad (su clave) o «estado».")
@click.option("--ruta", "rutas", multiple=True, help="Evaluar este grupo concreto (repetible) en vez de sortear.")
@click.option("--max-iter", type=int, default=None, help="Pasadas del lector por grupo (por defecto, RECODE_MAX_ITER).")
@click.option(
    "--usd-por-millon", type=float, default=None, help="Precio por millón de tokens de entrada, para estimar el coste."
)
@click.option("--dry-run", is_flag=True, help="Decir qué grupos se evaluarían y cuánto texto se enviaría, sin LLM.")
@_SNAPSHOT_OPTION
@_DATA_OPTION
def params_eval(
    cuantos: int,
    semilla: int,
    tipos: tuple[str, ...],
    comunidad: str | None,
    rutas: tuple[str, ...],
    max_iter: int | None,
    usd_por_millon: float | None,
    dry_run: bool,
    snapshot_dir: Path | None,
    data_dir: Path | None,
) -> None:
    """Mide el bucle contra codificaciones ya publicadas: las oculta, las hace releer de la ley y compara.

    Toma N grupos de los ficheros de parámetros (deducciones autonómicas por
    defecto), oculta al lector su codificación y sus citas, lanza el bucle normal
    (mismo control, mismo revisor) y compara el resultado campo a campo con la
    codificación humana. No escribe nada en los parámetros ni guarda propuestas.
    Sirve para decidir, con tu modelo real, si activar RECODE_AUTO_APPLY.
    """
    from hacienda_gpt.tax.recodificacion import evaluacion

    data_dir = data_dir or DATA_DIR
    texto_de = leer_snapshot(snapshot_dir) if snapshot_dir else _texto_del_boe()
    try:
        if rutas:
            elegidos = [objetivo_de_ruta(None, r.strip("/").split("/"), data_dir=data_dir) for r in rutas]
        else:
            todos = todos_los_objetivos(data_dir=data_dir)
            elegidos = evaluacion.seleccionar(
                todos.objetivos, cuantos, semilla=semilla, tipos=tipos or ["deduccion_autonomica"], ambito=comunidad
            )
    except RecodificacionError as exc:
        raise click.ClickException(str(exc)) from exc
    if dry_run:
        click.echo(json.dumps({"grupos": [_estimacion(o, texto_de) for o in elegidos]}, ensure_ascii=False, indent=2))
        return
    if not hay_credenciales_llm():
        raise click.ClickException("No hay credenciales del proveedor de LLM configurado: no se puede evaluar.")
    lector, revisor = _proveedores_de_recodificacion()
    resultado = evaluacion.evaluar(
        elegidos,
        lector=lector,
        revisor=revisor,
        texto_de=texto_de,
        data_dir=data_dir,
        max_iter=max_iter or settings.RECODE_MAX_ITER,
        usd_por_millon_de_tokens=usd_por_millon,
    )
    click.echo(resultado.model_dump_json(indent=2))
    if resultado.interrumpida:
        raise SystemExit(1)


def _leer_propuesta(almacen: AlmacenPropuestas, identificador: str) -> Propuesta:
    try:
        return almacen.leer(identificador)
    except RecodificacionError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("params-proposals")
@click.option("--estado", type=click.Choice(["propuesta", "aplicada", "rechazada", "sin_converger"]), default=None)
@click.option("--show", "identificador", default=None, help="Mostrar entera la propuesta con este identificador.")
@click.option("--pendientes", is_flag=True, help="Listar lo recodificado y aplicado que nadie ha confirmado todavía.")
@_PROPOSALS_OPTION
@_DATA_OPTION
def params_proposals(
    estado: str | None, identificador: str | None, pendientes: bool, proposals_dir: Path | None, data_dir: Path | None
) -> None:
    """Lista las propuestas de recodificación, o muestra una (antes, después, citas, control, revisor, casos)."""
    almacen = _almacen(proposals_dir)
    if identificador is not None:
        click.echo(_leer_propuesta(almacen, identificador).model_dump_json(indent=2))
        return
    if pendientes:
        filas = [p.model_dump() for p in pendientes_de_confirmar(data_dir or DATA_DIR)]
        click.echo(json.dumps({"pendientes_de_confirmar": filas}, ensure_ascii=False, indent=2))
        return
    click.echo(json.dumps([resumen_de(p) for p in almacen.listar(estado)], ensure_ascii=False, indent=2, default=str))


@cli.command("params-apply")
@click.argument("identificador")
@_SNAPSHOT_OPTION
@_PROPOSALS_OPTION
@_DATA_OPTION
def params_apply(
    identificador: str, snapshot_dir: Path | None, proposals_dir: Path | None, data_dir: Path | None
) -> None:
    """Escribe una propuesta en los ficheros de parámetros de cada ejercicio que toca.

    Antes relee la ley y repite el control determinista: si el texto ha cambiado
    desde la propuesta, o el fichero ya no contiene lo que iba a sustituir, no
    escribe nada. Lo aplicado queda marcado como «pendiente de revisión humana»
    hasta params-confirm.
    """
    almacen = _almacen(proposals_dir)
    propuesta = _leer_propuesta(almacen, identificador)
    texto_de = leer_snapshot(snapshot_dir) if snapshot_dir else _texto_del_boe()
    try:
        aplicada = aplicar_propuesta(
            propuesta, texto_de, almacen=almacen, data_dir=data_dir or DATA_DIR, hoy=hoy_en_espana()
        )
    except RecodificacionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(resumen_de(aplicada), ensure_ascii=False, indent=2, default=str))


@cli.command("params-reject")
@click.argument("identificador")
@click.option("--motivo", default="", help="Por qué se descarta (queda en la propuesta).")
@_PROPOSALS_OPTION
def params_reject(identificador: str, motivo: str, proposals_dir: Path | None) -> None:
    """Descarta una propuesta: no se aplicará y no se volverá a intentar mientras la ley no cambie."""
    almacen = _almacen(proposals_dir)
    try:
        rechazada = rechazar_propuesta(_leer_propuesta(almacen, identificador), motivo=motivo, almacen=almacen)
    except RecodificacionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(resumen_de(rechazada), ensure_ascii=False, indent=2, default=str))


@cli.command("params-confirm")
@click.argument("identificador", required=False)
@click.option("--ejercicio", type=int, default=None, help="Solo lo pendiente de este ejercicio.")
@click.option("--comunidad", default=None, help="Solo lo pendiente de esta comunidad (o «estado»).")
@click.option("--todas", is_flag=True, help="Confirmar todo lo pendiente que cumpla los filtros.")
@_PROPOSALS_OPTION
@_DATA_OPTION
def params_confirm(
    identificador: str | None,
    ejercicio: int | None,
    comunidad: str | None,
    todas: bool,
    proposals_dir: Path | None,
    data_dir: Path | None,
) -> None:
    """Una persona ha revisado lo recodificado: quita la marca «pendiente de revisión humana».

    Con el identificador de una propuesta aplicada confirma lo que esa propuesta
    escribió; sin él, lo pendiente que cumpla --ejercicio / --comunidad (hace falta --todas).
    """
    data_dir = data_dir or DATA_DIR
    almacen = _almacen(proposals_dir)
    pendientes: list[Pendiente] = pendientes_de_confirmar(data_dir)
    if identificador is not None:
        propuesta = _leer_propuesta(almacen, identificador)
        pendientes = [
            p
            for p in pendientes
            if p.tipo == propuesta.tipo and p.ruta == propuesta.ruta and p.ejercicio in propuesta.ejercicios
        ]
    elif not todas:
        raise click.ClickException("Indica el identificador de una propuesta aplicada, o --todas (con filtros).")
    if ejercicio is not None:
        pendientes = [p for p in pendientes if p.ejercicio == ejercicio]
    if comunidad is not None:
        pendientes = [p for p in pendientes if (p.ruta[1] if p.ruta[0] == "comunidades" else "estado") == comunidad]
    hechas = confirmar(pendientes, data_dir=data_dir, almacen=almacen)
    click.echo(
        json.dumps(
            {"confirmadas": hechas, "grupos": [f"{p.ejercicio} {p.etiqueta}" for p in pendientes]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    cli()
