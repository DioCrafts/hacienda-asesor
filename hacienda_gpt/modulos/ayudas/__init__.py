"""Módulo ``ayudas``: public aid the household is entitled to and has not asked for.

Exposes ``MODULO`` (see :mod:`hacienda_gpt.modulos.contrato`) and :func:`actualizar_convocatorias`, the
daily BOE reading the core's job runs once a day before the per-case notices.
"""

from __future__ import annotations

from hacienda_gpt.modulos.ayudas.modulo import MODULO, ModuloAyudas
from hacienda_gpt.modulos.ayudas.revision import actualizar_convocatorias

__all__ = ["MODULO", "ModuloAyudas", "actualizar_convocatorias"]
