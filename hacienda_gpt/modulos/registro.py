"""Discovery and validation of the household modules (see ``contrato.py``).

Every package ``hacienda_gpt.modulos.<clave>`` whose ``__init__`` exposes ``MODULO`` is found with
:mod:`pkgutil`, imported and checked against the contract. A module that fails to import or breaks a rule
is logged and left out: one broken module never takes the app down, and the others keep working.

Rules checked here (the core relies on them everywhere else):

* the key is a lower-case identifier, unique, and equal to the package name;
* name, description and specialist instructions are non-empty Spanish text;
* every fact is a :class:`HechoModulo` whose name is prefixed ``<clave>_`` (or is one of the shared facts
  the core already defines, which a module may declare to say it reads it but never redefines), of a known
  type, and not declared by another module;
* every tool is a :class:`HerramientaModulo` named ``<clave>_…`` (the tax module keeps the names its tools
  always had), unique across modules, with a valid effect and an object input schema;
* sources are :class:`FuenteModulo` and ``avisos`` is callable.

``MODULOS_DESACTIVADOS`` (settings) switches modules off for everyone without touching their code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import importlib
import logging
import pkgutil
import re
import threading
from typing import Any, get_args

from hacienda_gpt import settings
from hacienda_gpt.agent.providers.base import ToolSpec
from hacienda_gpt.modulos.contrato import (
    Efecto,
    FuenteModulo,
    HechoModulo,
    HerramientaModulo,
    Modulo,
    TipoHecho,
)

logger = logging.getLogger(__name__)

FISCAL = "fiscal"
PAQUETE = "hacienda_gpt.modulos"
_CLAVE_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_NOMBRE_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_EFECTOS = frozenset(get_args(Efecto))
_TIPOS = frozenset(get_args(TipoHecho))
# Notification kinds and key prefixes the core already uses: a module named like them would be ambiguous.
CLAVES_RESERVADAS = frozenset(
    {"plazo", "fin_de_ejercicio", "cambio_normativo", "resumen_semanal", "modulo", "nucleo", "contrato", "registro"}
)


def hechos_compartidos() -> frozenset[str]:
    """Facts the core defines (the tax profile vocabulary): modules read them and never redefine them."""
    from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE

    return frozenset(EXPECTED_FACT_TYPE)


@dataclass
class Registro:
    """The modules that passed validation, in a stable order (``fiscal`` first, then by key)."""

    modulos: dict[str, Modulo] = field(default_factory=dict)
    # origin (package or key) -> why it was left out
    errores: dict[str, str] = field(default_factory=dict)

    def __iter__(self) -> Any:
        return iter(self.modulos.values())

    def __contains__(self, clave: object) -> bool:
        return clave in self.modulos

    def get(self, clave: str) -> Modulo | None:
        return self.modulos.get(clave)

    def activos(self) -> list[Modulo]:
        """Registered modules minus the ones the operator switched off (``MODULOS_DESACTIVADOS``).

        The tax module cannot be switched off: the advisor's own tools are the tax tools.
        """
        apagados = desactivados_por_operador() - {FISCAL}
        return [modulo for clave, modulo in self.modulos.items() if clave not in apagados]

    def hechos(self) -> dict[str, tuple[Modulo, HechoModulo]]:
        """Facts owned by a module (shared facts a module merely declares are not included)."""
        compartidos = hechos_compartidos()
        return {
            hecho.nombre: (modulo, hecho)
            for modulo in self.modulos.values()
            for hecho in modulo.hechos
            if hecho.nombre not in compartidos
        }

    def herramienta(self, nombre: str) -> tuple[Modulo, HerramientaModulo] | None:
        for modulo in self.modulos.values():
            for herramienta in modulo.herramientas:
                if herramienta.spec.name == nombre:
                    return modulo, herramienta
        return None


def desactivados_por_operador() -> frozenset[str]:
    return frozenset(part.strip().lower() for part in settings.MODULOS_DESACTIVADOS.split(",") if part.strip())


def _texto(valor: Any) -> bool:
    return isinstance(valor, str) and bool(valor.strip())


def problemas_de(modulo: Any, *, paquete: str | None = None) -> list[str]:
    """Every contract rule ``modulo`` breaks on its own (cross-module uniqueness is checked by the registry)."""
    if not isinstance(modulo, Modulo):
        return ["no cumple el protocolo Modulo (faltan atributos o el método avisos)"]
    problemas: list[str] = []
    clave = modulo.clave
    if not isinstance(clave, str) or not _CLAVE_RE.match(clave):
        return [f"clave no válida: {clave!r} (minúsculas, cifras y guiones bajos)"]
    if clave in CLAVES_RESERVADAS:
        problemas.append(f"la clave {clave!r} está reservada por el núcleo")
    if paquete is not None and paquete != clave:
        problemas.append(f"la clave {clave!r} no coincide con el nombre del paquete {paquete!r}")
    for atributo in ("nombre", "descripcion", "instrucciones"):
        if not _texto(getattr(modulo, atributo, None)):
            problemas.append(f"{atributo} vacío")
    if not callable(getattr(modulo, "avisos", None)):
        problemas.append("avisos no es invocable")

    compartidos = hechos_compartidos()
    vistos: set[str] = set()
    for hecho in modulo.hechos or ():
        if not isinstance(hecho, HechoModulo):
            problemas.append(f"hecho que no es HechoModulo: {hecho!r}")
            continue
        nombre = hecho.nombre
        if nombre in vistos:
            problemas.append(f"hecho repetido: {nombre}")
        vistos.add(nombre)
        if hecho.tipo not in _TIPOS:
            problemas.append(f"{nombre}: tipo desconocido {hecho.tipo!r}")
        if nombre in compartidos:
            continue  # declared to say the module reads it; the core keeps its own definition
        if not isinstance(nombre, str) or not _NOMBRE_RE.match(nombre) or not nombre.startswith(f"{clave}_"):
            problemas.append(f"el hecho {nombre!r} debe empezar por {clave}_ (o ser un hecho compartido del núcleo)")
        if not _texto(hecho.etiqueta):
            problemas.append(f"{nombre}: etiqueta vacía")
        if hecho.validar is not None and not callable(hecho.validar):
            problemas.append(f"{nombre}: validar no es invocable")

    herramientas: set[str] = set()
    for herramienta in modulo.herramientas or ():
        if not isinstance(herramienta, HerramientaModulo) or not isinstance(herramienta.spec, ToolSpec):
            problemas.append(f"herramienta que no es HerramientaModulo con ToolSpec: {herramienta!r}")
            continue
        nombre = herramienta.spec.name
        if nombre in herramientas:
            problemas.append(f"herramienta repetida: {nombre}")
        herramientas.add(nombre)
        if clave != FISCAL and (not _NOMBRE_RE.match(nombre) or not nombre.startswith(f"{clave}_")):
            problemas.append(f"la herramienta {nombre!r} debe empezar por {clave}_")
        if herramienta.efecto not in _EFECTOS:
            problemas.append(f"{nombre}: efecto desconocido {herramienta.efecto!r}")
        if not callable(herramienta.ejecutar):
            problemas.append(f"{nombre}: ejecutar no es invocable")
        schema = herramienta.spec.input_schema
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            problemas.append(f"{nombre}: input_schema debe ser un objeto JSON Schema de tipo object")
        if not _texto(herramienta.spec.description):
            problemas.append(f"{nombre}: descripción vacía")

    for fuente in modulo.fuentes or ():
        if not isinstance(fuente, FuenteModulo) or not _texto(fuente.identificador):
            problemas.append(f"fuente que no es FuenteModulo con identificador: {fuente!r}")
    return problemas


def construir_registro(candidatos: Iterable[tuple[str, Any]], *, errores: Mapping[str, str] | None = None) -> Registro:
    """Validate ``(origin, module)`` pairs into a registry. ``origin`` is the package name (or a label).

    A module is accepted whole or not at all. When two modules claim the same key, fact or tool, the one
    accepted first (``fiscal``, then alphabetical by key) keeps it and the other is left out.
    """
    registro = Registro(errores=dict(errores or {}))
    ordenados = sorted(
        candidatos,
        key=lambda par: (getattr(par[1], "clave", None) != FISCAL, str(getattr(par[1], "clave", par[0]))),
    )
    hechos: dict[str, str] = {}
    herramientas: dict[str, str] = {}
    compartidos = hechos_compartidos()
    for origen, modulo in ordenados:
        problemas = problemas_de(modulo, paquete=origen)
        if not problemas:
            clave = modulo.clave
            if clave in registro.modulos:
                problemas.append(f"la clave {clave!r} ya la usa otro módulo")
            for hecho in modulo.hechos:
                if hecho.nombre not in compartidos and hecho.nombre in hechos:
                    problemas.append(f"el hecho {hecho.nombre} ya lo declara el módulo {hechos[hecho.nombre]}")
            for herramienta in modulo.herramientas:
                if herramienta.spec.name in herramientas:
                    problemas.append(
                        f"la herramienta {herramienta.spec.name} ya la declara el módulo "
                        f"{herramientas[herramienta.spec.name]}"
                    )
        if problemas:
            motivo = "; ".join(problemas)
            registro.errores[origen] = motivo
            logger.error("Module %s left out: %s", origen, motivo)
            continue
        registro.modulos[modulo.clave] = modulo
        hechos.update({h.nombre: modulo.clave for h in modulo.hechos if h.nombre not in compartidos})
        herramientas.update({h.spec.name: modulo.clave for h in modulo.herramientas})
    return registro


def descubrir(paquete: str = PAQUETE) -> tuple[list[tuple[str, Any]], dict[str, str]]:
    """Import every sub-package of ``paquete`` that exposes ``MODULO``. Import errors are returned, not raised."""
    base = importlib.import_module(paquete)
    encontrados: list[tuple[str, Any]] = []
    errores: dict[str, str] = {}
    for info in pkgutil.iter_modules(base.__path__):
        if not info.ispkg:
            continue  # the core's own files (contrato, registro, perfil…)
        nombre = f"{paquete}.{info.name}"
        try:
            paquete_modulo = importlib.import_module(nombre)
        except Exception as exc:  # a broken module must never crash the app
            errores[info.name] = f"no se pudo importar: {type(exc).__name__}: {exc}"
            logger.exception("Module package %s failed to import", nombre)
            continue
        modulo = getattr(paquete_modulo, "MODULO", None)
        if modulo is None:
            errores[info.name] = "el paquete no expone MODULO"
            logger.error("Module package %s exposes no MODULO", nombre)
            continue
        encontrados.append((info.name, modulo))
    return encontrados, errores


_lock = threading.Lock()
_cache: list[Registro] = []


def registro() -> Registro:
    """The process-wide registry, discovered once (thread-safe). :func:`reiniciar` forgets it (tests)."""
    if _cache:
        return _cache[0]
    with _lock:
        if not _cache:
            encontrados, errores = descubrir()
            _cache.append(construir_registro(encontrados, errores=errores))
        return _cache[0]


def reiniciar(nuevo: Registro | None = None) -> None:
    """Forget the cached registry, or replace it (tests use this to install fake modules)."""
    with _lock:
        _cache.clear()
        if nuevo is not None:
            _cache.append(nuevo)


def modulos_activos() -> list[Modulo]:
    return registro().activos()


__all__ = [
    "CLAVES_RESERVADAS",
    "FISCAL",
    "Registro",
    "construir_registro",
    "descubrir",
    "desactivados_por_operador",
    "hechos_compartidos",
    "modulos_activos",
    "problemas_de",
    "registro",
    "reiniciar",
]
