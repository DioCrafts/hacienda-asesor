"""The mortgage module's tools: schedule, next review, early repayment, change of bank.

Every tool reads the ``hipoteca_prestamo`` fact, computes with :mod:`calculo` and :mod:`comisiones`, and
returns Spanish text with the figures, the BOE sources and the caveats. Errors come back as
``ResultadoHerramienta(es_error=True)`` with a readable message, never as an exception.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from hacienda_gpt.agent.providers.base import ToolSpec
from hacienda_gpt.modulos.contrato import ContextoModulo, HerramientaModulo, ResultadoHerramienta
from hacienda_gpt.modulos.hipoteca.calculo import (
    Cuadro,
    Supuestos,
    calcular_cuadro,
    comparar_con_banco,
    cuota_francesa,
    dato_para_revision,
    euros,
    fechas_revision,
    filas_entre,
    fmt,
    fmt_pct,
    sumar_meses,
    tipo_revisado,
)
from hacienda_gpt.modulos.hipoteca.comisiones import Comision, comision_maxima, tramo_en
from hacienda_gpt.modulos.hipoteca.indices import INDICES, BOE_DOC_URL, Dato, SerieIndices, cargar_serie
from hacienda_gpt.modulos.hipoteca.modelo import AmortizacionRealizada, Prestamo
from hacienda_gpt.modulos.hipoteca.normativa import Normativa, cargar_normativa

ZERO = Decimal(0)

REDONDEO = (
    "Redondeo: cuota e intereses de cada periodo redondeados al céntimo (mitad hacia arriba), tipo mensual = "
    "TIN/12 y la última cuota ajusta el resto. Los bancos usan convenciones parecidas pero no idénticas "
    "(días reales/360, redondeo por truncamiento, cuota ajustada al céntimo superior), así que tu recibo "
    "puede diferir en céntimos por cuota y en algunos euros en el capital pendiente tras muchos años."
)
FISCAL = (
    "Fiscalidad: si compraste tu vivienda habitual antes de 2013 y ya aplicabas la deducción por vivienda "
    "(régimen transitorio, disposición transitoria 18.ª LIRPF), lo que pagas de hipoteca en el año cuenta para "
    "esa deducción con su límite; la calcula el módulo fiscal (dato pagos_vivienda_habitual_pre2013), no este."
)


class DatosHipotecaError(ValueError):
    """The loan data or the tool arguments are not usable; the message is for the user."""


class _Entorno:
    """What every tool needs: the loan, the published indices, the law and today's date."""

    def __init__(self, contexto: ContextoModulo, serie: SerieIndices | None = None, normativa: Normativa | None = None):
        raw = contexto.perfil.get("hipoteca_prestamo")
        if raw is None:
            raise DatosHipotecaError(
                "No tengo los datos de tu hipoteca (dato hipoteca_prestamo). Necesito al menos: capital inicial, "
                "fecha de firma, plazo en meses, si es fija, variable o mixta, el tipo inicial y, si es variable o "
                "mixta, el índice, el diferencial, cada cuánto se revisa y cuántos meses dura el tipo inicial."
            )
        try:
            self.prestamo = Prestamo.model_validate(raw)
        except ValidationError as exc:
            errores = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
            raise DatosHipotecaError(f"Los datos guardados de la hipoteca no son válidos: {errores}") from exc
        self.hoy = contexto.hoy
        self.serie = serie or cargar_serie()
        self.normativa = normativa or cargar_normativa()

    def cuadro(self, **kwargs: Any) -> Cuadro:
        return calcular_cuadro(self.prestamo, self.serie, Supuestos(hoy=self.hoy, **kwargs))

    def url(self, boe_id: str) -> str:
        if boe_id in self.normativa.fuentes_principales:
            return self.normativa.url(boe_id)
        return BOE_DOC_URL.format(boe_id=boe_id)


def _fecha(valor: Any, nombre: str) -> date | None:
    if valor in (None, ""):
        return None
    try:
        return date.fromisoformat(str(valor))
    except ValueError as exc:
        raise DatosHipotecaError(f"{nombre} debe ser una fecha AAAA-MM-DD (recibido «{valor}»).") from exc


def _decimal(valor: Any, nombre: str, minimo: Decimal | None = None) -> Decimal | None:
    if valor in (None, ""):
        return None
    try:
        texto = str(valor).strip().replace("€", "").replace("%", "").replace(" ", "")
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        numero = Decimal(texto)
    except InvalidOperation as exc:
        raise DatosHipotecaError(f"{nombre} debe ser un número (recibido «{valor}»).") from exc
    if not numero.is_finite() or (minimo is not None and numero < minimo):
        raise DatosHipotecaError(f"{nombre} no es válido (recibido «{valor}»).")
    return numero


def _describe(p: Prestamo) -> str:
    base = f"{fmt(p.capital_inicial)} € firmada el {p.fecha_firma:%d/%m/%Y} a {p.plazo_meses} meses"
    if p.tipo == "fijo":
        return f"Hipoteca de {base}, tipo fijo del {fmt_pct(p.tipo_inicial)}, sistema francés."
    indice = INDICES.get(p.indice or "", p.indice)
    variable = f"{indice} + {fmt_pct(p.diferencial or ZERO)}"
    if p.tipo == "mixto":
        return (
            f"Hipoteca mixta de {base}: {fmt_pct(p.tipo_inicial)} fijo los {p.meses_tipo_inicial} primeros meses y "
            f"después {variable}, revisable cada {p.revision_meses} meses, sistema francés."
        )
    return (
        f"Hipoteca variable de {base}: {fmt_pct(p.tipo_inicial)} los {p.meses_tipo_inicial} primeros meses y "
        f"después {variable}, revisable cada {p.revision_meses} meses, sistema francés."
    )


