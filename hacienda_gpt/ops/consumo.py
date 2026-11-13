"""Measure and limit what the app spends on LLM calls.

One user question can cost many model calls (the agent loop, the planner, two
researchers and a critic in team mode, the verifier; a scanned document is read
twice). This module:

* records every call in ``llm_usage`` (same SQLite file as the cases) with the
  tokens the API reported and their cost according to a price table kept as
  DATA (``data/precios_llm.json``) — an unknown model or price gives a NULL
  cost, never an estimate;
* aggregates by day (Europe/Madrid), user, case and purpose;
* enforces optional limits (:class:`Presupuesto`) BEFORE each call;
* provides :class:`MeteredProvider`, a wrapper with the ``LLMProvider``
  protocol that does the checking and the recording around any provider.

The limits are soft by one call: they are checked before a call starts, so a
call in flight (or several concurrent ones) can take the day slightly over.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
import sqlite3
from threading import Lock, RLock
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt import settings
from hacienda_gpt.agent.providers.base import (
    Attachment,
    Conversation,
    LLMProvider,
    ModelTurn,
    ProviderError,
    TextCallback,
    ToolResult,
    ToolSpec,
    Usage,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Agregado",
    "ConsumoStore",
    "ContextoConsumo",
    "MeteredConversation",
    "MeteredProvider",
    "Precio",
    "Presupuesto",
    "PresupuestoAgotadoError",
    "ResumenConsumo",
    "TablaPrecios",
    "Usage",
    "almacen_por_defecto",
    "calcular_coste",
    "cargar_precios",
    "contexto_consumo",
    "hoy_madrid",
    "medir",
]

PRECIOS_PATH = Path(__file__).resolve().parent / "data" / "precios_llm.json"
_MILLON = Decimal(1_000_000)
_COSTE_DECIMALES = Decimal("0.00000001")
_TOTAL_DECIMALES = Decimal("0.000001")
# Dated snapshot ids ("<model>-20260401") are priced like the model they pin.
_SUFIJO_FECHA_RE = re.compile(r"-\d{8}$")

Dimension = Literal["dia", "usuario", "caso", "proposito"]
Limite = Literal["diario", "diario_por_usuario", "llamadas_por_pregunta"]


# --- Time: budgets and daily totals follow the Spanish calendar day -----------


def _zona() -> Any:
    try:
        return ZoneInfo("Europe/Madrid")
    except ZoneInfoNotFoundError:  # pragma: no cover - only without a tz database
        return UTC


def hoy_madrid(ahora: datetime | None = None) -> date:
    """The calendar day in mainland Spain at ``ahora`` (default: now)."""
    return (ahora or datetime.now(UTC)).astimezone(_zona()).date()


def _inicio_del_dia(dia: date) -> datetime:
    """Midnight (Europe/Madrid) at the start of ``dia``, as an aware UTC datetime."""
    return datetime.combine(dia, time(0), tzinfo=_zona()).astimezone(UTC)


def _ts(momento: datetime) -> str:
    # Fixed-width UTC text, so comparing strings compares instants.
    return momento.astimezone(UTC).isoformat(timespec="microseconds")


# --- Prices (data, not code) --------------------------------------------------


class Precio(BaseModel):
    """USD per million tokens for one model. None = not looked up."""

    model_config = ConfigDict(extra="forbid")

    input: Decimal | None = Field(default=None, ge=0)
    output: Decimal | None = Field(default=None, ge=0)
    cache_read: Decimal | None = Field(default=None, ge=0)
    cache_write: Decimal | None = Field(default=None, ge=0)
    nota: str = ""


class TablaPrecios(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consultado_el: date
    moneda: Literal["USD"]
    unidad: str
    nota: str = Field(min_length=1)
    fuente: str = Field(min_length=1)
    modelos: dict[str, Precio]

    def precio_de(self, model: str) -> Precio | None:
        """The price of ``model``; a dated snapshot id uses its base model's."""
        precio = self.modelos.get(model)
        if precio is None:
            precio = self.modelos.get(_SUFIJO_FECHA_RE.sub("", model))
        return precio


