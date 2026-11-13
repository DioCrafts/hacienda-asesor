"""Format drift: notice when an external format stops being understood.

Every parser of an external format (the BOE's open-data JSON and XML, its
consolidated-law pages, the crawlers' listings, the user's documents) rests on
assumptions that nobody is told about when they break: the usual symptom is an
empty result that looks like "nothing new today". This module makes those
assumptions explicit and checks them on every real read:

* a :class:`Fuente` declares, for one format, its cheap invariants
  ("canaries"), how to trim a payload into a small sample, and how to replay
  the extraction on a sample;
* :func:`observar` is called by the real parsers with what they just read. It
  evaluates the canaries, records the outcome in a small JSON state file (same
  pattern as :mod:`hacienda_gpt.ops.vigia`) and keeps a rotating known-good
  sample and the latest bad one per source;
* :func:`calcular_estado` turns the recent observations into ``ok`` /
  ``degradada`` / ``rota``, and :func:`problemas` phrases it for ``ops doctor``;
* the format knowledge of the instrumented parsers lives in declarative,
  versioned specs (:func:`spec_activa`); :func:`ensayar` is the deterministic
  gate that replays a candidate spec on the known-good sample (must reproduce
  the stored extraction) and on the bad one (must satisfy the canaries);
* :class:`Propuesta` records what the diagnosis agent
  (:mod:`hacienda_gpt.ops.formatos_agente`) proposed, and :func:`aplicar`,
  :func:`rechazar` and :func:`confirmar` are the only ways a spec changes.

Nothing here calls an LLM and nothing here may break a parser: every entry
point used from the parsers swallows its own errors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import re
import statistics
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hacienda_gpt import settings

logger = logging.getLogger(__name__)

Estado = Literal["ok", "degradada", "rota", "sin_datos"]
Gravedad = Literal["grave", "leve"]
Politica = Literal["consecutivos", "tasa"]
# Same words as the parameter re-encoding loop (hacienda_gpt/tax/recodificacion), plus "confirmada".
EstadoPropuesta = Literal["propuesta", "aplicada", "rechazada", "sin_converger", "confirmada"]
TipoPropuesta = Literal["spec", "informe"]

_SCHEMA_VERSION = 1
MAX_OBSERVACIONES = 40
# Consecutive observations with a grave failure before a source is "rota".
ROTA_TRAS = 2
# A good sample is replaced at most this often (the previous one is kept too).
ROTACION_MUESTRA = timedelta(hours=24)
MAX_MUESTRA_CHARS = 60_000
MIN_BASE = 5
SPECS_INCORPORADAS = Path(__file__).resolve().parent / "data" / "formatos"
MAX_PATRON = 600


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class Fallo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canario: str
    gravedad: Gravedad
    # Shown to the operator: Spanish.
    detalle: str


class Lectura(BaseModel):
    """What the canaries say about one payload."""

    model_config = ConfigDict(extra="forbid")

    fallos: list[Fallo] = Field(default_factory=list)
    metricas: dict[str, float] = Field(default_factory=dict)

    @property
    def graves(self) -> list[Fallo]:
        return [fallo for fallo in self.fallos if fallo.gravedad == "grave"]


class Observacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    momento: str
    ok: bool
    fallos: list[Fallo] = Field(default_factory=list)
    metricas: dict[str, float] = Field(default_factory=dict)
    contexto: str = ""
    spec_version: int | None = None


class Spec(BaseModel):
    """Declarative extraction rules of one source, versioned."""

    model_config = ConfigDict(extra="forbid")

    fuente: str
    version: int
    descripcion: str = ""
    # "incorporada" for the spec shipped with the code; "propuesta <id>" otherwise.
    origen: str = "incorporada"
    reglas: dict[str, Any]


class Muestra(BaseModel):
    """A trimmed payload kept on disk, with what it is evidence of."""

    model_config = ConfigDict(extra="forbid")

    capturada: str
    contexto: str = ""
    spec_version: int | None = None
    # Good samples: the extraction the spec in force produced from ``texto``.
    esperado: Any = None
    fallos: list[Fallo] = Field(default_factory=list)
    texto: str = ""


class Ensayo(BaseModel):
    """Outcome of the deterministic gate for one candidate set of rules."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    motivos: list[str] = Field(default_factory=list)
    reproduce_muestra_buena: bool | None = None
    cumple_en_muestra_nueva: bool | None = None


