"""The ``luz`` module: facts, tools, sources and the proactive job (see ``contrato.py``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Any

from hacienda_gpt.modulos.contrato import (
    AvisoModulo,
    ContextoModulo,
    FuenteModulo,
    HechoModulo,
    HerramientaModulo,
)
from hacienda_gpt.modulos.luz.analisis import comparar
from hacienda_gpt.modulos.luz.consumo import cargar_consumo, validar_consumo, zona_valida
from hacienda_gpt.modulos.luz.datos import DatoNoDisponibleError, cargar_regulados
from hacienda_gpt.modulos.luz.factura import Contrato, FacturaError, euros, fmt
from hacienda_gpt.modulos.luz.herramientas import (
    HECHO_CONSUMO,
    HECHO_CUPS,
    HECHO_OFERTAS,
    HECHO_P1,
    HECHO_P2,
    HECHO_TARIFA,
    HECHO_ZONA,
    herramientas,
)
from hacienda_gpt.modulos.luz.ofertas import cargar_oferta, cargar_ofertas, validar_oferta, validar_ofertas

UMBRAL_ENV = "LUZ_UMBRAL_AHORRO_EUR"
UMBRAL_DEFECTO = Decimal(50)
_CUPS = re.compile(r"^ES\d{16}[A-Z]{2}(\d[A-Z])?$")


def validar_cups(valor: Any) -> str:
    texto = re.sub(r"\s+", "", str(valor)).upper()
    if not _CUPS.match(texto):
        raise ValueError("El CUPS tiene la forma ES + 16 cifras + 2 letras (a veces dos caracteres más, p. ej. 0F).")
    return texto


def validar_potencia(valor: Any) -> float:
    try:
        kw = Decimal(str(valor).replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("La potencia es un número de kW (p. ej. 4,6).") from exc
    if not Decimal(0) < kw <= 15:
        raise ValueError("En la tarifa 2.0TD la potencia es mayor que 0 y como mucho 15 kW.")
    return float(kw)


def umbral_ahorro() -> Decimal:
    try:
        return Decimal(os.environ.get(UMBRAL_ENV, str(UMBRAL_DEFECTO)))
    except InvalidOperation:
        return UMBRAL_DEFECTO


HECHOS: tuple[HechoModulo, ...] = (
    HechoModulo(
        HECHO_CUPS,
        "string",
        "CUPS del suministro",
        "Código del punto de suministro (ES + 20-22 caracteres). Identifica la vivienda ante la distribuidora.",
        validar_cups,
        sensible=True,
    ),
    HechoModulo(
        HECHO_P1, "number", "Potencia contratada P1 (kW)", "Potencia punta-llano de la factura.", validar_potencia
    ),
    HechoModulo(HECHO_P2, "number", "Potencia contratada P2 (kW)", "Potencia valle de la factura.", validar_potencia),
    HechoModulo(
        HECHO_ZONA,
        "string",
        "Sistema eléctrico",
        "peninsula, baleares, canarias, ceuta o melilla (por defecto se deduce de la comunidad autónoma).",
        zona_valida,
    ),
    HechoModulo(
        HECHO_TARIFA,
        "json",
        "Tarifa de luz actual",
        "Precios de tu contrato: energía (€/kWh por periodo o único), potencia (€/kW·día), descuentos, permanencia.",
        validar_oferta,
        sensible=True,
    ),
    HechoModulo(
        HECHO_OFERTAS,
        "json",
        "Ofertas de luz a comparar",
        "Lista de ofertas con el mismo formato que la tarifa actual.",
        validar_ofertas,
    ),
    HechoModulo(
        HECHO_CONSUMO,
        "json",
        "Consumo eléctrico",
        "Resumen del consumo importado (diario por periodos, horario reciente, perfil mensual, maxímetro).",
        validar_consumo,
        sensible=True,
    ),
)

INSTRUCCIONES = """Eres el especialista en la factura de la luz de una vivienda (peaje 2.0TD, hasta 15 kW).
- No calcules tú: usa las herramientas luz_*. Cada importe sale de una norma del BOE leída y citada (peajes de la
  CNMC, cargos del Ministerio, Circular 3/2020, Ley 38/1992, Ley 37/1992, RD 216/2014) o del contrato del usuario.
- Para reproducir una factura pide: fechas del periodo, potencias, precios exactos de su contrato (con todos los
  decimales) y los kWh de P1/P2/P3 o el CSV horario de Datadis; pasa total_factura_real para compararla al céntimo.
- Los precios de una comercializadora libre son datos del usuario: guárdalos en luz_tarifa_actual / luz_ofertas.
- PVPC y horas baratas necesitan los precios de REE (token LUZ_ESIOS_TOKEN o JSON del indicador 1001 pegado).
- En 2026 hubo rebajas temporales de IVA (10 %) e impuesto eléctrico (0,5 %), algunas condicionadas al IPC: si
  la herramienta avisa de un mes condicional, pregunta qué tipo aplicó la factura.
