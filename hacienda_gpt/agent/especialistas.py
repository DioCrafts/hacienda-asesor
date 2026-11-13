"""The central agent's specialists: one per household module (luz, hipoteca, suscripciones…).

The advisor keeps its tax tools. For every other module the user has switched on it gets ONE tool,
``consultar_<clave>``, which runs that module's specialist: a bounded sub-agent (the same loop as the
team's researchers, :func:`hacienda_gpt.agent.team.run_subagent`) whose tools are ONLY the module's tools
plus ``leer_expediente``, and whose system prompt carries the module's ``instrucciones``.

What the core guarantees, whatever the module does:

* every tool result is registered in the run's evidence ledger as a citable fragment (authority
  ``CALCULADORA``), so the specialist's figures reach the final answer with ``[F#]`` ids that the verifier
  checks like any other source;
* a tool with ``efecto="actua"`` is never executed here: the call becomes a pending action that only runs
  after the owner of the case confirms it (see :mod:`hacienda_gpt.modulos.acciones`);
* a ``prepara`` tool runs, and its result is labelled as a draft;
* a module can only write the facts it declared, validated (``ContextoModulo.registrar``);
* the specialist sees the shared facts and its own module's, never another module's;
* model calls are metered under the purpose ``especialista:<clave>`` against the same question budget;
* a module tool that raises is reported to the model as an error, never crashes the turn.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import logging
from typing import Any

from hacienda_gpt import settings
from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.prompts import MODULES_ADDENDUM, SPECIALIST_SYSTEM_PROMPT
from hacienda_gpt.agent.providers.base import LLMProvider, ToolCall, ToolSpec
from hacienda_gpt.agent.team import SubagentRun, run_subagent
from hacienda_gpt.agent.tools import ExtraTool, Toolbox, ToolOutcome
from hacienda_gpt.modulos.acciones import AccionPendiente, describir
from hacienda_gpt.modulos.contrato import ContextoModulo, HerramientaModulo, Modulo, ResultadoHerramienta
from hacienda_gpt.modulos.perfil import HechoInvalidoError, hechos_para_modulo, perfil_de, registrar_hechos_de_modulo
from hacienda_gpt.modulos.registro import FISCAL
from hacienda_gpt.proactive.jobs import hoy_en_espana

logger = logging.getLogger(__name__)

Emit = Callable[..., None]

READ_PROFILE_TOOL = ToolSpec(
    name="leer_expediente",
    description=(
        "Devuelve los datos del hogar que constan en el expediente del usuario y que puedes usar: los datos "
        "compartidos (comunidad autónoma, estado civil, hijos, ingresos…) y los de tu ámbito. Consúltalo antes "
        "de decir que falta un dato."
    ),
    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
)


def nombre_herramienta(modulo: Modulo) -> str:
    return f"consultar_{modulo.clave}"


def consult_spec(modulo: Modulo) -> ToolSpec:
    return ToolSpec(
        name=nombre_herramienta(modulo),
        description=(
            f"Consulta al especialista en «{modulo.nombre}». {modulo.descripcion.strip()} Devuelve su informe con "
            "fragmentos citables [F#]. Formula una pregunta autosuficiente con los datos del usuario que necesite."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pregunta": {"type": "string", "description": "Lo que necesitas saber, en español."},
                "contexto": {
                    "type": "string",
                    "description": "Datos del usuario o de la conversación que el especialista debe tener en cuenta.",
                },
            },
            "required": ["pregunta"],
        },
    )


def modules_addendum(modulos: Sequence[Modulo]) -> str:
    lineas = "\n".join(f"- `{nombre_herramienta(m)}` — {m.nombre}: {' '.join(m.descripcion.split())}" for m in modulos)
    return MODULES_ADDENDUM.format(especialistas=lineas)


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(content=message, summary=message[:120], is_error=True)


def _render(resultado: ResultadoHerramienta) -> str:
    texto = resultado.contenido.strip()
    if resultado.citas:
        texto += "\nFuentes: " + "; ".join(resultado.citas)
    return texto


class ModuleToolbox:
    """The specialist's tool set: its module's tools plus ``leer_expediente``. Duck-types :class:`Toolbox`."""

    def __init__(self, coordinador: Coordinador, modulo: Modulo) -> None:
        self._coordinador = coordinador
        self._modulo = modulo
        self._herramientas: dict[str, HerramientaModulo] = {h.spec.name: h for h in modulo.herramientas}

    def specs(self) -> list[ToolSpec]:
        return [*(h.spec for h in self._herramientas.values()), READ_PROFILE_TOOL]

    def execute(self, call: ToolCall) -> ToolOutcome:
        if call.arguments_error:
            return _error(f"No se pudo interpretar la llamada: {call.arguments_error}. Reenvíala con JSON válido.")
        if call.name == READ_PROFILE_TOOL.name:
            return self._leer_expediente()
        herramienta = self._herramientas.get(call.name)
        if herramienta is None:
            return _error(f"Herramienta no disponible: {call.name}.")
        if herramienta.efecto == "actua":
            return self._coordinador.proponer_accion(self._modulo, herramienta, call.arguments)
        try:
            resultado = herramienta.ejecutar(call.arguments, self._coordinador.contexto(self._modulo))
        except Exception as exc:  # a module bug is reported to the model, never crashes the turn
            logger.exception("module tool %s failed", call.name)
            return _error(f"La herramienta {call.name} falló: {type(exc).__name__}: {exc}")
        if not isinstance(resultado, ResultadoHerramienta):
            return _error(f"La herramienta {call.name} no devolvió un resultado válido.")
        if resultado.es_error:
            return _error(resultado.contenido or resultado.resumen or f"La herramienta {call.name} devolvió un error.")
        texto = _render(resultado)
        if herramienta.efecto == "prepara":
            texto = "BORRADOR (no se ha enviado, firmado ni contratado nada):\n" + texto
        fragment = self._coordinador.ledger.register(
            text=texto,
            metadata={
                "title": f"{self._modulo.nombre} · {call.name}",
                "source": f"modulo://{self._modulo.clave}/{call.name}/{len(self._coordinador.ledger) + 1}",
                "source_authority": "CALCULADORA",
                "section": "Borrador del módulo" if herramienta.efecto == "prepara" else "Cálculo del módulo",
            },
        )
        return ToolOutcome(
            content=f"[{fragment.fragment_id}] {texto}",
            summary=f"{resultado.resumen or call.name}: {fragment.fragment_id}"[:160],
        )

    def _leer_expediente(self) -> ToolOutcome:
        perfil = hechos_para_modulo(self._coordinador.perfil(), self._modulo)
        return ToolOutcome(
            content=json.dumps({"hechos": perfil}, ensure_ascii=False, default=str),
            summary=f"{len(perfil)} datos del hogar",
        )


@dataclass
class Coordinador:
    """Per-run state of the central agent's specialists (one per user question)."""

    provider: LLMProvider
    ledger: EvidenceLedger
    modulos: Sequence[Modulo]
    emit: Emit
    max_steps: int = field(default_factory=lambda: settings.ESPECIALISTA_MAX_STEPS)
    runs: list[SubagentRun] = field(default_factory=list)
    acciones: list[AccionPendiente] = field(default_factory=list)
    _toolbox: Toolbox | None = None

    @classmethod
    def para(
        cls, provider: LLMProvider, ledger: EvidenceLedger, modulos: Sequence[Modulo] | None, emit: Emit
    ) -> Coordinador | None:
        """A coordinator when at least one non-tax module is on; None keeps the advisor exactly as it was."""
        especialistas = [m for m in modulos or () if m.clave != FISCAL]
        if not especialistas:
            return None
        return cls(provider=provider, ledger=ledger, modulos=especialistas, emit=emit)

    def vincular(self, toolbox: Toolbox) -> None:
        """The advisor's toolbox: its case is the household profile the specialists read and write."""
        self._toolbox = toolbox

    def herramientas(self) -> list[ExtraTool]:
        return [(consult_spec(m), self._consultor(m)) for m in self.modulos]

    def addendum(self) -> str:
        return modules_addendum(self.modulos)

    # ------------------------------------------------------------------ #

    def perfil(self) -> dict[str, Any]:
        return perfil_de(self._toolbox.case if self._toolbox is not None else None)

    def contexto(self, modulo: Modulo) -> ContextoModulo:
        case = self._toolbox.case if self._toolbox is not None else None

        def registrar(valores: Mapping[str, Any]) -> None:
            toolbox = self._toolbox
            if toolbox is None or toolbox.case is None:
                raise HechoInvalidoError("No hay expediente en el que guardar los datos.")
            toolbox.case = registrar_hechos_de_modulo(toolbox.case, modulo, valores, fuente=f"modulo:{modulo.clave}")
            toolbox.case_changed = True

        return ContextoModulo(
            perfil=self.perfil(),
            hoy=hoy_en_espana(),
            case_id=case.case_id if case is not None else None,
            registrar=registrar if case is not None else None,
        )

    def proponer_accion(
        self, modulo: Modulo, herramienta: HerramientaModulo, argumentos: Mapping[str, Any]
    ) -> ToolOutcome:
        case = self._toolbox.case if self._toolbox is not None else None
        if case is None:
            return _error(
                "Esta herramienta actúa fuera de la aplicación y solo puede quedar pendiente de confirmación en un "
                "expediente; esta conversación no tiene expediente. No se ha hecho nada."
            )
        accion = AccionPendiente(
            case_id=case.case_id,
            modulo=modulo.clave,
            herramienta=herramienta.spec.name,
            argumentos=dict(argumentos),
            resumen=describir(modulo.nombre, herramienta.spec.description, argumentos),
        )
        self.acciones.append(accion)
        return ToolOutcome(
            content=(
                "ACCIÓN PENDIENTE DE CONFIRMACIÓN — NO se ha ejecutado. Queda en «Acciones pendientes» del usuario, "
                f"que puede confirmarla o rechazarla: {accion.resumen} Di al usuario que está pendiente de su "
                "confirmación; no digas que se ha hecho."
            ),
            summary=f"acción pendiente de confirmación: {herramienta.spec.name}",
        )

    def _consultor(self, modulo: Modulo) -> Callable[[dict[str, Any]], ToolOutcome]:
        def consultar(arguments: dict[str, Any]) -> ToolOutcome:
            pregunta = arguments.get("pregunta")
            if not isinstance(pregunta, str) or not pregunta.strip():
                return _error("Falta 'pregunta' (texto no vacío).")
            return self._consultar(modulo, pregunta.strip(), str(arguments.get("contexto") or "").strip())

        return consultar

    def _consultar(self, modulo: Modulo, pregunta: str, contexto: str) -> ToolOutcome:
        agente = f"especialista_{modulo.clave}"
        provider = self.provider
        con_proposito = getattr(provider, "con_proposito", None)
        if callable(con_proposito):
            provider = con_proposito(f"especialista:{modulo.clave}")
        antes = len(self.ledger)
        mensaje = f"<consulta>\n{pregunta}\n</consulta>"
        if contexto:
            mensaje += f"\n\n<contexto_del_asistente>\n{contexto}\n</contexto_del_asistente>"
        mensaje = f"Fecha de hoy: {hoy_en_espana().isoformat()}\n\n{mensaje}"
        self.emit("team", stage="specialist", agent=agente, status="start", modulo=modulo.clave)
        run = run_subagent(
            provider,
            agent=agente,
            system=SPECIALIST_SYSTEM_PROMPT.format(nombre=modulo.nombre, instrucciones=modulo.instrucciones.strip()),
            toolbox=ModuleToolbox(self, modulo),
            user_message=mensaje,
            max_steps=self.max_steps,
            emit=self.emit,
        )
        self.runs.append(run)
        self.emit("team", stage="specialist", agent=agente, status="done", modulo=modulo.clave)
        nuevos = self.ledger.all()[antes:]
        if not run.memo:
            return _error(
                f"El especialista en «{modulo.nombre}» no ha podido responder. No des cifras de ese ámbito; dile al "
                "usuario que no has podido consultarlo."
            )
        partes = [f"Informe del especialista en «{modulo.nombre}»:\n{run.memo}"]
        if nuevos:
            partes.append("Fragmentos que puedes citar: " + "; ".join(f"[{f.fragment_id}] {f.title}" for f in nuevos))
        else:
            partes.append("El especialista no ha usado ninguna herramienta: su informe no lleva cifras citables.")
        return ToolOutcome(
            content="\n\n".join(partes),
            summary=f"especialista {modulo.clave}: {len(nuevos)} fragmento(s)",
        )


__all__ = [
    "READ_PROFILE_TOOL",
    "Coordinador",
    "ModuleToolbox",
    "consult_spec",
    "modules_addendum",
    "nombre_herramienta",
]
