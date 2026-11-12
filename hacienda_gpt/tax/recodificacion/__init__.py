"""Agentic re-encoding of legal parameters: a model proposes data, deterministic checks decide.

See :mod:`hacienda_gpt.tax.recodificacion.nucleo` for the loop and its guarantees,
:mod:`~hacienda_gpt.tax.recodificacion.control` for the deterministic gate and
:mod:`~hacienda_gpt.tax.recodificacion.adaptadores` for the kinds of parameter group it maintains.
"""

from __future__ import annotations

from hacienda_gpt.tax.recodificacion.adaptadores import (
    Hallazgo,
    objetivo_de_ruta,
    objetivos_de_articulo,
    objetivos_de_rollover,
    objetivos_de_verificacion,
)
from hacienda_gpt.tax.recodificacion.control import Control
from hacienda_gpt.tax.recodificacion.nucleo import (
    Adaptador,
    AlmacenPropuestas,
    ControlFallidoError,
    DatosCambiadosError,
    Informe,
    LeyCambiadaError,
    Objetivo,
    Pendiente,
    Propuesta,
    RecodificacionError,
    SinTextoError,
    adaptador_de,
    adaptadores_registrados,
    aplicar_propuesta,
    avisar_operador,
    confirmar,
    ejecutar,
    pendientes_de_confirmar,
    rechazar_propuesta,
    recodificar,
    registrar,
)

__all__ = [
    "Adaptador",
    "AlmacenPropuestas",
    "Control",
    "ControlFallidoError",
    "DatosCambiadosError",
    "Hallazgo",
    "Informe",
    "LeyCambiadaError",
    "Objetivo",
    "Pendiente",
    "Propuesta",
    "RecodificacionError",
    "SinTextoError",
    "adaptador_de",
    "adaptadores_registrados",
    "aplicar_propuesta",
    "avisar_operador",
    "confirmar",
    "ejecutar",
    "objetivo_de_ruta",
    "objetivos_de_articulo",
    "objetivos_de_rollover",
    "objetivos_de_verificacion",
    "pendientes_de_confirmar",
    "rechazar_propuesta",
    "recodificar",
    "registrar",
]
