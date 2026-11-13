"""The household's dated obligations, built from the module's facts, and the proactive notices on them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from hacienda_gpt.modulos.caducidades import calculo
from hacienda_gpt.modulos.caducidades.calculo import CalculoError, Vencimiento, fmt
from hacienda_gpt.modulos.caducidades.hechos import (
    HECHO_ANTELACIONES,
    HECHO_COMPRAS,
    HECHO_CONTRATOS,
    HECHO_DOCUMENTOS,
    HECHO_FECHAS_NACIMIENTO,
    HECHO_VEHICULOS,
    leer_hecho,
    titular_texto,
    umbrales,
    validar_antelaciones,
    validar_compras,
    validar_contratos,
    validar_documentos,
    validar_fechas_nacimiento,
    validar_vehiculos,
)
from hacienda_gpt.modulos.caducidades.reglas import Reglas, cargar_reglas
from hacienda_gpt.modulos.contrato import AvisoModulo, Gravedad

NOMBRE_DOCUMENTO = {
    "dni": "DNI",
    "pasaporte": "Pasaporte",
    "permiso_conduccion": "Permiso de conducción",
    "licencia_conduccion": "Licencia de conducción",
    "certificado_electronico": "Certificado electrónico",
}


@dataclass
class Agenda:
    vencimientos: list[Vencimiento] = field(default_factory=list)
    # Items that could not be dated, with the reason (Spanish).
    pendientes: list[str] = field(default_factory=list)
    antelaciones: dict[str, int] = field(default_factory=dict)


def _d(valor: Any) -> date | None:
    return date.fromisoformat(valor) if isinstance(valor, str) and valor else None


def fecha_nacimiento(perfil: Mapping[str, Any], titular: str, fechas: Mapping[str, str]) -> date | None:
    """The holder's birth date: the module's own fact, or for ``hijo_N`` the shared ``descendientes`` detail."""
    if titular in fechas:
        return date.fromisoformat(fechas[titular])
    if titular.startswith("hijo_"):
        hijos = perfil.get("descendientes")
        if isinstance(hijos, list):
            indice = int(titular.removeprefix("hijo_")) - 1
            if 0 <= indice < len(hijos) and isinstance(hijos[indice], dict):
                return _d(hijos[indice].get("fecha_nacimiento"))
    return None


def _documento(reglas: Reglas, doc: Mapping[str, Any], nacimiento: date | None) -> Vencimiento:
    tipo = doc["tipo"]
    titulo = f"{doc.get('nombre') or NOMBRE_DOCUMENTO.get(tipo, tipo)} ({titular_texto(doc['titular'])})"
    expedicion, caducidad = _d(doc.get("fecha_expedicion")), _d(doc.get("fecha_caducidad"))
    if tipo == "dni":
        return calculo.calcular_dni(
            reglas,
            id=doc["id"],
            titulo=titulo,
            fecha_expedicion=expedicion,
            fecha_nacimiento=nacimiento,
            fecha_caducidad=caducidad,
        )
    if tipo == "pasaporte":
        return calculo.calcular_pasaporte(
            reglas,
            id=doc["id"],
            titulo=titulo,
            fecha_expedicion=expedicion,
            fecha_nacimiento=nacimiento,
            fecha_caducidad=caducidad,
            tiene_dni=doc.get("tiene_dni"),
        )
    if tipo in ("permiso_conduccion", "licencia_conduccion"):
        return calculo.calcular_permiso(
            reglas,
            id=doc["id"],
            titulo=titulo,
            clases=doc.get("clases") or [],
            fecha_inicio_vigencia=expedicion,
            fecha_nacimiento=nacimiento,
            fecha_caducidad=caducidad,
            es_licencia=tipo == "licencia_conduccion",
        )
    if tipo == "certificado_electronico":
        return calculo.calcular_certificado(
            reglas, id=doc["id"], titulo=titulo, fecha_caducidad=caducidad, fecha_expedicion=expedicion
        )
    assert caducidad is not None  # validated: "otro" requires it
    return calculo.calcular_otro_documento(id=doc["id"], titulo=titulo, fecha_caducidad=caducidad)


def construir_agenda(perfil: Mapping[str, Any], hoy: date, reglas: Reglas | None = None) -> Agenda:
    reglas = reglas or cargar_reglas()
    agenda = Agenda()
    fechas, error = leer_hecho(perfil, HECHO_FECHAS_NACIMIENTO, validar_fechas_nacimiento, {})
    if error:
        agenda.pendientes.append(error)
    agenda.antelaciones, error = leer_hecho(perfil, HECHO_ANTELACIONES, validar_antelaciones, {})
    if error:
        agenda.pendientes.append(error)

    documentos, error = leer_hecho(perfil, HECHO_DOCUMENTOS, validar_documentos, [])
    if error:
        agenda.pendientes.append(error)
    for doc in documentos:
        try:
            agenda.vencimientos.append(_documento(reglas, doc, fecha_nacimiento(perfil, doc["titular"], fechas)))
        except CalculoError as exc:
            nombre = doc.get("nombre") or NOMBRE_DOCUMENTO.get(doc["tipo"], doc["tipo"])
            agenda.pendientes.append(f"{nombre} ({titular_texto(doc['titular'])}): {exc}")

    vehiculos, error = leer_hecho(perfil, HECHO_VEHICULOS, validar_vehiculos, [])
    if error:
        agenda.pendientes.append(error)
    for veh in vehiculos:
        titulo = f"ITV de «{veh['etiqueta']}»"
        if veh.get("historico") and not veh.get("fecha_proxima_itv"):
            agenda.pendientes.append(
                f"{titulo}: los vehículos históricos tienen su propia frecuencia (art. 6.2 RD 920/2017), que no se "
                "calcula: introduce la fecha de la próxima ITV que figura en su tarjeta."
            )
            continue
        try:
            agenda.vencimientos.append(
                calculo.calcular_itv(
                    reglas,
                    hoy=hoy,
                    id=f"{veh['id']}:itv",
                    titulo=titulo,
                    categoria=veh["categoria"],
                    fecha_primera_matriculacion=date.fromisoformat(veh["fecha_primera_matriculacion"]),
                    fecha_ultima_itv=_d(veh.get("fecha_ultima_itv")),
                    fecha_caducidad_anterior=_d(veh.get("fecha_caducidad_itv_anterior")),
                    fecha_proxima_itv=_d(veh.get("fecha_proxima_itv")),
                )
            )
        except CalculoError as exc:
            agenda.pendientes.append(f"{titulo}: {exc}")

    contratos, error = leer_hecho(perfil, HECHO_CONTRATOS, validar_contratos, [])
    if error:
        agenda.pendientes.append(error)
    for con in contratos:
        vencimiento = date.fromisoformat(con["fecha_vencimiento"])
        if con["tipo"] in ("seguro", "seguro_vida"):
            agenda.vencimientos.append(
                calculo.calcular_seguro(
                    reglas,
                    hoy=hoy,
                    id=con["id"],
                    titulo=f"Renovación de «{con['etiqueta']}»",
                    fecha_vencimiento=vencimiento,
                    periodicidad_meses=con.get("periodicidad_meses") or 12,
                    es_vida=con["tipo"] == "seguro_vida",
                )
            )
        else:
            agenda.vencimientos.append(
                calculo.calcular_contrato(
                    hoy=hoy,
                    id=con["id"],
                    titulo=f"Renovación de «{con['etiqueta']}»",
                    fecha_vencimiento=vencimiento,
                    periodicidad_meses=con.get("periodicidad_meses"),
                    preaviso_dias=con.get("preaviso_dias"),
                )
            )

    compras, error = leer_hecho(perfil, HECHO_COMPRAS, validar_compras, [])
    if error:
        agenda.pendientes.append(error)
    for compra in compras:
        try:
            resultado = calcular_compra_de_hecho(reglas, compra, hoy)
        except CalculoError as exc:
            agenda.pendientes.append(f"Compra «{compra['descripcion']}»: {exc}")
            continue
        agenda.vencimientos.extend(resultado.vencimientos)

    agenda.vencimientos.sort(key=lambda v: (v.fecha_aviso is None, v.fecha_aviso or date.max, v.titulo))
    return agenda


def calcular_compra_de_hecho(reglas: Reglas, compra: Mapping[str, Any], hoy: date) -> calculo.ResultadoCompra:
    return calculo.calcular_compra(
        reglas,
        hoy=hoy,
        id=compra["id"],
        descripcion=compra["descripcion"],
        fecha_compra=date.fromisoformat(compra["fecha_compra"]),
        fecha_entrega=_d(compra.get("fecha_entrega")),
        tipo_bien=compra.get("tipo_bien", "bien"),
        modalidad=compra.get("modalidad", "tienda"),
        garantia_pactada_anos=compra.get("garantia_pactada_anos"),
        garantia_comercial_hasta=_d(compra.get("garantia_comercial_hasta")),
        vendedor_empresa=compra.get("vendedor_empresa", True),
    )


# --- Proactive notices --------------------------------------------------------

# Purchase-related dates only matter before they pass; documents, inspections and contracts warn when overdue.
_SIN_AVISO_VENCIDO = {"desistimiento", "presuncion", "garantia", "garantia_comercial", "seguro", "contrato"}


def _gravedad(dias: int) -> Gravedad:
    if dias <= 7:
        return "urgente"
    if dias <= 30:
        return "aviso"
    return "info"


def aviso_de(v: Vencimiento, hoy: date, antelaciones: Mapping[str, int]) -> AvisoModulo | None:
    """The notice due today for one dated item, or None.

    Only the closest threshold already reached is emitted (one notice per item and threshold, never all the
    crossed ones at once). Keys: ``caducidades:<item id>:<date>:<threshold>d`` (or ``:vencido``); every notice of
    an item replaces the earlier ones of the same item (``sustituye_prefijo``).
    """
    objetivo = v.fecha_aviso
    if objetivo is None or v.permanente:
        return None
    dias = (objetivo - hoy).days
    prefijo = f"caducidades:{v.id}:"
    fuentes = tuple(dict.fromkeys(f"{ev.boe_id} ({ev.articulo})" for ev in v.citas))
    acciones = tuple(v.tramite.pasos) if v.tramite else (v.que_hacer,)
    if dias < 0:
        if v.tipo in _SIN_AVISO_VENCIDO:
            return None
        return AvisoModulo(
            clave=f"{prefijo}{objetivo.isoformat()}:vencido",
            titulo=f"{v.titulo}: caducó el {fmt(objetivo)}",
            cuerpo=f"{v.titulo} venció el {fmt(objetivo)}. {v.que_hacer}",
            gravedad="urgente",
            acciones=acciones,
            fuentes=fuentes,
            vence=objetivo,
            sustituye_prefijo=prefijo,
        )
    alcanzados = [u for u in umbrales(v.tipo, antelaciones) if dias <= u]
    if not alcanzados:
        return None
    umbral = min(alcanzados)
    que = "vence" if v.accion_hasta is None else "es el último día para actuar"
    cuerpo = f"{v.titulo}: el {fmt(objetivo)} {que} (quedan {dias} días). {v.que_hacer}"
    if v.accion_hasta is not None and v.fecha is not None:
        cuerpo += f" Vencimiento: {fmt(v.fecha)}."
    return AvisoModulo(
        clave=f"{prefijo}{objetivo.isoformat()}:{umbral}d",
        titulo=f"{v.titulo}: {fmt(objetivo)} (en {dias} días)",
        cuerpo=cuerpo,
        gravedad=_gravedad(dias),
        acciones=acciones,
        fuentes=fuentes,
        vence=objetivo,
        sustituye_prefijo=prefijo,
    )


def avisos_de_agenda(perfil: Mapping[str, Any], hoy: date) -> list[AvisoModulo]:
    agenda = construir_agenda(perfil, hoy)
    avisos = [aviso for v in agenda.vencimientos if (aviso := aviso_de(v, hoy, agenda.antelaciones)) is not None]
    return avisos
