"""Proactive notices of the mortgage module (deterministic and idempotent: stable keys).

- ``hipoteca:revision:<fecha>:<BOE id>``: a rate review is coming (within ``HIPOTECA_DIAS_AVISO_REVISION``
  days, default 45) with the estimated instalment; replaced (``sustituye_prefijo``) when a newer index
  changes the estimate.
- ``hipoteca:indice:<BOE id>``: a newly published index value moves the estimated instalment of the next
  review by at least the household's threshold (fact ``hipoteca_umbral_aviso``, else
  ``HIPOTECA_UMBRAL_AVISO_EUR``, default 20 €) compared with the previous month's value.
- ``hipoteca:comision:<fecha>``: within 60 days the early-repayment fee cap drops (e.g. five years after
  signing a variable loan it disappears).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import os

from hacienda_gpt.modulos.contrato import AvisoModulo, ContextoModulo
from hacienda_gpt.modulos.hipoteca.calculo import (
    cuota_francesa,
    fechas_revision,
    fmt,
    fmt_pct,
    sumar_meses,
    tipo_revisado,
)
from hacienda_gpt.modulos.hipoteca.comisiones import comision_maxima
from hacienda_gpt.modulos.hipoteca.herramientas import DatosHipotecaError, _Entorno, estimar_revision
from hacienda_gpt.modulos.hipoteca.indices import SerieIndices
from hacienda_gpt.modulos.hipoteca.normativa import Normativa

DIAS_AVISO_REVISION = 45
UMBRAL_EUR = Decimal(20)
DIAS_AVISO_COMISION = 60
# A new index value is "new" for this many days after its publication.
DIAS_NOVEDAD_INDICE = 35


def _dias_aviso() -> int:
    try:
        return max(1, int(os.environ.get("HIPOTECA_DIAS_AVISO_REVISION", DIAS_AVISO_REVISION)))
    except ValueError:
        return DIAS_AVISO_REVISION


def _umbral(contexto: ContextoModulo) -> Decimal:
    for valor in (contexto.perfil.get("hipoteca_umbral_aviso"), os.environ.get("HIPOTECA_UMBRAL_AVISO_EUR")):
        if valor in (None, ""):
            continue
        try:
            umbral = Decimal(str(valor).replace(",", "."))
        except InvalidOperation:
            continue
        if umbral.is_finite() and umbral >= 0:
            return umbral
    return UMBRAL_EUR


def avisos_hipoteca(
    contexto: ContextoModulo, serie: SerieIndices | None = None, normativa: Normativa | None = None
) -> list[AvisoModulo]:
    if contexto.perfil.get("hipoteca_prestamo") is None:
        return []
    try:
        e = _Entorno(contexto, serie, normativa)
    except DatosHipotecaError:
        return []
    avisos: list[AvisoModulo] = []
    p = e.prestamo
    hoy = e.hoy
    if p.tipo != "fijo":
        futuras = [r for r in fechas_revision(p) if r > hoy]
        if futuras:
            cuadro = e.cuadro()
            proxima = futuras[0]
            try:
                est = estimar_revision(e, proxima, cuadro)
            except DatosHipotecaError:
                est = None
            if est is not None:
                dato = est["dato"]
                dif = est["cuota"] - est["cuota_antes"]
                umbral = _umbral(contexto)
                if (proxima - hoy).days <= _dias_aviso():
                    estado = (
                        "estimada con el último índice publicado" if est["proyeccion"] else "con el índice ya publicado"
                    )
                    avisos.append(
                        AvisoModulo(
                            clave=f"hipoteca:revision:{proxima.isoformat()}:{dato.boe_id}",
                            titulo=f"Revisión de tu hipoteca el {proxima:%d/%m/%Y}",
                            cuerpo=(
                                f"El {proxima:%d/%m/%Y} se revisa el tipo de tu hipoteca. Nueva cuota {estado}: "
                                f"{fmt(est['cuota'])} € ({'+' if dif >= 0 else ''}{fmt(dif)} € al mes), con un tipo del "
                                f"{fmt_pct(est['tipo'])}. Índice: {dato.cita}."
                            ),
                            gravedad="aviso" if dif >= umbral else "info",
                            acciones=("Ver la revisión (hipoteca_revision)",),
                            fuentes=(e.url(dato.boe_id),),
                            importe=est["cuota"],
                            vence=proxima,
                            sustituye_prefijo=f"hipoteca:revision:{proxima.isoformat()}:",
                        )
                    )
                anterior = e.serie.anterior(dato)
                if (
                    est["proyeccion"]
                    and anterior is not None
                    and timedelta(0) <= hoy - dato.publicado <= timedelta(days=DIAS_NOVEDAD_INDICE)
                ):
                    capital, restantes = est["capital"], est["restantes"]
                    tipo_antes, _ = tipo_revisado(p, anterior.valor)
                    cuota_antes = cuota_francesa(capital, tipo_antes, restantes, p.periodicidad_meses)
                    cambio = est["cuota"] - cuota_antes
                    if abs(cambio) >= umbral and umbral > 0:
                        avisos.append(
                            AvisoModulo(
                                clave=f"hipoteca:indice:{dato.boe_id}",
                                titulo="Nuevo índice publicado: cambia la estimación de tu próxima cuota",
                                cuerpo=(
                                    f"Se ha publicado {dato.cita}; el mes anterior fue {anterior.literal} %. Con este "
                                    f"valor, la cuota estimada tras la revisión del {proxima:%d/%m/%Y} sería "
                                    f"{fmt(est['cuota'])} € ({'+' if cambio >= 0 else ''}{fmt(cambio)} € respecto a la "
                                    f"estimación con el índice anterior)."
                                ),
                                gravedad="aviso" if cambio > 0 else "info",
                                acciones=("Ver la revisión (hipoteca_revision)",),
                                fuentes=(e.url(dato.boe_id),),
                                importe=est["cuota"],
                                vence=proxima,
                            )
                        )
    avisos += _aviso_comision(e)
    return avisos


def _aviso_comision(e: _Entorno) -> list[AvisoModulo]:
    """Notice when the fee cap is about to drop (a year boundary of the law within DIAS_AVISO_COMISION days)."""
    p, n, hoy = e.prestamo, e.normativa, e.hoy
    try:
        capital = e.cuadro().capital_pendiente(hoy)
    except (ValueError, ArithmeticError):
        return []
    if capital <= 0:
        return []
    ahora = comision_maxima(p, n, hoy, capital)
    if ahora.porcentaje <= 0:
        return []
    cortes = sorted(
        {
            sumar_meses(p.fecha_firma, int(anos) * 12)
            for anos in (
                n.comision_variable.opcion_b_anos,
                n.comision_variable.opcion_a_anos,
                n.comision_fijo.anos_iniciales,
                n.ley_41_2007.desistimiento_anos,
                n.cambio_a_fijo.anos,
            )
        }
        | ({sumar_meses(p.fecha_firma, p.meses_tipo_inicial)} if p.tipo == "mixto" and p.meses_tipo_inicial else set())
    )
    for corte in cortes:
        if not hoy < corte <= hoy + timedelta(days=DIAS_AVISO_COMISION):
            continue
        despues = comision_maxima(p, n, corte, capital)
        if despues.porcentaje < ahora.porcentaje:
            return [
                AvisoModulo(
                    clave=f"hipoteca:comision:{corte.isoformat()}",
                    titulo="Baja la comisión por amortizar tu hipoteca",
                    cuerpo=(
                        f"A partir del {corte:%d/%m/%Y} la comisión máxima por amortización anticipada pasa del "
                        f"{fmt_pct(ahora.porcentaje * 100)} al {fmt_pct(despues.porcentaje * 100)} del importe "
                        f"amortizado. Si pensabas amortizar, esperar a esa fecha puede ahorrarte hasta "
                        f"{fmt(ahora.importe - despues.importe)} € si cancelaras todo. " + " ".join(despues.fundamento)
                    ),
                    gravedad="info",
                    acciones=("Simular la amortización (hipoteca_amortizar)",),
                    fuentes=tuple(n.url(b) for b in despues.citas),
                    vence=corte,
                )
            ]
    return []
