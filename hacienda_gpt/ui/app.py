from datetime import UTC, datetime
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
import requests
import streamlit as st

from hacienda_gpt.decision.fact_extractor import FactExtractor, default_fact_extractor
from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.decision.turn_service import TurnOutcome, process_turn
from hacienda_gpt.llm.chain import answer_with_grounding, create_openai_chain
from hacienda_gpt.llm.grounding import AnswerEnvelope, AnswerMode
from hacienda_gpt.settings import API_BASE_URL, DECISION_DEBUG_MODE, DECISION_STATE_DB_PATH, UI_USE_API
from hacienda_gpt.utils import MissingOpenAIAPIKeyError, configure_logging, get_openai_api_key

# Custom image for the app icon and the assistant's avatar
bot_logo = "https://sede.agenciatributaria.gob.es/static_files/Sede/Tema/Agencia_tributaria/Memorias/2018/Imagenes/Introduccion.jpg"


@st.cache_resource
def load_chain():
    try:
        openai_api_key = get_openai_api_key()
    except MissingOpenAIAPIKeyError:
        st.error("Falta OPENAI_API_KEY en el entorno. Configúrala para usar HaciendaGPT.")
        st.stop()
    return create_openai_chain(openai_api_key=openai_api_key)


@st.cache_resource
def load_case_store() -> SQLiteCaseStateStore:
    return SQLiteCaseStateStore(DECISION_STATE_DB_PATH)


@st.cache_resource
def load_fact_extractor() -> FactExtractor:
    # Same extractor the API uses (OpenAI structured output when a key is set,
    # regex fallback otherwise), so UI and API interpret turns identically.
    return default_fact_extractor()


def _build_chat_history(messages: list[dict[str, str]]) -> list[HumanMessage | AIMessage]:
    history: list[HumanMessage | AIMessage] = []
    for message in messages:
        if message["role"] == "assistant":
            history.append(AIMessage(content=message["content"]))
        elif message["role"] == "user":
            history.append(HumanMessage(content=message["content"]))
    return history


def _ensure_case_id() -> str:
    if "case_id" not in st.session_state:
        st.session_state["case_id"] = f"case_{uuid4().hex}"
    return st.session_state["case_id"]


def _api_create_case_if_needed() -> str:
    if "api_case_id" in st.session_state:
        return st.session_state["api_case_id"]
    payload = {
        "user_id": st.session_state.get("user_id", "streamlit_user"),
        "jurisdiction": "ES",
        "tax_period": str(datetime.now(UTC).year),
    }
    r = requests.post(f"{API_BASE_URL}/cases", json=payload, timeout=20)
    r.raise_for_status()
    case_id = r.json()["case_id"]
    st.session_state["api_case_id"] = case_id
    return case_id


