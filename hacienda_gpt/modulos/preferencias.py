"""Which modules work for whom: the operator's switch, the defaults and each user's choice.

* ``MODULOS_DESACTIVADOS`` (operator) removes a module for everyone.
* ``fiscal`` is always on: it is the service the account was opened for, and the chat is its advisor.
* Every other module is OFF until the user switches it on (privacy by default, art. 25.2 GDPR: a module
  processes household data — bills, subscriptions, a mortgage — for a purpose the user has not chosen yet).
  ``MODULOS_ACTIVOS_POR_DEFECTO`` changes that default; with ``AUTH_MODE=off`` (a personal deployment, no
  accounts) it is the only switch there is.
* With accounts, the user's latest choice per module is stored in the auth store (append-only history, so
  the record of what was chosen and when is kept, and erased with the account).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hacienda_gpt import settings
from hacienda_gpt.modulos.contrato import Modulo
from hacienda_gpt.modulos.registro import FISCAL, Registro, registro

SIEMPRE_ACTIVOS = frozenset({FISCAL})


@dataclass(frozen=True)
class EstadoModulo:
    modulo: Modulo
    activo: bool
    # True when the user decided (False: the default applies).
    elegido: bool = False
    cuando: datetime | None = None

    @property
    def obligatorio(self) -> bool:
        return self.modulo.clave in SIEMPRE_ACTIVOS


def activos_por_defecto(reg: Registro | None = None) -> frozenset[str]:
    reg = reg or registro()
    valor = settings.MODULOS_ACTIVOS_POR_DEFECTO.strip().lower()
    if valor in {"todos", "all", "*"}:
        return frozenset(reg.modulos)
    return frozenset(part.strip() for part in valor.split(",") if part.strip()) | SIEMPRE_ACTIVOS


def _cuentas_activas() -> bool:
    return settings.AUTH_MODE == "required"


def estado_para_usuario(user_id: str | None, auth: Any | None, reg: Registro | None = None) -> list[EstadoModulo]:
    """Every module the operator offers, and whether it is on for ``user_id`` (None: no accounts)."""
    reg = reg or registro()
    por_defecto = activos_por_defecto(reg)
    elecciones = auth.module_choices(user_id) if (user_id is not None and auth is not None) else {}
    estados: list[EstadoModulo] = []
    for modulo in reg.activos():
        clave = modulo.clave
        eleccion = elecciones.get(clave)
        if clave in SIEMPRE_ACTIVOS:
            estados.append(EstadoModulo(modulo=modulo, activo=True))
        elif eleccion is not None:
            estados.append(EstadoModulo(modulo=modulo, activo=eleccion.activo, elegido=True, cuando=eleccion.timestamp))
        else:
            estados.append(EstadoModulo(modulo=modulo, activo=clave in por_defecto))
    return estados


def modulos_activos_para(user_id: str | None, auth: Any | None, reg: Registro | None = None) -> list[Modulo]:
    return [estado.modulo for estado in estado_para_usuario(user_id, auth, reg) if estado.activo]


def modulos_activos_para_caso(case_id: str, auth: Any | None = None, reg: Registro | None = None) -> list[Modulo]:
    """Modules active for the owner of a case (the defaults for a case without an owner or without accounts).

    With ``AUTH_MODE=off`` the auth store is never opened.
    """
    if not _cuentas_activas():
        return modulos_activos_para(None, None, reg)
    if auth is None:
        from hacienda_gpt.auth.deps import get_auth_store

        auth = get_auth_store()
    owner = auth.owner_of(case_id)
    return modulos_activos_para(owner, auth if owner is not None else None, reg)


__all__ = [
    "SIEMPRE_ACTIVOS",
    "EstadoModulo",
    "activos_por_defecto",
    "estado_para_usuario",
    "modulos_activos_para",
    "modulos_activos_para_caso",
]
