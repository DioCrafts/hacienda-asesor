from hacienda_gpt.agent.agent import ABSTAIN_MESSAGE, AgentMode, TaxAgent
from hacienda_gpt.agent.providers.base import ModelTurn, ProviderError
from hacienda_gpt.decision.schemas import CaseState
from .fakes import FakeSearcher, ScriptedProvider, corpus, text_turn, tool_turn, verdict

QUESTION = "¿Qué plazo tengo para reinvertir tras vender mi vivienda habitual?"
SEARCH = ("buscar_fuentes", {"consulta": "exención reinversión vivienda habitual plazo"})
GOOD_ANSWER = "La ganancia puede quedar exenta si reinviertes [F1]. El plazo es de dos años [F2]."


def _agent(provider: ScriptedProvider, **kwargs) -> TaxAgent:
    return TaxAgent(provider=provider, searcher=FakeSearcher(corpus()), **kwargs)


def test_search_then_answer_verified() -> None:
    provider = ScriptedProvider(
        [tool_turn(SEARCH), text_turn(GOOD_ANSWER)],
        [
            verdict(
                "respuesta",
                ("Exención por reinversión", ["F1"], "respaldada"),
                ("Plazo dos años", ["F2"], "respaldada"),
            )
        ],
    )

    result = _agent(provider).run(QUESTION, chat_history=[{"role": "user", "content": "hola"}])

    assert result.mode is AgentMode.VERIFIED
    assert result.answer == GOOD_ANSWER
    assert [c.fragment_id for c in result.citations] == ["F1", "F2"]
    assert result.citations[1].authority == "DGT" and result.citations[1].binding is True
    assert result.warnings == []
    assert (result.steps, result.revisions) == (2, 0)
    assert [(t.tool, t.is_error) for t in result.tool_trace] == [("buscar_fuentes", False)]
    assert result.usage == {"input_tokens": 20, "output_tokens": 10}
    assert (result.provider, result.model) == ("scripted", "scripted-1")

    # The model saw the fragments with ids; the user message carries the date context.
    assert "[F1]" in provider.tool_results[0].content and "[F3]" in provider.tool_results[0].content
    assert provider.started["user_message"].endswith(QUESTION)
    assert "Fecha de hoy" in provider.started["user_message"]
    assert [tool.name for tool in provider.started["tools"]] == ["buscar_fuentes"]

    # The verifier got only the CITED fragments, not everything retrieved.
    verifier_input = provider.json_requests[0]["user"]
    assert "[F1]" in verifier_input and "[F2]" in verifier_input and "[F3]" not in verifier_input


def test_unsupported_claim_triggers_one_revision_then_verifies() -> None:
    bad = "El plazo es de cinco años [F2]. Exención por reinversión [F1]."
    provider = ScriptedProvider(
        [tool_turn(SEARCH), text_turn(bad), text_turn(GOOD_ANSWER)],
        [
            verdict("respuesta", ("Plazo cinco años", ["F2"], "no_respaldada"), ("Exención", ["F1"], "respaldada")),
            verdict("respuesta", ("Exención", ["F1"], "respaldada"), ("Plazo dos años", ["F2"], "respaldada")),
        ],
    )

    result = _agent(provider).run(QUESTION)

    assert result.mode is AgentMode.VERIFIED
    assert result.answer == GOOD_ANSWER
    assert result.revisions == 1
    # The rejection was fed back with the offending claim and the verifier's reason.
    assert "Plazo cinco años" in provider.user_messages[0] and "no lo dice" in provider.user_messages[0]


def test_still_unsupported_after_revision_is_partially_verified_with_warnings() -> None:
    bad = "El plazo es de cinco años [F2]. Exención por reinversión [F1]."
    report = verdict("respuesta", ("Plazo cinco años", ["F2"], "parcial"), ("Exención", ["F1"], "respaldada"))
    provider = ScriptedProvider([tool_turn(SEARCH), text_turn(bad), text_turn(bad)], [report, report])

    result = _agent(provider).run(QUESTION)

    assert result.mode is AgentMode.PARTIALLY_VERIFIED
    assert result.answer == bad
    assert any("Plazo cinco años" in warning for warning in result.warnings)
    assert [claim.verdict.value for claim in result.claims] == ["parcial", "respaldada"]


def test_answer_with_no_supported_claim_is_replaced_by_abstention() -> None:
    invented = "El tipo aplicable es del 19 % [F1]."
    report = verdict("respuesta", ("Tipo del 19 %", ["F1"], "no_respaldada"))
    provider = ScriptedProvider([tool_turn(SEARCH), text_turn(invented)], [report])

    result = _agent(provider, max_revisions=0).run(QUESTION)

    assert result.mode is AgentMode.ABSTAINED
    assert result.answer == ABSTAIN_MESSAGE
    assert result.raw_answer == invented


