"""Strategy registry — dispatches files to the right ``IngestStrategy``.

Strategies are tried in registration order; the first whose ``probe`` returns
True claims the file. The default Docling-backed strategy is registered last
as the fallback, so adding a new specialised strategy is purely additive:
``strategies.register(MyStrategy())`` before the loader spawns workers and
your strategy claims its files.

The registry is a process-level list — strategies are typically constructed
at import time and ``register()``-ed in this module's ``__init__``. Tests can
reset the registry via :func:`reset_registry` to install fakes.

Worker processes spawned by the loader re-import this module on startup and
re-register the default strategies; do **not** rely on cross-process state.
"""

from __future__ import annotations

import logging
from threading import RLock

from hacienda_gpt.processor.strategies.base import IngestStrategy

logger = logging.getLogger(__name__)

_REGISTRY: list[IngestStrategy] = []
_REGISTRY_LOCK = RLock()


def register(strategy: IngestStrategy) -> None:
    """Append ``strategy`` to the registry.

    Strategies are evaluated in registration order — register the most
    specific (BOE consolidados, JOUE XML, …) BEFORE the default fallback,
    or the default will swallow everything.
    """
    with _REGISTRY_LOCK:
        # Reject duplicate IDs loudly: silent duplicates would create the
        # exact "which strategy actually ran?" ambiguity the registry is
        # designed to remove.
        existing = {s.strategy_id for s in _REGISTRY}
        if strategy.strategy_id in existing:
            raise ValueError(
                f"Strategy with id {strategy.strategy_id!r} already registered. "
                f"Use reset_registry() in tests or pick a different strategy_id."
            )
        _REGISTRY.append(strategy)
        logger.debug("Registered ingest strategy %s", strategy.strategy_id)


def select_strategy(file_path: str) -> IngestStrategy:
    """Return the first registered strategy whose ``probe`` claims ``file_path``.

    Raises :class:`RuntimeError` if no strategy claims the file — usually
    means the default fallback strategy wasn't registered, which is a
    project-setup bug, not a per-file failure.
    """
    with _REGISTRY_LOCK:
        for strategy in _REGISTRY:
            if strategy.probe(file_path):
                return strategy
    raise RuntimeError(
        f"No ingest strategy claimed {file_path!r}. Ensure the default "
        f"fallback strategy is registered last."
    )


def registered_strategies() -> list[IngestStrategy]:
    """Return a snapshot of the registry — for diagnostics and the
    pipeline fingerprint computation."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY)


def reset_registry() -> None:
    """Clear the registry. Test-only — production code should never call this."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def install_default_strategies() -> None:
    """Register the strategies shipped with the project.

    Idempotent: calling it twice is a no-op (each strategy probes for its
    own ``strategy_id`` before registering). Strategies are imported lazily
    so unit tests that monkey-patch one don't pay for loading the others.
    """
    # Import inside the function so this module stays importable when a
    # subset of strategy deps (lxml, mlx, ...) is missing — useful for
    # tooling that only needs the protocol/registry.
    from hacienda_gpt.processor.strategies.boe import BoeConsolidadoTableStrategy
    from hacienda_gpt.processor.strategies.default import DefaultDoclingStrategy

    with _REGISTRY_LOCK:
        present = {s.strategy_id for s in _REGISTRY}
        # Order matters: specialised strategies first, default last so it
        # only catches what nobody else claimed.
        if BoeConsolidadoTableStrategy.strategy_id not in present:
            register(BoeConsolidadoTableStrategy())
        if DefaultDoclingStrategy.strategy_id not in present:
            register(DefaultDoclingStrategy())


__all__ = [
    "IngestStrategy",
    "install_default_strategies",
    "register",
    "registered_strategies",
    "reset_registry",
    "select_strategy",
]
