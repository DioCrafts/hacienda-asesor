"""Tools of the ``caducidades`` module: deterministic, Spanish output with dates, quotes and caveats."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from hacienda_gpt.agent.providers.base import ToolSpec
from hacienda_gpt.modulos.caducidades import calculo
from hacienda_gpt.modulos.caducidades.agenda import (
    calcular_compra_de_hecho,
    construir_agenda,
    fecha_nacimiento,
)
from hacienda_gpt.modulos.caducidades.calculo import CalculoError, Vencimiento, fmt
from hacienda_gpt.modulos.caducidades.hechos import (
    HECHO_COMPRAS,
    HECHO_FECHAS_NACIMIENTO,
    Compra,
    leer_hecho,
    normalizar_titular,
    validar_compras,
    validar_fechas_nacimiento,
)
from hacienda_gpt.modulos.caducidades.reglas import Evidencia, Reglas, cargar_reglas
from hacienda_gpt.modulos.caducidades.ticket import verificar_propuesta
from hacienda_gpt.modulos.contrato import ContextoModulo, HerramientaModulo, ResultadoHerramienta

_FECHA = {"type": "string", "description": "Fecha AAAA-MM-DD."}


class _EntradaError(ValueError):
    pass


def _fecha(args: Mapping[str, Any], clave: str) -> date | None:
    valor = args.get(clave)
    if valor in (None, ""):
        return None
    try:
        return date.fromisoformat(str(valor))
    except ValueError as exc:
        raise _EntradaError(f"«{clave}» debe ser una fecha con formato AAAA-MM-DD (recibido: {valor}).") from exc


def _error(mensaje: str) -> ResultadoHerramienta:
    return ResultadoHerramienta(contenido=mensaje, resumen=mensaje[:120], es_error=True)


def _citas_texto(reglas: Reglas, citas: list[Evidencia]) -> list[str]:
    lineas: list[str] = []
    vistas: set[tuple[str, str, str]] = set()
    for ev in citas:
        if (ev.boe_id, ev.articulo, ev.cita) in vistas:
            continue
        vistas.add((ev.boe_id, ev.articulo, ev.cita))
        lineas.append(f"- {ev.articulo} ({reglas.url(ev.boe_id)}): «{ev.cita}»")
    return lineas


def _urls(reglas: Reglas, citas: list[Evidencia]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reglas.url(ev.boe_id) for ev in citas))


def texto_vencimiento(v: Vencimiento, hoy: date, reglas: Reglas, *, con_citas: bool = True) -> str:
    partes: list[str] = []
    if v.permanente:
        cabecera = f"{v.titulo}: validez permanente."
    elif v.fecha is None:
        cabecera = f"{v.titulo}: sin fecha (ver avisos)."
    else:
        dias = (v.fecha - hoy).days
        estado = f"en {dias} días" if dias >= 0 else f"hace {-dias} días"
        cabecera = f"{v.titulo}: {fmt(v.fecha)} ({estado}; fecha {v.origen})."
    partes.append(cabecera)
    if v.accion_hasta is not None:
        partes.append(f"Último día para actuar: {fmt(v.accion_hasta)} ({(v.accion_hasta - hoy).days} días).")
    partes.append(f"Qué hacer: {v.que_hacer}")
    if v.tramite is not None:
        partes.append(f"Trámite: {v.tramite.nombre} — {v.tramite.organismo}.")
        partes.extend(f"  · {paso}" for paso in v.tramite.pasos)
        if v.tramite.webs:
            partes.append("  Web oficial: " + ", ".join(v.tramite.webs))
    if v.lineas:
        partes.append("Cálculo:")
        partes.extend(
            f"  · {linea.concepto}: {linea.valor}" + (f" [{linea.referencia}]" if linea.referencia else "")
            for linea in v.lineas
        )
    if con_citas and v.citas:
        partes.append("Texto legal en que se basa:")
        partes.extend(_citas_texto(reglas, v.citas))
    if v.avisos:
        partes.append("Avisos:")
        partes.extend(f"  · {aviso}" for aviso in v.avisos)
    return "\n".join(partes)


# --- caducidades_proximas ---------------------------------------------------


def proximas(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    dias = args.get("dias", 365)
    if isinstance(dias, bool) or not isinstance(dias, int) or not 1 <= dias <= 3650:
        return _error("«dias» debe ser un número entero entre 1 y 3650.")
    incluir_vencidos = args.get("incluir_vencidos", True) is not False
    reglas = cargar_reglas()
    agenda = construir_agenda(ctx.perfil, ctx.hoy, reglas)
    lineas: list[str] = []
    sin_fecha: list[Vencimiento] = []
    citas: list[Evidencia] = []
    incluidos = 0
    for v in agenda.vencimientos:
        objetivo = v.fecha_aviso
        if objetivo is None:
            sin_fecha.append(v)
            continue
        restantes = (objetivo - ctx.hoy).days
        if restantes > dias or (restantes < 0 and not incluir_vencidos):
            continue
        if restantes < 0 and v.tipo in ("desistimiento", "presuncion", "garantia", "garantia_comercial"):
            continue  # a purchase date that already passed is history, not a to-do
        incluidos += 1
        estado = "VENCIDO" if restantes < 0 else f"en {restantes} días"
        linea = f"{incluidos}. {fmt(objetivo)} ({estado}) — {v.titulo}"
        if v.accion_hasta is not None and v.fecha is not None:
            linea += f" [último día para actuar; vence el {fmt(v.fecha)}]"
        linea += f"\n   Qué hacer: {v.que_hacer}"
        if v.tramite is not None:
            linea += f"\n   Dónde: {v.tramite.organismo}"
        referencias = list(dict.fromkeys(r.strip() for ln in v.lineas for r in ln.referencia.split(";") if r.strip()))
        if referencias:
            linea += "\n   Base: " + "; ".join(referencias)
        lineas.append(linea)
        citas.extend(v.citas)
    texto: list[str] = [f"Vencimientos de los próximos {dias} días (a {fmt(ctx.hoy)}), por fecha:"]
    texto.extend(lineas or ["(ninguno en ese plazo)"])
    if sin_fecha:
        texto.append("Sin fecha de aviso:")
        texto.extend(
            f"- {v.titulo}: " + ("permanente." if v.permanente else (v.avisos[-1] if v.avisos else "falta la fecha."))
            for v in sin_fecha
        )
    if agenda.pendientes:
        texto.append("No se han podido calcular:")
        texto.extend(f"- {p}" for p in agenda.pendientes)
    if not agenda.vencimientos and not agenda.pendientes:
        texto.append(
            "No hay documentos, vehículos, contratos ni compras registrados. Puedes darlos de alta: tipo, de quién son "
            "(titular, cónyuge, hijo N) y fechas; no hace falta el número del documento ni la matrícula."
        )
    texto.append(
        "Las fechas impresas en cada documento prevalecen sobre las calculadas. Pide el detalle de cualquiera con "
        "caducidades_calcular."
    )
    return ResultadoHerramienta(
        contenido="\n".join(texto),
        resumen=f"{incluidos} vencimiento(s) en {dias} días; {len(agenda.pendientes)} sin calcular",
        citas=_urls(reglas, citas),
    )


# --- caducidades_calcular ---------------------------------------------------

REGLAS_CALCULABLES = (
    "dni",
    "pasaporte",
    "permiso_conduccion",
    "itv",
    "certificado_electronico",
    "seguro",
    "garantia",
    "desistimiento",
)


def _nacimiento(args: Mapping[str, Any], ctx: ContextoModulo) -> date | None:
    propia = _fecha(args, "fecha_nacimiento")
    if propia is not None:
        return propia
    titular = args.get("titular")
    if not titular:
        return None
    fechas, _ = leer_hecho(ctx.perfil, HECHO_FECHAS_NACIMIENTO, validar_fechas_nacimiento, {})
    return fecha_nacimiento(ctx.perfil, normalizar_titular(titular), fechas)


def calcular(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    regla = args.get("regla")
    if regla not in REGLAS_CALCULABLES:
        return _error(f"«regla» debe ser una de: {', '.join(REGLAS_CALCULABLES)}.")
    reglas = cargar_reglas()
    try:
        v: Vencimiento
        if regla == "dni":
            v = calculo.calcular_dni(
                reglas,
                fecha_expedicion=_fecha(args, "fecha_expedicion"),
                fecha_nacimiento=_nacimiento(args, ctx),
                fecha_caducidad=_fecha(args, "fecha_caducidad"),
            )
        elif regla == "pasaporte":
            v = calculo.calcular_pasaporte(
                reglas,
                fecha_expedicion=_fecha(args, "fecha_expedicion"),
                fecha_nacimiento=_nacimiento(args, ctx),
                fecha_caducidad=_fecha(args, "fecha_caducidad"),
                tiene_dni=args.get("tiene_dni"),
            )
        elif regla == "permiso_conduccion":
            v = calculo.calcular_permiso(
                reglas,
                clases=list(args.get("clases") or []),
                fecha_inicio_vigencia=_fecha(args, "fecha_expedicion"),
                fecha_nacimiento=_nacimiento(args, ctx),
                fecha_caducidad=_fecha(args, "fecha_caducidad"),
                es_licencia=bool(args.get("es_licencia")),
            )
        elif regla == "itv":
            matriculacion = _fecha(args, "fecha_primera_matriculacion")
            if matriculacion is None or not args.get("categoria"):
                return _error("Para la ITV hacen falta «categoria» y «fecha_primera_matriculacion».")
            v = calculo.calcular_itv(
                reglas,
                hoy=ctx.hoy,
                categoria=str(args["categoria"]),
                fecha_primera_matriculacion=matriculacion,
                fecha_ultima_itv=_fecha(args, "fecha_ultima_itv"),
                fecha_caducidad_anterior=_fecha(args, "fecha_caducidad_itv_anterior"),
                fecha_proxima_itv=_fecha(args, "fecha_proxima_itv"),
            )
        elif regla == "certificado_electronico":
            v = calculo.calcular_certificado(
                reglas,
                fecha_caducidad=_fecha(args, "fecha_caducidad"),
                fecha_expedicion=_fecha(args, "fecha_expedicion"),
            )
        elif regla == "seguro":
            vencimiento = _fecha(args, "fecha_vencimiento")
            if vencimiento is None:
                return _error("Para un seguro hace falta «fecha_vencimiento» (fin del período en curso).")
            periodicidad = args.get("periodicidad_meses") or 12
            if isinstance(periodicidad, bool) or not isinstance(periodicidad, int) or not 1 <= periodicidad <= 120:
                return _error("«periodicidad_meses» debe ser un entero entre 1 y 120.")
            v = calculo.calcular_seguro(
                reglas,
                hoy=ctx.hoy,
                fecha_vencimiento=vencimiento,
                periodicidad_meses=periodicidad,
                es_vida=bool(args.get("es_vida")),
            )
        else:
            return _calcular_compra(args, ctx, reglas, solo=str(regla))
    except (_EntradaError, CalculoError, ValueError) as exc:
        return _error(str(exc))
    return ResultadoHerramienta(
        contenido=texto_vencimiento(v, ctx.hoy, reglas),
        resumen=f"{v.titulo}: " + ("permanente" if v.permanente else fmt(v.fecha)),
        citas=_urls(reglas, v.citas),
    )


def _compra_de_args(args: Mapping[str, Any]) -> Compra:
    campos = {
        k: args[k]
        for k in (
            "descripcion",
            "fecha_compra",
            "fecha_entrega",
            "tipo_bien",
            "modalidad",
            "garantia_pactada_anos",
            "garantia_comercial_hasta",
            "vendedor_empresa",
            "establecimiento",
            "importe",
        )
        if args.get(k) not in (None, "")
    }
    campos.setdefault("descripcion", "compra")
    if isinstance(campos.get("importe"), float):
        campos["importe"] = str(campos["importe"])
    try:
        return Compra.model_validate(campos)
    except ValueError as exc:
        raise _EntradaError(f"Datos de la compra no válidos: {exc}") from exc


def _texto_compra(resultado: calculo.ResultadoCompra, reglas: Reglas, hoy: date) -> str:
    partes = [f"Compra: {resultado.descripcion}"]
    if resultado.fin_desistimiento is not None:
        partes.append(f"Devolución sin dar motivos (desistimiento): hasta el {fmt(resultado.fin_desistimiento)}.")
    if resultado.fin_garantia is not None:
        partes.append(f"Garantía legal: hasta el {fmt(resultado.fin_garantia)}.")
    if resultado.fin_presuncion is not None:
        partes.append(
            f"El defecto se presume de origen (prueba a cargo del vendedor) hasta el {fmt(resultado.fin_presuncion)}."
        )
    if resultado.fin_garantia_comercial is not None:
        partes.append(f"Garantía comercial: hasta el {fmt(resultado.fin_garantia_comercial)}.")
    partes.append("Cálculo:")
    partes.extend(
        f"  · {ln.concepto}: {ln.valor}" + (f" [{ln.referencia}]" if ln.referencia else "") for ln in resultado.lineas
    )
    if resultado.citas:
        partes.append("Texto legal en que se basa:")
        partes.extend(_citas_texto(reglas, resultado.citas))
    if resultado.avisos:
        partes.append("Avisos:")
        partes.extend(f"  · {a}" for a in resultado.avisos)
    return "\n".join(partes)


def _calcular_compra(args: Mapping[str, Any], ctx: ContextoModulo, reglas: Reglas, solo: str) -> ResultadoHerramienta:
    compra = _compra_de_args(args)
    resultado = calcular_compra_de_hecho(reglas, compra.model_dump(mode="json", exclude_none=True), ctx.hoy)
    if solo == "desistimiento":
        fecha = resultado.fin_desistimiento
        resumen = f"Desistimiento hasta {fmt(fecha)}" if fecha else "Sin derecho legal de desistimiento"
    else:
        resumen = f"Garantía legal hasta {fmt(resultado.fin_garantia)}"
    return ResultadoHerramienta(
        contenido=_texto_compra(resultado, reglas, ctx.hoy), resumen=resumen, citas=_urls(reglas, resultado.citas)
    )


# --- caducidades_registrar_compra -------------------------------------------


def registrar_compra(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    if not args.get("descripcion") or not args.get("fecha_compra"):
        return _error("Para registrar una compra hacen falta «descripcion» y «fecha_compra».")
    reglas = cargar_reglas()
    try:
        compra = _compra_de_args(args)
        dato = compra.model_dump(mode="json", exclude_none=True)
        resultado = calcular_compra_de_hecho(reglas, dato, ctx.hoy)
    except (_EntradaError, CalculoError, ValueError) as exc:
        return _error(str(exc))
    texto = _texto_compra(resultado, reglas, ctx.hoy)
    guardar = args.get("guardar", True) is not False
    if guardar and ctx.registrar is not None:
        actuales, error = leer_hecho(ctx.perfil, HECHO_COMPRAS, validar_compras, [])
        if error:
            return _error(f"No se puede guardar: las compras ya registradas no son válidas ({error}).")
        nuevas = [c for c in actuales if c["id"] != dato["id"]] + [dato]
        try:
            ctx.registrar({HECHO_COMPRAS: validar_compras(nuevas)})
        except ValueError as exc:
            return _error(f"No se pudo guardar la compra: {exc}")
        texto += "\n\nCompra guardada: te avisaré antes de que acaben los plazos."
    elif guardar:
        texto += "\n\nNo se ha guardado (esta ejecución no puede escribir en tu expediente)."
    return ResultadoHerramienta(
        contenido=texto,
        resumen=f"{resultado.descripcion}: garantía hasta {fmt(resultado.fin_garantia)}"
        + (f", devolución hasta {fmt(resultado.fin_desistimiento)}" if resultado.fin_desistimiento else ""),
        citas=_urls(reglas, resultado.citas),
    )


# --- caducidades_leer_ticket ------------------------------------------------


def leer_ticket(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    texto = args.get("texto")
    if not isinstance(texto, str) or len(texto.strip()) < 10:
        return _error("Pásame el texto del tique o la factura en «texto».")
    propuesta = args.get("propuesta")
    if propuesta is not None and not isinstance(propuesta, list):
        return _error("«propuesta» debe ser una lista de {campo, valor, evidencia}.")
    resultado = verificar_propuesta(texto, propuesta)
    partes = ["Propuesta de compra leída del documento (no se ha guardado nada):"]
    if resultado.campos:
        partes.extend(f"- {c.campo}: {c.valor}  ← «{c.evidencia}»" for c in resultado.campos)
    else:
        partes.append("- No se ha podido leer ningún dato con su pasaje literal.")
    partes.extend(f"Aviso: {a}" for a in resultado.avisos)
    partes.append(
        "Confirma los datos con el usuario (y si fue compra online o en tienda, y si es un bien nuevo, de segunda mano "
        "o contenido digital) y llama a caducidades_registrar_compra."
    )
    return ResultadoHerramienta(
        contenido="\n".join(partes),
        resumen=f"{len(resultado.campos)} dato(s) propuestos del tique; {len(resultado.descartados)} descartados",
    )


# --- Specs ------------------------------------------------------------------

_COMPRA_PROPS: dict[str, Any] = {
    "descripcion": {"type": "string", "description": "Qué se compró."},
    "fecha_compra": _FECHA,
    "fecha_entrega": {**_FECHA, "description": "Fecha de entrega o del albarán, si es posterior a la compra."},
    "tipo_bien": {"type": "string", "enum": ["bien", "digital", "segunda_mano"]},
    "modalidad": {
        "type": "string",
        "enum": ["tienda", "distancia", "fuera_establecimiento", "visita_no_solicitada", "excursion"],
        "description": "tienda física, a distancia (online, teléfono), fuera del establecimiento…",
    },
    "garantia_pactada_anos": {"type": "integer", "description": "Solo segunda mano: plazo pactado (mínimo 1)."},
    "garantia_comercial_hasta": {**_FECHA, "description": "Fin de una garantía comercial adicional, si la hay."},
    "vendedor_empresa": {"type": "boolean", "description": "false si se compró a un particular."},
    "establecimiento": {"type": "string"},
    "importe": {"type": "string", "description": "Importe total en euros, con punto decimal."},
}


def _herramienta(
    nombre: str,
    descripcion: str,
    propiedades: dict[str, Any],
    ejecutar: Callable[[Mapping[str, Any], ContextoModulo], ResultadoHerramienta],
    requeridos: list[str] | None = None,
    efecto: str = "lectura",
) -> HerramientaModulo:
    schema: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": propiedades}
    if requeridos:
        schema["required"] = requeridos
    return HerramientaModulo(
        spec=ToolSpec(name=nombre, description=descripcion, input_schema=schema),
        ejecutar=ejecutar,
        efecto=efecto,  # type: ignore[arg-type]
    )


HERRAMIENTAS: tuple[HerramientaModulo, ...] = (
    _herramienta(
        "caducidades_proximas",
        "Lista por fecha los vencimientos del hogar (DNI, pasaporte, permiso de conducir, ITV, certificado "
        "electrónico, seguros y contratos, plazos de devolución y garantías de compras) con qué hacer y dónde.",
        {
            "dias": {"type": "integer", "description": "Horizonte en días (por defecto 365)."},
            "incluir_vencidos": {"type": "boolean", "description": "Incluir lo ya caducado (por defecto sí)."},
        },
        proximas,
    ),
    _herramienta(
        "caducidades_calcular",
        "Calcula una sola regla con su cita literal del BOE: caducidad del DNI o pasaporte por edad, vigencia del "
        "permiso de conducir (incluido 65 años o más), próxima ITV por categoría y antigüedad, certificado "
        "electrónico, plazo para no renovar un seguro, garantía legal o desistimiento de una compra.",
        {
            "regla": {"type": "string", "enum": list(REGLAS_CALCULABLES)},
            "titular": {"type": "string", "description": "titular, cónyuge o hijo N (para su fecha de nacimiento)."},
            "fecha_nacimiento": _FECHA,
            "fecha_expedicion": {**_FECHA, "description": "Expedición o última renovación/prórroga."},
            "fecha_caducidad": {**_FECHA, "description": "La impresa en el documento, si se conoce."},
            "clases": {"type": "array", "items": {"type": "string"}, "description": "Clases del permiso (B, C1…)."},
            "es_licencia": {"type": "boolean"},
            "tiene_dni": {"type": "boolean", "description": "Pasaporte de menor de 14: si tenía DNI."},
            "categoria": {
                "type": "string",
                "enum": list(cargar_reglas().itv.categorias),
                "description": "Categoría del vehículo (M1 turismo, N1 furgoneta, L moto, L1e ciclomotor…).",
            },
            "fecha_primera_matriculacion": _FECHA,
            "fecha_ultima_itv": {**_FECHA, "description": "Última inspección favorable."},
            "fecha_caducidad_itv_anterior": {**_FECHA, "description": "Caducidad que renovó esa inspección."},
            "fecha_proxima_itv": {**_FECHA, "description": "La anotada en la tarjeta ITV, si se conoce."},
            "fecha_vencimiento": {**_FECHA, "description": "Seguro: fin del período en curso."},
            "periodicidad_meses": {"type": "integer"},
            "es_vida": {"type": "boolean"},
            **_COMPRA_PROPS,
        },
        calcular,
        ["regla"],
    ),
    _herramienta(
        "caducidades_registrar_compra",
        "Calcula y guarda una compra: hasta cuándo se puede devolver (desistimiento) y hasta cuándo dura la "
        "garantía legal, con avisos antes de que acaben.",
        {**_COMPRA_PROPS, "guardar": {"type": "boolean", "description": "false para solo calcular."}},
        registrar_compra,
        ["descripcion", "fecha_compra"],
        efecto="prepara",
    ),
    _herramienta(
        "caducidades_leer_ticket",
        "Propone los datos de una compra a partir del texto de un tique o factura. Cada dato debe ir con el "
        "fragmento literal del documento; lo que no aparezca literalmente se descarta. No guarda nada.",
        {
            "texto": {"type": "string", "description": "Texto completo del tique o factura."},
            "propuesta": {
                "type": "array",
                "description": "Datos que has localizado, cada uno con su fragmento literal.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "campo": {
                            "type": "string",
                            "enum": ["descripcion", "establecimiento", "fecha_compra", "fecha_entrega", "importe"],
                        },
                        "valor": {"type": "string"},
                        "evidencia": {"type": "string"},
                    },
                    "required": ["campo", "valor", "evidencia"],
                },
            },
        },
        leer_ticket,
        ["texto"],
    ),
)
