"""Deterministic proactive jobs: deadline reminders and the year-end plan.

Neither job calls an LLM. Dates come from the fiscal calendar and amounts from
the IRPF calculator (through the optimiser for the year-end plan), so a daily
run over every case costs nothing and can only say what those modules already
stand behind.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
import hashlib
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hacienda_gpt.decision.schemas import CaseState, CaseStatus, parse_fiscal_year
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.store import Notification, NotificationStore
from hacienda_gpt.tax.calendario import proximos_vencimientos
from hacienda_gpt.tax.optimizador import PalancaElegida, optimizar_desde_hechos
from hacienda_gpt.tax.params import EjercicioNoDisponibleError, ejercicios_disponibles
from hacienda_gpt.tax.profile import DatosInvalidosError
from hacienda_gpt.tax.simulador import simular

logger = logging.getLogger(__name__)

URGENT_DAYS = 7
MAX_AVISOS = 3
MAX_POR_CONFIRMAR = 6
# The calendar's generic "last day to act" entry: the year-end plan covers it.
_CIERRE_EJERCICIO_ID = "irpf_cierre_ejercicio"
# Without any of these there is nothing to optimise.
INCOME_FACTS = (
    "rendimiento_trabajo_bruto",
    "rendimiento_actividad_neto",
    "rendimiento_alquiler_neto",
    "rendimiento_alquiler_vivienda_sin_reducir",
    "rendimiento_capital_mobiliario",
    "ganancias_patrimoniales",
)
# How to ask the user for a profile field, in their words.
ETIQUETAS_DATOS: dict[str, str] = {
    "estado_civil": "tu estado civil",
    "comunidad_autonoma": "la comunidad autónoma donde resides",
    "num_descendientes": "cuántos hijos tienes a tu cargo",
    "num_descendientes_menores_3": "cuántos de tus hijos tienen menos de tres años",
    "num_ascendientes_mayores_65": "si convives con padres o abuelos mayores de 65 años a tu cargo",
    "num_ascendientes_discapacidad": "si convives con padres o abuelos con una discapacidad reconocida del 33 % o más",
    "num_descendientes_discapacidad": "cuántos de tus hijos tienen una discapacidad reconocida del 33 % o más",
    "pagos_vivienda_habitual_pre2013": "lo que pagas al año de hipoteca, si compraste tu vivienda habitual antes de 2013",
    "alquiler_vivienda_habitual": "lo que pagas al año de alquiler por tu vivienda habitual",
    "gastos_escolaridad": "tus gastos de escolaridad del año",
    "cotizaciones_empleada_hogar": "las cotizaciones que pagas por una persona empleada de hogar",
    "hijos_nacidos_ultimos_3_ejercicios": "cuántos hijos han nacido o has adoptado en los tres últimos años",
    "rendimiento_trabajo_bruto": "tu salario o pensión bruta anual",
    "cotizaciones_seguridad_social": "lo que cotizas al año a la Seguridad Social",
    "retenciones": "las retenciones de IRPF que te han practicado en el año",
}


def hoy_en_espana() -> date:
    """Today's date in mainland Spain (deadlines are Spanish calendar days)."""
    try:
        return datetime.now(ZoneInfo("Europe/Madrid")).date()
    except ZoneInfoNotFoundError:  # pragma: no cover - only without a tz database
        return datetime.now(UTC).date()


def facts_de(case: CaseState) -> dict[str, Any]:
    return {fact.name: fact.value for fact in case.facts}


def _eur(amount: Decimal) -> str:
    """Spanish money format: 1.234,56 €."""
    text = f"{abs(amount):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{text} €"


def _fecha(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _dias(n: int) -> str:
    return "1 día" if n == 1 else f"{n} días"


def _etiqueta(campo: str) -> str:
    return ETIQUETAS_DATOS.get(campo, campo.replace("_", " "))


def _tiene_ingresos(facts: dict[str, Any]) -> bool:
    for name in INCOME_FACTS:
        value = facts.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool) and value != 0:
            return True
    return False


