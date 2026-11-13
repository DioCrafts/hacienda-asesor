"""Optional LLM labeller of merchant categories, OFF by default, behind a deterministic gate.

The detector never needs it: it only suggests a category for merchants the catalogue does not know. It
runs only when BOTH the environment variable ``SUSCRIPCIONES_ETIQUETADOR_LLM=1`` is set AND the core has
registered a callable with :func:`configurar`. The gate is deterministic:

- the callable receives ONLY the normalised merchant key (e.g. ``"ACME STREAMING"``): never amounts,
  dates, account numbers or the raw concept; keys with digits or longer than 40 characters are refused,
  so references or personal data left in a concept cannot leak;
- its answer is accepted only if it is exactly one of the closed list of categories (``CATEGORIAS``) and
  not ``"otros"``; anything else is ignored;
- errors are swallowed (the category stays ``otros``), and answers are cached per key for the process.

A category set this way is marked ``categoria_origen = "llm"`` and the tools say so.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import re

from hacienda_gpt.modulos.suscripciones.modelos import CATEGORIAS

Etiquetador = Callable[[str, tuple[str, ...]], str | None]

_etiquetador: Etiquetador | None = None
_cache: dict[str, str | None] = {}
_CLAVE_SEGURA = re.compile(r"^[A-Z][A-Z &]{1,39}$")


def configurar(funcion: Etiquetador | None) -> None:
    """Register (or remove, with None) the callable ``(clave, categorias) -> categoria | None``."""
    global _etiquetador
    _etiquetador = funcion
    _cache.clear()


def activo() -> bool:
    return os.environ.get("SUSCRIPCIONES_ETIQUETADOR_LLM", "").strip() == "1" and _etiquetador is not None


def etiquetar(clave: str) -> str | None:
    if not activo() or not _CLAVE_SEGURA.match(clave):
        return None
    if clave not in _cache:
        try:
            respuesta = _etiquetador(clave, CATEGORIAS) if _etiquetador else None
        except Exception:  # noqa: BLE001 - an optional helper must never break the analysis
            respuesta = None
        respuesta = (respuesta or "").strip().lower()
        _cache[clave] = respuesta if respuesta in CATEGORIAS and respuesta != "otros" else None
    return _cache[clave]
