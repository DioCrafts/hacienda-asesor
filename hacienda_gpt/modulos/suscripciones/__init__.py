"""Module ``suscripciones``: recurring charges detected in the bank statements the user exports.

Reads AEB Norma 43, CSV/XLSX, OFX/QFX and camt.053 statements in memory, detects subscriptions,
insurance, utilities, loans… with their periodicity, price changes, duplicates and trials, stores only the
derived list of services, warns before renewals and drafts cancellation letters with their legal basis
read from the BOE. No bank credentials and no open-banking connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from hacienda_gpt.modulos.contrato import AvisoModulo, ContextoModulo, FuenteModulo, HechoModulo, ModuloBase
from hacienda_gpt.modulos.suscripciones.almacen import NOMBRE_HECHO, validar_servicios
from hacienda_gpt.modulos.suscripciones.avisos import generar_avisos
from hacienda_gpt.modulos.suscripciones.derechos import cargar
from hacienda_gpt.modulos.suscripciones.herramientas import HERRAMIENTAS

INSTRUCCIONES = """\
Eres el especialista en suscripciones y cargos recurrentes del hogar. Trabajas SOLO con extractos que el \
usuario exporta de su banco (Norma 43, CSV, Excel XLSX, OFX/QFX o camt.053): nunca pidas claves ni acceso a \
la banca online. Para detectar servicios usa suscripciones_analizar_extracto con el fichero tal cual; para \
consultar lo ya detectado, suscripciones_listado y suscripciones_gasto_anual; para darse de baja, \
suscripciones_preparar_baja, que solo prepara un borrador: nunca digas que has enviado o cancelado nada. \
Las cifras, fechas y plazos salen de las herramientas; los derechos, de sus citas literales del BOE. Las \
fechas de próximo cargo son estimaciones: pide al usuario que confirme en su contrato las fechas de \
renovación. No sabes si el usuario usa un servicio: pregúntalo en vez de suponerlo. Si una herramienta dice \
que hay columnas ambiguas o importes sin signo, pregunta al usuario. No repitas en la conversación números \
de cuenta ni movimientos que no sean cargos recurrentes."""


@dataclass
class ModuloSuscripciones(ModuloBase):
    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        return generar_avisos(contexto)


def _fuentes() -> tuple[FuenteModulo, ...]:
    datos = cargar()
    return tuple(
        FuenteModulo(identificador=fp.boe_id, descripcion=fp.norma, url=fp.url)
        for fp in datos.fuentes_principales.values()
    )


MODULO = ModuloSuscripciones(
    clave="suscripciones",
    nombre="Suscripciones y recibos",
    descripcion=(
        "Detecta en los extractos bancarios que exporta el usuario los cargos recurrentes (suscripciones, "
        "seguros, luz, agua, telefonía, préstamos, gimnasio…), su periodicidad y próximo cargo, subidas de "
        "precio, cargos duplicados y pruebas gratuitas convertidas en pago; calcula el gasto anual por categoría, "
        "avisa antes de las renovaciones y prepara cartas de baja con su fundamento legal (TRLGDCU, Ley de "
        "Contrato de Seguro, Ley General de Telecomunicaciones, Ley 10/2025, servicios de pago)."
    ),
    instrucciones=INSTRUCCIONES,
    hechos=(
        HechoModulo(
            nombre=NOMBRE_HECHO,
            tipo="json",
            etiqueta="Servicios recurrentes detectados",
            descripcion=(
                "Lista derivada de los extractos analizados: por servicio, comercio, categoría, importe, "
                "periodicidad, fechas del primer, último y próximo cargo, estado y los cambios de precio, "
                "duplicados y pruebas detectados. Nunca contiene movimientos, saldos ni números de cuenta."
            ),
            validar=validar_servicios,
            sensible=True,
        ),
    ),
    herramientas=HERRAMIENTAS,
    fuentes=_fuentes(),
)

__all__ = ["MODULO"]
