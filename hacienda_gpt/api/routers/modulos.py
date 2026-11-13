"""Household modules: what each one is, who has it on, and the actions waiting for confirmation.

* ``GET /modulos`` — every module the operator offers, with its facts (for the profile form), its tools
  and its sources, and whether it is on for the caller.
* ``PUT /me/modulos/{clave}`` — the user switches a module on or off (accounts only). Switching off stops
  its specialist and its notices at once, removes its notices and rejects its pending actions; with
  ``borrar_datos`` its facts also leave every case of the user (and the audit trail).
* ``GET /acciones?case_id=…`` — the actions the specialists prepared for a case.
* ``POST /acciones/{id}/confirmar`` / ``/rechazar`` — only the owner of the case decides; an action runs at
  most once, and only while its module is still on.

Built by :func:`build_router` with the stores passed in (see ``api.py``), like the auth router.
The module deliberately has no ``from __future__ import annotations`` (FastAPI resolves the local aliases).
"""

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.api.security import require_api_key
from hacienda_gpt.auth.deps import CaseAccess, get_case_access, require_session
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.modulos.acciones import AccionPendiente, AccionStore
from hacienda_gpt.modulos.contrato import ContextoModulo, ResultadoHerramienta
from hacienda_gpt.modulos.perfil import HechoInvalidoError, perfil_de, registrar_hechos_de_modulo
from hacienda_gpt.modulos.preferencias import EstadoModulo, estado_para_usuario, modulos_activos_para_caso
from hacienda_gpt.modulos.registro import FISCAL, registro
from hacienda_gpt.proactive.bandeja import kind_de
from hacienda_gpt.proactive.jobs import hoy_en_espana
from hacienda_gpt.proactive.store import NotificationStore

logger = logging.getLogger(__name__)


class HechoInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str
    tipo: str
    etiqueta: str
    descripcion: str
    sensible: bool


class HerramientaInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str
    efecto: str
    descripcion: str


class FuenteInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identificador: str
    descripcion: str
    url: str


class ModuloInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clave: str
    nombre: str
    descripcion: str
    activo: bool
    # The tax module cannot be switched off.
    obligatorio: bool
    # True when the user chose (False: the default applies).
    elegido: bool
    elegido_el: datetime | None = None
    hechos: list[HechoInfo] = Field(default_factory=list)
    herramientas: list[HerramientaInfo] = Field(default_factory=list)
    fuentes: list[FuenteInfo] = Field(default_factory=list)


class ModulosResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # False without accounts: the operator's defaults apply and nobody can switch modules here.
    configurable: bool
    modulos: list[ModuloInfo]


class ModuloUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activo: bool
    # Only when switching off: also erase the module's facts from every case of the user.
    borrar_datos: bool = False


class AccionesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    acciones: list[AccionPendiente]


def _info(estado: EstadoModulo) -> ModuloInfo:
    modulo = estado.modulo
    return ModuloInfo(
        clave=modulo.clave,
        nombre=modulo.nombre,
        descripcion=modulo.descripcion,
        activo=estado.activo,
        obligatorio=estado.obligatorio,
        elegido=estado.elegido,
        elegido_el=estado.cuando,
        hechos=[
            HechoInfo(nombre=h.nombre, tipo=h.tipo, etiqueta=h.etiqueta, descripcion=h.descripcion, sensible=h.sensible)
            for h in modulo.hechos
        ],
        herramientas=[
            HerramientaInfo(nombre=h.spec.name, efecto=h.efecto, descripcion=h.spec.description)
            for h in modulo.herramientas
        ],
        fuentes=[
            FuenteInfo(identificador=f.identificador, descripcion=f.descripcion, url=f.url) for f in modulo.fuentes
        ],
    )