def test_fabricated_fragment_id_is_flagged_and_never_counts_as_support() -> None:
    answer = "El plazo es de dos años [F2]. Hay una deducción del 15 % [F9]."
    # The verifier claims F9 supports it, but F9 was never retrieved.
    report = verdict("respuesta", ("Plazo dos años", ["F2"], "respaldada"), ("Deducción 15 %", ["F9"], "respaldada"))
    provider = ScriptedProvider([tool_turn(SEARCH), text_turn(answer)], [report])

    result = _agent(provider, max_revisions=0).run(QUESTION)

    assert result.mode is AgentMode.PARTIALLY_VERIFIED
    assert [c.fragment_id for c in result.citations] == ["F2"]
    assert any("F9" in warning for warning in result.warnings)
    assert result.claims[1].verdict.value == "no_respaldada"


def test_clarifying_question_and_abstention_pass_through() -> None:
    ask = ScriptedProvider([text_turn("¿En qué comunidad autónoma resides?")], [verdict("pregunta_aclaratoria")])
    assert _agent(ask).run("¿Qué deducciones tengo?").mode is AgentMode.CLARIFICATION

    abstain = ScriptedProvider(
        [tool_turn(SEARCH), text_turn("No encuentro base suficiente en mis fuentes.")], [verdict("abstencion")]
    )
    result = _agent(abstain).run(QUESTION)
    assert result.mode is AgentMode.ABSTAINED
    assert result.answer == "No encuentro base suficiente en mis fuentes."


def test_verifier_failure_marks_answer_unverified_never_verified() -> None:
    provider = ScriptedProvider([tool_turn(SEARCH), text_turn(GOOD_ANSWER)], [ProviderError("verificador caído")])

    result = _agent(provider).run(QUESTION)

    assert result.mode is AgentMode.UNVERIFIED
    assert any("verificador caído" in warning for warning in result.warnings)
    assert result.revisions == 0


def test_answer_without_normative_claims_is_unverified() -> None:
    provider = ScriptedProvider([text_turn("Depende de muchos factores.")], [verdict("respuesta")])
    assert _agent(provider).run(QUESTION).mode is AgentMode.UNVERIFIED


def test_step_budget_forces_a_final_answer_without_tools() -> None:
    provider = ScriptedProvider(
        [tool_turn(SEARCH), tool_turn(SEARCH), text_turn(GOOD_ANSWER)],
        [verdict("respuesta", ("Exención", ["F1"], "respaldada"), ("Plazo", ["F2"], "respaldada"))],
    )

    result = _agent(provider, max_steps=3).run(QUESTION)

    assert result.mode is AgentMode.VERIFIED
    assert provider.allow_tools_log == [True, True, False]
    assert "agotado" in provider.user_messages[0]


def test_model_that_keeps_calling_tools_past_the_budget_abstains() -> None:
    provider = ScriptedProvider([tool_turn(SEARCH), tool_turn(SEARCH)])

    result = _agent(provider, max_steps=2).run(QUESTION)

    assert result.mode is AgentMode.ABSTAINED
    assert result.tool_trace[-1].is_error  # the out-of-budget call was not executed
    assert provider.json_requests == []


def test_refusal_and_empty_output_abstain() -> None:
    refused = ScriptedProvider([ModelTurn(text="", tool_calls=[], stop_reason="refusal")])
    assert _agent(refused).run(QUESTION).mode is AgentMode.ABSTAINED

    truncated = ScriptedProvider([ModelTurn(text="", tool_calls=[], stop_reason="max_tokens")])
    result = _agent(truncated).run(QUESTION)
    assert result.mode is AgentMode.ABSTAINED and "truncó" in result.warnings[0]


def test_case_tools_update_the_case_and_surface_it_to_the_caller() -> None:
    case = CaseState(case_id="case_1", user_id="u1", jurisdiction="ES", tax_period="2025")
    provider = ScriptedProvider(
        [
            tool_turn(("registrar_hechos", {"hechos": [{"nombre": "residencia_fiscal", "valor": "ES"}]})),
            text_turn("¿Qué tipo de ingresos has tenido?"),
        ],
        [verdict("pregunta_aclaratoria")],
    )

    result = _agent(provider).run("Vivo en Madrid", case=case)

    assert result.updated_case is not None
    assert [(f.name, f.value) for f in result.updated_case.facts] == [("residencia_fiscal", "ES")]
    assert "updated_case" not in result.model_dump()
    assert "Ejercicio del expediente: 2025" in provider.started["user_message"]
    assert "consultar_expediente" in provider.started["system"]
    assert len(provider.started["tools"]) == 4


def test_untouched_case_is_not_returned_for_persistence() -> None:
    case = CaseState(case_id="case_1", user_id="u1", jurisdiction="ES", tax_period="2025")
    provider = ScriptedProvider([text_turn("¿En qué comunidad resides?")], [verdict("pregunta_aclaratoria")])
    assert _agent(provider).run("hola", case=case).updated_case is None
