"""The contract every life-admin module implements (fiscal, luz, caducidades, ayudas…).

A module is a package ``hacienda_gpt.modulos.<clave>`` whose ``__init__`` exposes ``MODULO``, an object
satisfying :class:`Modulo`. The central agent never computes anything itself: it routes the user's
question to the specialist of each module, whose tools are deterministic calculators over the household
profile. The same rules as the tax calculator hold for every module:

- every legal or official figure is READ from its source (BOE first) and stored as versioned data with a
  literal quote; never from memory;
- tools compute deterministically and return text with the figures, their sources and their caveats;
- proactive jobs return :class:`AvisoModulo` items; the core turns them into notifications;
- a tool whose effect reaches outside the app (sending, contracting, cancelling) is ``efecto="actua"`` and
  the core never runs it without the user's explicit confirmation.

This file is the stable interface between the core and the modules: change it only with every module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from hacienda_gpt.agent.providers.base import ToolSpec

Efecto = Literal["lectura", "prepara", "actua"]
TipoHecho = Literal["string", "number", "boolean", "json"]
Gravedad = Literal["info", "aviso", "urgente"]


@dataclass(frozen=True)
class ResultadoHerramienta:
    """What a module tool returns to the agent.

    ``contenido`` is the full text the model reads (figures, sources, caveats, in Spanish); ``resumen`` a
    one-line summary for the trace; ``citas`` the sources (BOE ids, URLs) the answer may cite.
    """

    contenido: str
    resumen: str
    es_error: bool = False
    citas: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextoModulo:
    """What a module sees when one of its tools or jobs runs.

    ``perfil`` is the household profile: every fact of the user's case (name -> value; JSON-valued facts
    already parsed). A module reads the facts it declared and any shared one (comunidad_autonoma,
    estado_civil, num_descendientes…). ``registrar`` is present only when the run may write facts: it
    stores facts this module declared, validated by their ``validar``.
    """

    perfil: Mapping[str, Any]
    hoy: date
    case_id: str | None = None
    registrar: Callable[[Mapping[str, Any]], None] | None = None


@dataclass(frozen=True)
class HechoModulo:
    """A fact of the household profile that a module owns (stored in the user's case).

    ``nombre`` must be prefixed with the module key (``luz_potencia_contratada_kw``) unless it is one of
    the shared facts the core already defines. ``validar`` normalises a value or raises ``ValueError`` with
    a Spanish message; for ``json`` facts it receives the parsed value and returns the canonical one.
    ``sensible`` marks personal data the privacy docs must mention (health, a third party, finances).
    """

    nombre: str
    tipo: TipoHecho
    etiqueta: str
    descripcion: str
    validar: Callable[[Any], Any] | None = None
    sensible: bool = False


@dataclass(frozen=True)
class HerramientaModulo:
    spec: ToolSpec
    ejecutar: Callable[[Mapping[str, Any], ContextoModulo], ResultadoHerramienta]
    efecto: Efecto = "lectura"


@dataclass(frozen=True)
class AvisoModulo:
    """A proactive notice. ``clave`` dedupes it per case (same key = same notice, never repeated)."""

    clave: str
    titulo: str
    cuerpo: str
    gravedad: Gravedad = "info"
    acciones: tuple[str, ...] = ()
    fuentes: tuple[str, ...] = ()
    importe: Decimal | None = None
    vence: date | None = None
    # Keys of earlier notices of this module that this one replaces (e.g. an updated estimate).
    sustituye_prefijo: str | None = None


@dataclass(frozen=True)
class FuenteModulo:
    """A source a module's data is read from, for the maintenance loop (verification, format watch)."""

    identificador: str  # BOE id or a stable name
    descripcion: str
    url: str


@runtime_checkable
class Modulo(Protocol):
    clave: str  # "fiscal", "luz", "caducidades", "ayudas", "suscripciones", "hipoteca"
    nombre: str  # Spanish, for the UI
    descripcion: str  # Spanish, what the module answers: the central agent routes by it
    instrucciones: str  # Spanish system-prompt fragment for the module's specialist
    hechos: Sequence[HechoModulo]
    herramientas: Sequence[HerramientaModulo]
    fuentes: Sequence[FuenteModulo]

    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        """Deterministic proactive job, run daily per case. Must be idempotent (same keys)."""
        ...


@dataclass
class ModuloBase:
    """Convenience base: a module with no proactive job until it overrides ``avisos``."""

    clave: str
    nombre: str
    descripcion: str
    instrucciones: str
    hechos: Sequence[HechoModulo] = field(default_factory=tuple)
    herramientas: Sequence[HerramientaModulo] = field(default_factory=tuple)
    fuentes: Sequence[FuenteModulo] = field(default_factory=tuple)

    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        return []