def build_router(
    *,
    get_case_store: Callable[[], SQLiteCaseStateStore],
    get_notification_store: Callable[[], NotificationStore],
    get_accion_store: Callable[..., AccionStore],
) -> APIRouter:
    router = APIRouter()
    case_store_dep = Annotated[SQLiteCaseStateStore, Depends(get_case_store)]
    notification_store_dep = Annotated[NotificationStore, Depends(get_notification_store)]
    accion_store_dep = Annotated[AccionStore, Depends(get_accion_store)]
    access_dep = Annotated[CaseAccess, Depends(get_case_access)]

    def _respuesta(access: CaseAccess) -> ModulosResponse:
        user_id = access.user.id if access.user is not None else None
        estados = estado_para_usuario(user_id, access.store)
        return ModulosResponse(configurable=access.user is not None, modulos=[_info(e) for e in estados])

    @router.get(
        "/modulos", response_model=ModulosResponse, dependencies=[Depends(require_api_key), Depends(require_session)]
    )
    def get_modulos(access: access_dep) -> ModulosResponse:
        return _respuesta(access)

    @router.put("/me/modulos/{clave}", response_model=ModulosResponse, dependencies=[Depends(require_api_key)])
    def put_modulo(
        clave: str,
        payload: ModuloUpdate,
        cases: case_store_dep,
        notifications: notification_store_dep,
        acciones: accion_store_dep,
        access: access_dep,
    ) -> ModulosResponse:
        if access.user is None or access.store is None:
            raise HTTPException(
                status_code=404,
                detail="Sin cuentas de usuario, los módulos los activa el operador del servicio.",
            )
        user, auth = access.user, access.store
        estados = {e.modulo.clave: e for e in estado_para_usuario(user.id, auth)}
        estado = estados.get(clave)
        if estado is None:
            raise HTTPException(status_code=404, detail="Ese módulo no existe o no está disponible.")
        if clave == FISCAL:
            raise HTTPException(
                status_code=409, detail="El módulo de impuestos es la base del asesor y no se desactiva."
            )
        if payload.borrar_datos and payload.activo:
            raise HTTPException(status_code=422, detail="Solo se pueden borrar los datos de un módulo al desactivarlo.")
        if estado.activo != payload.activo or not estado.elegido:
            auth.record_module_choice(user.id, clave, payload.activo)
        if not payload.activo:
            nombres = {h.nombre for h in estado.modulo.hechos} if payload.borrar_datos else set()
            for case_id in auth.case_ids_of(user.id):
                # Derived data goes at once: its notices (rebuilt if switched on again) and its pending actions.
                notifications.remove_kind(case_id, kind_de(clave))
                acciones.rechazar_pendientes_de(case_id, clave)
                if nombres:
                    _borrar_hechos(cases, case_id, clave, nombres)
        return _respuesta(access)

    @router.get("/acciones", response_model=AccionesResponse, dependencies=[Depends(require_api_key)])
    def get_acciones(
        case_id: str, cases: case_store_dep, acciones: accion_store_dep, access: access_dep
    ) -> AccionesResponse:
        """The actions of one case (``?case_id=``), newest first. Another user's case is a 404."""
        access.check(case_id)
        if cases.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return AccionesResponse(case_id=case_id, acciones=acciones.list(case_id))

    def _accion_del_usuario(accion_id: str, acciones: AccionStore, access: CaseAccess) -> AccionPendiente:
        accion = acciones.get(accion_id)
        if accion is None:
            raise HTTPException(status_code=404, detail="action not found")
        # Somebody else's action reads exactly like one that does not exist.
        try:
            access.check(accion.case_id)
        except HTTPException as exc:
            raise HTTPException(status_code=404, detail="action not found") from exc
        return accion

    @router.post(
        "/acciones/{accion_id}/confirmar", response_model=AccionPendiente, dependencies=[Depends(require_api_key)]
    )
    def post_confirmar(
        accion_id: str, cases: case_store_dep, acciones: accion_store_dep, access: access_dep
    ) -> AccionPendiente:
        """The owner of the case confirms: the module's tool runs now, once."""
        accion = _accion_del_usuario(accion_id, acciones, access)
        if accion.estado != "pendiente":
            raise HTTPException(status_code=409, detail="Esta acción ya se decidió; no se puede ejecutar de nuevo.")
        case = cases.get_case(accion.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        activos = {m.clave: m for m in modulos_activos_para_caso(accion.case_id, access.store)}
        modulo = activos.get(accion.modulo)
        encontrada = registro().herramienta(accion.herramienta)
        if modulo is None or encontrada is None or encontrada[0].clave != modulo.clave:
            raise HTTPException(
                status_code=409,
                detail="El módulo de esta acción está desactivado o ya no la ofrece. No se ha hecho nada.",
            )
        herramienta = encontrada[1]
        if herramienta.efecto != "actua":
            raise HTTPException(status_code=409, detail="Esta herramienta ya no es una acción. No se ha hecho nada.")
        if acciones.transicion(accion_id, desde="pendiente", hacia="ejecutando") is None:
            raise HTTPException(status_code=409, detail="Esta acción ya se decidió; no se puede ejecutar de nuevo.")

        escrito: dict[str, Any] = {"case": case, "cambiado": False}

        def registrar(valores: Any) -> None:
            escrito["case"] = registrar_hechos_de_modulo(
                escrito["case"], modulo, valores, fuente=f"modulo:{modulo.clave}"
            )
            escrito["cambiado"] = True

        contexto = ContextoModulo(
            perfil=perfil_de(case), hoy=hoy_en_espana(), case_id=case.case_id, registrar=registrar
        )
        try:
            resultado = herramienta.ejecutar(dict(accion.argumentos), contexto)
            if not isinstance(resultado, ResultadoHerramienta):
                raise TypeError("la herramienta no devolvió un ResultadoHerramienta")
            texto, es_error = resultado.contenido, resultado.es_error
        except HechoInvalidoError as exc:
            texto, es_error = f"No se pudo completar: {exc}", True
        except Exception as exc:  # the action failed: recorded, never retried silently
            logger.exception("action %s failed", accion_id)
            texto, es_error = f"La acción falló: {type(exc).__name__}: {exc}", True
        if escrito["cambiado"] and not es_error:
            cases.save_case(escrito["case"])
        final = acciones.transicion(
            accion_id,
            desde="ejecutando",
            hacia="fallida" if es_error else "ejecutada",
            resultado=texto,
            es_error=es_error,
        )
        cases.append_audit_event(
            accion.case_id,
            {
                "event_type": "accion_confirmada",
                "accion_id": accion_id,
                "modulo": accion.modulo,
                "herramienta": accion.herramienta,
                "argumentos": accion.argumentos,
                "estado": "fallida" if es_error else "ejecutada",
            },
        )
        assert final is not None
        return final

    @router.post(
        "/acciones/{accion_id}/rechazar", response_model=AccionPendiente, dependencies=[Depends(require_api_key)]
    )
    def post_rechazar(
        accion_id: str, cases: case_store_dep, acciones: accion_store_dep, access: access_dep
    ) -> AccionPendiente:
        accion = _accion_del_usuario(accion_id, acciones, access)
        rechazada = acciones.transicion(accion_id, desde="pendiente", hacia="rechazada")
        if rechazada is None:
            raise HTTPException(status_code=409, detail="Esta acción ya se decidió.")
        cases.append_audit_event(
            accion.case_id,
            {
                "event_type": "accion_rechazada",
                "accion_id": accion_id,
                "modulo": accion.modulo,
                "herramienta": accion.herramienta,
            },
        )
        return rechazada

    return router


def _borrar_hechos(cases: SQLiteCaseStateStore, case_id: str, clave: str, nombres: set[str]) -> None:
    """Erase a module's facts from one case, and their values from its audit trail."""
    case = cases.get_case(case_id)
    if case is None:
        return
    borrados = sorted(f.name for f in case.facts if f.name in nombres)
    if not borrados:
        return
    cases.save_case(
        case.model_copy(
            update={"facts": [f for f in case.facts if f.name not in nombres], "updated_at": datetime.now(UTC)}
        )
    )
    redactados = sum(cases.redact_fact_in_audit(case_id, nombre) for nombre in borrados)
    cases.append_audit_event(
        case_id,
        {
            "event_type": "module_data_erased",
            "modulo": clave,
            "fact_names": borrados,
            "audit_events_redacted": redactados,
        },
    )
