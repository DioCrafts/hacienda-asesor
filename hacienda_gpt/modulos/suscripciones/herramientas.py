"""Tools of the ``suscripciones`` module (read, compute, prepare; nothing acts outside the app)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from hacienda_gpt.agent.providers.base import ToolSpec
from hacienda_gpt.modulos.contrato import ContextoModulo, HerramientaModulo, ResultadoHerramienta
from hacienda_gpt.modulos.suscripciones import almacen
from hacienda_gpt.modulos.suscripciones.derechos import DerechosConsumidor, cargar
from hacienda_gpt.modulos.suscripciones.deteccion import (
    ServicioDetectado,
    analizar,
    coste_anual,
    nivel_confianza,
    posible_olvido,
    sumar_meses,
)
from hacienda_gpt.modulos.suscripciones.formato import (
    MEDIO_TEXTO,
    PERIODICIDAD_TEXTO,
    euros,
    fecha,
    porcentaje,
    sumar_dias_habiles,
)
from hacienda_gpt.modulos.suscripciones.importadores import FORMATOS, bytes_de_entrada, importar
from hacienda_gpt.modulos.suscripciones.modelos import (
    CARGOS_POR_ANO,
    CATEGORIAS,
    ETIQUETAS_CATEGORIA,
    ImportacionError,
    ServicioGuardado,
)
from hacienda_gpt.modulos.suscripciones.tablas import configuracion_bancos

PRIVACIDAD = (
    "Privacidad: el extracto se ha procesado en memoria y no se ha guardado. Solo se guarda en tu expediente la "
    "lista de servicios recurrentes (nombre del comercio, importe, periodicidad, fechas del último y del próximo "
    "cargo, categoría y los cambios detectados); ni movimientos, ni saldos, ni números de cuenta."
)
LIMITES = (
    "La detección es heurística y determinista: un servicio con menos cargos de los necesarios (p. ej. un alta "
    "reciente o un pago anual que aparece una sola vez) puede no detectarse o detectarse con confianza baja. La "
    "aplicación no sabe si usas cada servicio."
)


def _error(mensaje: str) -> ResultadoHerramienta:
    return ResultadoHerramienta(contenido=mensaje, resumen=mensaje[:120], es_error=True)


def _citas_generales(datos: DerechosConsumidor) -> tuple[str, ...]:
    return datos.citas("baja_tracto_sucesivo", "subida_precio", "devolucion_adeudos", "atencion_clientela_ley_10_2025")


# --- suscripciones_analizar_extracto --------------------------------------------------------------

_CAMPOS_MAPEO = ["fecha", "fecha_valor", "concepto", "detalle", "importe", "cargo", "abono", "saldo", "signo", "divisa"]

SPEC_ANALIZAR = ToolSpec(
    name="suscripciones_analizar_extracto",
    description=(
        "Analiza un extracto bancario exportado por el usuario (Norma 43 / Cuaderno 43 de la AEB, CSV o Excel XLSX "
        "del banco, OFX/QFX o camt.053 XML) y detecta los cargos recurrentes: suscripciones, seguros, suministros, "
        "telefonía, préstamos, gimnasio…, con su periodicidad, próximo cargo previsto, subidas de precio, cargos "
        "duplicados y pruebas gratuitas convertidas en pago. El extracto se procesa en memoria y no se guarda; se "
        "guarda solo la lista de servicios detectados. Pasa el fichero tal cual: como texto en `contenido` o, si es "
        "binario (XLSX), en base64 en `contenido_base64`. Si responde que hay columnas ambiguas o importes sin "
        "signo, pregunta al usuario y repite con `mapeo_columnas` o `importes_positivos_son`."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "contenido": {"type": "string", "description": "Texto del fichero (Norma 43, CSV, OFX/QFX, camt.053)."},
            "contenido_base64": {"type": "string", "description": "Fichero binario (XLSX) codificado en base64."},
            "formato": {"type": "string", "enum": list(FORMATOS), "description": "Formato; 'auto' lo detecta."},
            "banco": {
                "type": "string",
                "enum": sorted(configuracion_bancos()["bancos"]),
                "description": "Banco que exportó el CSV/XLSX, si se sabe (solo para avisar si la cabecera no es la esperada).",
            },
            "mapeo_columnas": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "propertyNames": {"enum": _CAMPOS_MAPEO},
                "description": 'Para CSV/XLSX: campo -> nombre exacto de la columna, p. ej. {"importe": "Importe"}.',
            },
            "importes_positivos_son": {
                "type": "string",
                "enum": ["cargos", "abonos"],
                "description": "Solo si el usuario confirma que todos los importes sin signo son cargos o abonos.",
            },
            "guardar": {
                "type": "boolean",
                "description": "Guardar la lista de servicios detectados en el expediente (por defecto, sí).",
            },
        },
    },
)


def _linea_servicio(detectado: ServicioDetectado, hoy: date) -> list[str]:
    s = detectado.guardado
    importe = f"{'media ' if s.importe_variable else ''}{euros(s.importe)}"
    lineas = [
        f"• {s.etiqueta} — {importe} {PERIODICIDAD_TEXTO[s.periodicidad]} ({MEDIO_TEXTO[s.medio]}); "
        f"{s.num_cargos} cargo(s) del {fecha(s.primera_fecha)} al {fecha(s.ultima_fecha)}"
        + (f"; próximo previsto el {fecha(s.proxima_fecha)}" if s.proxima_fecha else "")
        + f"; gasto anual {euros(coste_anual(s))}; confianza {nivel_confianza(s.confianza)} ({str(s.confianza).replace('.', ',')}). "
        f"Categoría: {ETIQUETAS_CATEGORIA[s.categoria]}"
        + (" (sugerida por el etiquetador automático)" if s.categoria_origen == "llm" else "")
        + f". [id {s.id}]"
    ]
    for cambio in detectado.cambios:
        signo = "Subida" if cambio.nuevo > cambio.anterior else "Bajada"
        lineas.append(
            f"   {signo} de precio el {fecha(cambio.fecha)}: {euros(cambio.anterior)} → {euros(cambio.nuevo)} "
            f"({porcentaje(cambio.porcentaje)})."
        )
    for duplicado in detectado.duplicados:
        if duplicado.tipo == "cargo_doble":
            lineas.append(f"   Cargo duplicado el {fecha(duplicado.fecha)}: {duplicado.detalle}.")
        else:
            lineas.append(f"   Posible servicio duplicado: {duplicado.detalle}.")
    if s.prueba:
        lineas.append(
            f"   Prueba convertida en pago: cargo de {euros(s.prueba.importe_prueba)} el {fecha(s.prueba.fecha_prueba)} "
            f"y cobro completo desde el {fecha(s.prueba.fecha_primer_pago)}."
        )
    if posible_olvido(s):
        lineas.append("   Cargo pequeño y constante desde hace meses: comprueba si todavía lo usas.")
    lineas.extend(f"   {o}" for o in detectado.observaciones)
    return lineas


def _recortar(texto: str, maximo: int = 150) -> str:
    return texto if len(texto) <= maximo else texto[:maximo].rsplit(" ", 1)[0] + "…"


def _que_puedes_hacer(detectados: list[ServicioDetectado], datos: DerechosConsumidor, hoy: date) -> list[str]:
    lineas = [
        "Qué puedes hacer:",
        "- Darte de baja en la misma forma en que contrataste, sin penalizaciones desproporcionadas (art. 62.3 "
        f"TRLGDCU: «{_recortar(datos.derecho('baja_tracto_sucesivo').evidencia[1].cita)}»). La herramienta "
        "suscripciones_preparar_baja redacta la carta o el correo con su fundamento legal.",
    ]
    if any(d.cambios and d.cambios[-1].nuevo > d.cambios[-1].anterior for d in detectados):
        lineas.append(
            "- Ante una subida de precio: es abusiva la subida sin razones objetivas que no te reconozca el derecho a "
            "resolver el contrato (art. 85.10 TRLGDCU); en telefonía e internet, el operador debe avisarte con "
            f"{datos.entero('telecom_cambio_condiciones', 'antelacion_comunicacion_meses')} mes de antelación y "
            "puedes rescindir sin coste (art. 67.8 Ley 11/2022); en seguros, la aseguradora debe comunicar las "
            f"modificaciones con {datos.entero('seguro_oposicion_prorroga', 'aviso_modificacion_meses')} meses de "
            "antelación (art. 22.3 Ley 50/1980)."
        )
    if any(d.duplicados for d in detectados):
        semanas = datos.entero("devolucion_adeudos", "plazo_devolucion_semanas")
        lineas.append(
            f"- Ante un cargo duplicado por recibo domiciliado: puedes pedir a tu banco la devolución durante {semanas} "
            "semanas desde el cargo (art. 49.1 Real Decreto-ley 19/2018); reclama además a la empresa: las "
            "reclamaciones por cobros indebidos deben responderse en "
            f"{datos.entero('atencion_clientela_ley_10_2025', 'plazo_cobros_indebidos_dias')} días si la empresa está "
            "en el ámbito de la Ley 10/2025 (art. 17.3)."
        )
    return lineas


def ejecutar_analizar(argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    try:
        datos_fichero = bytes_de_entrada(argumentos.get("contenido"), argumentos.get("contenido_base64"))
        extracto = importar(
            datos_fichero,
            formato=str(argumentos.get("formato") or "auto"),
            banco=argumentos.get("banco"),
            mapeo_columnas=argumentos.get("mapeo_columnas"),
            importes_positivos_son=argumentos.get("importes_positivos_son"),
        )
    except ImportacionError as exc:
        return _error(f"No se ha podido leer el extracto: {exc}")
    del datos_fichero  # the raw statement is not kept beyond this call
    analisis = analizar(extracto)
    datos = cargar()
    detectados = list(analisis.servicios)
    activos = [d for d in detectados if d.guardado.estado == "activa"]
    inactivos = [d for d in detectados if d.guardado.estado != "activa"]
    total_anual = almacen.total([d.guardado for d in activos])

    cabecera = [
        f"Extracto leído: formato {extracto.formato}"
        + (f", {extracto.banco}" if extracto.banco else "")
        + (f", cuenta {extracto.cuenta}" if extracto.cuenta else "")
        + f"; {analisis.num_movimientos} movimientos del {fecha(analisis.desde)} al {fecha(analisis.hasta)} "
        f"({analisis.num_cargos} cargos)."
    ]
    if extracto.columnas:
        cabecera.append(
            "Columnas usadas: "
            + ", ".join(f"{campo} = «{columna}»" for campo, columna in extracto.columnas.items())
            + "."
        )
    cabecera.extend(f"Aviso de lectura: {a}" for a in extracto.avisos)

    cuerpo: list[str] = []
    if activos:
        cuerpo.append(
            f"Servicios recurrentes activos: {len(activos)}; gasto anual estimado {euros(total_anual)} "
            f"({euros(total_anual / 12)} al mes)."
        )
        for detectado in activos:
            cuerpo.extend(_linea_servicio(detectado, contexto.hoy))
    else:
        cuerpo.append("No se han detectado cargos recurrentes activos en este extracto.")
    if inactivos:
        cuerpo.append("Cargos recurrentes que ya no aparecen al final del extracto:")
        for detectado in inactivos:
            cuerpo.extend(_linea_servicio(detectado, contexto.hoy))

    guardado_txt = "La lista no se ha guardado (lo has pedido así)."
    if argumentos.get("guardar", True) is not False:
        if contexto.registrar is None:
            guardado_txt = "La lista no se ha guardado: esta ejecución no puede escribir en el expediente."
        else:
            try:
                anterior = almacen.leer(contexto.perfil)
            except ValueError:
                anterior = None
            fusion = almacen.fusionar(anterior, [d.guardado for d in detectados], contexto.hoy, analisis.hasta)
            contexto.registrar({almacen.NOMBRE_HECHO: fusion.model_dump(mode="json")})
            guardado_txt = (
                f"Guardada la lista de servicios en tu expediente ({len(fusion.servicios)} en total, incluidos los de "
                "extractos anteriores)."
            )

    contenido = "\n".join(
        cabecera
        + [""]
        + cuerpo
        + [""]
        + _que_puedes_hacer(detectados, datos, contexto.hoy)
        + ["", guardado_txt, PRIVACIDAD, LIMITES]
    )
    resumen = (
        f"{len(activos)} servicios recurrentes activos, {euros(total_anual)} al año"
        + (f"; {sum(1 for d in activos if d.cambios)} con cambio de precio" if any(d.cambios for d in activos) else "")
        + (f"; {sum(1 for d in activos if d.duplicados)} con duplicados" if any(d.duplicados for d in activos) else "")
    )
    return ResultadoHerramienta(contenido=contenido, resumen=resumen, citas=_citas_generales(datos))


# --- suscripciones_listado ------------------------------------------------------------------------

SPEC_LISTADO = ToolSpec(
    name="suscripciones_listado",
    description=(
        "Lista los servicios recurrentes guardados en el expediente (detectados en extractos analizados antes): "
        "importe, periodicidad, último y próximo cargo, categoría, cambios de precio y duplicados."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "incluir_inactivas": {"type": "boolean", "description": "Incluir los que ya no se cobran."},
            "categoria": {"type": "string", "enum": list(CATEGORIAS)},
        },
    },
)


def _guardados(contexto: ContextoModulo) -> list[ServicioGuardado] | ResultadoHerramienta:
    try:
        lista = almacen.leer(contexto.perfil)
    except ValueError as exc:
        return _error(f"La lista de servicios guardada está dañada: {exc}")
    return list(lista.servicios) if lista else []


def ejecutar_listado(argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    servicios = _guardados(contexto)
    if isinstance(servicios, ResultadoHerramienta):
        return servicios
    if not servicios:
        return ResultadoHerramienta(
            contenido="Aún no hay servicios guardados. Analiza un extracto con suscripciones_analizar_extracto.",
            resumen="Sin servicios guardados",
        )
    if not argumentos.get("incluir_inactivas"):
        servicios = [s for s in servicios if s.estado == "activa"]
    if categoria := argumentos.get("categoria"):
        servicios = [s for s in servicios if s.categoria == categoria]
    lineas = []
    for s in servicios:
        detectado = ServicioDetectado(
            s,
            (s.cambio_precio,) if s.cambio_precio else (),
            (s.duplicado,) if s.duplicado else (),
            ("Sin cargos recientes: puede que ya esté dada de baja.",) if s.estado != "activa" else (),
        )
        lineas.extend(_linea_servicio(detectado, contexto.hoy))
    total = almacen.total([s for s in servicios if s.estado == "activa"])
    contenido = "\n".join(
        [f"{len(servicios)} servicio(s); gasto anual de los activos: {euros(total)}."]
        + lineas
        + ["", "Las fechas «próximo previsto» son estimaciones a partir de los cargos anteriores.", PRIVACIDAD]
    )
    return ResultadoHerramienta(contenido=contenido, resumen=f"{len(servicios)} servicios, {euros(total)} al año")


# --- suscripciones_gasto_anual --------------------------------------------------------------------

SPEC_GASTO = ToolSpec(
    name="suscripciones_gasto_anual",
    description=(
        "Calcula el gasto anual estimado en servicios recurrentes activos, agrupado por categoría (streaming, "
        "telefonía, seguros, suministros, préstamos, gimnasio…), con el desglose de cada servicio."
    ),
    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
)


def ejecutar_gasto_anual(argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    servicios = _guardados(contexto)
    if isinstance(servicios, ResultadoHerramienta):
        return servicios
    activos = [s for s in servicios if s.estado == "activa"]
    if not activos:
        return ResultadoHerramienta(
            contenido="No hay servicios activos guardados. Analiza un extracto con suscripciones_analizar_extracto.",
            resumen="Sin servicios activos",
        )
    por_categoria: dict[str, list[ServicioGuardado]] = {}
    for s in activos:
        por_categoria.setdefault(s.categoria, []).append(s)
    filas = sorted(por_categoria.items(), key=lambda par: -almacen.total(par[1]))
    total = almacen.total(activos)
    lineas = [f"Gasto anual estimado en servicios recurrentes: {euros(total)} ({euros(total / 12)} al mes).", ""]
    for categoria, lista in filas:
        subtotal = almacen.total(lista)
        peso = (subtotal / total * 100) if total else Decimal(0)
        peso_txt = f"{peso:.1f}".replace(".", ",")
        lineas.append(f"{ETIQUETAS_CATEGORIA[categoria]}: {euros(subtotal)} al año ({peso_txt} %)")
        for s in sorted(lista, key=lambda x: -x.coste_anual):
            lineas.append(
                f"   {s.etiqueta}: {euros(s.importe)} × {CARGOS_POR_ANO[s.periodicidad]} "
                f"({PERIODICIDAD_TEXTO[s.periodicidad]}) = {euros(s.coste_anual)}"
                + (" — importe variable: media de los cargos" if s.importe_variable else "")
                + (" — confianza baja" if nivel_confianza(s.confianza) == "baja" else "")
            )
    avisos = [
        "",
        "Avisos:",
        "- Es una estimación: importe del último cargo (o la media, si varía) por el número de cargos al año.",
        "- Los préstamos y el alquiler se incluyen porque son cargos recurrentes, aunque no son suscripciones.",
    ]
    if any(s.duplicado for s in activos):
        avisos.append("- Hay servicios con cargos duplicados: el total puede incluir importes que deberías recuperar.")
    return ResultadoHerramienta(
        contenido="\n".join(lineas + avisos),
        resumen=f"{euros(total)} al año en {len(activos)} servicios",
    )


# --- suscripciones_preparar_baja ------------------------------------------------------------------

MOTIVOS = ["baja", "no_renovar", "subida_precio", "desistimiento", "cobro_tras_baja"]

SPEC_BAJA = ToolSpec(
    name="suscripciones_preparar_baja",
    description=(
        "Prepara un BORRADOR de carta o correo para darse de baja de un servicio recurrente (o no renovarlo, "
        "rechazar una subida de precio, desistir de un contrato a distancia o reclamar un cobro posterior a la "
        "baja), con su fundamento legal citado literalmente del BOE y los plazos calculados. No envía nada: el "
        "usuario lo revisa, completa sus datos y lo envía por el mismo canal por el que contrató."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["servicio"],
        "properties": {
            "servicio": {"type": "string", "description": "Id (srv-…) o nombre del servicio."},
            "motivo": {"type": "string", "enum": MOTIVOS},
            "canal": {"type": "string", "enum": ["email", "carta"]},
            "sector": {
                "type": "string",
                "enum": ["general", "telecomunicaciones", "seguros", "suministros"],
                "description": "Solo para un servicio que no esté en la lista guardada.",
            },
            "fecha_contratacion": {"type": "string", "description": "AAAA-MM-DD, para el desistimiento."},
            "fecha_vencimiento": {"type": "string", "description": "AAAA-MM-DD: fin del periodo en curso (seguros)."},
        },
    },
)


def _buscar(servicios: list[ServicioGuardado], texto: str) -> list[ServicioGuardado]:
    exacto = [s for s in servicios if s.id == texto.strip()]
    if exacto:
        return exacto
    buscado = texto.strip().lower()
    iguales = [s for s in servicios if s.etiqueta.lower() == buscado]
    return iguales or [s for s in servicios if buscado and buscado in s.etiqueta.lower()]


def _fecha_arg(argumentos: Mapping[str, Any], nombre: str) -> date | None:
    valor = argumentos.get(nombre)
    if not valor:
        return None
    return date.fromisoformat(str(valor))


def _cita(datos: DerechosConsumidor, clave: str, indice: int) -> str:
    ev = datos.derecho(clave).evidencia[indice]
    return f"«{ev.cita}» ({ev.articulo}, {datos.norma(ev.boe_id)})"


def ejecutar_preparar_baja(argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    servicios = _guardados(contexto)
    if isinstance(servicios, ResultadoHerramienta):
        return servicios
    try:
        fecha_contratacion = _fecha_arg(argumentos, "fecha_contratacion")
        fecha_vencimiento = _fecha_arg(argumentos, "fecha_vencimiento")
    except ValueError:
        return _error("Las fechas deben tener el formato AAAA-MM-DD.")
    texto = str(argumentos.get("servicio") or "")
    encontrados = _buscar(servicios, texto)
    if len(encontrados) > 1:
        return _error(
            "Hay varios servicios que encajan: "
            + "; ".join(f"{s.etiqueta} [{s.id}]" for s in encontrados)
            + ". Indica el id."
        )
    servicio = encontrados[0] if encontrados else None
    if servicio is None and not argumentos.get("sector"):
        disponibles = ", ".join(f"{s.etiqueta} [{s.id}]" for s in servicios) or "ninguno"
        return _error(
            f"No encuentro «{texto}» entre los servicios guardados ({disponibles}). Si no está en la lista, indica "
            "su sector (general, telecomunicaciones, seguros o suministros)."
        )
    etiqueta = servicio.etiqueta.removesuffix(" (PayPal)") if servicio else texto.strip()
    sector = servicio.sector if servicio else str(argumentos["sector"])
    motivo = str(argumentos.get("motivo") or "baja")
    canal = str(argumentos.get("canal") or "email")
    datos = cargar()
    hoy = contexto.hoy

    if sector in {"no_aplica", "financiero"}:
        explicacion = {
            "vivienda": "Es un pago de vivienda (alquiler o comunidad): su baja se rige por el contrato de arrendamiento "
            "o por los estatutos de la comunidad, que este módulo no modela.",
            "prestamos": "Es la cuota de un préstamo: no se cancela con una carta de baja; la amortización anticipada "
            "se rige por su normativa propia, que este módulo no modela.",
            "banca_comisiones": "Es una comisión bancaria: revísala con tu banco (contrato de cuenta); este módulo no "
            "modela su normativa.",
        }.get(
            servicio.categoria if servicio else "",
            "Es una transferencia periódica que ordenas tú: puedes cancelarla en tu banca online sin carta.",
        )
        return ResultadoHerramienta(contenido=explicacion, resumen=f"{etiqueta}: no aplica carta de baja")

    fundamentos: list[str] = []
    plazos: list[str] = []
    claves_citadas = ["baja_tracto_sucesivo", "atencion_cliente", "atencion_clientela_ley_10_2025"]
    peticion = f"solicito la baja del servicio «{etiqueta}» y la cancelación de la domiciliación o del cargo recurrente asociado"

    if motivo == "desistimiento":
        inicio = fecha_contratacion or (servicio.primera_fecha if servicio else None)
        if inicio is None:
            return _error("Para el desistimiento necesito la fecha de contratación (fecha_contratacion, AAAA-MM-DD).")
        plazo = datos.entero("desistimiento", "plazo_dias")
        limite = inicio + timedelta(days=plazo)
        if hoy > limite:
            return _error(
                f"El plazo de desistimiento de {plazo} días naturales desde el {fecha(inicio)} terminó el "
                f"{fecha(limite)}; ya no puedes desistir, pero sí darte de baja (motivo 'baja')."
            )
        peticion = (
            f"ejerzo mi derecho de desistimiento del contrato del servicio «{etiqueta}», celebrado a distancia el "
            f"{fecha(inicio)}, y solicito el reembolso de las cantidades abonadas"
        )
        fundamentos += [
            _cita(datos, "desistimiento", 0),
            _cita(datos, "desistimiento", 1),
            _cita(datos, "desistimiento", 2),
        ]
        plazos.append(
            f"Plazo de desistimiento: {plazo} días naturales desde el {fecha(inicio)}: hasta el {fecha(limite)}"
            + ("" if fecha_contratacion else " (fecha tomada del primer cargo del extracto: confírmala)")
            + ". Solo para contratos a distancia o fuera del establecimiento, salvo las excepciones del art. 103 TRLGDCU."
        )
        claves_citadas.append("desistimiento")
    elif sector == "seguros":
        meses = datos.entero("seguro_oposicion_prorroga", "preaviso_tomador_meses")
        vencimiento = fecha_vencimiento or (
            servicio.proxima_fecha if servicio and servicio.periodicidad == "anual" else None
        )
        if motivo in {"baja", "no_renovar", "subida_precio"}:
            peticion = (
                f"comunico mi oposición a la prórroga de la póliza del seguro «{etiqueta}», de modo que el contrato "
                "quede extinguido al término del periodo de seguro en curso"
            )
        fundamentos.append(_cita(datos, "seguro_oposicion_prorroga", 1))
        if vencimiento:
            limite = sumar_meses(vencimiento, -meses)
            plazos.append(
                f"Fin del periodo en curso: {fecha(vencimiento)}"
                + ("" if fecha_vencimiento else " (estimado por el último cargo anual: confírmalo en la póliza)")
                + f". La notificación escrita debe llegar al menos {meses} mes antes: a más tardar el {fecha(limite)}."
            )
            if hoy > limite:
                plazos.append(
                    "ATENCIÓN: ese plazo ya ha pasado para este vencimiento; la oposición surtiría efecto en el "
                    "siguiente, salvo que la póliza diga otra cosa."
                )
        else:
            plazos.append(
                f"Mira en la póliza la fecha de vencimiento: la notificación escrita debe hacerse al menos {meses} mes "
                "antes del fin del periodo en curso."
            )
        if motivo == "subida_precio":
            fundamentos.append(_cita(datos, "seguro_oposicion_prorroga", 2))
        fundamentos.append(_cita(datos, "seguro_oposicion_prorroga", 3))
        claves_citadas.append("seguro_oposicion_prorroga")
    elif sector == "telecomunicaciones":
        dias = datos.entero("telecom_duracion_y_baja", "antelacion_baja_dias_habiles")
        fundamentos += [_cita(datos, "telecom_duracion_y_baja", 2), _cita(datos, "telecom_duracion_y_baja", 3)]
        plazos.append(
            f"La baja surte efecto con una antelación mínima de {dias} días hábiles: si la envías hoy, a partir del "
            f"{fecha(sumar_dias_habiles(hoy, dias))} (sin contar festivos, que no se modelan). El operador no puede "
            "facturar lo devengado después."
        )
        if motivo == "subida_precio":
            meses = datos.entero("telecom_cambio_condiciones", "plazo_rescision_meses")
            peticion = (
                f"no acepto la modificación de las condiciones (subida de precio) del servicio «{etiqueta}» y ejerzo mi "
                "derecho a rescindir el contrato sin coste alguno"
            )
            fundamentos += [
                _cita(datos, "telecom_cambio_condiciones", 0),
                _cita(datos, "telecom_cambio_condiciones", 1),
            ]
            plazos.append(f"Debes rescindir en el plazo de {meses} mes desde la comunicación del cambio.")
            claves_citadas.append("telecom_cambio_condiciones")
        fundamentos.append(_cita(datos, "telecom_duracion_y_baja", 1))
        fundamentos.append(_cita(datos, "permanencia_proporcional", 0))
        claves_citadas += ["telecom_duracion_y_baja", "permanencia_proporcional"]
    else:
        if motivo == "no_renovar":
            peticion = f"comunico mi voluntad de no renovar el contrato del servicio «{etiqueta}» a su vencimiento"
            fundamentos += [_cita(datos, "prorroga_automatica", 0), _cita(datos, "prorroga_automatica", 1)]
            fundamentos.append(_cita(datos, "aviso_renovacion", 1))
            claves_citadas += ["prorroga_automatica", "aviso_renovacion"]
        if motivo == "subida_precio":
            peticion = (
                f"no acepto la subida de precio del servicio «{etiqueta}» y, en consecuencia, resuelvo el contrato"
            )
            fundamentos += [_cita(datos, "subida_precio", 0), _cita(datos, "subida_precio", 1)]
            claves_citadas.append("subida_precio")
        if sector == "suministros":
            plazos.append(
                "Suministros (luz, gas, agua): si cambias de compañía, la nueva suele tramitar la baja; su normativa "
                "sectorial no está modelada aquí."
            )

    if motivo == "cobro_tras_baja":
        semanas = datos.entero("devolucion_adeudos", "plazo_devolucion_semanas")
        cargo = servicio.ultima_fecha if servicio else None
        peticion = (
            f"reclamo la devolución de los importes cobrados por el servicio «{etiqueta}» después de la fecha en que "
            "solicité la baja ([fecha de la solicitud de baja])"
        )
        fundamentos += [_cita(datos, "devolucion_adeudos", 0), _cita(datos, "devolucion_adeudos", 1)]
        plazos.append(
            f"Si el cargo fue un recibo domiciliado, puedes pedir a tu banco su devolución durante {semanas} semanas"
            + (
                f" desde el cargo (para el último, del {fecha(cargo)}, hasta el {fecha(cargo + timedelta(weeks=semanas))})"
                if cargo
                else " desde el cargo"
            )
            + ". Devolver el recibo no da de baja el contrato."
        )
        claves_citadas.append("devolucion_adeudos")
    if motivo != "desistimiento":
        fundamentos.insert(0, _cita(datos, "baja_tracto_sucesivo", 1))
        fundamentos.insert(1, _cita(datos, "baja_tracto_sucesivo", 2))
    fundamentos.append(_cita(datos, "atencion_cliente", 0))
    fundamentos.append(_cita(datos, "atencion_clientela_ley_10_2025", 2))

    ley10 = datos.derecho("atencion_clientela_ley_10_2025")
    adaptacion = sumar_meses(
        date.fromisoformat(ley10.fuente.en_vigor_desde),
        datos.entero("atencion_clientela_ley_10_2025", "meses_adaptacion"),
    )
    dias_resolucion = datos.entero("atencion_clientela_ley_10_2025", "plazo_resolucion_dias_habiles")
    plazos.append(
        f"Respuesta: la empresa debe darte una clave o justificante de tu solicitud; si está en el ámbito de la Ley "
        f"10/2025 (servicios básicos o grandes empresas), debe resolverla en {dias_resolucion} días hábiles"
        + (
            f" ({datos.entero('atencion_clientela_ley_10_2025', 'plazo_cobros_indebidos_dias')} días si es un cobro indebido)"
            if motivo == "cobro_tras_baja"
            else ""
        )
        + (
            f"; las empresas tienen hasta el {fecha(adaptacion)} para adaptar sus servicios de atención a esa ley"
            if hoy < adaptacion
            else ""
        )
        + f". Fuera de ese ámbito, el TRLGDCU fija {datos.entero('atencion_cliente', 'plazo_respuesta_dias')} días para responder reclamaciones (art. 21.3)."
    )

    asunto = {
        "baja": f"Solicitud de baja del servicio {etiqueta}",
        "no_renovar": f"No renovación del contrato {etiqueta}",
        "subida_precio": f"Rechazo de la subida de precio y baja: {etiqueta}",
        "desistimiento": f"Desistimiento del contrato {etiqueta}",
        "cobro_tras_baja": f"Reclamación de cobros posteriores a la baja: {etiqueta}",
    }[motivo]
    if sector == "seguros" and motivo != "desistimiento":
        asunto = f"Oposición a la prórroga de la póliza {etiqueta}"
    encabezado = (
        [f"Asunto: {asunto}", "Para: [correo de atención al cliente o de bajas de la empresa]"]
        if canal == "email"
        else [
            "[Nombre y apellidos]\n[Dirección]\n",
            f"A la atención del servicio de atención al cliente de {etiqueta}\n[Dirección postal de la empresa]\n",
            f"[Lugar], a {fecha(hoy)}",
            f"Asunto: {asunto}",
        ]
    )
    carta = encabezado + [
        "",
        "Muy señores míos:",
        "",
        "Yo, [nombre y apellidos], con DNI/NIE [número], titular del contrato o de la cuenta de cliente "
        f"[número de cliente, de póliza o línea], por medio de la presente {peticion}, con efectos desde la fecha "
        "más próxima que permita la ley.",
        "",
        "Fundamento:",
        *[f"- {f}" for f in fundamentos],
        "",
        "Les ruego que me envíen confirmación escrita de la baja, con su clave identificativa o justificante, y que "
        "no efectúen ningún cargo posterior. En caso contrario, solicitaré la devolución de los recibos a mi entidad "
        "y presentaré reclamación ante los organismos de consumo.",
        "",
        "Atentamente,",
        "[Firma]" if canal == "carta" else "[Nombre y apellidos]",
    ]
    if sector == "seguros":
        carta.append("\n(Envíala por un medio que deje constancia escrita: la ley exige notificación escrita.)")

    contenido = "\n".join(
        [
            f"BORRADOR (no se ha enviado nada) — {asunto}",
            "",
            *carta,
            "",
            "Plazos y notas:",
            *[f"- {p}" for p in plazos],
            "- Envíala por el mismo medio por el que contrataste (art. 62.3 TRLGDCU) y guarda el justificante.",
            "- Completa los datos entre corchetes; la aplicación no los conoce ni los guarda.",
            "- No se ha comprobado si tu contrato concreto tiene condiciones más favorables; las cláusulas que limiten "
            "estos derechos no te vinculan en lo que sean abusivas.",
        ]
    )
    return ResultadoHerramienta(
        contenido=contenido,
        resumen=f"Borrador de {asunto.lower()}",
        citas=datos.citas(*dict.fromkeys(claves_citadas)),
    )


def _envolver(
    funcion: Callable[[Mapping[str, Any], ContextoModulo], ResultadoHerramienta],
) -> Callable[[Mapping[str, Any], ContextoModulo], ResultadoHerramienta]:
    """No exception ever reaches the agent: unexpected errors become a Spanish error result."""

    def ejecutar(argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
        try:
            return funcion(argumentos or {}, contexto)
        except ImportacionError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(f"Error interno del módulo de suscripciones ({type(exc).__name__}). No se ha guardado nada.")

    return ejecutar


HERRAMIENTAS: tuple[HerramientaModulo, ...] = (
    HerramientaModulo(SPEC_ANALIZAR, _envolver(ejecutar_analizar), "lectura"),
    HerramientaModulo(SPEC_LISTADO, _envolver(ejecutar_listado), "lectura"),
    HerramientaModulo(SPEC_GASTO, _envolver(ejecutar_gasto_anual), "lectura"),
    HerramientaModulo(SPEC_BAJA, _envolver(ejecutar_preparar_baja), "prepara"),
)
