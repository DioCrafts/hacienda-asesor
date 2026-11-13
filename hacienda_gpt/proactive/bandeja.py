"""The unified inbox: every module's proactive notices, and the weekly digest built from them.

* :func:`avisos_de_modulos` runs ``avisos`` of each household module switched on for a case and stores what
  it returns as notifications of kind ``modulo:<clave>``, keyed ``modulo:<clave>:<aviso.clave>`` (so two
  modules can never collide, nor collide with the tax keys). Storing is idempotent (same key, no second
  write); ``sustituye_prefijo`` removes the module's earlier notices with that key prefix once the new one
  is stored. A module that raises, or returns something that is not a valid notice, is logged and counted;
  the other modules still run.
* :func:`resumen_semanal` writes ONE message per case and ISO week with what is open in the inbox — what is
  due, the amounts the notices announce, the actions waiting for the user's decision — ordered by urgency.
  It is built deterministically from the notices and actions already stored: no model is involved, and no
  figure from two modules is ever added up.

The tax module keeps its own runner (:func:`hacienda_gpt.proactive.jobs.ejecutar_para_caso`), so its
notices are written exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
import logging
import re
from typing import Any
import unicodedata

from pydantic import ValidationError

from hacienda_gpt.decision.schemas import CaseState, CaseStatus
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.modulos.contrato import AvisoModulo, ContextoModulo, Modulo
from hacienda_gpt.proactive.store import KIND_RESUMEN, PREFIJO_MODULO, Notification, NotificationStore, modulo_de

logger = logging.getLogger(__name__)

# More than this from one module in one run is a bug in the module, not news for the user.
MAX_AVISOS_POR_MODULO = 50
_RANGO = {"urgente": 0, "aviso": 1, "info": 2}


def kind_de(clave: str) -> str:
    return f"{PREFIJO_MODULO}{clave}"


def clave_de(clave_modulo: str, clave_aviso: str) -> str:
    return f"{PREFIJO_MODULO}{clave_modulo}:{clave_aviso}"


def _notificacion(case_id: str, modulo: Modulo, aviso: AvisoModulo) -> Notification:
    if not isinstance(aviso, AvisoModulo):
        raise TypeError(f"no es un AvisoModulo: {type(aviso).__name__}")
    if not isinstance(aviso.clave, str) or not aviso.clave.strip():
        raise ValueError("aviso sin clave")
    return Notification(
        case_id=case_id,
        kind=kind_de(modulo.clave),
        key=clave_de(modulo.clave, aviso.clave),
        title=aviso.titulo,
        body=aviso.cuerpo,
        severity=aviso.gravedad,
        actions=list(aviso.acciones),
        sources=list(aviso.fuentes),
        amount=Decimal(aviso.importe) if aviso.importe is not None else None,
        due_date=aviso.vence,
    )


def avisos_de_modulos(
    case: CaseState, *, hoy: date, store: NotificationStore, modulos: Sequence[Modulo]
) -> tuple[dict[str, int], list[str]]:
    """Run every module's ``avisos`` for one case. Returns (notices created per module, modules that failed).

    Modules with their own runner (the tax module's ``ejecutar_en_bandeja``) are skipped here.
    """
    from hacienda_gpt.modulos.perfil import perfil_de

    perfil = perfil_de(case)
    creados: dict[str, int] = {}
    fallidos: list[str] = []
    for modulo in modulos:
        if callable(getattr(modulo, "ejecutar_en_bandeja", None)):
            continue
        try:
            avisos = list(modulo.avisos(ContextoModulo(perfil=perfil, hoy=hoy, case_id=case.case_id)))
        except Exception:
            logger.exception("Notices of module %s failed for case %s", modulo.clave, case.case_id)
            fallidos.append(modulo.clave)
            continue
        if len(avisos) > MAX_AVISOS_POR_MODULO:
            logger.error(
                "Module %s returned %d notices; only the first %d are kept",
                modulo.clave,
                len(avisos),
                MAX_AVISOS_POR_MODULO,
            )
            avisos = avisos[:MAX_AVISOS_POR_MODULO]
        nuevos = 0
        roto = False
        for aviso in avisos:
            try:
                notification = _notificacion(case.case_id, modulo, aviso)
            except (ValidationError, TypeError, ValueError, ArithmeticError) as exc:
                logger.error("Module %s returned an invalid notice for case %s: %s", modulo.clave, case.case_id, exc)
                roto = True
                continue
            if store.add(notification):
                nuevos += 1
                if aviso.sustituye_prefijo:
                    store.remove_superseded(
                        case.case_id,
                        key_prefix=clave_de(modulo.clave, aviso.sustituye_prefijo),
                        keep_key=notification.key,
                    )
        if roto:
            fallidos.append(modulo.clave)
        creados[modulo.clave] = nuevos
    return creados, fallidos


def avisos_de_modulos_para_todos(
    case_store: SQLiteCaseStateStore,
    store: NotificationStore,
    *,
    hoy: date,
    modulos_de: Callable[[CaseState], Sequence[Modulo]],
) -> dict[str, Any]:
    """The module notices over every open case: ``{"modulos": {clave: creados}, "errores_modulos": n}``."""
    por_modulo: dict[str, int] = {}
    errores = 0
    for case in case_store.list_all_cases():
        if case.status == CaseStatus.CLOSED:
            continue
        try:
            modulos = [m for m in modulos_de(case) if not callable(getattr(m, "ejecutar_en_bandeja", None))]
        except Exception:
            logger.exception("Could not tell which modules are on for case %s", case.case_id)
            errores += 1
            continue
        if not modulos:
            continue
        creados, fallidos = avisos_de_modulos(case, hoy=hoy, store=store, modulos=modulos)
        for clave, n in creados.items():
            por_modulo[clave] = por_modulo.get(clave, 0) + n
        errores += len(fallidos)
    return {"modulos": por_modulo, "errores_modulos": errores}


# --------------------------------------------------------------------------- #
# Weekly digest
# --------------------------------------------------------------------------- #


def _eur(amount: Decimal) -> str:
    text = f"{abs(amount):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{'-' if amount < 0 else ''}{text} €"


def _fecha(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def clave_semana(hoy: date) -> str:
    year, week, _ = hoy.isocalendar()
    return f"{KIND_RESUMEN}:{year}-W{week:02d}"


def abiertas(notifications: Sequence[Notification], *, hoy: date) -> list[Notification]:
    """What is still open: anything still due (read or not), and unread notices with no due date."""
    out = []
    for n in notifications:
        if n.kind == KIND_RESUMEN:
            continue
        if n.due_date is not None:
            if n.due_date >= hoy:
                out.append(n)
        elif n.read_at is None:
            out.append(n)
    return out


def _orden(n: Notification) -> tuple[Any, ...]:
    return (_RANGO.get(n.severity, 3), n.due_date or date.max, n.created_at, n.key)


def _linea(n: Notification, nombres: Mapping[str, str], *, hoy: date) -> str:
    clave = modulo_de(n.kind) or "fiscal"
    partes = [f"- [{nombres.get(clave, clave)}] {n.title}"]
    if n.due_date is not None:
        dias = (n.due_date - hoy).days
        cuando = "hoy" if dias == 0 else "mañana" if dias == 1 else f"en {dias} días"
        partes.append(f"vence el {_fecha(n.due_date)} ({cuando})")
    if n.amount is not None:
        partes.append(f"importe indicado: {_eur(n.amount)}")
    if n.severity == "urgente":
        partes.append("urgente")
    return " — ".join(partes)


# Two notices from DIFFERENT modules are shown together in the digest ("probably the same matter") when
# both have a due date at most this many days apart and their titles share a distinctive word (e.g. an
# insurance renewal entered in «caducidades» and the yearly charge detected in «suscripciones»).
DIAS_MISMO_ASUNTO = 3
_PALABRAS_GENERICAS = frozenset(
    {
        "renovacion",
        "renueva",
        "vence",
        "vencimiento",
        "caduca",
        "caducidad",
        "aviso",
        "plazo",
        "cargo",
        "cobro",
        "anual",
        "mensual",
        "proximo",
        "proxima",
        "fecha",
        "antes",
        "dias",
        "pago",
        "importe",
        "factura",
        "revisa",
        "revisar",
        "queda",
        "quedan",
        "desde",
        "hasta",
        "tienes",
        "puedes",
    }
)


def _palabras(titulo: str) -> set[str]:
    sin_acentos = "".join(c for c in unicodedata.normalize("NFKD", titulo.lower()) if not unicodedata.combining(c))
    palabras = {p.rstrip("s") for p in re.findall(r"[a-z]{5,}", sin_acentos)}
    return {p for p in palabras if p not in _PALABRAS_GENERICAS and p.rstrip("s") not in _PALABRAS_GENERICAS}


def mismo_asunto(a: Notification, b: Notification) -> bool:
    """Whether two notices of different modules likely announce the same thing (see :data:`DIAS_MISMO_ASUNTO`)."""
    if a.kind == b.kind or a.due_date is None or b.due_date is None:
        return False
    if abs((a.due_date - b.due_date).days) > DIAS_MISMO_ASUNTO:
        return False
    return bool(_palabras(a.title) & _palabras(b.title))


def agrupar(notificaciones: Sequence[Notification]) -> list[list[Notification]]:
    """Notices in order, each with the later ones that announce the same matter folded under it."""
    grupos: list[list[Notification]] = []
    for n in notificaciones:
        destino = next((g for g in grupos if any(mismo_asunto(n, otro) for otro in g)), None)
        if destino is None:
            grupos.append([n])
        else:
            destino.append(n)
    return grupos


def resumen_semanal(
    case: CaseState,
    *,
    hoy: date,
    store: NotificationStore,
    acciones: Any | None = None,
    nombres: Mapping[str, str] | None = None,
) -> int:
    """Write this week's digest for one case (0 or 1). Nothing is written when nothing is open."""
    key = clave_semana(hoy)
    actuales = store.list(case.case_id)
    if any(n.key == key for n in actuales):
        return 0
    nombres = {"fiscal": "Impuestos", **(nombres or {})}
    abiertos = sorted(abiertas(actuales, hoy=hoy), key=_orden)
    pendientes = acciones.list(case.case_id, estado="pendiente") if acciones is not None else []
    if not abiertos and not pendientes:
        return 0

    con_fecha = [n for n in abiertos if n.due_date is not None]
    importes = [n for n in abiertos if n.due_date is None and n.amount is not None]
    otros = [n for n in abiertos if n.due_date is None and n.amount is None]
    lunes = hoy - timedelta(days=hoy.weekday())
    secciones = [f"Lo que tienes abierto a {_fecha(hoy)} (semana del {_fecha(lunes)}), de lo más urgente a lo menos."]
    if pendientes:
        secciones.append(
            "**Pendiente de tu decisión** — no se hará nada hasta que lo confirmes:\n"
            + "\n".join(f"- [{nombres.get(a.modulo, a.modulo)}] {a.resumen}" for a in pendientes)
        )
    if con_fecha:
        lineas: list[str] = []
        for grupo in agrupar(con_fecha):
            lineas.append(_linea(grupo[0], nombres, hoy=hoy))
            for otro in grupo[1:]:
                lineas.append(
                    "  (probablemente lo mismo, avisado también por otro módulo) " + _linea(otro, nombres, hoy=hoy)[2:]
                )
        secciones.append("**Lo que vence**\n" + "\n".join(lineas))
    if importes:
        secciones.append(
            "**Importes previstos** (cargos y otras cifras que señalan los avisos)\n"
            + "\n".join(_linea(n, nombres, hoy=hoy) for n in importes)
        )
    if otros:
        secciones.append("**Otros avisos sin leer**\n" + "\n".join(_linea(n, nombres, hoy=hoy) for n in otros))
    if any(n.amount is not None for n in abiertos):
        secciones.append(
            "Cada importe es el que calcula su módulo para ese aviso: no se suman entre sí, porque no son de la "
            "misma naturaleza (un cargo, un ahorro estimado, una ayuda)."
        )

    titulo = f"Resumen semanal: {len(abiertos)} aviso{'s' if len(abiertos) != 1 else ''} abierto"
    titulo += "s" if len(abiertos) != 1 else ""
    if pendientes:
        titulo += f" y {len(pendientes)} acci{'ones' if len(pendientes) != 1 else 'ón'} por decidir"
    acciones_sugeridas = ["Abre cada aviso para ver el detalle, las fuentes y los pasos que propone."]
    if pendientes:
        acciones_sugeridas.insert(0, "Confirma o rechaza las acciones pendientes en «Acciones pendientes».")
    vence = min((n.due_date for n in con_fecha if n.due_date is not None), default=None)
    return int(
        store.add(
            Notification(
                case_id=case.case_id,
                kind=KIND_RESUMEN,
                key=key,
                title=titulo,
                body="\n\n".join(secciones),
                severity="aviso" if any(n.severity == "urgente" for n in abiertos) or pendientes else "info",
                actions=acciones_sugeridas,
                sources=[],
                due_date=vence,
            )
        )
    )


def resumen_semanal_para_todos(
    case_store: SQLiteCaseStateStore,
    store: NotificationStore,
    *,
    hoy: date,
    acciones: Any | None = None,
    nombres: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The weekly digest over every open case. One failing case never stops the batch."""
    summary: dict[str, Any] = {"hoy": hoy.isoformat(), "semana": clave_semana(hoy), "cases": 0, "resumen_semanal": 0}
    summary["errors"] = 0
    for case in case_store.list_all_cases():
        if case.status == CaseStatus.CLOSED:
            continue
        summary["cases"] += 1
        try:
            summary["resumen_semanal"] += resumen_semanal(
                case, hoy=hoy, store=store, acciones=acciones, nombres=nombres
            )
        except Exception:
            logger.exception("Weekly digest failed for case %s", case.case_id)
            summary["errors"] += 1
    return summary


__all__ = [
    "MAX_AVISOS_POR_MODULO",
    "abiertas",
    "avisos_de_modulos",
    "avisos_de_modulos_para_todos",
    "clave_de",
    "clave_semana",
    "kind_de",
    "resumen_semanal",
    "resumen_semanal_para_todos",
]