@lru_cache(maxsize=8)
def _leer_precios(path: str) -> TablaPrecios:
    return TablaPrecios.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def cargar_precios(path: str | Path | None = None) -> TablaPrecios:
    """Load the price table: ``path``, else ``LLM_PRICES_PATH``, else the bundled one."""
    return _leer_precios(str(path or settings.LLM_PRICES_PATH or PRECIOS_PATH))


def calcular_coste(usage: Usage, precios: TablaPrecios | None = None) -> Decimal | None:
    """Cost in USD of one call, or None when it cannot be known.

    None when the API reported no usage, the model is not in the table, or a
    token type that was actually used has no price. Never a guess.
    """
    if not usage.reported:
        return None
    precio = (precios or cargar_precios()).precio_de(usage.model)
    if precio is None:
        return None
    total = Decimal(0)
    for tokens, por_millon in (
        (usage.input_tokens, precio.input),
        (usage.output_tokens, precio.output),
        (usage.cache_read_tokens, precio.cache_read),
        (usage.cache_write_tokens, precio.cache_write),
    ):
        if tokens <= 0:
            continue
        if por_millon is None:
            return None
        total += Decimal(tokens) * por_millon / _MILLON
    return total.quantize(_COSTE_DECIMALES)


# --- Store --------------------------------------------------------------------


class Agregado(BaseModel):
    """Totals of a group of calls. ``coste_usd`` only adds the calls whose cost
    is known: with ``llamadas_sin_coste`` > 0 it is a lower bound."""

    model_config = ConfigDict(extra="forbid")

    # Day (YYYY-MM-DD), user id, case id or purpose; None for the overall total
    # and for calls not attributed to any user / case.
    clave: str | None = None
    llamadas: int = 0
    llamadas_sin_coste: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    coste_usd: Decimal = Decimal(0)


class ResumenConsumo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Both inclusive, calendar days in Europe/Madrid.
    desde: date
    hasta: date
    total: Agregado
    por_proposito: list[Agregado] = Field(default_factory=list)
    por_dia: list[Agregado] = Field(default_factory=list)
    por_usuario: list[Agregado] = Field(default_factory=list)
    por_caso: list[Agregado] = Field(default_factory=list)


