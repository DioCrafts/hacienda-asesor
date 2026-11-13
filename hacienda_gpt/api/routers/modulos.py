"""Household modules: what each one is, who has it on, and the actions waiting for confirmation.

* ``GET /modulos`` — every module the operator offers, with its facts (for the profile form), its tools
  and its sources, and whether it is on for the caller.
* ``PUT /me/modulos/{clave}`` — the user switches a module on or off (accounts only). Switching off stops
  its specialist and its notices at once, removes its notices and rejects its pending actions; with
  ``borrar_datos`` its facts also leave every case of the user (and the audit trail).
* ``GET /acciones?case_id=…`` — the actions the specialists prepared for a case.
* ``GET /propuestas?case_id=…`` — tax facts a module can fill in (``modulos/puentes.py``), for the user to accept.
* ``POST /acciones/{id}/confirmar`` / ``/rechazar`` — only the owner of the case decides; an action runs at
  most once, and only while its module is still on.
* ``POST /modulos/{clave}/herramientas/{nombre}`` — the user runs one of an active module's tools from the UI
  (e.g. saving a purchase); a tool that acts outside the app still only becomes a pending action.
* ``POST /modulos/{clave}/documento`` — a module that reads documents (optional ``leer_documento`` hook, see
  below) PROPOSES data from an upload; nothing is stored. Same consent as the tax document reader.

Optional hooks a module object may expose beyond the contract (proposed for the next contract version):
``leer_documento(provider, *, filename, data)`` returning ``{"campos": [{"campo", "valor", "evidencia"…}],
"avisos": […], "descartados": […]}`` (a pydantic model is accepted), and ``herramienta_de_documento``, the
tool the confirmed fields are passed to as ``{campo: valor}``.

Built by :func:`build_router` with the stores passed in (see ``api.py``), like the auth router.
The module deliberately has no ``from __future__ import annotations`` (FastAPI resolves the local aliases).
"""

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import LLMProvider, ProviderError
from hacienda_gpt.api.security import require_api_key
from hacienda_gpt.auth.deps import CONSENT_DOCUMENTS_DETAIL, CaseAccess, get_case_access, require_session
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.modulos.acciones import AccionPendiente, AccionStore, describir
from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo, ResultadoHerramienta
from hacienda_gpt.modulos.perfil import HechoInvalidoError, perfil_de, registrar_hechos_de_modulo
from hacienda_gpt.modulos.preferencias import EstadoModulo, estado_para_usuario, modulos_activos_para_caso
from hacienda_gpt.modulos.registro import FISCAL, registro
from hacienda_gpt.proactive.bandeja import kind_de
from hacienda_gpt.proactive.jobs import hoy_en_espana
from hacienda_gpt.proactive.store import NotificationStore
from hacienda_gpt.tax.documentos import DocumentoNoLegibleError, decode_upload

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
    # The tool takes a whole file (``contenido`` text or ``contenido_base64``): the UI offers an upload that
    # goes straight to the tool, never through the model.
    acepta_fichero: bool = False


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
    # The module reads uploaded documents (optional ``leer_documento`` hook) and where the fields go.
    lee_documentos: bool = False
    herramienta_de_documento: str | None = None


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


class EjecucionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    argumentos: dict[str, Any] = Field(default_factory=dict)


class ResultadoInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contenido: str
    resumen: str
    es_error: bool
    citas: list[str] = Field(default_factory=list)


class EjecucionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None when the tool acts outside the app: it became a pending action instead.
    resultado: ResultadoInfo | None = None
    accion: AccionPendiente | None = None
    # True when the tool wrote facts to the case.
    case_updated: bool = False


class DocumentoModuloRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1)


class DocumentoModuloResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modulo: str
    herramienta_destino: str | None
    propuesta: dict[str, Any]


class PropuestaInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str
    valor: float
    titulo: str
    explicacion: str
    modulo: str
    desglose: dict[str, str] = Field(default_factory=dict)


class PropuestasResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    propuestas: list[PropuestaInfo]


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
            HerramientaInfo(
                nombre=h.spec.name,
                efecto=h.efecto,
                descripcion=h.spec.description,
                acepta_fichero=_acepta_fichero(h.spec.input_schema),
            )
            for h in modulo.herramientas
        ],
        fuentes=[
            FuenteInfo(identificador=f.identificador, descripcion=f.descripcion, url=f.url) for f in modulo.fuentes
        ],
        lee_documentos=callable(getattr(modulo, "leer_documento", None)),
        herramienta_de_documento=_herramienta_de_documento(modulo),
    )


def _acepta_fichero(schema: Any) -> bool:
    propiedades = schema.get("properties") if isinstance(schema, dict) else None
    return isinstance(propiedades, dict) and bool({"contenido", "contenido_base64"} & set(propiedades))


def _herramienta_de_documento(modulo: Modulo) -> str | None:
    nombre = getattr(modulo, "herramienta_de_documento", None)
    if not isinstance(nombre, str) or nombre not in {h.spec.name for h in modulo.herramientas}:
        return None
    return nombre


def build_router(
    *,
    get_case_store: Callable[[], SQLiteCaseStateStore],
    get_notification_store: Callable[[], NotificationStore],
    get_accion_store: Callable[..., AccionStore],
    get_document_provider: Callable[..., LLMProvider] | None = None,
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

    @router.get("/propuestas", response_model=PropuestasResponse, dependencies=[Depends(require_api_key)])
    def get_propuestas(case_id: str, cases: case_store_dep, access: access_dep) -> PropuestasResponse:
        """Tax facts the active modules can fill in (``?case_id=``). Nothing is written: the user accepts one
        through ``PUT /cases/{id}/facts/{nombre}``."""
        from dataclasses import asdict

        from hacienda_gpt.modulos.puentes import propuestas_para

        access.check(case_id)
        case = cases.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        propuestas = propuestas_para(case, modulos_activos_para_caso(case_id, access.store), hoy=hoy_en_espana())
        return PropuestasResponse(case_id=case_id, propuestas=[PropuestaInfo(**asdict(p)) for p in propuestas])

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

    def _modulo_activo(clave: str, case_id: str, access: CaseAccess, cases: SQLiteCaseStateStore) -> Modulo:
        access.check(case_id)
        if cases.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        modulo = {m.clave: m for m in modulos_activos_para_caso(case_id, access.store)}.get(clave)
        if modulo is None:
            raise HTTPException(status_code=409, detail="Ese módulo no está activado para este expediente.")
        return modulo

    @router.post(
        "/modulos/{clave}/herramientas/{nombre}",
        response_model=EjecucionResponse,
        dependencies=[Depends(require_api_key)],
    )
    def post_herramienta(
        clave: str,
        nombre: str,
        payload: EjecucionRequest,
        cases: case_store_dep,
        acciones: accion_store_dep,
        access: access_dep,
    ) -> EjecucionResponse:
        """The user runs a module tool from the UI. Its facts are written (validated); acting tools only queue.

        This is also the upload path for files a module analyses (a bank statement as ``contenido`` or
        ``contenido_base64``): the file goes straight to the tool, never to the model, and only the names of
        the arguments are audited. What the tool derives is stored as its facts through ``registrar``.
        """
        modulo = _modulo_activo(clave, payload.case_id, access, cases)
        herramienta = next((h for h in modulo.herramientas if h.spec.name == nombre), None)
        if herramienta is None or clave == FISCAL:
            raise HTTPException(status_code=404, detail="Ese módulo no tiene esa herramienta.")
        if herramienta.efecto == "actua":
            accion = acciones.add(
                AccionPendiente(
                    case_id=payload.case_id,
                    modulo=clave,
                    herramienta=nombre,
                    argumentos=payload.argumentos,
                    resumen=describir(modulo.nombre, herramienta.spec.description, payload.argumentos),
                )
            )
            return EjecucionResponse(accion=accion)
        case = cases.get_case(payload.case_id)
        assert case is not None
        escrito: dict[str, Any] = {"case": case, "cambiado": False}

        def registrar(valores: Any) -> None:
            escrito["case"] = registrar_hechos_de_modulo(escrito["case"], modulo, valores, fuente="user_edit")
            escrito["cambiado"] = True

        contexto = ContextoModulo(
            perfil=perfil_de(case), hoy=hoy_en_espana(), case_id=case.case_id, registrar=registrar
        )
        try:
            resultado = herramienta.ejecutar(dict(payload.argumentos), contexto)
            if not isinstance(resultado, ResultadoHerramienta):
                raise TypeError("la herramienta no devolvió un ResultadoHerramienta")
        except HechoInvalidoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("module tool %s failed", nombre)
            raise HTTPException(status_code=500, detail=f"La herramienta falló: {type(exc).__name__}.") from exc
        guardado = bool(escrito["cambiado"]) and not resultado.es_error
        if guardado:
            cases.save_case(escrito["case"])
        # Only the argument NAMES: an uploaded file (a bank statement) must not be copied into the audit trail.
        cases.append_audit_event(
            payload.case_id,
            {
                "event_type": "module_tool_run_by_user",
                "modulo": clave,
                "herramienta": nombre,
                "argumentos_recibidos": sorted(payload.argumentos),
                "es_error": resultado.es_error,
            },
        )
        return EjecucionResponse(
            resultado=ResultadoInfo(
                contenido=resultado.contenido,
                resumen=resultado.resumen,
                es_error=resultado.es_error,
                citas=list(resultado.citas),
            ),
            case_updated=guardado,
        )

    def _sin_lector() -> LLMProvider:
        raise HTTPException(status_code=503, detail="El lector de documentos no está disponible.")

    @router.post(
        "/modulos/{clave}/documento", response_model=DocumentoModuloResponse, dependencies=[Depends(require_api_key)]
    )
    def post_documento(
        clave: str,
        payload: DocumentoModuloRequest,
        cases: case_store_dep,
        access: access_dep,
        provider: Annotated[LLMProvider, Depends(get_document_provider or _sin_lector)],
    ) -> DocumentoModuloResponse:
        """A module reads an uploaded document and PROPOSES data; nothing is stored, the file is not kept."""
        modulo = _modulo_activo(clave, payload.case_id, access, cases)
        if access.user is not None and access.store is not None:
            if not access.store.has_consent(access.user.id, "envio_documentos_a_proveedor_llm"):
                raise HTTPException(status_code=403, detail=CONSENT_DOCUMENTS_DETAIL)
        lector = getattr(modulo, "leer_documento", None)
        if not callable(lector):
            raise HTTPException(status_code=404, detail="Este módulo no lee documentos.")
        from hacienda_gpt.ops.consumo import PresupuestoAgotadoError, contexto_consumo

        try:
            data = decode_upload(payload.content_base64)
            with contexto_consumo(user_id=access.user.id if access.user else None, case_id=payload.case_id):
                propuesta = lector(provider, filename=payload.filename, data=data)
        except DocumentoNoLegibleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PresupuestoAgotadoError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail=f"No se pudo leer el documento: {exc}") from exc
        if isinstance(propuesta, BaseModel):
            datos: dict[str, Any] = propuesta.model_dump(mode="json")
        elif isinstance(propuesta, dict):
            datos = propuesta
        else:
            raise HTTPException(status_code=500, detail="El módulo devolvió una propuesta no válida.")
        cases.append_audit_event(
            payload.case_id,
            {
                "event_type": "module_document_read",
                "modulo": clave,
                "filename": payload.filename,
                "proposed_fields": [c.get("campo") for c in datos.get("campos", []) if isinstance(c, dict)],
            },
        )
        return DocumentoModuloResponse(
            modulo=clave, herramienta_destino=_herramienta_de_documento(modulo), propuesta=datos
        )

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