class Propuesta(BaseModel):
    """What a diagnosis ended in: a new spec to review, or a report for a developer."""

    model_config = ConfigDict(extra="forbid")

    id: str
    fuente: str
    creada: str
    tipo: TipoPropuesta
    # "sin_converger": a report for a developer (no spec could be proposed).
    estado: EstadoPropuesta = "sin_converger"
    # Identifies the failure diagnosed (failing canaries + bad sample), to avoid diagnosing it twice.
    firma: str = ""
    que_ha_cambiado: str = ""
    causa: str = ""
    justificacion: str = ""
    reflexion: list[str] = Field(default_factory=list)
    spec_base_version: int | None = None
    reglas_propuestas: dict[str, Any] | None = None
    ensayo: Ensayo | None = None
    intentos: list[dict[str, Any]] = Field(default_factory=list)
    informe: str | None = None
    llamadas_llm: int = 0
    aplicada_el: str | None = None
    aplicada_por: str | None = None
    spec_version_aplicada: int | None = None
    resuelta_el: str | None = None
    nota: str = ""


Recortar = Callable[[str], str]
Extraer = Callable[[Any, str], Any]
# (extraction, sample text, context, rules in force or under trial) → what the canaries say.
Canarios = Callable[[Any, str, str, Any], Lectura]
EnsayoPropio = Callable[[dict[str, Any], "str | Path | None"], "Ensayo"]


@dataclass(frozen=True)
class Fuente:
    """One external format the app reads, and how to tell it is still understood."""

    clave: str
    # Spanish: shown in reports and sent to the diagnosis agent.
    descripcion: str
    expectativas: tuple[str, ...]
    canarios: Canarios
    # Pydantic model of the spec's rules; None when the parser has no declarative spec.
    reglas: type[BaseModel] | None = None
    extraer: Extraer | None = None
    recortar: Recortar = lambda crudo: crudo[:MAX_MUESTRA_CHARS]
    # (metric, fraction): a drop of more than ``fraction`` against the recent median is a failure.
    caidas: tuple[tuple[str, float], ...] = ()
    politica: Politica = "consecutivos"
    # False for sources whose payloads must never be stored (the user's documents).
    guarda_muestras: bool = True
    # Where the parser lives, for the developer report.
    codigo: str = ""
    # A gate of its own, for sources whose spec is not an extraction replayed on samples.
    ensayo: EnsayoPropio | None = None
    # False: a proposal for this source is never applied without a person, whatever FORMAT_AUTO_APPLY says.
    auto_aplicable: bool = True


class FormatoError(RuntimeError):
    """An operation on specs or proposals that cannot be carried out (message in Spanish)."""


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_REGISTRO: dict[str, Fuente] = {}
_cargado = False


def registrar(fuente: Fuente) -> Fuente:
    _REGISTRO[fuente.clave] = fuente
    return fuente


def _cargar_registro() -> None:
    global _cargado
    if not _cargado:
        _cargado = True
        from hacienda_gpt.ops import formatos_fuentes  # noqa: F401 - registers the sources on import


def fuentes() -> dict[str, Fuente]:
    _cargar_registro()
    return dict(_REGISTRO)


def fuente_de(clave: str) -> Fuente:
    _cargar_registro()
    try:
        return _REGISTRO[clave]
    except KeyError as exc:
        raise FormatoError(f"Fuente desconocida: {clave}. Conocidas: {', '.join(sorted(_REGISTRO))}.") from exc


# --------------------------------------------------------------------------- #
# Paths and small helpers
# --------------------------------------------------------------------------- #


def _base(directorio: str | Path | None = None) -> Path:
    return Path(directorio) if directorio is not None else Path(settings.FORMAT_DIR)


def _state_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else Path(settings.FORMAT_STATE_PATH)


def _ahora(ahora: datetime | None = None) -> datetime:
    moment = ahora or datetime.now(UTC)
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


