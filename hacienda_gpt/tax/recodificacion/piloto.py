"""The autopilot: from "the BOE published a norm" to "the parameters say what the law says", with nobody asking.

One run does, in order:

1. **Norms** — for every norm the daily watch (``boe-novedades``) recorded as amending a law the parameters are read
   from, find the blocks of the law it changes: those the BOE's analysis names; once the BOE has consolidated the
   norm, those whose notes in the consolidated text name it ("Se modifica por… Ref. BOE-A-…"); before that, those
   the norm's own text names in its sentences ("Se modifica el artículo 5…"), and only when nothing does, those a
   model LISTS (each with a literal quote the program checks). Every group read from one of those blocks — by its
   article or because its quotes are there — is re-read from the WHOLE current article, even if its check still
   passes: a new condition may sit outside the passages it quotes. The reader may answer that the group does not
   change, which the gate (every old quote still in the text) and the reviewer have to bear out. If the norm is
   consolidated, the groups are re-read from the consolidated text, with the analysis as annotations. If it is not
   — consolidation takes days — they are re-read from the amending norm's own text; such a proposal is never
   applied without a person, and the groups are re-read again from the consolidated text when it arrives. Articles
   the norm ADDS to a community's law, with no group yet, are split into the deductions they hold.
2. **Changes nobody announced** — a law the BOE has updated since the files read it: every group read from a block
   whose wording changed after that reading is re-read the same way, unless a proposal already accepted was made
   from today's text.
3. **Verification** — every group of every parameter file is checked against the current text of its law (no
   model): what no longer matches becomes a target too, whatever caused it.
4. **Re-encoding** — the loop of :mod:`~hacienda_gpt.tax.recodificacion.nucleo` for each target, under the caps of
   the run and of the daily budget; proposals that need nobody are applied when auto-application is on.
5. **Provenance** — a stale law whose every check matches, with nothing open about it (no norm still followed, no
   group left to re-read, no proposal waiting or that did not converge), has today's reading recorded in every
   parameter file that cites it (:mod:`~hacienda_gpt.tax.recodificacion.procedencia`), so that ``params-check``
   stops reporting it.
6. **Report** — what was found, done and left, for the operator.

Steps 1 to 3 and 5 use no model (step 1 only when the norm's text cannot be parsed): without credentials the run
still says which groups must be re-read. The state file makes the run idempotent: a norm is handled once per stage
(unconsolidated, consolidated), and the loop itself never pays twice for the same group against the same text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.tax.params import DATA_DIR
from hacienda_gpt.tax.recodificacion.adaptadores import (
    objetivos_de_desglose,
    objetivos_de_verificacion,
    todos_los_objetivos,
)
from hacienda_gpt.tax.recodificacion.control import (
    _sin_acentos,
    articulos_que_modifica,
    bloques_anotados,
    clave_de_cabecera,
    partir_base,
    preceptos_de_analisis,
)
from hacienda_gpt.tax.recodificacion.nucleo import (
    MAX_ITER,
    AlmacenPropuestas,
    Informe,
    Objetivo,
    Propuesta,
    RecodificacionError,
    SinTextoError,
    clave_de,
    ejecutar,
    leer_texto,
    listar_preceptos,
    resumen_de,
    texto_del_aviso,
)
from hacienda_gpt.tax.recodificacion.procedencia import (
    FechaBoe,
    bloques_del_grupo,
    grupos_de_los_bloques,
    lecturas,
    refrescar_lo_que_se_pueda,
    releido_contra,
)
from hacienda_gpt.tax.verificacion import VERSION_DIARIO, TextoDe

Etapa = Literal["sin_consolidar", "consolidada", "cerrada"]
# What to do with the provenance of a law that is up to date again: record it, only say so, or neither.
Procedencia = Literal["anotar", "simular", "no"]
# How a block of the law was found to be changed by a norm.
VIA_ANALISIS = "análisis"
VIA_ANOTACIONES = "anotaciones del consolidado"
VIA_VERSION = "versión del consolidado"
VIA_TEXTO = "texto de la norma"
VIA_MODELO = "modelo"
# Days a norm may stay unconsolidated before the autopilot stops waiting for it and says so.
DIAS_ESPERA_CONSOLIDACION = 90
ASUNTO_PARTE = "HaciendaGPT: parte del piloto de parámetros"
_ANADE = re.compile(r"\bA[NÑ]ADE\b")
_NUMERO_DE_LEY = re.compile(r"\b\d+/\d{4}\b")
# "Ley 5/2026, de 31 de julio": how the BOE's notes in a consolidated text name the norm that changed a block.
_NOMBRE_CORTO = re.compile(
    r"^((?:Real\s+Decreto-ley|Real\s+Decreto\s+Legislativo|Real\s+Decreto|Decreto-ley|Decreto\s+Legislativo|"
    r"Decreto\s+Foral|Decreto|Ley\s+Org[aá]nica|Ley\s+Foral|Ley|Orden)\s+(?:[A-Z]+/)?\d+/\d{4},\s+de\s+\d{1,2}\s+de\s+"
    r"[a-záéíóú]+)",
    re.IGNORECASE,
)


class _Registro(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NormaModificadora(_Registro):
    """One (amending norm, amended law) pair and how far the autopilot has taken it."""

    norma: str
    ley: str
    fecha: str = ""
    titulo: str = ""
    # What the BOE's analysis says the norm does to the law, one line per reference.
    anotaciones: list[str] = Field(default_factory=list)
    # Blocks of the law the analysis names ("5", "9 bis", "da 61"), and the ones it says are added.
    preceptos: list[str] = Field(default_factory=list)
    anadidos: list[str] = Field(default_factory=list)
    etapa: Etapa = "sin_consolidar"
    # "fichero:ruta" of the groups re-read at each stage.
    grupos_sin_consolidar: list[str] = Field(default_factory=list)
    grupos_consolidada: list[str] = Field(default_factory=list)
    # Articles already split into the deductions they hold, per stage ("sin_consolidar:14 bis").
    desglosados: list[str] = Field(default_factory=list)
    # Every block of the law the norm changes and how it was found: "análisis", "anotaciones del consolidado",
    # "versión del consolidado", "texto de la norma", "modelo".
    articulos: dict[str, list[str]] = Field(default_factory=dict)
    # Stages in which a model was asked to list them (once per stage).
    listados: list[str] = Field(default_factory=list)
    vista_el: str = ""
    consolidada_el: str = ""
    # Why it was closed without re-reading anything, when that is the case.
    cerrada_por: str = ""


class EstadoPiloto(_Registro):
    schema_version: int = 1
    normas: dict[str, NormaModificadora] = Field(default_factory=dict)
    # Fingerprint of the last report sent, so that a day with nothing new is not announced again.
    ultimo_parte: str = ""
    actualizado_el: str = ""


def leer_estado(path: Path) -> EstadoPiloto:
    if not path.exists():
        return EstadoPiloto()
    try:
        return EstadoPiloto.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RecodificacionError(
            f"No se puede leer el estado del piloto ({path}): {str(exc)[:200]}. Corrígelo o bórralo (sin él se "
            "volverán a tratar las normas del registro)."
        ) from exc


def guardar_estado(path: Path, estado: EstadoPiloto) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(estado.model_dump_json(indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


class InformePiloto(_Registro):
    # Norms seen for the first time in this run, and every norm still open.
    normas_nuevas: list[str] = Field(default_factory=list)
    # Norms closed without re-reading: older than the text the parameters were read from, or never consolidated.
    normas_descartadas: list[str] = Field(default_factory=list)
    normas: list[dict[str, Any]] = Field(default_factory=list)
    # Groups whose check against the law fails today, by any cause.
    no_coinciden: list[str] = Field(default_factory=list)
    objetivos: int = 0
    sin_adaptador: list[str] = Field(default_factory=list)
    fuera_de_alcance: list[str] = Field(default_factory=list)
    errores: list[str] = Field(default_factory=list)
    desgloses: list[dict[str, Any]] = Field(default_factory=list)
    # Groups applied from an amending norm's own text that the consolidated text, now published, bears out.
    confirmadas_por_el_consolidado: list[str] = Field(default_factory=list)
    # Groups re-read because a block they are read from changed in a law the BOE updated after the files read it.
    releidos_por_cambio: list[str] = Field(default_factory=list)
    # The provenance step: laws whose reading was recorded, laws up to date, laws blocked and why.
    procedencia: dict[str, Any] | None = None
    # Why no model was called, when it was not.
    omitido: str | None = None
    recodificacion: dict[str, Any] | None = None
    propuestas: list[dict[str, Any]] = Field(default_factory=list)
    avisado_por: list[str] = Field(default_factory=list)

    @property
    def hay_algo(self) -> bool:
        return bool(
            self.normas_nuevas
            or self.normas_descartadas
            or self.no_coinciden
            or self.propuestas
            or self.errores
            or self.objetivos
            or self.confirmadas_por_el_consolidado
            or bool((self.procedencia or {}).get("refrescadas"))
        )


def normas_del_registro(registro: Mapping[str, Any], leyes: Mapping[str, str]) -> list[NormaModificadora]:
    """(norm, law) pairs of the novelties registry whose law is one the parameters are read from.

    ``registro`` is what ``boe-novedades`` keeps (BOE id → the novelty); ``leyes`` maps the BOE id of every source
    law of the parameters to its name. Repeals and amendments alike: either way the groups must be looked at.
    """
    out: list[NormaModificadora] = []
    for norma, novedad in registro.items():
        if not isinstance(novedad, Mapping):
            continue
        por_ley: dict[str, list[Mapping[str, Any]]] = {}
        for referencia in novedad.get("modifica") or []:
            if (
                isinstance(referencia, Mapping)
                and referencia.get("boe_id") in leyes
                and referencia.get("boe_id") != norma
            ):
                por_ley.setdefault(str(referencia["boe_id"]), []).append(referencia)
        for ley, referencias in por_ley.items():
            preceptos: list[str] = []
            anadidos: list[str] = []
            lineas: list[str] = []
            for referencia in referencias:
                accion, texto = str(referencia.get("accion") or ""), str(referencia.get("texto") or "")
                lineas.append(f"{norma}: {accion} {texto}".strip())
                # "MODIFICA los arts. 9 y 13 y AÑADE el art. 9 octies": what follows AÑADE is new.
                antes, hay, despues = texto.partition("AÑADE") if "AÑADE" in texto else (texto, "", "")
                if _ANADE.search(accion) and not hay:
                    antes, despues = "", texto
                preceptos += preceptos_de_analisis(antes) + preceptos_de_analisis(despues)
                anadidos += preceptos_de_analisis(despues)
            out.append(
                NormaModificadora(
                    norma=str(norma),
                    ley=ley,
                    fecha=str(novedad.get("fecha") or ""),
                    titulo=str(novedad.get("titulo") or "")[:200],
                    anotaciones=lineas,
                    preceptos=list(dict.fromkeys(preceptos)),
                    anadidos=list(dict.fromkeys(anadidos)),
                )
            )
    return out


def _id_de(objetivo: Objetivo) -> str:
    return f"{objetivo.fichero}:{'/'.join(objetivo.ruta)}"


def _bases(objetivo: Objetivo) -> set[str]:
    return {partir_base(" ".join(a.lower().split()))[0] for a in objetivo.articulos_ley}


def _ley_citada(nombre: str) -> str:
    match = _NUMERO_DE_LEY.search(nombre)
    return match.group(0) if match else ""


def _plano(texto: str) -> str:
    return " ".join(_sin_acentos(texto).lower().split())


def nombre_corto(titulo: str) -> str:
    """ "Ley 5/2026, de 31 de julio" for "Ley 5/2026, de 31 de julio, de medidas fiscales…"; "" when not recognised."""
    match = _NOMBRE_CORTO.match(" ".join(titulo.split()))
    return match.group(1) if match else ""


def articulos_en_el_consolidado(pagina: str, norma: NormaModificadora) -> dict[str, str]:
    """Blocks of a consolidated law that a norm changed, as the page itself says, and how it says so.

    A block whose latest wording was published on or after the norm's date and whose note names the norm ("Se
    modifica por el art. 2 de la Ley 3/2026… Ref. BOE-A-…") is a "versión del consolidado"; any other block whose
    notes name it (an older version date, a "Téngase en cuenta…" announcing a future wording) is an "anotaciones
    del consolidado".
    """
    from hacienda_gpt.proactive.impacto import articulos_modificados

    referencias = [norma.norma, *([corto] if (corto := nombre_corto(norma.titulo)) else [])]
    planas = [_plano(r) for r in referencias]
    encontrados: dict[str, str] = {}
    try:
        publicada: date | None = date.fromisoformat(norma.fecha[:10])
    except ValueError:
        publicada = None
    if publicada is not None:
        for articulo in articulos_modificados(pagina, desde=publicada - timedelta(days=1)):
            clave = clave_de_cabecera(articulo.titulo)
            if clave is not None and any(p in _plano(articulo.nota) for p in planas):
                encontrados[clave] = VIA_VERSION
    for clave in bloques_anotados(pagina, referencias):
        encontrados.setdefault(clave, VIA_ANOTACIONES)
    return encontrados


def _anotar_via(norma: NormaModificadora, articulo: str, via: str) -> None:
    vias = norma.articulos.setdefault(articulo, [])
    if via not in vias:
        vias.append(via)


def _localizar(
    norma: NormaModificadora,
    pagina: str,
    *,
    consolidada: bool,
    texto_de: TextoDe,
    nombre_ley: str,
    lector: Any,
) -> tuple[int, list[str], str | None]:
    """Find the blocks of the amended law the norm changes and record how (``norma.articulos``).

    Returns (model calls, errors for the report, why something is left for another run or None).
    """
    for precepto in norma.preceptos:
        _anotar_via(norma, precepto, VIA_ANALISIS)
    if consolidada:
        for clave, via in articulos_en_el_consolidado(pagina, norma).items():
            _anotar_via(norma, clave, via)
        return 0, [], None
    ley_citada = _ley_citada(nombre_ley)
    pagina_norma = texto_de(norma.norma, VERSION_DIARIO)
    if not pagina_norma:
        if norma.articulos:
            return 0, [], None
        return 0, [f"{norma.norma}: no se pudo leer su texto para saber qué artículos modifica"], "texto no legible"
    for clave in articulos_que_modifica(pagina_norma, ley=ley_citada):
        _anotar_via(norma, clave, VIA_TEXTO)
    if norma.articulos or "sin_consolidar" in norma.listados:
        return 0, [], None
    if lector is None:
        return 0, [], "sin modelo para listar los artículos que modifica"
    try:
        lista = listar_preceptos(
            lector, pagina=pagina_norma, norma=norma.norma, ley=norma.ley, nombre_ley=nombre_ley, ley_citada=ley_citada
        )
    except ProviderError as exc:
        return 0, [f"{norma.norma}: no se pudo pedir la lista de artículos que modifica: {str(exc)[:200]}"], "error"
    norma.listados.append("sin_consolidar")
    for listado in lista.preceptos:
        _anotar_via(norma, listado.articulo, VIA_MODELO)
    return lista.llamadas_llm, [f"{norma.norma}: {d}" for d in lista.descartados], None


def _enteros(objetivo: Objetivo, tocados: Sequence[str]) -> list[str]:
    """The whole articles a re-read after an amendment shows: the group's own, and those its quotes are in."""
    propios = [partir_base(" ".join(a.lower().split()))[0] for a in objetivo.articulos_ley]
    return list(dict.fromkeys([*propios, *tocados]))