class _Acumulador:
    def __init__(self, clave: str | None) -> None:
        self.clave = clave
        self.llamadas = 0
        self.sin_coste = 0
        self.tokens = [0, 0, 0, 0]
        self.coste = Decimal(0)

    def sumar(self, row: sqlite3.Row) -> None:
        self.llamadas += 1
        for index, column in enumerate(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")):
            self.tokens[index] += row[column] or 0
        if row["cost_usd"] is None:
            self.sin_coste += 1
        else:
            self.coste += Decimal(str(row["cost_usd"]))

    def agregado(self) -> Agregado:
        return Agregado(
            clave=self.clave,
            llamadas=self.llamadas,
            llamadas_sin_coste=self.sin_coste,
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            cache_read_tokens=self.tokens[2],
            cache_write_tokens=self.tokens[3],
            coste_usd=self.coste.quantize(_TOTAL_DECIMALES),
        )


class ConsumoStore:
    """SQLite log of LLM calls (same database file as the cases)."""

    def __init__(self, db_path: str, *, precios: TablaPrecios | None = None) -> None:
        self.db_path = str(Path(db_path))
        self._precios = precios
        self._lock = RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            # Token columns are NULL when the API reported no usage; cost_usd is
            # NULL whenever the cost is unknown. cost_usd is an operational
            # estimate (REAL so SQLite can add it up), not an accounting figure.
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    user_id TEXT,
                    case_id TEXT,
                    purpose TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    cost_usd REAL
                )
                """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def precios(self) -> TablaPrecios:
        return self._precios or cargar_precios()

    def registrar(
        self,
        usage: Usage,
        *,
        user_id: str | None = None,
        case_id: str | None = None,
        ts: datetime | None = None,
    ) -> Decimal | None:
        """Record one call and return its cost (None when unknown)."""
        coste = calcular_coste(usage, self.precios())
        tokens: tuple[int | None, ...] = (
            (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens)
            if usage.reported
            else (None, None, None, None)
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO llm_usage (ts, user_id, case_id, purpose, provider, model, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _ts(ts or datetime.now(UTC)),
                    user_id,
                    case_id,
                    usage.purpose,
                    usage.provider,
                    usage.model,
                    *tokens,
                    None if coste is None else float(coste),
                ),
            )
        return coste

    def coste_del_dia(self, dia: date, *, user_id: str | None = None) -> Decimal:
        """Known cost of the calls made on ``dia`` (Europe/Madrid), optionally of one user."""
        sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts >= ? AND ts < ?"
        params: list[Any] = [_ts(_inicio_del_dia(dia)), _ts(_inicio_del_dia(dia + timedelta(days=1)))]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return Decimal(str(row[0])).quantize(_TOTAL_DECIMALES)

    def _filas(self, desde: date, hasta: date) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM llm_usage WHERE ts >= ? AND ts < ? ORDER BY ts, id",
                (_ts(_inicio_del_dia(desde)), _ts(_inicio_del_dia(hasta + timedelta(days=1)))),
            ).fetchall()

    @staticmethod
    def _clave(row: sqlite3.Row, por: Dimension) -> str | None:
        if por == "dia":
            return hoy_madrid(datetime.fromisoformat(row["ts"])).isoformat()
        column = {"usuario": "user_id", "caso": "case_id", "proposito": "purpose"}[por]
        value = row[column]
        return None if value is None else str(value)

    def agregar(self, por: Dimension, *, desde: date, hasta: date) -> list[Agregado]:
        """Totals grouped by day / user / case / purpose, for days ``desde``..``hasta`` inclusive."""
        agregados: list[Agregado] = getattr(self.resumen(desde=desde, hasta=hasta, por=(por,)), _CAMPO[por])
        return agregados

    def resumen(
        self,
        *,
        desde: date,
        hasta: date,
        por: Sequence[Dimension] = ("proposito", "dia", "usuario", "caso"),
    ) -> ResumenConsumo:
        """Overall total plus the requested breakdowns, for ``desde``..``hasta`` inclusive."""
        total = _Acumulador(None)
        grupos: dict[Dimension, dict[str | None, _Acumulador]] = {dimension: {} for dimension in por}
        for row in self._filas(desde, hasta):
            total.sumar(row)
            for dimension, grupo in grupos.items():
                clave = self._clave(row, dimension)
                grupo.setdefault(clave, _Acumulador(clave)).sumar(row)
        resumen = ResumenConsumo(desde=desde, hasta=hasta, total=total.agregado())
        for dimension, grupo in grupos.items():
            agregados = [acumulador.agregado() for acumulador in grupo.values()]
            # Days in order; everything else by cost (then calls), biggest first.
            if dimension == "dia":
                agregados.sort(key=lambda item: item.clave or "")
            else:
                agregados.sort(key=lambda item: (-item.coste_usd, -item.llamadas, item.clave or ""))
            setattr(resumen, _CAMPO[dimension], agregados)
        return resumen


_CAMPO: dict[Dimension, str] = {
    "dia": "por_dia",
    "usuario": "por_usuario",
    "caso": "por_caso",
    "proposito": "por_proposito",
}


@lru_cache(maxsize=4)
def _almacen(db_path: str) -> ConsumoStore:
    return ConsumoStore(db_path)


def almacen_por_defecto() -> ConsumoStore:
    """The process-wide store on the cases database (``DECISION_STATE_DB_PATH``)."""
    return _almacen(settings.DECISION_STATE_DB_PATH)


# --- Budget -------------------------------------------------------------------


class PresupuestoAgotadoError(ProviderError):
    """A spend limit was reached; the message (Spanish) is meant for the user.

    A :class:`ProviderError` so that code which already degrades when the model
    is unavailable (verifier, planner, researchers) keeps doing so; it is not
    retryable. ``se_restablece`` is None for the per-question cap, which simply
    starts again with the next question.
    """

    def __init__(self, message: str, *, limite: Limite, se_restablece: datetime | None = None) -> None:
        super().__init__(message, retryable=False)
        self.limite: Limite = limite
        self.se_restablece = se_restablece


def _decimal_opcional(nombre: str, valor: str) -> Decimal | None:
    texto = valor.strip()
    if not texto:
        return None
    try:
        numero = Decimal(texto)
    except InvalidOperation:
        numero = Decimal("NaN")
    if not numero.is_finite() or numero <= 0:
        raise ValueError(f"{nombre} debe ser un importe en USD mayor que cero (o vacío para no limitar), no {valor!r}.")
    return numero


def _entero_opcional(nombre: str, valor: str) -> int | None:
    texto = valor.strip()
    if not texto:
        return None
    try:
        numero = int(texto)
    except ValueError:
        numero = 0
    if numero < 1:
        raise ValueError(f"{nombre} debe ser un entero mayor que cero (o vacío para no limitar), no {valor!r}.")
    return numero


def _formato_reinicio(momento: datetime) -> str:
    local = momento.astimezone(_zona())
    return f"el {local:%d/%m/%Y} a las {local:%H:%M} (hora peninsular española)"


@dataclass(frozen=True)
class Presupuesto:
    """Optional spend limits; None = no limit.

    The USD limits add up the KNOWN cost of the day's calls (Europe/Madrid day):
    calls to a model without a price do not count towards them.
    """

    store: ConsumoStore
    diario_usd: Decimal | None = None
    diario_por_usuario_usd: Decimal | None = None
    max_llamadas_por_pregunta: int | None = None

    @classmethod
    def desde_settings(cls, store: ConsumoStore) -> Presupuesto:
        """Limits from ``LLM_DAILY_BUDGET_USD``, ``LLM_DAILY_BUDGET_USD_PER_USER`` and
        ``LLM_MAX_CALLS_PER_QUESTION``. Raises ValueError on a value that is not a positive number."""
        return cls(
            store=store,
            diario_usd=_decimal_opcional("LLM_DAILY_BUDGET_USD", settings.LLM_DAILY_BUDGET_USD),
            diario_por_usuario_usd=_decimal_opcional(
                "LLM_DAILY_BUDGET_USD_PER_USER", settings.LLM_DAILY_BUDGET_USD_PER_USER
            ),
            max_llamadas_por_pregunta=_entero_opcional(
                "LLM_MAX_CALLS_PER_QUESTION", settings.LLM_MAX_CALLS_PER_QUESTION
            ),
        )

    @property
    def limita_en_usd(self) -> bool:
        return self.diario_usd is not None or self.diario_por_usuario_usd is not None

    def comprobar(
        self,
        user_id: str | None,
        case_id: str | None,
        *,
        llamadas_en_pregunta: int = 0,
        ahora: datetime | None = None,
    ) -> None:
        """Raise :class:`PresupuestoAgotadoError` if one more call is not allowed.

        ``llamadas_en_pregunta`` is how many calls the current question has
        already made. ``case_id`` is accepted for symmetry with the usage log;
        there is no per-case limit.
        """
        del case_id
        if self.max_llamadas_por_pregunta is not None and llamadas_en_pregunta >= self.max_llamadas_por_pregunta:
            raise PresupuestoAgotadoError(
                f"Esta consulta ha alcanzado el máximo de {self.max_llamadas_por_pregunta} llamadas al modelo de IA "
                "por pregunta. El límite se aplica a cada pregunta por separado: puedes volver a preguntar ahora "
                "mismo, mejor con una consulta más concreta.",
                limite="llamadas_por_pregunta",
            )
        if not self.limita_en_usd:
            return
        dia = hoy_madrid(ahora)
        reinicio = _inicio_del_dia(dia + timedelta(days=1))
        if self.diario_usd is not None and self.store.coste_del_dia(dia) >= self.diario_usd:
            raise PresupuestoAgotadoError(
                "El servicio ha alcanzado su límite diario de gasto en modelos de IA. "
                f"Se restablece {_formato_reinicio(reinicio)}.",
                limite="diario",
                se_restablece=reinicio,
            )
        if (
            self.diario_por_usuario_usd is not None
            and user_id is not None
            and self.store.coste_del_dia(dia, user_id=user_id) >= self.diario_por_usuario_usd
        ):
            raise PresupuestoAgotadoError(
                "Has alcanzado tu límite diario de uso de modelos de IA. "
                f"Se restablece {_formato_reinicio(reinicio)}.",
                limite="diario_por_usuario",
                se_restablece=reinicio,
            )


# --- Per-question context -----------------------------------------------------


@dataclass
class ContextoConsumo:
    """Who a question is for and how many model calls it has made so far."""

    user_id: str | None = None
    case_id: str | None = None
    llamadas: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)


_CONTEXTO: ContextVar[ContextoConsumo | None] = ContextVar("consumo_contexto", default=None)
# Where the usage reported by the provider during the current metered call lands.
_CAPTURA: ContextVar[list[Usage] | None] = ContextVar("consumo_captura", default=None)


@contextmanager
def contexto_consumo(*, user_id: str | None = None, case_id: str | None = None) -> Iterator[ContextoConsumo]:
    """Attribute the model calls made inside the block to one user question.

    For providers that live longer than a question (the API builds its agent
    once): every :class:`MeteredProvider` call made inside the block is recorded
    with this user / case and counted against the per-question cap. It is a
    context variable: code that fans out to threads must run them inside a copy
    of the context (``contextvars.copy_context()``) or their calls are recorded
    without user / case and outside the cap.
    """
    contexto = ContextoConsumo(user_id=user_id, case_id=case_id)
    token = _CONTEXTO.set(contexto)
    try:
        yield contexto
    finally:
        _CONTEXTO.reset(token)


def _capturar(usage: Usage) -> None:
    captura = _CAPTURA.get()
    if captura is not None:
        captura.append(usage)


def _usage_de_turno(turn: Any, provider: LLMProvider) -> Usage | None:
    """Usage from ``ModelTurn.usage``, for providers without an ``on_usage`` hook."""
    datos = getattr(turn, "usage", None)
    if not isinstance(datos, dict) or not datos:
        return None

    def numero(clave: str) -> int:
        valor = datos.get(clave)
        return valor if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0 else 0

    return Usage(
        provider=provider.name,
        model=provider.model,
        input_tokens=numero("input_tokens"),
        output_tokens=numero("output_tokens"),
        cache_read_tokens=numero("cache_read_input_tokens"),
        cache_write_tokens=numero("cache_creation_input_tokens"),
    )


def _instalar_captura(provider: Any) -> None:
    """Point ``provider.on_usage`` at :func:`_capturar`, once, keeping a hook someone else set."""
    if not hasattr(provider, "on_usage"):
        return
    previo = provider.on_usage
    if previo is None:
        provider.on_usage = _capturar
    elif previo is not _capturar and not getattr(previo, "captura_consumo", False):

        def ambos(usage: Usage) -> None:
            previo(usage)
            _capturar(usage)

        ambos.captura_consumo = True  # type: ignore[attr-defined]
        provider.on_usage = ambos


# --- Metering wrapper ---------------------------------------------------------


class MeteredProvider:
    """An ``LLMProvider`` that checks the budget before each call and records it after.

    Results (and exceptions) are exactly those of the wrapped provider; the only
    added behaviour is :class:`PresupuestoAgotadoError` when a limit is reached.

    ``user_id`` / ``case_id`` and the per-question call count come from the
    active :func:`contexto_consumo` when there is one. Without it they come from
    this wrapper: build one wrapper per question, or pass ``compartido=True``
    for a long-lived wrapper, in which case calls made outside a context are
    recorded without user / case and are not subject to the per-question cap
    (a counter that never resets would end up blocking every call).
    """

    def __init__(
        self,
        provider: LLMProvider,
        store: ConsumoStore,
        *,
        user_id: str | None = None,
        case_id: str | None = None,
        purpose: str,
        presupuesto: Presupuesto | None = None,
        compartido: bool = False,
        _contexto: ContextoConsumo | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._presupuesto = presupuesto
        self._compartido = compartido
        self._propio = _contexto or ContextoConsumo(user_id=user_id, case_id=case_id)
        self._avisados: set[str] = set()
        self.purpose = purpose
        self.name = provider.name
        self.model = provider.model
        # Providers with an ``on_usage`` hook report every API call they make
        # (a call may be more than one request); the others are measured from
        # ``ModelTurn.usage``. The hook is the same function for every wrapper,
        # so two wrappers can share one provider.
        _instalar_captura(provider)

    def __getattr__(self, name: str) -> Any:
        # Anything else (e.g. ``effort``) is the wrapped provider's.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._provider, name)

    @property
    def wrapped(self) -> LLMProvider:
        return self._provider

    def con_proposito(self, purpose: str) -> MeteredProvider:
        """A sibling wrapper recording under another purpose (same budget and question counter)."""
        return MeteredProvider(
            self._provider,
            self._store,
            purpose=purpose,
            presupuesto=self._presupuesto,
            compartido=self._compartido,
            _contexto=self._propio,
        )

    def nueva_pregunta(self, *, user_id: str | None = None, case_id: str | None = None) -> None:
        """Start counting a new question on this wrapper (when no :func:`contexto_consumo` is used)."""
        with self._propio._lock:
            self._propio.user_id = user_id
            self._propio.case_id = case_id
            self._propio.llamadas = 0

    def _contexto_activo(self) -> tuple[ContextoConsumo, bool]:
        """The context to charge and whether its call count is per question."""
        ambiente = _CONTEXTO.get()
        if ambiente is not None:
            return ambiente, True
        return self._propio, not self._compartido

    def _medir[T](self, llamada: Callable[[], T], usage_de: Callable[[T], Usage | None] | None = None) -> T:
        contexto, cuenta = self._contexto_activo()
        with contexto._lock:
            user_id, case_id = contexto.user_id, contexto.case_id
            if self._presupuesto is not None:
                self._presupuesto.comprobar(user_id, case_id, llamadas_en_pregunta=contexto.llamadas if cuenta else 0)
            contexto.llamadas += 1
        captura: list[Usage] = []
        token = _CAPTURA.set(captura)
        try:
            resultado = llamada()
        except BaseException:
            _CAPTURA.reset(token)
            # A failed call may still have billed requests (e.g. the first of two).
            self._registrar(captura, user_id, case_id)
            raise
        _CAPTURA.reset(token)
        if not captura:
            usage = usage_de(resultado) if usage_de is not None else None
            # The call happened even if nobody said how big it was.
            captura.append(usage or Usage(provider=self.name, model=self.model, reported=False))
        self._registrar(captura, user_id, case_id)
        return resultado

    def _registrar(self, usages: list[Usage], user_id: str | None, case_id: str | None) -> None:
        for usage in usages:
            try:
                coste = self._store.registrar(replace(usage, purpose=self.purpose), user_id=user_id, case_id=case_id)
            except Exception:  # the answer is already paid for: never lose it over bookkeeping
                logger.warning("could not record LLM usage", exc_info=True)
                continue
            if (
                coste is None
                and self._presupuesto is not None
                and self._presupuesto.limita_en_usd
                and usage.model not in self._avisados
            ):
                self._avisados.add(usage.model)
                logger.warning(
                    "LLM call to %s/%s recorded without cost (no usage data or no price in the table): "
                    "it does not count towards the daily USD limits.",
                    usage.provider,
                    usage.model,
                )

    def start(
        self,
        *,
        system: str,
        tools: list[ToolSpec],
        history: list[dict[str, str]],
        user_message: str,
    ) -> MeteredConversation:
        conversation = self._provider.start(system=system, tools=tools, history=history, user_message=user_message)
        return MeteredConversation(self, conversation)

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        attachments: Sequence[Attachment] | None = None,
    ) -> dict[str, Any]:
        # ``attachments`` is only forwarded when given, so providers written
        # before it existed keep working.
        extra: dict[str, Any] = {} if attachments is None else {"attachments": attachments}
        return self._medir(
            lambda: self._provider.complete_json(
                system=system, user=user, schema=schema, schema_name=schema_name, **extra
            )
        )


class MeteredConversation:
    """A conversation whose every ``step`` is budget-checked and recorded."""

    def __init__(self, metered: MeteredProvider, conversation: Conversation) -> None:
        self._metered = metered
        self._conversation = conversation

    def step(self, *, allow_tools: bool = True, on_text: TextCallback | None = None) -> ModelTurn:
        return self._metered._medir(
            lambda: self._conversation.step(allow_tools=allow_tools, on_text=on_text),
            lambda turn: _usage_de_turno(turn, self._metered.wrapped),
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._conversation.add_tool_results(results)

    def add_user_message(self, text: str) -> None:
        self._conversation.add_user_message(text)


def medir(provider: LLMProvider, purpose: str) -> MeteredProvider:
    """Wrap a long-lived provider with the default store and the limits from settings.

    For the places that build a provider once per process. Attribute each
    question with :func:`contexto_consumo` around the code that answers it.
    """
    store = almacen_por_defecto()
    return MeteredProvider(
        provider, store, purpose=purpose, presupuesto=Presupuesto.desde_settings(store), compartido=True
    )
