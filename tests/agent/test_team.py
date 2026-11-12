"""Team mode: when it activates, what each member sees, and how it degrades."""

from datetime import date
import threading
from typing import Any

from langchain_core.documents import Document
import pytest

from hacienda_gpt.agent.agent import AgentMode, TeamAgent
from hacienda_gpt.agent.providers.base import ModelTurn, ProviderError, ToolCall, ToolResult, ToolSpec
from hacienda_gpt.agent.team import detectar_tributos
from hacienda_gpt.doctrine.citas import Documento
from hacienda_gpt.doctrine.grafo import Arista, DoctrineGraph
from .fakes import FakeSearcher, verdict

LAW = Document(
    page_content="La adquisición por herencia tributa en el Impuesto sobre Sucesiones; la posterior venta genera ganancia patrimonial en el IRPF.",
    metadata={
        "title": "Ley 29/1987 ISD",
        "source_url": "https://www.boe.es/buscar/act.php?id=BOE-A-1987-28141",
        "source_authority": "BOE",
        "chunk_id": "law",
    },
)
OLD = Document(
    page_content="El valor de adquisición es el declarado en Sucesiones aunque no haya sido comprobado.",
    metadata={
        "title": "Consulta V1234-19",
        "source": "dgt-vinculantes/2019/V1234-19.html",
        "num_consulta": "V1234-19",
        "fecha_resolucion": "2019-05-10",
        "vinculante": True,
        "chunk_id": "old",
    },
)
RULING = Document(
    page_content="El valor de adquisición es el comprobado por la Administración en Sucesiones.",
    metadata={
        "title": "STS de 3 de junio de 2020",
        "source": "ts/STS_1500_2020.html",
        "source_authority": "TS_SALA_TERCERA",
        "ecli": "ES:TS:2020:1500",
        "numero_recurso": "1234/2019",
        "fecha_resolucion": "2020-06-03",
        "chunk_id": "ruling",
    },
)
COMPLEX = "Voy a heredar un piso de mi madre y quiero venderlo: ¿qué pago de Sucesiones, plusvalía municipal e IRPF?"
SIMPLE = "¿Qué plazo tengo para presentar el modelo 303 del IVA?"


def _tool(name: str, **arguments: Any) -> ModelTurn:
    return ModelTurn(text="", tool_calls=[ToolCall(id="c1", name=name, arguments=arguments)], stop_reason="tool_use")


def _text(text: str) -> ModelTurn:
    return ModelTurn(text=text, tool_calls=[], stop_reason="end", usage={"input_tokens": 10, "output_tokens": 5})


class _Conversation:
    def __init__(self, provider: "RoutedProvider", role: str) -> None:
        self.provider, self.role = provider, role

    def step(self, *, allow_tools: bool = True, on_text=None) -> ModelTurn:
        with self.provider.lock:
            return self.provider.turns[self.role].pop(0)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        with self.provider.lock:
            self.provider.tool_results[self.role].extend(results)

    def add_user_message(self, text: str) -> None:
        with self.provider.lock:
            self.provider.user_messages[self.role].append(text)


class RoutedProvider:
    """Scripts keyed by team role, so the test does not depend on thread order."""

    name, model = "routed", "routed-1"
    ROLES = ("asesor", "normativa", "doctrina", "critico")

    def __init__(self, *, plan: Any = None, verdicts: list | None = None, **turns: list[ModelTurn]) -> None:
        self.lock = threading.Lock()
        self.turns = {role: list(turns.get(role, [])) for role in self.ROLES}
        self.plan, self.verdicts = plan, list(verdicts or [])
        self.started: dict[str, list[dict]] = {role: [] for role in self.ROLES}
        self.tool_results: dict[str, list[ToolResult]] = {role: [] for role in self.ROLES}
        self.user_messages: dict[str, list[str]] = {role: [] for role in self.ROLES}
        self.planner_requests: list[dict] = []

    def start(self, *, system: str, tools: list[ToolSpec], history, user_message: str) -> _Conversation:
        if "investigador de normativa" in system:
            role = "normativa"
        elif "investigador de doctrina" in system:
            role = "doctrina"
        elif "revisor adversarial" in system:
            role = "critico"
        else:
            role = "asesor"
        with self.lock:
            if role != "asesor" and not self.turns[role]:
                raise ProviderError(f"{role} sin guion")
            self.started[role].append({"tools": [t.name for t in tools], "user_message": user_message})
        return _Conversation(self, role)

    def complete_json(self, *, system: str, user: str, schema: dict, schema_name: str) -> dict:
        if schema_name == "plan_de_investigacion":
            self.planner_requests.append({"user": user})
            if isinstance(self.plan, Exception):
                raise self.plan
            return self.plan
        return self.verdicts.pop(0)