def _facts_hash(facts: dict[str, Any]) -> str:
    canonical = json.dumps(facts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Deadline reminders
# --------------------------------------------------------------------------- #


def recordatorios_de_plazos(case: CaseState, *, hoy: date, store: NotificationStore, dias: int = 30) -> int:
    """One notification per filing deadline within ``dias`` days. Returns how many were created.

    Each deadline notifies once (the key carries the deadline id), so the
    severity is the one it had on the day the reminder was written.
    """
    created = 0
    for v in proximos_vencimientos(facts_de(case), hoy=hoy, dias=dias):
        if v.id.split(":", 1)[0] == _CIERRE_EJERCICIO_ID:
            continue
        nombre = f"Modelo {v.modelo} · {v.titulo}" if v.modelo else v.titulo
        if v.dias_restantes == 0:
            cuando = "vence hoy"
        elif v.dias_restantes == 1:
            cuando = "vence mañana"
        else:
            cuando = f"vence el {_fecha(v.fecha)}"
        lines = [
            f"**{nombre}** ({v.periodo}): el plazo termina el {_fecha(v.fecha)}. "
            f"A {_fecha(hoy)} quedan {_dias(v.dias_restantes)}.",
            v.detalle,
        ]
        if v.nota:
            lines.append(v.nota)
        if not v.verificado:
            lines.append(
                "Atención: esta fecha no está verificada contra la norma o la orden que la fija para este periodo. "
                "Confírmala en la sede electrónica de la Agencia Tributaria antes de darla por buena."
            )
        actions = [f"Presenta o prepara «{v.titulo}» ({v.periodo}) como muy tarde el {_fecha(v.fecha)}."]
        if not v.verificado:
            actions.append("Confirma la fecha exacta en el calendario del contribuyente de la Agencia Tributaria.")
        actions.append("Pregunta al asesor si tienes dudas sobre si te corresponde presentarlo o cómo hacerlo.")
        created += store.add(
            Notification(
                case_id=case.case_id,
                kind="plazo",
                key=f"plazo:{v.id}",
                title=f"{nombre} ({v.periodo}): {cuando}",
                body="\n\n".join(lines),
                severity="urgente" if v.dias_restantes <= URGENT_DAYS else "aviso",
                actions=actions,
                sources=[v.referencia],
                due_date=v.fecha,
            )
        )
    return created


# --------------------------------------------------------------------------- #
# Year-end plan
# --------------------------------------------------------------------------- #


def _elegir_avisos(avisos: list[str]) -> list[str]:
    """At most ``MAX_AVISOS`` caveats; the one about the approximated regional part always goes first."""
    aproximada = [a for a in avisos if "se ha aproximado" in a]
    resto = [a for a in avisos if a not in aproximada]
    return (aproximada + resto)[:MAX_AVISOS]


def _palanca(index: int, p: PalancaElegida) -> str:
    """One lever of the combined plan: what to do, what it is worth inside the plan and on its own."""
    naturaleza = {
        "ahorro_definitivo": "ahorro definitivo",
        "diferimiento": "diferimiento: lo recuperas más adelante",
        "requiere_gastar": f"requiere gastar: coste neto {_eur(p.coste_neto or Decimal(0))}",
    }[p.naturaleza]
    linea = f"{index}. **{p.nombre}** — {p.accion}. Ahorro dentro del plan: {_eur(p.ahorro_marginal)} ({naturaleza})"
    if p.ahorro_aislado != p.ahorro_marginal:
        linea += (
            f"; por sí sola ahorraría {_eur(p.ahorro_aislado)}"
            if p.ahorro_aislado >= 0
            else f"; por sí sola costaría {_eur(-p.ahorro_aislado)} más"
        )
    parts = [linea + "."]
    if p.requisitos:
        parts.append("   Ten en cuenta: " + " ".join(p.requisitos))
    parts.append(f"   Referencia: {p.referencia}" + ("" if p.verificado else " (parámetro no verificado)"))
    return "\n".join(parts)


def plan_fin_de_ejercicio(case: CaseState, *, hoy: date, store: NotificationStore) -> int:
    """Year-end optimiser: what the user can still do before 31 December. Returns 0 or 1.

    Acts only from 1 October to 31 December and only for a case whose fiscal
    year is the current one. The plan is the optimiser's best COMBINATION of
    levers (:mod:`hacienda_gpt.tax.optimizador`), with each lever's marginal
    contribution: standalone savings are never added up. What the case keeps for
    the optimiser — the spouse's figures (``datos_conyuge``, for a married user)
    and the preferences (``preferencias_optimizacion``: cash for pension plans,
    illiquidity, the donation, latent positions) — is used as stated, and the
    plan's assumptions say so. It is rewritten when the profile changes (the key
    carries a hash of the facts, those two included) and replaces the previous one.
    """
    ejercicio = parse_fiscal_year(case.tax_period)
    if ejercicio != hoy.year or not date(hoy.year, 10, 1) <= hoy <= date(hoy.year, 12, 31):
        return 0
    if ejercicio not in ejercicios_disponibles():
        return 0
    facts = facts_de(case)
    fin = date(ejercicio, 12, 31)
    quedan = (fin - hoy).days
    plazo = (
        "Hoy, 31 de diciembre, es el último día"
        if quedan == 0
        else f"Quedan {_dias(quedan)} hasta el 31 de diciembre, el último día"
    ) + f" para tomar decisiones con efecto en tu IRPF de {ejercicio}."
    prefix = f"fin_de_ejercicio:{ejercicio}:"

    if not _tiene_ingresos(facts):
        # Never replaces an existing plan, and is written once per fiscal year.
        if any(n.key.startswith(prefix) for n in store.list(case.case_id)):
            return 0
        faltan = ["tu salario o pensión bruta anual, o los demás ingresos que hayas tenido en el año"] + [
            _etiqueta(name)
            for name in ("retenciones", "cotizaciones_seguridad_social", "comunidad_autonoma")
            if name not in facts
        ]
        return int(
            store.add(
                Notification(
                    case_id=case.case_id,
                    kind="fin_de_ejercicio",
                    key=prefix + "faltan_datos",
                    title=f"Fin de ejercicio {ejercicio}: faltan tus cifras para preparar el plan",
                    body=(
                        f"{plazo}\n\n"
                        "Todavía no tengo ninguna cifra de tus ingresos de este año (salario o pensión, actividad "
                        "económica, alquileres, intereses, dividendos o ganancias), así que no puedo estimar tu "
                        "declaración ni decirte qué te conviene hacer antes de fin de año.\n\n"
                        "Para preparar tu plan necesito:\n" + "\n".join(f"- {item}" for item in faltan)
                    ),
                    severity="info",
                    actions=[f"Indica al asesor {item}." for item in faltan]
                    + ["Si lo prefieres, sube tu última nómina o tu certificado de retenciones y los leo de ahí."],
                    sources=["art. 12 LIRPF (BOE-A-2006-20764)"],
                    due_date=fin,
                )
            )
        )

    try:
        # The benefit catalogue only feeds the "one datum away" section; the plan is the optimiser's.
        simulacion = simular(ejercicio=ejercicio, facts=facts, con_beneficios=True)
        plan = optimizar_desde_hechos(ejercicio=ejercicio, fecha=hoy, facts=facts)
    except EjercicioNoDisponibleError:
        return 0
    except DatosInvalidosError as exc:
        logger.warning("Year-end plan skipped for case %s: %s", case.case_id, exc)
        return 0

    resultado = plan.base.declaraciones[0].resultado
    por_confirmar = [b for b in simulacion.beneficios if b.estado == "por_confirmar" and b.datos_que_faltan]
    total = plan.ahorro_total if plan.palancas else Decimal(0)

    if plan.base.resultado_total > 0:
        estimacion = f"a pagar {_eur(plan.base.resultado_total)}"
    elif plan.base.resultado_total < 0:
        estimacion = f"a devolver {_eur(plan.base.resultado_total)}"
    else:
        estimacion = "ni a pagar ni a devolver (0,00 €)"
    datos_de_hoy = "vuestros datos de hoy (los dos cónyuges)" if plan.con_conyuge else "tus datos de hoy"
    sections = [
        plazo,
        f"**Resultado estimado con {datos_de_hoy}:** {estimacion} "
        f"(cuota líquida estimada: {_eur(plan.base.cuota_liquida_total)}).",
    ]
    if simulacion.datos_no_aportados:
        sections.append(
            "No me consta "
            + "; ni ".join(_etiqueta(name) for name in simulacion.datos_no_aportados)
            + ". Lo he tomado como cero, así que el resultado real será distinto hasta que lo indiques."
        )

    if plan.palancas:
        optimo = plan.optimo.resultado_total
        destino = f"a pagar {_eur(optimo)}" if optimo > 0 else f"a devolver {_eur(optimo)}" if optimo < 0 else "0,00 €"
        if not plan.con_conyuge:
            quedaria = "tu declaración quedaría"
        elif plan.optimo.tributacion == "conjunta_biparental":
            quedaria = "vuestra declaración conjunta quedaría"
        else:
            quedaria = "vuestras dos declaraciones quedarían, sumadas,"
        block = [
            f"**Qué puedes hacer antes de fin de año** — haciendo todo a la vez, {quedaria} {destino}: "
            f"{_eur(total)} menos."
        ]
        block += [_palanca(i, p) for i, p in enumerate(plan.palancas, start=1)]
        block.append(
            "El ahorro de cada medida es lo que aporta dentro del plan combinado (lo que pagarías de más sin ella): "
            "las medidas interactúan y sus ahorros por separado no se pueden sumar."
        )
        if len(plan.palancas) > 1 and plan.interaccion:
            block.append(
                f"Juntas ahorran {_eur(abs(plan.interaccion))} {'más' if plan.interaccion > 0 else 'menos'} que la "
                f"suma de cada una por separado ({_eur(plan.suma_ahorros_aislados)})."
            )
            block += [f"- {motivo}" for motivo in plan.explicacion_interaccion]
        sections.append("\n".join(block))
    else:
        sections.append(
            "Con los datos actuales, la calculadora no encuentra ninguna medida con ahorro estimado que puedas "
            "tomar antes de fin de año."
        )
    donativo = next((nd for nd in plan.no_disponibles if nd.id == "donativo" and nd.causa == "restricciones"), None)
    if donativo is not None:
        sections.append(f"**Si pensabas donar** (no lo incluyo en el plan: requiere gastar). {donativo.detalle}")
    if plan.supuestos:
        sections.append("**Supuestos del plan**\n" + "\n".join(f"- {s}" for s in plan.supuestos))

    if por_confirmar:
        block = ["**Podría corresponderte y solo falta un dato**"]
        block += [
            f"- {b.nombre}: indícame {', '.join(_etiqueta(name) for name in b.datos_que_faltan)}."
            for b in por_confirmar[:MAX_POR_CONFIRMAR]
        ]
        sections.append("\n".join(block))

    limites = [f"- {aviso}" for aviso in _elegir_avisos(resultado.avisos)]
    limites.append(
        "- Son estimaciones de una calculadora con los límites indicados, no una liquidación: confirma los importes "
        "antes de decidir."
    )
    sections.append("**Límites de esta estimación**\n" + "\n".join(limites))

    actions = [f"{p.accion}. Ahorro dentro del plan: {_eur(p.ahorro_marginal)}." for p in plan.palancas]
    actions += [
        f"Indica al asesor {', '.join(_etiqueta(name) for name in b.datos_que_faltan)} " f"para comprobar «{b.nombre}»."
        for b in por_confirmar[:MAX_POR_CONFIRMAR]
    ]
    actions.append("Pide al asesor que simule cada opción con tus cifras antes de mover dinero.")

    sources: list[str] = []
    for reference in [*(p.referencia for p in plan.palancas), "art. 12 LIRPF (BOE-A-2006-20764)", resultado.fuente]:
        if reference and reference not in sources:
            sources.append(reference)

    key = prefix + _facts_hash(facts)
    added = store.add(
        Notification(
            case_id=case.case_id,
            kind="fin_de_ejercicio",
            key=key,
            title=(
                f"Plan de fin de ejercicio {ejercicio}: {_eur(total)} de ahorro estimado"
                if total > 0
                else f"Plan de fin de ejercicio {ejercicio}: tu situación antes del 31 de diciembre"
            ),
            body="\n\n".join(sections),
            severity="urgente" if quedan <= URGENT_DAYS and total > 0 else "aviso" if total > 0 else "info",
            actions=actions,
            sources=sources,
            amount=total if total > 0 else None,
            due_date=fin,
        )
    )
    if added:
        # A plan computed from an older profile (or the request for figures) is now stale.
        store.remove_superseded(case.case_id, key_prefix=prefix, keep_key=key)
    return int(added)


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #


def ejecutar_para_caso(case: CaseState, *, hoy: date, store: NotificationStore) -> dict[str, int]:
    """Run both deterministic jobs for one case; notifications created per kind."""
    return {
        "plazo": recordatorios_de_plazos(case, hoy=hoy, store=store),
        "fin_de_ejercicio": plan_fin_de_ejercicio(case, hoy=hoy, store=store),
    }


def ejecutar_para_todos(
    case_store: SQLiteCaseStateStore,
    store: NotificationStore,
    *,
    hoy: date,
    modulos_de: Callable[[CaseState], Sequence[Any]] | None = None,
    auth: Any | None = None,
) -> dict[str, Any]:
    """Run both jobs over every case that is not closed. One failing case never stops the batch.

    Then the household modules switched on for each case's owner (``modulos_de``; by default the owner's
    choices in ``auth``, or the defaults) write their notices (see :mod:`hacienda_gpt.proactive.bandeja`). The
    summary gains ``modulos`` (notices per module) and ``errores_modulos`` only when some module beyond the
    tax one is on, so a tax-only deployment reports exactly what it always did.
    """
    summary: dict[str, Any] = {"hoy": hoy.isoformat(), "cases": 0, "plazo": 0, "fin_de_ejercicio": 0, "errors": 0}
    for case in case_store.list_all_cases():
        if case.status == CaseStatus.CLOSED:
            continue
        summary["cases"] += 1
        try:
            created = ejecutar_para_caso(case, hoy=hoy, store=store)
        except Exception:
            logger.exception("Proactive jobs failed for case %s", case.case_id)
            summary["errors"] += 1
            continue
        summary["plazo"] += created["plazo"]
        summary["fin_de_ejercicio"] += created["fin_de_ejercicio"]

    from hacienda_gpt.proactive.bandeja import avisos_de_modulos_para_todos

    def por_defecto(case: CaseState) -> Sequence[Any]:
        from hacienda_gpt.modulos.preferencias import modulos_activos_para_caso

        return modulos_activos_para_caso(case.case_id, auth)

    try:
        modulos = avisos_de_modulos_para_todos(case_store, store, hoy=hoy, modulos_de=modulos_de or por_defecto)
    except Exception:  # the household modules never take the tax jobs down with them
        logger.exception("Household module notices failed")
        modulos = {"modulos": {}, "errores_modulos": 1}
    if modulos["modulos"] or modulos["errores_modulos"]:
        summary.update(modulos)
    return summary