def _resuelto(objetivo: Objetivo, texto_de: TextoDe, almacen: AlmacenPropuestas) -> bool:
    """Whether the latest proposal for this group, against the text it reads today, has been accepted."""
    try:
        _, texto = leer_texto(objetivo, texto_de)
    except SinTextoError:
        return False
    previa = almacen.con_clave(clave_de(objetivo, texto))
    return previa is not None and previa.estado == "aplicada"


def pilotar(
    registro: Mapping[str, Any],
    *,
    leyes: Mapping[str, str],
    comunidades: Mapping[str, str],
    texto_de: TextoDe,
    estado_path: Path,
    almacen: AlmacenPropuestas,
    data_dir: Path = DATA_DIR,
    lector: Any = None,
    revisor: Any = None,
    max_iter: int = MAX_ITER,
    max_grupos: int | None = None,
    auto_aplicar: bool = False,
    hoy: date | None = None,
    leidas: Mapping[str, str] | None = None,
    motivo_sin_modelo: str = "No hay credenciales del proveedor de LLM configurado: no se ha recodificado nada.",
    fecha_boe: FechaBoe | None = None,
    procedencia: Procedencia = "anotar",
) -> tuple[InformePiloto, list[Propuesta]]:
    """One run of the autopilot (see the module's docstring). Returns the report and the proposals made in it.

    ``leyes``: BOE id → name of every source law of the parameters. ``comunidades``: BOE id of a community's IRPF
    law → the community's key (only there can a new article be split into new deductions). ``leidas``: BOE id →
    date (AAAA-MM-DD) of the consolidated text the parameters were read from; a norm published on or before it is
    already in the data and is not re-read (activating the autopilot on an old registry must not re-pay for the
    past). ``lector`` and ``revisor`` are LLM providers, or None to run the deterministic steps alone.
    ``fecha_boe(boe_id)``: the day the BOE's metadata says a law was last updated (what ``params-check`` compares);
    without it, the date its page shows. ``procedencia``: "anotar" records today's reading of the laws that are up to
    date again, "simular" only reports them (texts that were not read from the BOE today), "no" skips the step.
    """
    hoy = hoy or date.today()
    estado = leer_estado(estado_path)
    informe = InformePiloto()
    objetivos: dict[str, Objetivo] = {}
    en_espera: set[str] = set()
    # Norms with something this run could not do (an article waiting to be split): they stay open.
    con_trabajo_pendiente: set[str] = set()
    llamadas_de_desglose = 0

    # ---- 1. Everything, against the law as it reads today ------------------
    verificacion = objetivos_de_verificacion(None, texto_de, data_dir=data_dir)
    fallan = {_id_de(o): o for o in verificacion.objetivos}
    aplicadas = almacen.listar("aplicada")

    # ---- 2. Norms that amend a source law ---------------------------------
    for vista in normas_del_registro(registro, leyes):
        clave = f"{vista.norma}|{vista.ley}"
        norma = estado.normas.get(clave)
        if norma is None:
            norma = estado.normas[clave] = vista.model_copy(update={"vista_el": hoy.isoformat()})
            leida = (leidas or {}).get(vista.ley, "")
            if vista.fecha and leida and vista.fecha[:10] <= leida[:10]:
                norma.etapa = "cerrada"
                norma.cerrada_por = f"publicada el {vista.fecha[:10]}: anterior al texto del que se leyeron los parámetros ({leida[:10]})"
                informe.normas_descartadas.append(f"{vista.norma} → {vista.ley}: {norma.cerrada_por}")
                continue
            informe.normas_nuevas.append(f"{vista.norma} modifica {vista.ley} ({leyes[vista.ley][:80]})")
        elif vista.anotaciones != norma.anotaciones:
            # The BOE publishes or completes its analysis days after the norm.
            norma.anotaciones, norma.preceptos, norma.anadidos = vista.anotaciones, vista.preceptos, vista.anadidos
    for clave, norma in estado.normas.items():
        if norma.etapa == "cerrada":
            continue
        pagina = texto_de(norma.ley, None)
        if not pagina:
            informe.errores.append(f"{norma.norma}: no se pudo leer {norma.ley} para saber si ya está consolidada")
            con_trabajo_pendiente.add(clave)
            continue
        if norma.etapa == "sin_consolidar" and norma.norma in pagina:
            norma.etapa, norma.consolidada_el = "consolidada", hoy.isoformat()
        vista_el = date.fromisoformat(norma.vista_el) if norma.vista_el else hoy
        if norma.etapa == "sin_consolidar" and (hoy - vista_el).days > DIAS_ESPERA_CONSOLIDACION:
            # The consolidated text never named it (a correction, a norm the BOE cites otherwise): stop waiting.
            norma.etapa = "cerrada"
            norma.cerrada_por = f"sin aparecer en el texto consolidado tras {DIAS_ESPERA_CONSOLIDACION} días"
            informe.normas_descartadas.append(f"{norma.norma} → {norma.ley}: {norma.cerrada_por}; revísala a mano")
            continue
        consolidada = norma.etapa == "consolidada"
        llamadas, errores, pendiente = _localizar(
            norma, pagina, consolidada=consolidada, texto_de=texto_de, nombre_ley=leyes[norma.ley], lector=lector
        )
        llamadas_de_desglose += llamadas
        informe.errores += errores
        if pendiente is not None:
            con_trabajo_pendiente.add(clave)
        preceptos = set(norma.articulos)
        suyos = todos_los_objetivos(data_dir=data_dir, boe_id=norma.ley)
        afectados: list[tuple[Objetivo, list[str]]] = []
        for objetivo in suyos.objetivos:
            # Read from one of those blocks: by its own article, or because its quotes are there.
            tocados = sorted(bloques_del_grupo(objetivo, pagina) & preceptos)
            if tocados:
                afectados.append((objetivo, tocados))
        hechos = norma.grupos_consolidada if consolidada else norma.grupos_sin_consolidar
        anotaciones = "\n".join(norma.anotaciones)
        for objetivo, tocados in afectados:
            ident = _id_de(objetivo)
            de_la_norma = [
                p
                for p in aplicadas
                if p.norma_texto == norma.norma and p.fichero == objetivo.fichero and p.ruta == objetivo.ruta
            ]
            if de_la_norma and not consolidada:
                # Already applied from the norm's own text: nothing more to read until the consolidated text arrives
                # (and until then its check against the old consolidated text says nothing).
                en_espera.add(ident)
                continue
            if de_la_norma and ident not in fallan:
                # Applied from the norm's own text, and its quotes are now in the consolidated law: borne out.
                if ident not in hechos:
                    hechos.append(ident)
                    informe.confirmadas_por_el_consolidado.append(objetivo.etiqueta)
                continue
            objetivo.origen = "norma"
            objetivo.anotaciones = anotaciones
            objetivo.motivo = [f"{norma.norma} modifica {', '.join(tocados)}"]
            # The whole article is read again, and "it does not change" is an answer the loop can verify.
            objetivo.articulos_ley = _enteros(objetivo, tocados)
            objetivo.admite_sin_cambios = True
            if not consolidada:
                objetivo.norma_texto, objetivo.ley_citada = norma.norma, _ley_citada(leyes[norma.ley])
                # Until the consolidated text arrives, a check of this group against it says nothing new.
                en_espera.add(_id_de(objetivo))
            objetivos.setdefault(_id_de(objetivo), objetivo)
        # Articles the norm adds, with no group yet: only a community's deductions can be created from nothing.
        comunidad = comunidades.get(norma.ley)
        etapa = "consolidada" if consolidada else "sin_consolidar"
        ya = {b for o in suyos.objetivos for b in _bases(o)}
        for precepto in norma.anadidos:
            marca = f"{etapa}:{precepto}"
            if comunidad is None or precepto in ya or precepto.startswith("d") or marca in norma.desglosados:
                continue
            if lector is None:
                informe.desgloses.append({"norma": norma.norma, "articulo": precepto, "pendiente": motivo_sin_modelo})
                con_trabajo_pendiente.add(clave)
                continue
            try:
                hallazgo, desglose = objetivos_de_desglose(
                    None,
                    comunidad=comunidad,
                    articulo=f"art. {precepto}",
                    lector=lector,
                    texto_de=texto_de,
                    data_dir=data_dir,
                    anotaciones=anotaciones,
                    norma_texto=None if consolidada else norma.norma,
                    ley_citada="" if consolidada else _ley_citada(leyes[norma.ley]),
                )
            except (ProviderError, RecodificacionError) as exc:
                informe.errores.append(f"{norma.norma}, art. {precepto}: no se pudo desglosar: {str(exc)[:200]}")
                con_trabajo_pendiente.add(clave)
                if type(exc).__name__ == "PresupuestoAgotadoError":
                    break
                continue
            llamadas_de_desglose += desglose.llamadas_llm if desglose else 0
            norma.desglosados.append(marca)
            informe.errores += hallazgo.errores
            informe.desgloses.append(
                {
                    "norma": norma.norma,
                    "articulo": precepto,
                    "deducciones_nuevas": [o.ruta[-1].partition("=")[2] for o in hallazgo.objetivos],
                }
            )
            for objetivo in hallazgo.objetivos:
                objetivos.setdefault(_id_de(objetivo), objetivo)
        informe.fuera_de_alcance += [f for f in suyos.fuera_de_alcance if f not in informe.fuera_de_alcance]
        informe.normas.append(
            {
                "norma": norma.norma,
                "ley": norma.ley,
                "etapa": norma.etapa,
                "preceptos": sorted(preceptos, key=_orden_de_bloque),
                # How each block was found: the analysis, the consolidated text, the norm's text, a model.
                "articulos": dict(sorted(norma.articulos.items(), key=lambda par: _orden_de_bloque(par[0]))),
                "grupos": [_id_de(o) for o, _ in afectados],
                "ya_tratados": [g for g in hechos if g in {_id_de(o) for o, _ in afectados}],
                # Why the blocks it changes are not known yet, when they are not.
                **({"pendiente": pendiente} if pendiente is not None and not preceptos else {}),
            }
        )

    # ---- 3. Laws the BOE updated since the files read them -----------------
    estados_de_ley, paginas = lecturas(texto_de, data_dir=data_dir, fecha_boe=fecha_boe)
    propuestas = almacen.listar()
    for estado_de_ley in estados_de_ley.values():
        if not estado_de_ley.desactualizada or not estado_de_ley.modificados:
            continue
        pagina = paginas[estado_de_ley.boe_id]
        for objetivo, tocados in grupos_de_los_bloques(
            estado_de_ley.boe_id, pagina, estado_de_ley.modificados, data_dir=data_dir
        ):
            ident = _id_de(objetivo)
            motivo = (
                f"{', '.join(tocados)}: redacción publicada después de que los parámetros leyeran "
                f"{estado_de_ley.boe_id} ({estado_de_ley.leida_el})"
            )
            if ident in objetivos:
                objetivos[ident].motivo = list(dict.fromkeys([*objetivos[ident].motivo, motivo]))
                continue
            if ident in en_espera or releido_contra(objetivo, pagina, propuestas) is not None:
                continue
            objetivo.origen = "relectura"
            objetivo.motivo = [motivo]
            objetivo.articulos_ley = _enteros(objetivo, tocados)
            objetivo.admite_sin_cambios = True
            objetivos[ident] = objetivo
            informe.releidos_por_cambio.append(f"{objetivo.etiqueta}: {', '.join(tocados)}")

    informe.sin_adaptador = verificacion.sin_adaptador
    informe.errores += verificacion.errores
    informe.fuera_de_alcance += [f for f in verificacion.fuera_de_alcance if f not in informe.fuera_de_alcance]
    for objetivo in verificacion.objetivos:
        informe.no_coinciden.append(f"{objetivo.etiqueta}: {'; '.join(objetivo.motivo[:2])}")
        if _id_de(objetivo) in en_espera:
            continue
        previo = objetivos.get(_id_de(objetivo))
        if previo is not None:
            # Named by a norm AND failing: one target, with both reasons.
            previo.motivo = list(dict.fromkeys([*previo.motivo, *objetivo.motivo]))
        else:
            objetivos[_id_de(objetivo)] = objetivo
    informe.objetivos = len(objetivos)

    # ---- 4. The loop --------------------------------------------------------
    nuevas: list[Propuesta] = []
    resultado: Informe | None = None
    if objetivos and lector is None:
        informe.omitido = motivo_sin_modelo
    elif objetivos:
        resultado, nuevas = ejecutar(
            list(objetivos.values()),
            lector=lector,
            revisor=revisor if revisor is not None else lector,
            texto_de=texto_de,
            almacen=almacen,
            data_dir=data_dir,
            max_iter=max_iter,
            max_objetivos=max_grupos,
            auto_aplicar=auto_aplicar,
            hoy=hoy,
        )
        resultado.llamadas_llm += llamadas_de_desglose
        informe.recodificacion = resultado.model_dump(exclude={"propuestas"})
        informe.errores += resultado.errores
        informe.propuestas = [resumen_de(p) for p in nuevas]
    _anotar_progreso(estado, objetivos, resultado, con_trabajo_pendiente)

    # ---- 5. Provenance of the laws that are up to date again ----------------
    if procedencia != "no":
        abiertas: dict[str, list[str]] = {}
        for norma in estado.normas.values():
            if norma.etapa != "cerrada":
                abiertas.setdefault(norma.ley, []).append(f"la norma {norma.norma} sigue en curso ({norma.etapa})")
        for objetivo in objetivos.values():
            if not _resuelto(objetivo, texto_de, almacen):
                abiertas.setdefault(objetivo.boe_id, []).append(
                    f"{objetivo.etiqueta[:100]}: pendiente de releer o de decidir"
                )
        try:
            hecho = refrescar_lo_que_se_pueda(
                texto_de,
                hoy=hoy,
                data_dir=data_dir,
                almacen=almacen,
                fecha_boe=fecha_boe,
                abiertas=abiertas,
                simular=procedencia == "simular",
            )
        except RecodificacionError as exc:
            informe.errores.append(f"No se pudo anotar la lectura de las leyes al día: {exc}")
        else:
            informe.procedencia = hecho.model_dump(exclude={"fijadas"})
    estado.actualizado_el = datetime.now().isoformat(timespec="seconds")
    guardar_estado(estado_path, estado)
    return informe, nuevas


