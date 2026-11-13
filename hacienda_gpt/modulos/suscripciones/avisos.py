"""Proactive notices of the ``suscripciones`` module, computed from the stored list of services only.

- renewal of a yearly or half-yearly service within ``SUSCRIPCIONES_DIAS_AVISO_RENOVACION`` days (30 by
  default) of the last day to stop it: one month before for insurance (art. 22.2 Ley 50/1980), two
  working days for telecom (art. 7 RD 899/2009), the estimated charge date otherwise;
- a price increase detected in the last 120 days;
- a double charge or a service charged twice detected in the last 120 days;
- a free trial that turned into a paid subscription in the last 60 days.

Deterministic and idempotent: the key of each notice is built from the service id and the date of the
event, so the same event never produces two notices. Monthly services get no renewal notice (they would
notify every month).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import os

from hacienda_gpt.modulos.contrato import AvisoModulo, ContextoModulo
from hacienda_gpt.modulos.suscripciones import almacen
from hacienda_gpt.modulos.suscripciones.derechos import DerechosConsumidor, cargar
from hacienda_gpt.modulos.suscripciones.deteccion import sumar_meses
from hacienda_gpt.modulos.suscripciones.formato import euros, fecha, porcentaje
from hacienda_gpt.modulos.suscripciones.modelos import CARGOS_POR_ANO, ServicioGuardado

DIAS_AVISO_POR_DEFECTO = 30
DIAS_RECIENTE = 120
DIAS_PRUEBA_RECIENTE = 60
PERIODICIDADES_RENOVACION = {"anual", "semestral"}


def dias_aviso() -> int:
    try:
        valor = int(os.environ.get("SUSCRIPCIONES_DIAS_AVISO_RENOVACION", DIAS_AVISO_POR_DEFECTO))
    except ValueError:
        return DIAS_AVISO_POR_DEFECTO
    return valor if 1 <= valor <= 120 else DIAS_AVISO_POR_DEFECTO


def _restar_dias_habiles(base: date, dias: int) -> date:
    actual = base
    while dias > 0:
        actual -= timedelta(days=1)
        if actual.weekday() < 5:
            dias -= 1
    return actual


def fecha_limite_baja(servicio: ServicioGuardado, datos: DerechosConsumidor) -> tuple[date, str] | None:
    """Last day to stop the next renewal and the legal basis of that date."""
    if servicio.proxima_fecha is None:
        return None
    if servicio.sector == "seguros":
        meses = datos.entero("seguro_oposicion_prorroga", "preaviso_tomador_meses")
        return (
            sumar_meses(servicio.proxima_fecha, -meses),
            f"en un seguro, la oposición a la prórroga debe notificarse por escrito al menos {meses} mes antes del "
            "fin del periodo (art. 22.2 Ley 50/1980)",
        )
    if servicio.sector == "telecomunicaciones":
        dias = datos.entero("telecom_duracion_y_baja", "antelacion_baja_dias_habiles")
        return (
            _restar_dias_habiles(servicio.proxima_fecha, dias),
            f"en telefonía, la baja surte efecto con {dias} días hábiles de antelación (art. 7 RD 899/2009)",
        )
    antelacion = datos.entero("aviso_renovacion", "antelacion_dias")
    return (
        servicio.proxima_fecha,
        "comprueba en tu contrato la fecha límite para comunicar que no quieres renovar; si contrataste a distancia, "
        f"la empresa debe avisarte con {antelacion} días de antelación a ese plazo (art. 97.1.p TRLGDCU)",
    )


def generar_avisos(contexto: ContextoModulo) -> list[AvisoModulo]:
    try:
        lista = almacen.leer(contexto.perfil)
    except ValueError:
        return []
    if lista is None:
        return []
    datos = cargar()
    hoy = contexto.hoy
    margen = dias_aviso()
    avisos: list[AvisoModulo] = []
    for s in lista.servicios:
        if s.estado != "activa":
            continue
        avisos.extend(_renovacion(s, datos, hoy, margen))
        avisos.extend(_subida(s, datos, hoy))
        avisos.extend(_duplicado(s, datos, hoy))
        if s.prueba and (hoy - s.prueba.fecha_primer_pago).days <= DIAS_PRUEBA_RECIENTE:
            avisos.append(
                AvisoModulo(
                    clave=f"suscripciones:prueba:{s.id}:{s.prueba.fecha_primer_pago.isoformat()}",
                    titulo=f"{s.etiqueta}: la prueba ya se cobra",
                    cuerpo=(
                        f"Tras un cargo de {euros(s.prueba.importe_prueba)} el {fecha(s.prueba.fecha_prueba)}, "
                        f"{s.etiqueta} cobra {euros(s.importe)} desde el {fecha(s.prueba.fecha_primer_pago)}. Si no "
                        "querías seguir, puedes darte de baja por el mismo medio por el que te diste de alta "
                        "(art. 62.3 TRLGDCU)."
                    ),
                    gravedad="info",
                    acciones=("Preparar la baja",),
                    fuentes=datos.citas("baja_tracto_sucesivo"),
                    importe=s.importe,
                )
            )
    return avisos


def _renovacion(s: ServicioGuardado, datos: DerechosConsumidor, hoy: date, margen: int) -> list[AvisoModulo]:
    if s.periodicidad not in PERIODICIDADES_RENOVACION or s.proxima_fecha is None:
        return []
    limite = fecha_limite_baja(s, datos)
    if limite is None:
        return []
    fecha_limite, fundamento = limite
    if not fecha_limite - timedelta(days=margen) <= hoy <= s.proxima_fecha:
        return []
    restantes = (fecha_limite - hoy).days
    if restantes < 0:
        estado = f"La fecha límite estimada para evitar esta renovación ({fecha(fecha_limite)}) ya ha pasado."
    else:
        estado = f"Tienes hasta el {fecha(fecha_limite)} ({restantes} días) para darte de baja si no quieres renovar."
    claves = ["baja_tracto_sucesivo"] + (
        ["seguro_oposicion_prorroga"]
        if s.sector == "seguros"
        else ["telecom_duracion_y_baja"] if s.sector == "telecomunicaciones" else ["aviso_renovacion"]
    )
    return [
        AvisoModulo(
            clave=f"suscripciones:renovacion:{s.id}:{s.proxima_fecha.isoformat()}",
            titulo=f"{s.etiqueta} se renueva el {fecha(s.proxima_fecha)}",
            cuerpo=(
                f"Próximo cargo {s.periodicidad} previsto de {euros(s.importe)} el {fecha(s.proxima_fecha)} "
                f"(estimado a partir de los cargos anteriores). {estado} Base: {fundamento}."
            ),
            gravedad="urgente" if restantes <= 7 else "aviso",
            acciones=("Preparar la baja", "Revisar el contrato"),
            fuentes=datos.citas(*claves),
            importe=s.importe,
            vence=fecha_limite,
        )
    ]


def _subida(s: ServicioGuardado, datos: DerechosConsumidor, hoy: date) -> list[AvisoModulo]:
    cambio = s.cambio_precio
    if cambio is None or cambio.nuevo <= cambio.anterior or (hoy - cambio.fecha).days > DIAS_RECIENTE:
        return []
    extra = (cambio.nuevo - cambio.anterior) * CARGOS_POR_ANO[s.periodicidad]
    if s.sector == "telecomunicaciones":
        derecho = (
            "en telefonía e internet el operador debe comunicarte los cambios con al menos "
            f"{datos.entero('telecom_cambio_condiciones', 'antelacion_comunicacion_meses')} mes de antelación y puedes "
            f"rescindir sin coste en {datos.entero('telecom_cambio_condiciones', 'plazo_rescision_meses')} mes desde la "
            "comunicación (art. 67.8 Ley 11/2022)"
        )
        claves = ("telecom_cambio_condiciones",)
    elif s.sector == "seguros":
        derecho = (
            "la aseguradora debe comunicar cualquier modificación del contrato al menos "
            f"{datos.entero('seguro_oposicion_prorroga', 'aviso_modificacion_meses')} meses antes del fin del periodo "
            "(art. 22.3 Ley 50/1980) y puedes oponerte a la prórroga (art. 22.2)"
        )
        claves = ("seguro_oposicion_prorroga",)
    else:
        derecho = (
            "es abusiva la subida sin razones objetivas que no te reconozca el derecho a resolver el contrato "
            "(art. 85.10 TRLGDCU), y puedes darte de baja en la misma forma en que contrataste (art. 62.3)"
        )
        claves = ("subida_precio", "baja_tracto_sucesivo")
    return [
        AvisoModulo(
            clave=f"suscripciones:subida:{s.id}:{cambio.fecha.isoformat()}",
            titulo=f"{s.etiqueta} ha subido de precio ({porcentaje(cambio.porcentaje)})",
            cuerpo=(
                f"Desde el {fecha(cambio.fecha)} pagas {euros(cambio.nuevo)} en lugar de {euros(cambio.anterior)}: "
                f"{euros(extra)} más al año. Base: {derecho}."
            ),
            gravedad="aviso",
            acciones=("Preparar la baja", "Comparar alternativas"),
            fuentes=datos.citas(*claves),
            importe=extra.quantize(Decimal("0.01")),
        )
    ]


def _duplicado(s: ServicioGuardado, datos: DerechosConsumidor, hoy: date) -> list[AvisoModulo]:
    d = s.duplicado
    if d is None or (hoy - d.fecha).days > DIAS_RECIENTE:
        return []
    semanas = datos.entero("devolucion_adeudos", "plazo_devolucion_semanas")
    vence = d.fecha + timedelta(weeks=semanas) if s.medio == "adeudo" and d.tipo == "cargo_doble" else None
    if d.tipo == "cargo_doble":
        titulo = f"{s.etiqueta}: cargo duplicado el {fecha(d.fecha)}"
        cuerpo = f"Se ha cobrado dos veces {euros(d.importe)} en el mismo periodo. Reclama el importe a la empresa"
        cuerpo += (
            f"; al ser un recibo domiciliado, puedes pedir a tu banco su devolución hasta el {fecha(vence)} "
            f"({semanas} semanas, art. 49.1 Real Decreto-ley 19/2018)."
            if vence
            else "; si fue un cargo con tarjeta que no autorizaste, comunícalo a tu banco sin demora (art. 43.1 "
            "Real Decreto-ley 19/2018)."
        )
    else:
        titulo = f"{s.etiqueta}: posible servicio duplicado"
        cuerpo = f"{d.detalle}. Si es el mismo servicio contratado dos veces, da de baja uno de ellos."
    return [
        AvisoModulo(
            clave=f"suscripciones:duplicado:{s.id}:{d.tipo}:{d.fecha.isoformat()}",
            titulo=titulo,
            cuerpo=cuerpo,
            gravedad="aviso",
            acciones=("Preparar la reclamación",),
            fuentes=datos.citas("devolucion_adeudos"),
            importe=d.importe,
            vence=vence,
        )
    ]