def _plan(compleja: bool = True) -> dict:
    return {
        "compleja": compleja,
        "motivo": "Intervienen Sucesiones, plusvalía municipal e IRPF.",
        "tributos": ["ISD", "IIVTNU", "IRPF"],
        "encargo_normativa": "Localiza la tributación de la herencia y de la venta posterior.",
        "encargo_doctrina": "Busca el criterio sobre el valor de adquisición del inmueble heredado.",
    }


def _graph() -> DoctrineGraph:
    g = DoctrineGraph(":memory:")
    g.upsert(Documento("DGT:V1234-19", "DGT", fecha=date(2019, 5, 10)), en_corpus=True)
    g.upsert(
        Documento("TS:ECLI:ES:TS:2020:1500", "TS", alias=["TS:REC:1234/2019"], fecha=date(2020, 6, 3)), en_corpus=True
    )
    g.add_edge(
        Arista(
            origen="TS:ECLI:ES:TS:2020:1500", destino="DGT:V1234-19", relacion="supera", confianza=0.85, metodo="llm"
        )
    )
    return g


def _agent(provider: RoutedProvider, docs: list[Document], **kwargs) -> TeamAgent:
    return TeamAgent(provider=provider, searcher=FakeSearcher(docs), **kwargs)


def test_taxes_are_detected_from_everyday_wording() -> None:
    assert detectar_tributos(COMPLEX) == ["IRPF", "Sucesiones y Donaciones", "Plusvalía municipal"]
    assert detectar_tributos(SIMPLE) == ["IVA"]
    assert detectar_tributos("¿Cuándo abre el plazo?") == []


def test_simple_question_never_touches_the_team() -> None:
    provider = RoutedProvider(
        asesor=[_text("Depende de tu periodicidad; ¿es trimestral o mensual?")],
        verdicts=[verdict("pregunta_aclaratoria")],
    )

    result = _agent(provider, [LAW]).run(SIMPLE)

    assert result.mode is AgentMode.CLARIFICATION and result.team is None
    assert provider.planner_requests == []  # one tax → not even the planner is asked
    assert provider.started["normativa"] == provider.started["doctrina"] == provider.started["critico"] == []


def test_planner_can_say_a_multi_tax_question_is_not_complex() -> None:
    provider = RoutedProvider(
        plan=_plan(compleja=False),
        asesor=[_text("¿En qué comunidad resides?")],
        verdicts=[verdict("pregunta_aclaratoria")],
    )
    result = _agent(provider, [LAW]).run(COMPLEX)
    assert len(provider.planner_requests) == 1 and result.team is None and provider.started["normativa"] == []


