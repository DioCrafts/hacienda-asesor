"""The tax module: the original advisor, exposed through the module contract.

Nothing is moved: the tools are the deterministic calculators of :mod:`hacienda_gpt.agent.tools` and the
proactive jobs those of :mod:`hacienda_gpt.proactive.jobs`; this package only adapts them to the contract
so the core can list, describe and run the tax module like any other.

Two things stay special, on purpose, so that nothing the user sees changes:

* the central agent keeps the tax tools (and the corpus search, which needs the vector index) directly in
  its own tool set instead of consulting a specialist;
* the daily inbox runs the tax jobs through :meth:`ModuloFiscal.ejecutar_en_bandeja`, which writes the
  notifications exactly as before (kinds ``plazo`` / ``fin_de_ejercicio``, same keys). ``avisos`` gives the
  same notices through the contract, computed on a throwaway store: the contract's context has no access to
  the notices already written, which the year-end job needs to avoid repeating its request for figures.

Its facts are the core's tax vocabulary (``decision/fact_extractor.EXPECTED_FACT_TYPE``), shared by every
module and edited in «Tu expediente», so ``hechos`` is empty here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from functools import cached_property
import re
from typing import Any

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall, ToolSpec
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE
from hacienda_gpt.decision.schemas import CaseState, Fact
from hacienda_gpt.modulos.contrato import (
    AvisoModulo,
    ContextoModulo,
    FuenteModulo,
    HechoModulo,
    HerramientaModulo,
    ResultadoHerramienta,
)
from hacienda_gpt.proactive.jobs import ejecutar_para_caso
from hacienda_gpt.proactive.store import NotificationStore
from hacienda_gpt.tax.profile import DETALLE_FACT_TYPES, DatosInvalidosError

_FRAGMENT_ID = re.compile(r"\[F\d+\]\s?")
# Tools that need the corpus index, the doctrine graph or a live case: they stay core tools of the agent.
_SOLO_NUCLEO = frozenset(
    {"buscar_fuentes", "consultar_vigencia", "consultar_expediente", "registrar_hechos", "evaluar_obligaciones"}
)


class _SinCorpus:
    """The contract's runs have no vector index: a search is refused, never faked."""

    def search(self, query: str, *, k: int, filters: Any) -> list[Any]:
        raise RuntimeError("la búsqueda en el corpus solo está disponible en el asesor")


def _caso_desde_perfil(perfil: Mapping[str, Any], *, hoy: date, case_id: str | None) -> CaseState:
    """A transient case with the tax facts of the profile (JSON facts back to their canonical text)."""
    from hacienda_gpt.tax.optimizador import detalle_a_texto

    facts: list[Fact] = []
    for nombre, valor in perfil.items():
        tipo = EXPECTED_FACT_TYPE.get(nombre)
        if tipo is None:
            continue  # another module's fact
        if nombre in DETALLE_FACT_TYPES and not isinstance(valor, str):
            try:
                valor = detalle_a_texto(nombre, valor)
            except DatosInvalidosError:
                continue
        facts.append(
            Fact(fact_id=f"fact_{nombre}", name=nombre, value=valor, value_type=tipo, source="perfil", confidence=1.0)
        )
    ahora = datetime.now(UTC)
    periodo = str(perfil.get("periodo_fiscal") or hoy.year)
    return CaseState(
        case_id=case_id or "perfil",
        user_id="perfil",
        jurisdiction="ES",
        tax_period=periodo,
        facts=facts,
        created_at=ahora,
        updated_at=ahora,
    )


def _herramienta(spec: ToolSpec) -> HerramientaModulo:
    def ejecutar(argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
        caso = _caso_desde_perfil(contexto.perfil, hoy=contexto.hoy, case_id=contexto.case_id)
        toolbox = Toolbox(searcher=_SinCorpus(), ledger=EvidenceLedger(), case=caso)
        outcome = toolbox.execute(ToolCall(id="modulo", name=spec.name, arguments=dict(argumentos)))
        # The throwaway ledger's ids mean nothing outside this call.
        return ResultadoHerramienta(
            contenido=_FRAGMENT_ID.sub("", outcome.content),
            resumen=_FRAGMENT_ID.sub("", outcome.summary),
            es_error=outcome.is_error,
        )

    return HerramientaModulo(spec=spec, ejecutar=ejecutar, efecto="lectura")


class ModuloFiscal:
    clave = "fiscal"
    nombre = "Impuestos"
    descripcion = (
        "Impuestos de las personas en España: IRPF (cálculo, deducciones estatales y autonómicas, declaración "
        "conjunta, optimización antes de fin de año), autónomos (modelos 130 y 303), Sociedades, Patrimonio, "
        "Sucesiones y Donaciones, calendario fiscal y normativa, doctrina y jurisprudencia tributarias."
    )
    instrucciones = (
        "Eres el asesor fiscal. Calcula siempre con las calculadoras deterministas y cita su resultado; nunca "
        "calcules impuestos de cabeza ni sumes ahorros calculados por separado."
    )
    hechos: Sequence[HechoModulo] = ()

    @cached_property
    def herramientas(self) -> Sequence[HerramientaModulo]:  # type: ignore[override]
        specs = Toolbox(searcher=_SinCorpus(), ledger=EvidenceLedger()).specs()
        return tuple(_herramienta(spec) for spec in specs if spec.name not in _SOLO_NUCLEO)

    @cached_property
    def fuentes(self) -> Sequence[FuenteModulo]:  # type: ignore[override]
        from hacienda_gpt.tax.fuentes import normas_de_parametros

        return tuple(
            FuenteModulo(
                identificador=norma.boe_id,
                descripcion=norma.norma,
                url=f"https://www.boe.es/buscar/act.php?id={norma.boe_id}",
            )
            for norma in normas_de_parametros()
        )

    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        """The tax notices for this profile (deadlines and year-end plan), through the contract."""
        caso = _caso_desde_perfil(contexto.perfil, hoy=contexto.hoy, case_id=contexto.case_id)
        store = NotificationStore(":memory:")
        try:
            ejecutar_para_caso(caso, hoy=contexto.hoy, store=store)
            notas = store.list(caso.case_id)
        finally:
            store.close()
        return [
            AvisoModulo(
                clave=n.key,
                titulo=n.title,
                cuerpo=n.body,
                gravedad=n.severity,
                acciones=tuple(n.actions),
                fuentes=tuple(n.sources),
                importe=n.amount,
                vence=n.due_date,
                sustituye_prefijo=":".join(n.key.split(":")[:2]) + ":" if n.kind == "fin_de_ejercicio" else None,
            )
            for n in reversed(notas)
        ]

    def ejecutar_en_bandeja(self, case: CaseState, *, hoy: date, store: NotificationStore) -> dict[str, int]:
        """The tax jobs, written straight to the inbox exactly as before the modules existed."""
        return ejecutar_para_caso(case, hoy=hoy, store=store)


MODULO = ModuloFiscal()

__all__ = ["MODULO", "ModuloFiscal"]