- Repite siempre los avisos de la herramienta. No contrates ni cambies nada por el usuario."""


@dataclass
class ModuloLuz:
    clave: str = "luz"
    nombre: str = "Luz"
    descripcion: str = (
        "Factura de la luz del hogar (2.0TD): reproduce una factura al céntimo con su consumo horario y su contrato, "
        "compara cuánto habría pagado con otras tarifas o con el PVPC, avisa si hay una oferta más barata, dice "
        "qué horas son más baratas y qué potencia contratada necesita."
    )
    instrucciones: str = INSTRUCCIONES
    hechos: tuple[HechoModulo, ...] = HECHOS
    herramientas: list[HerramientaModulo] = field(default_factory=herramientas)
    fuentes: tuple[FuenteModulo, ...] = field(default_factory=lambda: _fuentes())

    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        """Deterministic and offline: never calls REE (PVPC is left to the comparison tool)."""
        perfil = contexto.perfil
        salida: list[AvisoModulo] = []
        try:
            actual = cargar_oferta(perfil.get(HECHO_TARIFA)) if perfil.get(HECHO_TARIFA) else None
            ofertas = [o for o in cargar_ofertas(perfil.get(HECHO_OFERTAS)) if o.tipo == "fijo"]
            consumo = cargar_consumo(perfil.get(HECHO_CONSUMO))
        except ValueError:
            return []
        if actual is not None:
            for fecha, que in ((actual.precio_valido_hasta, "precio"), (actual.permanencia_hasta, "permanencia")):
                if fecha is not None and contexto.hoy <= fecha <= contexto.hoy + timedelta(days=45):
                    salida.append(
                        AvisoModulo(
                            clave=f"luz:fin_{que}:{fecha.isoformat()}",
                            titulo=f"Tu {'precio de la luz' if que == 'precio' else 'permanencia'} vence el "
                            f"{fecha:%d-%m-%Y}",
                            cuerpo=(
                                f"La tarifa «{actual.nombre}» tiene {'el precio garantizado' if que == 'precio' else 'permanencia'} "
                                f"hasta el {fecha:%d-%m-%Y}. Es buen momento para comparar ofertas con tu consumo real."
                            ),
                            gravedad="aviso",
                            acciones=("luz_comparar_tarifas",),
                            vence=fecha,
                        )
                    )
        if perfil.get(HECHO_TARIFA) or perfil.get(HECHO_CONSUMO):
            try:
                cargar_regulados().peaje(contexto.hoy)
            except DatoNoDisponibleError:
                salida.append(
                    AvisoModulo(
                        clave=f"luz:datos_regulados:{contexto.hoy.year}",
                        titulo=f"Faltan los peajes y cargos de {contexto.hoy.year}",
                        cuerpo=(
                            "Los cálculos de la luz de este año no están disponibles hasta cargar los nuevos peajes "
                            "(CNMC) y cargos (Ministerio) publicados en el BOE."
                        ),
                        gravedad="info",
                    )
                )
        if actual is None or not ofertas or consumo is None or perfil.get(HECHO_P1) in (None, ""):
            return salida
        try:
            p1 = Decimal(str(perfil[HECHO_P1]))
            p2 = Decimal(str(perfil.get(HECHO_P2) or perfil[HECHO_P1]))
            zona = zona_valida(perfil.get(HECHO_ZONA) or "peninsula")
            resultado = comparar(Contrato(p1, p2, zona), consumo, ofertas, actual)
        except (FacturaError, DatoNoDisponibleError, ValueError):
            return salida
        act, mejor = resultado.actual(), resultado.mejor()
        if act is None or mejor is None or mejor.es_actual or resultado.dias < 90:
            return salida
        ahorro = euros((act.total - mejor.total) * 365 / resultado.dias)
        if ahorro <= umbral_ahorro():
            return salida
        permanencia = ""
        if actual.permanencia_hasta and actual.permanencia_hasta > contexto.hoy:
            permanencia = f" Ojo: tu contrato tiene permanencia hasta el {actual.permanencia_hasta:%d-%m-%Y}"
            if actual.penalizacion_eur:
                permanencia += f" (penalización {fmt(actual.penalizacion_eur)} €)"
            permanencia += "."
        salida.append(
            AvisoModulo(
                clave=f"luz:ahorro:{mejor.oferta.clave()}:{resultado.hasta.isoformat()}",
                titulo=f"Con «{mejor.oferta.nombre}» pagarías unos {fmt(ahorro)} € menos al año",
                cuerpo=(
                    f"Con tu consumo del {resultado.desde:%d-%m-%Y} al {resultado.hasta:%d-%m-%Y} "
                    f"({resultado.dias} días), «{mejor.oferta.nombre}» habría costado {fmt(mejor.total)} € y tu tarifa "
                    f"«{act.oferta.nombre}» {fmt(act.total)} €, impuestos incluidos.{permanencia} "
                    "Compruébalo con luz_comparar_tarifas antes de cambiar."
                ),
                gravedad="aviso",
                acciones=("luz_comparar_tarifas",),
                fuentes=cargar_regulados().citas("BOE-A-2025-26348", "BOE-A-2025-26705"),
                importe=ahorro,
                sustituye_prefijo="luz:ahorro:",
            )
        )
        return salida


def _fuentes() -> tuple[FuenteModulo, ...]:
    datos = cargar_regulados()
    propias = tuple(
        FuenteModulo(identificador=f.boe_id, descripcion=f.norma, url=f.url) for f in datos.fuentes_principales.values()
    )
    return propias + (
        FuenteModulo(
            identificador="ESIOS-1001",
            descripcion="Precio horario del PVPC 2.0TD de Red Eléctrica (API ESIOS, indicador 1001); requiere token.",
            url="https://api.esios.ree.es/indicators/1001",
        ),
        FuenteModulo(
            identificador="DATADIS-CSV",
            descripcion="Exportación CSV de consumos horarios de Datadis (plataforma de las distribuidoras).",
            url="https://datadis.es",
        ),
    )


MODULO = ModuloLuz()