def test_complex_question_goes_through_researchers_advisor_and_critic() -> None:
    answer = "La herencia tributa en Sucesiones y la venta en el IRPF [F1]."
    provider = RoutedProvider(
        plan=_plan(),
        normativa=[
            _tool("buscar_fuentes", consulta="herencia y venta posterior", tipo_fuente="normativa"),
            _text("Hallazgos: la herencia tributa en ISD y la venta en IRPF [F1]."),
        ],
        doctrina=[
            _tool("buscar_fuentes", consulta="valor de adquisición inmueble heredado"),
            _text("Hallazgos: sin doctrina localizada."),
        ],
        asesor=[_text(answer)],
        critico=[_text("SIN OBJECIONES")],
        verdicts=[verdict("respuesta", ("Tributa en ISD e IRPF", ["F1"], "respaldada"))],
    )
    events: list[dict] = []

    result = _agent(provider, [LAW]).run(COMPLEX, on_event=events.append)

    assert result.mode is AgentMode.VERIFIED and result.answer == answer
    # The advisor cited a fragment only the researcher retrieved: the ledger is shared.
    assert [c.fragment_id for c in result.citations] == ["F1"]
    team = result.team
    assert team.trigger == "planificador" and team.tributos == ["ISD", "IIVTNU", "IRPF"] and team.objections is None
    assert [(s.agent, s.searches, s.produced_memo) for s in team.subagents] == [
        ("investigador_normativa", 1, True),
        ("investigador_doctrina", 1, True),
        ("critico", 0, False),
    ]
    assert {t.agent for t in result.tool_trace} == {"investigador_normativa", "investigador_doctrina"}
    assert result.usage["input_tokens"] >= 30  # advisor + both researchers

    # Researchers and the critic get research tools only: they cannot edit the case or compute.
    for role in ("normativa", "doctrina", "critico"):
        assert provider.started[role][0]["tools"] == ["buscar_fuentes"]
    assert "Localiza la tributación de la herencia" in provider.started["normativa"][0]["user_message"]
    assert COMPLEX in provider.started["doctrina"][0]["user_message"]
    advisor_message = provider.started["asesor"][0]["user_message"]
    assert "<investigacion_previa>" in advisor_message and "Informe del investigador de normativa" in advisor_message
    assert advisor_message.endswith(COMPLEX)
    assert (
        "<borrador>" in provider.started["critico"][0]["user_message"]
        and answer in provider.started["critico"][0]["user_message"]
    )

    stages = [(e["stage"], e["agent"], e["status"]) for e in events if e["type"] == "team"]
    assert stages[:2] == [("planning", "planificador", "start"), ("planning", "planificador", "done")]
    assert ("critic", "critico", "done") in stages
    assert {e["agent"] for e in events if e["type"] == "tool_call"} == {
        "investigador_normativa",
        "investigador_doctrina",
    }


def test_critic_objections_go_back_to_the_advisor_before_verification() -> None:
    objections = (
        "1. El borrador da por hecho que la vivienda era la habitual del causante; si no, no hay reducción [F1]."
    )
    provider = RoutedProvider(
        plan=_plan(),
        normativa=[_text("Hallazgos: nada.")],
        doctrina=[_text("Hallazgos: nada.")],
        asesor=[
            _tool("buscar_fuentes", consulta="herencia"),
            _text("Hay reducción del 95 % [F1]."),
            _text("Hay reducción solo si era la vivienda habitual del causante [F1]."),
        ],
        critico=[_tool("buscar_fuentes", consulta="reducción vivienda habitual causante"), _text(objections)],
        verdicts=[verdict("respuesta", ("Reducción condicionada", ["F1"], "respaldada"))],
    )

    result = _agent(provider, [LAW]).run(COMPLEX)

    assert result.answer.startswith("Hay reducción solo si") and result.mode is AgentMode.VERIFIED
    assert result.team.objections == objections
    assert (
        "inspector de Hacienda" in provider.user_messages["asesor"][0]
        and objections in provider.user_messages["asesor"][0]
    )
    assert len(provider.verdicts) == 0  # verified once, after the rewrite — not the discarded draft
    assert result.team.subagents[-1].agent == "critico" and result.team.subagents[-1].searches == 1