def _api_process_turn(user_input: str, chat_history: list[dict[str, str]] | None = None) -> dict:
    case_id = _api_create_case_if_needed()
    r = requests.post(
        f"{API_BASE_URL}/cases/{case_id}/turn",
        json={"user_input": user_input, "chat_history": chat_history or []},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _persist_turn_local(
    store: SQLiteCaseStateStore,
    case_id: str,
    user_input: str,
    assistant_output: str,
    extractor: FactExtractor | None = None,
    chat_history: list[dict[str, str]] | None = None,
) -> TurnOutcome:
    now = datetime.now(UTC)
    existing = store.get_case(case_id)
    user_id = st.session_state.get("user_id", "streamlit_user")

    # Start from the persisted case (or a fresh one) and run the *same* pipeline
    # the API's /cases/turn uses, via the shared turn_service.process_turn. This
    # keeps the question policy, ask_counts and gave_up_facts in sync between the
    # UI and the backend instead of letting the local path silently skip them.
    case = existing or CaseState(
        case_id=case_id,
        user_id=user_id,
        jurisdiction="ES",
        tax_period=str(now.year),
        created_at=now,
        updated_at=now,
    )

    outcome = process_turn(
        case=case,
        user_input=user_input,
        extractor=extractor or default_fact_extractor(),
        chat_history=chat_history,
    )
    store.save_case(outcome.case_state)
    store.append_audit_event(case_id, {"event_type": "turn_persisted", "user_input": user_input})
    store.append_audit_event(case_id, {"event_type": "assistant_responded", "assistant_output": assistant_output})
    return outcome


def _build_obligation_cards(case_state: CaseState) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for obligation in case_state.obligation_candidates:
        sources = ", ".join(sorted({e.title for e in obligation.evidence_refs if e.title})) or "Sin fuentes"
        missing = ", ".join(obligation.blocking_missing_facts) or "Ninguno"
        cards.append(
            {
                "title": obligation.title,
                "confidence": f"{obligation.confidence:.2f}",
                "risk": obligation.risk_level.value,
                "sources": sources,
                "missing": missing,
            }
        )
    return cards


def _render_obligation_cards(case_state: CaseState) -> None:
    cards = _build_obligation_cards(case_state)
    if not cards:
        return
    st.subheader("Obligaciones candidatas")
    for card in cards:
        with st.container(border=True):
            st.markdown(f"**{card['title']}**")
            col1, col2 = st.columns(2)
            col1.metric("Confianza", card["confidence"])
            col2.markdown(f"**Riesgo:** {card['risk']}")
            st.markdown(f"**Fuentes usadas:** {card['sources']}")
            st.markdown(f"**Dato faltante para confirmar:** {card['missing']}")


_GROUNDING_BANNER = {
    AnswerMode.CITED: ("success", "✅ Respuesta con citas normativas"),
    AnswerMode.UNCITED: (
        "warning",
        "⚠️ Sin citas normativas verificadas: trata esta respuesta como orientativa.",
    ),
    AnswerMode.ABSTAINED: (
        "info",
        "🛑 Abstención: no hay contexto normativo suficiente para responder con seguridad.",
    ),
}


def _render_grounding_banner(envelope: AnswerEnvelope) -> None:
    kind, message = _GROUNDING_BANNER[envelope.mode]
    if kind == "success":
        st.success(message)
    elif kind == "warning":
        st.warning(message)
    else:
        st.info(message)
    if envelope.reason:
        st.caption(envelope.reason)


def _render_citations(envelope: AnswerEnvelope) -> None:
    if not envelope.citations:
        return
    with st.expander(f"Fuentes citadas ({len(envelope.citations)})", expanded=envelope.mode is AnswerMode.CITED):
        for citation in envelope.citations:
            header = citation.title
            if citation.section:
                header = f"{header} — {citation.section}"
            st.markdown(f"**{header}**")
            st.markdown(f"`{citation.locator}`")
            if citation.snippet:
                st.caption(citation.snippet)


def _render_next_questions(questions) -> None:
    if not questions:
        return
    st.subheader("Para afinar la recomendación")
    for question in questions:
        st.markdown(f"- {question.question_text}")


def _render_debug(case_state: CaseState) -> None:
    if not DECISION_DEBUG_MODE:
        return
    with st.expander("Decision Debug (CaseState)", expanded=False):
        st.markdown(f"**case_id**: `{case_state.case_id}`")
        st.markdown("**Facts detectados**")
        if case_state.facts:
            st.json([fact.model_dump(mode="json") for fact in case_state.facts])
        else:
            st.info("No se detectaron facts en este turno.")
        st.markdown("**Facts faltantes**")
        if case_state.missing_facts:
            st.json([missing.model_dump(mode="json") for missing in case_state.missing_facts])
        else:
            st.info("No hay facts faltantes detectados en este turno.")


def main():
    configure_logging()
    st.set_page_config(page_title="HaciendaGPT", page_icon=":bank:", layout="centered")
    st.title("HaciendaGPT")

    chain = load_chain()
    store = load_case_store()
    extractor = load_fact_extractor()
    case_id = _ensure_case_id()

    if "messages" not in st.session_state:
        st.session_state["messages"] = [
            {
                "role": "assistant",
                "content": "¡Hola! ¿Cómo puedo ayudarte con tus preguntas relacionadas con la Agencia Tributaria?",
            }
        ]

    for message in st.session_state.messages:
        with st.chat_message(message["role"], avatar=bot_logo if message["role"] == "assistant" else None):
            st.markdown(message["content"])

    if query := st.chat_input("Preguntáme lo que quieras"):
        # Prior turns (role/content dicts), captured before this query is added,
        # for both the RAG chain and the fact extractor.
        prior_history = [
            {"role": m["role"], "content": m["content"]} for m in st.session_state.messages
        ]
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

        with st.chat_message("assistant", avatar=bot_logo):
            history = _build_chat_history(st.session_state.messages[:-1])
            # The grounding gate needs the full answer and its documents before
            # it can decide the verdict (and may replace the answer with an
            # abstention message), so there is nothing to stream token by token.
            # Render the finalized answer directly instead of faking a typewriter
            # effect that only added latency.
            with st.spinner("Consultando la normativa…"):
                envelope = answer_with_grounding(chain, query=query, chat_history=history)
            response = envelope.answer
            st.markdown(response)
            _render_grounding_banner(envelope)
            _render_citations(envelope)

        st.session_state.messages.append({"role": "assistant", "content": response})

        if UI_USE_API:
            try:
                turn = _api_process_turn(query, chat_history=prior_history)
                st.caption(f"API case_id: {turn['case_id']}")
            except Exception as exc:
                st.error(f"Error llamando API backend: {exc}. Usando fallback local.")
                outcome = _persist_turn_local(
                    store=store,
                    case_id=case_id,
                    user_input=query,
                    assistant_output=response,
                    extractor=extractor,
                    chat_history=prior_history,
                )
                _render_debug(outcome.case_state)
                _render_obligation_cards(outcome.case_state)
                _render_next_questions(outcome.selected_questions)
        else:
            outcome = _persist_turn_local(
                store=store,
                case_id=case_id,
                user_input=query,
                assistant_output=response,
                extractor=extractor,
                chat_history=prior_history,
            )
            _render_debug(outcome.case_state)
            _render_obligation_cards(outcome.case_state)
            _render_next_questions(outcome.selected_questions)


if __name__ == "__main__":
    main()
