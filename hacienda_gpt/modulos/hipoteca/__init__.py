"""Mortgage module ("hipoteca"): schedule, rate reviews with the official indices published in the BOE,
early repayment (term vs instalment) with the fees of Ley 5/2019, and change of bank.

See docs/modulos/hipoteca.md. ``MODULO`` is what the core discovers (hacienda_gpt/modulos/contrato.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from hacienda_gpt.modulos.contrato import AvisoModulo, ContextoModulo, FuenteModulo, HechoModulo, ModuloBase
from hacienda_gpt.modulos.hipoteca.avisos import avisos_hipoteca
from hacienda_gpt.modulos.hipoteca.herramientas import HERRAMIENTAS, pagos_del_ejercicio
from hacienda_gpt.modulos.hipoteca.modelo import validar_prestamo
from hacienda_gpt.modulos.hipoteca.normativa import cargar_normativa

__all__ = ["MODULO", "pagos_del_ejercicio"]


def _validar_umbral(valor: object) -> float:
    try:
        numero = float(str(valor).replace(",", "."))
    except ValueError as exc:
        raise ValueError("El umbral de aviso debe ser un importe en euros.") from exc
    if not 0 <= numero <= 10000:
        raise ValueError("El umbral de aviso debe estar entre 0 y 10.000 euros.")
    return numero


HECHOS = (
    HechoModulo(
        nombre="hipoteca_prestamo",
        tipo="json",
        etiqueta="Datos de la hipoteca",
        descripcion=(
            "Préstamo hipotecario del hogar, de la escritura y los recibos: capital_inicial, fecha_firma "
            "(AAAA-MM-DD), plazo_meses, tipo (fijo/variable/mixto), tipo_inicial (% TIN del primer periodo o "
            "el fijo), meses_tipo_inicial (variable: hasta la primera revisión; mixto: tramo fijo), indice "
            "(euribor_12m…), diferencial, revision_meses, indice_regla/indice_meses_antes (qué valor del índice "
            "usa cada revisión), periodicidad_meses, fecha_primera_cuota, bonificaciones [{concepto, "
            "reduccion_diferencial, coste_anual, cumplida}], revisiones_conocidas [{fecha, tipo}] (tipos que ya "
            "aplicó el banco), amortizaciones_realizadas [{fecha, importe, modalidad plazo|cuota}], "
            "comisiones_pactadas [{porcentaje, desde_anos, hasta_anos, tramo}] ([] si no hay comisión), "
            "tipo_minimo/tipo_maximo, redondeo_tipo_multiplo, vivienda_habitual, adquisicion_antes_2013 y "
            "comprobacion_banco [{fecha, cuota, capital_pendiente}] con cifras de un recibo para contrastar."
        ),
        validar=validar_prestamo,
        sensible=True,
    ),
    HechoModulo(
        nombre="hipoteca_umbral_aviso",
        tipo="number",
        etiqueta="Umbral de aviso de cambio de cuota (€)",
        descripcion="Cambio de la cuota estimada (en euros al mes) a partir del cual avisar cuando se publica un índice.",
        validar=_validar_umbral,
    ),
)

_N = cargar_normativa()

FUENTES = tuple(
    FuenteModulo(identificador=boe_id, descripcion=fp.norma, url=fp.url)
    for boe_id, fp in _N.fuentes_principales.items()
) + (
    FuenteModulo(
        identificador="bde_tipos_referencia_boe",
        descripcion=(
            "Resoluciones mensuales del Banco de España en el BOE con los tipos de interés oficiales de referencia "
            "del mercado hipotecario (Euríbor, IRPH, €STR…); serie en data/indices_referencia.json"
        ),
        url="https://www.boe.es/buscar/boe.php",
    ),
)

DESCRIPCION = (
    "Hipoteca del hogar: cuadro de amortización y capital pendiente (fija, variable o mixta, sistema francés), "
    "próxima revisión del tipo con el Euríbor u otro índice oficial publicado en el BOE, amortizar anticipadamente "
    "(reducir plazo o cuota, comisiones de la Ley 5/2019) y cambiar de banco (subrogación o novación) con sus costes."
)

INSTRUCCIONES = (
    "Eres el especialista en la hipoteca del hogar. No calcules nada de memoria: usa siempre las herramientas "
    "hipoteca_cuadro, hipoteca_revision, hipoteca_amortizar e hipoteca_cambiar_banco, que leen el dato "
    "hipoteca_prestamo y los índices oficiales publicados en el BOE. Si falta el dato, pide al usuario los datos de "
    "su escritura y de un recibo reciente (capital, fecha de firma, plazo, tipo, índice, diferencial, revisiones) y, "
    "para que el cálculo cuadre con su banco, los tipos que le aplicó en cada revisión y la cuota y el capital "
    "pendiente de un recibo. Explica que los resultados pueden diferir en céntimos del banco por el redondeo. Las "
    "comisiones que calculas son máximos legales. Al comparar amortizar con invertir, usa solo la rentabilidad que "
    "dé el usuario, sin recomendar productos. La deducción por vivienda habitual (compras anteriores a 2013) es del "
    "módulo fiscal: menciónala y deriva el cálculo. No contrates, no envíes nada al banco: como mucho prepara un "
    "borrador de solicitud para que el usuario lo envíe."
)


@dataclass
class ModuloHipoteca(ModuloBase):
    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        return avisos_hipoteca(contexto)


MODULO = ModuloHipoteca(
    clave="hipoteca",
    nombre="Hipoteca",
    descripcion=DESCRIPCION,
    instrucciones=INSTRUCCIONES,
    hechos=HECHOS,
    herramientas=HERRAMIENTAS,
    fuentes=FUENTES,
)
