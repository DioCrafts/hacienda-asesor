"""The ``caducidades`` module object: facts, tools, sources and the daily proactive job."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hacienda_gpt.modulos.caducidades.agenda import avisos_de_agenda
from hacienda_gpt.modulos.caducidades.hechos import (
    HECHO_ANTELACIONES,
    HECHO_COMPRAS,
    HECHO_CONTRATOS,
    HECHO_DOCUMENTOS,
    HECHO_FECHAS_NACIMIENTO,
    HECHO_VEHICULOS,
    validar_antelaciones,
    validar_compras,
    validar_contratos,
    validar_documentos,
    validar_fechas_nacimiento,
    validar_vehiculos,
)
from hacienda_gpt.modulos.caducidades.herramientas import HERRAMIENTAS
from hacienda_gpt.modulos.caducidades.reglas import cargar_reglas
from hacienda_gpt.modulos.contrato import AvisoModulo, ContextoModulo, FuenteModulo, HechoModulo, ModuloBase

HECHOS: tuple[HechoModulo, ...] = (
    HechoModulo(
        nombre=HECHO_DOCUMENTOS,
        tipo="json",
        etiqueta="Documentos del hogar",
        descripcion=(
            "Lista de documentos con fecha de caducidad: tipo (dni, pasaporte, permiso_conduccion, "
            "licencia_conduccion, certificado_electronico, otro), titular (titular, cónyuge, hijo N), "
            "fecha_expedicion o de la última renovación, fecha_caducidad impresa, clases del permiso. Sin números "
            'de documento. Ejemplo: [{"tipo": "dni", "titular": "titular", "fecha_caducidad": "2031-04-03"}].'
        ),
        validar=validar_documentos,
        sensible=True,
    ),
    HechoModulo(
        nombre=HECHO_FECHAS_NACIMIENTO,
        tipo="json",
        etiqueta="Fechas de nacimiento de los titulares",
        descripcion=(
            "Fechas de nacimiento para aplicar las reglas por edad (DNI, pasaporte, permiso): "
            '{"titular": "1980-05-02", "cónyuge": "1982-01-15"}. Para los hijos se usa también la fecha de '
            "nacimiento de «descendientes» si consta."
        ),
        validar=validar_fechas_nacimiento,
        sensible=True,
    ),
    HechoModulo(
        nombre=HECHO_VEHICULOS,
        tipo="json",
        etiqueta="Vehículos (ITV)",
        descripcion=(
            "Lista de vehículos: etiqueta (p. ej. «coche familiar»; no la matrícula), categoria (M1, N1, L, L1e…), "
            "fecha_primera_matriculacion y, si se conocen, fecha_ultima_itv, fecha_caducidad_itv_anterior y "
            "fecha_proxima_itv (la de la tarjeta ITV)."
        ),
        validar=validar_vehiculos,
    ),
    HechoModulo(
        nombre=HECHO_CONTRATOS,
        tipo="json",
        etiqueta="Seguros y contratos con renovación",
        descripcion=(
            "Lista de seguros y contratos: tipo (seguro, seguro_vida, otro), etiqueta, fecha_vencimiento del "
            "período en curso, periodicidad_meses (12 por defecto en seguros) y, para «otro», preaviso_dias de "
            "su contrato."
        ),
        validar=validar_contratos,
    ),
    HechoModulo(
        nombre=HECHO_COMPRAS,
        tipo="json",
        etiqueta="Compras con garantía",
        descripcion=(
            "Lista de compras: descripcion, fecha_compra, fecha_entrega, tipo_bien (bien, digital, segunda_mano), "
            "modalidad (tienda, distancia, fuera_establecimiento…), garantia_comercial_hasta, establecimiento, importe."
        ),
        validar=validar_compras,
        sensible=True,
    ),
    HechoModulo(
        nombre=HECHO_ANTELACIONES,
        tipo="json",
        etiqueta="Antelación de los avisos de caducidad",
        descripcion=(
            'Días de antelación del primer aviso por tipo, p. ej. {"itv": 45, "pasaporte": 120}. Por defecto: DNI 60, '
            "pasaporte 90, permiso 90, ITV 30, certificado 30, seguros y contratos 30, devolución 7, garantía 30."
        ),
        validar=validar_antelaciones,
    ),
)

INSTRUCCIONES = """\
Eres el especialista en caducidades y garantías del hogar. Respondes con las herramientas caducidades_*; nunca \
calculas una fecha de memoria. Reglas:
- Las fechas salen de las reglas leídas del BOE (DNI: RD 1553/2005; pasaporte: RD 896/2003; permiso: Reglamento \
General de Conductores; ITV: RD 920/2017; seguros: Ley 50/1980; garantía y desistimiento: TRLGDCU) o de la fecha \
impresa en el documento, que siempre prevalece. Cita la regla y su artículo.
- Para dar de alta un documento pide solo el tipo, de quién es (titular, cónyuge, hijo N) y las fechas; NO pidas ni \
guardes números de documento, matrículas ni otros identificadores.
- Para una compra, pide la fecha de compra y de entrega, si fue en tienda o a distancia y si es un bien nuevo, de \
segunda mano o contenido digital. Si el usuario pega un tique, usa caducidades_leer_ticket con los datos que \
localices y su fragmento literal, confirma con él y después caducidades_registrar_compra.
- Indica siempre qué hay que hacer, dónde y con cuánta antelación, y los avisos de lo que no se modela.
"""


@dataclass
class ModuloCaducidades(ModuloBase):
    # The core's document hook: an uploaded receipt proposes a purchase, which the user confirms and then
    # registers with this tool.
    herramienta_de_documento = "caducidades_registrar_compra"

    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        return avisos_de_agenda(contexto.perfil, contexto.hoy)

    def leer_documento(self, provider: Any, *, filename: str, data: bytes) -> Any:
        """Propose a purchase from an uploaded receipt (see :func:`ticket.leer_ticket`)."""
        from hacienda_gpt.modulos.caducidades.ticket import leer_ticket

        return leer_ticket(provider, filename=filename, data=data)


def _fuentes() -> tuple[FuenteModulo, ...]:
    reglas = cargar_reglas()
    return tuple(
        FuenteModulo(identificador=fp.boe_id, descripcion=fp.norma, url=fp.url)
        for fp in reglas.fuentes_principales.values()
    )


MODULO = ModuloCaducidades(
    clave="caducidades",
    nombre="Caducidades y garantías",
    descripcion=(
        "Avisa de la caducidad del DNI, pasaporte, permiso de conducir y certificado electrónico, de la próxima "
        "ITV, de la renovación de seguros y contratos, y de los plazos de devolución y garantía de las compras; "
        "calcula cada fecha con la regla legal y dice qué trámite hacer y dónde."
    ),
    instrucciones=INSTRUCCIONES,
    hechos=HECHOS,
    herramientas=HERRAMIENTAS,
    fuentes=_fuentes(),
)
