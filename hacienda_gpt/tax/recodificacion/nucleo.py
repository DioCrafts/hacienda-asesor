"""The re-encoding loop: read → deterministic gate → independent review → bounded retries → proposal.

When a group of parameters stops matching the text of its law, a model re-reads
the law and PROPOSES new data with the literal quotes that prove it. The
calculator stays deterministic: it only ever reads the parameter files, and a
proposal reaches them through :func:`aplicar_propuesta`, which re-runs every
deterministic check against the law as it reads that day.

The loop knows nothing about any particular kind of parameter. A *group* is a
JSON path in a parameter file, the pydantic model of what lives there, its
quotes and the law (BOE id and version) it is read from; everything specific
to a kind of group — its schema, its figure rules, how a test case is run
through the calculator, where its quotes are stored, how its provenance is
marked — belongs to an :class:`Adaptador`.

Two rules about time are enforced here for every adapter:

* the tax years a proposal touches are derived from the EFFECTS date of the
  wording, which must be printed in a literal quote and read the same by the
  reader and by the reviewer. An amending law published today may state
  effects from 1 January of the previous year, so a proposal can touch more
  than one year's file, and it names every one of them;
* the loop never chooses a historical version of a law by date. It reads the
  current consolidated text, or the version the operator names; a year the
  wording does not reach is left untouched and reported.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from contextvars import ContextVar
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Literal, Protocol
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.tax.params import DATA_DIR
from hacienda_gpt.tax.recodificacion import prompts
from hacienda_gpt.tax.recodificacion.control import (
    CENT,
    Control,
    TextoLegal,
    articulos_que_contienen,
    citas_no_literales,
    controlar_efectos,
    es_literal,
    extraer_articulos,
    extraer_de_modificadora,
    fecha_iso,
    indice_de,
)
from hacienda_gpt.tax.verificacion import VERSION_DIARIO, TextoDe, _comprimido

logger = logging.getLogger(__name__)

Estado = Literal["propuesta", "aplicada", "rechazada", "sin_converger"]
Resultado = Literal["recodificada", "retirada"]
MAX_ITER = 3
# Times the reader may ask for further articles of the same law before it has to answer.
MAX_AMPLIACIONES = 2
# Legal text sent to the model in one call; beyond it the group is left to a person.
MAX_CARACTERES_TEXTO = 200_000


class RecodificacionError(RuntimeError):
    """The loop or the application of a proposal cannot go on; the message is for the operator (Spanish)."""


class SinTextoError(RecodificacionError):
    """The law could not be read, or the article is not in it."""


class LeyCambiadaError(RecodificacionError):
    """The law reads differently from when the proposal was made."""


class DatosCambiadosError(RecodificacionError):
    """The parameter file no longer holds what the proposal was going to replace."""


class ControlFallidoError(RecodificacionError):
    """The proposal does not pass the deterministic gate today."""


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


class _Registro(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Objetivo(_Registro):
    """One group of parameters to re-read, in every tax year it may have to change."""

    tipo: str
    # Parameter file with the year as a placeholder, e.g. "irpf_{ejercicio}.json".
    fichero: str
    # Path inside the file; a step "campo=valor" selects the item of a list whose field has that value.
    ruta: list[str]
    etiqueta: str
    # Community key, or "estado".
    ambito: str
    boe_id: str
    # None = the current consolidated text. A historical version (AAAAMMDD) only when the operator names it.
    version: str | None = None
    # Articles of the law to read ("5", "9 quater").
    articulos_ley: list[str]
    # Tax years asked for: the proposal touches those the wording produces effects in.
    ejercicios: list[int]
    # What each year's file holds today (None: the group is not there).
    antes: dict[int, Any] = Field(default_factory=dict)
    citas_antes: list[str] = Field(default_factory=list)
    # Why it is revisited: "verificacion", "articulo" (asked by the operator), "rollover".
    origen: str = "verificacion"
    motivo: list[str] = Field(default_factory=list)
    # For a group that is an item of a list and is not in the file: the item to put it after ("" = first).
    insertar_tras: str | None = None
    # What the BOE's analysis of the amending norm says about this law ("MODIFICA…, con efectos de…").
    anotaciones: str = ""
    # A previous encoding to show the reader when no file holds the group any more (withdrawn at a rollover).
    referencia_anterior: Any = None
    # BOE id of an amending norm whose OWN text is read instead of the consolidated law, for the days the BOE
    # takes to consolidate it. Such a proposal is never applied without a person.
    norma_texto: str | None = None
    # Number of the amended law as the amending norm cites it ("35/2006"), to find the passages about it.
    ley_citada: str = ""


class Discrepancia(_Registro):
    gravedad: Literal["error", "observacion"]
    tipo: str
    descripcion: str
    cita: str = ""
    cita_literal: bool = False


class CasoEvaluado(_Registro):
    descripcion: str
    datos: Any = None
    fundamento: str = ""
    esperado: Decimal | None = None
    obtenido: Decimal | None = None
    coincide: bool | None = None
    detalle: str | None = None
    # Why the case could not be run (the reviewer's fault, not the encoding's).
    error: str | None = None


class Veredicto(_Registro):
    conforme: bool
    discrepancias: list[Discrepancia] = Field(default_factory=list)
    efectos_desde: str = ""
    cita_efectos: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class Iteracion(_Registro):
    numero: int
    resultado_lector: str
    valor: Any = None
    citas: list[str] = Field(default_factory=list)
    efectos_desde: str = ""
    efectos_hasta: str = ""
    cita_efectos: str = ""
    motivo: str = ""
    cambios: str = ""
    control: Control | None = None
    veredicto: Veredicto | None = None
    casos: list[CasoEvaluado] = Field(default_factory=list)
    objeciones: list[str] = Field(default_factory=list)


class Propuesta(_Registro):
    """What the loop concluded about one group, kept on disk whatever the outcome."""

    id: str
    estado: Estado
    resultado: Resultado | None = None
    tipo: str
    fichero: str
    ruta: list[str]
    etiqueta: str
    ambito: str
    boe_id: str
    version: str | None = None
    articulos_ley: list[str]
    sha256_html: str
    # Hash of the text of those articles (whitespace-insensitive): what "the law has not changed" means.
    sha256_texto: str
    origen: str
    motivo: list[str] = Field(default_factory=list)
    ejercicios_pedidos: list[int]
    # Every tax year whose file this proposal changes.
    ejercicios: list[int] = Field(default_factory=list)
    ejercicios_excluidos: dict[int, str] = Field(default_factory=dict)
    efectos_desde: str = ""
    efectos_hasta: str = ""
    cita_efectos: str = ""
    anotaciones: str = ""
    antes: dict[int, Any] = Field(default_factory=dict)
    citas_antes: list[str] = Field(default_factory=list)
    despues: Any = None
    citas: list[str] = Field(default_factory=list)
    motivo_lector: str = ""
    cambios: str = ""
    control: Control | None = None
    veredicto: Veredicto | None = None
    casos: list[CasoEvaluado] = Field(default_factory=list)
    objeciones_abiertas: list[str] = Field(default_factory=list)
    iteraciones: list[Iteracion] = Field(default_factory=list)
    max_iter: int
    llamadas_llm: int = 0
    caracteres_enviados: int = 0
    modelo_lector: str
    modelo_revisor: str
    insertar_tras: str | None = None
    norma_texto: str | None = None
    ley_citada: str = ""
    clave: str
    creada_el: datetime
    actualizada_el: datetime
    aplicada_el: datetime | None = None
    aplicada_automaticamente: bool = False
    rechazada_el: datetime | None = None
    motivo_rechazo: str = ""
    confirmada_el: datetime | None = None
    avisada_por: list[str] = Field(default_factory=list)

    @property
    def aplicable_sin_persona(self) -> bool:
        """Passed the gate with nothing left to look at and the reviewer agreed; never a withdrawal."""
        return (
            self.estado == "propuesta"
            and self.resultado == "recodificada"
            and self.control is not None
            and self.control.ok
            and not self.control.revisar
            and self.veredicto is not None
            and self.veredicto.conforme
        )


class AlmacenPropuestas:
    """Proposals as JSON files, one per proposal, under a directory."""

    def __init__(self, directorio: str | Path) -> None:
        self.directorio = Path(directorio)

    def guardar(self, propuesta: Propuesta) -> Path:
        self.directorio.mkdir(parents=True, exist_ok=True)
        path = self.directorio / f"{propuesta.id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(propuesta.model_dump_json(indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def leer(self, identificador: str) -> Propuesta:
        if not re.fullmatch(r"[\w.-]+", identificador):
            raise RecodificacionError(f"Identificador de propuesta no válido: {identificador!r}.")
        exacto = self.directorio / f"{identificador}.json"
        candidatos = [exacto] if exacto.exists() else sorted(self.directorio.glob(f"{identificador}*.json"))
        if len(candidatos) != 1:
            raise RecodificacionError(
                f"No hay ninguna propuesta «{identificador}»."
                if not candidatos
                else f"«{identificador}» coincide con {len(candidatos)} propuestas: indica el identificador completo."
            )
        return Propuesta.model_validate_json(candidatos[0].read_text(encoding="utf-8"))

    def listar(self, estado: str | None = None) -> list[Propuesta]:
        if not self.directorio.is_dir():
            return []
        todas = [Propuesta.model_validate_json(p.read_text(encoding="utf-8")) for p in self.directorio.glob("*.json")]
        return sorted((p for p in todas if estado is None or p.estado == estado), key=lambda p: p.creada_el)

    def con_clave(self, clave: str) -> Propuesta | None:
        return next((p for p in reversed(self.listar()) if p.clave == clave), None)


# --------------------------------------------------------------------------- #
# Paths inside a parameter file
# --------------------------------------------------------------------------- #


def _indice(lista: list[Any], paso: str) -> int | None:
    campo, _, valor = paso.partition("=")
    return next((i for i, item in enumerate(lista) if isinstance(item, dict) and item.get(campo) == valor), None)


def _bajar(nodo: Any, paso: str) -> Any:
    if isinstance(nodo, list):
        indice = _indice(nodo, paso) if "=" in paso else int(paso)
        if indice is None:
            raise KeyError(paso)
        return nodo[indice]
    return nodo[paso]


def leer_ruta(raw: dict[str, Any], ruta: Sequence[str]) -> Any:
    """What ``raw`` holds at ``ruta``; None when it is not there."""
    nodo: Any = raw
    try:
        for paso in ruta:
            nodo = _bajar(nodo, paso)
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    return nodo


def escribir_ruta(raw: dict[str, Any], ruta: Sequence[str], valor: Any, *, insertar_tras: str | None = None) -> None:
    """Put ``valor`` at ``ruta`` (None removes it). A missing list item is inserted after ``insertar_tras``.

    The last step of a path into a list is either "campo=valor" (the item whose field has that value) or a plain
    index, for lists whose items have no field that tells them apart; an index must exist.
    """
    padre: Any = raw
    for paso in ruta[:-1]:
        padre = _bajar(padre, paso)
    ultimo = ruta[-1]
    if not isinstance(padre, list):
        if valor is None:
            padre.pop(ultimo, None)
        else:
            padre[ultimo] = valor
        return
    if ultimo.isdigit():
        if int(ultimo) >= len(padre):
            raise IndexError(f"no hay elemento {ultimo} en la lista de {'/'.join(ruta[:-1])}")
        if valor is None:
            del padre[int(ultimo)]
        else:
            padre[int(ultimo)] = valor
        return
    indice = _indice(padre, ultimo)
    if valor is None:
        if indice is not None:
            del padre[indice]
    elif indice is not None:
        padre[indice] = valor
    else:
        campo = ultimo.partition("=")[0]
        tras = _indice(padre, f"{campo}={insertar_tras}") if insertar_tras else None
        if insertar_tras == "":
            padre.insert(0, valor)
        elif tras is not None:
            padre.insert(tras + 1, valor)
        else:
            padre.append(valor)


def _sangria(texto: str) -> int:
    match = re.search(r"\n( +)\S", texto)
    return len(match.group(1)) if match else 2


def _volcar(path: Path, data: Any, *, sangria: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=sangria) + "\n", encoding="utf-8")
    tmp.replace(path)


def ejercicios_en(data_dir: Path, fichero: str) -> list[int]:
    """Tax years for which the parameter file ``fichero`` ("irpf_{ejercicio}.json") exists."""
    patron = re.compile(re.escape(fichero).replace(re.escape("{ejercicio}"), r"(\d{4})"))
    return sorted(int(m.group(1)) for p in data_dir.iterdir() if (m := patron.fullmatch(p.name)))


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


class Adaptador(ABC):
    """Everything the loop needs to know about one kind of parameter group."""

    tipo: str
    # Parameter file this kind of group lives in, with the year as a placeholder.
    fichero: str
    # One sentence for the prompts: what this group is.
    que: str
    # Whether "the law no longer fits the schema" may be answered by taking the group out of the file.
    permite_retirada: bool = False
    min_casos: int = 2

    @abstractmethod
    def esquema(self) -> str:
        """The schema of the value, generated from the pydantic models the gate validates with."""

    @abstractmethod
    def guia(self) -> str:
        """Encoding rules of this kind of group, for the reader and the reviewer."""

    @abstractmethod
    def guia_casos(self, objetivo: Objetivo) -> str:
        """How the reviewer writes ``datos_json`` of a test case."""

    def extra_revisor(self) -> dict[str, Any]:
        """Further properties of the reviewer's answer: independent readings the loop compares with the proposal."""
        return {}

    @abstractmethod
    def controlar(
        self, valor: Any, citas: list[str], objetivo: Objetivo, *, efectos_desde: date | None, datos: dict[int, Any]
    ) -> tuple[BaseModel | None, Control]:
        """Validate the proposed value and check its figures against the quotes. ``datos``: each year's file."""

    @abstractmethod
    def normalizar(self, modelo: BaseModel) -> Any:
        """The JSON kept in the proposal (no provenance marks)."""

    @abstractmethod
    def evaluar_caso(self, modelo: BaseModel, datos: Any, objetivo: Objetivo) -> tuple[Decimal, str | None]:
        """Amount the calculator gives for a test case with the proposed value, and a note. ValueError: unusable case."""

    def casos_minimos(self, objetivo: Objetivo) -> int:
        """Evaluable test cases the reviewer must bring for this group (0: no calculator can run a case for it)."""
        return self.min_casos

    # -- optional: letting the generic commands find this kind of group ------ #

    def grupos(self, raw: dict[str, Any]) -> list[list[str]]:
        """Stable path of every group of this kind in one parameter file. Default: none (found some other way)."""
        return []

    def reconocer(self, ruta: Sequence[str], raw: dict[str, Any]) -> list[str] | None:
        """The stable path of the group at ``ruta`` when this adapter maintains it, else None."""
        return list(ruta) if list(ruta) in self.grupos(raw) else None

    def objetivo(
        self,
        ruta: list[str],
        ficheros: dict[int, dict[str, Any]],
        *,
        data_dir: Path,
        origen: str,
        motivo: Sequence[str] = (),
        version: str | None = None,
        anotaciones: str = "",
    ) -> Objetivo:
        """The target for the group at ``ruta`` in every year of ``ficheros`` (which law, which articles, its quotes)."""
        raise RecodificacionError(f"El adaptador «{self.tipo}» no sabe localizar sus grupos en la ley.")

    def limpiar_caches(self) -> None:
        """Drop whatever caches the parameter file of this adapter after it has been written. Default: nothing."""

    def desajustes(self, objetivo: Objetivo, ejercicio: int, texto_de: TextoDe, *, data_dir: Path) -> list[str] | None:
        """Why the group no longer matches its law in ``ejercicio`` ([] = it matches).

        None (the default) asks for the generic check: every stored quote must still be in the law.
        """
        return None

    def contrastar(self, modelo: BaseModel, extra: dict[str, Any]) -> list[str]:
        """Objections from comparing the proposal with the reviewer's independent readings."""
        return []

    def ejercicios_afectados(
        self, desde: date, hasta: date | None, candidatos: Iterable[int]
    ) -> tuple[list[int], dict[int, str]]:
        """Tax years a wording with those effects applies to. Default: taxes accrued on 31 December (IRPF)."""
        dentro: list[int] = []
        fuera: dict[int, str] = {}
        for ejercicio in sorted(set(candidatos)):
            if date(ejercicio, 12, 31) < desde:
                fuera[ejercicio] = (
                    f"la redacción leída produce efectos desde {desde.isoformat()}: no se aplica al ejercicio "
                    f"{ejercicio}, que necesita la redacción aplicable a ese ejercicio (indíquela con --version)"
                )
            elif hasta is not None and date(ejercicio, 12, 31) > hasta:
                fuera[ejercicio] = f"la redacción leída deja de producir efectos el {hasta.isoformat()}"
            else:
                dentro.append(ejercicio)
        return dentro, fuera

    @abstractmethod
    def json_final(self, propuesta: Propuesta, anterior: Any, *, hoy: date) -> Any:
        """What is written to the file: the proposed value plus the mark "re-encoded, pending human review"."""

    def al_retirar(self, raw: dict[str, Any], propuesta: Propuesta) -> None:
        """Hook run on each year's file before the group is removed from it (may refuse)."""

    @abstractmethod
    def validar_fichero(self, raw: dict[str, Any]) -> None:
        """Raise if ``raw`` is no longer a valid parameter file."""

    @abstractmethod
    def citas_de(self, data_dir: Path, objetivo: Objetivo, ejercicio: int) -> list[str]:
        """The quotes stored today for the group."""

    @abstractmethod
    def guardar_citas(self, data_dir: Path, propuesta: Propuesta, ficheros: dict[int, dict[str, Any]]) -> None:
        """Store the proposal's quotes where this kind of group keeps them. ``ficheros``: every year's file."""

    @abstractmethod
    def pendientes(self, raw: dict[str, Any]) -> list[tuple[list[str], str]]:
        """(path, label) of every group of this kind marked as pending human review in ``raw``."""

    @abstractmethod
    def confirmar(self, raw: dict[str, Any], ruta: Sequence[str]) -> None:
        """Clear the pending mark of the group at ``ruta``."""


