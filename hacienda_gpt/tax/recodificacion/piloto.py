"""The autopilot: from "the BOE published a norm" to "the parameters say what the law says", with nobody asking.

One run does, in order:

1. **Norms** — for every norm the daily watch (``boe-novedades``) recorded as amending a law the parameters are read
   from, find the groups that hang from the articles the BOE's analysis names. If the BOE has already consolidated
   the norm into the law, those groups are re-read from the consolidated text, with the analysis as annotations
   (it often carries the effects clause). If it has not — consolidation takes days — they are re-read from the
   amending norm's own text; such a proposal is never applied without a person, and the groups are re-read again
   from the consolidated text when it arrives. Articles the norm ADDS to a community's law, with no group yet, are
   split into the deductions they hold and each one is offered as new.
2. **Verification** — every group of every parameter file is checked against the current text of its law (no
   model): what no longer matches becomes a target too, whatever caused it.
3. **Re-encoding** — the loop of :mod:`~hacienda_gpt.tax.recodificacion.nucleo` for each target, under the caps of
   the run and of the daily budget; proposals that need nobody are applied when auto-application is on.
4. **Report** — what was found, done and left, for the operator.

Steps 1 and 2 use no model: without credentials the run still says which groups must be re-read. The state file
makes the run idempotent: a norm is handled once per stage (unconsolidated, consolidated), and the loop itself never
pays twice for the same group against the same text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
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
from hacienda_gpt.tax.recodificacion.control import partir_base, preceptos_de_analisis
from hacienda_gpt.tax.recodificacion.nucleo import (
    MAX_ITER,
    AlmacenPropuestas,
    Informe,
    Objetivo,
    Propuesta,
    RecodificacionError,
    ejecutar,
    resumen_de,
    texto_del_aviso,
)
from hacienda_gpt.tax.verificacion import TextoDe

Etapa = Literal["sin_consolidar", "consolidada", "cerrada"]
ASUNTO_PARTE = "HaciendaGPT: parte del piloto de parámetros"
_ANADE = re.compile(r"\bA[NÑ]ADE\b")
_NUMERO_DE_LEY = re.compile(r"\b\d+/\d{4}\b")


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
    vista_el: str = ""
    consolidada_el: str = ""


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
    # Why no model was called, when it was not.
    omitido: str | None = None
    recodificacion: dict[str, Any] | None = None
    propuestas: list[dict[str, Any]] = Field(default_factory=list)
    avisado_por: list[str] = Field(default_factory=list)

    @property
    def hay_algo(self) -> bool:
        return bool(
            self.normas_nuevas
            or self.no_coinciden
            or self.propuestas
            or self.errores
            or self.objetivos
            or self.confirmadas_por_el_consolidado
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
    motivo_sin_modelo: str = "No hay credenciales del proveedor de LLM configurado: no se ha recodificado nada.",
) -> tuple[InformePiloto, list[Propuesta]]:
    """One run of the autopilot (see the module's docstring). Returns the report and the proposals made in it.

    ``leyes``: BOE id → name of every source law of the parameters. ``comunidades``: BOE id of a community's IRPF
    law → the community's key (only there can a new article be split into new deductions). ``lector`` and
    ``revisor`` are LLM providers, or None to run the deterministic steps alone.
    """
    hoy = hoy or date.today()
    estado = leer_estado(estado_path)
    informe = InformePiloto()
    objetivos: dict[str, Objetivo] = {}
    en_espera: set[str] = set()
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
            informe.normas_nuevas.append(f"{vista.norma} modifica {vista.ley} ({leyes[vista.ley][:80]})")
        elif vista.anotaciones != norma.anotaciones:
            # The BOE publishes or completes its analysis days after the norm.
            norma.anotaciones, norma.preceptos, norma.anadidos = vista.anotaciones, vista.preceptos, vista.anadidos
    for norma in estado.normas.values():
        if norma.etapa == "cerrada":
            continue
        pagina = texto_de(norma.ley, None)
        if not pagina:
            informe.errores.append(f"{norma.norma}: no se pudo leer {norma.ley} para saber si ya está consolidada")
            continue
        if norma.etapa == "sin_consolidar" and norma.norma in pagina:
            norma.etapa, norma.consolidada_el = "consolidada", hoy.isoformat()
        consolidada = norma.etapa == "consolidada"
        suyos = todos_los_objetivos(data_dir=data_dir, boe_id=norma.ley)
        afectados = [o for o in suyos.objetivos if _bases(o) & set(norma.preceptos)]
        hechos = norma.grupos_consolidada if consolidada else norma.grupos_sin_consolidar
        anotaciones = "\n".join(norma.anotaciones)
        for objetivo in afectados:
            ident = _id_de(objetivo)
            if consolidada and ident not in fallan and ident not in hechos:
                de_la_norma = [
                    p
                    for p in aplicadas
                    if p.norma_texto == norma.norma and p.fichero == objetivo.fichero and p.ruta == objetivo.ruta
                ]
                if de_la_norma:
                    # Applied from the norm's own text, and its quotes are now in the consolidated law: borne out.
                    hechos.append(ident)
                    informe.confirmadas_por_el_consolidado.append(objetivo.etiqueta)
                    continue
            objetivo.origen = "norma"
            objetivo.anotaciones = anotaciones
            objetivo.motivo = [f"{norma.norma} modifica {', '.join(sorted(_bases(objetivo) & set(norma.preceptos)))}"]
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
                "preceptos": norma.preceptos,
                "grupos": [_id_de(o) for o in afectados],
                "ya_tratados": [g for g in hechos if g in {_id_de(o) for o in afectados}],
            }
        )

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

    # ---- 3. The loop --------------------------------------------------------
    nuevas: list[Propuesta] = []
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
        _anotar_progreso(estado, objetivos, resultado, hoy)
    else:
        _anotar_progreso(estado, objetivos, Informe(), hoy)
    estado.actualizado_el = datetime.now().isoformat(timespec="seconds")
    guardar_estado(estado_path, estado)
    return informe, nuevas


def _anotar_progreso(estado: EstadoPiloto, objetivos: Mapping[str, Objetivo], resultado: Informe, hoy: date) -> None:
    """Record which groups of each norm now have a proposal for the text of its stage; close what is finished."""
    tratados = {
        ident
        for ident, objetivo in objetivos.items()
        if objetivo.origen == "norma"
        and objetivo.etiqueta not in resultado.sin_procesar
        and not any(error.startswith(objetivo.etiqueta + ":") for error in resultado.errores)
    }
    for norma in estado.normas.values():
        if norma.etapa == "cerrada":
            continue
        consolidada = norma.etapa == "consolidada"
        hechos = norma.grupos_consolidada if consolidada else norma.grupos_sin_consolidar
        suyos = [
            ident
            for ident, o in objetivos.items()
            if o.origen == "norma" and o.boe_id == norma.ley and (o.norma_texto == norma.norma) != consolidada
        ]
        hechos += [ident for ident in suyos if ident in tratados and ident not in hechos]
        if consolidada and all(ident in hechos for ident in suyos) and resultado.presupuesto_agotado is None:
            # Every group named by the norm has been re-read from the consolidated text (or there was none): from
            # here on the daily verification is what watches them.
            norma.etapa = "cerrada"


def texto_del_parte(informe: InformePiloto, nuevas: Sequence[Propuesta]) -> str:
    """The run as a message for the operator (Spanish, plain text)."""
    lineas: list[str] = []
    if informe.normas_nuevas:
        lineas += ["Normas nuevas que modifican leyes de las que salen parámetros:"]
        lineas += [f"- {n}" for n in informe.normas_nuevas]
    abiertas = [n for n in informe.normas if n["etapa"] != "cerrada"]
    for norma in abiertas:
        estado = (
            "ya consolidada en el BOE"
            if norma["etapa"] == "consolidada"
            else "AÚN SIN CONSOLIDAR: se lee de su propio texto y se repetirá con el consolidado"
        )
        lineas.append(
            f"- {norma['norma']} → {norma['ley']} ({estado}); preceptos: {', '.join(norma['preceptos']) or 'no los nombra'}; "
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
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = [
    "ASUNTO_PARTE",
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