def _orden_de_bloque(clave: str) -> tuple[int, int, str]:
    """Articles in their numerical order, then the provisions."""
    numero = re.match(r"(\d+)", clave)
    return (0, int(numero.group(1)), clave) if numero else (1, 0, clave)


def _anotar_progreso(
    estado: EstadoPiloto, objetivos: Mapping[str, Objetivo], resultado: Informe | None, pendientes: set[str]
) -> None:
    """Record which groups of each norm now have a proposal for the text of its stage; close what is finished.

    ``resultado`` is None when the loop did not run. A norm is closed once the BOE has consolidated it and every
    group it names has been re-read from the consolidated text (or borne out by it): from there on the daily
    verification is what watches those groups.
    """
    de_normas = {ident: o for ident, o in objetivos.items() if o.origen == "norma"}
    tratados = (
        set()
        if resultado is None
        else {
            ident
            for ident, objetivo in de_normas.items()
            if objetivo.etiqueta not in resultado.sin_procesar
            and not any(error.startswith(objetivo.etiqueta + ":") for error in resultado.errores)
        }
    )
    agotado = resultado is not None and resultado.presupuesto_agotado is not None
    for clave, norma in estado.normas.items():
        if norma.etapa == "cerrada":
            continue
        consolidada = norma.etapa == "consolidada"
        hechos = norma.grupos_consolidada if consolidada else norma.grupos_sin_consolidar
        suyos = [
            ident
            for ident, o in de_normas.items()
            if o.boe_id == norma.ley and (o.norma_texto == norma.norma) != consolidada
        ]
        hechos += [ident for ident in suyos if ident in tratados and ident not in hechos]
        if consolidada and clave not in pendientes and not agotado and all(ident in hechos for ident in suyos):
            norma.etapa = "cerrada"