_ADAPTADORES: dict[str, Adaptador] = {}
# The parameter files (by year) of the run in course, for adapters whose test cases go through a whole calculator.
_FICHEROS: ContextVar[dict[int, Any] | None] = ContextVar("recodificacion_ficheros", default=None)


def ficheros_en_curso() -> dict[int, Any]:
    """Each year's parameter file as the loop in course was given them (empty outside a run)."""
    return _FICHEROS.get() or {}


def _cargar_adaptadores() -> None:
    """Import every module that registers adapters. The ones for the other parameter files are optional."""
    import importlib

    from hacienda_gpt.tax.recodificacion import adaptadores  # noqa: F401 - registers the IRPF adapters

    try:
        importlib.import_module("hacienda_gpt.tax.recodificacion.adaptadores_otros")
    except ModuleNotFoundError as exc:
        if exc.name != "hacienda_gpt.tax.recodificacion.adaptadores_otros":
            raise


def registrar(adaptador: Adaptador) -> Adaptador:
    _ADAPTADORES[adaptador.tipo] = adaptador
    return adaptador


def adaptador_de(tipo: str) -> Adaptador:
    if tipo not in _ADAPTADORES:
        _cargar_adaptadores()
    try:
        return _ADAPTADORES[tipo]
    except KeyError as exc:
        raise RecodificacionError(f"No hay adaptador de recodificación para «{tipo}».") from exc