def _escribir(path: Path, texto: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    tmp.replace(path)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"


def canonico(value: Any) -> Any:
    """``value`` as plain JSON data, so two extractions can be compared exactly."""
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def patron_seguro(patron: str) -> str:
    """Validate a regular expression that comes from a spec; return it unchanged.

    A spec may be written by an LLM, so beyond compiling, the pattern must be
    short and free of the constructs behind catastrophic backtracking (a
    quantified group that itself contains a quantifier) and of backreferences.
    This is a heuristic, not a proof: it is one reason specs are reviewed by a
    person unless auto-apply is switched on.
    """
    if not isinstance(patron, str) or not patron or len(patron) > MAX_PATRON:
        raise ValueError(f"patrón vacío o de más de {MAX_PATRON} caracteres")
    if re.search(r"\\[1-9]|\(\?P=", patron):
        raise ValueError("el patrón usa retrorreferencias")
    if re.search(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d+,\d*\})", patron):
        raise ValueError("el patrón tiene un grupo cuantificado que contiene otro cuantificador")
    try:
        re.compile(patron)
    except re.error as exc:
        raise ValueError(f"el patrón no compila: {exc}") from exc
    return patron


# --------------------------------------------------------------------------- #
# Specs
# --------------------------------------------------------------------------- #

_spec_cache: dict[Path, tuple[float, Spec]] = {}
_reglas_cache: dict[tuple[str, int, str], BaseModel] = {}


def _leer_spec(path: Path) -> Spec | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _spec_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    spec = Spec(**json.loads(path.read_text(encoding="utf-8")))
    _spec_cache[path] = (mtime, spec)
    return spec


def spec_incorporada(clave: str) -> Spec:
    """The spec shipped with the code (version 1: the behaviour the parsers always had)."""
    spec = _leer_spec(SPECS_INCORPORADAS / f"{clave}.json")
    if spec is None:
        raise FormatoError(f"No hay spec incorporada para {clave}.")
    return spec


def _ruta_spec(clave: str, directorio: str | Path | None = None) -> Path:
    return _base(directorio) / "specs" / f"{clave}.json"


def spec_activa(clave: str, directorio: str | Path | None = None) -> Spec:
    """The spec in force: the applied one under ``FORMAT_DIR/specs`` if valid, else the bundled one.

    An unreadable or outdated override never breaks a parser: it is logged and
    the bundled spec is used.
    """
    incorporada = spec_incorporada(clave)
    try:
        aplicada = _leer_spec(_ruta_spec(clave, directorio))
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("Applied format spec for %s is unreadable, using the bundled one: %s", clave, exc)
        return incorporada
    if aplicada is None or aplicada.fuente != clave or aplicada.version <= incorporada.version:
        return incorporada
    return aplicada


def reglas_de[M: BaseModel](clave: str, modelo: type[M], directorio: str | Path | None = None) -> M:
    """The rules of the spec in force, validated; the bundled rules if the applied ones do not validate."""
    spec = spec_activa(clave, directorio)
    key = (clave, spec.version, spec.origen)
    cached = _reglas_cache.get(key)
    if isinstance(cached, modelo):
        return cached
    try:
        reglas = modelo(**spec.reglas)
    except ValidationError as exc:
        logger.warning("Format spec %s v%s does not validate, using the bundled one: %s", clave, spec.version, exc)
        return modelo(**spec_incorporada(clave).reglas)
    _reglas_cache[key] = reglas
    return reglas


def _reglas_actuales(fuente: Fuente, directorio: str | Path | None = None) -> tuple[BaseModel | None, int | None]:
    if fuente.reglas is None:
        return None, None
    return reglas_de(fuente.clave, fuente.reglas, directorio), spec_activa(fuente.clave, directorio).version


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #


@contextmanager
def _locked(target: Path) -> Iterator[None]:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.with_name(target.name + ".lock"), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def leer_estado(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Per-source state (``{}`` when nothing has been observed yet). Raises ValueError if unreadable."""
    target = _state_path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))["fuentes"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{target}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(entry, dict) for entry in data.values()):
        raise ValueError(f"{target}: unexpected structure")
    return data


def _modificar_estado(path: str | Path | None, cambio: Callable[[dict[str, dict[str, Any]]], None]) -> None:
    target = _state_path(path)
    with _locked(target):
        try:
            data = leer_estado(target)
        except ValueError as exc:
            logger.warning("Unreadable format state, starting a new one: %s", exc)
            target.replace(target.with_name(target.name + ".corrupt"))
            data = {}
        cambio(data)
        payload = {"schema_version": _SCHEMA_VERSION, "updated_at": _ahora().isoformat(), "fuentes": data}
        _escribir(target, _json(payload))


def observaciones_de(entrada: dict[str, Any] | None) -> list[Observacion]:
    out: list[Observacion] = []
    for raw in (entrada or {}).get("observaciones") or []:
        try:
            out.append(Observacion(**raw))
        except (TypeError, ValidationError):
            continue
    return out


def calcular_estado(observaciones: Sequence[Observacion], politica: Politica = "consecutivos") -> Estado:
    """Status of a source from its recent observations.

    ``consecutivos`` (structured sources): ``ok`` when the last read passed;
    ``rota`` when the last :data:`ROTA_TRAS` reads failed a grave canary, or a
    grave one failed and the source was never read well; ``degradada``
    otherwise. ``tasa`` (the user's documents, where one empty read means
    little): by the share of failed reads among the last ten.
    """
    if not observaciones:
        return "sin_datos"
    if politica == "tasa":
        ventana = list(observaciones)[-10:]
        tasa = sum(not obs.ok for obs in ventana) / len(ventana)
        if len(ventana) >= 5 and tasa >= 0.8:
            return "rota"
        if len(ventana) >= 4 and tasa >= 0.5:
            return "degradada"
        return "ok"
    if observaciones[-1].ok:
        return "ok"
    graves = 0
    for obs in reversed(observaciones):
        if obs.ok or not any(fallo.gravedad == "grave" for fallo in obs.fallos):
            break
        graves += 1
    nunca_bien = all(not obs.ok for obs in observaciones)
    if graves >= ROTA_TRAS or (graves >= 1 and nunca_bien):
        return "rota"
    return "degradada"


def _caidas(fuente: Fuente, previas: Sequence[Observacion], metricas: dict[str, float]) -> list[Fallo]:
    """Baseline canary: a metric far below the median of the recent good reads."""
    fallos: list[Fallo] = []
    for metrica, fraccion in fuente.caidas:
        if metrica not in metricas:
            continue
        base = [obs.metricas[metrica] for obs in previas if obs.ok and metrica in obs.metricas]
        if len(base) < MIN_BASE:
            continue
        mediana = statistics.median(base)
        if mediana > 0 and metricas[metrica] < mediana * (1 - fraccion):
            fallos.append(
                Fallo(
                    canario=f"caida_{metrica}",
                    gravedad="leve",
                    detalle=(
                        f"{metrica} = {metricas[metrica]:g}, más de un {fraccion:.0%} por debajo de la mediana "
                        f"reciente ({mediana:g}, {len(base)} lecturas)."
                    ),
                )
            )
    return fallos


# --------------------------------------------------------------------------- #
# Samples
# --------------------------------------------------------------------------- #


def _ruta_muestra(clave: str, nombre: str, directorio: str | Path | None = None) -> Path:
    return _base(directorio) / "muestras" / clave / f"{nombre}.json"


def leer_muestra(clave: str, nombre: str, directorio: str | Path | None = None) -> Muestra | None:
    """``nombre``: "buena", "buena_anterior" or "mala"."""
    path = _ruta_muestra(clave, nombre, directorio)
    try:
        return Muestra(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError):
        return None


def guardar_muestra(clave: str, nombre: str, muestra: Muestra, directorio: str | Path | None = None) -> Path:
    path = _ruta_muestra(clave, nombre, directorio)
    _escribir(path, _json(muestra.model_dump(mode="json")))
    return path


def evaluar(fuente: Fuente, reglas: BaseModel | None, texto: str, contexto: str) -> tuple[Any, Lectura]:
    """Replay the extraction (if the source has one) on ``texto`` and run the canaries. Never raises."""
    extraccion: Any = None
    if fuente.extraer is not None:
        try:
            extraccion = canonico(fuente.extraer(reglas, texto))
        except Exception as exc:
            fallo = Fallo(canario="lectura", gravedad="grave", detalle=f"No se pudo leer: {type(exc).__name__}: {exc}")
            return None, Lectura(fallos=[fallo])
    try:
        return extraccion, fuente.canarios(extraccion, texto, contexto, reglas)
    except Exception as exc:
        fallo = Fallo(
            canario="canarios", gravedad="grave", detalle=f"Los canarios fallaron: {type(exc).__name__}: {exc}"
        )
        return extraccion, Lectura(fallos=[fallo])


def _texto(crudo: str | bytes | None) -> str:
    if crudo is None:
        return ""
    return crudo.decode("utf-8", errors="replace") if isinstance(crudo, bytes | bytearray) else str(crudo)


def _recortada(fuente: Fuente, texto: str) -> str:
    try:
        return fuente.recortar(texto)[:MAX_MUESTRA_CHARS]
    except Exception:
        return texto[:MAX_MUESTRA_CHARS]


def _guardar_muestras(
    fuente: Fuente,
    texto: str,
    lectura: Lectura,
    contexto: str,
    momento: datetime,
    directorio: str | Path | None,
) -> None:
    reglas, version = _reglas_actuales(fuente, directorio)
    recorte = _recortada(fuente, texto)
    if lectura.fallos:
        # Baseline drops are judged on the whole payload: the sample only has to carry the grave ones.
        guardar_muestra(
            fuente.clave,
            "mala",
            Muestra(
                capturada=momento.isoformat(),
                contexto=contexto,
                spec_version=version,
                fallos=lectura.fallos,
                texto=recorte,
            ),
            directorio,
        )
        return
    actual = leer_muestra(fuente.clave, "buena", directorio)
    if actual is not None and actual.spec_version == version:
        try:
            if momento - datetime.fromisoformat(actual.capturada) < ROTACION_MUESTRA:
                return
        except ValueError:
            pass
    esperado, en_recorte = evaluar(fuente, reglas, recorte, contexto)
    if en_recorte.fallos:
        # The trimmed sample must stand on its own, or the gate could never be passed.
        return
    if actual is not None:
        guardar_muestra(fuente.clave, "buena_anterior", actual, directorio)
    guardar_muestra(
        fuente.clave,
        "buena",
        Muestra(
            capturada=momento.isoformat(), contexto=contexto, spec_version=version, esperado=esperado, texto=recorte
        ),
        directorio,
    )


# --------------------------------------------------------------------------- #
# Observing
# --------------------------------------------------------------------------- #

_SIN_EXTRACCION: Any = object()


def observar(
    clave: str,
    crudo: str | bytes | None,
    *,
    extraccion: Any = _SIN_EXTRACCION,
    contexto: str = "",
    fallos_extra: Sequence[Fallo] = (),
    metricas_extra: dict[str, float] | None = None,
    ahora: datetime | None = None,
    state_path: str | Path | None = None,
    directorio: str | Path | None = None,
    forzar: bool = False,
) -> Observacion | None:
    """Record what a parser just read. Called from the real parsers: NEVER raises.

    ``extraccion`` is what the parser obtained (plain data, in the shape the
    source's ``extraer`` returns); when omitted it is recomputed from ``crudo``.
    Does nothing unless ``FORMAT_WATCH`` is on (``forzar`` is for tests and the CLI).
    """
    if not (forzar or settings.FORMAT_WATCH):
        return None
    try:
        return _observar(
            fuente_de(clave),
            _texto(crudo),
            extraccion=extraccion,
            contexto=contexto,
            fallos_extra=fallos_extra,
            metricas_extra=metricas_extra or {},
            ahora=_ahora(ahora),
            state_path=state_path,
            directorio=directorio,
        )
    except Exception:
        logger.warning("Could not record the format observation for %s", clave, exc_info=True)
        return None


def _observar(
    fuente: Fuente,
    texto: str,
    *,
    extraccion: Any,
    contexto: str,
    fallos_extra: Sequence[Fallo],
    metricas_extra: dict[str, float],
    ahora: datetime,
    state_path: str | Path | None,
    directorio: str | Path | None,
) -> Observacion:
    reglas, version = _reglas_actuales(fuente, directorio)
    if extraccion is _SIN_EXTRACCION:
        _, lectura = evaluar(fuente, reglas, texto, contexto)
    else:
        try:
            lectura = fuente.canarios(canonico(extraccion), texto, contexto, reglas)
        except Exception as exc:
            detalle = f"Los canarios fallaron: {type(exc).__name__}: {exc}"
            lectura = Lectura(fallos=[Fallo(canario="canarios", gravedad="grave", detalle=detalle)])
    lectura = Lectura(fallos=[*lectura.fallos, *fallos_extra], metricas={**lectura.metricas, **metricas_extra})
    resultado: list[Observacion] = []

    def cambio(data: dict[str, dict[str, Any]]) -> None:
        entrada = data.setdefault(fuente.clave, {})
        previas = observaciones_de(entrada)
        fallos = [*lectura.fallos, *_caidas(fuente, previas, lectura.metricas)]
        obs = Observacion(
            momento=ahora.isoformat(),
            ok=not fallos,
            fallos=fallos,
            metricas=lectura.metricas,
            contexto=contexto[:200],
            spec_version=version,
        )
        resultado.append(obs)
        todas = [*previas, obs][-MAX_OBSERVACIONES:]
        estado = calcular_estado(todas, fuente.politica)
        if entrada.get("estado") != estado:
            entrada["estado_desde"] = obs.momento
        entrada["estado"] = estado
        entrada["observaciones"] = [item.model_dump(mode="json") for item in todas]
        if obs.ok:
            entrada["ultima_ok"] = obs.momento
        else:
            entrada["ultimo_fallo"] = obs.momento

    _modificar_estado(state_path, cambio)
    obs = resultado[0]
    if fuente.guarda_muestras and texto:
        _guardar_muestras(fuente, texto, Lectura(fallos=obs.fallos, metricas=obs.metricas), contexto, ahora, directorio)
    return obs


# --------------------------------------------------------------------------- #
# Reading the status
# --------------------------------------------------------------------------- #


class EstadoFuente(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fuente: str
    descripcion: str
    estado: Estado
    desde: str | None = None
    ultima_lectura: str | None = None
    ultima_ok: str | None = None
    spec_version: int | None = None
    fallos: list[Fallo] = Field(default_factory=list)
    propuestas_pendientes: list[str] = Field(default_factory=list)


def resumen(state_path: str | Path | None = None, directorio: str | Path | None = None) -> list[EstadoFuente]:
    """Status of every registered source (``sin_datos`` for those never read)."""
    data = leer_estado(state_path)
    pendientes: dict[str, list[str]] = {}
    for propuesta in listar_propuestas(directorio):
        if propuesta.estado in ("propuesta", "aplicada", "sin_converger"):
            pendientes.setdefault(propuesta.fuente, []).append(propuesta.id)
    out: list[EstadoFuente] = []
    for clave, fuente in sorted(fuentes().items()):
        entrada = data.get(clave)
        observaciones = observaciones_de(entrada)
        estado = calcular_estado(observaciones, fuente.politica)
        ultima = observaciones[-1] if observaciones else None
        fallos: list[Fallo] = []
        if estado in ("degradada", "rota"):
            fallos = next((obs.fallos for obs in reversed(observaciones) if not obs.ok), [])
        version = spec_activa(clave, directorio).version if fuente.reglas is not None else None
        out.append(
            EstadoFuente(
                fuente=clave,
                descripcion=fuente.descripcion,
                estado=estado,
                desde=(entrada or {}).get("estado_desde"),
                ultima_lectura=ultima.momento if ultima else None,
                ultima_ok=(entrada or {}).get("ultima_ok"),
                spec_version=version,
                fallos=fallos,
                propuestas_pendientes=pendientes.get(clave, []),
            )
        )
    return out


def problemas(state_path: str | Path | None = None, directorio: str | Path | None = None) -> list[str]:
    """Sentences in Spanish for ``ops doctor`` (empty: every observed format is understood)."""
    try:
        estados = resumen(state_path, directorio)
    except ValueError as exc:
        return [f"formatos: no se puede leer el estado ({exc})."]
    out: list[str] = []
    for item in estados:
        if item.estado not in ("degradada", "rota"):
            continue
        detalle = "; ".join(f"{fallo.canario}: {fallo.detalle}" for fallo in item.fallos[:3]) or "sin detalle"
        cola = (
            f" Propuestas por revisar: {', '.join(item.propuestas_pendientes)}." if item.propuestas_pendientes else ""
        )
        out.append(
            f"formato {item.fuente} ({item.descripcion}): {item.estado.upper()} desde {item.desde or '?'}. {detalle}{cola}"
        )
    return out


def firma_del_fallo(clave: str, directorio: str | Path | None = None) -> str:
    """Stable id of the failure currently on record for a source (failing canaries + bad sample)."""
    mala = leer_muestra(clave, "mala", directorio)
    canarios = sorted(fallo.canario for fallo in mala.fallos) if mala else []
    spec = spec_activa(clave, directorio).version if fuente_de(clave).reglas is not None else 0
    cuerpo = json.dumps([clave, spec, canarios, hashlib.sha256((mala.texto if mala else "").encode()).hexdigest()])
    return hashlib.sha256(cuerpo.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def ensayar(clave: str, reglas_propuestas: dict[str, Any], directorio: str | Path | None = None) -> Ensayo:
    """Deterministic gate for a candidate spec. Passing it is necessary to apply a proposal.

    1. the rules validate against the source's model (unknown keys refused);
    2. they differ from the rules in force;
    3. replayed on every known-good sample they reproduce the stored extraction exactly;
    4. replayed on the new (bad) sample, no canary fails.
    """
    fuente = fuente_de(clave)
    if fuente.ensayo is not None:
        return fuente.ensayo(reglas_propuestas, directorio)
    if fuente.reglas is None or fuente.extraer is None:
        return Ensayo(ok=False, motivos=["La fuente no tiene spec declarativa: no admite adaptación por datos."])
    try:
        reglas = fuente.reglas(**reglas_propuestas)
    except (ValidationError, TypeError) as exc:
        return Ensayo(ok=False, motivos=[f"Las reglas no son válidas para esta fuente: {exc}"])
    actuales, _ = _reglas_actuales(fuente, directorio)
    if actuales is not None and canonico(reglas.model_dump()) == canonico(actuales.model_dump()):
        return Ensayo(ok=False, motivos=["Las reglas propuestas son idénticas a las vigentes."])

    motivos: list[str] = []
    buenas = [m for m in (leer_muestra(clave, n, directorio) for n in ("buena", "buena_anterior")) if m is not None]
    reproduce: bool | None = None
    if not buenas:
        motivos.append("No hay muestra buena guardada: no se puede comprobar que lo anterior se sigue leyendo igual.")
    for muestra in buenas:
        extraccion, lectura = evaluar(fuente, reglas, muestra.texto, muestra.contexto)
        igual = not lectura.fallos and extraccion == canonico(muestra.esperado)
        reproduce = igual if reproduce is None else (reproduce and igual)
        if not igual:
            causa = "; ".join(f.detalle for f in lectura.fallos) or "la extracción difiere de la guardada"
            motivos.append(f"No reproduce la muestra buena del {muestra.capturada[:10]}: {causa}.")

    mala = leer_muestra(clave, "mala", directorio)
    cumple: bool | None = None
    if mala is None:
        motivos.append("No hay muestra nueva guardada contra la que ensayar.")
    else:
        _, lectura = evaluar(fuente, reglas, mala.texto, mala.contexto)
        cumple = not lectura.fallos
        if not cumple:
            motivos.append(
                "En la muestra nueva siguen fallando: "
                + "; ".join(f"{f.canario} ({f.detalle})" for f in lectura.fallos)
            )
    return Ensayo(ok=not motivos, motivos=motivos, reproduce_muestra_buena=reproduce, cumple_en_muestra_nueva=cumple)


# --------------------------------------------------------------------------- #
# Proposals
# --------------------------------------------------------------------------- #


def _ruta_propuesta(propuesta_id: str, directorio: str | Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", propuesta_id):
        raise FormatoError(f"Identificador de propuesta no válido: {propuesta_id!r}.")
    return _base(directorio) / "propuestas" / f"{propuesta_id}.json"


def guardar_propuesta(propuesta: Propuesta, directorio: str | Path | None = None) -> Path:
    path = _ruta_propuesta(propuesta.id, directorio)
    _escribir(path, _json(propuesta.model_dump(mode="json")))
    return path


def leer_propuesta(propuesta_id: str, directorio: str | Path | None = None) -> Propuesta:
    path = _ruta_propuesta(propuesta_id, directorio)
    try:
        return Propuesta(**json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise FormatoError(f"No existe la propuesta {propuesta_id}.") from exc
    except (ValueError, ValidationError) as exc:
        raise FormatoError(f"La propuesta {propuesta_id} no se puede leer: {exc}") from exc


def listar_propuestas(directorio: str | Path | None = None) -> list[Propuesta]:
    out: list[Propuesta] = []
    for path in sorted((_base(directorio) / "propuestas").glob("*.json")):
        try:
            out.append(Propuesta(**json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, ValidationError):
            logger.warning("Unreadable format proposal: %s", path)
    return out


def nuevo_id(clave: str, ahora: datetime | None = None, directorio: str | Path | None = None) -> str:
    base = f"{_ahora(ahora).strftime('%Y%m%dT%H%M%S')}-{clave}"
    candidato, n = base, 1
    while _ruta_propuesta(candidato, directorio).exists():
        n += 1
        candidato = f"{base}-{n}"
    return candidato


def aplicar(
    propuesta_id: str,
    *,
    por: str = "manual",
    ahora: datetime | None = None,
    directorio: str | Path | None = None,
) -> Propuesta:
    """Put a proposed spec in force. The gate is run again here: nothing is applied on a stale result."""
    propuesta = leer_propuesta(propuesta_id, directorio)
    if propuesta.tipo != "spec" or propuesta.reglas_propuestas is None:
        raise FormatoError("Esta propuesta es un informe para un desarrollador: no contiene una spec que aplicar.")
    if propuesta.estado != "propuesta":
        raise FormatoError(
            f"La propuesta está en estado «{propuesta.estado}»; solo se aplican las que están «propuesta»."
        )
    vigente = spec_activa(propuesta.fuente, directorio)
    if propuesta.spec_base_version != vigente.version:
        raise FormatoError(
            f"La propuesta parte de la spec v{propuesta.spec_base_version}, pero la vigente es la v{vigente.version}: "
            "vuelve a diagnosticar."
        )
    ensayo = ensayar(propuesta.fuente, propuesta.reglas_propuestas, directorio)
    if not ensayo.ok:
        propuesta.ensayo = ensayo
        guardar_propuesta(propuesta, directorio)
        raise FormatoError("La propuesta no supera el ensayo determinista: " + " ".join(ensayo.motivos))
    momento = _ahora(ahora).isoformat()
    nueva = Spec(
        fuente=propuesta.fuente,
        version=vigente.version + 1,
        descripcion=propuesta.que_ha_cambiado[:500],
        origen=f"propuesta {propuesta.id}",
        reglas=propuesta.reglas_propuestas,
    )
    carpeta = _base(directorio) / "specs"
    # History first: the previous spec in force stays on disk for ``rechazar`` to restore.
    _escribir(carpeta / "historial" / f"{propuesta.fuente}.v{vigente.version}.json", _json(vigente.model_dump()))
    _escribir(_ruta_spec(propuesta.fuente, directorio), _json(nueva.model_dump()))
    propuesta.estado = "aplicada"
    propuesta.ensayo = ensayo
    propuesta.aplicada_el = momento
    propuesta.aplicada_por = por
    propuesta.spec_version_aplicada = nueva.version
    guardar_propuesta(propuesta, directorio)
    return propuesta


def rechazar(
    propuesta_id: str,
    *,
    motivo: str = "",
    ahora: datetime | None = None,
    directorio: str | Path | None = None,
) -> Propuesta:
    """Discard a proposal. If it had been applied, the previous spec is put back in force."""
    propuesta = leer_propuesta(propuesta_id, directorio)
    if propuesta.estado in ("rechazada", "confirmada"):
        raise FormatoError(f"La propuesta ya está «{propuesta.estado}».")
    if propuesta.estado == "aplicada":
        vigente = spec_activa(propuesta.fuente, directorio)
        if vigente.version != propuesta.spec_version_aplicada:
            raise FormatoError(
                f"La spec vigente (v{vigente.version}) ya no es la que aplicó esta propuesta "
                f"(v{propuesta.spec_version_aplicada}): no se puede deshacer automáticamente."
            )
        anterior = _base(directorio) / "specs" / "historial" / f"{propuesta.fuente}.v{vigente.version - 1}.json"
        previa = _leer_spec(anterior)
        ruta = _ruta_spec(propuesta.fuente, directorio)
        if previa is None or previa.version <= spec_incorporada(propuesta.fuente).version:
            ruta.unlink(missing_ok=True)
        else:
            _escribir(ruta, _json(previa.model_dump()))
        _spec_cache.pop(ruta, None)
    propuesta.estado = "rechazada"
    propuesta.resuelta_el = _ahora(ahora).isoformat()
    propuesta.nota = motivo
    guardar_propuesta(propuesta, directorio)
    return propuesta


def confirmar(
    propuesta_id: str,
    *,
    nota: str = "",
    ahora: datetime | None = None,
    directorio: str | Path | None = None,
) -> Propuesta:
    """A person has reviewed an applied spec (or read a developer report) and closes the proposal."""
    propuesta = leer_propuesta(propuesta_id, directorio)
    if propuesta.tipo == "spec" and propuesta.estado != "aplicada":
        raise FormatoError("Solo se confirma una propuesta de spec ya aplicada (usa «apply» primero).")
    if propuesta.tipo == "informe" and propuesta.estado != "sin_converger":
        raise FormatoError(f"El informe ya está «{propuesta.estado}».")
    propuesta.estado = "confirmada"
    propuesta.resuelta_el = _ahora(ahora).isoformat()
    propuesta.nota = nota
    guardar_propuesta(propuesta, directorio)
    return propuesta