def texto_del_parte(informe: InformePiloto, nuevas: Sequence[Propuesta]) -> str:
    """The run as a message for the operator (Spanish, plain text)."""
    lineas: list[str] = []
    if informe.normas_nuevas:
        lineas += ["Normas nuevas que modifican leyes de las que salen parámetros:"]
        lineas += [f"- {n}" for n in informe.normas_nuevas]
    if informe.normas_descartadas:
        lineas += ["", "Normas que no se releen:"] + [f"- {n}" for n in informe.normas_descartadas]
    abiertas = [n for n in informe.normas if n["etapa"] != "cerrada"]
    for norma in abiertas:
        estado = (
            "ya consolidada en el BOE"
            if norma["etapa"] == "consolidada"
            else "AÚN SIN CONSOLIDAR: se lee de su propio texto y se repetirá con el consolidado"
        )
        articulos = norma.get("articulos") or {}
        preceptos = ", ".join(
            f"{a} ({', '.join(articulos.get(a, []))})" if articulos.get(a) else a for a in norma["preceptos"]
        )
        desconocidos = "aún no se sabe cuáles" + (f" ({norma['pendiente']})" if norma.get("pendiente") else "")
        lineas.append(
            f"- {norma['norma']} → {norma['ley']} ({estado}); preceptos: {preceptos or desconocidos}; "
            f"grupos afectados: {len(norma['grupos'])}"
        )
    for desglose in informe.desgloses:
        nuevas_deducciones = desglose.get("deducciones_nuevas")
        lineas.append(
            f"- {desglose['norma']}, art. {desglose['articulo']}: "
            + (
                f"deducciones nuevas detectadas: {', '.join(nuevas_deducciones) or 'ninguna'}"
                if nuevas_deducciones is not None
                else f"artículo nuevo sin desglosar ({desglose.get('pendiente')})"
            )
        )
    if informe.releidos_por_cambio:
        lineas += ["", "Grupos que se releen porque su artículo cambió en el texto consolidado desde que se leyeron:"]
        lineas += [f"- {g[:200]}" for g in informe.releidos_por_cambio[:25]]
    if informe.confirmadas_por_el_consolidado:
        lineas += ["", "El texto consolidado ya publicado respalda lo que se aplicó desde la norma modificadora:"]
        lineas += [f"- {g[:200]}" for g in informe.confirmadas_por_el_consolidado]
    if informe.no_coinciden:
        lineas += ["", f"Grupos que hoy no coinciden con el texto de su ley ({len(informe.no_coinciden)}):"]
        lineas += [f"- {g[:300]}" for g in informe.no_coinciden[:25]]
    if informe.sin_adaptador or informe.fuera_de_alcance:
        lineas += ["", "No los puede releer el bucle (revísalos a mano):"]
        lineas += [f"- {g[:300]}" for g in [*informe.sin_adaptador, *informe.fuera_de_alcance][:15]]
    if informe.omitido:
        lineas += ["", f"{informe.omitido} Grupos en espera: {informe.objetivos}."]
    recodificacion = informe.recodificacion or {}
    if recodificacion.get("presupuesto_agotado"):
        lineas += ["", f"Presupuesto agotado: {recodificacion['presupuesto_agotado']}"]
    if recodificacion.get("sin_procesar"):
        lineas += ["", f"Quedan para la próxima ejecución: {len(recodificacion['sin_procesar'])} grupo(s)."]
    procedencia = informe.procedencia or {}
    if procedencia.get("refrescadas"):
        lineas += ["", "Leyes cuya lectura se anota hoy en los ficheros (params-check deja de señalarlas):"]
        lineas += [
            f"- {r['boe_id']}: texto consolidado a {r['consolidado_a']}"
            + (" (simulado: no se ha escrito)" if r.get("simulado") else f" → {', '.join(r.get('ficheros', []))}")
            for r in procedencia["refrescadas"]
        ]
    if procedencia.get("bloqueadas"):
        lineas += ["", "Leyes actualizadas en el BOE cuya lectura NO se anota todavía:"]
        lineas += [
            f"- {ley}: {'; '.join(m[:200] for m in motivos[:3])}" for ley, motivos in procedencia["bloqueadas"].items()
        ]
    if informe.errores:
        lineas += ["", "Errores:"] + [f"- {e[:300]}" for e in informe.errores[:15]]
    if nuevas:
        lineas += ["", "Propuestas de esta ejecución:", texto_del_aviso(nuevas)]
    return "\n".join(lineas).strip() + "\n"


def huella_del_parte(informe: InformePiloto) -> str:
    """Identity of what a report says, so that the same unchanged situation is not sent every day."""
    material = json.dumps(
        [
            sorted(n["norma"] + n["etapa"] for n in informe.normas if n["etapa"] != "cerrada"),
            sorted(informe.no_coinciden),
            sorted(p["id"] for p in informe.propuestas),
            sorted(informe.errores),
            informe.omitido,
            sorted(str(d) for d in informe.desgloses),
            sorted(informe.confirmadas_por_el_consolidado),
            sorted(informe.normas_descartadas),
            sorted(informe.releidos_por_cambio),
            sorted(r["boe_id"] for r in (informe.procedencia or {}).get("refrescadas", [])),
            sorted((informe.procedencia or {}).get("bloqueadas", {})),
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = [
    "ASUNTO_PARTE",
    "articulos_en_el_consolidado",
    "nombre_corto",
    "EstadoPiloto",
    "InformePiloto",
    "NormaModificadora",
    "guardar_estado",
    "huella_del_parte",
    "leer_estado",
    "normas_del_registro",
    "pilotar",
    "texto_del_parte",
]