def adaptadores_registrados() -> list[Adaptador]:
    _cargar_adaptadores()
    return list(_ADAPTADORES.values())


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


class _Examen(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    control: Control
    modelo: BaseModel | None = None
    desde: date | None = None
    hasta: date | None = None
    ejercicios: list[int] = Field(default_factory=list)
    excluidos: dict[int, str] = Field(default_factory=dict)


def _con_anotaciones(texto: TextoLegal, anotaciones: str) -> tuple[str, str]:
    completo = f"{texto.texto}\n{anotaciones}" if anotaciones else texto.texto
    return completo, _comprimido(completo)


def examinar(
    adaptador: Adaptador,
    objetivo: Objetivo,
    texto: TextoLegal,
    *,
    valor: Any,
    citas: list[str],
    efectos_desde: str,
    efectos_hasta: str,
    cita_efectos: str,
    datos: dict[int, Any],
) -> _Examen:
    """The deterministic gate: no model is consulted and nothing a model said is taken on trust."""
    completo, comprimido = _con_anotaciones(texto, objetivo.anotaciones)
    efectos, desde, hasta = controlar_efectos(
        efectos_desde,
        efectos_hasta,
        cita_efectos,
        comprimido=comprimido,
        texto=completo,
        candidatos=objetivo.ejercicios,
    )
    control = Control(fallos=list(efectos.fallos), revisar=list(efectos.revisar))
    modelo: BaseModel | None = None
    if valor is not None:
        # The figures must be in the articles themselves; the amending norm's annotations only date them.
        control.fallos += citas_no_literales(citas, texto.comprimido)
        modelo, propio = adaptador.controlar(valor, citas, objetivo, efectos_desde=desde, datos=datos)
        control.fallos += propio.fallos
        control.revisar += propio.revisar
    examen = _Examen(control=control, modelo=modelo, desde=desde, hasta=hasta)
    if desde is not None:
        examen.ejercicios, examen.excluidos = adaptador.ejercicios_afectados(desde, hasta, objetivo.ejercicios)
    if objetivo.norma_texto is not None:
        control.revisar.append(
            f"leída del texto de la norma modificadora {objetivo.norma_texto}, que el BOE aún no había consolidado en "
            f"{objetivo.boe_id}: compruébela contra el texto consolidado cuando se publique"
        )
    if objetivo.version is not None:
        control.revisar.append(
            f"leída de la versión histórica {objetivo.version} indicada por el operador: compruebe que ninguna norma "
            "posterior la ha modificado con efectos retroactivos para los ejercicios afectados"
        )
    return examen


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def _texto(valor: Any) -> str:
    return valor.strip() if isinstance(valor, str) else ""


def _lista_de_textos(valor: Any) -> list[str]:
    return [item for item in valor if isinstance(item, str) and item.strip()] if isinstance(valor, list) else []


def _modelo_de(provider: Any) -> str:
    return f"{getattr(provider, 'name', '?')}/{getattr(provider, 'model', '?')}"


def clave_de(objetivo: Objetivo, texto: TextoLegal) -> str:
    """Identity of "this group, as the files hold it, against this text": the same work is not paid for twice."""
    material = json.dumps(
        [
            objetivo.tipo,
            objetivo.fichero,
            objetivo.ruta,
            objetivo.boe_id,
            objetivo.version,
            *([objetivo.norma_texto] if objetivo.norma_texto else []),
            texto.sha256,
            sorted(objetivo.ejercicios),
            {str(k): v for k, v in sorted(objetivo.antes.items())},
            objetivo.anotaciones,
            sorted(objetivo.motivo),
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def leer_texto(objetivo: Objetivo, texto_de: TextoDe, articulos: Sequence[str] | None = None) -> tuple[str, TextoLegal]:
    """The law's page and the text of the group's articles. Raises :class:`SinTextoError`.

    With ``objetivo.norma_texto`` the page is the amending norm as the daily BOE published it, and the text is
    what that norm says about the group's articles.
    """
    pedidos = list(articulos if articulos is not None else objetivo.articulos_ley)
    if objetivo.norma_texto is not None:
        pagina = texto_de(objetivo.norma_texto, VERSION_DIARIO)
        if not pagina:
            raise SinTextoError(f"No se pudo leer el texto de {objetivo.norma_texto} en el BOE del día.")
        texto = extraer_de_modificadora(pagina, pedidos, ley=objetivo.ley_citada)
        if not texto.encontrados:
            raise SinTextoError(
                f"{objetivo.norma_texto} no nombra en su texto el artículo {', '.join(pedidos)} de {objetivo.boe_id}: "
                "habrá que esperar al texto consolidado."
            )
    else:
        pagina = texto_de(objetivo.boe_id, objetivo.version)
        if not pagina:
            raise SinTextoError(f"No se pudo leer {objetivo.boe_id} (versión {objetivo.version or 'vigente'}).")
        texto = extraer_articulos(pagina, pedidos)
        primero = " ".join(objetivo.articulos_ley[0].lower().split()) if objetivo.articulos_ley else None
        if primero is not None and primero in texto.faltan:
            raise SinTextoError(
                f"El artículo {objetivo.articulos_ley[0]} no aparece en {objetivo.boe_id}: puede haberse derogado o "
                "renumerado. Revísalo a mano."
            )
    if len(texto.texto) > MAX_CARACTERES_TEXTO:
        raise SinTextoError(
            f"Los artículos {', '.join(texto.encontrados)} de {objetivo.boe_id} ocupan {len(texto.texto)} caracteres: "
            "demasiado para leerlos de una vez."
        )
    return pagina, texto


def _origen_del_texto(objetivo: Objetivo) -> str:
    if objetivo.norma_texto is not None:
        return (
            f"TODAVÍA SIN CONSOLIDAR: se muestra el texto de la norma modificadora {objetivo.norma_texto}, que da la "
            "nueva redacción entre comillas; codifica esa nueva redacción y toma los efectos de esa misma norma"
        )
    return "texto consolidado vigente" if objetivo.version is None else "versión " + objetivo.version


def _mensaje_lector(objetivo: Objetivo, texto: TextoLegal, indice: list[str], previas: list[Iteracion]) -> str:
    partes = [
        prompts.etiquetar(
            "tarea",
            f"Grupo: {objetivo.etiqueta}\nÁmbito: {objetivo.ambito}\nNorma: {objetivo.boe_id}"
            f" ({_origen_del_texto(objetivo)})\n"
            f"Ejercicios pedidos: {', '.join(map(str, sorted(objetivo.ejercicios)))}\n"
            f"Artículos mostrados: {', '.join(texto.encontrados)}",
        ),
        prompts.etiquetar("texto_legal", texto.texto),
    ]
    if objetivo.anotaciones:
        partes.append(prompts.etiquetar("anotaciones_de_la_norma_modificadora", objetivo.anotaciones))
    partes.append(prompts.etiquetar("indice_de_la_ley", "\n".join(indice)))
    if objetivo.antes:
        ejercicio = max(objetivo.antes)
        anterior = objetivo.antes[ejercicio] if objetivo.antes[ejercicio] is not None else objetivo.referencia_anterior
        if anterior is not None:
            contenido = json.dumps(anterior, ensure_ascii=False, indent=1)
            if objetivo.citas_antes:
                contenido += "\n\nCitas en las que se apoyaba:\n" + "\n".join(f"- {c}" for c in objetivo.citas_antes)
            if objetivo.motivo:
                contenido += "\n\nPor qué se revisa: " + "; ".join(objetivo.motivo[:8])
            partes.append(prompts.etiquetar("codificacion_anterior", contenido, ejercicio=ejercicio))
    if previas:
        ultima = previas[-1]
        rechazada = json.dumps(
            {"valor": ultima.valor, "evidencia": ultima.citas, "efectos_desde": ultima.efectos_desde},
            ensure_ascii=False,
            indent=1,
        )
        partes.append(prompts.etiquetar("propuesta_rechazada", rechazada, iteracion=ultima.numero))
        partes.append(prompts.etiquetar("objeciones", "\n".join(f"- {o}" for o in ultima.objeciones)))
    return "\n\n".join(partes)


def _mensaje_revisor(objetivo: Objetivo, texto: TextoLegal, valor: Any, citas: list[str]) -> str:
    partes = [
        prompts.etiquetar(
            "tarea",
            f"Grupo: {objetivo.etiqueta}\nÁmbito: {objetivo.ambito}\nNorma: {objetivo.boe_id}"
            f" ({_origen_del_texto(objetivo)})\n"
            f"Ejercicio de los casos de prueba: {max(objetivo.ejercicios)}",
        ),
        prompts.etiquetar("texto_legal", texto.texto),
    ]
    if objetivo.anotaciones:
        partes.append(prompts.etiquetar("anotaciones_de_la_norma_modificadora", objetivo.anotaciones))
    propuesta = json.dumps({"valor": valor, "evidencia": citas}, ensure_ascii=False, indent=1)
    partes.append(prompts.etiquetar("propuesta", propuesta))
    return "\n\n".join(partes)


def _veredicto(raw: dict[str, Any], comprimido: str, extra: Iterable[str]) -> Veredicto:
    discrepancias: list[Discrepancia] = []
    brutas = raw.get("discrepancias")
    for item in brutas if isinstance(brutas, list) else []:
        if not isinstance(item, dict) or not _texto(item.get("descripcion")):
            continue
        cita = _texto(item.get("cita"))
        discrepancias.append(
            Discrepancia(
                # Anything that is not plainly an observation counts as an error.
                gravedad="observacion" if item.get("gravedad") == "observacion" else "error",
                tipo=_texto(item.get("tipo")) or "otro",
                descripcion=_texto(item.get("descripcion")),
                cita=cita,
                cita_literal=es_literal(cita, comprimido),
            )
        )
    return Veredicto(
        conforme=raw.get("conforme") is True,
        discrepancias=discrepancias,
        efectos_desde=_texto(raw.get("efectos_desde")),
        cita_efectos=_texto(raw.get("cita_efectos")),
        extra={nombre: raw.get(nombre) for nombre in extra},
    )


def _evaluar_casos(
    adaptador: Adaptador, modelo: BaseModel, raw: dict[str, Any], objetivo: Objetivo, comprimido: str
) -> list[CasoEvaluado]:
    casos: list[CasoEvaluado] = []
    brutos = raw.get("casos")
    for item in brutos if isinstance(brutos, list) else []:
        if not isinstance(item, dict):
            continue
        caso = CasoEvaluado(
            descripcion=_texto(item.get("descripcion")) or "(sin descripción)",
            fundamento=_texto(item.get("fundamento")),
        )
        casos.append(caso)
        try:
            caso.datos = json.loads(_texto(item.get("datos_json")))
            caso.esperado = Decimal(_texto(item.get("importe_esperado")).replace(",", ".")).quantize(CENT)
        except (json.JSONDecodeError, InvalidOperation):
            caso.error = "datos_json o importe_esperado mal formados"
            continue
        if not es_literal(caso.fundamento, comprimido):
            caso.error = "el fundamento no es una cita literal del texto legal"
            continue
        try:
            obtenido, caso.detalle = adaptador.evaluar_caso(modelo, caso.datos, objetivo)
        except ValueError as exc:
            caso.error = f"caso no evaluable: {str(exc)[:200]}"
            continue
        caso.obtenido = obtenido.quantize(CENT)
        caso.coincide = abs(caso.obtenido - caso.esperado) <= CENT
    return casos


def _objeciones_del_revisor(
    adaptador: Adaptador,
    modelo: BaseModel,
    veredicto: Veredicto,
    casos: list[CasoEvaluado],
    desde: date | None,
    min_casos: int | None = None,
) -> list[str]:
    min_casos = adaptador.min_casos if min_casos is None else min_casos
    objeciones = [
        f"Revisor ({d.tipo}): {d.descripcion}" + (f" — «{d.cita[:300]}»" if d.cita else "")
        for d in veredicto.discrepancias
        if d.gravedad == "error"
    ]
    if not veredicto.conforme and not objeciones:
        objeciones.append("El revisor no da su conformidad y no concreta ninguna discrepancia.")
    segun_revisor = fecha_iso(veredicto.efectos_desde)
    if segun_revisor is None or segun_revisor != desde:
        objeciones.append(
            f"Efectos: el lector lee que la redacción produce efectos desde {desde.isoformat() if desde else '?'} y el "
            f"revisor desde {veredicto.efectos_desde or '?'} («{veredicto.cita_efectos[:200]}»). Sin acuerdo no se sabe "
            "a qué ejercicios se aplica."
        )
    objeciones += adaptador.contrastar(modelo, veredicto.extra)
    for caso in casos:
        if caso.coincide is False:
            objeciones.append(
                f"Caso «{caso.descripcion}» {json.dumps(caso.datos, ensure_ascii=False)}: según la ley corresponden "
                f"{caso.esperado} € («{caso.fundamento[:200]}») y con la codificación propuesta la calculadora da "
                f"{caso.obtenido} €" + (f" ({caso.detalle})" if caso.detalle else "") + "."
            )
    evaluables = sum(1 for caso in casos if caso.error is None)
    if evaluables < min_casos:
        objeciones.append(
            f"El revisor solo aportó {evaluables} caso(s) de prueba evaluable(s); hacen falta al menos "
            f"{min_casos} para dar la propuesta por contrastada."
        )
    return objeciones


def recodificar(
    objetivo: Objetivo,
    *,
    lector: Any,
    revisor: Any,
    texto_de: TextoDe,
    datos: dict[int, Any] | None = None,
    max_iter: int = MAX_ITER,
    ahora: datetime | None = None,
) -> Propuesta:
    """Run the loop for one group and return its proposal (not saved; nothing is written to any parameter file).

    ``lector`` and ``revisor`` are LLM providers (``complete_json``); they may be the same object — the reviewer
    is always a separate call that sees only the legal text and the proposal. ``datos`` are the parameter files by
    year, for checks that look at the group's neighbours. Raises :class:`SinTextoError` when the law cannot be read
    and lets the provider's ``ProviderError`` (budget included) through: an interrupted loop proposes nothing.
    """
    adaptador = adaptador_de(objetivo.tipo)
    ahora = ahora or datetime.now(UTC)
    datos = datos or {}
    _FICHEROS.set(datos)
    pagina, texto_base = leer_texto(objetivo, texto_de)
    propios = [" ".join(a.lower().split()) for a in objetivo.articulos_ley]
    consolidado = objetivo.norma_texto is None
    # Where the previous encoding read from, too — unless the text is an amending norm, which has other articles.
    otros = articulos_que_contienen(pagina, objetivo.citas_antes, junto_a=propios) if consolidado else []
    articulos = list(dict.fromkeys([*propios, *otros]))
    _, texto = leer_texto(objetivo, texto_de, articulos)
    indice = indice_de(pagina) if consolidado else []
    min_casos = adaptador.casos_minimos(objetivo)
    sistema_lector = prompts.SISTEMA_LECTOR.format(
        reglas=prompts.REGLAS_COMUNES, que=adaptador.que, guia=adaptador.guia(), esquema=adaptador.esquema()
    )
    sistema_revisor = prompts.SISTEMA_REVISOR.format(
        reglas_revisor=prompts.REGLAS_REVISOR,
        que=adaptador.que,
        guia=adaptador.guia(),
        esquema=adaptador.esquema(),
        min_casos=min_casos,
        guia_casos=adaptador.guia_casos(objetivo),
    )
    extra = adaptador.extra_revisor()
    iteraciones: list[Iteracion] = []
    llamadas = enviados = ampliaciones = 0
    resultado: Resultado | None = None
    examen: _Examen | None = None
    convergio = False

    while len(iteraciones) < max_iter and not convergio:
        usuario = _mensaje_lector(objetivo, texto, indice, iteraciones)
        llamadas += 1
        enviados += len(sistema_lector) + len(usuario)
        leido = lector.complete_json(
            system=sistema_lector, user=usuario, schema=prompts.ESQUEMA_LECTOR, schema_name="recodificacion_lectura"
        )
        iteracion = Iteracion(
            numero=len(iteraciones) + 1,
            resultado_lector=_texto(leido.get("resultado")) or "?",
            citas=_lista_de_textos(leido.get("evidencia")),
            efectos_desde=_texto(leido.get("efectos_desde")),
            efectos_hasta=_texto(leido.get("efectos_hasta")),
            cita_efectos=_texto(leido.get("cita_efectos")),
            motivo=_texto(leido.get("motivo")),
            cambios=_texto(leido.get("cambios")),
        )
        pedidos = [
            " ".join(a.lower().split())
            for a in _lista_de_textos(leido.get("articulos_necesarios"))
            if " ".join(a.lower().split()) not in articulos
        ]
        if pedidos:
            ampliado = extraer_articulos(pagina, [*articulos, *pedidos]) if consolidado else texto
            servible = (
                set(ampliado.encontrados) - set(texto.encontrados) and len(ampliado.texto) <= MAX_CARACTERES_TEXTO
            )
            if servible and ampliaciones < MAX_AMPLIACIONES:
                # More law to read is not a failed attempt: ask again without spending an iteration.
                ampliaciones += 1
                articulos, texto = list(ampliado.encontrados), ampliado
                continue
            iteraciones.append(iteracion)
            iteracion.objeciones = [
                f"No se pueden añadir los artículos pedidos ({', '.join(pedidos)}): no existen, no caben o ya se han "
                "ampliado bastantes veces. Codifica con el texto mostrado o responde «no_calculable» sin pedir más."
            ]
            continue
        comprimido = _con_anotaciones(texto, objetivo.anotaciones)[1]
        iteraciones.append(iteracion)
        resultado, examen = None, None
        retirada = iteracion.resultado_lector == "no_calculable"
        if retirada and not adaptador.permite_retirada:
            iteracion.objeciones = [
                "El lector considera que el texto no encaja en el esquema y este grupo no se puede retirar: "
                + (iteracion.motivo or "sin motivo")
            ]
            break
        if retirada and all(valor is None for valor in objetivo.antes.values()):
            # Nothing in the files to withdraw: a deduction that is not encoded simply stays out.
            iteracion.objeciones = [f"No calculable: {iteracion.motivo or 'sin motivo'}"]
            break
        if retirada and not iteracion.motivo:
            iteracion.objeciones = ["«no_calculable» necesita un motivo que diga qué dato o fórmula falta."]
            continue
        if not retirada:
            try:
                iteracion.valor = json.loads(_texto(leido.get("valor_json")))
            except json.JSONDecodeError:
                iteracion.objeciones = ["valor_json no es JSON válido."]
                continue
        examen = examinar(
            adaptador,
            objetivo,
            texto,
            valor=iteracion.valor,
            citas=iteracion.citas,
            efectos_desde=iteracion.efectos_desde,
            efectos_hasta=iteracion.efectos_hasta,
            cita_efectos=iteracion.cita_efectos,
            datos=datos,
        )
        iteracion.control = examen.control
        if not examen.control.ok:
            # The gate comes first: a proposal that breaks a rule is not worth a reviewer's call.
            iteracion.objeciones = [f"Control: {fallo}" for fallo in examen.control.fallos]
            continue
        if not examen.ejercicios:
            iteracion.objeciones = [f"{ejercicio}: {razon}" for ejercicio, razon in examen.excluidos.items()]
            break
        if retirada:
            # Leaving a group out is the conservative answer and a person decides on it: no reviewer needed.
            resultado, convergio = "retirada", True
            break
        assert examen.modelo is not None
        iteracion.valor = adaptador.normalizar(examen.modelo)
        usuario = _mensaje_revisor(objetivo, texto, iteracion.valor, iteracion.citas)
        llamadas += 1
        enviados += len(sistema_revisor) + len(usuario)
        revisado = revisor.complete_json(
            system=sistema_revisor,
            user=usuario,
            schema=prompts.esquema_revisor(extra),
            schema_name="recodificacion_revision",
        )
        iteracion.veredicto = _veredicto(revisado, comprimido, extra)
        iteracion.casos = _evaluar_casos(adaptador, examen.modelo, revisado, objetivo, comprimido)
        iteracion.objeciones = _objeciones_del_revisor(
            adaptador, examen.modelo, iteracion.veredicto, iteracion.casos, examen.desde, min_casos
        )
        if not iteracion.objeciones:
            resultado, convergio = "recodificada", True

    ultima = iteraciones[-1] if iteraciones else None
    clave = clave_de(objetivo, texto_base)
    return Propuesta(
        id=f"{ahora:%Y%m%d-%H%M%S}-{_slug(objetivo.ambito)}-{_slug(objetivo.etiqueta)[:40]}-{clave[:6]}",
        estado="propuesta" if convergio else "sin_converger",
        resultado=resultado if convergio else None,
        tipo=objetivo.tipo,
        fichero=objetivo.fichero,
        ruta=objetivo.ruta,
        etiqueta=objetivo.etiqueta,
        ambito=objetivo.ambito,
        boe_id=objetivo.boe_id,
        version=objetivo.version,
        articulos_ley=list(texto.encontrados),
        sha256_html=hashlib.sha256(pagina.encode("utf-8")).hexdigest(),
        sha256_texto=texto.sha256,
        origen=objetivo.origen,
        motivo=objetivo.motivo,
        ejercicios_pedidos=sorted(objetivo.ejercicios),
        ejercicios=examen.ejercicios if convergio and examen else [],
        ejercicios_excluidos=examen.excluidos if examen else {},
        efectos_desde=ultima.efectos_desde if ultima else "",
        efectos_hasta=ultima.efectos_hasta if ultima else "",
        cita_efectos=ultima.cita_efectos if ultima else "",
        anotaciones=objetivo.anotaciones,
        antes=objetivo.antes,
        citas_antes=objetivo.citas_antes,
        despues=ultima.valor if ultima and resultado != "retirada" else None,
        citas=ultima.citas if ultima and resultado != "retirada" else [],
        motivo_lector=ultima.motivo if ultima else "",
        cambios=ultima.cambios if ultima else "",
        control=ultima.control if ultima else None,
        veredicto=ultima.veredicto if ultima else None,
        casos=ultima.casos if ultima else [],
        objeciones_abiertas=[] if convergio or ultima is None else ultima.objeciones,
        iteraciones=iteraciones,
        max_iter=max_iter,
        llamadas_llm=llamadas,
        caracteres_enviados=enviados,
        modelo_lector=_modelo_de(lector),
        modelo_revisor=_modelo_de(revisor),
        insertar_tras=objetivo.insertar_tras,
        norma_texto=objetivo.norma_texto,
        ley_citada=objetivo.ley_citada,
        clave=clave,
        creada_el=ahora,
        actualizada_el=ahora,
    )


def _slug(texto: str) -> str:
    plano = "".join(ch for ch in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", plano).strip("-") or "x"


# --------------------------------------------------------------------------- #
# Applying, rejecting, confirming
# --------------------------------------------------------------------------- #


def _objetivo_de(propuesta: Propuesta) -> Objetivo:
    return Objetivo(
        tipo=propuesta.tipo,
        fichero=propuesta.fichero,
        ruta=propuesta.ruta,
        etiqueta=propuesta.etiqueta,
        ambito=propuesta.ambito,
        boe_id=propuesta.boe_id,
        version=propuesta.version,
        articulos_ley=propuesta.articulos_ley,
        ejercicios=propuesta.ejercicios_pedidos,
        antes=propuesta.antes,
        citas_antes=propuesta.citas_antes,
        origen=propuesta.origen,
        insertar_tras=propuesta.insertar_tras,
        anotaciones=propuesta.anotaciones,
        norma_texto=propuesta.norma_texto,
        ley_citada=propuesta.ley_citada,
    )


def _limpiar_caches() -> None:
    from hacienda_gpt.tax import params, verificacion

    params.load_params.cache_clear()
    verificacion._evidencia_deducciones.cache_clear()
    verificacion._evidencia_parametros.cache_clear()
    # Each adapter knows which loader caches its own file.
    for adaptador in list(_ADAPTADORES.values()):
        adaptador.limpiar_caches()


def aplicar_propuesta(
    propuesta: Propuesta,
    texto_de: TextoDe,
    *,
    almacen: AlmacenPropuestas | None = None,
    data_dir: Path = DATA_DIR,
    hoy: date | None = None,
    automatica: bool = False,
) -> Propuesta:
    """Write an accepted proposal into the parameter file of every tax year it touches.

    Nothing is taken from the proposal on trust: the law is read again and must be the text the proposal was made
    from; each file must still hold what the proposal replaces; and the whole deterministic gate is run again. Any of
    them failing raises and writes nothing.
    """
    if propuesta.estado != "propuesta" or propuesta.resultado is None:
        raise RecodificacionError(f"La propuesta {propuesta.id} está «{propuesta.estado}»: no se puede aplicar.")
    adaptador = adaptador_de(propuesta.tipo)
    hoy = hoy or date.today()
    objetivo = _objetivo_de(propuesta)
    _, texto = leer_texto(objetivo, texto_de)
    if texto.sha256 != propuesta.sha256_texto:
        raise LeyCambiadaError(
            f"El texto de {propuesta.boe_id} ({', '.join(propuesta.articulos_ley)}) ha cambiado desde que se hizo la "
            f"propuesta {propuesta.id}: vuelve a lanzar la recodificación."
        )
    ficheros: dict[int, dict[str, Any]] = {}
    sangrias: dict[int, int] = {}
    _FICHEROS.set(ficheros)
    for ejercicio in ejercicios_en(data_dir, propuesta.fichero):
        contenido = (data_dir / propuesta.fichero.format(ejercicio=ejercicio)).read_text(encoding="utf-8")
        ficheros[ejercicio], sangrias[ejercicio] = json.loads(contenido), _sangria(contenido)
    for ejercicio in propuesta.ejercicios:
        if ejercicio not in ficheros:
            raise DatosCambiadosError(f"No existe {propuesta.fichero.format(ejercicio=ejercicio)}.")
        if leer_ruta(ficheros[ejercicio], propuesta.ruta) != propuesta.antes.get(ejercicio):
            raise DatosCambiadosError(
                f"{propuesta.fichero.format(ejercicio=ejercicio)} ya no contiene en {'/'.join(propuesta.ruta)} lo que "
                f"la propuesta {propuesta.id} iba a sustituir: alguien lo ha cambiado. Vuelve a lanzar la recodificación."
            )
    examen = examinar(
        adaptador,
        objetivo,
        texto,
        valor=propuesta.despues if propuesta.resultado == "recodificada" else None,
        citas=propuesta.citas,
        efectos_desde=propuesta.efectos_desde,
        efectos_hasta=propuesta.efectos_hasta,
        cita_efectos=propuesta.cita_efectos,
        datos=ficheros,
    )
    if not examen.control.ok:
        raise ControlFallidoError(
            f"La propuesta {propuesta.id} no supera hoy el control: " + "; ".join(examen.control.fallos[:6])
        )
    if examen.ejercicios != propuesta.ejercicios:
        raise ControlFallidoError(
            f"Los ejercicios afectados hoy ({examen.ejercicios}) no son los de la propuesta ({propuesta.ejercicios})."
        )
    if automatica and examen.control.revisar:
        raise ControlFallidoError("Queda algo por revisar a mano: no se aplica sin una persona.")

    for ejercicio in propuesta.ejercicios:
        raw = ficheros[ejercicio]
        if propuesta.resultado == "retirada":
            adaptador.al_retirar(raw, propuesta)
            escribir_ruta(raw, propuesta.ruta, None)
        else:
            final = adaptador.json_final(propuesta, propuesta.antes.get(ejercicio), hoy=hoy)
            escribir_ruta(raw, propuesta.ruta, final, insertar_tras=propuesta.insertar_tras)
        if isinstance(raw.get("revisiones"), list):
            raw["revisiones"].append(
                {
                    "norma": f"Recodificación automática: {propuesta.etiqueta}",
                    "boe_id": propuesta.boe_id,
                    "revisado_el": hoy.isoformat(),
                    "resultado": (
                        f"{'Retirada' if propuesta.resultado == 'retirada' else 'Recodificada'} a partir del texto "
                        f"del BOE (propuesta {propuesta.id}; efectos desde {propuesta.efectos_desde}). Pendiente de "
                        "revisión humana."
                    ),
                }
            )
        adaptador.validar_fichero(raw)
    # Everything validated: only now is anything written.
    adaptador.guardar_citas(data_dir, propuesta, ficheros)
    for ejercicio in propuesta.ejercicios:
        _volcar(
            data_dir / propuesta.fichero.format(ejercicio=ejercicio), ficheros[ejercicio], sangria=sangrias[ejercicio]
        )
    _limpiar_caches()
    ahora = datetime.now(UTC)
    aplicada = propuesta.model_copy(
        update={
            "estado": "aplicada",
            "aplicada_el": ahora,
            "actualizada_el": ahora,
            "aplicada_automaticamente": automatica,
            "control": examen.control,
        }
    )
    if almacen is not None:
        almacen.guardar(aplicada)
    return aplicada


def rechazar_propuesta(
    propuesta: Propuesta, *, motivo: str = "", almacen: AlmacenPropuestas | None = None
) -> Propuesta:
    if propuesta.estado not in ("propuesta", "sin_converger"):
        raise RecodificacionError(f"La propuesta {propuesta.id} está «{propuesta.estado}»: no se puede rechazar.")
    ahora = datetime.now(UTC)
    rechazada = propuesta.model_copy(
        update={"estado": "rechazada", "rechazada_el": ahora, "actualizada_el": ahora, "motivo_rechazo": motivo}
    )
    if almacen is not None:
        almacen.guardar(rechazada)
    return rechazada


class Pendiente(_Registro):
    tipo: str
    fichero: str
    ejercicio: int
    ruta: list[str]
    etiqueta: str


def pendientes_de_confirmar(data_dir: Path = DATA_DIR) -> list[Pendiente]:
    """Every group, in any parameter file, that was re-encoded automatically and nobody has confirmed yet."""
    out: list[Pendiente] = []
    for adaptador in adaptadores_registrados():
        fichero = adaptador.fichero
        for ejercicio in ejercicios_en(data_dir, fichero):
            raw = json.loads((data_dir / fichero.format(ejercicio=ejercicio)).read_text(encoding="utf-8"))
            out += [
                Pendiente(tipo=adaptador.tipo, fichero=fichero, ejercicio=ejercicio, ruta=ruta, etiqueta=etiqueta)
                for ruta, etiqueta in adaptador.pendientes(raw)
            ]
    return out


def confirmar(
    pendientes: Iterable[Pendiente], *, data_dir: Path = DATA_DIR, almacen: AlmacenPropuestas | None = None
) -> int:
    """A person has reviewed these groups: clear their "pending human review" mark. Returns how many were cleared."""
    por_fichero: dict[tuple[str, int], list[Pendiente]] = {}
    for pendiente in pendientes:
        por_fichero.setdefault((pendiente.fichero, pendiente.ejercicio), []).append(pendiente)
    hechas: list[Pendiente] = []
    for (fichero, ejercicio), suyos in por_fichero.items():
        path = data_dir / fichero.format(ejercicio=ejercicio)
        contenido = path.read_text(encoding="utf-8")
        raw = json.loads(contenido)
        for pendiente in suyos:
            adaptador = adaptador_de(pendiente.tipo)
            if any(ruta == pendiente.ruta for ruta, _ in adaptador.pendientes(raw)):
                adaptador.confirmar(raw, pendiente.ruta)
                hechas.append(pendiente)
        adaptador_de(suyos[0].tipo).validar_fichero(raw)
        _volcar(path, raw, sangria=_sangria(contenido))
    _limpiar_caches()
    if almacen is not None and hechas:
        ahora = datetime.now(UTC)
        for propuesta in almacen.listar("aplicada"):
            if propuesta.confirmada_el is None and any(
                h.tipo == propuesta.tipo and h.ruta == propuesta.ruta and h.ejercicio in propuesta.ejercicios
                for h in hechas
            ):
                almacen.guardar(propuesta.model_copy(update={"confirmada_el": ahora, "actualizada_el": ahora}))
    return len(hechas)


# --------------------------------------------------------------------------- #
# Splitting an article into the groups it holds
# --------------------------------------------------------------------------- #


class Elemento(_Registro):
    """One group a reader found inside an article (a deduction of an article that holds several)."""

    etiqueta: str
    titulo: str
    cita: str


class Desglose(_Registro):
    boe_id: str
    articulos_ley: list[str]
    elementos: list[Elemento] = Field(default_factory=list)
    # What the reader listed and the program did not accept, with the reason.
    descartados: list[str] = Field(default_factory=list)
    llamadas_llm: int = 0
    caracteres_enviados: int = 0


def desglosar(objetivo: Objetivo, *, lector: Any, texto_de: TextoDe, etiqueta_articulo: str) -> Desglose:
    """Ask a reader which groups the article of ``objetivo`` holds; each one then goes through the normal loop.

    Nothing the reader says is used unverified: an item is kept only if its quote is literally in the article and
    its label hangs from the article's own label (``etiqueta_articulo``: "art. 5" → "art. 5.Dos.1"). The reader
    only LISTS; it encodes nothing here. Lets the provider's ``ProviderError`` through.
    """
    from hacienda_gpt.tax.recodificacion.control import ruta_de_etiqueta

    adaptador = adaptador_de(objetivo.tipo)
    _, texto = leer_texto(objetivo, texto_de)
    sistema = prompts.SISTEMA_DESGLOSE.format(que=adaptador.que, etiqueta=etiqueta_articulo)
    usuario = "\n\n".join(
        [
            prompts.etiquetar(
                "tarea",
                f"Norma: {objetivo.boe_id} ({_origen_del_texto(objetivo)})\nÁmbito: {objetivo.ambito}\n"
                f"Artículo: {etiqueta_articulo}",
            ),
            prompts.etiquetar("texto_legal", texto.texto),
        ]
    )
    leido = lector.complete_json(
        system=sistema, user=usuario, schema=prompts.ESQUEMA_DESGLOSE, schema_name="recodificacion_desglose"
    )
    desglose = Desglose(
        boe_id=objetivo.boe_id,
        articulos_ley=list(texto.encontrados),
        llamadas_llm=1,
        caracteres_enviados=len(sistema) + len(usuario),
    )
    raiz = ruta_de_etiqueta(etiqueta_articulo)
    vistas: set[str] = set()
    brutos = leido.get("elementos")
    for item in brutos if isinstance(brutos, list) else []:
        if not isinstance(item, dict):
            continue
        etiqueta, titulo, cita = (_texto(item.get(k)) for k in ("etiqueta", "titulo", "cita"))
        etiqueta = " ".join(etiqueta.split())
        propia = ruta_de_etiqueta(etiqueta)
        if raiz is None or propia is None or not (propia == raiz or propia.startswith(raiz + ".")):
            desglose.descartados.append(f"«{etiqueta}»: la etiqueta no cuelga de «{etiqueta_articulo}»")
        elif not etiqueta.startswith(etiqueta_articulo):
            desglose.descartados.append(f"«{etiqueta}»: la etiqueta debe empezar por «{etiqueta_articulo}»")
        elif not es_literal(cita, texto.comprimido):
            desglose.descartados.append(f"«{etiqueta}»: su cita no es literal")
        elif etiqueta in vistas:
            desglose.descartados.append(f"«{etiqueta}»: repetida")
        else:
            vistas.add(etiqueta)
            desglose.elementos.append(Elemento(etiqueta=etiqueta, titulo=titulo, cita=cita))
    return desglose


# --------------------------------------------------------------------------- #
# A run over several groups
# --------------------------------------------------------------------------- #


class Informe(_Registro):
    objetivos: int = 0
    propuestas: list[dict[str, Any]] = Field(default_factory=list)
    # Groups whose proposal for this very text already exists (nothing was spent on them).
    ya_tratados: list[dict[str, str]] = Field(default_factory=list)
    errores: list[str] = Field(default_factory=list)
    sin_procesar: list[str] = Field(default_factory=list)
    presupuesto_agotado: str | None = None
    llamadas_llm: int = 0


def resumen_de(propuesta: Propuesta) -> dict[str, Any]:
    return {
        "id": propuesta.id,
        "estado": propuesta.estado,
        "resultado": propuesta.resultado,
        "tipo": propuesta.tipo,
        "grupo": propuesta.etiqueta,
        "ambito": propuesta.ambito,
        "ejercicios": propuesta.ejercicios,
        "ejercicios_excluidos": propuesta.ejercicios_excluidos,
        "efectos_desde": propuesta.efectos_desde,
        "iteraciones": len(propuesta.iteraciones),
        "por_revisar": propuesta.control.revisar if propuesta.control else [],
        "objeciones_abiertas": propuesta.objeciones_abiertas,
    }


def ejecutar(
    objetivos: Sequence[Objetivo],
    *,
    lector: Any,
    revisor: Any,
    texto_de: TextoDe,
    almacen: AlmacenPropuestas,
    data_dir: Path = DATA_DIR,
    max_iter: int = MAX_ITER,
    max_objetivos: int | None = None,
    auto_aplicar: bool = False,
    forzar: bool = False,
    hoy: date | None = None,
    al_terminar: Callable[[Propuesta], None] | None = None,
) -> tuple[Informe, list[Propuesta]]:
    """Run the loop for each group, save every proposal and, with ``auto_aplicar``, apply the ones that need nobody.

    A budget error stops the run at once (what was left is listed); any other failure is recorded and the run goes on.
    """
    from hacienda_gpt.ops.consumo import PresupuestoAgotadoError

    informe = Informe(objetivos=len(objetivos))
    nuevas: list[Propuesta] = []
    hechos = 0
    for posicion, objetivo in enumerate(objetivos):
        try:
            _, texto = leer_texto(objetivo, texto_de)
        except SinTextoError as exc:
            informe.errores.append(f"{objetivo.etiqueta}: {exc}")
            continue
        previa = almacen.con_clave(clave_de(objetivo, texto))
        if previa is not None and not forzar:
            informe.ya_tratados.append({"grupo": objetivo.etiqueta, "propuesta": previa.id, "estado": previa.estado})
            continue
        if max_objetivos is not None and hechos >= max_objetivos:
            informe.sin_procesar.append(objetivo.etiqueta)
            continue
        hechos += 1
        ficheros = {
            ejercicio: json.loads((data_dir / objetivo.fichero.format(ejercicio=ejercicio)).read_text(encoding="utf-8"))
            for ejercicio in ejercicios_en(data_dir, objetivo.fichero)
        }
        try:
            propuesta = recodificar(
                objetivo, lector=lector, revisor=revisor, texto_de=texto_de, datos=ficheros, max_iter=max_iter
            )
        except PresupuestoAgotadoError as exc:
            informe.presupuesto_agotado = str(exc)
            informe.sin_procesar += [o.etiqueta for o in objetivos[posicion:]]
            break
        except (ProviderError, SinTextoError) as exc:
            informe.errores.append(f"{objetivo.etiqueta}: {exc}")
            continue
        almacen.guardar(propuesta)
        informe.llamadas_llm += propuesta.llamadas_llm
        if auto_aplicar and propuesta.aplicable_sin_persona:
            try:
                propuesta = aplicar_propuesta(
                    propuesta, texto_de, almacen=almacen, data_dir=data_dir, hoy=hoy, automatica=True
                )
            except RecodificacionError as exc:
                informe.errores.append(f"{objetivo.etiqueta}: no se aplicó automáticamente: {exc}")
        nuevas.append(propuesta)
        informe.propuestas.append(resumen_de(propuesta))
        if al_terminar is not None:
            al_terminar(propuesta)
    return informe, nuevas


# --------------------------------------------------------------------------- #
# Telling the operator
# --------------------------------------------------------------------------- #


class _Canal(Protocol):
    nombre: str

    def enviar(self, asunto: str, cuerpo: str) -> None: ...


ASUNTO_AVISO = "HaciendaGPT: propuestas de recodificación de parámetros"
_ESTADOS = {
    "propuesta": "PENDIENTE DE TU DECISIÓN",
    "aplicada": "APLICADA AUTOMÁTICAMENTE (pendiente de confirmar)",
    "sin_converger": "SIN CONVERGER (revísala a mano)",
    "rechazada": "RECHAZADA",
}


def texto_del_aviso(propuestas: Sequence[Propuesta]) -> str:
    lineas: list[str] = []
    for p in propuestas:
        que = "retirar" if p.resultado == "retirada" else "recodificar"
        lineas.append(f"- [{_ESTADOS[p.estado]}] {p.etiqueta} ({p.ambito}): {que}")
        if p.ejercicios:
            lineas.append(f"  Ejercicios: {', '.join(map(str, p.ejercicios))} (efectos desde {p.efectos_desde}).")
        for ejercicio, razon in p.ejercicios_excluidos.items():
            lineas.append(f"  No toca {ejercicio}: {razon}.")
        if p.cambios:
            lineas.append(f"  Cambios: {p.cambios}")
        if p.motivo_lector:
            lineas.append(f"  Motivo: {p.motivo_lector}")
        for nota in (p.control.revisar if p.control else [])[:5]:
            lineas.append(f"  Revisar: {nota}")
        for objecion in p.objeciones_abiertas[:5]:
            lineas.append(f"  Objeción abierta: {objecion[:300]}")
        lineas.append(f"  Propuesta: {p.id}")
    lineas += [
        "",
        "Para verlas: python -m hacienda_gpt.cli.proactive params-proposals --show <id>",
        "Para decidir: params-apply <id> | params-reject <id>; tras revisar una aplicada: params-confirm <id>",
    ]
    return "\n".join(lineas) + "\n"


def avisar_operador(
    propuestas: Sequence[Propuesta], canales: Sequence[_Canal], *, almacen: AlmacenPropuestas | None = None
) -> list[str]:
    """Send one message about ``propuestas`` through the operator's channels; returns the channels that took it."""
    if not propuestas:
        return []
    cuerpo = texto_del_aviso(propuestas)
    avisados: list[str] = []
    for canal in canales:
        try:
            canal.enviar(ASUNTO_AVISO, cuerpo)
        except Exception as exc:  # the proposals are on disk either way
            logger.warning("Could not send the proposals on %s: %s: %s", canal.nombre, type(exc).__name__, exc)
        else:
            avisados.append(canal.nombre)
    if almacen is not None and avisados:
        for propuesta in propuestas:
            almacen.guardar(propuesta.model_copy(update={"avisada_por": sorted({*propuesta.avisada_por, *avisados})}))
    return avisados
