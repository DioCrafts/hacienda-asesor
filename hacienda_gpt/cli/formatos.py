"""Format drift commands, mounted as ``ops formatos …`` (see ``hacienda_gpt/cli/ops.py``).

Meant for cron, after the jobs that read the sources::

    # Estado de cada formato y, si hay modelo configurado, diagnóstico de los que fallan.
    30 9 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops formatos check --diagnose \
                    >> logs/ops.log 2>&1

See ``docs/operacion.md`` and ``docs/formatos_inventario.md``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
import urllib.request

import click

from hacienda_gpt import settings
from hacienda_gpt.ops import formatos
from hacienda_gpt.ops.formatos import FormatoError
from hacienda_gpt.ops.vigia import vigilado

logger = logging.getLogger(__name__)

BOE = "https://www.boe.es"
ASUNTO_FORMATOS = "HaciendaGPT: un formato externo ha dejado de entenderse"
PRECIOS_MAX_DIAS = 120


def _hoy() -> date:
    """Today (UTC). A function so that tests can pin it: the calendar check depends on the date."""
    return datetime.now(UTC).date()


def _canales() -> list[Any]:
    from hacienda_gpt.proactive.entrega import ConfiguracionCanalError, canales_configurados

    try:
        return list(canales_configurados())
    except ConfiguracionCanalError as exc:
        logger.warning("Channels are misconfigured: %s", exc)
        return []


def _avisar(asunto: str, lineas: list[str]) -> list[str]:
    """Send the operator a message through the configured channels; return those that accepted it."""
    avisados: list[str] = []
    cuerpo = "\n".join(f"- {linea}" for linea in lineas) + "\n"
    for canal in _canales():
        try:
            canal.enviar(asunto, cuerpo)
        except Exception as exc:
            logger.warning("Could not send the report on %s: %s: %s", canal.nombre, type(exc).__name__, exc)
        else:
            avisados.append(canal.nombre)
    return avisados


def _proveedores() -> tuple[Any, Any]:
    """Diagnosis and reflection providers, both metered against the daily budget."""
    from hacienda_gpt.agent.providers import create_provider
    from hacienda_gpt.ops.consumo import medir

    analista = medir(create_provider(model=settings.FORMAT_MODEL or None), "formatos")
    if settings.FORMAT_REVIEWER_MODEL and settings.FORMAT_REVIEWER_MODEL != settings.FORMAT_MODEL:
        revisor = medir(create_provider(model=settings.FORMAT_REVIEWER_MODEL), "formatos-revisor")
    else:
        revisor = analista.con_proposito("formatos-revisor")
    return analista, revisor


def modelos_configurados() -> dict[str, str]:
    """Model ids the app is configured to call, with the setting that names each (local models excluded)."""
    proveedor = settings.LLM_PROVIDER.strip().lower()
    base = {"anthropic": "ANTHROPIC_MODEL", "claude": "ANTHROPIC_MODEL", "openai": "OPENAI_AGENT_MODEL"}.get(proveedor)
    if base is None:
        return {}
    nombres = (base, "FORMAT_MODEL", "FORMAT_REVIEWER_MODEL", "RECODE_MODEL", "RECODE_REVIEWER_MODEL")
    if not settings.VERIFIER_PROVIDER or settings.VERIFIER_PROVIDER.strip().lower() == proveedor:
        nombres += ("VERIFIER_MODEL",)
    out: dict[str, str] = {}
    for nombre in nombres:
        modelo = str(getattr(settings, nombre, "") or "").strip()
        if modelo:
            out.setdefault(modelo, nombre)
    return out


def avisos_de_datos_propios(hoy: date | None = None) -> list[str]:
    """Bundled data that ages without anyone noticing (not a format, but the same silent failure)."""
    hoy = hoy or datetime.now(UTC).date()
    avisos: list[str] = []
    try:
        from hacienda_gpt.ops.consumo import cargar_precios

        precios = cargar_precios()
        consultado = date.fromisoformat(str(precios.consultado_el))
        if consultado > hoy:
            avisos.append(
                f"La tabla de precios de los modelos dice haberse consultado el {consultado.isoformat()}, "
                "una fecha futura: revisa «consultado_el»."
            )
        elif (hoy - consultado).days > PRECIOS_MAX_DIAS:
            avisos.append(
                f"La tabla de precios de los modelos se consultó el {consultado.isoformat()} (hace más de "
                f"{PRECIOS_MAX_DIAS} días): los costes y los límites en USD pueden estar desfasados."
            )
        # A configured model without a price is recorded at no cost: it never counts towards a USD limit.
        for modelo, ajuste in modelos_configurados().items():
            precio = precios.precio_de(modelo)
            if precio is None or precio.input is None or precio.output is None:
                avisos.append(
                    f"El modelo {modelo} ({ajuste}) no tiene precio de entrada y salida en la tabla: sus llamadas "
                    "se registran sin coste y no cuentan para los límites diarios en USD."
                )
    except Exception as exc:
        avisos.append(f"No se puede leer la tabla de precios de los modelos: {type(exc).__name__}: {exc}")
    return avisos


def _diagnosticar(
    fuentes: list[str] | None, *, force: bool, auto_apply: bool | None, max_iter: int | None, dry_run: bool
) -> dict[str, Any]:
    from hacienda_gpt.ops import formatos_agente

    pendientes = fuentes or formatos_agente.pendientes_de_diagnostico(forzar=force)
    if dry_run or not pendientes:
        return {"pendientes": pendientes, "llm": "no consultado", "resultados": []}
    if not formatos_agente.hay_llm():
        return {
            "pendientes": pendientes,
            "llm": "no configurado: se informa solo del estado determinista",
            "resultados": [],
        }
    try:
        analista, revisor = _proveedores()
    except Exception as exc:  # a misconfigured provider or budget must not hide the deterministic status
        return {"pendientes": pendientes, "llm": f"no disponible: {type(exc).__name__}: {exc}", "resultados": []}
    resultados = formatos_agente.diagnosticar_pendientes(
        analista, revisor=revisor, fuentes=pendientes, max_intentos=max_iter, auto_aplicar=auto_apply
    )
    return {"pendientes": pendientes, "llm": "consultado", "resultados": resultados}


@click.group("formatos")
def cli() -> None:
    """Deriva de formatos: ¿siguen entendiéndose el BOE, los boletines y los documentos?"""


@cli.command("check")
@click.option(
    "--diagnose/--no-diagnose", default=False, show_default=True, help="Diagnosticar con el LLM lo que falle."
)
@click.option("--notify/--no-notify", default=True, show_default=True, help="Avisar al operador por sus canales.")
@click.option("--fail-on-problem", is_flag=True, help="Termina con código 1 si alguna fuente está degradada o rota.")
def check(diagnose: bool, notify: bool, fail_on_problem: bool) -> None:
    """Estado de cada formato externo según las últimas lecturas reales. No usa ningún LLM.

    Con --diagnose, además, pide al modelo un diagnóstico de las fuentes que fallan
    (solo si hay un modelo configurado; si no, lo dice y sigue).
    """
    with vigilado("formatos"):
        # The calendar data is checked here: it is ours, it needs no network and it ages by the day.
        from hacienda_gpt.ops.formatos_calendario import observar_calendario

        observar_calendario(_hoy())
        estados = formatos.resumen()
        problemas = formatos.problemas()
        salida: dict[str, Any] = {
            "fuentes": [item.model_dump(mode="json") for item in estados if item.estado != "sin_datos"],
            "sin_datos": [item.fuente for item in estados if item.estado == "sin_datos"],
            "problemas": problemas,
            "otros_avisos": avisos_de_datos_propios(),
        }
        if diagnose:
            salida["diagnostico"] = _diagnosticar(None, force=False, auto_apply=None, max_iter=None, dry_run=False)
        if notify and problemas:
            lineas = list(problemas)
            for resultado in (salida.get("diagnostico") or {}).get("resultados", []):
                lineas.append(
                    f"diagnóstico de {resultado['fuente']}: {resultado['resultado']}"
                    + (f" ({resultado['propuesta']})" if resultado.get("propuesta") else "")
                    + (f" — {resultado.get('motivo')}" if resultado.get("motivo") else "")
                )
            salida["avisado_por"] = _avisar(ASUNTO_FORMATOS, lineas)
        click.echo(json.dumps(salida, ensure_ascii=False, indent=2))
    if fail_on_problem and problemas:
        raise SystemExit(1)


SONDA_NORMA = "BOE-A-2006-20764"  # Ley 35/2006 (IRPF): the law most parameters come from
_AGENTE = "hacienda-asesor/0.1 (vigilancia de formatos)"
_FALLO: Any = object()


def _descargar(url: str, accept: str) -> str | None:
    """GET ``url``; None on 404. Other errors propagate (the probe reports them)."""
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": _AGENTE})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return str(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def sondear(hoy: date, descargar: Any = _descargar) -> list[str]:
    """Read one fresh payload of each BOE format with the real parsers, so the watch has something to judge.

    Returns the errors met (network, HTTP). What was read is recorded by the
    parsers themselves, exactly as in a scheduled run.
    """
    from hacienda_gpt.ops.formatos_boe import ReglasMetadatos, entrada_metadatos
    from hacienda_gpt.proactive.impacto import articulos_modificados
    from hacienda_gpt.proactive.novedades import SECCION_GENERAL, items_del_sumario, leer_analisis

    errores: list[str] = []

    def intentar(nombre: str, accion: Any) -> Any:
        try:
            return accion()
        except Exception as exc:
            errores.append(f"{nombre}: {type(exc).__name__}: {exc}")
            return _FALLO

    for atras in range(1, 8):
        dia = hoy - timedelta(days=atras)
        crudo = intentar(
            f"sumario {dia.isoformat()}",
            lambda dia=dia: descargar(BOE + f"/datosabiertos/api/boe/sumario/{dia:%Y%m%d}", "application/json"),
        )
        if crudo is _FALLO:
            # Not knowing is not the same as "there was no summary that day".
            continue
        formatos.observar("boe_sumario_dias", None, extraccion={"dia": dia.isoformat(), "hay": crudo is not None})
        if crudo is None:
            continue
        try:
            payload = json.loads(crudo)
        except ValueError:
            payload = {}
        items = intentar("sumario", lambda payload=payload, dia=dia: items_del_sumario(payload, dia))
        items = [] if items is _FALLO else items
        formatos.observar(
            "boe_sumario",
            crudo,
            extraccion=[item.model_dump(mode="json", exclude={"fecha"}) for item in items],
            contexto=dia.isoformat(),
        )
        general = next((item for item in items if item.seccion == SECCION_GENERAL), None)
        if general is not None:
            xml = intentar(
                f"xml {general.boe_id}",
                lambda: descargar(BOE + f"/diario_boe/xml.php?id={general.boe_id}", "application/xml"),
            )
            if xml and xml is not _FALLO:
                intentar("análisis", lambda: leer_analisis(xml))
                formatos.observar("boe_documento_xml", xml, contexto=general.boe_id)
        break

    crudo = intentar(
        "metadatos",
        lambda: descargar(
            BOE + f"/datosabiertos/api/legislacion-consolidada/id/{SONDA_NORMA}/metadatos", "application/json"
        ),
    )
    if crudo and crudo is not _FALLO:
        formatos.observar("boe_metadatos", crudo, contexto=SONDA_NORMA)
        intentar(
            "metadatos",
            lambda: entrada_metadatos(
                formatos.reglas_de("boe_metadatos", ReglasMetadatos), json.loads(crudo), SONDA_NORMA
            ),
        )
    pagina = intentar("texto consolidado", lambda: descargar(BOE + f"/buscar/act.php?id={SONDA_NORMA}", "text/html"))
    if pagina and pagina is not _FALLO:
        # ``articulos_modificados`` reports to the watch by itself.
        intentar("texto consolidado", lambda: articulos_modificados(pagina, desde=hoy - timedelta(days=3650)))
    return errores


def con_memoria(descargar: Any) -> Any:
    """``descargar`` that fetches each URL once per probe (the IRPF law is read by both probes)."""
    cache: dict[tuple[str, str], str | None] = {}

    def una_vez(url: str, accept: str) -> str | None:
        if (url, accept) not in cache:
            cache[(url, accept)] = descargar(url, accept)
        return cache[(url, accept)]

    return una_vez


SONDEADAS = (
    "boe_sumario",
    "boe_sumario_dias",
    "boe_documento_xml",
    "boe_metadatos",
    "boe_consolidado_html",
    "calendario_fiscal",
    "calendario_boe",
)


@cli.command("probe")
@click.option("--fail-on-problem", is_flag=True, help="Termina con código 1 si algo falla o no se pudo leer.")
def probe(fail_on_problem: bool) -> None:
    """Lee ahora del BOE una muestra de cada formato y dice si se entiende. No usa ningún LLM.

    Comprueba además el calendario fiscal: su coherencia y que los artículos del
    BOE que fijan sus plazos siguen diciendo lo mismo. Para ejecutarlo donde no
    corren las tareas diarias (GitHub Actions) o para comprobar un cambio de
    spec. Solo necesita acceso a www.boe.es.
    """
    from hacienda_gpt.ops.formatos_calendario import sondear_calendario

    if not settings.FORMAT_WATCH:
        raise click.ClickException("FORMAT_WATCH está desactivado: la sonda no registraría nada.")
    with vigilado("formatos-probe"):
        hoy = _hoy()
        descargar = con_memoria(_descargar)
        errores = sondear(hoy, descargar) + sondear_calendario(hoy, descargar)
        problemas = formatos.problemas()
        sondeadas = SONDEADAS
        estados = {item.fuente: item.estado for item in formatos.resumen() if item.fuente in sondeadas}
        click.echo(
            json.dumps({"estados": estados, "problemas": problemas, "errores": errores}, ensure_ascii=False, indent=2)
        )
    sin_leer = [fuente for fuente, estado in estados.items() if estado == "sin_datos"]
    if fail_on_problem and (problemas or errores or sin_leer):
        raise SystemExit(1)


@cli.command("diagnose")
@click.option("--fuente", "fuentes", multiple=True, help="Solo esta fuente (repetible). Por defecto, las que fallan.")
@click.option("--force", is_flag=True, help="Volver a diagnosticar un fallo que ya tiene propuesta o informe.")
@click.option("--auto-apply/--no-auto-apply", default=None, help="Aplicar sin esperar lo que supere ensayo y revisión.")
@click.option("--max-iter", type=int, default=None, help="Rondas por fuente (por defecto, FORMAT_MAX_ITER).")
@click.option("--notify/--no-notify", default=True, show_default=True, help="Avisar al operador por sus canales.")
@click.option("--dry-run", is_flag=True, help="Decir qué se diagnosticaría, sin llamar al LLM.")
def diagnose(
    fuentes: tuple[str, ...], force: bool, auto_apply: bool | None, max_iter: int | None, notify: bool, dry_run: bool
) -> None:
    """Pide al modelo que explique qué ha cambiado en las fuentes que fallan y proponga una spec nueva.

    Cada propuesta se ensaya de forma determinista sobre la muestra buena y la
    nueva, y la revisa una segunda llamada independiente. Nada se aplica solo
    salvo con FORMAT_AUTO_APPLY (o --auto-apply).
    """
    try:
        salida = _diagnosticar(
            list(fuentes) or None, force=force, auto_apply=auto_apply, max_iter=max_iter, dry_run=dry_run
        )
    except FormatoError as exc:
        raise click.ClickException(str(exc)) from exc
    if notify and salida["resultados"]:
        salida["avisado_por"] = _avisar(
            ASUNTO_FORMATOS,
            [
                f"{r['fuente']}: {r['resultado']}"
                + (f" ({r['propuesta']}, {r.get('estado')})" if r.get("propuesta") else "")
                + (f" — {r.get('que_ha_cambiado') or r.get('motivo') or ''}")
                for r in salida["resultados"]
            ],
        )
    click.echo(json.dumps(salida, ensure_ascii=False, indent=2))
    if any(r["resultado"] == "error_llm" for r in salida["resultados"]):
        raise SystemExit(1)


def _resumen_de(propuesta: formatos.Propuesta) -> dict[str, Any]:
    return {
        "id": propuesta.id,
        "fuente": propuesta.fuente,
        "tipo": propuesta.tipo,
        "estado": propuesta.estado,
        "creada": propuesta.creada,
        "que_ha_cambiado": propuesta.que_ha_cambiado,
        "ensayo_superado": propuesta.ensayo.ok if propuesta.ensayo else None,
        "informe": propuesta.informe,
    }


@cli.command("proposals")
@click.option(
    "--estado",
    type=click.Choice(["propuesta", "aplicada", "rechazada", "sin_converger", "confirmada"]),
    default=None,
)
@click.option("--show", "identificador", default=None, help="Mostrar entera la propuesta con este identificador.")
def proposals(estado: str | None, identificador: str | None) -> None:
    """Lista las propuestas de spec y los informes para desarrollador."""
    try:
        if identificador:
            click.echo(
                json.dumps(formatos.leer_propuesta(identificador).model_dump(mode="json"), ensure_ascii=False, indent=2)
            )
            return
    except FormatoError as exc:
        raise click.ClickException(str(exc)) from exc
    todas = [p for p in formatos.listar_propuestas() if estado is None or p.estado == estado]
    click.echo(json.dumps([_resumen_de(p) for p in todas], ensure_ascii=False, indent=2))


def _resolver(accion: Any, identificador: str, **kwargs: Any) -> None:
    try:
        propuesta = accion(identificador, **kwargs)
    except FormatoError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(_resumen_de(propuesta), ensure_ascii=False, indent=2))


@cli.command("apply")
@click.argument("identificador")
def apply(identificador: str) -> None:
    """Pone en vigor la spec de una propuesta (vuelve a pasar el ensayo determinista antes)."""
    _resolver(formatos.aplicar, identificador, por="manual")


@cli.command("reject")
@click.argument("identificador")
@click.option("--motivo", default="", help="Por qué se descarta (queda en la propuesta).")
def reject(identificador: str, motivo: str) -> None:
    """Descarta una propuesta; si ya estaba aplicada, restaura la spec anterior."""
    _resolver(formatos.rechazar, identificador, motivo=motivo)


@cli.command("confirm")
@click.argument("identificador")
@click.option("--nota", default="", help="Comentario de quien revisa.")
def confirm(identificador: str, nota: str) -> None:
    """Da por revisada una spec aplicada, o por leído un informe para desarrollador."""
    _resolver(formatos.confirmar, identificador, nota=nota)


@cli.command("sources")
def sources() -> None:
    """Qué fuentes se vigilan, qué da por supuesto cada lector y con qué spec."""
    salida = []
    for clave, fuente in sorted(formatos.fuentes().items()):
        spec = formatos.spec_activa(clave) if fuente.reglas is not None else None
        salida.append(
            {
                "fuente": clave,
                "descripcion": fuente.descripcion,
                "expectativas": list(fuente.expectativas),
                "spec": {"version": spec.version, "origen": spec.origen} if spec else None,
                "codigo": fuente.codigo,
                "ruta_specs_aplicadas": str(Path(settings.FORMAT_DIR) / "specs"),
            }
        )
    click.echo(json.dumps(salida, ensure_ascii=False, indent=2))