def _dato_texto(d: Dato | None) -> str:
    if d is None:
        return "sin dato publicado"
    return d.cita


def _ejecutar(
    funcion: Callable[[Mapping[str, Any], ContextoModulo], ResultadoHerramienta],
) -> Callable[[Mapping[str, Any], ContextoModulo], ResultadoHerramienta]:
    def envoltura(args: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
        try:
            return funcion(args, contexto)
        except DatosHipotecaError as exc:
            return ResultadoHerramienta(contenido=str(exc), resumen="Faltan o no valen datos", es_error=True)
        except (ValueError, ArithmeticError) as exc:
            return ResultadoHerramienta(
                contenido=f"No he podido hacer el cálculo con esos datos: {exc}",
                resumen="Error de cálculo",
                es_error=True,
            )

    envoltura.__name__ = funcion.__name__
    return envoltura


# --- hipoteca_cuadro ------------------------------------------------------------------------------


def pagos_del_ejercicio(contexto: ContextoModulo, ejercicio: int) -> dict[str, Decimal] | None:
    """Capital and interest of the ordinary instalments due in ``ejercicio`` (for the tax module), plus the
    early repayments made that year. None when there is no loan in the profile."""
    if contexto.perfil.get("hipoteca_prestamo") is None:
        return None
    entorno = _Entorno(contexto)
    cuadro = entorno.cuadro()
    capital, intereses = cuadro.pagos_del_ano(ejercicio)
    extra = sum((a.importe for a in entorno.prestamo.amortizaciones_realizadas if a.fecha.year == ejercicio), ZERO)
    return {"capital": capital, "intereses": intereses, "amortizaciones_anticipadas": extra}


@_ejecutar
def hipoteca_cuadro(args: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    e = _Entorno(contexto)
    p = e.prestamo
    fecha = _fecha(args.get("fecha"), "fecha") or e.hoy
    ejercicio = int(args.get("ejercicio") or fecha.year)
    n_cuotas = max(1, min(int(args.get("cuotas") or 12), 120))
    cuadro = e.cuadro()
    pagada = cuadro.ultima_pagada(fecha)
    proxima = cuadro.proxima(fecha)
    vigente = cuadro.cambio_vigente(fecha)
    lineas = [_describe(p), "", f"A fecha {fecha:%d/%m/%Y}:"]
    pagadas = pagada.numero if pagada else 0
    lineas.append(
        f"- Cuotas vencidas: {pagadas} de {len(cuadro.filas)}. Capital pendiente: "
        f"{fmt(cuadro.capital_pendiente(fecha))} €"
        + (f" (tras la cuota del {pagada.fecha:%d/%m/%Y}; sin los intereses devengados desde entonces)." if pagada else ".")
    )
    origen = {
        "contrato": "tipo inicial del contrato",
        "banco": "el que te comunicó el banco",
        "boe": vigente.detalle,
        "estimado": "estimado: " + vigente.detalle,
    }[vigente.origen]
    lineas.append(f"- Tipo vigente: {fmt_pct(vigente.tipo)} desde el {vigente.fecha:%d/%m/%Y} ({origen}).")
    if vigente.dato is not None:
        lineas.append(f"  Índice usado: {vigente.dato.cita}.")
    if proxima is not None:
        lineas.append(
            f"- Próxima cuota: n.º {proxima.numero}, {proxima.fecha:%d/%m/%Y}: {fmt(proxima.cuota)} € "
            f"(intereses {fmt(proxima.intereses)} €, capital {fmt(proxima.amortizacion)} €)."
        )
    revisiones = [r for r in fechas_revision(p) if r > fecha]
    if revisiones:
        lineas.append(f"- Próxima revisión del tipo: {revisiones[0]:%d/%m/%Y} (usa hipoteca_revision para estimarla).")
    lineas.append(
        f"- Última cuota prevista: {cuadro.fecha_fin:%d/%m/%Y}. Intereses que quedan por pagar: "
        f"{fmt(cuadro.intereses_desde(fecha))} €"
        + (" (estimado: las revisiones futuras usan el último índice publicado)." if cuadro.hay_estimados() else ".")
    )
    lineas += ["", f"Próximas {n_cuotas} cuotas (n.º · fecha · tipo · cuota · intereses · capital · pendiente):"]
    for f in filas_entre(cuadro, fecha, n_cuotas):
        marca = " (estimada)" if f.estimado else ""
        extra = f" · amortización anticipada {fmt(f.amortizacion_extra)} €" if f.amortizacion_extra else ""
        lineas.append(
            f"  {f.numero} · {f.fecha:%d/%m/%Y} · {fmt_pct(f.tipo)} · {fmt(f.cuota)} €{marca} · {fmt(f.intereses)} € · "
            f"{fmt(f.amortizacion)} € · {fmt(f.capital_pendiente)} €{extra}"
        )
    capital, intereses = cuadro.pagos_del_ano(ejercicio)
    extra_ano = sum((a.importe for a in p.amortizaciones_realizadas if a.fecha.year == ejercicio), ZERO)
    lineas += [
        "",
        f"Año {ejercicio}: cuotas por {fmt(capital + intereses)} € (capital {fmt(capital)} €, intereses "
        f"{fmt(intereses)} €)" + (f" y amortizaciones anticipadas por {fmt(extra_ano)} €." if extra_ano else "."),
    ]
    if p.adquisicion_antes_2013 is not False and p.vivienda_habitual:
        lineas.append(FISCAL)
    comprobacion = comparar_con_banco(cuadro)
    if comprobacion:
        lineas += ["", "Comprobación con las cifras de tu banco:"] + [f"- {x}" for x in comprobacion]
    else:
        lineas += [
            "",
            "Para comprobar el cálculo con tu banco, añade en comprobacion_banco la cuota y el capital pendiente "
            "de un recibo (y en revisiones_conocidas los tipos que te aplicó en cada revisión).",
        ]
    lineas += ["", REDONDEO, *cuadro.avisos]
    citas = sorted({c.dato.boe_id for c in cuadro.cambios if c.dato is not None})
    return ResultadoHerramienta(
        contenido="\n".join(lineas),
        resumen=f"Capital pendiente {fmt(cuadro.capital_pendiente(fecha))} €"
        + (f"; próxima cuota {fmt(proxima.cuota)} €" if proxima else ""),
        citas=tuple(e.url(c) for c in citas),
    )


# --- hipoteca_revision ----------------------------------------------------------------------------


def estimar_revision(
    e: _Entorno, fecha_revision: date, cuadro: Cuadro | None = None, desplazamientos: list[Decimal] | None = None
) -> dict[str, Any]:
    """Estimate of a review: index value used, new rate, instalment before and after, sensitivity."""
    p = e.prestamo
    cuadro = cuadro or e.cuadro()
    dato, proyeccion = dato_para_revision(p, e.serie, fecha_revision, e.hoy)
    if dato is None:
        raise DatosHipotecaError("No hay ningún valor publicado del índice de tu préstamo en la serie del BOE.")
    afectadas = [f for f in cuadro.filas if sumar_meses(f.fecha, -p.periodicidad_meses) >= fecha_revision]
    if not afectadas:
        raise DatosHipotecaError("Esa revisión cae después de la última cuota.")
    primera = afectadas[0]
    anteriores = [f for f in cuadro.filas if f.numero < primera.numero]
    cuota_antes = anteriores[-1].cuota if anteriores else primera.cuota
    capital = anteriores[-1].capital_pendiente if anteriores else p.capital_inicial
    restantes = len(cuadro.filas) - primera.numero + 1

    def escenario(valor: Decimal) -> tuple[Decimal, Decimal]:
        tipo, _ = tipo_revisado(p, valor)
        return tipo, cuota_francesa(capital, tipo, restantes, p.periodicidad_meses)

    tipo, detalle = tipo_revisado(p, dato.valor)
    cuota = cuota_francesa(capital, tipo, restantes, p.periodicidad_meses)
    sensibilidad = []
    for d in desplazamientos or []:
        t, c = escenario(dato.valor + d)
        sensibilidad.append((d, dato.valor + d, t, c))
    return {
        "fecha": fecha_revision,
        "dato": dato,
        "proyeccion": proyeccion,
        "tipo": tipo,
        "detalle": detalle,
        "cuota_antes": cuota_antes,
        "cuota": cuota,
        "capital": capital,
        "restantes": restantes,
        "primera_cuota": primera,
        "sensibilidad": sensibilidad,
    }


def _regla_indice(p: Prestamo) -> str:
    if p.indice_regla == "meses_antes":
        return f"el valor del mes que está {p.indice_meses_antes} mes(es) antes del mes de la revisión"
    return "el último valor publicado en el BOE en la fecha de la revisión"


@_ejecutar
def hipoteca_revision(args: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    e = _Entorno(contexto)
    p = e.prestamo
    if p.tipo == "fijo":
        return ResultadoHerramienta(
            contenido=_describe(p) + "\nTu préstamo es a tipo fijo: no tiene revisiones de tipo; la cuota no cambia "
            "salvo que amortices anticipadamente.",
            resumen="Préstamo fijo: sin revisiones",
        )
    fecha = _fecha(args.get("fecha_revision"), "fecha_revision")
    futuras = [r for r in fechas_revision(p) if r > e.hoy]
    if fecha is None:
        if not futuras:
            return ResultadoHerramienta(contenido="No quedan revisiones de tipo en tu préstamo.", resumen="Sin revisiones")
        fecha = futuras[0]
    desplazamientos = [
        _decimal(x, "desplazamientos_pp") or ZERO for x in (args.get("desplazamientos_pp") or ["-1", "-0.5", "0.5", "1"])
    ]
    est = estimar_revision(e, fecha, desplazamientos=desplazamientos)
    dato: Dato = est["dato"]
    dif = est["cuota"] - est["cuota_antes"]
    lineas = [_describe(p), ""]
    lineas.append(f"Revisión del {fecha:%d/%m/%Y} (se aplica desde la cuota n.º {est['primera_cuota'].numero}).")
    lineas.append(f"Regla del contrato: {_regla_indice(p)}.")
    if est["proyeccion"]:
        lineas.append(
            f"El índice que corresponde todavía no está publicado. Estimación con el último publicado: {dato.cita}."
        )
    else:
        lineas.append(f"Índice que corresponde (ya publicado): {dato.cita}.")
    lineas.append(f"Nuevo tipo: {est['detalle']}.")
    lineas.append(
        f"Cuota: de {fmt(est['cuota_antes'])} € a {fmt(est['cuota'])} € ({'+' if dif >= 0 else ''}{fmt(dif)} € al mes), "
        f"sobre un capital de {fmt(est['capital'])} € y {est['restantes']} cuotas restantes."
    )
    if est["sensibilidad"]:
        lineas += ["", "Sensibilidad (si el índice fuera distinto):"]
        for d, valor, tipo, cuota in est["sensibilidad"]:
            lineas.append(
                f"- índice {fmt_pct(valor)} ({'+' if d >= 0 else ''}{fmt(d)} puntos): tipo {fmt_pct(tipo)}, cuota "
                f"{fmt(cuota)} € ({'+' if cuota >= est['cuota_antes'] else ''}{fmt(cuota - est['cuota_antes'])} €)."
            )
    ultimos = sorted(e.serie.datos.get(p.indice or "", {}).values(), key=lambda d: d.mes)[-6:]
    if ultimos:
        lineas += ["", "Últimos valores publicados del índice:"] + [f"- {d.cita}" for d in ultimos]
    if p.revisiones_conocidas:
        lineas.append("Las revisiones que ya te comunicó el banco se usan tal cual (revisiones_conocidas).")
    lineas += ["", f"Fuente de los índices: resoluciones del Banco de España en el BOE ({e.normativa.indices_oficiales.cita_corta()}).", REDONDEO]
    citas = {e.url(dato.boe_id), e.normativa.url(e.normativa.indices_oficiales.fuente.boe_id)}
    return ResultadoHerramienta(
        contenido="\n".join(lineas),
        resumen=f"Revisión {fecha:%d/%m/%Y}: cuota {fmt(est['cuota'])} € ({'+' if dif >= 0 else ''}{fmt(dif)} €)",
        citas=tuple(sorted(citas)),
    )


# --- hipoteca_amortizar ---------------------------------------------------------------------------


def _valor_futuro_ahorros(base: Cuadro, otro: Cuadro, desde: date, horizonte: date, r_mensual: Decimal) -> Decimal:
    """Future value at ``horizonte`` of investing every month what ``otro`` saves against ``base``."""
    pagos_otro = {f.fecha: f.cuota for f in otro.filas if f.fecha > desde}
    total = ZERO
    for f in base.filas:
        if f.fecha <= desde:
            continue
        ahorro = f.cuota - pagos_otro.get(f.fecha, ZERO)
        meses = (horizonte.year - f.fecha.year) * 12 + horizonte.month - f.fecha.month
        total += ahorro * (1 + r_mensual) ** meses
    return total


def _comision_texto(c: Comision) -> list[str]:
    lineas = [f"- {x}" for x in c.fundamento]
    if c.importe > 0 and c.es_maximo:
        lineas.append(
            "- Es un máximo: la comisión no puede superar la pérdida financiera del banco (art. 23.8 Ley 5/2019), "
            "que puede ser menor o nula. Pide al banco la información del art. 23.2 (te la debe dar en tres días hábiles)."
        )
    return lineas


@_ejecutar
def hipoteca_amortizar(args: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    e = _Entorno(contexto)
    p = e.prestamo
    importe = _decimal(args.get("importe"), "importe", Decimal("0.01"))
    if importe is None:
        raise DatosHipotecaError("Indica el importe que quieres amortizar (importe, en euros).")
    base = e.cuadro()
    fecha = _fecha(args.get("fecha"), "fecha")
    if fecha is None:
        proxima = base.proxima(e.hoy)
        if proxima is None:
            raise DatosHipotecaError("El préstamo ya está pagado según el cuadro.")
        fecha = proxima.fecha
    pendiente_antes = base.capital_pendiente(fecha)
    if importe >= pendiente_antes:
        importe = pendiente_antes
    comision = comision_maxima(p, e.normativa, fecha, importe)
    plazo = e.cuadro(amortizaciones=(AmortizacionRealizada(fecha=fecha, importe=importe, modalidad="plazo"),))
    cuota = e.cuadro(amortizaciones=(AmortizacionRealizada(fecha=fecha, importe=importe, modalidad="cuota"),))
    ahorro_plazo = base.intereses_desde(fecha) - plazo.intereses_desde(fecha)
    ahorro_cuota = base.intereses_desde(fecha) - cuota.intereses_desde(fecha)
    sig_base = base.proxima(fecha)
    sig_cuota = cuota.proxima(fecha)
    lineas = [_describe(p), ""]
    lineas.append(
        f"Amortizar {fmt(importe)} € el {fecha:%d/%m/%Y} (tras la cuota de esa fecha; capital pendiente antes: "
        f"{fmt(pendiente_antes)} €)."
    )
    lineas += ["", "Comisión por amortización anticipada:"]
    lineas.append(f"- {fmt(comision.importe)} € ({fmt_pct(comision.porcentaje * 100)} del importe).")
    lineas += _comision_texto(comision)
    lineas += ["", "A) Reducir plazo (misma cuota, acabas antes):"]
    if plazo.filas and plazo.filas[-1].capital_pendiente == 0 and importe >= pendiente_antes:
        lineas.append("- Cancelas el préstamo.")
    lineas.append(
        f"- Última cuota: {plazo.fecha_fin:%d/%m/%Y} en vez de {base.fecha_fin:%d/%m/%Y} "
        f"({len(base.filas) - len(plazo.filas)} cuotas menos)."
    )
    lineas.append(f"- Intereses que te ahorras: {fmt(ahorro_plazo)} €; neto de comisión: {fmt(ahorro_plazo - comision.importe)} €.")
    lineas += ["", "B) Reducir cuota (mismo plazo):"]
    if sig_base and sig_cuota:
        lineas.append(f"- Cuota: de {fmt(sig_base.cuota)} € a {fmt(sig_cuota.cuota)} € ({fmt(sig_cuota.cuota - sig_base.cuota)} € al mes).")
    lineas.append(f"- Intereses que te ahorras: {fmt(ahorro_cuota)} €; neto de comisión: {fmt(ahorro_cuota - comision.importe)} €.")
    lineas.append(
        "- Reducir plazo ahorra más intereses; reducir cuota da más margen mensual. Con el mismo dinero, la diferencia "
        f"de intereses entre ambas es de {fmt(ahorro_plazo - ahorro_cuota)} €."
    )
    if base.hay_estimados():
        lineas.append(
            "- En un préstamo variable el ahorro futuro depende de los índices: se ha estimado con el último publicado."
        )
    rentabilidad = _decimal(args.get("rentabilidad_alternativa_pct"), "rentabilidad_alternativa_pct", Decimal(-50))
    if rentabilidad is not None:
        r_anual = rentabilidad / 100
        r_mensual = (1 + r_anual) ** (Decimal(1) / 12) - 1
        horizonte = base.fecha_fin
        meses = (horizonte.year - fecha.year) * 12 + horizonte.month - fecha.month
        invertido = importe + comision.importe
        fv_invertir = euros(invertido * (1 + r_mensual) ** meses)
        fv_plazo = euros(_valor_futuro_ahorros(base, plazo, fecha, horizonte, r_mensual))
        fv_cuota = euros(_valor_futuro_ahorros(base, cuota, fecha, horizonte, r_mensual))
        lineas += [
            "",
            f"Comparación aritmética con tu alternativa ({fmt_pct(rentabilidad)} anual neta, supuesto tuyo), con el mismo "
            f"dinero ({fmt(invertido)} €, importe más comisión) y hasta el fin actual del préstamo ({horizonte:%d/%m/%Y}):",
            f"- Invertirlo: {fmt(fv_invertir)} € al final.",
            f"- Amortizar reduciendo plazo e invertir cada mes lo que dejas de pagar: {fmt(fv_plazo)} €.",
            f"- Amortizar reduciendo cuota e invertir cada mes la diferencia de cuota: {fmt(fv_cuota)} €.",
        ]
        mejor = max((fv_invertir, "invertir"), (fv_plazo, "reducir plazo"), (fv_cuota, "reducir cuota"))
        lineas.append(f"- Con estos supuestos sale mayor: {mejor[1]}.")
        tipo_actual = base.cambio_vigente(fecha).tipo
        lineas.append(
            f"- Amortizar equivale a obtener, sin riesgo, el tipo de tu préstamo ({fmt_pct(tipo_actual)} ahora) menos el "
            "efecto de la comisión; la alternativa solo gana si su rentabilidad neta lo supera de forma sostenida."
        )
        lineas.append(
            "- No es una recomendación de inversión: la rentabilidad que indicas no está garantizada, la inversión "
            "tiene riesgo y liquidez distinta, y amortizar no se puede deshacer. Los rendimientos de la alternativa "
            "tributan en el IRPF: usa una rentabilidad neta de impuestos o pide el cálculo al módulo fiscal."
        )
    ra = e.normativa.reembolso_anticipado
    lineas += [
        "",
        f"Derechos: puedes amortizar en cualquier momento; el contrato puede exigir un preaviso de como máximo "
        f"{ra.preaviso_maximo_meses} mes, y el banco debe darte la información de la operación en tres días hábiles "
        f"({ra.cita_corta()}). Si tienes un seguro vinculado del que el banco es beneficiario, al cancelar tienes "
        "derecho al extorno de la prima no consumida (art. 23.3 Ley 5/2019).",
    ]
    if p.vivienda_habitual and p.adquisicion_antes_2013 is not False:
        lineas.append(FISCAL + " Lo que amortices también entra en esa base: compruébalo con el módulo fiscal.")
    lineas.append(REDONDEO)
    citas = sorted({e.normativa.url(b) for b in comision.citas} | {e.normativa.url(ra.fuente.boe_id)})
    return ResultadoHerramienta(
        contenido="\n".join(lineas),
        resumen=(
            f"Amortizar {fmt(importe)} €: ahorro {fmt(ahorro_plazo)} € (plazo) o {fmt(ahorro_cuota)} € (cuota); "
            f"comisión máx. {fmt(comision.importe)} €"
        ),
        citas=tuple(citas),
    )


# --- hipoteca_cambiar_banco -----------------------------------------------------------------------

_GASTOS = ("tasacion", "notaria", "registro", "gestoria", "comision_apertura", "comision_novacion", "impuestos", "otros")


def _oferta(args: Mapping[str, Any], e: _Entorno, fecha: date, capital: Decimal, restantes_meses: int) -> Prestamo:
    o = args.get("oferta")
    if not isinstance(o, Mapping):
        raise DatosHipotecaError(
            "Indica la oferta: oferta = {tipo: fijo|variable|mixto, tipo_interes (TIN %), y si es variable o mixta "
            "diferencial, indice, meses_tipo_inicial y revision_meses; opcional plazo_meses}."
        )
    tipo = o.get("tipo") or "fijo"
    datos: dict[str, Any] = {
        "capital_inicial": capital,
        "fecha_firma": fecha,
        "plazo_meses": int(o.get("plazo_meses") or restantes_meses),
        "tipo": tipo,
        "tipo_inicial": o.get("tipo_interes"),
        "periodicidad_meses": e.prestamo.periodicidad_meses,
    }
    if datos["tipo_inicial"] in (None, ""):
        raise DatosHipotecaError("Falta el tipo de interés de la oferta (oferta.tipo_interes, TIN en %).")
    if tipo != "fijo":
        datos.update(
            indice=o.get("indice") or "euribor_12m",
            diferencial=o.get("diferencial"),
            meses_tipo_inicial=o.get("meses_tipo_inicial") or (6 if tipo == "variable" else None),
            revision_meses=o.get("revision_meses") or 12,
        )
    try:
        return Prestamo.model_validate(datos)
    except ValidationError as exc:
        errores = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in exc.errors())
        raise DatosHipotecaError(f"La oferta no es válida: {errores}") from exc


@_ejecutar
def hipoteca_cambiar_banco(args: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    e = _Entorno(contexto)
    p = e.prestamo
    n = e.normativa
    modalidad = args.get("modalidad") or "subrogacion"
    if modalidad not in ("subrogacion", "novacion", "cancelacion_y_nueva"):
        raise DatosHipotecaError("modalidad debe ser subrogacion, novacion o cancelacion_y_nueva.")
    base = e.cuadro()
    fecha = _fecha(args.get("fecha"), "fecha") or (base.proxima(e.hoy).fecha if base.proxima(e.hoy) else e.hoy)
    capital = base.capital_pendiente(fecha)
    if capital <= 0:
        raise DatosHipotecaError("No queda capital pendiente en esa fecha.")
    restantes = [f for f in base.filas if f.fecha > fecha]
    nueva = _oferta(args, e, fecha, capital, len(restantes) * p.periodicidad_meses)
    nuevo = calcular_cuadro(nueva, e.serie, Supuestos(hoy=e.hoy))
    gastos_raw = args.get("gastos") or {}
    if not isinstance(gastos_raw, Mapping):
        raise DatosHipotecaError("gastos debe ser un objeto {tasacion, notaria, registro, …} en euros.")
    desconocidos = set(gastos_raw) - set(_GASTOS)
    if desconocidos:
        raise DatosHipotecaError(f"Conceptos de gasto no reconocidos: {', '.join(sorted(desconocidos))}.")
    gastos = {k: _decimal(v, f"gastos.{k}", ZERO) or ZERO for k, v in gastos_raw.items()}
    vinc_actual = _decimal(args.get("vinculaciones_coste_anual_actual"), "vinculaciones_coste_anual_actual", ZERO)
    if vinc_actual is None:
        vinc_actual = p.coste_bonificaciones()
    vinc_nueva = _decimal(args.get("vinculaciones_coste_anual_nueva"), "vinculaciones_coste_anual_nueva", ZERO) or ZERO

    tramo = tramo_en(p, fecha)
    periodo_fijo = n.cambio_a_fijo.periodo_fijo_minimo_anos * 12
    a_fijo = tramo == "variable" and (
        nueva.tipo == "fijo" or (nueva.tipo == "mixto" and (nueva.meses_tipo_inicial or 0) >= periodo_fijo)
    )
    notas: list[str] = []
    if modalidad == "novacion":
        comision = Comision(porcentaje=ZERO, importe=ZERO, es_maximo=False)
        if a_fijo:
            comision.fundamento.append(
                "En una novación de variable a fijo sin amortizar capital no puede cobrarse comisión por este concepto "
                f"({n.cambio_a_fijo.cita_corta()})."
            )
            comision.citas.append(n.cambio_a_fijo.fuente.boe_id)
        if nueva.plazo_meses > len(restantes) * p.periodicidad_meses:
            tope = euros(capital * n.ley_2_1994.limite_novacion_ampliacion_plazo)
            cobrada = gastos.get("comision_novacion", ZERO)
            notas.append(
                f"Ampliar el plazo en una novación: la comisión de modificación no puede superar el "
                f"{fmt_pct(n.ley_2_1994.limite_novacion_ampliacion_plazo * 100)} del capital pendiente, {fmt(tope)} € "
                f"({n.ley_2_1994.cita_corta()}, art. 10)"
                + (f"; la que indicas ({fmt(cobrada)} €) lo supera." if cobrada > tope else ".")
            )
    else:
        operacion = "cambio_a_fijo" if a_fijo else ("subrogacion" if modalidad == "subrogacion" else "amortizacion")
        comision = comision_maxima(p, n, fecha, capital, operacion)
    costes_iniciales = comision.importe + sum(gastos.values(), ZERO)

    # Month by month: what you would pay with each loan (instalments + linked products), until both end.
    fechas = sorted({f.fecha for f in restantes} | {f.fecha for f in nuevo.filas})
    actual_por_fecha = {f.fecha: f.cuota for f in restantes}
    nueva_por_fecha = {f.fecha: f.cuota for f in nuevo.filas}
    meses_por_cuota = Decimal(p.periodicidad_meses) / 12
    acumulado = -costes_iniciales
    equilibrio: date | None = None if costes_iniciales > 0 else fecha
    total_actual = ZERO
    total_nuevo = costes_iniciales
    for f in fechas:
        pago_actual = actual_por_fecha.get(f, ZERO) + (vinc_actual * meses_por_cuota if f in actual_por_fecha else ZERO)
        pago_nuevo = nueva_por_fecha.get(f, ZERO) + (vinc_nueva * meses_por_cuota if f in nueva_por_fecha else ZERO)
        total_actual += pago_actual
        total_nuevo += pago_nuevo
        acumulado += pago_actual - pago_nuevo
        if equilibrio is None and acumulado >= 0:
            equilibrio = f
    cuota_actual = restantes[0].cuota if restantes else ZERO
    cuota_nueva = nuevo.filas[0].cuota
    lineas = [_describe(p), ""]
    lineas.append(
        {
            "subrogacion": "Subrogación: otro banco paga tu deuda y se queda con el préstamo con nuevas condiciones.",
            "novacion": "Novación: cambias las condiciones con tu banco actual.",
            "cancelacion_y_nueva": "Cancelar y firmar una hipoteca nueva en otro banco.",
        }[modalidad]
    )
    lineas.append(f"Fecha supuesta: {fecha:%d/%m/%Y}; capital pendiente: {fmt(capital)} €.")
    lineas.append(f"Oferta: {_describe(nueva).replace('Hipoteca', 'préstamo', 1)}")
    lineas += ["", "Costes de cambiar:"]
    lineas.append(f"- Comisión al banco actual: {fmt(comision.importe)} €.")
    lineas += _comision_texto(comision)
    for k, v in gastos.items():
        lineas.append(f"- {k.replace('_', ' ').capitalize()}: {fmt(v)} €.")
    lineas.append(f"- Total costes iniciales: {fmt(costes_iniciales)} €.")
    lineas += ["", "Comparación:"]
    lineas.append(f"- Cuota: actual {fmt(cuota_actual)} €, nueva {fmt(cuota_nueva)} € ({fmt(cuota_nueva - cuota_actual)} €).")
    lineas.append(
        f"- Productos vinculados al año: actual {fmt(vinc_actual)} €, nueva {fmt(vinc_nueva)} €."
    )
    lineas.append(
        f"- Lo que pagarías desde hoy hasta el final (cuotas, vinculaciones y costes): actual {fmt(total_actual)} €, "
        f"nueva {fmt(total_nuevo)} €. Diferencia: {fmt(total_actual - total_nuevo)} € "
        + ("a favor del cambio." if total_actual > total_nuevo else "en contra del cambio.")
    )
    lineas.append(
        f"- Intereses que quedan: actual {fmt(sum((f.intereses for f in restantes), ZERO))} €, nueva "
        f"{fmt(nuevo.intereses_totales())} €."
    )
    if equilibrio is not None and total_actual > total_nuevo:
        lineas.append(f"- Recuperas los costes del cambio hacia el {equilibrio:%m/%Y}.")
    elif total_actual <= total_nuevo:
        lineas.append("- Con estas cifras no recuperas los costes del cambio.")
    if nuevo.fecha_fin != (restantes[-1].fecha if restantes else fecha):
        lineas.append(
            f"- Ojo: los plazos no coinciden (actual hasta {restantes[-1].fecha:%m/%Y}, nueva hasta "
            f"{nuevo.fecha_fin:%m/%Y}); alargar el plazo baja la cuota pero suele subir los intereses totales."
        )
    if base.hay_estimados() or nuevo.hay_estimados():
        lineas.append("- Las cuotas variables futuras se han estimado con el último índice publicado; pueden cambiar.")
    lineas += notas
    l2 = n.ley_2_1994
    lineas += [
        "",
        "Lo que dice la ley:",
        f"- Subrogación: el nuevo banco te da una oferta vinculante y un documento de gastos con la comisión máxima; el "
        f"banco actual puede igualarla (enervar) formalizando una novación en quince días naturales ({l2.cita_corta()}, art. 2).",
        f"- Gastos: la tasación corresponde al prestatario; notaría de la escritura de préstamo, registro y gestoría, al "
        f"prestamista ({n.gastos.cita_corta()}; la Ley 2/1994 lo aplica al documento de gastos de la subrogación).",
        f"- La escritura de subrogación y la de novación (tipo o plazo) están exentas de AJD en su modalidad gradual, y "
        f"notaría y registro se calculan con aranceles reducidos (registro con una reducción del "
        f"{fmt_pct(l2.reduccion_arancel_registral * 100)}) ({l2.cita_corta()}, arts. 7, 8 y 9).",
    ]
    if a_fijo:
        lineas.append(
            f"- Pasar de variable a fijo (o a un primer periodo fijo de al menos {n.cambio_a_fijo.periodo_fijo_minimo_anos} "
            f"años) tiene el tope de comisión del art. 23.6 Ley 5/2019, aplicable a cualquier préstamo."
        )
    lineas.append(
        "- Los costes los introduces tú desde la oferta (FEIN); no se han calculado aranceles concretos."
    )
    lineas.append(REDONDEO)
    citas = sorted({n.url(b) for b in [*comision.citas, l2.fuente.boe_id, n.gastos.fuente.boe_id]})
    return ResultadoHerramienta(
        contenido="\n".join(lineas),
        resumen=f"Cambio de banco: diferencia total {fmt(total_actual - total_nuevo)} €; costes {fmt(costes_iniciales)} €",
        citas=tuple(citas),
    )


# --- Specs ----------------------------------------------------------------------------------------

_FECHA = {"type": "string", "description": "Fecha AAAA-MM-DD."}

HERRAMIENTAS: tuple[HerramientaModulo, ...] = (
    HerramientaModulo(
        spec=ToolSpec(
            name="hipoteca_cuadro",
            description=(
                "Cuadro de amortización de la hipoteca del hogar (sistema francés, fija, variable o mixta) con los "
                "índices oficiales publicados en el BOE: capital pendiente a una fecha, tipo vigente, próximas "
                "cuotas, intereses pendientes, pagos de un año (para el módulo fiscal) y comparación con las "
                "cifras del banco."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "fecha": {**_FECHA, "description": "Fecha a la que calcular (por defecto, hoy)."},
                    "ejercicio": {"type": "integer", "description": "Año del que sumar lo pagado (por defecto, el de la fecha)."},
                    "cuotas": {"type": "integer", "minimum": 1, "maximum": 120, "description": "Cuántas cuotas futuras listar."},
                },
            },
        ),
        ejecutar=hipoteca_cuadro,
    ),
    HerramientaModulo(
        spec=ToolSpec(
            name="hipoteca_revision",
            description=(
                "Estima la próxima revisión del tipo de una hipoteca variable o mixta con el último índice publicado "
                "en el BOE (Euríbor u otro), la nueva cuota y su sensibilidad a cambios del índice."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "fecha_revision": {**_FECHA, "description": "Revisión a estimar (por defecto, la próxima)."},
                    "desplazamientos_pp": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Variaciones del índice en puntos para la sensibilidad (por defecto -1, -0,5, +0,5, +1).",
                    },
                },
            },
        ),
        ejecutar=hipoteca_revision,
    ),
    HerramientaModulo(
        spec=ToolSpec(
            name="hipoteca_amortizar",
            description=(
                "Compara amortizar anticipadamente reduciendo plazo o reduciendo cuota: intereses ahorrados, nueva "
                "cuota o fecha de fin y comisión máxima según la Ley 5/2019 (o la ley aplicable al préstamo). "
                "Opcionalmente compara, con aritmética determinista, con una rentabilidad alternativa que indique el "
                "usuario (no es asesoramiento de inversión)."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["importe"],
                "properties": {
                    "importe": {"type": "number", "exclusiveMinimum": 0, "description": "Euros a amortizar."},
                    "fecha": {**_FECHA, "description": "Fecha de la amortización (por defecto, la próxima cuota)."},
                    "rentabilidad_alternativa_pct": {
                        "type": "number",
                        "description": "Rentabilidad anual neta (%) que el usuario supone para la alternativa.",
                    },
                },
            },
        ),
        ejecutar=hipoteca_amortizar,
    ),
    HerramientaModulo(
        spec=ToolSpec(
            name="hipoteca_cambiar_banco",
            description=(
                "Calcula si compensa cambiar de banco (subrogación), novar con el banco actual o cancelar y firmar "
                "otra hipoteca con la oferta que introduce el usuario: comisión al banco actual según la ley, costes, "
                "nueva cuota, total a pagar y mes en que se recuperan los costes."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["oferta"],
                "properties": {
                    "modalidad": {"type": "string", "enum": ["subrogacion", "novacion", "cancelacion_y_nueva"]},
                    "fecha": {**_FECHA, "description": "Fecha del cambio (por defecto, la próxima cuota)."},
                    "oferta": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["tipo", "tipo_interes"],
                        "properties": {
                            "tipo": {"type": "string", "enum": ["fijo", "variable", "mixto"]},
                            "tipo_interes": {"type": "number", "description": "TIN (%) fijo o del periodo inicial."},
                            "diferencial": {"type": "number"},
                            "indice": {"type": "string", "enum": list(INDICES)},
                            "meses_tipo_inicial": {"type": "integer", "minimum": 1},
                            "revision_meses": {"type": "integer", "minimum": 1},
                            "plazo_meses": {"type": "integer", "minimum": 1, "description": "Por defecto, el que te queda."},
                        },
                    },
                    "gastos": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {k: {"type": "number", "minimum": 0} for k in _GASTOS},
                        "description": "Gastos que pagarías tú, en euros (de la FEIN u oferta).",
                    },
                    "vinculaciones_coste_anual_actual": {"type": "number", "minimum": 0},
                    "vinculaciones_coste_anual_nueva": {"type": "number", "minimum": 0},
                },
            },
        ),
        ejecutar=hipoteca_cambiar_banco,
    ),
)