def test_unresolved_doctrine_conflict_escalates_to_the_team(monkeypatch) -> None:
    stale = "El valor de adquisición es el declarado [F1]."
    fixed = "La DGT sostenía el valor declarado [F1], pero el Supremo fijó que es el comprobado [F2]."
    supported = verdict("respuesta", ("Valor declarado", ["F1"], "respaldada"))
    provider = RoutedProvider(
        # First pass (single agent): relies on the overruled consulta twice → partially verified.
        asesor=[
            _tool("buscar_fuentes", consulta="valor adquisición herencia"),
            _text(stale),
            _text(stale),
            _text(fixed),
        ],
        normativa=[_text("Hallazgos: nada relevante.")],
        doctrina=[
            _tool("buscar_fuentes", consulta="valor comprobado sucesiones"),
            _text("El Supremo superó el criterio de la DGT [F2]."),
        ],
        critico=[_text("SIN OBJECIONES")],
        verdicts=[
            supported,
            supported,
            verdict(
                "respuesta",
                ("DGT: valor declarado", ["F1"], "respaldada"),
                ("TS: valor comprobado", ["F2"], "respaldada"),
            ),
        ],
    )

    result = _agent(provider, [OLD, RULING], graph=_graph()).run(
        "¿Qué valor de adquisición uso al vender un piso heredado?"
    )

    assert provider.planner_requests == []  # one tax: escalation came from the conflict, not the planner
    assert result.team.trigger == "conflicto" and result.mode is AgentMode.VERIFIED and result.answer == fixed
    brief = provider.started["doctrina"][0]["user_message"]
    assert "Superada por STS rec. 1234/2019" in brief and "criterio vigente" in brief
    assert "consultar_vigencia" in provider.started["doctrina"][0]["tools"]


def test_team_degrades_when_planner_or_a_researcher_fails() -> None:
    no_plan = RoutedProvider(
        plan=ProviderError("sin red"),
        asesor=[_text("¿Cuánto vale el piso?")],
        verdicts=[verdict("pregunta_aclaratoria")],
    )
    assert _agent(no_plan, [LAW]).run(COMPLEX).team is None  # falls back to the single agent

    # The doctrine researcher has no script → its start() raises; the answer still comes out.
    lame = RoutedProvider(
        plan=_plan(),
        normativa=[_text("Hallazgos: ISD e IRPF [F1].")],
        asesor=[_tool("buscar_fuentes", consulta="herencia"), _text("Tributa en ISD e IRPF [F1].")],
        critico=[_text("SIN OBJECIONES")],
        verdicts=[verdict("respuesta", ("ISD e IRPF", ["F1"], "respaldada"))],
    )
    result = _agent(lame, [LAW]).run(COMPLEX)
    assert result.mode is AgentMode.VERIFIED
    assert [(s.agent, s.produced_memo) for s in result.team.subagents][:2] == [
        ("investigador_normativa", True),
        ("investigador_doctrina", False),
    ]
    assert "investigador de doctrina" not in lame.started["asesor"][0]["user_message"]


def test_modes_never_and_always() -> None:
    never = RoutedProvider(plan=_plan(), asesor=[_text("¿Cuánto vale?")], verdicts=[verdict("pregunta_aclaratoria")])
    assert _agent(never, [LAW], mode="never").run(COMPLEX).team is None and never.planner_requests == []

    always = RoutedProvider(
        plan=_plan(compleja=False),
        normativa=[_text("Hallazgos: nada.")],
        doctrina=[_text("Hallazgos: nada.")],
        asesor=[_text("¿Presentas el IVA trimestral o mensualmente?")],
        critico=[_text("SIN OBJECIONES")],
        verdicts=[verdict("pregunta_aclaratoria")],
    )
    result = _agent(always, [LAW], mode="always").run(SIMPLE)
    assert result.team.trigger == "forzado" and len(always.started["normativa"]) == 1


@pytest.mark.parametrize("parallel", [True, False])
def test_researchers_share_one_ledger_without_id_collisions(monkeypatch, parallel: bool) -> None:
    from hacienda_gpt import settings

    monkeypatch.setattr(settings, "SUBAGENTS_PARALLEL", parallel)
    provider = RoutedProvider(
        plan=_plan(),
        normativa=[_tool("buscar_fuentes", consulta="a"), _text("N [F1].")],
        doctrina=[_tool("buscar_fuentes", consulta="b"), _text("D [F1].")],
        asesor=[_text("Respuesta [F1] [F2] [F3].")],
        critico=[_text("SIN OBJECIONES")],
        verdicts=[verdict("respuesta", ("x", ["F1"], "respaldada"))],
    )
    result = _agent(provider, [LAW, OLD, RULING]).run(COMPLEX)
    # Both researchers retrieved the same three chunks: three ids, not six.
    assert sorted(c.fragment_id for c in result.citations) == ["F1", "F2", "F3"]
