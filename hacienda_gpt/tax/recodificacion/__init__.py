"""Agentic re-encoding of legal parameters: a model proposes data, deterministic checks decide.

See :mod:`hacienda_gpt.tax.recodificacion.nucleo` for the loop and its guarantees,
:mod:`~hacienda_gpt.tax.recodificacion.control` for the deterministic gate and
:mod:`~hacienda_gpt.tax.recodificacion.adaptadores` for the kinds of parameter group it maintains.
"""

from __future__ import annotations

import importlib

from hacienda_gpt.tax.recodificacion.adaptadores import (
    Hallazgo,
    control_de_lo_publicado,
    objetivo_de_ruta,
    objetivos_de_articulo,
    objetivos_de_desglose,
    objetivos_de_rollover,
    objetivos_de_verificacion,
    todos_los_objetivos,
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

try:
    # The adapters of the other parameter files (self-employed, companies, wealth, inheritance) register themselves
    # on import. The module is optional: without it only the IRPF file is maintained.
    importlib.import_module("hacienda_gpt.tax.recodificacion.adaptadores_otros")
except ModuleNotFoundError as exc:  # pragma: no cover - depends on what is installed
    if exc.name != "hacienda_gpt.tax.recodificacion.adaptadores_otros":
        raise

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
    "control_de_lo_publicado",
    "ejecutar",
    "objetivo_de_ruta",
    "objetivos_de_articulo",
    "objetivos_de_desglose",
    "objetivos_de_rollover",
    "objetivos_de_verificacion",
    "todos_los_objetivos",
    "pendientes_de_confirmar",
    "rechazar_propuesta",
    "recodificar",
    "registrar",
]
